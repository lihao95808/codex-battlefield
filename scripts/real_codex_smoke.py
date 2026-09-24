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
WORKSPACE = ROOT / "workspace"
EXPECTED_TEXT = "codex battlefield real smoke ok"


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
    target = WORKSPACE / "smoke-targets" / f"real-codex-smoke-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    (target / "README.md").write_text("# Real Codex smoke target\n", encoding="utf-8")
    run(["git", "init"], cwd=target)
    run(["git", "config", "user.email", "codex-battlefield-smoke@example.local"], cwd=target)
    run(["git", "config", "user.name", "Codex Battlefield Smoke"], cwd=target)
    run(["git", "add", "README.md"], cwd=target)
    run(["git", "commit", "-m", "init smoke target"], cwd=target)
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


async def run_smoke(args: argparse.Namespace) -> int:
    if shutil.which(args.codex_bin) is None:
        print(f"找不到 Codex CLI：{args.codex_bin}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    smoke_dir = WORKSPACE / "runs" / f"real-codex-smoke-{stamp}"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    db_path = WORKSPACE / "smoke-dbs" / f"real-codex-smoke-{stamp}.sqlite3"
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
        "在目标 Git 仓库中创建 BATTLEFIELD_SMOKE.txt，内容必须严格等于 "
        f"{EXPECTED_TEXT}，不要添加尾随换行；完成后验证文件内容和字节长度。"
    )

    summary: dict[str, Any] = {
        "ok": False,
        "goal": goal,
        "target_repo": str(target_repo),
        "db_path": str(db_path),
        "smoke_dir": str(smoke_dir),
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
            smoke_files = [
                Path(task.worktree_path) / "BATTLEFIELD_SMOKE.txt"
                for task in tasks
                if task.worktree_path and (Path(task.worktree_path) / "BATTLEFIELD_SMOKE.txt").exists()
            ]
            matching_files = [path for path in smoke_files if path.read_text(encoding="utf-8") == EXPECTED_TEXT]
            run_kinds = {run.kind for run in runs}
            debug_paths_ok = all(
                bool(run.prompt_path and run.last_message_path and run.logs_path and run.workdir and run.constitution_hash)
                for run in runs
            )
            root_done = root is not None and root.status == TaskStatus.DONE.value
            summary.update(
                {
                    "root_id": root_id,
                    "root_status": root.status if root else None,
                    "run_kinds": sorted(run_kinds),
                    "run_count": len(runs),
                    "debug_paths_ok": debug_paths_ok,
                    "smoke_files": [str(path) for path in smoke_files],
                    "matching_files": [str(path) for path in matching_files],
                    "tree": summarize_tree(orchestrator, db, root_id),
                }
            )
            summary["ok"] = (
                root_done
                and bool(matching_files)
                and {"planner", "worker", "verifier"}.issubset(run_kinds)
                and debug_paths_ok
            )
    except Exception as exc:
        summary["error"] = repr(exc)
        (smoke_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 1

    (smoke_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["ok"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行真实 Codex CLI 文件修改烟测。")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI 可执行文件。")
    parser.add_argument("--model", default="", help="真实 Codex CLI 使用的模型；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--reasoning-effort", default="", help="真实 Codex CLI 推理强度；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--timeout", type=int, default=300, help="单次 Codex 调用超时时间，单位秒。")
    parser.add_argument(
        "--constitution-runner",
        choices=["mock", "real"],
        default="mock",
        help="宪法生成使用 mock 还是真实 Codex；默认 mock，烟测重点验证真实 planner/worker/verifier。",
    )
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(run_smoke(parse_args())))


if __name__ == "__main__":
    main()
