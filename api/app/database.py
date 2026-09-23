from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import WORKSPACE_DIR, get_settings
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
    if _should_block_destructive_sql(settings.database_url, statement):
        raise RuntimeError(
            "Refusing to drop tables in the production Codex Battlefield database. "
            "Set CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP=1 only for an intentional manual recovery."
        )


def _should_block_destructive_sql(database_url: str, statement: str) -> bool:
    if os.getenv("CODEX_BATTLEFIELD_ALLOW_PRODUCTION_SCHEMA_DROP", "").lower() in {"1", "true", "yes"}:
        return False
    normalized = " ".join(statement.lower().split())
    if "drop table" not in normalized:
        return False
    return _is_production_database_url(database_url)


def _is_production_database_url(database_url: str) -> bool:
    if not database_url.startswith("sqlite:///"):
        return False
    raw_path = database_url.removeprefix("sqlite:///")
    try:
        db_path = Path(raw_path).resolve()
        production_path = (WORKSPACE_DIR / "battlefield.sqlite3").resolve()
    except OSError:
        return False
    return db_path == production_path


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_columns()


def _migrate_sqlite_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    task_additions = {
        "node_meta_json": "TEXT DEFAULT '{}'",
        "constitution_id": "VARCHAR(40)",
        "constitution_hash": "VARCHAR(80)",
        "constitution_json": "TEXT DEFAULT '{}'",
        "constitution_version": "INTEGER",
        "leaf_attempts": "INTEGER DEFAULT 0",
        "verification_failures": "INTEGER DEFAULT 0",
        "converted_from_leaf": "BOOLEAN DEFAULT 0",
    }
    run_additions = {
        "prompt_path": "TEXT",
        "last_message_path": "TEXT",
        "workdir": "TEXT",
        "constitution_hash": "VARCHAR(80)",
        "constitution_version": "INTEGER",
        "usage_json": "TEXT",
    }
    constitution_additions = {
        "supersedes_id": "VARCHAR(40)",
        "amendment_direction": "VARCHAR(16)",
        "effective_at": "DATETIME",
    }
    with engine.begin() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(task_nodes)").fetchall()
        existing = {row[1] for row in rows}
        for column, ddl in task_additions.items():
            if column not in existing:
                connection.exec_driver_sql(f"ALTER TABLE task_nodes ADD COLUMN {column} {ddl}")
        rows = connection.exec_driver_sql("PRAGMA table_info(run_records)").fetchall()
        existing = {row[1] for row in rows}
        for column, ddl in run_additions.items():
            if column not in existing:
                connection.exec_driver_sql(f"ALTER TABLE run_records ADD COLUMN {column} {ddl}")
        rows = connection.exec_driver_sql("PRAGMA table_info(constitutions)").fetchall()
        existing = {row[1] for row in rows}
        for column, ddl in constitution_additions.items():
            if column not in existing:
                connection.exec_driver_sql(f"ALTER TABLE constitutions ADD COLUMN {column} {ddl}")


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
