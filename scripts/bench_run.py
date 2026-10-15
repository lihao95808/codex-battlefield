"""能力基准：把固定任务集跑成一份可对照的报告。

**它回答的问题**：换模型（如 DeepSeek flash）、换 harness、改提示词之后，这套编排是变强还是变弱。
现在没有这个数字，所有"优化"都不可反驳（`docs/spec-capability-benchmark.md` §1）。

三条口径（照 spec 来，别在这里改）：

1. **成功率只由外部判据算**：树自称 `done` 只进 `tree_claimed_done`；两者不一致的次数就是
   `overconfident`——"它会不会谎报完成"，这是头号指标。
2. **读不到的指标记 `null`，不填 0**（`bench.normalize_metrics` 保证）。
3. **`unsolvable` 任务一旦声称完成 → 整档判失败**（进程退出码非 0），因为那不是"任务失败"，
   而是**这一档不可信**。

用法
  python scripts/bench_run.py --provider mock
  python scripts/bench_run.py --provider mock --tasks add-multiply,fix-subtract
  python scripts/bench_run.py --provider real --codex-bin codex --timeout 900
  python scripts/bench_run.py --compare bench/reports/a.json bench/reports/b.json

为什么不走 HTTP：本机实测 8765 曾被无关进程占着，基准不该依赖端口。这里直接进程内驱动
`Orchestrator`（与 `real_codex_smoke.py` 同一条路径：draft → lock → start(auto_run=False) → run_tree）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCH_DIR = ROOT / "bench"
TASKS_DIR = BENCH_DIR / "tasks"
FIXTURES_DIR = BENCH_DIR / "fixtures"
REPORTS_DIR = BENCH_DIR / "reports"
RUNS_DIR = BENCH_DIR / "runs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", default="mock", choices=["mock", "real"], help="被测通道")
    parser.add_argument("--tasks", default="", help="逗号分隔的任务 id；默认全部")
    parser.add_argument("--constitution-runner", default="mock", choices=["mock", "real"], help="宪法生成走哪条通道")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--timeout", type=int, default=900, help="单次 Codex 调用的硬超时（秒）")
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=0,
        help="覆盖任务自带的墙钟上限（秒）；0=用任务里的值。前台跑短测时用它，**即使没跑完也会写报告**",
    )
    parser.add_argument("--stamp", default="", help="本次跑的标签（默认 UTC 时间戳）")
    parser.add_argument("--compare", nargs="*", default=None, help="只做对照：给若干报告 json 路径")
    parser.add_argument(
        "--debug-stacks",
        type=int,
        default=0,
        help="每 N 秒把**所有线程/协程的栈**打到 stderr（诊断「卡住」用；0=关）",
    )
    parser.add_argument(
        "--heartbeat",
        type=int,
        default=60,
        help="每 N 秒刷新一次中途报告（带 partial=true；最小 15s）。它是「被外部掐掉」与「自己挂住」的区分器",
    )
    parser.add_argument(
        "--hard-stop",
        type=int,
        default=0,
        help=(
            "线程级硬停（秒；0=关）：到点先落线程栈与 partial 报告，再 os._exit(3)。"
            "**它绕开事件循环**，因为循环卡住时 asyncio 定时器（含 --task-timeout）不会触发"
        ),
    )
    return parser.parse_args()


def load_tasks(selection: str) -> list[dict[str, Any]]:
    from app import bench  # 延迟导入：先设好环境变量再导 app（见 prepare_imports）

    paths = sorted(TASKS_DIR.glob("*/task.json"))
    if not paths:
        raise SystemExit(f"找不到任务定义：{TASKS_DIR}")
    tasks = [bench.load_task(path) for path in paths]
    if selection.strip():
        wanted = [item.strip() for item in selection.split(",") if item.strip()]
        unknown = [item for item in wanted if item not in {task["id"] for task in tasks}]
        if unknown:
            raise SystemExit(f"未知任务 id：{unknown}")
        tasks = [task for task in tasks if task["id"] in wanted]
    return tasks


def prepare_imports(db_path: pathlib.Path) -> None:
    """**必须在 import app 之前**设库地址：`app.database` 在导入时就绑定了 engine。"""
    os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ.setdefault("CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB", "0")
    sys.path.insert(0, str(ROOT / "api"))


def copy_fixture(task: dict[str, Any], target: pathlib.Path) -> None:
    """把 fixture 复制成本次任务的目标工程，并**让它自成一个 git 仓库**。

    为什么必须自成仓库（实测踩过）：目标目录若只是"位于另一个 git 仓库内部"的普通目录，
    `git rev-parse --is-inside-work-tree` 在它身上也为真，于是 `_assign_worktree` 会对外层仓库执行
    `git worktree add` —— 被测 agent 拿到的**是整个控制塔仓库的检出**（它在里面看到 `bench/fixtures/**`，
    就把它当工程改了），基准测的东西当场失真。自成仓库之后，worktree 只会是这份 fixture 的副本。
    """
    source = FIXTURES_DIR / str(task.get("fixture") or "")
    if not source.is_dir():
        raise SystemExit(f"{task['id']}: 找不到 fixture：{source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache"))
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=bench@example.local", "-c", "user.name=Bench", "commit", "-q", "-m", "fixture baseline"],
    ):
        result = subprocess.run(command, cwd=str(target), capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"{task['id']}: 目标工程初始化失败（{' '.join(command)}）：{result.stderr.strip()}")


def collect_metrics(db: Any, root_id: str, *, wall_clock_seconds: float) -> dict[str, Any]:
    """从库里**如实**取数：取不到就 `None`（`bench.normalize_metrics` 会保留 `None`，不填 0）。

    **只算这棵树的子树**：基准一批会跑好几个任务，若按"库里所有节点"统计，任务 2 的指标会把任务 1 的
    也算进去（第一版就是这个错，`agent_starts` 报出 67 万）。子树遍历带 `seen` 集合，顺带免疫脏数据里的环。
    """
    from sqlalchemy import select

    from app.models import RunRecord, TaskNode
    from app.usage import summarize_usages
    from app.utils import loads

    nodes = list(db.scalars(select(TaskNode)).all())
    children: dict[str | None, list[str]] = {}
    for node in nodes:
        children.setdefault(node.parent_id, []).append(node.id)
    subtree: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in subtree:
            continue
        subtree.add(current)
        stack.extend(children.get(current, []))
    tasks = [node for node in nodes if node.id in subtree]

    runs = [run for run in db.scalars(select(RunRecord)).all() if run.task_id in subtree]

    created = settled = recurrences = human = 0
    unsettled_types: dict[str, int] = {}
    for task in tasks:
        meta = loads(task.node_meta_json, {})
        items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        created += len(items)
        for item in items:
            if item.get("settled_at"):
                settled += 1
            else:
                key = str(item.get("type") or "unknown")
                unsettled_types[key] = unsettled_types.get(key, 0) + 1
            recurrences += int(item.get("recurrence_count") or 0)
            if str(item.get("settled_by") or "") == "human":
                human += 1

    # 用量直接按本棵树的 run 记录汇总（不经过看板，避免"看板的筛选规则"与基准口径耦合）
    usage = summarize_usages([loads(run.usage_json, None) for run in runs if run.usage_json])

    return {
        # 记账行数：每次 agent 记账一行（含合成行与 mock 占位），它是"树干了多少活"的近似
        "agent_starts": len(runs),
        # 其中真的调用了 CLI 的行数（有 usage 才说明真跑过）
        "cli_runs": sum(1 for run in runs if run.usage_json),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "wall_clock_seconds": round(float(wall_clock_seconds), 3),
        "human_interventions": human,
        "obligations_created": created,
        "obligations_settled": settled,
        "obligation_recurrences": recurrences,
        "blocked_categories": unsettled_types,
        "node_count": len(tasks),
    }


async def run_one(
    task: dict[str, Any],
    args: argparse.Namespace,
    *,
    base_settings: Any,
    db_url: str,
    run_dir: pathlib.Path,
    inflight: dict[str, Any],
) -> dict[str, Any]:
    from app import bench
    from app.database import SessionLocal
    from app.models import TaskNode
    from app.orchestrator import Orchestrator

    target = run_dir / "target"
    copy_fixture(task, target)
    # 这里曾经要 `prepare_schemas(run_dir)`：把仓库根的 `schemas/` 复制到隔离 workspace 旁边，
    # 因为 `runner.py` 当时用 `workspace_dir.parent / "schemas"` 推 schema 位置，
    # 而基准的隔离 workspace 旁边没有那个目录 -> codex 秒退 0 token（实测 5 秒）。
    # 现在 `runner.py` 锚在 ROOT_DIR 上（见 tests/test_schema_dir_is_repo_anchored.py），
    # 这个复制步骤已无必要，故删除。
    # 心跳靠这里取"现在在跑哪个任务的第几秒"：进程被外部掐掉时，报告里至少留下这一行
    inflight["task_id"] = str(task["id"])
    inflight["started"] = time.monotonic()
    settings = base_settings.__class__(
        database_url=db_url,
        runner_mode=args.provider,
        codex_bin=args.codex_bin,
        codex_model=args.model,
        codex_reasoning_effort=args.reasoning_effort,
        codex_timeout_seconds=args.timeout,
        codex_ignore_rules=True,
        codex_ephemeral=True,
        max_concurrency=1,
        mock_delay_seconds=0,
        workspace_dir=run_dir / "workspace",
        # 任务自己声明预算上限：超出就由既有的额度池休眠，而不是把"跑不完"记成"做错了"
        agent_burst_limit=int(task.get("max_agent_starts") or 0),
    )
    orchestrator = Orchestrator(settings)
    note = ""
    started = time.monotonic()
    root_id = ""
    status = "not_started"
    task_timeout = int(task.get("timeout_seconds") or args.timeout)
    if args.task_timeout:
        # 显式覆盖：前台短测要的是"这一轮有个有界的结果"，而不是被工具层掐掉（死进程不写报告）。
        # 覆盖值会写进报告备注，免得事后把"被上限截断"读成"模型没做完"。
        task_timeout = int(args.task_timeout)
        note = f"墙钟上限被 --task-timeout 覆盖为 {task_timeout}s（任务自带 {task.get('timeout_seconds')}s）"
    try:
        constitution_id = await orchestrator.draft_constitution(
            task["goal"], target_path=str(target), runner_mode=args.constitution_runner
        )
        orchestrator.lock_constitution(constitution_id)
        root_id = await orchestrator.start_project(
            task["goal"],
            target_path=str(target),
            runner_mode=args.provider,
            constitution_id=constitution_id,
            auto_run=False,
        )
        await asyncio.wait_for(orchestrator.run_tree(root_id, runner_mode=args.provider), timeout=task_timeout)
        with SessionLocal() as db:
            root = db.get(TaskNode, root_id)
            status = str(root.status) if root else "missing"
    except asyncio.TimeoutError:
        # 超时是**单独一种结果**：它既不是"完成"，也不是"判据不成立"
        status = "timeout"
        note = f"超过墙钟上限 {task_timeout}s 仍未到终态"
    except Exception as exc:  # noqa: BLE001 —— 基准要把"跑不起来"如实记下来，而不是把整批跑挂掉
        status = "error"
        note = f"{type(exc).__name__}: {exc}"
    wall = time.monotonic() - started
    if not note and status in {"hibernating", "paused", "blocked", "failed", "canceled", "missing", "not_started"}:
        # 未到终态要写清楚原因：休眠=额度/预算用尽，这与"做完/做错"是三种不同的结果
        note = f"未到终态（{status}）"

    # 判据打在**根节点的 worktree** 上，不是那份还没被写过的目标拷贝。
    #
    # 实测教训（第三次踩"测量自己撒谎"）：产品契约是"agent 在自己 worktree 里干活、集成落到父/根的
    # worktree"，`target_path` 是种子而不是回写目的地——产品自己的烟测也只在各节点 worktree 里找产物。
    # 一开始把判据打在目标拷贝上，真实档跑出了"树 done 但判据 1/3 → 谎报 1"的假结论，
    # 而真相是成果好好地躺在根 worktree 里（`worktrees/<root>/calc/ops.py` 有 multiply）。
    checked_dir = target
    if root_id:
        with SessionLocal() as session:
            root_task = session.get(TaskNode, root_id)
            if (
                root_task is not None
                and root_task.worktree_path
                and pathlib.Path(root_task.worktree_path).is_dir()
            ):
                checked_dir = pathlib.Path(root_task.worktree_path)

    results: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    try:
        results = bench.evaluate_checks(task, checked_dir, python_executable=sys.executable)
        if root_id:
            with SessionLocal() as db:
                metrics = collect_metrics(db, root_id, wall_clock_seconds=wall)
        else:
            metrics = {"wall_clock_seconds": round(wall, 3)}
    except Exception as exc:  # noqa: BLE001 —— 收尾失败也要**留下一条记录**，不能让整个任务的读数凭空消失
        note = f"{note}；收尾失败：{type(exc).__name__}: {exc}".strip("；")
    summary = bench.summarize_task(
        task, results, tree_status=status, metrics=metrics, fixture_dir=str(checked_dir), notes=note
    )
    inflight["task_id"] = ""
    print(
        f"  {task['id']}: 外部判据 {summary['checks_passed']}/{summary['checks_total']}"
        f"（未判定 {summary['checks_unjudged']}） 树状态={status}"
        f" 自称完成={summary['tree_claimed_done']} 谎报={summary['overconfident']}{('  ' + note) if note else ''}"
    )
    return summary


def _assert_isolated_database(engine: Any, db_path: pathlib.Path) -> None:
    """**开跑前**确认引擎连的就是本次基准的库。

    第一版把 app 的 import 写在了设环境变量之前，于是整批跑进了**开发库**：
    指标把历史所有 run 记录都算了进来（`agent_starts` 报出 67 万），而且往开发树上加了 3 棵垃圾树。
    这类错误的失效方向是"错得毫无提示"，所以在最前面加一条硬断言：连错了就**当场停**。
    """
    url = str(getattr(engine, "url", "") or "")
    if pathlib.PurePath(db_path.as_posix()).name not in url:
        raise SystemExit(
            "拒绝开跑：数据库不是本次基准的库。\n"
            f"  期望包含：{db_path.name}\n"
            f"  实际引擎：{url}\n"
            "原因通常是 `import app.*` 早于环境变量设置——`app.config` 的默认值在**导入时**求值。"
        )


def write_report(
    path: pathlib.Path,
    *,
    provider: str,
    tasks: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    started_at: str,
    notes: str,
    partial: bool = True,
) -> dict[str, Any]:
    """把当前进度写成一份报告（**每个任务之后 + 心跳都调**：进程在收尾时消失也不该带走已完成的读数）。

    `partial=True` 是默认值：中途落盘**必须先声明自己不是结论**（`bench.build_report` 会写进 json），
    否则被杀掉的那一轮会留下一份"看着完整"的报告。

    落盘用 tmp + `os.replace`：报告会被心跳反复重写，读到半截的 json 比没报告更误导。
    """
    from app import bench  # 延迟导入：先设好环境变量再导 app

    report = bench.build_report(
        provider=provider,
        task_set_hash_value=bench.task_set_hash(tasks),
        summaries=summaries,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        notes=notes,
        partial=partial,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return report


async def run_all(args: argparse.Namespace, *, stamp: str, session_dir: pathlib.Path, db_path: pathlib.Path) -> int:
    # 环境已在 `main()` 里设好（**必须早于任何 app 导入**），这里才允许导入 app
    from app import bench
    from app.config import get_settings
    from app.database import engine, init_db

    _assert_isolated_database(engine, db_path)
    init_db()
    base_settings = get_settings()
    db_url = f"sqlite:///{db_path.as_posix()}"
    tasks = load_tasks(args.tasks)

    started_at = datetime.now(timezone.utc).isoformat()
    print(f"跑 {len(tasks)} 个任务，通道={args.provider}，目录={session_dir}", flush=True)
    summaries: list[dict[str, Any]] = []
    report_path = REPORTS_DIR / f"{stamp}-{args.provider}.json"
    inflight: dict[str, Any] = {"task_id": "", "started": 0.0, "index": 0}
    done_count = len(tasks)

    async def heartbeat() -> None:
        """每 `--heartbeat` 秒刷新一次报告：**进程被外部掐掉时，留下的读数要能说明它在哪**。

        两种"没有 traceback 的消失"都要能被区分出来：
        1. **自己崩**：异常穿过循环而报告只在成功路径上写 → 磁盘什么都没有（实测：`Path` 未定义，
           只在 real 档才走到那一行，连丢两轮真实基准，现场看起来像"产品卡死"）；现在的围栏会先落
           一份"中断"报告（见下面的 `except BaseException`）。
        2. **被外面掐掉**：前台命令有 10 分钟上限，超了就杀进程——没有 traceback、没有 exit code。
           心跳报告带 `partial=true` + "当前任务已跑 N 秒"，把"被掐"与"自己挂住"分开（后者连心跳都停）。
        """
        while True:
            await asyncio.sleep(interval)
            if not inflight["task_id"]:
                continue
            elapsed = round(time.monotonic() - float(inflight["started"]), 1)
            write_report(
                report_path,
                provider=args.provider,
                tasks=tasks,
                summaries=list(summaries),
                started_at=started_at,
                notes=(
                    f"run_dir={session_dir}（进行中：{inflight['index']}/{done_count}，"
                    f"当前 {inflight['task_id']} 已跑 {elapsed}s）"
                ),
            )
            print(f"[心跳] {inflight['task_id']} 已跑 {elapsed}s（报告已刷新）", flush=True)

    interval = max(15, int(args.heartbeat))
    ticker = asyncio.create_task(heartbeat())

    # **硬停线程**：不依赖事件循环。
    #
    # 为什么不能只靠 `asyncio.wait_for` 兜底（实测 2026-09-22）：那一轮真实基准在 `impossible-no-write`
    # 上冻了 30 分钟以上——循环停在 `_poll` 的 `GetQueuedCompletionStatus` 里，**它自己的定时器全部
    # 不再触发**：60s 心跳、600s CLI 看门狗、1500s 任务上限、连 faulthandler 的 120s dump 都停了。
    # 也就是说"事件循环卡住"这种失效模式下，"用事件循环兜底超时"是一条**恒假的判据**。
    # 所以硬停必须是普通线程 + `os._exit`（不 unwind：卡住的循环没法优雅退出），且退出前落证据。
    stop_event = threading.Event()
    stacks_path = report_path.with_suffix(report_path.suffix + ".stacks.txt")
    hard_stop_seconds = max(0, int(args.hard_stop))

    def hard_stop() -> None:
        if hard_stop_seconds <= 0 or stop_event.wait(hard_stop_seconds):
            return
        stacks = ""
        try:
            with stacks_path.open("w", encoding="utf-8") as handle:
                faulthandler.dump_traceback(file=handle, all_threads=True)
            stacks = f"；线程栈已落盘：{stacks_path}"
        except OSError as exc:  # 落不下栈也要继续硬停：留住报告比留住栈重要
            stacks = f"；线程栈落盘失败：{type(exc).__name__}: {exc}"
        write_report(
            report_path,
            provider=args.provider,
            tasks=tasks,
            summaries=list(summaries),
            started_at=started_at,
            notes=(
                f"run_dir={session_dir}（硬停：超过 {hard_stop_seconds}s 仍未收尾，"
                f"当前 {inflight['task_id'] or '收尾中'} 已跑 "
                f"{round(time.monotonic() - float(inflight['started']), 1) if inflight['started'] else 0}s{stacks}）"
            ),
        )
        print(f"[硬停] 超过 {hard_stop_seconds}s 未收尾，已落盘报告与线程栈后退出{stacks}", flush=True)
        os._exit(3)

    if hard_stop_seconds > 0:
        threading.Thread(target=hard_stop, name="bench-hard-stop", daemon=True).start()

    try:
        for index, task in enumerate(tasks, start=1):
            inflight["index"] = index
            print(f"[{index}/{len(tasks)}] {task['id']} 开始", flush=True)
            try:
                summary = await run_one(
                    task,
                    args,
                    base_settings=base_settings,
                    db_url=db_url,
                    run_dir=session_dir / task["id"],
                    inflight=inflight,
                )
            except BaseException as exc:  # noqa: BLE001 —— 连 KeyboardInterrupt 也要先留证据再往上抛
                # 围栏：**任何**把这一轮带走的异常都得先落一份报告，写明死在哪个任务的哪一步。
                # 这是被一次真事件逼出来的：`bench_run.py` 里一个未定义的名字（`Path`）在
                # "判据求值之前"抛出，异常一路穿出 `run_all`，而报告只在**成功**路径上写 → 磁盘上
                # 什么都没有，现场只剩"DB 停在 verifying"，看起来就是"产品卡死了"。
                # 失效方向：**照实记 + 继续抛**（不吞异常：崩溃必须让退出码非 0，报告只是留证据）。
                write_report(
                    report_path,
                    provider=args.provider,
                    tasks=tasks,
                    summaries=list(summaries),
                    started_at=started_at,
                    notes=(
                        f"run_dir={session_dir}（中断：{task['id']} 抛出 "
                        f"{type(exc).__name__}: {exc}）"
                    ),
                )
                print(
                    f"[{index}/{len(tasks)}] {task['id']} 中断：{type(exc).__name__}: {exc}"
                    "（已落盘中断报告）",
                    flush=True,
                )
                raise
            summaries.append(summary)
            # **每跑完一个任务就落一次盘**：上一版只在最后写报告，于是进程在收尾时消失就把整批读数
            # 一起带走了（实测：树 16:05 就 done 了，报告却从来没出现）。分段落盘让"死了也只丢最后一个"。
            write_report(
                report_path,
                provider=args.provider,
                tasks=tasks,
                summaries=summaries,
                started_at=started_at,
                notes=f"run_dir={session_dir}（进行中：{index}/{len(tasks)}）",
            )
            print(f"[{index}/{len(tasks)}] {task['id']} 完成；报告已落盘（进行中）", flush=True)
    finally:
        # 心跳必须停干净：否则它会在正式报告写完之后再覆盖一份 `partial=true` 的进去
        ticker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker
        # 同时解除硬停：正常收尾就不能再被"超时"打断（否则最后一份正式报告会被 partial 覆盖）
        stop_event.set()
    report = write_report(
        report_path,
        provider=args.provider,
        tasks=tasks,
        summaries=summaries,
        started_at=started_at,
        notes=f"run_dir={session_dir}",
        partial=False,
    )
    print(flush=True)
    print(bench.compare([report]), flush=True)
    print(flush=True)
    print(f"报告：{report_path}", flush=True)
    if not report["channel_evidence"]["valid"]:
        # 状态机跑完了、判据也判了，但**模型一次都没被调用** —— 这种分数不能用。
        # 实测踩过一次：`runner.py` 当时从 `workspace_dir.parent` 推 schema 位置，
        # 隔离 workspace 旁边没有 `schemas/`，codex 秒失败、5 秒"跑完"、0 token。
        # 根因已修（schema 现锚在 ROOT_DIR），但这条判据要留着——它是"零调用"的通用探测器。
        print("RESULT: 无效测量 —— 这一档一次 CLI 调用都没有，模型什么都没干，分数不可用")
        return 1
    if report["hard_failures"]:
        print("该档出现『unsolvable 任务声称完成』：")
        for item in report["hard_failures"]:
            print(f"  - {item['task_id']}: {item.get('notes') or ''}")
        if args.provider == "mock":
            # 失效方向写明：mock 档的"声称完成"是**渠道性质**——模拟 runner 不做真实工作、验收也是模拟的，
            # 它没有任何渠道能知道外部判据不成立。所以这条判据只在 real 档构成"体系不可信"。
            # 报告里 `trustworthy=false` 照记，不粉饰；只是退出码不因此变非 0（否则默认 smoke 永远是红的）。
            print("RESULT: mock 档照例如此（模拟 runner 不做真实工作）——该判据只在 --provider real 上有效")
            return 0
        print("RESULT: 该档不可信")
        return 1
    print("RESULT: 没有谎报完成（unsolvable 任务都守住了）")
    return 0


def compare_reports(paths: list[str]) -> int:
    from app import bench

    reports = [json.loads(pathlib.Path(path).read_text(encoding="utf-8")) for path in paths]
    partial = [path for path, report in zip(paths, reports, strict=True) if report.get("partial")]
    if partial:
        # 失效方向：**拒绝**。中途报告的合计只覆盖"已经跑完的那几个任务"，拿它对表会得到一张
        # 看起来正常、实际少算了几行的表——这类错误没有任何提示，所以宁可在这里停下。
        for path in partial:
            print(f"拒绝对照：{path} 是中途读数（partial=true），不是一轮完整的跑批", file=sys.stderr)
        return 2
    print(bench.compare(reports))
    return 0


def main() -> int:
    args = parse_args()
    # 卡死时也要有现场：`bench_run` 曾经两次在同一个位置"无声消失"，只留下半截目录。
    # `faulthandler` 不依赖任何第三方包，`--debug-stacks N` 会每 N 秒把所有线程/协程的栈打到 stderr。
    faulthandler.enable()
    if args.debug_stacks:
        faulthandler.dump_traceback_later(args.debug_stacks, repeat=True, file=sys.stderr)
    if args.compare is not None:
        if not args.compare:
            print("--compare 需要给至少一份报告 json", file=sys.stderr)
            return 2
        prepare_imports(ROOT / "workspace" / "bench-compare.sqlite3")  # 只为让 `app.bench` 可导入
        return compare_reports(list(args.compare))

    # 先把本次基准的库与环境定下来，**然后**才允许导入 app：
    # `app.config` 的字段默认值（含 database_url）是在**导入时**求值的，晚一步设 env 就没用了。
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    session_dir = RUNS_DIR / f"{stamp}-{args.provider}"
    session_dir.mkdir(parents=True, exist_ok=True)
    db_path = session_dir / "bench.sqlite3"
    prepare_imports(db_path)
    return asyncio.run(run_all(args, stamp=stamp, session_dir=session_dir, db_path=db_path))


if __name__ == "__main__":
    sys.exit(main())
