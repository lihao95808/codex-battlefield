from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base


settings = get_settings()
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@event.listens_for(Engine, "before_cursor_execute")
def prevent_accidental_production_schema_drop(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
    if _should_block_destructive_sql(_live_database_url(), statement):
        raise RuntimeError(
            "Refusing to drop tables in the production Codex Battlefield database. "
            "Set CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP=1 only for an intentional manual recovery."
        )


def _live_database_url() -> str:
    """当前生效的库 URL。

    读**模块属性**而不是闭包捕获的常量：`tests/test_constitution_versions.py`
    会 `monkeypatch.setattr(database, "settings", ...)` 换掉库来做迁移测试，
    闭包捕获旧值会让守卫对着**上一个**库判断。
    """
    return getattr(settings, "database_url", "") or ""


def _should_block_destructive_sql(database_url: str, statement: str) -> bool:
    if os.getenv("CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP", "").lower() in {"1", "true", "yes"}:
        return False
    normalized = " ".join(statement.lower().split())
    if "drop table" not in normalized:
        return False
    return not _is_disposable_database_url(database_url)


def _is_disposable_database_url(database_url: str) -> bool:
    """这个库是不是**随手可丢**的（测试库）？

    ## 为什么把语义从"是不是生产库"反过来（2026-10-14，H3）

    原先的判据是"路径 == `<repo>/workspace/battlefield.sqlite3` 才算生产库"。
    状态与工程解耦之后，库会落到 `<state_home>/projects/<key>/` 或调用方指定的
    任意位置，于是它与那条硬编码路径**永远不相等** ——
    `DROP TABLE` 保护在**真正的活库**上静默失效。它不报错，只是不再保护。

    而且当时 `WORKSPACE_DIR` 已经不再是默认库位置，那个比较连回退路径都算不上。

    ## 现在的判据（失效方向刻意选"宁可挡错"）

    只有**明确可丢**的库才放行，其余一律当生产库保护：

    * SQLite 临时目录下的库（pytest 的 `tmp_path` 就在那儿）

    反过来判"是生产库"没法做对：生产库现在可以在任何位置
    （`<state_home>/projects/<key>/`、`CODEX_BATTLEFIELD_DATABASE_URL` 指定的任意路径、
    以及迁移期仍在用的 `<repo>/workspace/`），没有可枚举的特征。
    挡住一次合法的 DROP 只是多敲一个环境变量；放过一次非法的 DROP 是不可逆的数据丢失。
    """
    if not database_url.startswith("sqlite:///"):
        # 非 SQLite（例如将来换 Postgres）不在本守卫的射程内
        return False
    raw_path = database_url.removeprefix("sqlite:///")
    try:
        db_path = Path(raw_path).resolve()
    except OSError:
        return False

    temp_roots: list[Path] = []
    for candidate in (os.getenv("TEMP"), os.getenv("TMP"), tempfile.gettempdir()):
        if not candidate:
            continue
        try:
            temp_roots.append(Path(candidate).resolve())
        except OSError:
            continue
    for root in temp_roots:
        try:
            db_path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()


def _sqlite_column_additions() -> dict[str, dict[str, str]]:
    """SQLite 建表后要补的列（表 → {列名: DDL}）。

    **抽成函数是为了只有一个真相**：迁移脚本（`app.migration.ensure_schema`）
    要在**另一个库文件**上做同样的事。DDL 抄一份到迁移脚本里迟早会漂移，
    而漂移的表现是"迁过来的库缺列 → 运行时报错"，且只在迁移路径上出现。
    """
    return {
        "task_nodes": {
            "node_meta_json": "TEXT DEFAULT '{}'",
            "constitution_id": "VARCHAR(40)",
            "constitution_hash": "VARCHAR(80)",
            "constitution_json": "TEXT DEFAULT '{}'",
            "constitution_version": "INTEGER",
            "leaf_attempts": "INTEGER DEFAULT 0",
            "verification_failures": "INTEGER DEFAULT 0",
            "converted_from_leaf": "BOOLEAN DEFAULT 0",
        },
        "run_records": {
            "prompt_path": "TEXT",
            "last_message_path": "TEXT",
            "workdir": "TEXT",
            "constitution_hash": "VARCHAR(80)",
            "constitution_version": "INTEGER",
            "usage_json": "TEXT",
        },
        "constitutions": {
            "supersedes_id": "VARCHAR(40)",
            "amendment_direction": "VARCHAR(16)",
            "effective_at": "DATETIME",
        },
        "steward_messages": {
            "metadata_json": "TEXT DEFAULT '{}'",
        },
    }


def _migrate_sqlite_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    additions = _sqlite_column_additions()
    with engine.begin() as connection:
        for table, columns in additions.items():
            # 表可能还不存在 —— 真实场景里这是**正常**的：
            # `init_db()` 建全部表，但迁移路径（老库原地补列）可能面对一个
            # 还没有新表的库。不加这个判断就会 `no such table` 直接把启动打挂。
            # （实测：给旧库跑迁移时，`steward_messages` 还没建出来。）
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).first()
            if not exists:
                continue
            rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            for column, ddl in columns.items():
                if column not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
