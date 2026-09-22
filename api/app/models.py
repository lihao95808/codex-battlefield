from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    PLANNED = "planned"
    PLANNING = "planning"
    DECOMPOSING = "decomposing"
    QUEUED = "queued"
    WAITING_CHILDREN = "waiting_children"
    RUNNING = "running"
    WORKING = "working"
    VERIFYING = "verifying"
    ACCEPTING = "accepting"
    DONE = "done"
    REPAIR = "repair"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"
    HIBERNATING = "hibernating"
    CANCELED = "canceled"


class RunKind(StrEnum):
    CONSTITUTION = "constitution"
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"
    ACCEPTANCE = "acceptance"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = {
    TaskStatus.DONE,
    TaskStatus.BLOCKED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}

STOPPED_STATUSES = {
    TaskStatus.PAUSED,
    TaskStatus.HIBERNATING,
}


class TaskNode(Base):
    __tablename__ = "task_nodes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("task_nodes.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PLANNED.value)
    scope_json: Mapped[str] = mapped_column(Text, default="[]")
    acceptance_json: Mapped[str] = mapped_column(Text, default="[]")
    node_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    constitution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    constitution_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    constitution_json: Mapped[str] = mapped_column(Text, default="{}")
    constitution_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaf_attempts: Mapped[int] = mapped_column(Integer, default=0)
    verification_failures: Mapped[int] = mapped_column(Integer, default=0)
    converted_from_leaf: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(240), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parent: Mapped["TaskNode | None"] = relationship(remote_side="TaskNode.id", back_populates="children")
    children: Mapped[list["TaskNode"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    runs: Mapped[list["RunRecord"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    events: Mapped[list["EventRecord"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class ConstitutionRecord(Base):
    __tablename__ = "constitutions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    root_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    amendment_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80))
    markdown: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunRecord(Base):
    __tablename__ = "run_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_nodes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED.value)
    command: Mapped[str] = mapped_column(Text, default="")
    sandbox: Mapped[str] = mapped_column(String(64), default="read-only")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    workdir: Mapped[str | None] = mapped_column(Text, nullable=True)
    constitution_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    constitution_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[TaskNode] = relationship(back_populates="runs")


class EventRecord(Base):
    __tablename__ = "event_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task_nodes.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    task: Mapped[TaskNode | None] = relationship(back_populates="events")
