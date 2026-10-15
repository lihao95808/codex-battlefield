"""旧布局 → 新布局的迁移（`docs/spec-state-project-decoupling.md` §10）。

## 迁什么

| 旧位置 | 新位置 |
|---|---|
| `<repo>/workspace/battlefield.sqlite3` | `<state_home>/projects/<key>/battlefield.sqlite3` |
| `TaskNode.worktree_path` 里的旧 worktree 根 | 重写为新的 worktree 根 |
| `<repo>/workspace/codex-home/`（Hub 隔离配置） | `<state_home>/projects/<key>/codex-home/` |

## 四条不可动摇的规矩

1. **复制，不移动。** 旧库留在原地；删除是不可逆动作，由人决定。
2. **必须重写 `TaskNode.worktree_path`。** 它是持久化列，值里带着旧 worktree 根。
   不重写的后果不是"路径难看"，而是 `worktrees.is_managed_worktree`（**删除守卫**）
   与实际路径不一致——会挡住清理，或者更糟：**删错东西**（语义见 spec §11-H7）。
3. **可重入。** 重复跑不产生第二份副本；用副本的 sha256 判断"这个库是不是已经迁过了"。
4. **不猜工程根。** 旧库里的 `constitutions.target_path` 可能指向 `workspace/projects/...`
   这种一次性目录（实测本机就是这样），那**不是**工程根。推断不出来就要求显式传，
   而不是挑一个看着像的。

## 为什么不能靠 target_path 自动推断

实测本机的旧库只有一条 constitution，target_path 是
`D:\\work\\codex-battlefield\\workspace\\projects\\game-618ff8ec-run-5d75656002`
——它在旧工作区**内部**，是一次性目标目录。把它当工程根会把状态挂到一个临时目录上。
所以本模块把这种情况**标成不可用**并说明理由，让人显式指定。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .state_layout import project_key, resolve_state_home
from .project_store import MountedProject, mount, validate_project_root

MIGRATION_MARKER = "migration.json"
LEGACY_DB_NAME = "battlefield.sqlite3"
CODEX_HOME_DIRNAME = "codex-home"


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass
class Candidate:
    """一个可能被当作工程根的路径，附带"能不能用、为什么"。"""

    path: Path
    source: str
    usable: bool
    reason: str


@dataclass
class MigrationPlan:
    legacy_db: Path
    candidates: list[Candidate] = field(default_factory=list)
    task_count: int = 0
    worktree_rows: int = 0

    @property
    def usable(self) -> list[Candidate]:
        return [item for item in self.candidates if item.usable]


def find_legacy_db(repo_root: Path) -> Path | None:
    candidate = Path(repo_root) / "workspace" / LEGACY_DB_NAME
    return candidate if candidate.is_file() else None


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def propose_candidates(legacy_db: Path, repo_root: Path) -> list[Candidate]:
    """从旧库里找出候选工程根，并逐条判定可用性（**不猜**）。"""
    candidates: list[Candidate] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        key = str(path).lower()
        if key in seen:
            return
        seen.add(key)
        if _is_inside(path, Path(repo_root) / "workspace"):
            candidates.append(
                Candidate(
                    path=path,
                    source=source,
                    usable=False,
                    reason=(
                        "它位于旧工作区**内部**（workspace/projects/...），是一次性目标目录，"
                        "不是工程根。挂到它上面会把状态绑到一个临时目录。"
                    ),
                )
            )
        elif not path.is_absolute():
            candidates.append(Candidate(path=path, source=source, usable=False, reason="不是绝对路径。"))
        else:
            candidates.append(Candidate(path=path, source=source, usable=True, reason="看起来是一个真实的工程目录。"))

    uri = "file:///" + str(legacy_db).replace("\\", "/")
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return candidates
    try:
        try:
            rows = con.execute(
                "SELECT DISTINCT target_path FROM constitutions WHERE target_path IS NOT NULL"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for (raw,) in rows:
            if raw:
                add(Path(str(raw)), "constitutions.target_path")
    finally:
        con.close()

    # 仓库自己也是一个候选（旧库放在 <repo>/workspace/ 下，说明树是跑在仓库上的）
    add(Path(repo_root), "旧库位置（<repo>/workspace/）")

    return candidates


def build_plan(repo_root: Path) -> MigrationPlan | None:
    legacy_db = find_legacy_db(repo_root)
    if legacy_db is None:
        return None

    plan = MigrationPlan(legacy_db=legacy_db, candidates=propose_candidates(legacy_db, repo_root))

    uri = "file:///" + str(legacy_db).replace("\\", "/")
    try:
        con = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=10)
        try:
            plan.task_count = con.execute("SELECT COUNT(*) FROM task_nodes").fetchone()[0]
            plan.worktree_rows = con.execute(
                "SELECT COUNT(*) FROM task_nodes WHERE worktree_path IS NOT NULL AND worktree_path != ''"
            ).fetchone()[0]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return plan


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def rewrite_worktree_paths(db_path: Path, *, old_root: Path, new_root: Path) -> int:
    """把 `task_nodes.worktree_path` 里的旧 worktree 根重写为新的。返回改动行数。

    这是**删除守卫**能否继续工作的前提（模块 docstring 的规矩 2）。
    逐个值判断前缀、再写回，避免用 SQL 字符串拼接（路径里有反斜杠与中文）。

    表不存在时返回 0 而不是抛错：新建/空库本来就没有 `task_nodes`，
    那不是错误（实测踩过：对刚复制的空库直接改，报 `no such table: task_nodes`）。
    """
    old_prefix = str(old_root).rstrip("\\/")
    new_prefix = str(new_root).rstrip("\\/")

    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        if not _table_exists(con, "task_nodes"):
            return 0
        rows = con.execute(
            "SELECT id, worktree_path FROM task_nodes WHERE worktree_path IS NOT NULL AND worktree_path != ''"
        ).fetchall()
        changed = 0
        updates: list[tuple[str, str]] = []
        for node_id, raw in rows:
            text = str(raw)
            tail = None
            if text.lower().startswith(old_prefix.lower() + "\\"):
                tail = text[len(old_prefix) + 1 :]
            elif text.lower().startswith(old_prefix.lower() + "/"):
                tail = text[len(old_prefix) + 1 :]
            elif text.lower() == old_prefix.lower():
                tail = ""
            if tail is None:
                continue
            updates.append((f"{new_prefix}\\{tail}" if tail else new_prefix, node_id))
        for value, node_id in updates:
            con.execute("UPDATE task_nodes SET worktree_path = ? WHERE id = ?", (value, node_id))
            changed += 1
        con.commit()
        return changed
    finally:
        con.close()


def ensure_schema(db_path: Path) -> None:
    """让目标库具备当前 schema（建表 + 补列）。

    为什么必须做：旧库是 SQLite 文件，**复制过来只带它自己原有的表**。
    如果它本来就是空的/结构落后的，后面的 `task_nodes` 改写就会
    `no such table`（实测就是这个报错）。

    做法上不新造一套 DDL，也不 import `app.database`（那会在 import 期绑定
    一个引擎，换库还得靠 monkeypatch）。这里直接用 SQLAlchemy 在**指定路径**
    上开会话：`Base.metadata.create_all` 建缺的表，再补上模型里后来加的列。
    """
    from sqlalchemy import create_engine, inspect, text

    from .database import _sqlite_column_additions  # 单一真相：列定义跟产品走
    from .models import Base

    engine = create_engine(f"sqlite:///{Path(db_path).as_posix()}")
    try:
        Base.metadata.create_all(bind=engine)
        additions = _sqlite_column_additions()
        inspector = inspect(engine)
        with engine.begin() as connection:
            for table, columns in additions.items():
                if not inspector.has_table(table):
                    continue
                existing = {column["name"] for column in inspector.get_columns(table)}
                for column, ddl in columns.items():
                    if column in existing:
                        continue
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    finally:
        engine.dispose()


def _already_migrated(project: MountedProject, source_sha: str) -> bool:
    marker = project.project_dir / MIGRATION_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(payload.get("source_sha256") == source_sha and project.db_path.is_file())


@dataclass
class MigrationResult:
    project: MountedProject
    copied: bool
    skipped_reason: str = ""
    worktree_rows_rewritten: int = 0
    codex_home_copied: bool = False
    source_sha256: str = ""


def migrate(
    *,
    repo_root: Path,
    project_root: Path,
    state_home: Path | None = None,
    rewrite_worktrees: bool = True,
) -> MigrationResult:
    """把旧库迁到 `project_root` 对应的工程状态目录。**复制**，不动旧库。"""
    legacy_db = find_legacy_db(repo_root)
    if legacy_db is None:
        raise FileNotFoundError(f"找不到旧库：{Path(repo_root) / 'workspace' / LEGACY_DB_NAME}")

    home = Path(state_home) if state_home is not None else resolve_state_home()
    resolved_root = validate_project_root(project_root)

    # 先挂载（幂等）拿到规范的 key 与目录，再往里拷——
    # 这样不必自己算 key，也不会产生第二份工程目录。
    project = mount(resolved_root, state_home=home, make_active=False)

    source_sha = sha256_file(legacy_db)
    if _already_migrated(project, source_sha):
        return MigrationResult(
            project=project,
            copied=False,
            skipped_reason="这个库（按 sha256 判断）已经迁过了，未产生第二份副本。",
            source_sha256=source_sha,
        )

    project.db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy_db, project.db_path)

    # 先让目标库具备当前 schema，再改它的行。
    # 顺序不能反：旧库可能压根没有 `task_nodes`（实测就是这个报错
    # `no such table: task_nodes`），改写会直接炸。
    ensure_schema(project.db_path)

    old_worktree_root = Path(repo_root) / "workspace" / "worktrees"
    new_worktree_root = project.project_dir / "worktrees"
    new_worktree_root.mkdir(parents=True, exist_ok=True)

    rewritten = 0
    if rewrite_worktrees:
        rewritten = rewrite_worktree_paths(
            project.db_path, old_root=old_worktree_root, new_root=new_worktree_root
        )

    # Hub 的隔离 CODEX_HOME 原来也住在 workspace 里；一起搬，
    # 否则会重演"provider 配置丢失 -> 401"（docs/provider-setup.md §2.1）
    codex_home_copied = False
    legacy_codex_home = Path(repo_root) / "workspace" / CODEX_HOME_DIRNAME
    if legacy_codex_home.is_dir():
        target = project.project_dir / CODEX_HOME_DIRNAME
        if not target.exists():
            shutil.copytree(legacy_codex_home, target, dirs_exist_ok=True)
            codex_home_copied = True

    marker = {
        "source_db": str(legacy_db),
        "source_sha256": source_sha,
        "migrated_to": str(project.db_path),
        "project_root": str(resolved_root),
        "worktree_rows_rewritten": rewritten,
        "codex_home_copied": codex_home_copied,
        "old_worktree_root": str(old_worktree_root),
        "new_worktree_root": str(new_worktree_root),
    }
    (project.project_dir / MIGRATION_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return MigrationResult(
        project=project,
        copied=True,
        worktree_rows_rewritten=rewritten,
        codex_home_copied=codex_home_copied,
        source_sha256=source_sha,
    )


def expected_key(project_root: Path) -> str:
    return project_key(project_root)
