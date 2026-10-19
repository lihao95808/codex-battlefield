"""Bounded REAL Battlefield 3D task, using the product orchestrator and Codex runner.

No direct model API calls, no submitted reference answer, isolated DB/state/CODEX_HOME.
Seed contains only a locked scene declaration and explicit scene3d contract.
The root must delegate a complete 3D leaf and run real parent acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=40, check=False)
    if proc.returncode:
        raise RuntimeError(f"command failed ({proc.returncode}): {args}")


def setup(args) -> dict:
    home = Path(args.output).resolve()
    home.mkdir(parents=True, exist_ok=args.resume)
    workspace, state, codex_home = home / "workspace", home / "state", home / "codex-home"
    for p in (workspace, state, codex_home):
        p.mkdir(exist_ok=args.resume)
    user_config = home / "user.toml"
    user_config.write_text("# Isolated scene3d probe; no user settings.\n", encoding="utf-8")
    db = home / "probe.sqlite3"
    # Settings/engine bind at import time: reset product-specific inherited state first.
    for key in list(os.environ):
        if key.startswith("CODEX_BATTLEFIELD_"):
            del os.environ[key]
    os.environ.update({
        "CODEX_BATTLEFIELD_CONFIG": str(user_config),
        "CODEX_BATTLEFIELD_STATE_HOME": str(state),
        "CODEX_BATTLEFIELD_WORKSPACE": str(workspace),
        "CODEX_BATTLEFIELD_DATABASE_URL": "sqlite:///" + db.as_posix(),
        "CODEX_BATTLEFIELD_CODEX_HOME": str(codex_home),
        "CODEX_BATTLEFIELD_BLENDER": args.blender,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    catalog = Path(args.model_catalog)
    if not catalog.is_file():
        raise RuntimeError(f"model catalog missing: {catalog}")
    copied_catalog = codex_home / "models.json"
    shutil.copyfile(catalog, copied_catalog)
    codex_home.joinpath("config.toml").write_text(
        'model = "deepseek-flash"\nmodel_provider = "hub"\n'
        'model_reasoning_effort = "none"\n'
        + 'model_catalog_json = ' + json.dumps(copied_catalog.as_posix()) + '\n'
        +
        '[model_providers.hub]\nname = "Local Hub"\n'
        'base_url = "http://127.0.0.1:15721/v1"\nwire_api = "responses"\n'
        'requires_openai_auth = false\n', encoding="utf-8",
    )
    sys.path.insert(0, str(ROOT / "api"))
    target = home / "target"
    if args.resume:
        if not db.is_file() or not (home / "summary.json").is_file():
            raise RuntimeError("resume requires an existing isolated DB and summary")
        return {"home": home, "workspace": workspace, "state": state, "codex_home": codex_home,
                "db": db, "target": target}
    target.mkdir()
    from app.scene3d import castle_declaration
    target.joinpath("scene.declaration.json").write_text(castle_declaration().dumps(), encoding="utf-8")
    target.joinpath("scene3d.contract.json").write_text(json.dumps({
        "version": 1, "declaration": "scene.declaration.json", "parts": "scene.parts.json",
    }, indent=2), encoding="utf-8")
    target.joinpath("README.md").write_text(
        "# Battlefield 3D real task\nSeed has no filled parts or rendered answer.\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "config", "user.name", "Battlefield 3D probe"],
                ["git", "config", "user.email", "scene3d@example.local"], ["git", "add", "."],
                ["git", "commit", "-m", "Seed immutable 3D contract (no answer)"]):
        command(cmd, target)
    return {"home": home, "workspace": workspace, "state": state, "codex_home": codex_home,
            "db": db, "target": target}


async def drive_product_tree(orchestrator, root_id: str, load_root_status) -> None:
    """Keep the product's deferred scheduler alive; a scheduling-pass return is not DONE."""
    await orchestrator.run_tree(root_id, runner_mode="real")
    while True:
        status = load_root_status()
        if status is None or status in {"done", "failed", "blocked", "canceled", "hibernating", "paused"}:
            return
        pending = [t for t in orchestrator._deferred_parent_tasks.values() if not t.done()]
        pending += [t for t in orchestrator._background_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=2, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(orchestrator.settings.scheduler_deferred_retry_seconds)
            await orchestrator.run_tree(root_id, runner_mode="real")


async def execute(args, env) -> int:
    from app.config import Settings
    from app.database import SessionLocal, engine, init_db
    from app.models import EventRecord, RunRecord, TaskNode, TaskStatus
    from app.orchestrator import Orchestrator
    from app.scene3d import Declaration, evaluate

    assert Path(engine.url.database).resolve() == env["db"].resolve(), "wrong database binding"
    init_db()
    settings = Settings(
        state_home=env["state"], workspace_dir=env["workspace"], database_url="sqlite:///" + env["db"].as_posix(),
        project_key="", runner_mode="real", codex_bin=args.codex, codex_home=str(env["codex_home"]),
        codex_model="deepseek-flash", codex_model_planner="deepseek-flash", codex_model_worker="deepseek-flash",
        codex_model_verifier="deepseek-flash", codex_model_acceptance="gpt-6.1-sol",
        codex_model_constitution="gpt-6.1-sol", codex_reasoning_effort="none",
        codex_reasoning_effort_planner="none", codex_reasoning_effort_worker="none",
        codex_reasoning_effort_verifier="none", codex_reasoning_effort_acceptance="low",
        codex_sandbox_planner=args.sandbox, codex_sandbox_worker=args.sandbox,
        codex_sandbox_verifier=args.sandbox, codex_sandbox_acceptance=args.sandbox,
        codex_timeout_seconds=240, codex_idle_timeout_seconds=120, codex_retry_attempts=0,
        codex_ignore_rules=True, codex_ignore_user_config=False, codex_ephemeral=True,
        max_concurrency=1, mock_delay_seconds=0, budget_token_ceiling=12000000,
        worktree_recycle_enabled=False, parent_skill_note_task_enabled=False,
    )
    orchestrator = Orchestrator(settings)
    goal = (
        "在 Blender 中真实交付一个可查看的 3D 石堡。本次是有界任务战场端到端基准，不做无限世界。"
        "根节点必须 decision=decompose，只委派一个完整可执行子任务，然后运行真实 acceptance；"
        "子任务必须保留这里全部交付标准与宪法底线，标题明确含 3D Blender。"
        "子任务 planner 必须 execute+300%，不要继续拆分已给定的有限构件。"
        "工作树已包含 scene.declaration.json 和 scene3d.contract.json，它们是冻结只读契约，不能修改。"
        "worker 根据20个slots生成 scene.parts.json（顶层parts数组，slot/builder/params），"
        "expect 数值严格遵守，每个free参数自行选择域内合法值，tiers/planks/steps/mullions/rails取整数。"
        "严禁调用工具reference命令或reference_parts来抄参考答案，严禁读其它任务产物；"
        "可以读声明与提供的工具帮助，用自己的逻辑写参数和生成脚本。"
        "先使用SCENE3D_TOOL describe --declaration scene.declaration.json读取紧凑槽位与域摘要，"
        "不必展开完整工具源码、28个checks或多次查询help；读取摘要后直接生成自己的参数脚本。"
        "用提示词提供的SCENE3D_TOOL执行validate --json，必须所有28个特征+强制连通通过。"
        "验证通过后，通过相同工具调用真实Blender render，只渲染hero（900x620、24 samples），不要自写渲染器。"
        "在工作树写SCENE3D_DELIVERY.md，说明构件层级、最小细节5cm/最多20个槽位的精度出口、"
        "实际验证命令、实际PNG和GLB相对路径。"
        "changed_files必须逐个列出scene.parts.json、生成脚本（若有）、SCENE3D_DELIVERY.md、"
        "render返回的PNG、GLB与render_cfg.json，父节点要能整合全部产物。"
        "不要把README或截图当作几何校验通过，不创建占位图片、不修改战场应用或校验器。"
        "verifier独立执行相同validate，父acceptance检查整合文件与真实图片，检查该任务是否全部交付。"
        "按项目宪法逐字传递global_acceptance，避免漏掉底线；只做这一个叶子，所有阶段总运行预算约8分钟。"
        "验证与出图成功后只写简洁交付说明并立即返回最终JSON，不要重复整篇源码检查、重渲染或开展无关探索。"
    )
    summary = {"ok": False, "scope": "real orchestrator task tree", "output": str(env["home"]),
               "database": str(env["db"]), "target": str(env["target"]), "sandbox": args.sandbox,
               "models": {"planner": "deepseek-flash", "worker": "deepseek-flash", "verifier": "deepseek-flash",
                          "acceptance": "gpt-6.1-sol"}, "root_id": None,
               "optional_skill_note_tasks": False,
               "flash_reasoning_effort": "none", "acceptance_reasoning_effort": "low",
               "constitution_source": "product mock draft, frozen test contract; not a model-generated constitution"}
    report_path = env["home"] / "summary.json"
    started = time.monotonic()
    root_id = None
    active = None
    if args.resume:
        previous = json.loads(report_path.read_text(encoding="utf-8"))
        root_id = previous["root_id"]
        summary["resumed_from"] = root_id
        summary["previous_phase_seconds"] = previous.get("elapsed_seconds", 0)
        report_path.with_name(f"summary-before-resume-{datetime.now():%H%M%S}.json").write_text(
            json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    try:
        if not root_id:
            cid = await asyncio.wait_for(orchestrator.draft_constitution(goal, str(env["target"]), runner_mode="mock"), timeout=30)
            orchestrator.lock_constitution(cid)
            root_id = await asyncio.wait_for(orchestrator.start_project(
                goal, target_path=str(env["target"]), runner_mode="real", constitution_id=cid, auto_run=False,
            ), timeout=40)
        summary["root_id"] = root_id
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"ROOT_ID={root_id}", flush=True)

        def load_root_status():
            with SessionLocal() as db:
                root = db.get(TaskNode, root_id)
                return root.status if root else None

        active = asyncio.create_task(drive_product_tree(orchestrator, root_id, load_root_status))
        await asyncio.wait_for(active, timeout=max(1, args.seconds - (time.monotonic() - started)))
    except Exception as exc:  # noqa: BLE001 - retain real runtime failure evidence and cancel the task tree
        summary["error"] = repr(exc)
        if active and not active.done():
            active.cancel()
        if root_id:
            try:
                await asyncio.wait_for(orchestrator.cancel_tree(root_id), timeout=20)
            except Exception as cancel_exc:  # noqa: BLE001 - record cancellation failure separately
                summary["cancel_error"] = repr(cancel_exc)
    finally:
        with SessionLocal() as db:
            tasks = db.query(TaskNode).all()
            runs = db.query(RunRecord).all()
            events = db.query(EventRecord).all()
            real = [r for r in runs if r.kind in {"planner", "worker", "verifier", "acceptance"}
                    and r.logs_path and r.prompt_path and r.last_message_path and "--model" in r.command]
            summary["tasks"] = [{"id": t.id, "parent_id": t.parent_id, "depth": t.depth, "title": t.title,
                                  "status": t.status, "worktree": t.worktree_path, "result": json.loads(t.result_json or "{}")}
                                 for t in tasks]
            summary["runs"] = [{"id": r.id, "task_id": r.task_id, "kind": r.kind, "status": r.status,
                                 "exit_code": r.exit_code, "command": r.command, "workdir": r.workdir,
                                 "prompt": r.prompt_path, "logs": r.logs_path, "last_message": r.last_message_path,
                                 "usage": json.loads(r.usage_json or "{}"), "constitution_hash": r.constitution_hash,
                                 "result": json.loads(r.result_json or "{}")}
                                for r in runs]
            summary["scene3d_events"] = [{"type": e.type, "task_id": e.task_id,
                                         "payload": json.loads(e.payload_json)}
                                        for e in events if "scene3d" in e.type]
            root = db.get(TaskNode, root_id) if root_id else None
            kinds = {r.kind for r in real}
            summary["real_run_kinds"] = sorted(kinds)
            summary["real_run_count"] = len(real)
            summary["root_status"] = root.status if root else None
            summary["root_worktree"] = root.worktree_path if root else None
            delivery_target = orchestrator._integration_target_path(db, root) if root else None
            if delivery_target:
                directory = Path(delivery_target)
                summary["delivery_directory"] = str(directory)
                summary["delivery_location_kind"] = "root_worktree" if root.worktree_path else "root_integration_target"
                filled = directory / "scene.parts.json"
                if filled.exists():
                    frozen = Declaration.from_path(env["target"] / "scene.declaration.json")
                    summary["independent_geometry"] = evaluate(frozen, json.loads(filled.read_text(encoding="utf-8"))["parts"])
                    summary["parts_sha256"] = sha(filled)
                images = []
                for image in directory.rglob("*.png"):
                    header = image.read_bytes()[:24]
                    if header[:8] == b"\x89PNG\r\n\x1a\n" and header[12:16] == b"IHDR":
                        images.append({"path": str(image), "sha256": sha(image), "bytes": image.stat().st_size,
                                       "dimensions": list(struct.unpack(">II", header[16:24]))})
                summary["images"] = images
                summary["glbs"] = [str(p) for p in directory.rglob("*.glb") if p.read_bytes()[:4] == b"glTF"]
                summary["declaration_unchanged"] = sha(directory / "scene.declaration.json") == sha(env["target"] / "scene.declaration.json")
                forbidden = []
                for r in real:
                    if r.kind == "worker":
                        text = Path(r.logs_path).read_text(encoding="utf-8", errors="replace")
                        for line in text.splitlines():
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            item = event.get("item", {})
                            executed = item.get("command", "") if isinstance(item, dict) else ""
                            if " reference --" in executed or "reference_parts(" in executed:
                                forbidden.append(r.id)
                                break
                summary["reference_shortcut_runs"] = forbidden
            summary["ok"] = bool(root and root.status == TaskStatus.DONE.value
                and {"planner", "worker", "verifier", "acceptance"} <= kinds
                and all(r.exit_code == 0 for r in real)
                and summary.get("independent_geometry", {}).get("ok")
                and summary.get("images") and summary.get("glbs")
                and summary.get("declaration_unchanged") and not summary.get("reference_shortcut_runs")
                and summary.get("scene3d_events"))
        summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({k: summary.get(k) for k in ("ok", "root_id", "root_status", "elapsed_seconds", "error", "real_run_kinds", "real_run_count", "images")}, ensure_ascii=True, indent=2), flush=True)
    return 0 if summary["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(Path(os.environ.get("TEMP", ".")) / f"battlefield-scene3d-real-{datetime.now():%Y%m%d-%H%M%S}"))
    parser.add_argument("--resume", action="store_true", help="Continue the same isolated root and DB; do not reseed or replace task results")
    parser.add_argument("--seconds", type=int, default=480)
    parser.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"], default="workspace-write")
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--model-catalog", default=str(Path.home() / ".codex" / "models.json"))
    parser.add_argument("--blender", default=r"D:\SOFT\Media\Blender\5.2.0\blender.exe")
    args = parser.parse_args()
    if not 30 <= args.seconds <= 540:
        parser.error("seconds must be 30..540 (bounded ten-minute attempt)")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    env = setup(args)
    return asyncio.run(execute(args, env))


if __name__ == "__main__":
    raise SystemExit(main())
