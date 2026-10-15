"""`battlefield-project` —— 装载/切换"被改的工程"。

用法：
    battlefield-project list                 已挂载工程 + 各自库路径与体积
    battlefield-project current              当前工程、状态根、库、worktree 是否跨盘
    battlefield-project use <工程目录>       挂载并切为当前工程（原子写 active.json）
    battlefield-project unmount <key>        从 registry 摘掉（默认不删状态目录）
    battlefield-project doctor               体检：状态根可写 / 库可打开 / 无 key 碰撞 / worktree 同盘
    battlefield-project where <工程目录>     只算 key 与目标目录，不落盘（干跑）

设计要点（`docs/spec-state-project-decoupling.md` §9）：

* **切换 = 写 `active.json` + 重启 API**。不是"运行期热切"——热换库会让进程里
  缓存的 session 仍指向旧库，那是静默写错库（决策 D1）。所以 `use` 会明确提示重启。
* `use` 必须**原子**：半写的 `active.json` 会让下次启动读成"未绑定"，
  于是悄悄退回老工作区，而用户以为在跑另一个工程。
* 工程目录取**规范化绝对路径**，相对路径直接拒绝（否则同一工程会得到不同 key）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from app.project_store import (  # noqa: E402
    ProjectError,
    current_project,
    directory_size_bytes,
    list_projects,
    mount,
    remove_project,
    validate_project_root,
    worktree_drive_matches,
)
from app.state_layout import (  # noqa: E402
    config_file,
    default_state_home,
    project_key,
    resolve_state_home,
    set_config_value,
    set_state_home,
)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.1f}GB"


def cmd_list(args: argparse.Namespace) -> int:
    home = resolve_state_home()
    projects = list_projects()
    active = current_project()
    active_key = active.key if active else ""

    print(f"状态根：{home}")
    if not projects:
        print("\n还没有挂载任何工程。")
        print("  挂载一个： battlefield-project use <工程目录绝对路径>")
        return 0

    print()
    print(f"{'':2} {'key':<44} {'库':>10}  {'状态体积':>9}  工程目录")
    for item in projects:
        mark = "*" if item.key == active_key else " "
        db = _human(item.db_size_bytes()) if item.db_exists else "—"
        size = _human(directory_size_bytes(item.project_dir))
        print(f"{mark:2} {item.key:<44} {db:>10}  {size:>9}  {item.root}")
    print()
    print("  * = 当前工程")
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    home = resolve_state_home()
    active = current_project()

    print(f"状态根      : {home}")
    if active is None:
        print("当前工程    : （未绑定）")
        print()
        print("  说明：未绑定时，每工程状态回退到仓库内的 <repo>/workspace（保持向后兼容）。")
        print("  切换：battlefield-project use <工程目录绝对路径>")
        return 0

    print(f"当前工程    : {active.key}")
    print(f"工程目录    : {active.root}")
    print(f"状态目录    : {active.project_dir}")
    print(f"库          : {active.db_path}  ({'存在' if active.db_exists else '尚未创建'})")

    same_drive = worktree_drive_matches(active.project_dir, active.root)
    if same_drive:
        print("worktree    : 与工程同盘（正常）")
    else:
        print("worktree    : ⚠️ 跨盘！git worktree 在跨盘时不可靠")
        print(f"              状态目录在 {active.project_dir.drive}，工程在 {active.root.drive}")
        print("              建议把状态根设到与工程同盘：CODEX_BATTLEFIELD_STATE_HOME=<同盘目录>")

    if not active.root.is_dir():
        print("            ⚠️ 工程目录当前不存在（曾被移动或删除？）")

    print()
    print("  改完要**重启 API** 才生效（运行期不热切库，见 spec 决策 D1）。")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    try:
        project = mount(args.path)
    except ProjectError as exc:
        print(f"拒绝挂载：{exc}")
        return 2

    print(f"已挂载并切换：{project.key}")
    print(f"  工程目录  : {project.root}")
    print(f"  状态目录  : {project.project_dir}")
    print(f"  库        : {project.db_path}  ({'已存在' if project.db_exists else '首次运行时创建'})")

    if not project.root.is_dir():
        print("  ⚠️ 这个目录当前不存在。挂载本身没问题，但跑任务前得先建好。")

    if not worktree_drive_matches(project.project_dir, project.root):
        print("  ⚠️ 状态目录与工程**不同盘**：git worktree 在跨盘时不可靠。")
        print(f"     建议 CODEX_BATTLEFIELD_STATE_HOME 设到 {project.root.drive} 盘的某个目录。")

    print()
    print("  下一步：重启 API —— battlefield-project current 会复核它读的是哪个库。")
    return 0


def cmd_where(args: argparse.Namespace) -> int:
    """干跑：只算 key 与目标目录，**不写任何文件**。"""
    try:
        resolved = validate_project_root(args.path)
    except ProjectError as exc:
        print(f"拒绝：{exc}")
        return 2
    key = project_key(resolved)
    home = resolve_state_home()
    print(f"工程目录(规范化) : {resolved}")
    print(f"project key      : {key}")
    print(f"将使用的状态目录 : {home / 'projects' / key}")
    print(f"将使用的库       : {home / 'projects' / key / 'battlefield.sqlite3'}")
    print()
    print("  （干跑：未写入任何文件）")
    return 0


def cmd_unmount(args: argparse.Namespace) -> int:
    try:
        result = remove_project(args.key, purge=args.purge)
    except ProjectError as exc:
        print(f"失败：{exc}")
        return 2
    print(f"已从 registry 摘除：{result['key']}")
    print(f"  原工程目录 : {result['root']}")
    if result["was_active"]:
        print("  它原本是当前工程，active.json 已清除 -> 此后回退仓库内 workspace")
    if result["purged"]:
        print("  状态目录已删除（--purge）")
    else:
        print("  状态目录**保留**在磁盘上（要删加 --purge）")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    home = resolve_state_home()
    problems = 0
    print(f"状态根：{home}")

    # 1. 可写
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("  [ok]   状态根可写")
    except OSError as exc:
        problems += 1
        print(f"  [FAIL] 状态根不可写：{exc}")

    projects = list_projects()
    if not projects:
        print("  [warn] 还没有挂载任何工程（当前会回退仓库内 workspace）")
    else:
        print(f"  [ok]   已挂载 {len(projects)} 个工程")

    # 1b. 状态根不能落在某个已挂载工程里面
    #
    # 理由：工程目录里出现状态根（`projects/`）会让它被当成被改的工程的一部分，
    # 既污染工程，也让 worktree 递归地把状态卷进去。
    home_resolved = home.resolve()
    for item in projects:
        try:
            root_resolved = Path(item.root).resolve()
        except OSError:
            continue
        if home_resolved == root_resolved or root_resolved in home_resolved.parents:
            problems += 1
            print(f"  [FAIL] 状态根落在工程目录**里面**：{item.root}")
            print("         把状态根放到工程之外（换盘也行，见 state show 的提示）")
            break

    # 1c. 状态根与工程跨盘 -> git worktree 不可靠（spec §8）
    if projects:
        home_drive = Path(home).drive.lower()
        off_drive = [item for item in projects if Path(item.root).drive.lower() != home_drive]
        if off_drive:
            print(f"  [warn] {len(off_drive)} 个工程与状态根**不同盘**（状态 {home_drive or '?'}）：")
            for item in off_drive[:4]:
                print(f"           {item.root}  ({Path(item.root).drive})")
            print("         git worktree 跨盘不可靠。把状态根挪到与主要工程同盘：")
            print("           battlefield-project state move <同盘目录> --apply")
        else:
            print("  [ok]   状态根与所有工程同盘（worktree 可用）")

    # 2. key 碰撞（同 key 不同 root 是不可能的，但要复核 key 重算一致）
    for item in projects:
        recomputed = project_key(item.root)
        if recomputed != item.key:
            problems += 1
            print(f"  [FAIL] key 与工程目录不一致：{item.key} != {recomputed}（root={item.root}）")

    # 3. 库可打开 + worktree 同盘
    for item in projects:
        if item.db_exists:
            try:
                import sqlite3

                uri = "file:///" + str(item.db_path).replace("\\", "/")
                con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=5)
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                con.close()
                print(f"  [ok]   库可打开：{item.key}")
            except Exception as exc:  # noqa: BLE001 - 体检要把任何异常都报出来
                problems += 1
                print(f"  [FAIL] 库打不开：{item.key} -> {exc}")
        else:
            print(f"  [ok]   库尚未创建（首次运行会建）：{item.key}")

        if not worktree_drive_matches(item.project_dir, item.root):
            problems += 1
            print(f"  [FAIL] worktree 与工程跨盘：{item.key}")
            print(f"         状态 {item.project_dir.drive} / 工程 {item.root.drive}")

        if not item.root.is_dir():
            print(f"  [warn] 工程目录不存在：{item.root}")

    # 4. active.json 与 registry 是否自洽
    active = current_project()
    if active is not None:
        keys = {item.key for item in projects}
        if active.key not in keys:
            problems += 1
            print(f"  [FAIL] active.json 指向一个不在 registry 里的工程：{active.key}")
        else:
            print(f"  [ok]   active.json 自洽：{active.key}")

    print()
    if problems:
        print(f"发现 {problems} 个问题。")
        return 1
    print("体检通过。")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    """把旧 `<repo>/workspace/` 里的库迁到新的每工程状态目录。

    **默认只出方案，不动手**：迁移是有后果的动作，先让人看清它打算干什么。
    `--apply` 才真的迁（仍然是**复制**，旧库原地留着）。
    """
    from app import migration

    repo_root = Path(args.repo).resolve() if args.repo else ROOT
    plan = migration.build_plan(repo_root)

    if plan is None:
        print(f"没有找到旧库：{repo_root / 'workspace' / 'battlefield.sqlite3'}")
        print("  不需要迁移（或者这个仓库本来就没跑过）。")
        return 0

    print(f"旧库      : {plan.legacy_db}")
    print(f"  task_nodes: {plan.task_count} 行，其中带 worktree_path 的 {plan.worktree_rows} 行")
    print()
    print("候选工程根（本工具**不替你猜**）：")
    for item in plan.candidates:
        mark = "可用 " if item.usable else "不可用"
        print(f"  [{mark}] {item.path}")
        print(f"          来源：{item.source}")
        print(f"          理由：{item.reason}")

    usable = plan.usable
    chosen: Path | None = None
    if args.project_root:
        chosen = Path(args.project_root).resolve()
        if not any(item.path == chosen and item.usable for item in usable):
            # 显式指定时仍然允许，但要说清它不在可用候选里
            print()
            print(f"注意：你指定的工程根不在上面的可用候选里：{chosen}")
            print("      仍按你指定的执行（你是知道情况的）。")
    elif len(usable) == 1:
        chosen = usable[0].path
        print()
        print(f"只有一个可用的候选，按它执行：{chosen}")
    elif not usable:
        print()
        print("没有可用的候选工程根。请显式指定：")
        print("  battlefield-project migrate --project-root <工程目录绝对路径> --apply")
        return 2
    else:
        print()
        print(f"有 {len(usable)} 个可用候选，**不替你选**。请显式指定：")
        for item in usable:
            print(f"  battlefield-project migrate --project-root \"{item.path}\" --apply")
        return 2

    if not args.apply:
        target = migration.expected_key(chosen)
        print()
        print("方案（未执行；加 --apply 才动手）：")
        print(f"  工程根    : {chosen}")
        print(f"  目标 key  : {target}")
        print(f"  目标库    : {resolve_state_home() / 'projects' / target / 'battlefield.sqlite3'}")
        print(f"  worktree  : 会把 {plan.worktree_rows} 行的 worktree_path 从旧根重写到新根")
        print("  旧库      : **原地保留**（复制，不移动）")
        print()
        print("  注意：重写 task_nodes.worktree_path 是必须的 —— 它是删除守卫的判据依据，")
        print("        不重写会让守卫与实际路径不一致（挡住清理，或更糟：删错东西）。")
        return 0

    try:
        result = migration.migrate(repo_root=repo_root, project_root=chosen)
    except (FileNotFoundError, Exception) as exc:  # noqa: BLE001 - CLI 要把失败原因原样说出来
        print(f"迁移失败：{exc}")
        return 1

    if not result.copied:
        print(f"未迁移：{result.skipped_reason}")
        print(f"  现有工程：{result.project.key} -> {result.project.project_dir}")
        return 0

    print("迁移完成（旧库原地保留）。")
    print(f"  工程 key      : {result.project.key}")
    print(f"  新库          : {result.project.db_path}")
    print(f"  worktree 重写 : {result.worktree_rows_rewritten} 行")
    if result.codex_home_copied:
        print("  隔离 CODEX_HOME: 已一起搬迁（Hub 的 provider 配置跟着走）")
    print(f"  凭据          : {result.project.project_dir / migration.MIGRATION_MARKER}")
    print()
    print("  下一步：battlefield-project use " + str(chosen) + "  然后重启 API。")
    return 0


def cmd_set_repo_root(args: argparse.Namespace) -> int:
    """记录"这个工具装在哪"，供 PATH 上的包装器反查。

    为什么需要：包装器可以放在 PATH 的任意目录，它得知道实现文件在哪。
    靠"同目录"就要求包装器留在仓库里，没法到处敲；写进配置后仓库搬走也只改一处。
    不传参数时就写**当前运行的这个 checkout**（自注册）。
    """
    target = Path(args.path).expanduser().resolve() if args.path else ROOT
    script = target / "scripts" / "battlefield_project.py"
    if not script.is_file():
        print(f"这里看起来不是 Codex Battlefield 仓库：{target}")
        print(f"  找不到 {script}")
        return 2
    set_config_value("repo_root", str(target))
    print(f"已记录 repo_root = {target}")
    print(f"  写入 {config_file()}")
    print("  包装器（PATH 上的 battlefield-project.cmd）会读它来定位实现。")
    return 0


def cmd_get_state_home(args: argparse.Namespace) -> int:
    """只打印一行状态根 —— 给脚本用（与 `state_paths.py --get state_home` 同义）。"""
    print(resolve_state_home())
    return 0


def cmd_state_show(args: argparse.Namespace) -> int:
    """状态根现在是什么、从哪来的。"""
    env_value = __import__("os").environ.get("CODEX_BATTLEFIELD_STATE_HOME", "")
    resolved = resolve_state_home()

    print(f"生效状态根  : {resolved}")
    if env_value:
        print("来源        : 环境变量 CODEX_BATTLEFIELD_STATE_HOME（优先级最高）")
    elif resolved == default_state_home():
        print("来源        : 内置默认值（没有配置文件）")
    else:
        print(f"来源        : 配置文件 {config_file()}")
    print(f"默认值      : {default_state_home()}")
    print(f"配置文件    : {config_file()}  ({'存在' if config_file().is_file() else '不存在'})")
    print(f"目录存在    : {resolved.is_dir()}")
    print(f"占用        : {_human(directory_size_bytes(resolved))}")

    active = current_project()
    if active is None:
        print("当前工程    : （未绑定）")
        return 0

    print(f"当前工程    : {active.root}")
    same = worktree_drive_matches(active.project_dir, active.root)
    if same:
        print("worktree    : 与工程同盘（正常）")
    else:
        print(f"worktree    : ⚠️ 跨盘（状态 {Path(resolved).drive} / 工程 {active.root.drive}）")
        print("              git worktree 跨盘不可靠。用下面这条把状态根挪到工程同盘：")
        print(f"              battlefield-project state move {active.root.drive}\\<某目录>\\battlefield-state")
    return 0


def cmd_set_state_home(args: argparse.Namespace) -> int:
    """写状态根到用户级配置文件（持久；GUI 启动的 API 也能读到）。"""
    try:
        target = Path(args.path).expanduser().resolve()
    except OSError as exc:
        print(f"路径无法解析：{exc}")
        return 2
    if not target.is_absolute():
        print("需要绝对路径。")
        return 2

    before = resolve_state_home()
    set_state_home(target)
    print(f"已写入 {config_file()}")
    print(f"  状态根：{before}  ->  {target}")
    if target != before:
        print()
        print("  注意：**已有状态还在旧位置**。要在新位置可用的方式二选一：")
        print(f"    a) 搬过去：battlefield-project state move {target}   （复制 + 切换）")
        print("    b) 或忽略旧状态，在新位置重新挂载工程：battlefield-project use <工程目录>")
    print()
    print("  环境变量 CODEX_BATTLEFIELD_STATE_HOME 若已设置，会**盖过**这份配置。")
    return 0


def cmd_state_move(args: argparse.Namespace) -> int:
    """把状态根整体搬到新位置，并切换过去。

    只做文件复制 + 改指针，**不重算 project key**（key 由工程路径决定，与状态根无关），
    所以搬完之后每个工程仍然认得自己的目录。
    """
    import shutil

    target = Path(args.path).expanduser().resolve()
    source = resolve_state_home().resolve()
    if target == source:
        print(f"状态根已经在这里了：{source}")
        return 0
    if not source.is_dir():
        print(f"旧状态根不存在，没什么可搬：{source}")
        print("  直接用 battlefield-project set-state-home 改指针即可。")
        return 0

    size = _human(directory_size_bytes(source))
    print(f"从 {source}  ->  {target}")
    print(f"  体积：{size}")

    if not args.apply:
        print()
        print("**默认只预演**。加 --apply 才真的复制。")
        print("  旧状态根**保持不动**（复制不移动）；确认无误后自己删，或加 --purge-old。")
        return 0

    if target.exists() and any(target.iterdir()):
        if not args.overwrite:
            print(f"目标已存在且非空，拒绝覆盖：{target}")
            print("  确认要合并就加 --overwrite。")
            return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    set_state_home(target)

    print("完成。")
    print(f"  新状态根：{target}")
    print(f"  指针已写入 {config_file()}")

    if args.purge_old:
        removed = True
        try:
            shutil.rmtree(source)
        except OSError as exc:
            removed = False
            print(f"  ⚠️ 旧状态根删除失败（保留着，不影响使用）：{exc}")
        if removed:
            print(f"  旧状态根已删除：{source}")
    else:
        print(f"  旧状态根**保留**：{source}（要删就自己删，或下次加 --purge-old）")

    active = current_project()
    if active is not None and not worktree_drive_matches(active.project_dir, active.root):
        print()
        print(f"  ⚠️ 仍然跨盘（状态 {target.drive} / 工程 {active.root.drive}）。")
    print()
    print("  环境变量若设了 CODEX_BATTLEFIELD_STATE_HOME，会盖过这次改动。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="battlefield-project",
        description="装载/切换 Codex Battlefield 的\"被改工程\"（状态与工程解耦）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="已挂载工程清单").set_defaults(func=cmd_list)
    sub.add_parser("current", help="当前工程与状态根").set_defaults(func=cmd_current)

    use = sub.add_parser("use", help="挂载并切为当前工程")
    use.add_argument("path", help="工程目录（必须绝对路径）")
    use.set_defaults(func=cmd_use)

    where = sub.add_parser("where", help="干跑：只算 key，不落盘")
    where.add_argument("path")
    where.set_defaults(func=cmd_where)

    unmount = sub.add_parser("unmount", help="从 registry 摘掉一个工程")
    unmount.add_argument("key")
    unmount.add_argument("--purge", action="store_true", help="同时删除状态目录（不可逆）")
    unmount.set_defaults(func=cmd_unmount)

    sub.add_parser("doctor", help="体检").set_defaults(func=cmd_doctor)

    migrate = sub.add_parser("migrate", help="把旧 workspace/ 里的库迁到每工程状态目录")
    migrate.add_argument("--repo", default="", help="仓库根（默认：本脚本所在仓库）")
    migrate.add_argument("--project-root", default="", help="工程根（不猜；候选多于一个时必填）")
    migrate.add_argument("--apply", action="store_true", help="真的执行（默认只出方案）")
    migrate.set_defaults(func=cmd_migrate)

    state = sub.add_parser("state", help="查看/修改状态根")
    state_sub = state.add_subparsers(dest="state_command")

    sub.add_parser("get-state-home", help="只打印生效的状态根").set_defaults(func=cmd_get_state_home)

    show = state_sub.add_parser("show", help="状态根现在是什么、从哪来的")
    show.set_defaults(func=cmd_state_show)

    move = state_sub.add_parser("move", help="把状态根整体搬到新位置（默认只预演）")
    move.add_argument("path", help="新状态根目录（绝对路径）")
    move.add_argument("--apply", action="store_true", help="真的复制（默认只预演）")
    move.add_argument("--overwrite", action="store_true", help="目标非空时允许合并")
    move.add_argument("--purge-old", action="store_true", help="复制成功并切换后删除旧状态根")
    move.set_defaults(func=cmd_state_move)

    setter = sub.add_parser("set-state-home", help="只改指针到某个状态根（不搬文件）")
    setter.add_argument("path", help="状态根目录（绝对路径）")
    setter.set_defaults(func=cmd_set_state_home)

    repo = sub.add_parser("set-repo-root", help="记录本 checkout 位置（供 PATH 包装器反查）")
    repo.add_argument("path", nargs="?", default="", help="仓库根；省略则用当前运行的 checkout")
    repo.set_defaults(func=cmd_set_repo_root)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
