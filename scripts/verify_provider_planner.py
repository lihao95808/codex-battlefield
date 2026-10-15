"""最小验证：只发一次真实 planner 调用，检查 **provider 通道**与 planner JSON 契约。

不绑定任何厂商：provider / 模型 / 端点都来自隔离 CODEX_HOME 的 `config.toml`
（由 `scripts/use_provider.ps1` 生成）。先用它跑通一次，再开整棵树 ——
这样"通道不通"和"planner 写错"能分开定位。
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from state_paths import workspace_dir  # noqa: E402  (同一目录下的解析器)

# 每工程状态的位置**不在仓库里**了（`docs/spec-state-project-decoupling.md`）：
# 绑定了工程就是 `<状态根>/projects/<key>/`，未绑定才回退 `workspace/`。
# 硬编码 `ROOT/"workspace"` 会让本脚本把 `--cd` 指到一个不存在的地方
# （实测：planner 直接 blocked）。
WORKSPACE = workspace_dir()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用一次真实 planner 调用验证 provider 通道。")
    parser.add_argument("--timeout", type=int, default=180, help="单次 Codex 调用超时秒数。")
    parser.add_argument("--model", default="", help="覆盖模型；默认留空，使用隔离 CODEX_HOME 的 config.toml。")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe_dir = WORKSPACE / "runs" / f"provider-probe-{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    db_path = WORKSPACE / "probe-dbs"
    db_path.mkdir(parents=True, exist_ok=True)
    db_file = db_path / f"provider-probe-{stamp}.sqlite3"

    target = WORKSPACE / "probe-targets" / f"provider-probe-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    (target / "README.md").write_text("# probe target\n", encoding="utf-8")

    os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    os.environ["CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS"] = str(args.timeout)
    # 不硬编码任何厂商的 key 名：隔离 config.toml 的 env_key 由 use_provider.ps1 决定，
    # 而 runner 在该变量缺失时会自动补占位值（runner._ensure_provider_env_key）。
    sys.path.insert(0, str(ROOT / "api"))

    from app.config import get_settings
    from app.database import SessionLocal, init_db
    from app.models import TaskNode
    from app.orchestrator import Orchestrator
    from app.runner import CodexExecRunner
    from app.schemas import PlannerDecision

    init_db()
    base = get_settings()
    settings = base.__class__(
        database_url=f"sqlite:///{db_file.as_posix()}",
        runner_mode="real",
        codex_home=os.environ.get("CODEX_BATTLEFIELD_CODEX_HOME", ""),
        codex_model=args.model,
        codex_reasoning_effort="",
        codex_timeout_seconds=args.timeout,
        codex_ignore_rules=True,
        codex_ignore_user_config=False,
        codex_ephemeral=True,
        max_concurrency=1,
        mock_delay_seconds=0,
        workspace_dir=WORKSPACE,
    )

    summary: dict[str, object] = {
        "ok": False,
        "probe_dir": str(probe_dir),
        "codex_home": settings.codex_home or "(default ~/.codex)",
        "target": str(target),
    }

    orchestrator = Orchestrator(settings)
    goal = "在目标仓库新增 PLAYER_MOVE.md，用一段话说明 2D 横板游戏玩家左右移动的实现要点。"

    constitution_id = await orchestrator.draft_constitution(goal, target_path=str(target), runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    root_id = await orchestrator.start_project(
        goal,
        target_path=str(target),
        runner_mode="real",
        constitution_id=constitution_id,
        auto_run=False,
    )

    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)

    runner = CodexExecRunner(settings)
    envelope = await runner.plan(root)

    summary["root_id"] = root_id
    summary["exit_code"] = envelope.exit_code
    summary["command"] = envelope.command
    summary["logs_path"] = envelope.logs_path
    summary["last_message_path"] = envelope.last_message_path
    summary["prompt_path"] = envelope.prompt_path

    decision = PlannerDecision.model_validate(envelope.payload)
    summary["decision"] = decision.decision
    summary["leaf_certainty"] = decision.leaf_certainty
    summary["reason"] = decision.reason
    summary["children"] = [child.title for child in decision.children]
    summary["risks"] = list(decision.risks)
    summary["why_not_leaf"] = decision.why_not_leaf
    summary["ok"] = envelope.exit_code == 0

    (probe_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
