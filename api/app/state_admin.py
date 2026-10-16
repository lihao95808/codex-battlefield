"""状态根的**概览 / 迁移预演 / 重置**（P2-5）。

## 为什么需要它

状态与工程解耦之后，数据落在哪、占多大、旧库还剩什么，这些原来只能靠
`battlefield-project state show` / `migrate` / `doctor` 这些命令去看。
而"换机器、清空重来、把状态搬走"恰恰是用户最怕做错的几件事 ——
它们**不可逆**，而界面里没有任何入口，用户只能对着命令行犹豫。

本模块只做两件事，而且都刻意做成**只读或先预演**：

* `overview()`  —— 纯只读：状态根、每个工程的大小、旧库候选。
* `migration_preview()` —— 复用 `migration.build_plan`，**不调用** `migrate()`。

真正的迁移与删除分别走既有的 `migration.migrate()` 与
`project_store.remove_project()` —— 不在这里另写一份，避免两条实现漂移
（漂移的后果是"界面删了、CLI 还认得"这种最难查的不一致）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def overview(settings: Any) -> dict[str, Any]:
    """状态根与各工程的只读概览。**不写任何文件。**"""
    from .project_store import (
        current_project,
        directory_size_bytes,
        list_projects,
        worktree_drive_matches,
    )
    from .state_layout import config_file

    layout = settings.layout
    state_home = Path(layout.state_home)
    active = current_project(state_home=state_home)
    active_key = active.key if active else ""

    projects: list[dict[str, Any]] = []
    for project in list_projects(state_home=state_home):
        projects.append(
            {
                "key": project.key,
                "root": str(project.root),
                "project_dir": str(project.project_dir),
                "active": project.key == active_key,
                "root_exists": Path(project.root).is_dir(),
                "drive_matches": worktree_drive_matches(project.project_dir, project.root),
                "db_bytes": project.db_size_bytes(),
                # 整个状态目录（含 runs / worktrees / 快照）—— "清空重来"要删的就是它，
                # 所以用户该在删之前看到它到底多大。
                "state_bytes": directory_size_bytes(project.project_dir),
            }
        )

    return {
        "state_home": str(state_home),
        "config_file": str(config_file()),
        "bound": bool(layout.bound),
        "active_key": active_key,
        "project_root": str(layout.project_root) if layout.project_root else None,
        # 落点与实际使用的是否一致：切了工程没重启时会是 false。
        "snapshot_matches_active_file": (
            layout.project_key
            == (active.key if active else "")
        ),
        "projects": projects,
        "legacy": _legacy_state(settings),
    }


def _legacy_state(settings: Any) -> dict[str, Any]:
    """旧 `<repo>/workspace/` 还剩什么 —— 迁移的入口。

    只读探测：文件在不在、多大、有多少节点。**不解析内容、不改动**。
    """
    from .config import ROOT_DIR
    from .migration import LEGACY_DB_NAME

    legacy_db = Path(ROOT_DIR) / "workspace" / LEGACY_DB_NAME
    if not legacy_db.is_file():
        return {"present": False, "db_path": str(legacy_db)}
    return {
        "present": True,
        "db_path": str(legacy_db),
        "db_bytes": legacy_db.stat().st_size,
    }


def migration_preview(settings: Any, *, repo_root: Path | None = None) -> dict[str, Any]:
    """迁移**预演**：会迁什么、迁到哪、有没有坑。**不调用 `migrate()`。**

    为什么单独做一步预演：迁移不可逆（虽然实现是"复制"，但用户不知道），
    而"目标工程选错了"是这里唯一会真正造成损失的错。所以让用户在动手前
    看到目标 key、目标目录、旧库大小与节点数、以及每个候选工程能不能用。
    """
    from .config import ROOT_DIR
    from .migration import build_plan, expected_key

    root = Path(repo_root) if repo_root is not None else Path(ROOT_DIR)
    plan = build_plan(root)
    if plan is None:
        return {
            "available": False,
            "reason": "旧工作区里没有 battlefield.sqlite3 —— 没有需要迁移的东西。",
            "repo_root": str(root),
        }

    active = settings.layout.project_root
    return {
        "available": True,
        "repo_root": str(root),
        "legacy_db": str(plan.legacy_db),
        "legacy_db_bytes": plan.legacy_db.stat().st_size,
        "legacy_task_count": plan.task_count,
        "legacy_worktree_rows": plan.worktree_rows,
        # 当前绑定工程的迁移落点 —— 用户最可能想迁到这里。
        "current_project_root": str(active) if active else None,
        "target_key": expected_key(active) if active else None,
        "candidates": [
            {
                "path": str(candidate.path),
                "source": candidate.source,
                "usable": candidate.usable,
                "reason": candidate.reason,
            }
            for candidate in plan.candidates
        ],
    }
