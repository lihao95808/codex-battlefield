from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from state_paths import workspace_dir  # noqa: E402  (同一目录下的解析器)

# 每工程状态不在仓库里了：绑定了工程就是 <状态根>/projects/<key>/，
# 未绑定才回退 workspace/。
WORKSPACE = workspace_dir()
PROBE_FILE = "GAME_VERTICAL_SLICE_PROBE.md"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)


def run(command: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"命令失败：{' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def make_target_repo(stamp: str) -> Path:
    target = WORKSPACE / "recursive-probe-targets" / f"real-recursive-probe-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    (target / "README.md").write_text("# 2D action game recursive probe\n", encoding="utf-8")
    run(["git", "init"], cwd=target)
    run(["git", "config", "user.email", "codex-battlefield-probe@example.local"], cwd=target)
    run(["git", "config", "user.name", "Codex Battlefield Probe"], cwd=target)
    run(["git", "add", "README.md"], cwd=target)
    run(["git", "commit", "-m", "init recursive probe target"], cwd=target)
    return target


def prepare_imports(db_path: Path, codex_timeout: int) -> None:
    os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS"] = str(codex_timeout)
    sys.path.insert(0, str(ROOT / "api"))


def summarize_tree(orchestrator: Any, db: Any, root_id: str) -> dict[str, Any]:
    tree = orchestrator.get_tree(db, root_id=root_id)
    return {
        "stats": tree["stats"],
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "depth": task["depth"],
                "status": task["status"],
                "worktree_path": task["worktree_path"],
                "converted_from_leaf": task["converted_from_leaf"],
                "leaf_attempts": task["leaf_attempts"],
                "verification_failures": task["verification_failures"],
            }
            for task in tree["tasks"]
        ],
        "constitution": tree["constitution"],
    }


async def run_probe(args: argparse.Namespace) -> int:
    if shutil.which(args.codex_bin) is None:
        print(f"找不到 Codex CLI：{args.codex_bin}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    probe_dir = WORKSPACE / "runs" / f"real-recursive-probe-{stamp}"
    probe_dir.mkdir(parents=True, exist_ok=True)
    db_path = WORKSPACE / "probe-dbs" / f"real-recursive-probe-{stamp}.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    target_repo = make_target_repo(stamp)
    prepare_imports(db_path, args.timeout)

    from app.config import get_settings
    from app.database import SessionLocal, init_db
    from app.models import RunRecord, TaskNode, TaskStatus
    from app.orchestrator import Orchestrator

    init_db()
    settings = get_settings()
    settings = settings.__class__(
        database_url=f"sqlite:///{db_path.as_posix()}",
        runner_mode="real",
        codex_bin=args.codex_bin,
        codex_model=args.model,
        codex_reasoning_effort=args.reasoning_effort,
        codex_timeout_seconds=args.timeout,
        codex_ignore_rules=True,
        codex_ephemeral=True,
        max_concurrency=1,
        mock_delay_seconds=0,
        workspace_dir=WORKSPACE,
    )
    orchestrator = Orchestrator(settings)

    goal = (
        "制作一款 2D 横板过关格斗游戏垂直切片的真实递归探针。"
        "当前验证范围不是完整游戏，而是必须真实跑通递归链路：根节点必须拆分为一个可执行叶子子任务；"
        f"该叶子必须在目标 Git 仓库的工作树中创建 {PROBE_FILE}，内容用中文说明玩家移动、基础攻击、敌人、"
        "一个小关卡、基础 UI、自动化验证计划；父节点随后必须用 acceptance agent 验收这个叶子结果。"
        "不要把本探针扩张成完整商业游戏，也不要拆成多个同级子任务。"
    )

    summary: dict[str, Any] = {
        "ok": False,
        "goal": goal,
        "target_repo": str(target_repo),
        "db_path": str(db_path),
        "probe_dir": str(probe_dir),
    }
    try:
        constitution_id = await orchestrator.draft_constitution(goal, target_path=str(target_repo), runner_mode=args.constitution_runner)
        orchestrator.lock_constitution(constitution_id)
        root_id = await orchestrator.start_project(
            goal,
            target_path=str(target_repo),
            runner_mode="real",
            constitution_id=constitution_id,
            auto_run=False,
        )
        await orchestrator.run_tree(root_id, runner_mode="real")

        with SessionLocal() as db:
            root = db.get(TaskNode, root_id)
            runs = db.query(RunRecord).all()
            tasks = db.query(TaskNode).all()
            run_kinds = {run.kind for run in runs}
            probe_files = [
                Path(task.worktree_path) / PROBE_FILE
                for task in tasks
                if task.worktree_path and (Path(task.worktree_path) / PROBE_FILE).exists()
            ]
            debug_paths_ok = all(
                bool(run.prompt_path and run.last_message_path and run.logs_path and run.workdir and run.constitution_hash)
                for run in runs
            )
            acceptance_runs = [run for run in runs if run.kind == "acceptance" and run.status == "succeeded"]
            max_depth = max((task.depth for task in tasks), default=0)
            summary.update(
                {
                    "root_id": root_id,
                    "root_status": root.status if root else None,
                    "run_kinds": sorted(run_kinds),
                    "run_count": len(runs),
                    "debug_paths_ok": debug_paths_ok,
                    "probe_files": [str(path) for path in probe_files],
                    "acceptance_run_count": len(acceptance_runs),
                    "max_depth": max_depth,
                    "tree": summarize_tree(orchestrator, db, root_id),
                }
            )
            summary["ok"] = (
                root is not None
                and root.status == TaskStatus.DONE.value
                and max_depth >= 1
                and bool(probe_files)
                and {"planner", "worker", "verifier", "acceptance"}.issubset(run_kinds)
                and bool(acceptance_runs)
                and debug_paths_ok
            )
    except Exception as exc:
        summary["error"] = repr(exc)
        (probe_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 1

    (probe_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["ok"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行真实 Codex CLI 递归链路探针。")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI 可执行文件。")
    parser.add_argument("--model", default="", help="真实 Codex CLI 使用的模型；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--reasoning-effort", default="", help="真实 Codex CLI 推理强度；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--timeout", type=int, default=300, help="单次 Codex 调用超时时间，单位秒。")
    parser.add_argument(
        "--constitution-runner",
        choices=["mock", "real"],
        default="mock",
        help="宪法生成使用 mock 还是真实 Codex；默认 mock，探针重点验证真实 planner/worker/verifier/acceptance。",
    )
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run_probe(parse_args())))


if __name__ == "__main__":
    main()
