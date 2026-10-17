"""P5-3 的后半句：**真实模型**的一轮管家对话 → 风险分级 → 预授权闭环。

ROADMAP 里 P5-3 早已把"接缝"钉住了（`web/e2e/l4-flow.spec.ts` 打真后端），但有一条一直
写着"未验"：**预授权 vs 风险分级在真实 steward 动作上的实时闭环**。本地用 `RUNNER=mock`
只能证明"授权到期/安全约束生效"，证明不了"真模型提议的动作也会被同一套闸门管住"。

这个脚本补的就是这一条。它调的是**产品自己的四个部件**（不是另写一套）：

1. `steward.build_context` —— 状态快照 + 能力目录；
2. `runner.steward_chat` —— **真实** Codex CLI（隔离 `CODEX_HOME`，本机 OpenAI 兼容端点）；
3. `steward.parse_chat` + `steward_actions.propose` —— 提议落库，顺带过预授权那道闸；
4. `action_risk` —— 风险分级、`can_pre_authorize`、`make_grant`。

判据（`summary["ok"]`）：

* 真实调用 `exit_code == 0` 且 `StewardChat` 解析通过；
* 至少一条提议动作是**已知能力**（不是 `unclassified`），并且每条都有风险等级；
* **没有预授权**时，那条提议落 `pending`；
* 给了覆盖「该动作 + 该目标」的预授权后，同一提议落 `approved` 且 `result` 以 `[预授权]` 开头；
* **高风险动作永远不能被预授权**（`can_pre_authorize` 常量判断 + `make_grant` 当场拒绝）；
* 执行一步**故意不做**：`run_approved` 会把真实 Codex 拉起来，与本验证目标无关，执行链路
  由 `api/tests/test_steward_actions.py` 覆盖（这里如实记 `execute_skipped_reason`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from state_paths import workspace_dir  # noqa: E402  (同一目录下的解析器)

WORKSPACE = workspace_dir()

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

MESSAGE = (
    "根节点下面有个子节点一直是 blocked，我先不想动整棵树。请直接给出可执行的动作"
    "（从能力目录里挑），说明为什么、以及这一步的风险。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用真实模型验证管家提议 → 风险分级 → 预授权闭环。")
    parser.add_argument("--model", default="", help="覆盖模型；默认用隔离 CODEX_HOME 的 config.toml。")
    parser.add_argument("--timeout", type=int, default=180, help="单次 Codex 调用超时秒数。")
    parser.add_argument("--max-turns", type=int, default=3, help="最多问几轮，取第一份可用的提议。")
    return parser.parse_args()


def _seed(db, models) -> tuple[str, str]:
    """种一棵"根 + 一个 blocked 子节点"的树，让快照有东西可看。"""

    TaskNode, TaskStatus = models
    root = TaskNode(
        id="steward-probe-root",
        title="管家真实验证用的小树",
        description="只为生成状态快照，不会真的跑。",
        depth=0,
        status=TaskStatus.WAITING_CHILDREN.value,
    )
    child = TaskNode(
        id="steward-probe-child",
        parent_id=root.id,
        title="卡住的子节点",
        description="验证预授权闭环用的假节点。",
        depth=1,
        status=TaskStatus.BLOCKED.value,
        error_message="上一次运行超时（这是探针种的假错误）。",
    )
    db.add_all([root, child])
    db.commit()
    return root.id, child.id


async def main_async(args: argparse.Namespace) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe_dir = WORKSPACE / "runs" / f"steward-real-{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    db_path = WORKSPACE / "probe-dbs" / f"steward-real-{stamp}.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS"] = str(args.timeout)
    sys.path.insert(0, str(ROOT / "api"))

    from app import action_risk, steward, steward_actions
    from app.config import get_settings
    from app.database import SessionLocal, init_db
    from app.models import TaskNode, TaskStatus
    from app.runner import make_runner

    init_db()
    base = get_settings()
    settings = base.__class__(
        database_url=f"sqlite:///{db_path.as_posix()}",
        runner_mode="real",
        codex_model=args.model,
        codex_reasoning_effort="",
        codex_timeout_seconds=args.timeout,
        codex_ignore_rules=True,
        codex_ephemeral=True,
        # 只读模式会挡住预授权（那是独立的一道闸）。这里要验的就是预授权，所以关掉它，
        # 并在 summary 里如实记下这个设置。
        steward_read_only=False,
        max_concurrency=1,
        mock_delay_seconds=0,
        workspace_dir=WORKSPACE,
    )
    summary: dict[str, object] = {
        "ok": False,
        "probe_dir": str(probe_dir),
        "db_path": str(db_path),
        "codex_home": settings.codex_home or "(default ~/.codex)",
        "codex_model": args.model or "(config.toml)",
        "steward_read_only": settings.steward_read_only,
        "turns": [],
    }

    with SessionLocal() as db:
        root_id, child_id = _seed(db, (TaskNode, TaskStatus))
        summary["root_id"] = root_id
        summary["child_id"] = child_id

    runner = make_runner(settings)
    usable: dict[str, object] | None = None
    for turn in range(1, max(1, args.max_turns) + 1):
        with SessionLocal() as db:
            history_text = steward.render_history(steward.recent_messages(db))
            status_text, caps_text = steward.build_context(db, settings, MESSAGE, history_text)
            envelope = await runner.steward_chat(status_text, caps_text, history_text, MESSAGE)
            chat = steward.parse_chat(envelope.payload)
            proposed = [item.model_dump() for item in chat.proposed_actions]
            known = steward_actions.known_actions()
            graded = [
                {
                    "action": item.get("action", ""),
                    "target": item.get("target", ""),
                    "known": item.get("action", "") in known,
                    "risk": action_risk.risk_of(str(item.get("action", ""))),
                    "pre_authorizable": action_risk.can_pre_authorize(str(item.get("action", ""))),
                }
                for item in proposed
            ]
            queued, rejected = steward_actions.propose(db, proposed, read_only=settings.steward_read_only)
            rows = [
                {
                    "id": row.id,
                    "action": row.action,
                    "target": row.target,
                    "status": row.status,
                    "result": row.result,
                }
                for row in queued
            ]
            summary["turns"].append(
                {
                    "turn": turn,
                    "exit_code": envelope.exit_code,
                    "reply": chat.reply,
                    "situation": chat.situation,
                    "needs_user_input": chat.needs_user_input,
                    "confidence": chat.confidence,
                    "graded": graded,
                    "queued": rows,
                    "rejected": rejected,
                }
            )
        if usable is None:
            candidates = [item for item in rows if item["status"] == steward_actions.PENDING]
            first_known = next((g for g in graded if g["known"]), None)
            if candidates and first_known is not None:
                usable = {"row": candidates[0], "graded": first_known}
                break

    # —— 闭环：给模型**自己提议的那个动作**发一份预授权，再走一次同一段代码 ——
    if usable is not None:
        row = usable["row"]
        action, target = row["action"], row["target"]
        grant = action_risk.make_grant(
            actions=[action],
            targets=[target] if target else None,
            minutes=10,
            granted_by="verify_steward_real_turn",
            reason="P5-3 真实闭环验证（10 分钟后自动失效）",
        )
        steward_actions.pre_authorization.set(grant)
        with SessionLocal() as db:
            queued, rejected = steward_actions.propose(
                db,
                [{"action": action, "target": target, "why": "复验预授权", "risk": row.get("risk", "")}],
                read_only=settings.steward_read_only,
            )
            granted_rows = [
                {"id": item.id, "action": item.action, "status": item.status, "result": item.result}
                for item in queued
            ]
            summary["granted"] = {
                "grant": {
                    "actions": list(grant.actions),
                    "targets": list(grant.targets),
                    "expires_at": grant.expires_at.isoformat(),
                    "granted_by": grant.granted_by,
                },
                "queued": granted_rows,
                "rejected": rejected,
            }
        steward_actions.pre_authorization.clear()

    # —— 高风险：预授权永远不适用（常量判断，不依赖模型）——
    high_action = next((name for name in sorted(action_risk.RISK_BY_ACTION) if not action_risk.can_pre_authorize(name)), "")
    high_checks: dict[str, object] = {"action": high_action, "can_pre_authorize": action_risk.can_pre_authorize(high_action) if high_action else None}
    if high_action:
        try:
            action_risk.make_grant(
                actions=[high_action], minutes=10, granted_by="verify_steward_real_turn", reason="应当被拒"
            )
            high_checks["make_grant_rejected"] = False
        except action_risk.RiskError as exc:
            high_checks["make_grant_rejected"] = True
            high_checks["make_grant_error"] = str(exc)
    summary["high_risk"] = high_checks
    summary["execute_skipped_reason"] = (
        "run_approved 会把真实 Codex 拉起来；执行链路由 api/tests/test_steward_actions.py 覆盖，"
        "这里只验证“提议 → 分级 → 预授权”三段的真实闭环。"
    )

    # —— 判定 ——
    pending_ok = False
    approved_ok = False
    if isinstance(usable, dict):
        pending_ok = usable["row"]["status"] == steward_actions.PENDING
    granted = summary.get("granted") or {}
    queued = granted.get("queued") or []
    approved_ok = bool(queued) and queued[0]["status"] == steward_actions.APPROVED and str(queued[0]["result"]).startswith("[预授权]")
    graded_ok = any(
        turn.get("graded") and all(entry["risk"] for entry in turn["graded"]) for turn in summary["turns"]
    )
    summary["ok"] = bool(
        summary["turns"]
        and all(turn["exit_code"] == 0 for turn in summary["turns"])
        and isinstance(usable, dict)
        and pending_ok
        and approved_ok
        and graded_ok
        and summary["high_risk"].get("can_pre_authorize") is False
        and summary["high_risk"].get("make_grant_rejected") is True
    )

    (probe_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["ok"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()
