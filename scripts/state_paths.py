"""状态路径解析 CLI —— 给**非 Python 调用方**（尤其看门狗 ps1）用的只读入口。

## 为什么需要它

状态与工程解耦之后，"某个状态文件在哪"不再能靠拼路径回答：

* 机器级状态在 `<状态根>/`，
* 每工程状态在 `<状态根>/projects/<工程 key>/`，
* 而 `<工程 key>` 由规范化绝对路径哈希而来。

看门狗（`scripts/battlefield_watchdog.ps1`）是**独立的计划任务进程**，它自己拼
`$WorkspaceDir` 并读库与健康文件。如果它继续按老路径找，而 API 已经把东西搬到
新位置，就会出现"看门狗读写的地方 API 已经不看了"——静默错位，只在出问题时暴露
（`docs/spec-state-project-decoupling.md` §11-H4）。

所以给 ps1 一个**唯一的解析入口**：它调本脚本拿路径，而不是自己拼。
这样"路径怎么算"只有一份实现，ps1 与 Python 不会各自漂移。

## 用法

    python scripts/state_paths.py                 # 人看：全部路径 + 绑定状态
    python scripts/state_paths.py --get state_home
    python scripts/state_paths.py --get db
    python scripts/state_paths.py --get runs

`--get` 只输出**一行**值，方便 ps1 直接赋给变量。
解析不出来（例如没绑工程就要 db）时退出码非 0，并把原因写到 stderr ——
ps1 侧必须能区分"路径是这个"和"拿不到路径"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from app.state_layout import NotBoundError, load_layout  # noqa: E402
from app.state_layout import (  # noqa: E402
    ACTIVE_FILENAME,
    CODEX_HEALTH_FILENAME,
    GUARDIAN_STATE_FILENAME,
    MACHINE_SAFETY_FILENAME,
    SLOT_RESERVATIONS_FILENAME,
    WATCHDOG_LOG_FILENAME,
    WATCHDOG_STATUS_FILENAME,
)


def workspace_dir(settings: object | None = None) -> Path:
    """**每工程工作区**的路径 —— 给仓库里的脚本用，别再自己拼 `ROOT/"workspace"`。

    语义与 `Settings.workspace_dir` 一致：
      * 绑定了工程 -> `<状态根>/projects/<key>/`
      * 未绑定     -> `<repo>/workspace/`（向后兼容）

    为什么要这个函数：脚本里散落的 `ROOT / "workspace"` 在状态搬到状态根之后
    就**指向一个不存在的地方**。实测踩过：`verify_provider_planner.py`
    硬编码 `workspace_dir=WORKSPACE`，于是隔离 CODEX_HOME 用不上、
    codex 的 `--cd` 指到已删除的目录，planner 直接 `blocked`。

    显式传 `settings` 时用它自己的（尊重调用方的 overrides）。
    """
    if settings is not None:
        explicit = getattr(settings, "workspace_dir", None)
        if explicit:
            return Path(explicit)
    layout = load_layout()
    if layout.bound:
        return layout.project_dir
    return ROOT / "workspace"


# `--get` 接受的键 → 取值函数。机器级（M）与每工程（P）都在这里，
# 调用方一眼能看出哪些不需要绑工程。
GETTERS = {
    # 机器级：不依赖绑定的工程
    "state_home": lambda layout: layout.state_home,
    "tools_dir": lambda layout: layout.tools_dir,
    "watchdog_log": lambda layout: layout.machine_path(WATCHDOG_LOG_FILENAME),
    "codex_health": lambda layout: layout.machine_path(CODEX_HEALTH_FILENAME),
    "machine_safety": lambda layout: layout.machine_path(MACHINE_SAFETY_FILENAME),
    "watchdog_status": lambda layout: layout.machine_path(WATCHDOG_STATUS_FILENAME),
    "guardian_state": lambda layout: layout.machine_path(GUARDIAN_STATE_FILENAME),
    "slot_reservations": lambda layout: layout.machine_path(SLOT_RESERVATIONS_FILENAME),
    "active_file": lambda layout: layout.machine_path(ACTIVE_FILENAME),
    # 每工程：未绑定时抛 NotBoundError
    "project_key": lambda layout: layout.project_key,
    "project_root": lambda layout: layout.project_root,
    "project_dir": lambda layout: layout.project_dir,
    "db": lambda layout: layout.db_path,
    "runs": lambda layout: layout.runs_dir,
    "worktrees": lambda layout: layout.worktrees_dir,
    "snapshots": lambda layout: layout.snapshots_dir,
    "checkpoint_ledger": lambda layout: layout.checkpoint_ledger_dir,
    "full_checkpoints": lambda layout: layout.full_checkpoints_dir,
    "leases": lambda layout: layout.leases_dir,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="解析 Codex Battlefield 的状态路径（只读；供 ps1 等非 Python 调用方使用）。",
    )
    parser.add_argument("--get", default="", help=f"只输出一个值。可选：{', '.join(sorted(GETTERS))}")
    parser.add_argument("--state-home", default="", help="覆盖状态根（默认按环境变量/用户主目录）")
    parser.add_argument("--project-key", default="", help="覆盖工程 key（默认读 active.json）")
    args = parser.parse_args(argv)

    layout = load_layout(
        state_home=Path(args.state_home) if args.state_home else None,
        project_key_override=args.project_key,
    )

    if args.get:
        getter = GETTERS.get(args.get)
        if getter is None:
            print(f"未知的 --get 键：{args.get}（可选：{', '.join(sorted(GETTERS))}）", file=sys.stderr)
            return 2
        try:
            value = getter(layout)
        except NotBoundError as exc:
            # 退出码非 0 + stderr 说明：调用方必须能区分"拿不到"与"拿到了个空值"
            print(str(exc), file=sys.stderr)
            return 3
        if value is None:
            return 3
        print(value)
        return 0

    info = layout.describe()
    print(f"状态根      : {info['state_home']}")
    print(f"绑定工程    : {info['project_key'] or '（未绑定）'}")
    if info["bound"]:
        print(f"工程目录    : {info['project_root']}")
        print(f"工程状态目录: {info['project_dir']}")
        print(f"库          : {info['db_path']}")
        print(f"运行记录    : {info['runs_dir']}")
        print(f"工作树      : {info['worktrees_dir']}")
    else:
        print("  （未绑定：每工程路径不可用；机器级路径见下）")
    print()
    print("机器级状态：")
    for key in (
        "watchdog_log",
        "watchdog_status",
        "codex_health",
        "machine_safety",
        "guardian_state",
        "slot_reservations",
        "active_file",
    ):
        print(f"  {key:<18}: {GETTERS[key](layout)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
