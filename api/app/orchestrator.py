from __future__ import annotations

import asyncio
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import CODEX_KINDS, ROOT_DIR, Settings, get_settings, machine_root_of
from .database import SessionLocal
from .events import event_bus
from . import trace
from .models import ConstitutionRecord, EventRecord, RunKind, RunRecord, RunStatus, STOPPED_STATUSES, TaskNode, TaskStatus, TERMINAL_STATUSES, utcnow
from . import blockers
from . import budget
from . import calibration
from . import checkpoint
from . import concurrency
from . import constitution_versions
from . import constraints
from . import convergence
from . import failure_retrospective
from . import godot_files
from . import godot_gates
from . import integration
from . import isolation
from . import obligations
from . import runtime_markers
from . import runtime_logs
from . import recovery
from . import regression
from . import requirements
from . import skill_notes
from . import verification_surface
from . import watchdog
from . import worktrees
from .codex_signals import (
    is_local_skill_failure,
)
from .godot_gates import (
    AUDIO_ASSET_SUFFIXES,
    IMAGE_ASSET_SUFFIXES,
    TEXT_DELIVERABLE_SUFFIXES,
)
from .runner import (
    BaseCodexRunner,
    RunnerEnvelope,
    make_runner,
    usage_from_codex_logs,
)
from .schemas import AcceptanceResult, ChildTaskSpec, PlannerDecision, VerifierResult, WorkerResult
from .usage import summarize_usages
from .utils import (
    coerce_utc,
    dumps,
    has_irreversible_question_mark_corruption,
    has_mojibake_corruption,
    loads,
    new_id,
    repair_mojibake,
    repair_mojibake_text,
)


ACTIVE_FRONTIER_STATUSES = concurrency.ACTIVE_FRONTIER_STATUSES

ACTIVE_AGENT_STATUSES = runtime_logs.ACTIVE_AGENT_STATUSES

CONVERGENCE_SLICE_REPEAT_LIMIT = convergence.CONVERGENCE_SLICE_REPEAT_LIMIT
CODEX_LOG_TIMESTAMP_RE = runtime_logs.CODEX_LOG_TIMESTAMP_RE
WORKER_EMPTY_DECOMPOSITION_RETRY_LIMIT = 1
RETRYABLE_BLOCK_CATEGORIES = blockers.RETRYABLE_BLOCK_CATEGORIES
RESOURCE_PRESSURE_BLOCK_CATEGORIES = blockers.RESOURCE_PRESSURE_BLOCK_CATEGORIES
FAST_RETRYABLE_BLOCK_CATEGORIES = blockers.FAST_RETRYABLE_BLOCK_CATEGORIES
FAST_RETRYABLE_BLOCK_RETRY_LIMIT = 2
PENDING_CODEX_SLOT_RESERVATION_SECONDS = 45
CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS = 300
WORKSPACE_WRITE_BLOCK_SOURCES = blockers.WORKSPACE_WRITE_BLOCK_SOURCES
PROTOCOL_FAILURE_SOURCES = blockers.PROTOCOL_FAILURE_SOURCES
WORKER_EMPTY_DECOMPOSITION_EXECUTION_HINTS = (
    "can complete",
    "can be completed",
    "can safely complete",
    "i can complete",
    "i will implement",
    "i will generate",
    "i will directly",
    "directly execute",
    "directly deliver",
    "bounded enough",
    "clear boundary",
    "leaf boundary",
    "300% complete",
    "\u53ef\u4ee5\u5b8c\u6210",
    "\u53ef\u5b8c\u6210",
    "\u80fd\u591f\u5b8c\u6210",
    "\u80fd\u5b8c\u6210",
    "\u76f4\u63a5\u5b8c\u6210",
    "\u76f4\u63a5\u6267\u884c",
    "\u76f4\u63a5\u4ea4\u4ed8",
    "\u6211\u5c06",
    "\u8fb9\u754c\u6e05\u6670",
    "\u8fb9\u754c\u660e\u786e",
    "300% \u5b8c\u6210",
    "300%\u5b8c\u6210",
    "300%\u53ef\u5b8c\u6210",
)
WORKER_EMPTY_DECOMPOSITION_SPLIT_HINTS = (
    "cannot complete",
    "can't complete",
    "cannot be completed",
    "must split",
    "must decompose",
    "not 300%",
    "not safe",
    "\u65e0\u6cd5\u5b8c\u6210",
    "\u4e0d\u80fd\u5b8c\u6210",
    "\u5fc5\u987b\u62c6",
    "\u9700\u8981\u62c6",
    "\u9700\u8981\u7ee7\u7eed\u62c6",
    "\u65e0\u6cd5\u76f4\u63a5",
    "\u4e0d\u80fd\u76f4\u63a5",
    "\u4e0d\u786e\u5b9a",
)

SKILL_NOTE_COVERAGE_ID = skill_notes.SKILL_NOTE_COVERAGE_ID
SKILL_NOTE_VERIFICATION_KIND = skill_notes.SKILL_NOTE_VERIFICATION_KIND


class DynamicConcurrencyLimiter:
    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active_count(self) -> int:
        return self._active

    async def set_capacity(self, capacity: int) -> tuple[int, int]:
        new_capacity = max(1, int(capacity))
        async with self._condition:
            old_capacity = self._capacity
            self._capacity = new_capacity
            self._condition.notify_all()
        return old_capacity, new_capacity

    async def __aenter__(self) -> "DynamicConcurrencyLimiter":
        async with self._condition:
            while self._active >= self._capacity:
                await self._condition.wait()
            self._active += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        async with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


class Orchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._background_tasks: set[asyncio.Task] = set()
        self._auto_resume_tasks: dict[str, asyncio.Task] = {}
        self._deferred_parent_tasks: dict[str, asyncio.Task] = {}
        self._node_locks: dict[str, asyncio.Lock] = {}
        self._agent_starts_by_root: Counter[str] = Counter()
        initial_capacity = max(1, self.settings.max_concurrency)
        if self.settings.auto_concurrency_enabled:
            auto_upper = max(1, int(self.settings.max_concurrency), int(self.settings.auto_concurrency_max))
            auto_lower = max(1, min(int(self.settings.auto_concurrency_min), auto_upper))
            initial_capacity = auto_upper if self.settings.goal_mode else auto_lower
        self._agent_semaphore = DynamicConcurrencyLimiter(initial_capacity)
        self._external_codex_process_cache: tuple[datetime, list[dict[str, Any]]] | None = None
        self._external_codex_process_scan_failed = False
        self._pending_codex_slot_reservations: dict[str, datetime] = {}

    def _budget_guard_path(self) -> Path:
        return budget.budget_guard_path(machine_root_of(self.settings))

    def _codex_slot_reservations_path(self) -> Path:
        # 机器级：槽位按**真机上真实存在的 codex 进程数**算（还带跨进程字节锁）。
        # 写进某个工程目录会让多个工程各拿一套槽位，等于没有并发上限（spec D3 / §11-H1）。
        return machine_root_of(self.settings) / "codex_slot_reservations.json"

    def _codex_slot_reservations_lock_path(self) -> Path:
        return machine_root_of(self.settings) / "codex_slot_reservations.lock"

    def _read_budget_guard(self) -> dict[str, Any]:
        return budget.read_budget_guard(machine_root_of(self.settings))

    def _write_budget_guard(self, payload: dict[str, Any]) -> None:
        budget.write_budget_guard(machine_root_of(self.settings), payload)

    def _active_budget_guard(self, root_id: str | None = None) -> dict[str, Any] | None:
        return budget.active_budget_guard(machine_root_of(self.settings), root_id)

    async def set_budget_guard(
        self,
        root_id: str | None,
        resume_after_seconds: int,
        reason: str,
        hibernate_tree: bool = True,
    ) -> dict[str, Any]:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        resume_after_seconds = max(60, int(resume_after_seconds))
        reason = repair_mojibake_text(reason).strip() or "余额不足，保存状态并低功耗等待账户额度刷新。"
        resume_at = utcnow() + timedelta(seconds=resume_after_seconds)
        hibernated_count = 0
        if root_id and hibernate_tree:
            result = await self.hibernate_tree(root_id, resume_after_seconds, reason)
            hibernated_count = int(result.get("count") or 0)
        payload = {
            "status": "active",
            "root_id": root_id,
            "resume_at": resume_at.isoformat(),
            "reason": reason,
            "created_at": utcnow().isoformat(),
            "resume_after_seconds": resume_after_seconds,
        }
        self._write_budget_guard(payload)
        if root_id:
            with SessionLocal() as db:
                if db.get(TaskNode, root_id) is not None:
                    self._add_event(
                        db,
                        root_id,
                        "budget.guard_hibernate",
                        "预算守卫已生效：余额不足时保持低功耗，等待账户额度刷新后再允许恢复。",
                        {
                            "resume_at": resume_at.isoformat(),
                            "reason": reason,
                            "hibernated_count": hibernated_count,
                        },
                    )
                    db.commit()
        return {
            "status": "budget_guard_active",
            "root_id": root_id,
            "active": True,
            "resume_at": resume_at,
            "reason": reason,
            "hibernated_count": hibernated_count,
        }

    def clear_budget_guard(self, root_id: str | None = None) -> dict[str, Any]:
        return budget.clear_budget_guard(
            machine_root_of(self.settings),
            root_id,
            frozen_mode=self.settings.frozen_mode,
        )

    def _api_restart_flag_path(self) -> Path:
        return watchdog.api_restart_flag_path(self.settings.workspace_dir)

    def _api_restart_drain_flag_path(self) -> Path:
        return watchdog.api_restart_drain_flag_path(self.settings.workspace_dir)

    def _api_restart_pending(self) -> bool:
        return self._api_restart_flag_path().is_file()

    def _api_restart_drain_pending(self) -> bool:
        return self._api_restart_drain_flag_path().is_file()

    def set_api_restart_drain(self, armed: bool) -> dict[str, Any]:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        restart_flag = self._api_restart_flag_path()
        drain_flag = self._api_restart_drain_flag_path()
        restart_flag.parent.mkdir(parents=True, exist_ok=True)
        if armed:
            if not restart_flag.is_file():
                restart_flag.write_text("load latest code when drain completes\n", encoding="ascii")
            drain_flag.write_text("allow short hibernation for maintenance restart\n", encoding="ascii")
            status = "armed"
        else:
            drain_flag.unlink(missing_ok=True)
            status = "disarmed"
        return {
            "status": status,
            "api_restart_pending": restart_flag.is_file(),
            "api_restart_flag_path": str(restart_flag),
            "api_restart_drain_armed": drain_flag.is_file(),
            "api_restart_drain_flag_path": str(drain_flag),
        }

    def _hold_agent_start_for_api_restart(self, task_id: str, phase: str) -> bool:
        if not self._api_restart_drain_pending():
            return False
        flag_path = self._api_restart_drain_flag_path()
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return True
            root_id = self._root_id_for_task(db, task)
            root = db.get(TaskNode, root_id)
            root_meta = loads(root.node_meta_json, {}) if root is not None else {}
            runner_mode = root_meta.get("runner_mode") if isinstance(root_meta, dict) else None
            if (str(runner_mode) if runner_mode in {"mock", "real"} else self.settings.runner_mode) != "real":
                return False
            meta = loads(task.node_meta_json, {})
            last_drain = meta.get("api_restart_drain") if isinstance(meta.get("api_restart_drain"), dict) else {}
            already_reported = last_drain.get("phase") == phase and last_drain.get("flag_path") == str(flag_path)
            meta["api_restart_drain"] = {
                "phase": phase,
                "flag_path": str(flag_path),
                "updated_at": utcnow().isoformat(),
            }
            task.node_meta_json = dumps(meta)
            self._mark(
                db,
                task,
                TaskStatus.QUEUED,
                "API 有待加载更新，调度器进入维护排空：暂不启动新的 Codex CLI，等待现有活跃 agent 归零后安全重启。",
            )
            if not already_reported:
                payload = {"task_id": task_id, "root_id": root_id, "phase": phase, "flag_path": str(flag_path)}
                self._add_event(
                    db,
                    task.id,
                    "scheduler.api_restart_drain",
                    "API 更新标记已存在；该节点暂缓启动新的 Codex CLI，等待维护排空后由 watchdog 重启服务。",
                    payload,
                )
                if root_id != task.id:
                    self._add_event(
                        db,
                        root_id,
                        "scheduler.api_restart_drain",
                        "检测到 API 更新标记；调度器暂停补位新的 Codex CLI，等待活跃 agent 跑完后安全重启。",
                        payload,
                    )
            db.commit()
        return True

    def budget_guard_state(self, root_id: str | None = None) -> dict[str, Any]:
        return budget.budget_guard_state(machine_root_of(self.settings), root_id)

    async def draft_constitution(self, goal: str, target_path: str | None = None, runner_mode: str | None = None) -> str:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        goal = repair_mojibake_text(goal)
        target_path = repair_mojibake_text(target_path) if target_path else None
        runner = make_runner(self.settings, runner_mode)
        envelope = await runner.draft_constitution(goal, target_path)
        payload = repair_mojibake(envelope.payload)
        markdown = str(payload.get("markdown") or "# 项目宪法\n\n生成器未返回正文。")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        data = repair_mojibake(data)
        constitution_id = new_id("const")
        content_hash = self._constitution_hash(markdown, data)
        with SessionLocal() as db:
            record = ConstitutionRecord(
                id=constitution_id,
                goal=goal,
                target_path=target_path,
                status="draft",
                version=1,
                content_hash=content_hash,
                markdown=markdown,
                data_json=dumps(data),
            )
            db.add(record)
            self._record_constitution_run(db, constitution_id, envelope)
            self._add_event(
                db,
                None,
                "constitution.drafted",
                "项目宪法草案已生成，等待锁定后启动任务树。",
                {"constitution_id": constitution_id, "hash": content_hash},
            )
            db.commit()
        return constitution_id

    def update_constitution(self, constitution_id: str, markdown: str, data: dict[str, Any]) -> None:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        markdown = repair_mojibake_text(markdown)
        data = repair_mojibake(data)
        with SessionLocal() as db:
            record = self._get_constitution(db, constitution_id)
            if record.status == "locked":
                raise ValueError("已锁定的项目宪法不能修改。")
            record.markdown = markdown
            record.data_json = dumps(data)
            record.version += 1
            record.content_hash = self._constitution_hash(markdown, data)
            record.updated_at = utcnow()
            self._add_event(
                db,
                None,
                "constitution.updated",
                "项目宪法草案已更新。",
                {"constitution_id": constitution_id, "hash": record.content_hash, "version": record.version},
            )
            db.commit()

    def lock_constitution(self, constitution_id: str) -> None:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        with SessionLocal() as db:
            record = self._get_constitution(db, constitution_id)
            record.status = "locked"
            record.locked_at = utcnow()
            record.updated_at = utcnow()
            self._add_event(
                db,
                None,
                "constitution.locked",
                "项目宪法已锁定，后续 agent 必须继承该版本。",
                {"constitution_id": constitution_id, "hash": record.content_hash, "version": record.version},
            )
            db.commit()

    def amend_constitution(
        self,
        root_id: str,
        *,
        markdown: str,
        data: dict[str, Any],
        direction: str,
        actor: str,
        note: str = "",
        revalidate_exemptions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """在树运行中修订宪法：新增一版、按状态分治迁移、登记再验证队列。

        **一次修订是一个事务**（`docs/spec-constitution-amendment.md` §8）：新版本行、
        `constitution.amended` 事件、节点迁移、再验证债务要么全落地要么全不落地。中途失败
        即整体回滚，所以"新版本行存在"本身等价于"这次修订已完成"，续跑判定就靠它。

        **权限边界**（§7）：`direction` 必须由调用方**显式**声明，`actor` 必须非空。
        root agent 不能自己改宪法——它只能产出修订建议。这里不做默认补全：一个漏填的
        `direction` 若被默认成 `loosen`，就是产出方自己给自己放宽。

        **按状态分治**（§6）：不做"全树刷一遍"。已完成的节点不自动重做（收紧时进再验证
        队列）；执行中的节点只登记、不打断（中途换版会产出半旧半新的产物）。

        **没接线的部分**（照实写）：对**已完成的叶子**，再验证由 `_revalidate_stale_leaves` 在父节点
        验收前执行（`constitution_revalidate_enabled`）。本方法负责"登记"与"迁移"；`blocked`/`failed`
        的节点在这里被**重新分类并重新排队**（§6），但**起跑由调用方决定**（路由在事务提交后 kick），
        一次修订事务里不该顺手拉起一堆 agent。
        """
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        if direction not in constitution_versions.DIRECTIONS:
            raise ValueError(
                "宪法修订必须显式声明方向（tighten / loosen / mixed / neutral），"
                "不能由代码推断：产出方自己判断方向等于自己放水。"
            )
        actor = repair_mojibake_text(actor or "").strip()
        if not actor:
            raise ValueError("宪法修订必须标明发起者：这是人的决定，不是 agent 的。")
        markdown = repair_mojibake_text(markdown)
        data = repair_mojibake(data)
        # 节点级豁免：**理由必填**。豁免是"有意不带这个节点走新宪法"，没有理由的豁免
        # 与"静默放宽"没有区别（AGENTS 硬约定 4），所以在事务之外先拒掉。
        exemptions: dict[str, str] = {}
        for node_id, reason in (revalidate_exemptions or {}).items():
            cleaned_reason = repair_mojibake_text(str(reason or "")).strip()
            if not str(node_id or "").strip():
                raise ValueError("再验证豁免必须给出节点 id。")
            if not cleaned_reason:
                raise ValueError(f"再验证豁免必须给出理由（节点 {node_id}）：没有理由的豁免就是静默放宽。")
            exemptions[str(node_id).strip()] = cleaned_reason

        with SessionLocal() as db:
            # 只做存在性校验：根节点不在时这里就抛（`_get_task` 抛 `KeyError`），
            # 不然后面那句"没有绑定宪法版本"会把"根节点根本不存在"报成一个风马牛不相及的原因。
            self._get_task(db, root_id)
            head = db.scalars(
                select(ConstitutionRecord)
                .where(ConstitutionRecord.root_id == root_id)
                .order_by(ConstitutionRecord.version.desc())
            ).first()
            if head is None:
                raise ValueError(f"任务树 {root_id} 还没有绑定任何项目宪法版本。")
            content_hash = self._constitution_hash(markdown, data)
            if head.content_hash == content_hash:
                # 幂等键是 `(root_id, content_hash)`——与 §8 写的 `(root_id, to_version)` 等价，
                # 因为重放一定会重算出同样的目标版本。**但不能拿"下一版号"当键**：它会随着头
                # 移动，于是同一次修订重复调用会一路 v2、v3 加下去（这正是第一版实现的问题）。
                # 取内容当键还有一层好处：头已经就是要改成的样子时，不该凭空造出一版。
                return {
                    "status": "already_applied",
                    "root_id": root_id,
                    "version": head.version,
                    "constitution_id": head.id,
                    "direction": head.amendment_direction,
                }
            to_version = head.version + 1

            amendment = ConstitutionRecord(
                id=new_id("const"),
                root_id=root_id,
                goal=head.goal,
                target_path=head.target_path,
                status="locked",
                version=to_version,
                supersedes_id=head.id,
                amendment_direction=direction,
                effective_at=utcnow(),
                content_hash=content_hash,
                markdown=markdown,
                data_json=dumps(data),
                locked_at=utcnow(),
            )
            db.add(amendment)

            tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root_id)
            migrated: list[str] = []
            stamped: list[str] = []
            registered: list[str] = []
            reclassified: list[str] = []
            exempted: list[str] = []
            skipped_unknown: list[str] = []
            by_action: dict[str, int] = {}
            by_status: dict[str, int] = {}
            queued: list[TaskNode] = []
            known_ids = {task.id for task in tasks}
            unknown_exemptions = sorted(set(exemptions) - known_ids)
            if unknown_exemptions:
                # 不存在的节点 id 会被静默忽略，人被"豁免成功了"误导 → 直接拒。
                raise ValueError(f"再验证豁免里有不属于这棵树的节点 id：{unknown_exemptions}")
            for task in tasks:
                if constitution_versions.normalize_version(task.constitution_version) is None:
                    skipped_unknown.append(task.id)
                    continue
                if not constitution_versions.is_stale(task, to_version):
                    continue
                action = constitution_versions.action_for_status(task.status, direction=direction)
                by_action[action] = by_action.get(action, 0) + 1
                by_status[task.status] = by_status.get(task.status, 0) + 1
                if action == constitution_versions.WAIT_RUNNING:
                    # 只登记不打断：中途换版会让产物半旧半新，且 constitution_verdict 与实际不符。
                    # 兑现登记的那一步在 `_enqueue_revalidate_after_run`（节点完成时）。
                    meta = loads(task.node_meta_json, {})
                    meta["revalidate_after_run"] = {"version": to_version, "direction": direction, "actor": actor}
                    task.node_meta_json = dumps(meta)
                    registered.append(task.id)
                    queued.append(task)
                    continue
                if action == constitution_versions.REVALIDATE or action == constitution_versions.STAMP_ONLY:
                    # `done` 是既成事实：不自动重做。
                    # - `STAMP_ONLY`（放宽/中性）：产物仍然合规，只更新**版本戳**；快照三件套
                    #   （id/hash/json）保持"产物实际产生于哪一版"，两者语义不同，刻意不强行对齐。
                    # - `REVALIDATE`（收紧）：连版本戳也不给——产物按旧版验收过，用新版本号给它
                    #   盖章就是静默 claim 通过，正是本设计要消灭的那种。
                    if action == constitution_versions.STAMP_ONLY:
                        task.constitution_version = to_version
                        task.updated_at = utcnow()
                        stamped.append(task.id)
                    elif task.id in exemptions:
                        # 节点级豁免：**不进队列、也不盖新版本戳**——产物是按旧版验收的，
                        # 用新版本号给它盖章就是静默 claim 通过（§6）。豁免只记在这一个节点上
                        # 并写明版本，所以下一次收紧它照样进队列（债键含版本）。
                        task_meta = loads(task.node_meta_json, {})
                        task_meta["constitution_revalidate_exemption"] = {
                            "version": to_version,
                            "from_version": head.version,
                            "reason": exemptions[task.id],
                            "actor": actor,
                            "at": utcnow().isoformat(),
                        }
                        task.node_meta_json = dumps(task_meta)
                        exempted.append(task.id)
                        self._add_event(
                            db,
                            task.id,
                            "constitution.revalidate_exempted",
                            f"该节点按旧宪法（v{task.constitution_version}）验收过，人显式豁免它跟着 v{to_version} 再验证。",
                            {
                                "node_id": task.id,
                                "from_version": head.version,
                                "to_version": to_version,
                                "actor": actor,
                                "reason": exemptions[task.id],
                            },
                        )
                    else:
                        queued.append(task)
                    continue
                if action == constitution_versions.RECLASSIFY:
                    # §6「blocked/failed：按新宪法重新分类并重试」。
                    # **只换快照不算"重新分类"**：节点还停在 blocked/failed 上，什么都不会发生。
                    # 但**放宽方向不允许重新排队**（K4：放宽不得追溯洗白已失败/阻塞的节点）——
                    # 那种情况只换快照，节点保持原状态，等人工或既有修复通路处理。
                    self._replace_constitution_snapshot(task, amendment)
                    if not constitution_versions.forces_revalidation(direction):
                        migrated.append(task.id)
                        continue
                    # 重新排进队列（与 `retry_task` 同一套状态迁移）；跑与不跑由路由在事务
                    # 提交后决定——一次修订事务里不该顺手起一堆 agent。
                    task.status = TaskStatus.QUEUED.value
                    task.error_message = None
                    task.updated_at = utcnow()
                    task_meta = loads(task.node_meta_json, {})
                    task_meta["last_reclassify_at"] = utcnow().isoformat()
                    task_meta["last_reclassify_version"] = to_version
                    task.node_meta_json = dumps(task_meta)
                    reclassified.append(task.id)
                    self._add_event(
                        db,
                        task.id,
                        "task.reclassified_by_amendment",
                        f"宪法已修订到 v{to_version}，该节点按新宪法重新分类并重新排队重试。",
                        {"from_version": head.version, "to_version": to_version, "actor": actor},
                    )
                    continue
                self._replace_constitution_snapshot(task, amendment)
                migrated.append(task.id)

            obligations_by_owner = constitution_versions.version_debt(
                queued, effective_version=to_version, direction=direction
            )
            for owner_id, debt in obligations_by_owner.items():
                owner = db.get(TaskNode, owner_id)
                if owner is None:
                    continue
                meta = loads(owner.node_meta_json, {})
                existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
                meta["obligations"] = obligations.upsert(existing, debt)
                owner.node_meta_json = dumps(meta)

            # B 部分：修订是**人**的动作，所以它有权重新冻结验收面——但**只在声明的路径清单变了**时。
            # 清单没变、只是内容变了，那是"改考卷"，不许靠一次无关的修订把它洗白（那种改动要由
            # 篡改门禁拦下，或由人显式改声明）。
            root_node = db.get(TaskNode, root_id)
            if root_node is not None:
                previous = loads(root_node.node_meta_json, {}).get(verification_surface.SURFACE_KEY)
                declared, _invalid = verification_surface.declared_paths(
                    data.get(verification_surface.DECLARED_KEY)
                )
                if verification_surface.declared_changed(previous, declared):
                    self._freeze_acceptance_surface(
                        db, root_node, data=data, version=to_version, frozen_by=f"amend:{actor}"
                    )

            self._add_event(
                db,
                root_id,
                "constitution.amended",
                f"项目宪法已修订到 v{to_version}（{direction}），存量节点按状态分治迁移。",
                {
                    "root_id": root_id,
                    "from_version": head.version,
                    "to_version": to_version,
                    "from_constitution_id": head.id,
                    "to_constitution_id": amendment.id,
                    "direction": direction,
                    "actor": actor,
                    "note": repair_mojibake_text(note or ""),
                    "affected_by_status": by_status,
                    "affected_by_action": by_action,
                    "migrated_ids": migrated,
                    "stamped_ids": stamped,
                    "revalidate_registered_ids": registered,
                    "reclassified_ids": reclassified,
                    "revalidate_required_ids": [task.id for task in queued if task.status == TaskStatus.DONE.value],
                    "revalidate_exempted_ids": exempted,
                    "unknown_version_ids": skipped_unknown,
                },
            )
            db.commit()
            summary = {
                "status": "amended",
                "root_id": root_id,
                "from_version": head.version,
                "to_version": to_version,
                "constitution_id": amendment.id,
                "content_hash": content_hash,
                "direction": direction,
                "migrated_count": len(migrated),
                "stamped_count": len(stamped),
                "registered_count": len(registered),
                "reclassified_count": len(reclassified),
                "reclassified_ids": reclassified,
                "revalidate_queued_count": len(queued),
                "revalidate_exempted_count": len(exempted),
                "revalidate_exempted_ids": exempted,
                "unknown_version_count": len(skipped_unknown),
                "affected_by_status": by_status,
                "affected_by_action": by_action,
            }
        self._safe_write_state_snapshot(root_id, "constitution.amended")
        return summary

    def _replace_constitution_snapshot(self, task: TaskNode, amendment: ConstitutionRecord) -> None:
        """把节点换到新版本的快照与版本戳上（三者必须一起换，否则是半旧半新）。"""
        task.constitution_id = amendment.id
        task.constitution_hash = amendment.content_hash
        task.constitution_json = amendment.data_json
        task.constitution_version = amendment.version
        task.updated_at = utcnow()

    def _stamp_constitution_version_on_completion(self, db: Session, task: TaskNode) -> None:
        """节点**完成**时，把版本戳对齐到它自己快照的版本。

        版本戳的语义是"该节点的产物是在这一版判据下通过的"。修订时未完成的节点已经换了新快照
        （§6 分状态裁决），它们完成时本来就该记新版本；而 `done` 节点在收紧方向上**刻意不给戳**
        （拿新版本号给旧产物盖章 = 静默 claim 通过）——那类节点只有真重新通过一次才会走到这里。

        未登记版本（`None`）的存量节点也在这里补齐：它的快照就来自这条 `constitution_id`，
        所以"它是在这一版下通过的"是可判定的事实，不是猜测。
        """
        if not task.constitution_id:
            return
        record = db.get(ConstitutionRecord, task.constitution_id)
        if record is None:
            return
        current = constitution_versions.normalize_version(task.constitution_version)
        if current is None or current < record.version:
            task.constitution_version = record.version
            task.updated_at = utcnow()

    def _enqueue_revalidate_after_run(self, db: Session, task: TaskNode) -> str | None:
        """执行中节点跑完后的入队（宪法 SPEC §6/§8）。

        修订发生时，执行中的节点既不能换快照（会产出半旧半新的产物），也不能什么都不做——
        它们的产物是按旧判据做的，做完必须有人复查。修订当场只登记（`revalidate_after_run`），
        这里在它**完成**时把登记兑现：

        - 收紧：把 `constitution_stale` + `revalidate_required` 债写到**它的父节点**上
          （与其它版本债同一处），于是既有的 `_revalidate_stale_leaves` 会在父节点验收前把它
          重跑一遍；通过即追平、清债。
        - 放宽/中性：产物仍合规，只把版本戳追平到登记的那一版。

        两种情况都清掉登记。返回用什么方式兑现的（`enqueued` / `stamped` / `None`）。
        """
        meta = loads(task.node_meta_json, {})
        registration = meta.get("revalidate_after_run")
        if not isinstance(registration, dict):
            return None
        version = constitution_versions.normalize_version(registration.get("version"))
        direction = str(registration.get("direction") or constitution_versions.DIRECTION_TIGHTEN)
        meta.pop("revalidate_after_run", None)
        task.node_meta_json = dumps(meta)
        if version is None:
            return None
        owner_id = task.parent_id or task.id
        owner = db.get(TaskNode, owner_id)
        if owner is None:
            return None
        owner_meta = loads(owner.node_meta_json, {})
        existing = owner_meta.get("obligations") if isinstance(owner_meta.get("obligations"), list) else []
        if not constitution_versions.forces_revalidation(direction):
            # 放宽：不再复查，但要把版本戳追平——否则它会以"落后"的身份一直挂在看板上
            task.constitution_version = max(
                constitution_versions.normalize_version(task.constitution_version) or 0, version
            )
            task.updated_at = utcnow()
            return self._settle_owner_version_debt(db, owner, owner_meta, existing, task, outcome="stamped")
        if not constitution_versions.is_stale(task, version):
            # 登记期间它自己追平了（例如中途重跑过一次）：把之前记下的落后债按事实清掉，
            # 否则门控会拿一条已经不再成立的事实继续挡人。
            return self._settle_owner_version_debt(db, owner, owner_meta, existing, task, outcome=None)
        incoming = constitution_versions.version_debt([task], effective_version=version, direction=direction)
        debt = incoming.get(owner_id) or []
        if not debt:
            return None
        owner_meta["obligations"] = obligations.upsert(existing, debt)
        owner.node_meta_json = dumps(owner_meta)
        self._add_event(
            db,
            task.id,
            "task.revalidate_enqueued",
            "执行中节点的产物是在修订前完成的，已入再验证队列。",
            {"version": version, "direction": direction, "owner_id": owner_id, "obligation_ids": [item["id"] for item in debt]},
        )
        return "enqueued"

    def _settle_owner_version_debt(
        self,
        db: Session,
        owner: TaskNode,
        owner_meta: dict[str, Any],
        existing: list[dict[str, Any]],
        node: TaskNode,
        *,
        outcome: str | None,
    ) -> str | None:
        """把拥有者上属于这个节点、且已不再成立的版本债按事实清掉。"""
        effective = self._constitution_head_version(db, node.constitution_id)
        settled = obligations.settle_version_caught_up(
            existing,
            versions={node.id: constitution_versions.normalize_version(node.constitution_version)},
            effective_version=effective,
        )
        if settled != existing:
            owner_meta["obligations"] = settled
            owner.node_meta_json = dumps(owner_meta)
        return outcome

    async def start_project(
        self,
        goal: str,
        target_path: str | None = None,
        runner_mode: str | None = None,
        constitution_id: str | None = None,
        auto_run: bool = True,
    ) -> str:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        goal = repair_mojibake_text(goal)
        target_path = repair_mojibake_text(target_path) if target_path else None
        root_id = new_id("root")
        with SessionLocal() as db:
            constitution = self._resolve_locked_constitution(db, constitution_id)
            constitution.root_id = root_id
            constitution_data = loads(constitution.data_json, {})
            root = TaskNode(
                id=root_id,
                title="根节点 / 递归 Codex 战场目标",
                description=goal,
                depth=0,
                status=TaskStatus.PLANNED.value,
                scope_json=dumps([target_path] if target_path else []),
                node_meta_json=dumps({"runner_mode": runner_mode or self.settings.runner_mode, "agent_burst_used": 0}),
                constitution_id=constitution.id,
                constitution_hash=constitution.content_hash,
                constitution_json=constitution.data_json,
                constitution_version=constitution.version,
                acceptance_json=dumps(
                    [
                        f"完成或继续拆解这个根目标：{goal}",
                        "任务树必须遵守已锁定项目宪法的品质下限和规模上限。",
                        "每个节点必须遵守 300% 可完成铁律。",
                        "叶子节点验收 fail/repair 一次必须转父节点重新拆分。",
                        "父节点必须通过 acceptance agent 的真实整合验收后才能完成。",
                        *constitution_data.get("global_acceptance", []),
                    ]
                ),
            )
            db.add(root)
            # B 部分：宪法若声明了验收面，就在根节点创建时（= 宪法已锁定）把它冻结下来。
            self._freeze_acceptance_surface(
                db, root, data=constitution_data, version=constitution.version, frozen_by="constitution_lock"
            )
            self._add_event(
                db,
                root.id,
                "project.started",
                "根目标已创建，并绑定锁定项目宪法。",
                {
                    "runner_mode": runner_mode or self.settings.runner_mode,
                    "constitution_id": constitution.id,
                    "constitution_hash": constitution.content_hash,
                },
            )
            db.commit()
        self._safe_write_state_snapshot(root_id, "project.started")
        self._agent_starts_by_root[root_id] = 0
        self._open_preview(root_id)
        if auto_run:
            if self._should_detach_tree_runner(root_id, runner_mode):
                self._spawn_detached_tree_runner(root_id, runner_mode)
            else:
                task = asyncio.create_task(self.run_tree(root_id, runner_mode))
                self._background_tasks.add(task)
                task.add_done_callback(lambda completed, rid=root_id: self._finish_background_task(completed, rid))
        return root_id

    async def start_one_shot_project(self, goal: str, runner_mode: str | None = None, auto_run: bool = True) -> str:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        goal = repair_mojibake_text(goal)
        target_path = self._new_one_shot_target_path(goal)
        target_path.mkdir(parents=True, exist_ok=True)
        constitution_id = await self.draft_constitution(goal, str(target_path), runner_mode)
        self.lock_constitution(constitution_id)
        return await self.start_project(
            goal=goal,
            target_path=str(target_path),
            runner_mode=runner_mode,
            constitution_id=constitution_id,
            auto_run=auto_run,
        )

    def _new_one_shot_target_path(self, goal: str) -> Path:
        hint = "project"
        lowered = goal.lower()
        if "godot" in lowered:
            hint = "godot"
        elif "game" in lowered or "游戏" in goal:
            hint = "game"
        digest = hashlib.sha1(goal.encode("utf-8")).hexdigest()[:8]
        return self.settings.workspace_dir / "projects" / f"{hint}-{digest}-{new_id('run')}"

    async def run_tree(self, root_id: str, runner_mode: str | None = None) -> None:
        if self.settings.frozen_mode:
            return
        runner = make_runner(self.settings, runner_mode)
        semaphore = self._agent_semaphore
        self.backfill_skill_note_children(root_id)
        self.recover_deferred_parent_resumes(root_id)
        self.recover_finished_agent_messages(root_id)
        self.recover_finished_acceptance_messages(root_id)
        self._auto_retry_fast_retryable_blocks(root_id)
        await self._adjust_agent_concurrency(root_id, semaphore)
        await self._process_node(root_id, runner, semaphore)
        await self._auto_hibernate_retryable_resource_blocks(root_id)

    def _finish_background_task(self, completed: asyncio.Task, root_id: str | None) -> None:
        """后台树运行的统一收尾：注销引用，并且**取走异常**。

        原先是 `add_done_callback(self._background_tasks.discard)`。`set.discard` 永不抛异常，
        所以它**从不取异常**——`run_tree` 里的意外错误会变成一条没人接的
        `Task exception was never retrieved`，只落在 uvicorn 的 stderr 里。树则停在崩溃那一刻的
        状态（例如 `planning`），既不是 `blocked` 也没有事件，**看门狗因此看不见它**。

        实测症状（2026-10-14）：`PATH` 上没有 `git` 时 `_is_git_repo` 抛 `FileNotFoundError`，
        4 个节点死在 `planning`、`active_agents` 仍计 4，`blocked=0`、事件为空，
        树靠 `/api/tasks/{root}/kick` 才恢复。

        这里做三件事：注销引用、**把异常落成事件与 `blocked` 状态**（让看门狗和看板都看得见）、
        然后写 stderr 保留原有可观测性。任务自身的异常处理仍归 `_process_node` 负责。
        """
        self._background_tasks.discard(completed)
        try:
            error = completed.exception()
        except asyncio.CancelledError:
            # 主动取消不是失败，不制造阻塞债务。
            return
        if error is None:
            return
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-4000:]
        sys.stderr.write(f"[orchestrator] tree runner crashed root={root_id}: {error!r}\n{tb}\n")
        try:
            with SessionLocal() as db:
                if root_id:
                    self._add_event(
                        db,
                        root_id,
                        "tree.runner_crashed",
                        f"树运行器异常退出（不是节点自身的阻塞）：{error!r}。任务树已停止推进，需要 kick。",
                        {"root_id": root_id, "error": repr(error), "traceback_tail": tb},
                    )
                    root = db.get(TaskNode, root_id)
                    if root is not None and root.status not in TERMINAL_STATUSES:
                        self._mark(db, root, TaskStatus.BLOCKED, f"树运行器异常退出：{error!r}")
                db.commit()
        except Exception as booking_error:  # 记账失败不能顶掉原始错误
            sys.stderr.write(f"[orchestrator] failed to record runner crash: {booking_error!r}\n")


    def _should_detach_tree_runner(self, root_id: str, runner_mode: str | None = None) -> bool:
        if not self.settings.api_detached_real_scheduler or not self.settings.goal_mode:
            return False
        effective_runner_mode = runner_mode or self._runner_mode_for_task(root_id)
        return effective_runner_mode == "real"

    def _repo_python_executable(self) -> str:
        """分离式 scheduler 用哪个解释器。

        长期自主运行靠的就是这条链路（goal 模式下把树 runner 甩到独立进程），所以**不能**在
        找不到 venv 时静默退回系统 `python`：那大概率没装 fastapi 等依赖，表现是"起了个进程、
        立刻失败"，而人不在场。两种常见布局都探一遍（`api/.venv` 与仓库根 `.venv`），
        都没有才退回 `python`（保留在系统级安装下仍能跑的可能）。
        """
        candidates = (
            ROOT_DIR / "api" / ".venv" / "Scripts" / "python.exe",
            ROOT_DIR / ".venv" / "Scripts" / "python.exe",
            ROOT_DIR / "api" / ".venv" / "bin" / "python",
            ROOT_DIR / ".venv" / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return "python"

    def _spawn_detached_tree_runner(self, root_id: str, runner_mode: str | None = None) -> dict[str, Any]:
        script_path = ROOT_DIR / "scripts" / "local_kick_tree.py"
        executable = self._repo_python_executable()
        effective_runner_mode = runner_mode or self._runner_mode_for_task(root_id)
        args = [
            executable,
            str(script_path),
            "--root-id",
            root_id,
            "--runner-mode",
            effective_runner_mode,
            "--max-concurrency",
            str(max(1, int(self.settings.max_concurrency))),
            "--resume-after-seconds",
            str(max(60, int(self.settings.resource_block_resume_after_seconds))),
            "--machine-pressure-resume-after-seconds",
            str(max(60, int(self.settings.machine_safety_resume_after_seconds))),
            "--safe-min-available-memory-mb",
            str(max(0, int(self.settings.machine_safety_min_available_memory_mb))),
        ]
        if not self.settings.auto_concurrency_enabled:
            args.append("--fixed-concurrency")
        safe_root_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", root_id)
        out_path = self.settings.workspace_dir / f"local-kick-{safe_root_id}-api.out.log"
        err_path = self.settings.workspace_dir / f"local-kick-{safe_root_id}-api.err.log"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        env = os.environ.copy()
        env["CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START"] = "0"
        try:
            with out_path.open("ab") as stdout, err_path.open("ab") as stderr:
                process = subprocess.Popen(
                    args,
                    cwd=str(ROOT_DIR),
                    stdout=stdout,
                    stderr=stderr,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    close_fds=True,
                    creationflags=creationflags,
                )
        except OSError as exc:
            with SessionLocal() as db:
                self._add_event(
                    db,
                    root_id,
                    "scheduler.detached_start_failed",
                    f"Detached scheduler launch failed; API will fall back to in-process scheduling: {exc}",
                    {"error": str(exc), "runner_mode": effective_runner_mode},
                )
                db.commit()
            return {"status": "queued", "root_id": root_id, "scheduler": "in_process_fallback", "error": str(exc)}
        with SessionLocal() as db:
            self._add_event(
                db,
                root_id,
                "scheduler.detached_started",
                "GoalMode real scheduler delegated to local_kick_tree supervisor so API remains a control plane.",
                {
                    "pid": process.pid,
                    "runner_mode": effective_runner_mode,
                    "stdout": str(out_path),
                    "stderr": str(err_path),
                },
            )
            db.commit()
        return {"status": "queued", "root_id": root_id, "scheduler": "detached", "pid": process.pid}

    async def kick_tree(self, root_id: str, runner_mode: str | None = None) -> dict[str, Any]:
        if self.settings.frozen_mode:
            return {"status": "frozen", "root_id": root_id}
        with SessionLocal() as db:
            task = self._get_task(db, root_id)
            if task.status in TERMINAL_STATUSES:
                return {"status": "terminal", "root_id": root_id}
            if task.status in STOPPED_STATUSES:
                return {"status": "stopped", "root_id": root_id}
            self._add_event(
                db,
                root_id,
                "scheduler.kick",
                "调度器已被手动接力；继续推进现有子树，不重建根节点或覆盖已有结果。",
                {},
            )
            # R3：顺手清扫终态 worktree（判据是保守的：终态 + 证据已归档 + 保留期已过 + 在受管根下）
            self._recycle_finished_worktrees(db, root_id)
            db.commit()
        effective_runner_mode = runner_mode or self._runner_mode_for_task(root_id)
        if self._should_detach_tree_runner(root_id, effective_runner_mode):
            return self._spawn_detached_tree_runner(root_id, effective_runner_mode)
        background_task = asyncio.create_task(self.run_tree(root_id, effective_runner_mode))
        self._background_tasks.add(background_task)
        background_task.add_done_callback(lambda completed, rid=root_id: self._finish_background_task(completed, rid))
        return {"status": "queued", "root_id": root_id}

    async def retry_task(self, task_id: str) -> None:
        if self.settings.frozen_mode:
            return
        retryable_child_ids: list[str] = []
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            retryable_child_ids = [child.id for child in self._retryable_blocked_children(task)]
            if retryable_child_ids:
                for child in task.children:
                    if child.id not in retryable_child_ids:
                        continue
                    child.status = TaskStatus.QUEUED.value
                    child.error_message = None
                    child.updated_at = utcnow()
                    meta = loads(child.node_meta_json, {})
                    meta["last_parent_retry_at"] = utcnow().isoformat()
                    meta["last_parent_retry_by"] = task.id
                    child.node_meta_json = dumps(meta)
                    self._add_event(db, child.id, "task.retry", "父节点已根据阻塞上报重新启动该子节点。", {"parent_id": task.id})
                task.status = TaskStatus.WAITING_CHILDREN.value
                task.error_message = None
                task.updated_at = utcnow()
                self._add_event(
                    db,
                    task.id,
                    "parent.retry_blocked_children",
                    "父节点已选择等待后重试资源类阻塞子节点。",
                    {"child_ids": retryable_child_ids},
                )
                db.commit()
                background_task = asyncio.create_task(self._retry_children_then_accept(task.id, retryable_child_ids))
                self._background_tasks.add(background_task)
                background_task.add_done_callback(lambda completed, rid=task_id: self._finish_background_task(completed, rid))
                return
            task.status = TaskStatus.QUEUED.value
            task.error_message = None
            task.updated_at = utcnow()
            self._add_event(db, task_id, "task.retry", "任务已重新排队等待重试。", {})
            db.commit()
        background_task = asyncio.create_task(self.run_tree(task_id, self._runner_mode_for_task(task_id)))
        self._background_tasks.add(background_task)
        background_task.add_done_callback(lambda completed, rid=task_id: self._finish_background_task(completed, rid))

    async def _retry_children_then_accept(self, parent_id: str, child_ids: list[str]) -> None:
        runner = make_runner(self.settings, self._runner_mode_for_task(parent_id))
        semaphore = self._agent_semaphore
        await self._process_children_with_dependencies(parent_id, child_ids, runner, semaphore)
        if not self._is_task_stopped(parent_id):
            await self._accept_parent(parent_id, runner, semaphore)

    def set_task_status(self, task_id: str, status: TaskStatus, message: str) -> None:
        if self.settings.frozen_mode:
            return
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            task.status = status.value
            task.updated_at = utcnow()
            self._add_event(db, task_id, f"task.{status.value}", message, {})
            db.commit()

    def cancel_tree(self, root_id: str | None = None) -> int:
        if self.settings.frozen_mode:
            return 0
        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
            if root_id is None:
                tasks = all_tasks
            else:
                tasks = self._subtree(all_tasks, root_id)
            canceled = 0
            for task in tasks:
                if task.status in TERMINAL_STATUSES:
                    continue
                task.status = TaskStatus.CANCELED.value
                task.error_message = None
                task.updated_at = utcnow()
                canceled += 1
                self._add_event(db, task.id, "task.canceled", "用户已停止该任务。", {"status": TaskStatus.CANCELED.value})
            db.commit()
            return canceled

    async def cancel_all(self, root_id: str | None = None) -> int:
        canceled = self.cancel_tree(root_id)
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return canceled

    async def hibernate_tree(self, root_id: str, resume_after_seconds: int, reason: str) -> dict[str, Any]:
        if self.settings.frozen_mode:
            return {"status": "frozen", "count": 0, "resume_at": ""}
        resume_after_seconds = max(60, resume_after_seconds)
        resume_at = utcnow() + timedelta(seconds=resume_after_seconds)
        with SessionLocal() as db:
            root = self._get_task(db, root_id)
            reason = self._normalize_hibernation_reason(db, root.id, reason)
            tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root.id)
            hibernated = 0
            for task in tasks:
                if not self._can_hibernate_task(task):
                    continue
                meta = loads(task.node_meta_json, {})
                meta["hibernation"] = {
                    "owner_id": root.id,
                    "previous_status": task.status,
                    "resume_at": resume_at.isoformat(),
                    "reason": reason,
                    "status": "scheduled",
                    "scheduled_at": utcnow().isoformat(),
                }
                task.node_meta_json = dumps(meta)
                self._mark(
                    db,
                    task,
                    TaskStatus.HIBERNATING,
                    f"任务树已保存状态并进入休眠，计划在 {resume_at.isoformat()} 自动恢复。原因：{reason}",
                )
                hibernated += 1
            self._add_event(
                db,
                root.id,
                "tree.hibernate",
                f"任务树已保存状态并休眠；{resume_after_seconds} 秒后自动恢复。",
                {"resume_at": resume_at.isoformat(), "reason": reason, "hibernated_count": hibernated},
            )
            db.commit()

        self._safe_write_state_snapshot(root_id, "tree.hibernate")
        self._schedule_auto_resume(root_id, resume_at)
        current_task = asyncio.current_task()
        tasks_to_cancel = [task for task in self._background_tasks if task is not current_task]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        return {"status": "hibernating", "count": hibernated, "resume_at": resume_at.isoformat()}

    async def resume_hibernated_tree(self, root_id: str) -> dict[str, Any]:
        if self.settings.frozen_mode:
            return {"status": "frozen", "count": 0}
        guard = self._active_budget_guard(root_id)
        if guard:
            resume_at = guard["resume_at"]
            self._schedule_auto_resume(root_id, resume_at)
            return {
                "status": "budget_guard_sleep",
                "count": 0,
                "resume_at": resume_at.isoformat(),
                "reason": str(guard.get("reason") or ""),
            }
        with SessionLocal() as db:
            root = self._get_task(db, root_id)
            tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root.id)
            resumed = 0
            for task in tasks:
                if task.status != TaskStatus.HIBERNATING.value:
                    continue
                meta = loads(task.node_meta_json, {})
                hibernation = meta.get("hibernation") if isinstance(meta.get("hibernation"), dict) else {}
                if hibernation.get("owner_id") != root.id:
                    continue
                hibernation["status"] = "resumed"
                hibernation["resumed_at"] = utcnow().isoformat()
                meta["hibernation"] = hibernation
                task.node_meta_json = dumps(meta)
                task.status = self._resume_status_from_hibernation(hibernation)
                task.error_message = None
                task.updated_at = utcnow()
                self._add_event(
                    db,
                    task.id,
                    "task.hibernate_resume",
                    "休眠时间已到，任务已恢复为可继续调度状态。",
                    {"owner_id": root.id, "status": task.status},
                )
                resumed += 1
            self._add_event(
                db,
                root.id,
                "tree.hibernate_resume",
                "任务树休眠结束，已自动恢复递归执行。",
                {"resumed_count": resumed},
            )
            if resumed:
                self._set_agent_burst_used_for_root(db, root.id, 0)
            db.commit()
        self._safe_write_state_snapshot(root_id, "tree.hibernate_resume")
        current_task = asyncio.current_task()
        existing = self._auto_resume_tasks.pop(root_id, None)
        if existing and existing is not current_task and not existing.done():
            existing.cancel()
        self._agent_starts_by_root[root_id] = 0
        if resumed:
            runner_mode = self._runner_mode_for_task(root_id)
            if self._should_detach_tree_runner(root_id, runner_mode):
                self._spawn_detached_tree_runner(root_id, runner_mode)
            else:
                background_task = asyncio.create_task(self.run_tree(root_id, runner_mode))
                self._background_tasks.add(background_task)
                background_task.add_done_callback(lambda completed, rid=root_id: self._finish_background_task(completed, rid))
        return {"status": "queued" if resumed else "noop", "count": resumed}

    def _resume_status_from_hibernation(self, hibernation: dict[str, Any]) -> str:
        return watchdog.resume_status_from_hibernation(hibernation)

    def schedule_hibernation_wakeups(self) -> int:
        if self.settings.frozen_mode:
            return 0
        scheduled = 0
        with SessionLocal() as db:
            tasks = list(db.scalars(select(TaskNode).where(TaskNode.status == TaskStatus.HIBERNATING.value)).all())
        owners: dict[str, datetime] = {}
        for task in tasks:
            meta = loads(task.node_meta_json, {})
            hibernation = meta.get("hibernation") if isinstance(meta.get("hibernation"), dict) else {}
            owner_id = str(hibernation.get("owner_id") or "")
            resume_at_raw = str(hibernation.get("resume_at") or "")
            if not owner_id or owner_id != task.id or not resume_at_raw:
                continue
            try:
                resume_at = datetime.fromisoformat(resume_at_raw)
            except ValueError:
                resume_at = utcnow()
            owners[owner_id] = resume_at
        for owner_id, resume_at in owners.items():
            self._schedule_auto_resume(owner_id, resume_at)
            scheduled += 1
        return scheduled

    def _schedule_auto_resume(self, root_id: str, resume_at: datetime) -> None:
        resume_at = self._coerce_utc(resume_at) or utcnow()
        current_task = asyncio.current_task()
        existing = self._auto_resume_tasks.pop(root_id, None)
        if existing and existing is not current_task and not existing.done():
            existing.cancel()

        async def wakeup() -> None:
            try:
                delay = max(0.0, (resume_at - utcnow()).total_seconds())
                if delay:
                    await asyncio.sleep(delay)
                while True:
                    guard = self._active_budget_guard(root_id)
                    if not guard:
                        break
                    guard_resume_at = self._coerce_utc(guard["resume_at"]) or utcnow()
                    guard_delay = max(0.0, (guard_resume_at - utcnow()).total_seconds())
                    if guard_delay <= 0:
                        break
                    await asyncio.sleep(guard_delay)
                await self.resume_hibernated_tree(root_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_auto_resume_failure(root_id, exc)

        task = asyncio.create_task(wakeup())
        self._auto_resume_tasks[root_id] = task
        task.add_done_callback(lambda completed: self._auto_resume_tasks.pop(root_id, None) if self._auto_resume_tasks.get(root_id) is completed else None)

    def _can_hibernate_task(self, task: TaskNode) -> bool:
        return blockers.can_hibernate_task(task, self.settings)

    def _record_auto_resume_failure(self, root_id: str, exc: Exception) -> None:
        with SessionLocal() as db:
            task = db.get(TaskNode, root_id)
            payload = {"error": str(exc)}
            self._add_event(
                db,
                root_id if task is not None else None,
                "tree.hibernate_resume_failed",
                f"自动恢复任务树失败：{exc}",
                payload,
            )
            db.commit()

    async def _auto_hibernate_retryable_resource_blocks(self, root_id: str) -> None:
        if not self.settings.auto_hibernate_on_resource_block:
            return
        with SessionLocal() as db:
            root = db.get(TaskNode, root_id)
            if root is None or root.status == TaskStatus.HIBERNATING.value:
                return
            tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root.id)
            if any(task.status in ACTIVE_AGENT_STATUSES for task in tasks):
                return
            retryable_reports = [
                self._child_block_report(task)
                for task in tasks
                if task.status == TaskStatus.BLOCKED.value
                and self._is_resource_pressure_block_report(self._child_block_report(task))
            ]
        if not retryable_reports:
            return
        reason = self._resource_auto_hibernate_reason(retryable_reports)
        resume_after_seconds = self._retry_after_seconds_from_reports(retryable_reports)
        await self.hibernate_tree(root_id, resume_after_seconds, reason)

    def _retry_after_seconds_from_reports(self, reports: list[dict[str, Any]]) -> int:
        return watchdog.retry_after_seconds_from_reports(reports, self.settings.resource_block_resume_after_seconds)

    def _is_task_stopped(self, task_id: str) -> bool:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            return task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES

    def recover_stale_agent_statuses(self) -> int:
        if self.settings.frozen_mode:
            return 0
        external_status_by_task_id = self._live_external_agent_statuses_by_task_id(max_cache_age_seconds=0)
        recovered_external = self._sync_live_external_agent_statuses(external_status_by_task_id=external_status_by_task_id)
        stale_statuses = {
            TaskStatus.PLANNING.value,
            TaskStatus.DECOMPOSING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.WORKING.value,
            TaskStatus.VERIFYING.value,
            TaskStatus.ACCEPTING.value,
        }
        with SessionLocal() as db:
            tasks = list(
                db.scalars(
                    select(TaskNode).where(
                        TaskNode.status.in_(stale_statuses),
                        ~TaskNode.id.in_(external_status_by_task_id),
                    )
                ).all()
            )
            for task in tasks:
                self._mark(
                    db,
                    task,
                    TaskStatus.QUEUED,
                    "服务重启后，旧的运行态已恢复为排队态；请重试或继续该节点。",
                )
            db.commit()
        return len(tasks) + recovered_external

    def _live_external_agent_statuses_by_task_id(self, max_cache_age_seconds: float = 2.0) -> dict[str, str]:
        return {
            str(process.get("task_id") or ""): str(process.get("status") or "")
            for process in self._external_codex_processes(max_cache_age_seconds=max_cache_age_seconds)
            if process.get("task_id") and process.get("status")
        }

    def refresh_external_codex_process_cache(self) -> int:
        """**主动**刷新"外部 codex 进程"缓存，返回这次扫到几条。

        为什么需要它（2026-10-17 实测，干净的对照）：服务端**彻底安静**之后（页面加载完 6.5 秒、
        侧栏那批请求早已结束）第一次 `/api/tree` 仍然要 **1196ms**，紧接着两次只要 233/241ms ——
        这正是 `_external_codex_processes` 的缓存（TTL 2 秒）过期后、下一次调用**同步**去跑
        PowerShell/WMI 扫描的签名（cProfile 里那段 `TextIOWrapper.read` 是 0.672–0.895 秒）。

        所以由 API 进程的后台任务每 1.5 秒调一次这里，让请求永远命中热缓存。
        非 Windows 上 `_external_codex_processes` 直接返回空表（不缓存），因此这是零代价的。
        """

        return len(self._external_codex_processes(max_cache_age_seconds=0))

    def _sync_live_external_agent_statuses(
        self,
        *,
        db: Session | None = None,
        external_status_by_task_id: dict[str, str] | None = None,
    ) -> int:
        if self.settings.frozen_mode:
            return 0
        status_by_task_id = external_status_by_task_id if external_status_by_task_id is not None else self._live_external_agent_statuses_by_task_id()
        if not status_by_task_id:
            return 0

        def sync(session: Session) -> int:
            recovered = 0
            tasks = list(session.scalars(select(TaskNode).where(TaskNode.id.in_(status_by_task_id))).all())
            for task in tasks:
                if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                    continue
                status_value = status_by_task_id.get(task.id)
                if not status_value or task.status == status_value:
                    continue
                try:
                    status = TaskStatus(status_value)
                except ValueError:
                    continue
                self._mark(
                    session,
                    task,
                    status,
                    "检测到该节点仍有真实 Codex CLI 进程运行；同步为实际 agent 状态，而不是停留在排队展示。",
                )
                recovered += 1
            if recovered:
                session.commit()
            return recovered

        if db is not None:
            return sync(db)
        with SessionLocal() as session:
            return sync(session)

    def cleanup_stalled_external_codex_agents(
        self,
        root_id: str | None = None,
        *,
        max_kills: int = 13,
    ) -> dict[str, Any]:
        if self.settings.frozen_mode:
            return {"status": "frozen", "root_id": root_id, "stalled": 0, "requeued": 0, "stopped": []}

        recovered_agent_messages = self.recover_finished_agent_messages(root_id)
        recovered_acceptance_messages = self.recover_finished_acceptance_messages(root_id)
        stopped: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
            selected_root_id, selection = self._select_watchdog_root(all_tasks, root_id)
            tasks = self._subtree(all_tasks, selected_root_id) if selected_root_id else []
            diagnostics = self._active_agent_diagnostics(db, tasks)
            diagnostics_by_task_id = {
                str(item.get("task_id") or ""): item
                for item in diagnostics
                if item.get("task_id")
            }
            stalled_by_task_id = {
                str(item.get("task_id") or ""): item
                for item in diagnostics
                if item.get("severity") == "stalled" and item.get("task_id")
            }

            processes_by_task_id: dict[str, list[dict[str, Any]]] = {}
            for process in self._external_codex_processes(max_cache_age_seconds=0):
                task_id = str(process.get("task_id") or "")
                if task_id in diagnostics_by_task_id:
                    processes_by_task_id.setdefault(task_id, []).append(process)

            cleanup_candidates: dict[str, tuple[dict[str, Any], str, bool]] = {}
            for task_id, diagnostic in stalled_by_task_id.items():
                processes = processes_by_task_id.get(task_id, [])
                cleanup_candidates[task_id] = (
                    diagnostic,
                    "external_process_stalled" if processes else "stalled_active_status_without_live_process",
                    False,
                )
            for task_id, diagnostic in diagnostics_by_task_id.items():
                if task_id in cleanup_candidates:
                    continue
                if diagnostic.get("latest_run_status") == RunStatus.FAILED.value and not processes_by_task_id.get(task_id):
                    cleanup_candidates[task_id] = (diagnostic, "failed_run_without_live_process", True)
                    continue
                no_progress_seconds = int(diagnostic.get("no_progress_seconds") or 0)
                if not processes_by_task_id.get(task_id):
                    if no_progress_seconds >= PENDING_CODEX_SLOT_RESERVATION_SECONDS:
                        cleanup_candidates[task_id] = (
                            diagnostic,
                            "active_status_without_live_process_after_startup_grace",
                            True,
                        )
                    continue
                if not diagnostic.get("latest_progress_at") and no_progress_seconds >= CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS:
                    cleanup_candidates[task_id] = (
                        diagnostic,
                        "live_process_without_current_output_after_startup_grace",
                        True,
                    )

            if not cleanup_candidates:
                return {
                    "status": "ok",
                    "root_id": selected_root_id,
                    "selection": selection,
                    "stalled": len(stalled_by_task_id),
                    "requeued": 0,
                    "stopped": [],
                    "skipped": [],
                    "recovered_agent_messages": recovered_agent_messages,
                    "recovered_acceptance_messages": recovered_acceptance_messages,
                }

            for task_id, (diagnostic, cleanup_reason, bypass_idle_threshold) in cleanup_candidates.items():
                if len(stopped) >= max(0, int(max_kills)):
                    skipped.append({"task_id": task_id, "reason": "max_kills_reached"})
                    continue
                task = db.get(TaskNode, task_id)
                if task is None or task.status not in ACTIVE_AGENT_STATUSES:
                    skipped.append({"task_id": task_id, "reason": "task_not_active"})
                    continue
                phase = str(diagnostic.get("phase") or self._agent_phase_for_status(task.status))
                last_message_path = self.settings.workspace_dir / "runs" / task.id / f"{phase}-last-message.json"
                if self._last_message_has_recoverable_payload(last_message_path):
                    skipped.append({"task_id": task_id, "reason": "last_message_exists"})
                    continue
                no_progress_seconds = int(diagnostic.get("no_progress_seconds") or 0)
                idle_timeout_seconds = int(diagnostic.get("idle_timeout_seconds") or 0)
                if not bypass_idle_threshold and (idle_timeout_seconds <= 0 or no_progress_seconds < idle_timeout_seconds):
                    skipped.append({"task_id": task_id, "reason": "below_idle_threshold"})
                    continue

                processes = processes_by_task_id.get(task_id, [])
                kill_results: list[dict[str, Any]] = []
                for process in processes:
                    pid = process.get("pid")
                    try:
                        pid_int = int(pid)
                    except (TypeError, ValueError):
                        continue
                    kill_results.append(self._stop_external_codex_process_tree(pid_int))
                if processes and not any(result.get("stopped") for result in kill_results):
                    skipped.append({"task_id": task_id, "reason": "process_stop_failed", "kill_results": kill_results})
                    continue

                meta = loads(task.node_meta_json, {})
                meta["external_stall_cleanup"] = {
                    "phase": phase,
                    "pids": [result.get("pid") for result in kill_results],
                    "reason": cleanup_reason,
                    "no_progress_seconds": no_progress_seconds,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "requeued_at": utcnow().isoformat(),
                }
                task.node_meta_json = dumps(meta)
                self._mark(
                    db,
                    task,
                    TaskStatus.QUEUED,
                    "External Codex CLI phase has no recoverable last-message and no usable live process; the watchdog requeued this phase.",
                )
                self._reserve_pending_codex_slot(task.id)
                event_payload = {
                    "root_id": selected_root_id,
                    "phase": phase,
                    "kill_results": kill_results,
                    "cleanup_reason": cleanup_reason,
                    "no_progress_seconds": no_progress_seconds,
                    "idle_timeout_seconds": idle_timeout_seconds,
                    "pending_slot_grace_seconds": PENDING_CODEX_SLOT_RESERVATION_SECONDS,
                    "startup_no_output_grace_seconds": CODEX_STARTUP_NO_OUTPUT_GRACE_SECONDS,
                }
                self._add_event(
                    db,
                    task.id,
                    "watchdog.external_stall_requeued",
                    "Requeued a stalled Codex task after stopping its external process or detecting that no live process remained.",
                    event_payload,
                )
                stopped.append(
                    {
                        "task_id": task.id,
                        "phase": phase,
                        "kill_results": kill_results,
                        "reason": cleanup_reason,
                    }
                )

            if stopped:
                db.commit()
                self._external_codex_process_cache = None
            else:
                db.rollback()

        return {
            "status": "cleaned" if stopped else "ok",
            "root_id": selected_root_id,
            "selection": selection,
            "stalled": len(stalled_by_task_id),
            "requeued": len(stopped),
            "stopped": stopped,
            "skipped": skipped,
            "recovered_agent_messages": recovered_agent_messages,
            "recovered_acceptance_messages": recovered_acceptance_messages,
        }

    def _last_message_has_recoverable_payload(self, path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            raw = path.read_bytes()
        except OSError:
            return True
        if not raw.strip():
            return False
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict)

    def _stop_external_codex_process_tree(self, pid: int) -> dict[str, Any]:
        if pid <= 0:
            return {"pid": pid, "stopped": False, "exit_code": None, "stdout": "invalid pid"}
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    encoding=locale.getpreferredencoding(False),
                    errors="replace",
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"pid": pid, "stopped": False, "exit_code": None, "stdout": "", "stderr": str(exc)}
            output = f"{completed.stdout}\n{completed.stderr}".strip()
            normalized = output.lower()
            stopped = completed.returncode == 0 or "not found" in normalized or "not running" in normalized
            return {
                "pid": pid,
                "stopped": stopped,
                "exit_code": completed.returncode,
                "stdout": output[-2000:],
            }
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            return {"pid": pid, "stopped": True, "exit_code": 0, "stdout": "process already exited"}
        except OSError as exc:
            return {"pid": pid, "stopped": False, "exit_code": None, "stdout": "", "stderr": str(exc)}
        return {"pid": pid, "stopped": True, "exit_code": 0, "stdout": "sent SIGTERM"}

    def recover_finished_acceptance_messages(self, root_id: str | None = None) -> int:
        if self.settings.frozen_mode:
            return 0
        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
            selected_root_id = root_id or self._latest_root_id(all_tasks)
            tasks = self._subtree(all_tasks, selected_root_id) if selected_root_id else all_tasks
            recovered = self._recover_finished_acceptance_messages(db, tasks)
            if recovered:
                db.commit()
            return recovered

    def recover_finished_agent_messages(self, root_id: str | None = None) -> int:
        if self.settings.frozen_mode:
            return 0
        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
            selected_root_id = root_id or self._latest_root_id(all_tasks)
            tasks = self._subtree(all_tasks, selected_root_id) if selected_root_id else all_tasks
            recovered = self._recover_finished_agent_messages(db, tasks)
            if recovered:
                db.commit()
            return recovered

    def _recover_finished_agent_messages(
        self,
        db: Session,
        tasks: list[TaskNode],
        min_age_seconds: int = 120,
    ) -> int:
        return recovery.recover_finished_agent_messages(self, db, tasks, min_age_seconds)

    def _recover_finished_planner_message(self, db: Session, task: TaskNode, min_age_seconds: int) -> int:
        return recovery.recover_finished_planner_message(self, db, task, min_age_seconds)

    def _recover_finished_worker_message(self, db: Session, task: TaskNode, min_age_seconds: int) -> int:
        return recovery.recover_finished_worker_message(self, db, task, min_age_seconds)

    def _recover_finished_verifier_message(self, db: Session, task: TaskNode, min_age_seconds: int) -> int:
        return recovery.recover_finished_verifier_message(self, db, task, min_age_seconds)

    def _read_recoverable_last_message(
        self,
        task_id: str,
        kind: RunKind,
        min_age_seconds: int,
    ) -> tuple[Path, bytes, dict[str, Any]] | None:
        run_dir = self.settings.workspace_dir / "runs" / task_id
        last_message_path = run_dir / f"{kind.value}-last-message.json"
        if not last_message_path.is_file():
            return None
        last_message_at = self._file_mtime_utc(last_message_path)
        if last_message_at is None or self._seconds_since(last_message_at) < max(0, min_age_seconds):
            return None
        try:
            raw = last_message_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return last_message_path, raw, payload

    def _existing_recovered_run(self, db: Session, task: TaskNode, kind: RunKind, last_message_path: Path) -> RunRecord | None:
        return db.scalar(
            select(RunRecord).where(
                RunRecord.task_id == task.id,
                RunRecord.kind == kind.value,
                RunRecord.last_message_path == str(last_message_path),
            )
        )

    def _recovered_envelope(self, task: TaskNode, kind: RunKind, payload: dict[str, Any], last_message_path: Path) -> RunnerEnvelope:
        run_dir = self.settings.workspace_dir / "runs" / task.id
        logs_path = run_dir / f"{kind.value}.jsonl"
        prompt_path = run_dir / f"{kind.value}-prompt.txt"
        logs_path_text = str(logs_path) if logs_path.is_file() else None
        return RunnerEnvelope(
            command=f"recovered completed {kind.value} last-message",
            exit_code=0,
            logs_path=logs_path_text,
            payload=payload,
            prompt_path=str(prompt_path) if prompt_path.is_file() else None,
            last_message_path=str(last_message_path),
            workdir=task.worktree_path,
            usage=usage_from_codex_logs(logs_path_text),
        )

    def _worker_result_for_recovery(self, db: Session, task: TaskNode) -> WorkerResult | None:
        pending = self._pending_worker_result(task)
        if pending is not None:
            return pending
        latest_worker_run = db.scalars(
            select(RunRecord)
            .where(RunRecord.task_id == task.id, RunRecord.kind == RunKind.WORKER.value, RunRecord.status == RunStatus.SUCCEEDED.value)
            .order_by(RunRecord.id.desc())
        ).first()
        if latest_worker_run is None:
            return None
        payload = repair_mojibake(loads(latest_worker_run.result_json, {}))
        if not isinstance(payload, dict):
            return None
        try:
            worker_result = WorkerResult.model_validate(payload)
        except Exception:
            return None
        return worker_result if worker_result.status == "succeeded" else None

    def _recover_finished_acceptance_messages(
        self,
        db: Session,
        tasks: list[TaskNode],
        min_age_seconds: int = 120,
    ) -> int:
        return recovery.recover_finished_acceptance_messages(self, db, tasks, min_age_seconds)

    def _settle_recovered_acceptance_state(
        self,
        db: Session,
        task: TaskNode,
        acceptance_result: AcceptanceResult,
        last_message_path: Path,
    ) -> bool:
        children = [
            child
            for child in db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all()
            if child.status not in STOPPED_STATUSES
        ]
        child_ids: list[str] = []
        if acceptance_result.verdict == "repair" and acceptance_result.suggested_children:
            existing_titles = {self._normalize_signature_text(child.title) for child in children}
            missing_specs = [
                spec
                for spec in acceptance_result.suggested_children
                if self._normalize_signature_text(spec.title) not in existing_titles
            ]
            if missing_specs:
                child_ids = self._create_children(db, task, missing_specs)
                children = [
                    child
                    for child in db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all()
                    if child.status not in STOPPED_STATUSES
                ]

        unfinished = [child for child in children if child.status != TaskStatus.DONE.value]
        if acceptance_result.verdict == "repair" and (child_ids or unfinished):
            self._add_event(
                db,
                task.id,
                "acceptance.recovered_state_settled",
                "Recovered acceptance result was already recorded; parent state is settled back to waiting for repair children.",
                {
                    "child_ids": child_ids,
                    "unfinished_child_ids": [child.id for child in unfinished],
                    "last_message_path": str(last_message_path),
                    "verdict": acceptance_result.verdict,
                },
            )
            self._mark(db, task, TaskStatus.WAITING_CHILDREN, "Recovered acceptance repair is waiting for child tasks.")
            return True

        self._add_event(
            db,
            task.id,
            "acceptance.recovered_state_settled",
            "Recovered acceptance result was already recorded; parent is requeued so normal acceptance handling can continue.",
            {"last_message_path": str(last_message_path), "verdict": acceptance_result.verdict},
        )
        self._mark(db, task, TaskStatus.QUEUED, "Recovered acceptance result already exists; requeued for normal parent acceptance.")
        return True

    async def _process_node(self, task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        lock = self._node_locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            with SessionLocal() as db:
                task = self._get_task(db, task_id)
                self._add_event(
                    db,
                    task.id,
                    "task.concurrent_skip",
                    "该节点已有一个运行中的动作，本次重复启动已跳过。",
                    {"status": task.status},
                )
                db.commit()
            return
        async with lock:
            try:
                await self._process_node_unlocked(task_id, runner, semaphore)
            finally:
                self._release_pending_codex_slot(task_id)

    def _try_mark_for_agent(self, task_id: str, status: TaskStatus, message: str) -> bool:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return False
            self._mark(db, task, status, message)
            db.commit()
        return True

    def _try_mark_for_revalidation(self, task_id: str) -> bool:
        """把**已完成**的节点重新推进验证态。返回 `False` 表示没抢到（状态已被别人改过）。

        与 `_try_mark_for_agent` 只差一条：允许从终态 `done` 出来。再验证的前提恰恰是"它已经是
        按旧版判据通过的产物"，用普通入口会被终态检查挡住，于是再验证一步都走不出去。
        """
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in STOPPED_STATUSES or task.status != TaskStatus.DONE.value:
                return False
            self._mark(db, task, TaskStatus.VERIFYING, "宪法修订后重新验证该节点产物。")
            db.commit()
        return True

    def _codex_process_rows(self) -> list[dict[str, Any]] | None:
        """这次扫描的**原始行**：`[{"ProcessId":…, "CommandLine":…}, …]`；失败返回 `None`。

        用 **WQL 服务端过滤**（`-Query … WHERE Name='codex.exe'`）而不是"取回全部进程再在
        PowerShell 里 `Where-Object`"：实测（2026-10-17）cProfile 里那次全表扫描占掉第一次
        `get_tree` 的 **0.895 秒**，换成服务端过滤后降到 **0.672 秒**（同一棵树本身的成本是
        150–250ms）。语义完全一样：仍然拿 `CommandLine` 做 `@openai\\codex|codex-win32` 过滤。

        ⚠️ 更彻底的做法是换成 `psutil.process_iter(["pid","name","cmdline"])`（几十毫秒，
        而且非 Windows 也能用），但**本机装不上 psutil**（`pip install psutil` 报
        "No matching distribution"，这个环境的索引里没有），所以**没法验证** ——
        按"不发布没验过的路径"的规矩，这一轮不引入它，只记在 `docs/scale-baseline.md` §5 ⑥。
        """

        if os.name != "nt":
            return []
        command = (
            "Get-CimInstance -Query \"SELECT ProcessId,CommandLine FROM Win32_Process WHERE Name='codex.exe'\" | "
            "Where-Object { $_.CommandLine -match '@openai\\\\codex|codex-win32' } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress -Depth 3"
        )
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        if not completed.stdout.strip():
            return []
        try:
            raw = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        if isinstance(raw, dict):
            raw = [raw]
        return [item for item in (raw if isinstance(raw, list) else []) if isinstance(item, dict)]

    def _external_codex_processes(self, max_cache_age_seconds: float = 2.0) -> list[dict[str, Any]]:
        now = utcnow()
        cached = self._external_codex_process_cache
        if cached is not None and (now - cached[0]).total_seconds() <= max_cache_age_seconds:
            return list(cached[1])
        workspace = str(Path(self.settings.workspace_dir)).replace("/", "\\").lower()
        rows = self._codex_process_rows()
        if rows is None:
            self._external_codex_process_scan_failed = True
            return []
        self._external_codex_process_scan_failed = False
        processes: list[dict[str, Any]] = []
        for item in rows:
            command_line = str(item.get("CommandLine") or "")
            normalized = command_line.replace("/", "\\").lower()
            if workspace and workspace not in normalized:
                continue
            if not self._is_battlefield_agent_codex_command_line(command_line):
                continue
            task_id = self._task_id_from_codex_command_line(command_line)
            status = self._agent_status_from_codex_command_line(command_line)
            processes.append(
                {
                    "pid": item.get("ProcessId"),
                    "task_id": task_id,
                    "status": status.value if status is not None else "",
                }
            )
        self._external_codex_process_cache = (now, processes)
        return list(processes)

    def _filter_nonterminal_external_codex_processes(self, processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not processes:
            return []
        try:
            with SessionLocal() as db:
                filtered: list[dict[str, Any]] = []
                for process in processes:
                    task_id = str(process.get("task_id") or "")
                    task = db.get(TaskNode, task_id) if task_id else None
                    if task is not None and (task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES):
                        continue
                    filtered.append(process)
                return filtered
        except Exception:
            return processes

    def _task_id_from_codex_command_line(self, command_line: str) -> str:
        return runtime_logs.task_id_from_codex_command_line(command_line)

    def _agent_status_from_codex_command_line(self, command_line: str) -> TaskStatus | None:
        return runtime_logs.agent_status_from_codex_command_line(command_line)

    def _is_battlefield_agent_codex_command_line(self, command_line: str) -> bool:
        return runtime_logs.is_battlefield_agent_codex_command_line(command_line)

    def _codex_slots_active(self, *, max_cache_age_seconds: float = 5.0) -> int:
        processes = self._external_codex_processes(max_cache_age_seconds=max_cache_age_seconds)
        process_task_ids = {str(item.get("task_id") or "") for item in processes if item.get("task_id")}
        pending_task_ids = self._pending_codex_slot_task_ids()
        return max(
            self._agent_semaphore.active_count,
            len(processes) + len(pending_task_ids - process_task_ids),
        )

    def _pending_codex_slot_task_ids(self) -> set[str]:
        now = utcnow()
        expired = [
            task_id
            for task_id, reserved_at in self._pending_codex_slot_reservations.items()
            if (now - reserved_at).total_seconds() > PENDING_CODEX_SLOT_RESERVATION_SECONDS
        ]
        for task_id in expired:
            self._pending_codex_slot_reservations.pop(task_id, None)
        return set(self._pending_codex_slot_reservations) | self._shared_pending_codex_slot_task_ids()

    def _reserve_pending_codex_slot(self, task_id: str) -> None:
        self._pending_codex_slot_task_ids()
        self._pending_codex_slot_reservations[task_id] = utcnow()
        self._mutate_shared_pending_codex_slot_reservations(lambda reservations: reservations.__setitem__(task_id, utcnow()))

    def _release_pending_codex_slot(self, task_id: str) -> None:
        self._pending_codex_slot_reservations.pop(task_id, None)
        self._mutate_shared_pending_codex_slot_reservations(lambda reservations: reservations.pop(task_id, None))

    def _shared_pending_codex_slot_task_ids(self) -> set[str]:
        try:
            return self._mutate_shared_pending_codex_slot_reservations(lambda reservations: set(reservations))
        except OSError:
            return set()

    def _mutate_shared_pending_codex_slot_reservations(self, mutator: Any) -> Any:
        lock_path = self._codex_slot_reservations_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                if os.name == "nt":
                    import msvcrt

                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                reservations = self._read_shared_pending_codex_slot_reservations_unlocked()
                reservations = self._prune_pending_codex_slot_reservations(reservations)
                result = mutator(reservations)
                self._write_shared_pending_codex_slot_reservations_unlocked(reservations)
                return result
            finally:
                try:
                    if os.name == "nt":
                        import msvcrt

                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

    def _read_shared_pending_codex_slot_reservations_unlocked(self) -> dict[str, datetime]:
        path = self._codex_slot_reservations_path()
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw_reservations = payload.get("reservations") if isinstance(payload, dict) else None
        if not isinstance(raw_reservations, dict):
            return {}
        reservations: dict[str, datetime] = {}
        for task_id, reserved_at_raw in raw_reservations.items():
            try:
                reserved_at = datetime.fromisoformat(str(reserved_at_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            reserved_at = self._coerce_utc(reserved_at)
            if reserved_at is None:
                continue
            reservations[str(task_id)] = reserved_at
        return reservations

    def _write_shared_pending_codex_slot_reservations_unlocked(self, reservations: dict[str, datetime]) -> None:
        path = self._codex_slot_reservations_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utcnow().isoformat(),
            "ttl_seconds": PENDING_CODEX_SLOT_RESERVATION_SECONDS,
            "reservations": {task_id: reserved_at.isoformat() for task_id, reserved_at in sorted(reservations.items())},
        }
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, path)

    def _prune_pending_codex_slot_reservations(self, reservations: dict[str, datetime]) -> dict[str, datetime]:
        now = utcnow()
        return {
            task_id: reserved_at
            for task_id, reserved_at in reservations.items()
            if (now - reserved_at).total_seconds() <= PENDING_CODEX_SLOT_RESERVATION_SECONDS
        }

    def _hold_agent_start_for_external_codex_slots(self, task_id: str, phase: str) -> bool:
        capacity = max(1, self._agent_semaphore.capacity)
        processes = self._external_codex_processes(max_cache_age_seconds=0.5)
        process_task_ids = sorted({str(item.get("task_id") or "") for item in processes if item.get("task_id")})
        pending_task_ids = sorted(self._pending_codex_slot_task_ids())
        effective_slots = len(processes) + len(set(pending_task_ids) - set(process_task_ids))
        same_task_is_running = task_id in process_task_ids or task_id in pending_task_ids
        if effective_slots < capacity and not same_task_is_running:
            return False
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return True
            root_id = self._root_id_for_task(db, task)
            root = db.get(TaskNode, root_id)
            root_meta = loads(root.node_meta_json, {}) if root is not None else {}
            runner_mode = root_meta.get("runner_mode") if isinstance(root_meta, dict) else None
            effective_runner_mode = str(runner_mode) if runner_mode in {"mock", "real"} else self.settings.runner_mode
            if effective_runner_mode != "real":
                return False
            payload = {
                "task_id": task_id,
                "root_id": root_id,
                "phase": phase,
                "runner_mode": effective_runner_mode,
                "external_codex_processes": len(processes),
                "pending_codex_slot_reservations": pending_task_ids,
                "effective_codex_slots": effective_slots,
                "capacity": capacity,
                "process_task_ids": process_task_ids,
                "same_task_is_running": same_task_is_running,
            }
            self._mark(
                db,
                task,
                TaskStatus.QUEUED,
                f"检测到本工作区仍有 {len(processes)}/{capacity} 个真实 Codex CLI 进程占用槽位，或本节点已有旧进程未收尾；本节点暂缓启动，避免重启后重复开工。",
            )
            self._add_event(
                db,
                task.id,
                "scheduler.external_codex_slots_full",
                "真实 Codex CLI 进程仍在运行，调度器暂缓启动新的 Codex CLI，等待旧进程收尾或写出 last-message。",
                payload,
            )
            if root_id != task.id:
                self._add_event(
                    db,
                    root_id,
                    "scheduler.external_codex_slots_full",
                    "检测到 API 重启边界遗留的真实 Codex CLI 进程仍在占槽；调度器保持排队，避免超过并发上限。",
                    payload,
                )
            db.commit()
        return True

    def _try_reserve_pending_codex_slot_with_capacity(self, task_id: str, phase: str) -> bool:
        capacity = max(1, self._agent_semaphore.capacity)
        outcome: dict[str, Any] = {}

        def reserve_if_available(reservations: dict[str, datetime]) -> None:
            processes = self._external_codex_processes(max_cache_age_seconds=2.0)
            process_task_ids = sorted({str(item.get("task_id") or "") for item in processes if item.get("task_id")})
            pending_task_ids = sorted(reservations)
            effective_slots = len(processes) + len(set(pending_task_ids) - set(process_task_ids))
            same_task_is_running = task_id in process_task_ids or task_id in pending_task_ids
            same_task_reserved_here = task_id in self._pending_codex_slot_reservations and task_id in pending_task_ids
            outcome["payload"] = {
                "task_id": task_id,
                "phase": phase,
                "external_codex_processes": len(processes),
                "pending_codex_slot_reservations": pending_task_ids,
                "effective_codex_slots": effective_slots,
                "capacity": capacity,
                "process_task_ids": process_task_ids,
                "same_task_is_running": same_task_is_running,
                "same_task_reserved_here": same_task_reserved_here,
            }
            if task_id in process_task_ids:
                outcome["allowed"] = False
                return
            if same_task_reserved_here:
                outcome["allowed"] = True
                return
            if effective_slots >= capacity or task_id in pending_task_ids:
                outcome["allowed"] = False
                return
            reserved_at = utcnow()
            reservations[task_id] = reserved_at
            self._pending_codex_slot_reservations[task_id] = reserved_at
            outcome["allowed"] = True

        try:
            self._mutate_shared_pending_codex_slot_reservations(reserve_if_available)
        except OSError:
            if self._hold_agent_start_for_external_codex_slots(task_id, phase):
                return False
            self._reserve_pending_codex_slot(task_id)
            return True

        if outcome.get("allowed"):
            return True
        payload = dict(outcome.get("payload") or {})
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return False
            root_id = self._root_id_for_task(db, task)
            payload["root_id"] = root_id
            payload["runner_mode"] = self._runner_mode_for_task(task_id)
            self._mark(
                db,
                task,
                TaskStatus.QUEUED,
                f"Codex CLI 槽位已满：当前 {payload.get('effective_codex_slots')} / {payload.get('capacity')}；本轮启动延后。",
            )
            self._add_event(
                db,
                task.id,
                "scheduler.external_codex_slots_full",
                "跨进程 Codex CLI 槽位预约已满；调度器延后启动，避免 API/local_kick 同时超发。",
                payload,
            )
            if root_id != task.id:
                self._add_event(
                    db,
                    root_id,
                    "scheduler.external_codex_slots_full",
                    "跨进程 Codex CLI 槽位预约已满；调度器延后启动，避免 API/local_kick 同时超发。",
                    payload,
                )
            db.commit()
        return False

    async def _reserve_agent_start(self, task_id: str, phase: str) -> bool:
        if self._hold_agent_start_for_api_restart(task_id, phase):
            return False
        if await self._hold_agent_start_for_machine_safety(task_id, phase):
            return False
        reserved_pending_slot = self._runner_mode_for_task(task_id) == "real"
        if reserved_pending_slot:
            if not self._try_reserve_pending_codex_slot_with_capacity(task_id, phase):
                return False
        limit = self._agent_burst_limit()
        if limit <= 0:
            return True
        reserve = self._agent_burst_effective_reserve()
        work_limit = self._agent_burst_work_limit()

        root_id = task_id
        should_hibernate = False
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                if reserved_pending_slot:
                    self._release_pending_codex_slot(task_id)
                return False
            root_id = self._root_id_for_task(db, task)
            used = self._agent_burst_used_for_root(db, root_id)
            if used >= work_limit:
                should_hibernate = True
                reason = (
                    f"本轮 Codex agent 工作启动额度已达到 {used}/{work_limit}，系统主动保存状态并休眠；"
                    f"总预算 {limit}，保留 {reserve} 次给自动恢复钩子。"
                )
                self._add_event(
                    db,
                    root_id,
                    "budget.guard_hibernate",
                    reason,
                    {
                        "task_id": task_id,
                        "phase": phase,
                        "used": used,
                        "limit": limit,
                        "work_limit": work_limit,
                        "reserve": reserve,
                        "resume_after_seconds": self.settings.agent_burst_resume_after_seconds,
                    },
                )
            else:
                used += 1
                self._set_agent_burst_used_for_root(db, root_id, used)
                self._add_event(
                    db,
                    task_id,
                    "budget.agent_start",
                    f"本轮 Codex agent 工作启动额度已使用 {used}/{work_limit}；总预算 {limit}，保留 {reserve}；阶段：{phase}。",
                    {"root_id": root_id, "phase": phase, "used": used, "limit": limit, "work_limit": work_limit, "reserve": reserve},
                )
            db.commit()

        if not should_hibernate:
            return True

        if reserved_pending_slot:
            self._release_pending_codex_slot(task_id)
        await self.hibernate_tree(
            root_id,
            self.settings.agent_burst_resume_after_seconds,
            f"达到本轮 Codex agent 工作启动额度，主动休眠；总预算 {limit}，保留 {reserve} 次给自动恢复钩子。",
        )
        return False

    async def _hold_agent_start_for_machine_safety(self, task_id: str, phase: str) -> bool:
        pressure, payload = self._machine_safety_pressure()
        if not pressure:
            return False
        resume_after = max(300, int(self.settings.machine_safety_resume_after_seconds))
        root_id = task_id
        reason = (
            "Machine safety guard: available memory is below the configured threshold; "
            "saved task state and paused new Codex starts to keep the local machine responsive."
        )
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return True
            root_id = self._root_id_for_task(db, task)
            event_payload = {
                **payload,
                "task_id": task_id,
                "phase": phase,
                "resume_after_seconds": resume_after,
            }
            self._add_event(db, root_id, "machine_safety.guard_hibernate", reason, event_payload)
            db.commit()
        await self.hibernate_tree(root_id, resume_after, reason)
        return True

    def _machine_safety_pressure(self) -> tuple[bool, dict[str, Any]]:
        return runtime_logs.machine_safety_pressure(self.settings, self._available_memory_mb)

    def _available_memory_mb(self) -> float | None:
        return runtime_logs.available_memory_mb()

    def _agent_burst_limit(self) -> int:
        return budget.agent_burst_limit(self.settings)

    def _agent_burst_effective_reserve(self) -> int:
        return budget.agent_burst_effective_reserve(self.settings)

    def _agent_burst_work_limit(self) -> int:
        return budget.agent_burst_work_limit(self.settings)

    def _agent_burst_used_for_root(self, db: Session, root_id: str) -> int:
        root = db.get(TaskNode, root_id)
        if root is None:
            return self._agent_starts_by_root[root_id]
        meta = loads(root.node_meta_json, {})
        if "agent_burst_used" in meta:
            raw_used = meta.get("agent_burst_used")
        else:
            raw_used = max(self._agent_starts_by_root[root_id], self._reconstruct_agent_burst_used(db, root_id))
        try:
            used = max(0, int(raw_used))
        except (TypeError, ValueError):
            used = 0
        self._agent_starts_by_root[root_id] = used
        return used

    def _set_agent_burst_used_for_root(self, db: Session, root_id: str, used: int) -> None:
        root = db.get(TaskNode, root_id)
        self._agent_starts_by_root[root_id] = max(0, int(used))
        if root is None:
            return
        meta = loads(root.node_meta_json, {})
        meta["agent_burst_used"] = self._agent_starts_by_root[root_id]
        meta["agent_burst_updated_at"] = utcnow().isoformat()
        root.node_meta_json = dumps(meta)

    async def _adjust_agent_concurrency(self, task_id: str, limiter: Any) -> None:
        if not isinstance(limiter, DynamicConcurrencyLimiter):
            return
        with SessionLocal() as db:
            try:
                decision = self._auto_concurrency_decision(db, task_id)
            except KeyError:
                return
        old_capacity, new_capacity = await limiter.set_capacity(int(decision["target"]))
        with SessionLocal() as db:
            root = db.get(TaskNode, str(decision["root_id"]))
            if root is None:
                return
            meta = loads(root.node_meta_json, {})
            previous = meta.get("auto_concurrency") if isinstance(meta.get("auto_concurrency"), dict) else {}
            meta["auto_concurrency"] = {
                **decision,
                "old_capacity": old_capacity,
                "new_capacity": new_capacity,
                "updated_at": utcnow().isoformat(),
            }
            root.node_meta_json = dumps(meta)
            if old_capacity == new_capacity and previous.get("mode") == decision.get("mode"):
                db.commit()
                return
            self._add_event(
                db,
                root.id,
                "concurrency.auto_adjust",
                f"自动油门已从 {old_capacity} 调整为 {new_capacity}：{decision['reason']}",
                {"old_capacity": old_capacity, "new_capacity": new_capacity, **decision},
            )
            db.commit()

    def _legacy_auto_concurrency_decision(self, db: Session, task_id: str) -> dict[str, Any]:
        lower, upper = self._auto_concurrency_bounds()
        task = self._get_task(db, task_id)
        root_id = self._root_id_for_task(db, task)
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        tasks = self._subtree(all_tasks, root_id)
        counts = Counter(task.status for task in tasks)
        active_agent_statuses = sum(1 for item in tasks if item.status in ACTIVE_AGENT_STATUSES)
        codex_slots_active = self._codex_slots_active()
        active_agents = max(active_agent_statuses, codex_slots_active)
        slot_pressure_agents = codex_slots_active
        queued_work = (
            counts[TaskStatus.PLANNED.value]
            + counts[TaskStatus.QUEUED.value]
            + counts[TaskStatus.REPAIR.value]
        )
        parent_ids = {item.parent_id for item in tasks if item.parent_id}
        active_frontier = sum(1 for item in tasks if item.id not in parent_ids and item.status in ACTIVE_FRONTIER_STATUSES)
        resource_pressure, pressure_reason = self._resource_pressure_state(db, tasks)
        target = lower
        mode = "low_power"
        reason = f"低功率巡航：队列 {queued_work}，活跃 {active_agents}，未达到提速阈值。"
        if not self.settings.auto_concurrency_enabled:
            target = max(1, self.settings.max_concurrency)
            mode = "manual"
            reason = "自动油门关闭，使用固定并发。"
        elif counts[TaskStatus.HIBERNATING.value] > 0:
            target = lower
            mode = "hibernate_low_power"
            reason = "任务树存在休眠节点，保持低功率等待流量/时间规则刷新。"
        elif resource_pressure:
            target = lower
            mode = "resource_cooldown"
            reason = f"检测到资源/流量压力，进入低功率冷却：{pressure_reason}"
        elif self._should_preserve_agent_budget(db, root_id, upper):
            target = lower
            mode = "budget_preserve"
            reason = "本轮 agent 启动预算接近下限，保持低功率运行并等待刷新或人工补充。"
        elif queued_work >= max(1, self.settings.auto_concurrency_queue_threshold) and active_frontier > slot_pressure_agents:
            target = upper
            mode = "high_throughput"
            reason = f"队列明显充足且无资源压力：可执行前线 {active_frontier}，排队 {queued_work}，提速到上限。"
        elif queued_work >= max(2, self.settings.auto_concurrency_queue_threshold // 2) and upper > lower:
            target = min(upper, lower + 1)
            mode = "balanced"
            reason = f"队列有一定余量且无资源压力：排队 {queued_work}，使用均衡档。"
        return {
            "root_id": root_id,
            "enabled": int(self.settings.auto_concurrency_enabled),
            "mode": mode,
            "target": max(lower, min(upper, int(target))),
            "lower_bound": lower,
            "upper_bound": upper,
            "queue_threshold": max(1, self.settings.auto_concurrency_queue_threshold),
            "queued_work": queued_work,
            "active_frontier": active_frontier,
            "active_agents": active_agents,
            "slot_pressure_agents": slot_pressure_agents,
            "resource_pressure": int(resource_pressure),
            "reason": reason,
        }

    def _auto_concurrency_decision(self, db: Session, task_id: str) -> dict[str, Any]:
        return concurrency.auto_concurrency_decision(db, task_id, self)

    def _auto_concurrency_backlog_target(
        self,
        *,
        lower: int,
        upper: int,
        queue_threshold: int,
        queued_work: int,
        active_frontier: int,
        active_agents: int,
    ) -> int:
        return concurrency.auto_concurrency_backlog_target(
            lower=lower,
            upper=upper,
            queue_threshold=queue_threshold,
            queued_work=queued_work,
            active_frontier=active_frontier,
            active_agents=active_agents,
        )

    def _auto_concurrency_fuel_state(
        self,
        *,
        queued_work: int,
        active_frontier: int,
        active_agents: int,
        lower: int,
        upper: int,
        codex_health: dict[str, Any],
    ) -> dict[str, Any]:
        return concurrency.auto_concurrency_fuel_state(
            self.settings,
            queued_work=queued_work,
            active_frontier=active_frontier,
            active_agents=active_agents,
            lower=lower,
            upper=upper,
            codex_health=codex_health,
        )

    def _auto_concurrency_bounds(self) -> tuple[int, int]:
        return budget.auto_concurrency_bounds(self.settings)

    def _reported_codex_slots_capacity(
        self,
        *,
        codex_slots_active: int = 0,
        auto_concurrency_snapshot: dict[str, Any] | None = None,
    ) -> int:
        # Report the target capacity; occupied slots may exceed it briefly and should stay visible.
        values = [max(1, int(self._agent_semaphore.capacity))]
        if auto_concurrency_snapshot:
            for key in ("target", "desired"):
                try:
                    values.append(max(0, int(auto_concurrency_snapshot.get(key) or 0)))
                except (TypeError, ValueError):
                    pass
        return max(values)

    def _should_preserve_agent_budget(self, db: Session, root_id: str, upper: int) -> bool:
        limit = self._agent_burst_limit()
        if limit <= 0:
            return False
        remaining = self._agent_burst_work_limit() - self._agent_burst_used_for_root(db, root_id)
        return remaining <= max(1, upper)

    def _resource_pressure_state(self, db: Session, tasks: list[TaskNode]) -> tuple[bool, str]:
        return runtime_logs.resource_pressure_state(db, tasks, self.settings)

    def _is_resource_pressure_block_report(self, report: dict[str, Any]) -> bool:
        return runtime_logs.is_resource_pressure_block_report(report)

    def _is_fast_retryable_block_report(self, report: dict[str, Any]) -> bool:
        normalized = self._normalize_block_report(report)
        return bool(normalized.get("retryable")) and str(normalized.get("category") or "") in FAST_RETRYABLE_BLOCK_CATEGORIES

    def _auto_retry_fast_retryable_blocks(self, root_id: str) -> int:
        if self.settings.frozen_mode:
            return 0
        with SessionLocal() as db:
            root = db.get(TaskNode, root_id)
            if root is None:
                return 0
            tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root.id)
            requeued = 0
            for task in tasks:
                if task.status != TaskStatus.BLOCKED.value:
                    continue
                report = self._child_block_report(task)
                if not self._is_fast_retryable_block_report(report):
                    continue
                meta = loads(task.node_meta_json, {})
                category = str(report.get("category") or "fast_retryable")
                retries_by_category = meta.get("fast_retryable_block_retries_by_category")
                if not isinstance(retries_by_category, dict):
                    retries_by_category = {}
                if category in retries_by_category:
                    attempts = int(retries_by_category.get(category) or 0)
                elif str(meta.get("last_fast_retryable_block_retry_reason") or "") == category:
                    attempts = int(meta.get("fast_retryable_block_retries") or 0)
                else:
                    attempts = 0
                if attempts >= FAST_RETRYABLE_BLOCK_RETRY_LIMIT:
                    continue
                meta["block_report"] = report
                meta["fast_retryable_block_retries"] = attempts + 1
                retries_by_category[category] = attempts + 1
                meta["fast_retryable_block_retries_by_category"] = retries_by_category
                meta["last_fast_retryable_block_retry_at"] = utcnow().isoformat()
                meta["last_fast_retryable_block_retry_reason"] = category
                task.node_meta_json = dumps(meta)
                task.status = TaskStatus.QUEUED.value
                task.error_message = None
                task.updated_at = utcnow()
                self._add_event(
                    db,
                    task.id,
                    "task.fast_retryable_block_requeued",
                    "可快速重试的 Codex 通道阻塞已重新排队；不会触发整棵树低功耗休眠。",
                    {"root_id": root.id, "category": category, "attempts": attempts + 1},
                )
                if task.parent_id:
                    self._add_event(
                        db,
                        task.parent_id,
                        "parent.fast_retryable_child_requeued",
                        "可快速重试的子阻塞已重新排队，父节点继续等待收敛。",
                        {"child_id": task.id, "root_id": root.id, "category": category},
                    )
                requeued += 1
            if requeued:
                db.commit()
            return requeued

    def _active_resource_pressure_from_logs(self, tasks: list[TaskNode]) -> tuple[bool, str]:
        return runtime_logs.active_resource_pressure_from_logs(tasks, self.settings)

    def _reconstruct_agent_burst_used(self, db: Session, root_id: str) -> int:
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        task_ids = [task.id for task in self._subtree(all_tasks, root_id)]
        if not task_ids:
            return 0
        reset_event = db.scalars(
            select(EventRecord)
            .where(EventRecord.task_id == root_id, EventRecord.type == "tree.hibernate_resume")
            .order_by(EventRecord.created_at.desc())
        ).first()
        query = select(EventRecord).where(EventRecord.task_id.in_(task_ids), EventRecord.type == "budget.agent_start")
        if reset_event is not None:
            query = query.where(EventRecord.created_at > reset_event.created_at)
        return len(list(db.scalars(query).all()))

    def _block_task(self, db: Session, task: TaskNode, reason: str, source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        report = self._build_block_report(task, reason, source, payload)
        meta = loads(task.node_meta_json, {})
        meta["block_report"] = report
        task.node_meta_json = dumps(meta)
        self._mark(db, task, TaskStatus.BLOCKED, reason)
        if task.parent_id:
            self._add_event(
                db,
                task.parent_id,
                "child.blocked_report",
                f"子节点 {task.title} 上报阻塞：{reason}",
                {"child_id": task.id, "report": report},
            )
        return report

    def _suppress_blocked_recovery(self, task: TaskNode, key: str, suppression: str) -> None:
        """把"这次 blocked 为什么不拆子任务"写进节点 meta（供人事后核对，也让看板能读）。

        为什么不用事件代替：这条决策的输入（scope 里的恢复链标记、block_report 分类）都在 meta 里，
        判据与证据放在同一处才好复核；`_block_task` 自己也会写 meta，所以顺序是**先写这条再 block**。
        """
        meta = loads(task.node_meta_json, {})
        meta[key] = suppression
        meta[f"{key}_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)

    def _build_block_report(self, task: TaskNode, reason: str, source: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return blockers.build_block_report(task, reason, source, payload, self.settings)

    def _dependency_reports_are_retryable(self, payload: dict[str, Any] | None) -> bool:
        return blockers.dependency_reports_are_retryable(payload)

    def _dependency_block_context(self, payload: dict[str, Any] | None) -> dict[str, Any] | None:
        return blockers.dependency_block_context(payload)

    def _dependency_report_context(self, report: dict[str, Any]) -> dict[str, Any] | None:
        return blockers.dependency_report_context(report)

    def _normalize_block_report(self, report: dict[str, Any]) -> dict[str, Any]:
        return blockers.normalize_block_report(report)
    @staticmethod
    def _false_readonly_sandbox_category(reason: str, source: str) -> str:
        return blockers.false_readonly_sandbox_category(reason, source)

    @staticmethod
    def _false_tool_capability_protocol_category(reason: str, source: str) -> str:
        return blockers.false_tool_capability_protocol_category(reason, source)

    def _block_category(self, reason: str) -> str:
        return blockers.block_category(reason)

    def _protocol_block_category(self, reason: str, source: str) -> str:
        return blockers.protocol_block_category(reason, source)

    @staticmethod
    def _obvious_non_protocol_block_text(normalized: str) -> bool:
        return blockers.obvious_non_protocol_block_text(normalized)

    def _transient_block_category(self, reason: str) -> str:
        return blockers.transient_block_category(reason)

    def _explicit_retryable_resource_reason(self, normalized_reason: str) -> bool:
        return blockers.explicit_retryable_resource_reason(normalized_reason)

    def _skill_lookup_report_context(self, category: str, parent_action: str) -> dict[str, Any] | None:
        return blockers.skill_lookup_report_context(category, parent_action, self.settings)

    def _skill_lookup_recovery_text(self) -> str:
        url = self.settings.skill_km_url.strip()
        if not self.settings.skill_lookup_enabled or not url:
            return ""
        return (
            "\n\nSkill/KM 自救要求：在修复前先尝试使用配置的 Skill/KM 知识库寻找解决方案。"
            f"\nSkill/KM 入口：{url}"
            "\n搜索关键词应来自具体错误文本、失败命令、工具名、API 名、文件路径或验证日志。"
            "\n不要消耗时间做人类登录、SSO、权限申请或验证码；如果遇到这些卡点，必须明确上报为需要人工授权，并继续给父层可处理的证据。"
        )

    @staticmethod
    def _is_human_required_blocker(report: dict[str, Any]) -> bool:
        return (
            bool(report.get("human_action_required"))
            or str(report.get("category") or "") in {"human_auth", "visual_uncertain"}
            or str(report.get("parent_action") or "") in {"human_auth_required", "human_visual_review"}
        )

    def _visual_uncertain_payload(self, payload: dict[str, Any], phase: str) -> dict[str, Any]:
        next_payload = dict(payload)
        block_report: dict[str, Any] = {
            "category": "visual_uncertain",
            "retryable": False,
            "parent_action": "human_visual_review",
            "phase": phase,
        }
        for key in ("checks", "evidence", "suggested_children"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                block_report[key] = repair_mojibake(value)
        next_payload["block_report"] = block_report
        return next_payload

    def _child_block_report(self, child: TaskNode) -> dict[str, Any]:
        return blockers.child_block_report(child, self.settings)

    def _retryable_blocked_children(self, task: TaskNode) -> list[TaskNode]:
        return [
            child
            for child in task.children
            if child.status == TaskStatus.BLOCKED.value and bool(self._child_block_report(child).get("retryable"))
        ]

    def _store_child_block_reports(self, db: Session, task: TaskNode, reports: list[dict[str, Any]]) -> None:
        """把子节点阻塞报告写进 meta —— **由债务表派生**（§7.3），不再独立成第二份真相。

        传入的 `reports` 会先变成 `child_blocked` / `child_failed` 债务，再把未清债务重建回这个
        列表。于是"债被清掉"与"报告消失"是同一件事，不会出现一边清了另一边还留着的状态。

        用**调用方的 session**：`task` 属于那个 session，另开一个会写出两份互相看不见的状态。
        """
        self._refresh_child_status_obligations(db, task, reports=reports)

    def _refresh_child_status_obligations(
        self, db: Session, task: TaskNode, *, reports: list[dict[str, Any]] | None = None
    ) -> None:
        """维护 `child_blocked` / `child_failed` 债务，并据此重建 `blocked_child_reports`。

        债务是唯一真相：报告列表是从**未清的这类债**派生出来的。清债按事实——子节点不再是
        阻塞/失败状态（例如已被修复）即清，通路记 `repair_child`。
        """
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
        by_child_id = {child.id: child for child in children}
        provided = {str(report.get("task_id") or ""): report for report in (reports or [])}
        # 已被修复分支取代的阻塞子节点**不算还堵着**：沿用既有的
        # `_superseded_by_done_repair` 判据，否则"修好了"与"还堵着"会同时成立，
        # 债永远清不掉（实测：两条既有契约当场变红）。
        blocking_children = [
            child
            for child in children
            if child.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}
            and not self._superseded_by_done_repair(child, by_child_id)
        ]
        incoming: list[dict[str, Any]] = []
        for child in blocking_children:
            report = provided.get(child.id) or self._child_block_report(child)
            fact = obligations.child_status_debt(
                task.id, child.id, status=child.status, report=report, depth=task.depth
            )
            if fact is not None:
                incoming.append(fact)
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        updated = obligations.upsert(
            obligations.settle_child_status_cleared(
                existing, still_blocking_child_ids=set(by_child_id) & {child.id for child in blocking_children}
            ),
            incoming,
        )
        meta["obligations"] = updated
        meta["blocked_child_reports"] = obligations.child_status_reports(updated)
        meta["blocked_child_report_updated_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)

    def _superseded_by_done_repair(self, child: TaskNode, siblings_by_id: dict[str, TaskNode]) -> bool:
        if child.status not in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}:
            return False
        meta = loads(child.node_meta_json, {})
        repair_ids = meta.get("superseded_by_repair_child_ids")
        if not isinstance(repair_ids, list):
            return False
        return any(
            (repair := siblings_by_id.get(str(repair_id))) is not None and repair.status == TaskStatus.DONE.value
            for repair_id in repair_ids
        )

    def _has_active_block_repair(self, child: TaskNode, siblings_by_id: dict[str, TaskNode]) -> bool:
        meta = loads(child.node_meta_json, {})
        repair_ids = meta.get("superseded_by_repair_child_ids")
        if not isinstance(repair_ids, list):
            return False
        return any(
            (repair := siblings_by_id.get(str(repair_id))) is not None
            and repair.status not in TERMINAL_STATUSES
            and repair.status not in STOPPED_STATUSES
            for repair_id in repair_ids
        )

    def _blocker_visibility_counts(self, tasks: list[TaskNode]) -> dict[str, int]:
        tasks_by_id = {task.id: task for task in tasks}
        active_blocked = 0
        superseded_blocked = 0
        repair_pending_blocked = 0
        repair_done_blocked = 0
        inactive_branch_blocked = 0
        resource_wait_blocked = 0
        inactive_ancestor_statuses = {
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELED.value,
            TaskStatus.PAUSED.value,
            TaskStatus.HIBERNATING.value,
        }

        def has_inactive_ancestor(task: TaskNode) -> bool:
            parent_id = task.parent_id
            seen: set[str] = set()
            while parent_id and parent_id not in seen:
                seen.add(parent_id)
                parent = tasks_by_id.get(parent_id)
                if parent is None:
                    return False
                if parent.status in inactive_ancestor_statuses:
                    return True
                parent_id = parent.parent_id
            return False

        for task in tasks:
            if task.status not in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}:
                continue
            if has_inactive_ancestor(task):
                inactive_branch_blocked += 1
                superseded_blocked += 1
                continue
            report = self._child_block_report(task)
            if self._is_resource_pressure_block_report(report):
                resource_wait_blocked += 1
                continue
            meta = loads(task.node_meta_json, {})
            repair_ids = meta.get("superseded_by_repair_child_ids")
            if not isinstance(repair_ids, list) or not repair_ids:
                active_blocked += 1
                continue
            repairs = [
                repair
                for repair_id in repair_ids
                if (repair := tasks_by_id.get(str(repair_id))) is not None
            ]
            if not repairs:
                active_blocked += 1
                continue
            if any(repair.status == TaskStatus.DONE.value for repair in repairs):
                superseded_blocked += 1
                repair_done_blocked += 1
            elif any(repair.status not in TERMINAL_STATUSES and repair.status not in STOPPED_STATUSES for repair in repairs):
                superseded_blocked += 1
                repair_pending_blocked += 1
            else:
                active_blocked += 1
        return {
            "active_blocked": active_blocked,
            "superseded_blocked": superseded_blocked,
            "repair_pending_blocked": repair_pending_blocked,
            "repair_done_blocked": repair_done_blocked,
            "inactive_branch_blocked": inactive_branch_blocked,
            "resource_wait_blocked": resource_wait_blocked,
        }

    def _attach_repair_children_to_blockers(self, blockers: list[TaskNode], repair_child_ids: list[str]) -> None:
        for blocker in blockers:
            meta = loads(blocker.node_meta_json, {})
            repair_ids = meta.get("superseded_by_repair_child_ids")
            if not isinstance(repair_ids, list):
                repair_ids = []
            for child_id in repair_child_ids:
                if child_id not in repair_ids:
                    repair_ids.append(child_id)
            meta["superseded_by_repair_child_ids"] = repair_ids
            meta["superseded_reason"] = "parent_blocked_child_repair"
            meta["superseded_updated_at"] = utcnow().isoformat()
            blocker.node_meta_json = dumps(meta)

    def _blocked_child_repair_children(self, parent: TaskNode, blockers: list[TaskNode]) -> list[ChildTaskSpec]:
        reports = [self._child_block_report(child) for child in blockers]
        title_bits = [repair_mojibake_text(child.title) for child in blockers[:2]]
        suffix = "等" if len(blockers) > 2 else ""
        title = f"修复或替代阻塞子成果：{'、'.join(title_bits)}{suffix}"
        summary = "\n".join(
            f"- {repair_mojibake_text(str(report.get('title') or report.get('task_id')))}"
            f"（{report.get('source') or 'unknown'}）：{repair_mojibake_text(str(report.get('reason') or '未提供原因'))}"
            for report in reports
        )
        blocker_ids = [child.id for child in blockers]
        parent_scope = loads(parent.scope_json, [])
        parent_acceptance = loads(parent.acceptance_json, [])
        return [
            ChildTaskSpec(
                title=title,
                description=(
                    "父节点检测到子节点阻塞/失败，但不能把整棵递归树停在人工审查。"
                    "请在当前 WORKING_DIRECTORY 内检查阻塞子节点的交付、日志和工作树，"
                    "选择修复原交付或实现等价替代交付，并明确说明哪些原子节点已被替代。"
                    f"\n\n阻塞节点ID：{', '.join(blocker_ids)}\n阻塞上报：\n{summary}"
                    f"{self._skill_lookup_recovery_text()}"
                ),
                scope=[
                    *[str(item) for item in parent_scope],
                    *[f"blocked:{child.id}:{repair_mojibake_text(child.title)}" for child in blockers],
                ],
                acceptance=[
                    "必须产出可被父节点整合的 changed_files，路径相对当前 WORKING_DIRECTORY。",
                    "必须说明每个被替代/修复的阻塞子节点 ID、原失败原因和采取的修复策略。",
                    "交付物必须覆盖被阻塞子节点原本承诺的父级验收边界，不能把父目标降级为更小 demo。",
                    "必须提供本地静态检查、启动检查或人工验收步骤；如果工具缺失，必须说明替代证据。",
                    *[str(item) for item in parent_acceptance[:4]],
                ],
                coverage_ids=["blocked-child-repair", *[str(report.get("task_id") or "") for report in reports]],
                verification_kind="blocked child repair + replacement integration evidence",
                estimated_leaf=False,
            )
        ]

    async def _process_children_with_dependencies(
        self,
        parent_id: str,
        child_ids: list[str],
        runner: BaseCodexRunner,
        semaphore: asyncio.Semaphore,
    ) -> None:
        remaining = set(child_ids)
        deferred_until_next_run: set[str] = set()
        while remaining:
            await self._adjust_agent_concurrency(parent_id, semaphore)
            ready: list[str] = []
            pending_worker_priority: dict[str, bool] = {}
            ready_priority: dict[str, tuple[int, int, datetime, str]] = {}
            with SessionLocal() as db:
                parent = self._get_task(db, parent_id)
                if parent.status in TERMINAL_STATUSES or parent.status in STOPPED_STATUSES:
                    return
                siblings = list(parent.children)
                legacy_settled_ids, legacy_breaker_ids = self._settle_legacy_convergence_slice_children(db, parent, siblings)
                if legacy_settled_ids or legacy_breaker_ids:
                    db.flush()
                    remaining.difference_update(legacy_settled_ids)
                    remaining.update(legacy_breaker_ids)
                    siblings = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all())
                redundant_breaker_ids = self._settle_redundant_convergence_cycle_breaker_children(db, parent, siblings)
                if redundant_breaker_ids:
                    db.flush()
                    remaining.difference_update(redundant_breaker_ids)
                    siblings = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all())
                siblings_by_id = {child.id: child for child in siblings}
                for child_id in list(remaining):
                    if child_id in deferred_until_next_run:
                        continue
                    child = siblings_by_id.get(child_id)
                    if child is None:
                        remaining.discard(child_id)
                        continue
                    if child.status in TERMINAL_STATUSES or child.status in STOPPED_STATUSES:
                        remaining.discard(child_id)
                        continue
                    dependency_ids = self._dependency_ids(child, siblings)
                    missing = [dependency_id for dependency_id in dependency_ids if dependency_id not in siblings_by_id]
                    if missing:
                        self._block_task(
                            db,
                            child,
                            f"依赖节点不存在，无法安全执行：{', '.join(missing)}",
                            "dependency.missing",
                            {"dependencies": missing},
                        )
                        remaining.discard(child_id)
                        continue
                    if self._is_skill_note_task(child):
                        dependencies = [siblings_by_id[dependency_id] for dependency_id in dependency_ids]
                        if all(
                            dependency.status in TERMINAL_STATUSES or dependency.status in STOPPED_STATUSES
                            for dependency in dependencies
                        ):
                            ready.append(child_id)
                            pending_worker_priority[child_id] = self._pending_worker_result(child) is not None
                            ready_priority[child_id] = self._scheduler_ready_priority(child, pending_worker_priority[child_id])
                        continue
                    broken = [
                        dependency
                        for dependency in (siblings_by_id[dependency_id] for dependency_id in dependency_ids)
                        if dependency.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value}
                    ]
                    optional_broken = [dependency for dependency in broken if self._is_optional_skill_lookup_dependency(dependency)]
                    if optional_broken:
                        self._skip_optional_dependencies(db, child, optional_broken)
                        optional_ids = {dependency.id for dependency in optional_broken}
                        dependency_ids = [dependency_id for dependency_id in dependency_ids if dependency_id not in optional_ids]
                        broken = [dependency for dependency in broken if dependency.id not in optional_ids]
                    if broken:
                        dependency_reports = self._dependency_block_reports(broken)
                        self._block_task(
                            db,
                            child,
                            self._dependency_block_reason(dependency_reports),
                            "dependency.blocked",
                            {"dependencies": [dependency.id for dependency in broken], "dependency_reports": dependency_reports},
                        )
                        remaining.discard(child_id)
                        continue
                    if all(siblings_by_id[dependency_id].status == TaskStatus.DONE.value for dependency_id in dependency_ids):
                        ready.append(child_id)
                        pending_worker_priority[child_id] = self._pending_worker_result(child) is not None
                        ready_priority[child_id] = self._scheduler_ready_priority(child, pending_worker_priority[child_id])
                db.commit()

            if not ready:
                return
            ready.sort(key=lambda child_id: ready_priority.get(child_id, (99, 99, datetime.max, child_id)))
            await asyncio.gather(*(self._process_node(child_id, runner, semaphore) for child_id in ready))
            with SessionLocal() as db:
                completed_or_stopped = {
                    child_id
                    for child_id in ready
                    if (task := db.get(TaskNode, child_id)) is not None
                    and (task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES)
                }
                unfinished = sorted(set(ready) - completed_or_stopped)
                if unfinished:
                    self._add_event(
                        db,
                        parent_id,
                        "scheduler.child_deferred",
                        "子节点本轮执行后仍未进入完成/终态，父节点保留等待状态，避免误判或忙循环。",
                        {
                            "child_ids": unfinished,
                            "retry_after_seconds": max(0.0, self.settings.scheduler_deferred_retry_seconds),
                        },
                    )
                    db.commit()
                    self._schedule_deferred_parent_resume(parent_id, unfinished)
            remaining.difference_update(completed_or_stopped)
            deferred_until_next_run.update(set(ready) - completed_or_stopped)
            if not completed_or_stopped:
                return

    def _schedule_deferred_parent_resume(self, parent_id: str, child_ids: list[str]) -> None:
        delay = self.settings.scheduler_deferred_retry_seconds
        if delay < 0:
            return
        existing = self._deferred_parent_tasks.get(parent_id)
        if existing is not None and not existing.done():
            return

        async def deferred_resume() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                with SessionLocal() as db:
                    parent = db.get(TaskNode, parent_id)
                    if parent is None or parent.status in TERMINAL_STATUSES or parent.status in STOPPED_STATUSES:
                        return
                    root_id = self._root_id_for_task(db, parent)
                    self._add_event(
                        db,
                        parent_id,
                        "scheduler.deferred_resume",
                        "调度器自动接力仍在等待的子树；这是本地调度动作，本身不消耗 Codex token。",
                        {"child_ids": child_ids, "delay_seconds": max(0.0, delay), "root_id": root_id},
                    )
                    db.commit()
                runner = make_runner(self.settings, self._runner_mode_for_task(parent_id))
                await self._process_node(parent_id, runner, self._agent_semaphore)
                await self._auto_hibernate_retryable_resource_blocks(root_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with SessionLocal() as db:
                    if db.get(TaskNode, parent_id) is not None:
                        self._add_event(
                            db,
                            parent_id,
                            "scheduler.deferred_resume_failed",
                            f"调度器自动接力失败：{exc}",
                            {"child_ids": child_ids, "error": str(exc)},
                        )
                        db.commit()

        task = asyncio.create_task(deferred_resume())
        self._deferred_parent_tasks[parent_id] = task
        self._background_tasks.add(task)

        def cleanup(completed: asyncio.Task) -> None:
            if self._deferred_parent_tasks.get(parent_id) is completed:
                self._deferred_parent_tasks.pop(parent_id, None)
            self._background_tasks.discard(completed)

        task.add_done_callback(cleanup)

    def recover_deferred_parent_resumes(self, root_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            settled_unreachable = self._settle_unreachable_descendants_under_terminal_ancestors(db, root_id)
            if settled_unreachable:
                db.commit()
            candidates = self._deferred_parent_resume_candidates(db, root_id)
        scheduled: list[dict[str, Any]] = []
        for parent_id, child_ids in candidates:
            existing = self._deferred_parent_tasks.get(parent_id)
            if existing is not None and not existing.done():
                continue
            self._schedule_deferred_parent_resume(parent_id, child_ids)
            scheduled.append({"parent_id": parent_id, "child_ids": child_ids})
        if scheduled:
            with SessionLocal() as db:
                root = db.get(TaskNode, root_id)
                if root is not None:
                    self._add_event(
                        db,
                        root_id,
                        "scheduler.deferred_recovered",
                        "服务重启或恢复后已重建等待父节点的本地接力调度，继续推进 queued frontier。",
                        {"scheduled": scheduled, "count": len(scheduled), "settled_unreachable": sorted(settled_unreachable)},
                    )
                    db.commit()
        return {
            "status": "scheduled",
            "count": len(scheduled),
            "scheduled": scheduled,
            "settled_unreachable": sorted(settled_unreachable),
        }

    def settle_unreachable_descendants_under_terminal_ancestors(self, root_id: str) -> dict[str, Any]:
        with SessionLocal() as db:
            settled = self._settle_unreachable_descendants_under_terminal_ancestors(db, root_id)
            if settled:
                db.commit()
        return {"status": "settled", "count": len(settled), "task_ids": sorted(settled)}

    def _settle_unreachable_descendants_under_terminal_ancestors(self, db: Session, root_id: str) -> set[str]:
        root = db.get(TaskNode, root_id)
        if root is None:
            return set()
        tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()), root_id)
        terminal_ancestor_by_id: dict[str, TaskNode] = {}
        settled: set[str] = set()
        now = utcnow().isoformat()
        for task in sorted(tasks, key=lambda item: (item.depth, item.created_at, item.id)):
            inherited_terminal = terminal_ancestor_by_id.get(task.parent_id or "")
            if inherited_terminal and task.status not in TERMINAL_STATUSES and task.status not in ACTIVE_AGENT_STATUSES:
                meta = loads(task.node_meta_json, {})
                if not isinstance(meta, dict):
                    meta = {}
                meta["settled_unreachable_due_to_terminal_ancestor"] = {
                    "ancestor_id": inherited_terminal.id,
                    "ancestor_status": inherited_terminal.status,
                    "settled_at": now,
                }
                task.node_meta_json = dumps(meta)
                task.result_json = dumps(
                    {
                        "system": {
                            "status": "canceled_unreachable_due_to_terminal_ancestor",
                            "ancestor_id": inherited_terminal.id,
                            "ancestor_status": inherited_terminal.status,
                            "reason": "A terminal ancestor means this child can no longer be scheduled safely; it is stale state from an already-settled branch.",
                        }
                    }
                )
                self._mark(
                    db,
                    task,
                    TaskStatus.CANCELED,
                    "Terminal ancestor already settled this branch; stale descendant canceled so recursive convergence can continue.",
                )
                settled.add(task.id)
            if task.status in TERMINAL_STATUSES:
                terminal_ancestor_by_id[task.id] = task
            elif inherited_terminal:
                terminal_ancestor_by_id[task.id] = inherited_terminal
        if settled:
            self._add_event(
                db,
                root_id,
                "scheduler.unreachable_descendants_settled",
                "Stale non-terminal descendants below terminal ancestors were canceled so deferred parent acceptance can converge.",
                {"task_ids": sorted(settled), "count": len(settled)},
            )
        return settled

    def _deferred_parent_resume_candidates(self, db: Session, root_id: str) -> list[tuple[str, list[str]]]:
        root = db.get(TaskNode, root_id)
        if root is None:
            return []
        tasks = self._subtree(list(db.scalars(select(TaskNode).order_by(TaskNode.depth.desc(), TaskNode.created_at)).all()), root_id)
        children_by_parent: dict[str, list[TaskNode]] = {}
        for task in tasks:
            if task.parent_id:
                children_by_parent.setdefault(task.parent_id, []).append(task)

        candidates: list[tuple[str, list[str]]] = []
        resumable_parent_statuses = {TaskStatus.WAITING_CHILDREN.value, TaskStatus.QUEUED.value}
        for parent in tasks:
            if parent.id == root_id:
                continue
            if parent.status not in resumable_parent_statuses:
                continue
            children = children_by_parent.get(parent.id, [])
            if not children:
                continue
            if any(child.status in ACTIVE_AGENT_STATUSES for child in children):
                continue
            if all(child.status == TaskStatus.DONE.value for child in children):
                candidates.append((parent.id, []))
                continue
            if parent.status != TaskStatus.WAITING_CHILDREN.value:
                continue
            child_ids = [
                child.id
                for child in sorted(children, key=lambda item: self._scheduler_ready_priority(item, self._pending_worker_result(item) is not None))
                if child.status not in TERMINAL_STATUSES and child.status not in STOPPED_STATUSES
            ]
            if child_ids:
                candidates.append((parent.id, child_ids))
        return candidates

    def _dependency_ids(self, child: TaskNode, siblings: list[TaskNode]) -> list[str]:
        meta = loads(child.node_meta_json, {})
        siblings_by_id = {task.id: task for task in siblings}
        siblings_by_title = {self._normalize_signature_text(task.title): task.id for task in siblings}
        dependency_ids: list[str] = []
        raw_dependencies = []
        if isinstance(meta.get("depends_on_ids"), list):
            raw_dependencies.extend(str(item) for item in meta["depends_on_ids"])
        if isinstance(meta.get("depends_on"), list):
            raw_dependencies.extend(str(item) for item in meta["depends_on"])
        for raw_dependency in raw_dependencies:
            dependency_id = raw_dependency if raw_dependency in siblings_by_id else siblings_by_title.get(self._normalize_signature_text(raw_dependency))
            if dependency_id and dependency_id != child.id and dependency_id not in dependency_ids:
                dependency_ids.append(dependency_id)
        return dependency_ids

    def _is_optional_skill_lookup_dependency(self, dependency: TaskNode) -> bool:
        if dependency.status != TaskStatus.CANCELED.value:
            return False
        text = repair_mojibake_text(f"{dependency.title} {dependency.description} {dependency.error_message or ''}").lower()
        optional_markers = (
            "skill_notes",
            "skill note",
            "skill/km",
            "skill km",
            "km ",
            "skill_km_url",
            "技能笔记",
            "技能沉淀",
            "自救",
            "授权",
            "权限",
        )
        return self._is_skill_note_task(dependency) or any(marker in text for marker in optional_markers)

    def _skip_optional_dependencies(self, db: Session, child: TaskNode, dependencies: list[TaskNode]) -> None:
        meta = loads(child.node_meta_json, {})
        dependency_ids = {dependency.id for dependency in dependencies}
        dependency_titles = {self._normalize_signature_text(dependency.title) for dependency in dependencies}
        depends_on_ids = [str(item) for item in meta.get("depends_on_ids", [])] if isinstance(meta.get("depends_on_ids"), list) else []
        depends_on = [str(item) for item in meta.get("depends_on", [])] if isinstance(meta.get("depends_on"), list) else []
        meta["depends_on_ids"] = [dependency_id for dependency_id in depends_on_ids if dependency_id not in dependency_ids]
        meta["depends_on"] = [
            dependency
            for dependency in depends_on
            if dependency not in dependency_ids and self._normalize_signature_text(dependency) not in dependency_titles
        ]
        skipped = meta.get("optional_dependencies_skipped")
        if not isinstance(skipped, list):
            skipped = []
        skipped.extend(
            {
                "task_id": dependency.id,
                "title": repair_mojibake_text(dependency.title),
                "status": dependency.status,
                "skipped_at": utcnow().isoformat(),
                "reason": "optional_skill_lookup_dependency_canceled",
            }
            for dependency in dependencies
        )
        meta["optional_dependencies_skipped"] = skipped[-20:]
        child.node_meta_json = dumps(meta)
        self._add_event(
            db,
            child.id,
            "dependency.optional_skipped",
            "Optional Skill/KM dependency was canceled; continuing with remaining dependencies.",
            {"dependencies": [dependency.id for dependency in dependencies]},
        )

    def _dependency_block_reports(self, dependencies: list[TaskNode]) -> list[dict[str, Any]]:
        reports: list[dict[str, Any]] = []
        for dependency in dependencies:
            dependency_report = self._child_block_report(dependency)
            reason = str(dependency_report.get("reason") or dependency.error_message or f"依赖节点状态为 {dependency.status}")
            reports.append(
                {
                    "task_id": dependency.id,
                    "title": repair_mojibake_text(dependency.title),
                    "status": dependency.status,
                    "status_label": self._status_label(dependency.status),
                    "source": dependency_report.get("source") or "dependency.status",
                    "category": dependency_report.get("category") or self._block_category(reason),
                    "reason": repair_mojibake_text(reason),
                    "retryable": bool(dependency_report.get("retryable")),
                    "parent_action": dependency_report.get("parent_action") or "manual_review",
                }
            )
        return reports

    def _dependency_block_reason(self, dependency_reports: list[dict[str, Any]]) -> str:
        if not dependency_reports:
            return "依赖节点未成功完成，当前节点不能继续执行。"
        if len(dependency_reports) == 1:
            report = dependency_reports[0]
            return (
                f"依赖节点「{report['title']}」未成功完成"
                f"（状态：{report['status_label']}），当前节点不能继续执行。根因：{report['reason']}"
            )
        titles = "、".join(f"「{report['title']}」" for report in dependency_reports[:3])
        suffix = "等" if len(dependency_reports) > 3 else ""
        first = dependency_reports[0]
        return f"{len(dependency_reports)} 个依赖节点未成功完成：{titles}{suffix}。首个根因：{first['reason']}"

    def _status_label(self, status: str) -> str:
        return {
            TaskStatus.PLANNED.value: "已规划",
            TaskStatus.PLANNING.value: "规划中",
            TaskStatus.DECOMPOSING.value: "拆解中",
            TaskStatus.QUEUED.value: "排队中",
            TaskStatus.WAITING_CHILDREN.value: "等待子任务",
            TaskStatus.RUNNING.value: "执行中",
            TaskStatus.WORKING.value: "执行中",
            TaskStatus.VERIFYING.value: "验收中",
            TaskStatus.ACCEPTING.value: "整合验收",
            TaskStatus.DONE.value: "已完成",
            TaskStatus.REPAIR.value: "待修复",
            TaskStatus.BLOCKED.value: "已阻塞",
            TaskStatus.FAILED.value: "失败",
            TaskStatus.PAUSED.value: "已暂停",
            TaskStatus.HIBERNATING.value: "休眠中",
            TaskStatus.CANCELED.value: "已取消",
        }.get(status, status)

    async def _process_node_unlocked(self, task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        existing_child_ids: list[str] | None = None
        pending_worker_result: WorkerResult | None = None
        recovered_planner_decision: PlannerDecision | None = None
        root_gate_child_ids: list[str] | None = None
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            children = list(task.children)
            if children:
                backfilled_child_ids = self._backfill_parent_skill_note_child(db, task)
                if backfilled_child_ids:
                    db.flush()
                    children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
                settled_final_gate_ids = self._settle_redundant_final_gate_sibling_branches(db, task, children)
                if settled_final_gate_ids:
                    db.flush()
                    children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
                settled_passed_final_gate_ids = self._settle_passed_root_final_gate_branches(db, task)
                if settled_passed_final_gate_ids:
                    db.flush()
                    children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
                proactive_final_gate_child_ids = self._proactive_root_final_gate_children(db, task, children)
                if proactive_final_gate_child_ids:
                    db.flush()
                    children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
                proactive_block_repair_child_ids = self._proactive_blocked_child_repair_children(db, task, children)
                if proactive_block_repair_child_ids:
                    db.flush()
                    children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
                existing_child_ids = [child.id for child in children if child.status not in TERMINAL_STATUSES and child.status not in STOPPED_STATUSES]
                self._mark(db, task, TaskStatus.WAITING_CHILDREN, "检测到已有子任务，继续推进现有子树，避免重复规划父节点。")
                db.commit()
            else:
                pending_worker_result = self._pending_worker_result(task)
            if existing_child_ids is None and pending_worker_result is not None:
                self._mark(db, task, TaskStatus.QUEUED, "检测到已有 worker 成功结果，恢复时直接进入叶子验收，避免重复执行 worker。")
                db.commit()
            elif existing_child_ids is None and pending_worker_result is None:
                root_gate_child_ids = self._pre_planner_root_final_gate_children(db, task)
                if root_gate_child_ids:
                    db.commit()
                else:
                    recovered_planner_decision = self._pending_recovered_planner_decision(db, task)

        if existing_child_ids is not None:
            if existing_child_ids:
                await self._process_children_with_dependencies(task_id, existing_child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return
        if pending_worker_result is not None:
            await self._resume_pending_leaf_verification(task_id, pending_worker_result, runner, semaphore)
            return
        if root_gate_child_ids:
            await self._process_children_with_dependencies(task_id, root_gate_child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return
        if recovered_planner_decision is not None:
            await self._execute_leaf(task_id, runner, semaphore)
            return

        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            self._mark(db, task, TaskStatus.QUEUED, "规划器已排队，正在等待 Codex CLI 执行槽位；排队不消耗模型 token。")
            self._seed_planner_worktree_context(db, task)
            db.commit()

        try:
            async with semaphore:
                if not await self._reserve_agent_start(task_id, "planner"):
                    return
                if not self._try_mark_for_agent(
                    task_id,
                    TaskStatus.PLANNING,
                    "规划器正在基于项目宪法和 300% 铁律判断该节点是否为叶子。",
                ):
                    return
                planner_envelope = await runner.plan(self._snapshot_task(task_id))
                if self._is_task_stopped(task_id):
                    return
            decision = PlannerDecision.model_validate(planner_envelope.payload)
        except Exception as exc:
            if self._is_task_stopped(task_id):
                return
            with SessionLocal() as db:
                task = self._get_task(db, task_id)
                self._block_task(db, task, f"规划器失败：{exc}", "planner.exception")
                db.commit()
            return
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            self._record_run(db, task, RunKind.PLANNER, planner_envelope, "read-only")
            self._add_event(db, task.id, "planner.decision", decision.reason, decision.model_dump())
            db.commit()

            if self._is_false_missing_objective(decision, task):
                self._add_event(
                    db,
                    task.id,
                    "planner.corrected",
                    "规划器误判目标缺失，但该节点目标非空；系统已将该节点按可执行叶子处理。",
                    decision.model_dump(),
                )
                decision = PlannerDecision(
                    decision="execute",
                    reason="已修正规划器对非空目标的误判，该节点改为直接执行。",
                )
            if decision.decision == "blocked":
                # 分类走**工具侧构造器**（它保留 payload 里已有的报告，并对已知误判做重分类），
                # 而不是只看 payload —— 之前只有验收路径这么做，两条路径口径不一致。
                block_report = self._build_block_report(task, decision.reason, "planner.blocked", planner_envelope.payload)
                suppression = blockers.blocked_recovery_suppression(
                    block_report,
                    scope=loads(task.scope_json, []),
                    has_description=bool(task.description.strip()),
                )
                if suppression:
                    self._suppress_blocked_recovery(task, "planner_blocked_recovery_suppressed", suppression)
                    self._block_task(
                        db,
                        task,
                        decision.reason,
                        "planner.blocked",
                        {
                            **planner_envelope.payload,
                            "block_report": block_report,
                        },
                    )
                    db.commit()
                    return
                child_ids = []
                if task.description.strip():
                    signature = self._planner_blocked_recovery_signature(task, decision.reason)
                    meta = loads(task.node_meta_json, {})
                    attempted = {str(item) for item in meta.get("planner_blocked_recovery_signatures", []) if item}
                    if signature in attempted:
                        meta["planner_blocked_recovery_repeated"] = {
                            "signature": signature,
                            "reason": decision.reason,
                            "repeated_at": utcnow().isoformat(),
                        }
                        task.node_meta_json = dumps(meta)
                        self._block_task(
                            db,
                            task,
                            decision.reason,
                            "planner.blocked",
                            {
                                **planner_envelope.payload,
                                "planner_blocked_recovery_signature": signature,
                                "planner_blocked_recovery_attempted": True,
                            },
                        )
                        db.commit()
                        return
                    meta["planner_blocked_recovery_signatures"] = sorted(attempted | {signature})
                    meta["planner_blocked_recovery_reason"] = decision.reason
                    meta["planner_blocked_recovery_updated_at"] = utcnow().isoformat()
                    task.node_meta_json = dumps(meta)
                    child_ids = self._create_children(
                        db,
                        task,
                        self._planner_blocked_recovery_children(task, decision.reason, planner_envelope.payload, signature),
                    )
                    if child_ids:
                        self._add_event(
                            db,
                            task.id,
                            "planner.blocked_converted",
                            "规划器返回 blocked，但该节点目标明确；系统已转为递归恢复子任务，避免直接死停。",
                            {
                                "reason": decision.reason,
                                "signature": signature,
                                "child_ids": child_ids,
                                "planner": planner_envelope.payload,
                            },
                        )
                        self._mark(db, task, TaskStatus.WAITING_CHILDREN, "规划阻塞已转为递归恢复子任务；父节点等待恢复结果。")
                    db.commit()
                    if not child_ids:
                        return
                else:
                    self._block_task(db, task, decision.reason, "planner.blocked", planner_envelope.payload)
                    db.commit()
                    return
            elif decision.decision == "repair":
                repair_specs = decision.children or self._fallback_decomposition(task, decision.reason)
                child_ids = self._create_children(db, task, repair_specs)
                if child_ids:
                    self._add_event(
                        db,
                        task.id,
                        "planner.repair_converted",
                        "规划器返回 repair，系统已转为递归修复子任务，避免停在人工待修复状态。",
                        {"reason": decision.reason, "child_ids": child_ids, "planner": planner_envelope.payload},
                    )
                    self._mark(db, task, TaskStatus.WAITING_CHILDREN, "规划器 repair 已转为递归修复子任务；父节点等待修复结果。")
                db.commit()
                if not child_ids:
                    return
            elif decision.decision == "decompose":
                child_ids = self._create_children(db, task, decision.children)
                if child_ids:
                    self._mark(db, task, TaskStatus.WAITING_CHILDREN, "子任务已激活；父节点等待子节点完成，不占用 Codex CLI。")
                db.commit()
                if not child_ids:
                    return
            else:
                child_ids = []

        if child_ids:
            await self._process_children_with_dependencies(task_id, child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return

        await self._execute_leaf(task_id, runner, semaphore)

    async def _execute_leaf(self, task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            self._assign_worktree(db, task)
            self._seed_worktree_from_parent(db, task)
            self._seed_worktree_from_dependencies(db, task)
            task.leaf_attempts += 1
            self._mark(db, task, TaskStatus.QUEUED, "叶子执行器已排队，正在等待 Codex CLI 执行槽位；排队不消耗模型 token。")
            db.commit()

        deferred_child_ids: list[str] = []
        should_accept_deferred_parent = False
        stop_after_slot = False
        phase = "worker"
        worker_result: WorkerResult | None = None
        verifier_envelope: RunnerEnvelope | None = None
        verifier_result: VerifierResult | None = None
        try:
            async with semaphore:
                if not await self._reserve_agent_start(task_id, "worker"):
                    return
                if not self._try_mark_for_agent(
                    task_id,
                    TaskStatus.WORKING,
                    "叶子执行器已启动；正在先自检是否满足 300% 可完成铁律。",
                ):
                    return
                worker_envelope = await runner.work(self._snapshot_task(task_id))
                if self._is_task_stopped(task_id):
                    return
                worker_result = WorkerResult.model_validate(worker_envelope.payload)
                with SessionLocal() as db:
                    task = self._get_task(db, task_id)
                    if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                        return
                    self._record_run(db, task, RunKind.WORKER, worker_envelope, "workspace-write")
                    if worker_result.status == "blocked":
                        if self._is_foundation_missing_block(task, worker_result.summary, worker_envelope.payload):
                            task.result_json = dumps({"worker": worker_result.model_dump()})
                            deferred_child_ids = self._convert_leaf_to_parent(
                                db,
                                task,
                                worker_result.suggested_children or self._foundation_missing_children(task, worker_result.summary),
                                "worker.foundation_missing",
                                worker_result.summary,
                                {"worker": worker_result.model_dump(), "auto_converted_from_blocked": True},
                            )
                            should_accept_deferred_parent = True
                            stop_after_slot = True
                        else:
                            self._block_task(db, task, worker_result.summary, "worker.blocked", worker_envelope.payload)
                            stop_after_slot = True
                    elif worker_result.status == "needs_decomposition":
                        task.result_json = dumps({"worker": worker_result.model_dump()})
                        if self._should_retry_empty_worker_decomposition(task, worker_result):
                            self._queue_empty_worker_decomposition_retry(db, task, worker_result, source="worker.needs_decomposition")
                        else:
                            deferred_child_ids = self._convert_leaf_to_parent(
                                db,
                                task,
                                worker_result.suggested_children,
                                "worker.needs_decomposition",
                                worker_result.summary,
                                {"worker": worker_result.model_dump()},
                            )
                            should_accept_deferred_parent = True
                        stop_after_slot = True
                    elif worker_result.status == "failed":
                        task.result_json = dumps({"worker": worker_result.model_dump()})
                        if self._should_retry_empty_worker_decomposition(task, worker_result):
                            self._queue_empty_worker_decomposition_retry(db, task, worker_result, source="worker.failed_converted")
                        else:
                            deferred_child_ids = self._convert_leaf_to_parent(
                                db,
                                task,
                                worker_result.suggested_children,
                                "worker.failed_converted",
                                worker_result.summary,
                                {"worker": worker_result.model_dump(), "auto_converted_from_worker_failed": True},
                            )
                            should_accept_deferred_parent = True
                        stop_after_slot = True
                    elif not worker_result.changed_files and not worker_result.tests and not worker_result.evidence:
                        self._mark(db, task, TaskStatus.FAILED, "执行器报告成功，但没有提供变更文件、测试或证据。")
                        stop_after_slot = True
                    else:
                        changed_file_issues = self._worker_changed_file_issues(task, worker_result, worker_envelope)
                        if changed_file_issues:
                            reason = self._worker_changed_file_issue_reason(changed_file_issues)
                            task.result_json = dumps(
                                {
                                    "worker": worker_result.model_dump(),
                                    "changed_file_issues": changed_file_issues,
                                }
                            )
                            deferred_child_ids = self._convert_leaf_to_parent(
                                db,
                                task,
                                self._changed_file_repair_children(task, changed_file_issues),
                                "worker.changed_files_missing",
                                reason,
                                {
                                    "worker": worker_result.model_dump(),
                                    "changed_file_issues": changed_file_issues,
                                    "auto_converted_from_missing_changed_files": True,
                                },
                            )
                            should_accept_deferred_parent = True
                            stop_after_slot = True
                        else:
                            repaired_files = self._repair_changed_text_files(task, worker_result)
                            if repaired_files:
                                self._add_event(
                                    db,
                                    task.id,
                                    "worker.text_encoding_repaired",
                                    "已在进入验收前自动修复 worker 产物中的 UTF-8 中文乱码。",
                                    {"files": repaired_files},
                                )
                            text_file_issues = self._worker_text_file_issues(task, worker_result)
                            if text_file_issues:
                                reason = self._worker_text_file_issue_reason(text_file_issues)
                                task.result_json = dumps(
                                    {
                                        "worker": worker_result.model_dump(),
                                        "text_file_issues": text_file_issues,
                                    }
                                )
                                deferred_child_ids = self._convert_leaf_to_parent(
                                    db,
                                    task,
                                    self._text_file_repair_children(task, text_file_issues),
                                    "worker.text_encoding_corrupt",
                                    reason,
                                    {
                                        "worker": worker_result.model_dump(),
                                        "text_file_issues": text_file_issues,
                                        "auto_converted_from_text_corruption": True,
                                    },
                                )
                                should_accept_deferred_parent = True
                                stop_after_slot = True
                            else:
                                task.progress_current = 1
                                task.progress_total = 2
                                task.result_json = dumps({"worker": worker_result.model_dump()})
                                self._mark(db, task, TaskStatus.VERIFYING, "叶子执行完成；验收器复用当前 Codex CLI 槽位立即启动。")
                    db.commit()
                if not stop_after_slot:
                    phase = "verifier"
                    if not await self._reserve_agent_start(task_id, "verifier"):
                        return
                    verifier_envelope = await runner.verify(self._snapshot_task(task_id), worker_result)
                    if self._is_task_stopped(task_id):
                        return
                    verifier_result = VerifierResult.model_validate(verifier_envelope.payload)
        except Exception as exc:
            if self._is_task_stopped(task_id):
                return
            failed_phase = "验收器" if phase == "verifier" else "执行器"
            failed_source = "verifier.exception" if phase == "verifier" else "worker.exception"
            with SessionLocal() as db:
                task = self._get_task(db, task_id)
                self._block_task(db, task, f"{failed_phase}失败：{exc}", failed_source)
                db.commit()
            return
        if deferred_child_ids:
            await self._process_children_with_dependencies(task_id, deferred_child_ids, runner, semaphore)
            if should_accept_deferred_parent and not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return
        if worker_result is None or verifier_envelope is None or verifier_result is None:
            return
        child_ids = self._record_verifier_result(task_id, worker_result, verifier_envelope, verifier_result)
        if child_ids:
            await self._process_children_with_dependencies(task_id, child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)

    async def _resume_pending_leaf_verification(
        self,
        task_id: str,
        worker_result: WorkerResult,
        runner: BaseCodexRunner,
        semaphore: asyncio.Semaphore,
    ) -> None:
        deferred_child_ids: list[str] = []
        try:
            async with semaphore:
                if not self._try_mark_for_agent(task_id, TaskStatus.VERIFYING, "恢复已有 worker 结果；验收器已启动。"):
                    return
                with SessionLocal() as db:
                    task = self._get_task(db, task_id)
                    repaired_files = self._repair_changed_text_files(task, worker_result)
                    if repaired_files:
                        self._add_event(
                            db,
                            task.id,
                            "worker.text_encoding_repaired",
                            "已在恢复验收前自动修复 worker 产物中的 UTF-8 中文乱码。",
                            {"files": repaired_files},
                        )
                    text_file_issues = self._worker_text_file_issues(task, worker_result)
                    if text_file_issues:
                        reason = self._worker_text_file_issue_reason(text_file_issues)
                        task.result_json = dumps(
                            {
                                "worker": worker_result.model_dump(),
                                "text_file_issues": text_file_issues,
                            }
                        )
                        deferred_child_ids = self._convert_leaf_to_parent(
                            db,
                            task,
                            self._text_file_repair_children(task, text_file_issues),
                            "worker.text_encoding_corrupt",
                            reason,
                            {
                                "worker": worker_result.model_dump(),
                                "text_file_issues": text_file_issues,
                                "auto_converted_from_text_corruption": True,
                            },
                        )
                    db.commit()
                if not deferred_child_ids:
                    if not await self._reserve_agent_start(task_id, "verifier"):
                        return
                    verifier_envelope = await runner.verify(self._snapshot_task(task_id), worker_result)
                    if self._is_task_stopped(task_id):
                        return
                    verifier_result = VerifierResult.model_validate(verifier_envelope.payload)
        except Exception as exc:
            if self._is_task_stopped(task_id):
                return
            with SessionLocal() as db:
                task = self._get_task(db, task_id)
                self._block_task(db, task, f"验收器失败：{exc}", "verifier.exception")
                db.commit()
            return
        if deferred_child_ids:
            await self._process_children_with_dependencies(task_id, deferred_child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return
        child_ids = self._record_verifier_result(task_id, worker_result, verifier_envelope, verifier_result)
        if child_ids:
            await self._process_children_with_dependencies(task_id, child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)

    def _revalidation_worker_result(self, task: TaskNode) -> WorkerResult | None:
        """再验证用的 worker 产物：**不要求**它还没有 verifier 结论。

        这一点与 `_pending_worker_result` 的差别是必须的：那条路走的是"还没验过的产物"，
        而再验证打交道的恰恰是**已经验过一次、且产出了结论**的节点。用前者的判据，刚跑完
        被入队的节点会一条都匹配不上——登记写了、队列空了，接线看着在、实际是死的。
        """
        result = repair_mojibake(loads(task.result_json, {}))
        worker_payload = result.get("worker") if isinstance(result, dict) else None
        if not isinstance(worker_payload, dict):
            return None
        try:
            worker_result = WorkerResult.model_validate(worker_payload)
        except Exception:
            return None
        return worker_result if worker_result.status == "succeeded" else None

    def _pending_worker_result(self, task: TaskNode) -> WorkerResult | None:
        result = repair_mojibake(loads(task.result_json, {}))
        if not isinstance(result, dict) or "verifier" in result:
            return None
        worker_payload = result.get("worker")
        if not isinstance(worker_payload, dict):
            return None
        try:
            worker_result = WorkerResult.model_validate(worker_payload)
        except Exception:
            return None
        return worker_result if worker_result.status == "succeeded" else None

    def _pending_recovered_planner_decision(self, db: Session, task: TaskNode) -> PlannerDecision | None:
        if task.status != TaskStatus.QUEUED.value or task.children:
            return None
        if self._pending_worker_result(task) is not None:
            return None
        meta = loads(task.node_meta_json, {})
        if not isinstance(meta, dict) or meta.get("recovered_planner_decision") != "execute":
            return None
        expected_sha = str(meta.get("recovered_planner_last_message_sha") or "")
        if not expected_sha:
            return None
        latest = db.scalars(
            select(RunRecord)
            .where(RunRecord.task_id == task.id, RunRecord.kind == RunKind.PLANNER.value, RunRecord.status == RunStatus.SUCCEEDED.value)
            .order_by(RunRecord.id.desc())
        ).first()
        if latest is None or not latest.last_message_path:
            return None
        try:
            raw = Path(latest.last_message_path).read_bytes()
            if hashlib.sha1(raw).hexdigest() != expected_sha:
                return None
            payload = json.loads(raw.decode("utf-8"))
            decision = PlannerDecision.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None
        return decision if decision.decision == "execute" else None

    def _proactive_blocked_child_repair_children(self, db: Session, task: TaskNode, children: list[TaskNode]) -> list[str]:
        siblings_by_id = {child.id: child for child in children}
        blocking_children = [
            child
            for child in children
            if child.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}
            and not self._superseded_by_done_repair(child, siblings_by_id)
        ]
        if not blocking_children:
            return []
        blocked_reports = [self._child_block_report(child) for child in blocking_children]
        reports_by_child_id = {str(report.get("task_id") or ""): report for report in blocked_reports}
        self._store_child_block_reports(db, task, blocked_reports)
        blockers_needing_repair = [
            child
            for child in blocking_children
            if not self._is_human_required_blocker(reports_by_child_id.get(child.id, {}))
            and not bool(reports_by_child_id.get(child.id, {}).get("retryable"))
            if not self._has_active_block_repair(child, siblings_by_id)
        ]
        if not blockers_needing_repair:
            return []
        repair_child_ids = self._create_children(db, task, self._blocked_child_repair_children(task, blockers_needing_repair))
        if not repair_child_ids:
            return []
        self._attach_repair_children_to_blockers(blockers_needing_repair, repair_child_ids)
        self._add_event(
            db,
            task.id,
            "parent.blocked_child_repair_preempted",
            "父节点提前将失败/阻塞子节点转为递归修复/替代子任务，避免被低优先级队列挡住。",
            {"blocked_child_ids": [child.id for child in blockers_needing_repair], "repair_child_ids": repair_child_ids},
        )
        self._mark(db, task, TaskStatus.WAITING_CHILDREN, "失败/阻塞子节点已提前转为递归修复/替代子任务；父节点等待修复结果。")
        return repair_child_ids

    def _pre_planner_root_final_gate_children(self, db: Session, task: TaskNode) -> list[str]:
        if task.parent_id is not None or task.children:
            return []
        final_gate = self._final_root_product_gate(db, task)
        if not final_gate.get("required") or final_gate.get("passed"):
            return []
        meta = loads(task.node_meta_json, {})
        meta["pre_planner_final_gate"] = final_gate
        meta["pre_planner_final_gate_checked_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)
        child_ids = self._create_final_gate_repair_children(db, task, final_gate)
        if not child_ids:
            return []
        task.result_json = dumps({"final_gate": final_gate})
        self._add_event(
            db,
            task.id,
            "final_gate.pre_planner_repair_required",
            "根节点没有可继续子任务，但最终成品门禁未通过；系统先创建递归修复子任务，避免从空白状态重复规划。",
            {"child_ids": child_ids, "final_gate": final_gate},
        )
        self._mark(db, task, TaskStatus.WAITING_CHILDREN, "根节点最终成品门禁未通过；父节点等待递归修复子任务。")
        return child_ids

    def _proactive_root_final_gate_children(self, db: Session, task: TaskNode, children: list[TaskNode]) -> list[str]:
        if task.parent_id is not None:
            return []
        final_gate = self._final_root_product_gate(db, task)
        if not final_gate.get("required") or final_gate.get("passed"):
            return []
        missing = [str(item) for item in final_gate.get("missing", []) if item]
        if not missing:
            return []
        missing_signature = hashlib.sha1("\n".join(sorted(missing)).encode("utf-8")).hexdigest()[:16]
        for child in children:
            meta = loads(child.node_meta_json, {})
            if (
                meta.get("proactive_final_gate_missing_signature") == missing_signature
                and child.status not in TERMINAL_STATUSES
                and child.status not in STOPPED_STATUSES
            ):
                return []
        child_specs = self._final_gate_repair_children(task, final_gate)
        child_ids = self._create_final_gate_repair_children(db, task, final_gate, child_specs)
        if not child_ids:
            return []
        for child_id in child_ids:
            child = db.get(TaskNode, child_id)
            if child is None:
                continue
            meta = loads(child.node_meta_json, {})
            meta["proactive_final_gate"] = True
            meta["proactive_final_gate_missing_signature"] = missing_signature
            meta["proactive_final_gate_checked_at"] = utcnow().isoformat()
            child.node_meta_json = dumps(meta)
        meta = loads(task.node_meta_json, {})
        meta["proactive_final_gate"] = {
            "missing_signature": missing_signature,
            "missing": missing,
            "child_ids": child_ids,
            "checked_at": utcnow().isoformat(),
        }
        task.node_meta_json = dumps(meta)
        task.result_json = dumps({"final_gate": final_gate})
        self._add_event(
            db,
            task.id,
            "final_gate.proactive_repair_required",
            "根节点最终成品门禁仍有硬缺口；系统已提前创建递归修复子任务，避免等待整棵旧子树验收后才补最终成品内容。",
            {"child_ids": child_ids, "final_gate": final_gate, "missing_signature": missing_signature},
        )
        self._mark(db, task, TaskStatus.WAITING_CHILDREN, "根节点最终成品门禁未通过；已提前补充递归修复子任务。")
        return child_ids

    def _pre_acceptance_gate_children(self, db: Session, task: TaskNode, child_payloads: list[dict]) -> list[str]:
        integration_gate = self._parent_godot_integration_gate(db, task)
        final_gate = self._final_root_product_gate(db, task)
        is_final_gate_repair = False
        if integration_gate.get("required") and not integration_gate.get("passed"):
            child_specs = self._integration_gate_repair_children(task, integration_gate)
            event_type = "integration_gate.pre_acceptance_repair_required"
            message = "父节点 Godot 集成门禁在 acceptance 前已确定失败；系统先创建递归修复子任务，节省一次整合验收调用。"
        elif final_gate.get("required") and not final_gate.get("passed"):
            is_final_gate_repair = True
            child_specs = self._final_gate_repair_children(task, final_gate)
            event_type = "final_gate.pre_acceptance_repair_required"
            message = "根节点最终成品门禁在 acceptance 前已确定失败；系统先创建递归修复子任务，节省一次整合验收调用。"
        else:
            return []

        if is_final_gate_repair:
            child_ids = self._create_final_gate_repair_children(db, task, final_gate, child_specs)
        else:
            child_ids = self._create_children(db, task, child_specs)
        if not child_ids:
            return []
        task.result_json = dumps(
            {
                "accepted_children": [str(child.get("id")) for child in child_payloads],
                "integration_gate": integration_gate,
                "final_gate": final_gate,
                "pre_acceptance_gate": True,
            }
        )
        self._add_event(
            db,
            task.id,
            event_type,
            message,
            {"child_ids": child_ids, "integration_gate": integration_gate, "final_gate": final_gate},
        )
        self._mark(db, task, TaskStatus.WAITING_CHILDREN, "确定性门禁未通过；父节点等待递归修复子任务。")
        return child_ids

    def _record_verifier_result(
        self,
        task_id: str,
        worker_result: WorkerResult,
        verifier_envelope: RunnerEnvelope,
        verifier_result: VerifierResult,
    ) -> list[str]:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            child_ids = self._apply_verifier_result(db, task, worker_result, verifier_envelope, verifier_result)
            db.commit()
        return child_ids

    def _apply_verifier_result(
        self,
        db: Session,
        task: TaskNode,
        worker_result: WorkerResult,
        verifier_envelope: RunnerEnvelope,
        verifier_result: VerifierResult,
    ) -> list[str]:
        child_ids: list[str] = []
        if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
            return []
        if self._existing_recovered_run(db, task, RunKind.VERIFIER, Path(verifier_envelope.last_message_path or "")) is None:
            self._record_run(db, task, RunKind.VERIFIER, verifier_envelope, "workspace-write")
        task.result_json = dumps({"worker": worker_result.model_dump(), "verifier": verifier_result.model_dump()})
        task.progress_current = 2
        task.progress_total = 2
        if verifier_result.verdict == "pass":
            self._stamp_constitution_version_on_completion(db, task)
            self._mark(db, task, TaskStatus.DONE, verifier_result.reason)
            self._enqueue_revalidate_after_run(db, task)
            return []
        elif verifier_result.verdict == "repair":
            task.verification_failures += 1
            child_ids = self._convert_leaf_to_parent(
                db,
                task,
                verifier_result.suggested_children,
                "task.leaf_misclassified",
                verifier_result.reason,
                {"worker": worker_result.model_dump(), "verifier": verifier_result.model_dump()},
            )
        elif verifier_result.verdict == "blocked":
            self._block_task(db, task, verifier_result.reason, "verifier.blocked", verifier_envelope.payload)
            child_ids = []
        elif verifier_result.verdict == "visual_uncertain":
            task.verification_failures += 1
            if verifier_result.suggested_children:
                child_ids = self._convert_leaf_to_parent(
                    db,
                    task,
                    verifier_result.suggested_children,
                    "verifier.visual_uncertain_recursive_evidence",
                    verifier_result.reason,
                    {
                        "worker": worker_result.model_dump(),
                        "verifier": verifier_result.model_dump(),
                        "visual_uncertain": True,
                    },
                )
            else:
                self._block_task(
                    db,
                    task,
                    verifier_result.reason,
                    "verifier.visual_uncertain",
                    self._visual_uncertain_payload(verifier_envelope.payload, "verifier"),
                )
                child_ids = []
        else:
            task.verification_failures += 1
            child_ids = self._convert_leaf_to_parent(
                db,
                task,
                verifier_result.suggested_children,
                "task.leaf_misclassified",
                verifier_result.reason,
                {"worker": worker_result.model_dump(), "verifier": verifier_result.model_dump()},
            )
        return child_ids

    def _create_final_gate_repair_children(
        self,
        db: Session,
        task: TaskNode,
        final_gate: dict[str, Any],
        children: list[ChildTaskSpec] | None = None,
    ) -> list[str]:
        child_specs = list(children if children is not None else self._final_gate_repair_children(task, final_gate))
        if not child_specs:
            return []
        active_by_key = self._active_final_gate_repair_keys(db, task)
        deduped: list[dict[str, Any]] = []
        filtered: list[ChildTaskSpec] = []
        for child in child_specs:
            signature = self._child_signature(child)
            keys = [f"signature:{signature}"]
            category_key = self._final_gate_repair_key_for_spec(child)
            if category_key:
                keys.append(f"category:{category_key}")
            existing_child_ids = sorted({child_id for key in keys for child_id in active_by_key.get(key, [])})
            if existing_child_ids:
                deduped.append(
                    {
                        "signature": signature,
                        "category_key": category_key,
                        "title": child.title,
                        "existing_child_ids": existing_child_ids,
                    }
                )
                continue
            filtered.append(child)
        if deduped:
            self._add_event(
                db,
                task.id,
                "final_gate.repair_deduplicated",
                "已复用同签名的最终门禁修复分支，避免重复消耗 Codex CLI 槽位。",
                {
                    "deduped": deduped,
                    "final_gate_missing": final_gate.get("missing", []),
                },
            )
        if not filtered:
            return []
        return self._create_children(db, task, filtered)

    def _active_final_gate_repair_keys(self, db: Session, parent: TaskNode) -> dict[str, list[str]]:
        active: dict[str, list[str]] = {}
        db.flush()
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all())
        for child in children:
            if child.status in TERMINAL_STATUSES:
                continue
            meta = loads(child.node_meta_json, {})
            coverage_ids = {str(item) for item in (meta.get("coverage_ids") or [])}
            if (
                "final-product-gate" not in coverage_ids
                and meta.get("proactive_final_gate") is not True
                and not bool(meta.get("final_product_gate_lineage"))
            ):
                continue
            signature = str(meta.get("split_signature") or self._task_signature(child))
            active.setdefault(f"signature:{signature}", []).append(child.id)
            category_key = self._final_gate_repair_key_from_parts(coverage_ids, str(meta.get("verification_kind") or ""), child.title)
            if category_key:
                active.setdefault(f"category:{category_key}", []).append(child.id)
        return active

    def _final_gate_repair_key_for_spec(self, child: ChildTaskSpec) -> str | None:
        return self._final_gate_repair_key_from_parts(set(str(item) for item in child.coverage_ids), child.verification_kind, child.title)

    def _final_gate_repair_key_from_parts(self, coverage_ids: set[str], verification_kind: str, title: str) -> str | None:
        if "final-product-gate" not in coverage_ids:
            return None
        category_ids = sorted(item for item in coverage_ids if item != "final-product-gate")
        if category_ids:
            return "|".join(category_ids)
        normalized_verification = self._normalize_signature_text(verification_kind)
        if normalized_verification:
            return normalized_verification
        return self._normalize_signature_text(title)

    def _settle_passed_root_final_gate_branches(self, db: Session, task: TaskNode) -> set[str]:
        if task.parent_id is not None:
            return set()
        final_gate = self._final_root_product_gate(db, task)
        if not final_gate.get("required") or not final_gate.get("passed"):
            return set()

        checked_at = utcnow().isoformat()
        meta = loads(task.node_meta_json, {})
        if not isinstance(meta, dict):
            meta = {}
        meta["passed_root_final_gate"] = {
            "target": final_gate.get("target"),
            "main_scene": final_gate.get("main_scene"),
            "checked_at": checked_at,
            "check_count": len(final_gate.get("checks", [])),
        }
        task.node_meta_json = dumps(meta)

        result = loads(task.result_json, {})
        if not isinstance(result, dict):
            result = {}
        result["final_gate"] = final_gate
        task.result_json = dumps(result)

        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        subtree = self._subtree(all_tasks, task.id)
        parent_by_id = {node.id: node.parent_id for node in subtree}
        final_gate_branch_ids = {
            node.id
            for node in subtree
            if node.id != task.id and self._is_final_product_gate_task(node)
        }

        def belongs_to_final_gate_branch(node: TaskNode) -> bool:
            cursor: str | None = node.id
            while cursor:
                if cursor in final_gate_branch_ids:
                    return True
                cursor = parent_by_id.get(cursor)
            return False

        stale_branches = [
            node
            for node in subtree
            if node.id != task.id
            and node.status != TaskStatus.DONE.value
            and belongs_to_final_gate_branch(node)
        ]
        if not stale_branches:
            return set()

        settled_ids: set[str] = set()
        for node in stale_branches:
            node_meta = loads(node.node_meta_json, {})
            if not isinstance(node_meta, dict):
                node_meta = {}
            node_meta["superseded_by_passed_root_final_gate"] = task.id
            node_meta["superseded_by_passed_root_final_gate_at"] = checked_at
            node_meta["superseded_final_gate_target"] = final_gate.get("target")
            node.node_meta_json = dumps(node_meta)
            node.result_json = dumps(
                {
                    "system": {
                        "status": "superseded_by_passed_root_final_gate",
                        "root_id": task.id,
                        "target": final_gate.get("target"),
                        "main_scene": final_gate.get("main_scene"),
                        "reason": "The authoritative root final product gate already passes, so this stale final-gate repair branch no longer needs Codex CLI work.",
                    }
                }
            )
            self._mark(
                db,
                node,
                TaskStatus.DONE,
                "Root final product gate already passes; stale final-gate repair branch is superseded.",
            )
            settled_ids.add(node.id)

        self._add_event(
            db,
            task.id,
            "final_gate.passed_branches_settled",
            "Root final product gate passed; stale final-product repair branches were settled so the tree can converge.",
            {
                "settled_ids": sorted(settled_ids),
                "target": final_gate.get("target"),
                "main_scene": final_gate.get("main_scene"),
                "check_count": len(final_gate.get("checks", [])),
            },
        )
        return settled_ids

    def _settle_redundant_final_gate_sibling_branches(
        self,
        db: Session,
        parent: TaskNode,
        children: list[TaskNode],
    ) -> set[str]:
        if parent.parent_id is not None:
            return set()
        groups: dict[str, list[TaskNode]] = {}
        for child in children:
            key = self._final_gate_branch_duplicate_key(child)
            if key:
                groups.setdefault(key, []).append(child)
        settled: set[str] = set()
        for key, siblings in groups.items():
            if len(siblings) < 2:
                continue
            infos = [
                {
                    "task": child,
                    "counts": self._subtree_status_counts(db, child.id),
                }
                for child in siblings
            ]
            canonical = max(
                infos,
                key=lambda item: (
                    self._active_count_from_counts(item["counts"]),
                    int(item["counts"].get(TaskStatus.DONE.value, 0)),
                    (item["task"].updated_at.timestamp() if item["task"].updated_at else 0.0),
                    item["task"].id,
                ),
            )
            canonical_task = canonical["task"]
            for item in infos:
                task = item["task"]
                if task.id == canonical_task.id:
                    continue
                if self._active_count_from_counts(item["counts"]) > 0:
                    continue
                settled.update(
                    self._supersede_redundant_final_gate_subtree(
                        db,
                        task,
                        canonical_task.id,
                        key,
                        item["counts"],
                    )
                )
        return settled

    def _final_gate_branch_duplicate_key(self, task: TaskNode) -> str | None:
        meta = loads(task.node_meta_json, {})
        if not isinstance(meta, dict):
            return None
        coverage_ids = {str(item) for item in meta.get("coverage_ids", [])}
        if (
            "final-product-gate" not in coverage_ids
            and meta.get("proactive_final_gate") is not True
            and not bool(meta.get("final_product_gate_lineage"))
        ):
            return None
        signature = str(meta.get("split_signature") or "")
        if not signature:
            return None
        return f"signature:{signature}"

    def _subtree_status_counts(self, db: Session, root_id: str) -> Counter[str]:
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        return Counter(task.status for task in self._subtree(all_tasks, root_id))

    def _active_count_from_counts(self, counts: Counter[str] | dict[str, int]) -> int:
        return sum(int(counts.get(status, 0)) for status in ACTIVE_AGENT_STATUSES)

    def _supersede_redundant_final_gate_subtree(
        self,
        db: Session,
        task: TaskNode,
        canonical_id: str,
        duplicate_key: str,
        counts: Counter[str],
    ) -> set[str]:
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        subtree = self._subtree(all_tasks, task.id)
        if any(node.status in ACTIVE_AGENT_STATUSES for node in subtree):
            return set()
        settled_ids: set[str] = set()
        now = utcnow().isoformat()
        for node in subtree:
            meta = loads(node.node_meta_json, {})
            if not isinstance(meta, dict):
                meta = {}
            meta["redundant_final_gate_superseded_by"] = canonical_id
            meta["redundant_final_gate_duplicate_key"] = duplicate_key
            meta["redundant_final_gate_superseded_at"] = now
            node.node_meta_json = dumps(meta)
            node.result_json = dumps(
                {
                    "system": {
                        "status": "superseded_by_final_gate_sibling",
                        "canonical_task_id": canonical_id,
                        "duplicate_key": duplicate_key,
                        "reason": "Equivalent final-product gate branch is already represented by the canonical sibling.",
                    }
                }
            )
            if node.status != TaskStatus.DONE.value:
                self._mark(
                    db,
                    node,
                    TaskStatus.DONE,
                    "重复的最终成品门禁分支已由同类主分支覆盖，跳过执行以避免重复消耗 Codex 槽位。",
                )
            settled_ids.add(node.id)
        self._add_event(
            db,
            task.parent_id or task.id,
            "final_gate.redundant_branch_pruned",
            "调度器已清理同 split_signature 的重复最终门禁分支；保留主分支继续验收，重复分支不再启动 Codex CLI。",
            {
                "root_child_id": task.id,
                "canonical_task_id": canonical_id,
                "duplicate_key": duplicate_key,
                "settled_ids": sorted(settled_ids),
                "status_counts": dict(counts),
            },
        )
        return settled_ids

    def _parent_acceptance_readiness_children(self, parent: TaskNode, children: list[TaskNode]) -> list[TaskNode]:
        if self._is_skill_note_task(parent):
            return children
        deliverable_children = [child for child in children if not self._is_skill_note_task(child)]
        return deliverable_children or children

    def _parent_acceptance_integration_children(self, parent: TaskNode, children: list[TaskNode]) -> list[TaskNode]:
        if self._is_skill_note_task(parent):
            return children
        deliverable_children = [child for child in children if not self._is_skill_note_task(child)]
        if not deliverable_children:
            return children
        return [
            child
            for child in children
            if not self._is_skill_note_task(child) or child.status == TaskStatus.DONE.value
        ]

    def _settle_nonblocking_skill_note_children(
        self,
        db: Session,
        parent: TaskNode,
        children: list[TaskNode],
        readiness_children: list[TaskNode],
    ) -> list[str]:
        readiness_ids = {child.id for child in readiness_children}
        if len(readiness_ids) == len(children):
            return []
        settled_ids: list[str] = []
        for child in children:
            if child.id in readiness_ids or not self._is_skill_note_task(child):
                continue
            settled_ids.extend(self._cancel_inactive_skill_note_subtree(db, parent, child))
        if settled_ids:
            self._add_event(
                db,
                parent.id,
                "parent.skill_note_nonblocking",
                "父节点已将未启动的 skill-note 辅助任务标记为非阻塞，由父层内置归拢替代，先整合真实交付子成果。",
                {"settled_skill_note_task_ids": settled_ids},
            )
        return settled_ids

    def _cancel_inactive_skill_note_subtree(self, db: Session, parent: TaskNode, child: TaskNode) -> list[str]:
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        subtree = self._subtree(all_tasks, child.id)
        if any(node.status in ACTIVE_AGENT_STATUSES for node in subtree):
            return []
        settled: list[str] = []
        for node in subtree:
            if node.status in TERMINAL_STATUSES or node.status in STOPPED_STATUSES:
                continue
            payload = loads(node.result_json, {})
            if not isinstance(payload, dict):
                payload = {}
            payload["system"] = {
                "status": "superseded_by_parent_nonblocking_skill_note",
                "parent_id": parent.id,
                "settled_at": utcnow().isoformat(),
            }
            node.result_json = dumps(payload)
            self._mark(
                db,
                node,
                TaskStatus.CANCELED,
                "Skill-note 辅助任务已由父层内置归拢替代，不再阻塞真实交付整合。",
            )
            settled.append(node.id)
        return settled

    async def _accept_parent(self, task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore) -> None:
        auto_hibernate_root_id: str | None = None
        auto_hibernate_reason: str | None = None
        blocked_repair_child_ids: list[str] = []
        pre_acceptance_gate_child_ids: list[str] = []
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            settled_passed_final_gate_ids = self._settle_passed_root_final_gate_branches(db, task)
            if settled_passed_final_gate_ids:
                db.flush()
            all_children = list(task.children)
            siblings_by_id = {child.id: child for child in all_children}
            children = [
                child
                for child in all_children
                if not self._superseded_by_done_repair(child, siblings_by_id)
            ]
            readiness_children = self._parent_acceptance_readiness_children(task, children)
            integration_children = self._parent_acceptance_integration_children(task, children)
            nonblocking_skill_note_ids = self._settle_nonblocking_skill_note_children(
                db,
                task,
                children,
                readiness_children,
            )
            if nonblocking_skill_note_ids:
                db.flush()
            # 版本债按"事实"结清一遍（节点已追平就不该再挂着），再刷新两类"事实"债务：
            # §7.3 的子节点阻塞/失败债（`blocked_child_reports` 由它派生）与 §7.2.3 的
            # 上浮事实。
            # 位置在**流程入口**而不是"子节点全 done 之后"：这个节点停止推进的大多数情形
            # （子节点阻塞/修复中/等待）都会在下面各自 return，放在后面等于大多数时候不跑，
            # 于是"整棵树卡在第几层"永远看不到。刷新本身是幂等的（按事实重算），多跑无害。
            self._settle_caught_up_version_obligations(db, task)
            self._refresh_child_status_obligations(db, task)
            self._refresh_upward_blocking_facts(db, task)
            task.progress_total = len(readiness_children)
            task.progress_current = sum(1 for child in readiness_children if child.status == TaskStatus.DONE.value)
            blocking_children = [
                child
                for child in readiness_children
                if child.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value}
                and not self._superseded_by_done_repair(child, siblings_by_id)
            ]
            if blocking_children:
                self._record_synthetic_run(db, task, RunKind.ACCEPTANCE, "一个或多个子节点阻塞或失败，未启动整合验收。")
                blocked_reports = [
                    self._child_block_report(child)
                    for child in blocking_children
                ]
                reports_by_child_id = {str(report.get("task_id") or ""): report for report in blocked_reports}
                self._store_child_block_reports(db, task, blocked_reports)
                self._add_event(
                    db,
                    task.id,
                    "parent.child_block_reports",
                    "父节点已收到子节点阻塞上报，正在判断是否可以等待后重试。",
                    {"reports": blocked_reports},
                )
                retryable_reports = [report for report in blocked_reports if report.get("retryable")]
                if blocked_reports and len(retryable_reports) == len(blocked_reports):
                    resource_retryable_reports = [report for report in retryable_reports if self._is_resource_pressure_block_report(report)]
                    self._mark(
                        db,
                        task,
                        TaskStatus.WAITING_CHILDREN,
                        "子节点因资源/流量类问题阻塞；父节点保留处理机会，可等待后重试这些子节点。",
                    )
                    if self.settings.auto_hibernate_on_resource_block and resource_retryable_reports:
                        auto_hibernate_root_id = self._root_id_for_task(db, task)
                        auto_hibernate_reason = self._resource_auto_hibernate_reason(resource_retryable_reports)
                else:
                    human_required_reports = [report for report in blocked_reports if self._is_human_required_blocker(report)]
                    if human_required_reports:
                        self._add_event(
                            db,
                            task.id,
                            "parent.human_action_required",
                            "父节点收集到需要人工登录、授权或权限处理的阻塞；系统会继续推进其他不依赖该卡点的任务。",
                            {"reports": human_required_reports},
                        )
                    blockers_needing_repair = [
                        child
                        for child in blocking_children
                        if not self._is_human_required_blocker(reports_by_child_id.get(child.id, {}))
                        and not bool(reports_by_child_id.get(child.id, {}).get("retryable"))
                        if not self._has_active_block_repair(child, siblings_by_id)
                    ]
                    if blockers_needing_repair:
                        repair_specs = self._blocked_child_repair_children(task, blockers_needing_repair)
                        blocked_repair_child_ids = self._create_children(db, task, repair_specs)
                        if blocked_repair_child_ids:
                            self._attach_repair_children_to_blockers(blockers_needing_repair, blocked_repair_child_ids)
                            self._add_event(
                                db,
                                task.id,
                                "parent.blocked_child_repair_scheduled",
                                "父节点已把不可自动重试的子节点阻塞转为递归修复/替代子任务。",
                                {"blocked_child_ids": [child.id for child in blockers_needing_repair], "repair_child_ids": blocked_repair_child_ids},
                            )
                            self._mark(
                                db,
                                task,
                                TaskStatus.WAITING_CHILDREN,
                                "子节点阻塞已转为递归修复/替代子任务；父节点等待修复结果。",
                            )
                        else:
                            self._block_task(
                                db,
                                task,
                                "存在不可自动重试的子节点阻塞或失败，但未能创建修复子任务。",
                                "parent.child_blocked",
                                {"child_reports": blocked_reports},
                            )
                    else:
                        self._mark(
                            db,
                            task,
                            TaskStatus.WAITING_CHILDREN,
                            "子节点阻塞需要人工处理或已有递归修复任务在处理中；父节点等待解除卡点或修复结果。",
                        )
                db.commit()
                if auto_hibernate_root_id and auto_hibernate_reason:
                    await self.hibernate_tree(
                        auto_hibernate_root_id,
                        self.settings.resource_block_resume_after_seconds,
                        auto_hibernate_reason,
                    )
                if blocked_repair_child_ids:
                    await self._process_children_with_dependencies(task_id, blocked_repair_child_ids, runner, semaphore)
                    if not self._is_task_stopped(task_id):
                        await self._accept_parent(task_id, runner, semaphore)
                return
            if any(child.status != TaskStatus.DONE.value for child in readiness_children):
                self._record_synthetic_run(db, task, RunKind.ACCEPTANCE, "一个或多个子节点尚未完成，父节点继续等待。")
                self._mark(db, task, TaskStatus.WAITING_CHILDREN, "仍有子节点未完成，父节点等待中。")
                db.commit()
                return
            self._integrate_child_outputs(db, task, integration_children)
            # R6：父层主导写入失败复盘（与 skill note 开关解耦；复盘产物里也会带上它）
            self._write_failure_retrospective(db, task)
            self._consolidate_parent_skill_notes(db, task)
            child_payloads = [self.serialize_task(child, False) for child in integration_children]
            pre_acceptance_gate_child_ids = self._pre_acceptance_gate_children(db, task, child_payloads)
            if pre_acceptance_gate_child_ids:
                db.commit()
            else:
                self._mark(db, task, TaskStatus.QUEUED, "父节点整合验收已排队，正在等待 Codex CLI 执行槽位。")
                db.commit()

        if pre_acceptance_gate_child_ids:
            await self._process_children_with_dependencies(task_id, pre_acceptance_gate_child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)
            return

        if self.settings.constitution_revalidate_enabled:
            # 修订后的再验证必须在**父节点自己验收之前**跑：否则父节点会拿"旧版下通过的产物"
            # 去声称完成，而那正是收紧修订要复查的东西（宪法 SPEC §6）。
            revalidated_child_ids = await self._revalidate_stale_leaves(task_id, runner, semaphore)
            if revalidated_child_ids:
                await self._process_children_with_dependencies(task_id, revalidated_child_ids, runner, semaphore)
                if not self._is_task_stopped(task_id):
                    await self._accept_parent(task_id, runner, semaphore)
                return

        try:
            async with semaphore:
                if not self._try_mark_for_agent(task_id, TaskStatus.ACCEPTING, "父节点正在运行 acceptance agent 做真实整合验收。"):
                    return
                if not await self._reserve_agent_start(task_id, "acceptance"):
                    return
                acceptance_envelope = await runner.accept(self._snapshot_task(task_id), child_payloads)
                if self._is_task_stopped(task_id):
                    return
            acceptance_result = AcceptanceResult.model_validate(acceptance_envelope.payload)
        except Exception as exc:
            if self._is_task_stopped(task_id):
                return
            with SessionLocal() as db:
                task = self._get_task(db, task_id)
                self._block_task(db, task, f"父节点整合验收失败：{exc}", "acceptance.exception")
                db.commit()
            return

        child_ids: list[str] = []
        final_gate_repair_gate: dict[str, Any] | None = None
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return
            self._record_run(db, task, RunKind.ACCEPTANCE, acceptance_envelope, "workspace-write")
            db.flush()
            if acceptance_result.verdict == "pass":
                integration_gate = self._parent_godot_integration_gate(db, task)
                final_gate = self._final_root_product_gate(db, task)
                task.result_json = dumps(
                    {
                        "accepted_children": [child["id"] for child in child_payloads],
                        "acceptance": acceptance_result.model_dump(),
                        "integration_gate": integration_gate,
                        "final_gate": final_gate,
                    }
                )
                if integration_gate["required"] and not integration_gate["passed"]:
                    acceptance_result = AcceptanceResult(
                        verdict="repair",
                        reason="父节点 Godot 集成门禁未通过，必须继续递归修复断链或入口问题。",
                        suggested_children=self._integration_gate_repair_children(task, integration_gate),
                        constitution_verdict="within_bounds",
                    )
                    self._add_event(
                        db,
                        task.id,
                        "integration_gate.repair_required",
                        "父节点 Godot 集成门禁未通过，已转为递归修复子任务。",
                        integration_gate,
                    )
                elif final_gate["required"] and not final_gate["passed"]:
                    final_gate_repair_gate = final_gate
                    acceptance_result = AcceptanceResult(
                        verdict="repair",
                        reason="根节点最终成品门禁未通过，必须继续递归修复。",
                        suggested_children=self._final_gate_repair_children(task, final_gate),
                        constitution_verdict="within_bounds",
                    )
                    self._add_event(
                        db,
                        task.id,
                        "final_gate.repair_required",
                        "根节点最终成品门禁未通过，已转为递归修复子任务。",
                        final_gate,
                    )
                else:
                    # 收尾前的最后一道判据：未清的集成冲突债务（`integration_conflict_strategy=repair` 档）。
                    # 放在"验收通过之后、标 done 之前"——判据 2（§6.1）就是替换这一行。
                    # R5 的跨版本回归债务必须先登记，再进这道门禁：登记晚了，这一轮就静默过了。
                    # B 部分的验收面篡改同理，而且它**更靠前**：先问"裁判有没有被动过"，再问"产物有没有回退"。
                    self._record_acceptance_surface_obligations(db, task)
                    self._record_requirement_obligations(db, task)
                    self._record_regression_obligations(db, task)
                    if self._obligation_gate(db, task):
                        return
                    self._stamp_constitution_version_on_completion(db, task)
                    # 门禁通过且版本戳已更新之后才重记基线——被拦下的那一轮不算"通过"。
                    self._refresh_regression_baseline(db, task)
                    self._mark(db, task, TaskStatus.DONE, acceptance_result.reason)
                    self._enqueue_revalidate_after_run(db, task)
                    db.commit()
                    return
            if acceptance_result.verdict == "blocked":
                task.result_json = dumps({"accepted_children": [child["id"] for child in child_payloads], "acceptance": acceptance_result.model_dump()})
                block_report = self._build_block_report(task, acceptance_result.reason, "acceptance.blocked", acceptance_envelope.payload)
                suppression = blockers.blocked_recovery_suppression(
                    block_report,
                    scope=loads(task.scope_json, []),
                    has_description=bool(task.description.strip()),
                )
                if suppression:
                    self._suppress_blocked_recovery(task, "acceptance_blocked_recovery_suppressed", suppression)
                    self._block_task(
                        db,
                        task,
                        acceptance_result.reason,
                        "acceptance.blocked",
                        {
                            **acceptance_envelope.payload,
                            "block_report": block_report,
                        },
                    )
                    db.commit()
                    return
                signature = self._acceptance_blocked_recovery_signature(task, acceptance_result.reason)
                meta = loads(task.node_meta_json, {})
                attempted = {str(item) for item in meta.get("acceptance_blocked_recovery_signatures", []) if item}
                if signature in attempted:
                    meta["acceptance_blocked_recovery_repeated"] = {
                        "signature": signature,
                        "reason": acceptance_result.reason,
                        "repeated_at": utcnow().isoformat(),
                    }
                    task.node_meta_json = dumps(meta)
                    self._block_task(
                        db,
                        task,
                        acceptance_result.reason,
                        "acceptance.blocked",
                        {
                            **acceptance_envelope.payload,
                            "acceptance_blocked_recovery_signature": signature,
                            "acceptance_blocked_recovery_attempted": True,
                        },
                    )
                    db.commit()
                    return
                meta["acceptance_blocked_recovery_signatures"] = sorted(attempted | {signature})
                meta["acceptance_blocked_recovery_reason"] = acceptance_result.reason
                meta["acceptance_blocked_recovery_updated_at"] = utcnow().isoformat()
                task.node_meta_json = dumps(meta)
                child_ids = self._create_children(
                    db,
                    task,
                    self._acceptance_blocked_recovery_children(task, acceptance_result.reason, acceptance_envelope.payload, signature),
                )
                if child_ids:
                    self._add_event(
                        db,
                        task.id,
                        "acceptance.blocked_converted",
                        "父节点整合验收返回 blocked，但阻塞不是资源类；系统已转为递归恢复子任务。",
                        {
                            "reason": acceptance_result.reason,
                            "signature": signature,
                            "child_ids": child_ids,
                            "acceptance": acceptance_envelope.payload,
                        },
                    )
                    self._mark(db, task, TaskStatus.WAITING_CHILDREN, "父节点整合阻塞已转为递归恢复子任务。")
                db.commit()
                if not child_ids:
                    return
            elif acceptance_result.verdict == "visual_uncertain":
                task.result_json = dumps({"accepted_children": [child["id"] for child in child_payloads], "acceptance": acceptance_result.model_dump()})
                if acceptance_result.suggested_children:
                    child_ids = self._create_children(db, task, acceptance_result.suggested_children)
                    self._add_event(
                        db,
                        task.id,
                        "acceptance.visual_uncertain_recursive_evidence",
                        "父节点视觉验收不确定，但仍有可递归补证路径；已创建视觉证据子任务。",
                        {"child_ids": child_ids, "acceptance": acceptance_result.model_dump()},
                    )
                    self._mark(db, task, TaskStatus.WAITING_CHILDREN, "视觉验收不确定，父节点等待递归补证子任务。")
                else:
                    self._block_task(
                        db,
                        task,
                        acceptance_result.reason,
                        "acceptance.visual_uncertain",
                        self._visual_uncertain_payload(acceptance_envelope.payload, "acceptance"),
                    )
                db.commit()
                if not child_ids:
                    return
            else:
                repair_cycle_limit = self._acceptance_repair_cycle_limit()
                repair_count = self._acceptance_repair_count(db, task.id)
                if repair_cycle_limit > 0 and repair_count >= repair_cycle_limit:
                    task.result_json = dumps({"accepted_children": [child["id"] for child in child_payloads], "acceptance": acceptance_result.model_dump()})
                    meta = loads(task.node_meta_json, {})
                    meta["manual_review_reason"] = "acceptance_repair_cycle_limit"
                    meta["acceptance_repair_cycles"] = repair_count
                    meta["acceptance_repair_cycle_limit"] = repair_cycle_limit
                    task.node_meta_json = dumps(meta)
                    self._add_event(
                        db,
                        task.id,
                        "task.manual_review_required",
                        "父节点连续整合验收 repair，系统已停止继续拆分，等待人工审查。",
                        {"acceptance_repair_cycles": repair_count, "limit": repair_cycle_limit},
                    )
                    self._mark(db, task, TaskStatus.REPAIR, "父节点连续整合验收 repair，等待人工审查后再继续。")
                    db.commit()
                    return
                if final_gate_repair_gate is not None:
                    child_ids = self._create_final_gate_repair_children(
                        db,
                        task,
                        final_gate_repair_gate,
                        acceptance_result.suggested_children,
                    )
                else:
                    child_ids = self._create_children(db, task, acceptance_result.suggested_children)
                task.result_json = dumps({"accepted_children": [child["id"] for child in child_payloads], "acceptance": acceptance_result.model_dump()})
                if child_ids:
                    self._mark(db, task, TaskStatus.WAITING_CHILDREN, "父节点整合验收要求新增修复子任务。")
                else:
                    self._mark(db, task, TaskStatus.REPAIR, "存在需要修复的子节点，父节点不能完成。")
                db.commit()
        if child_ids:
            await self._process_children_with_dependencies(task_id, child_ids, runner, semaphore)
            if not self._is_task_stopped(task_id):
                await self._accept_parent(task_id, runner, semaphore)

    def _inherited_constraints_for_parent(self, parent: TaskNode) -> list[str]:
        """父节点的 must/never + 它继承来的链（§7.1.3 的"逐层累积"）。"""
        parent_meta = loads(parent.node_meta_json, {})
        inherited = parent_meta.get("inherited_constraints")
        return constraints.inherit_down(
            loads(parent.acceptance_json, []),
            inherited if isinstance(inherited, list) else [],
        )

    def _refresh_constraint_coverage(self, db: Session, parent: TaskNode) -> list[str]:
        """核对"子节点集合是否逐字继承了下沉约束"（§7.1.4），返回仍然缺失的约束。

        判据是归一化后的**严格相等**（见 `api/app/constraints.py` 的说明）：改写就是侵蚀，
        所以"重述"算缺口。这是唯一一条在**创建时**就核对的债务——其余都是完成时。

        缺口记在父节点上，**不拦创建**：同一个 prompt 再跑一次只会得到同一个改写，
        拦住创建等于把树钉死在那一步。缺口的用处是"创建时就让它可见"，让 agent 有机会
        在下一次规划里补上，或让人裁定。
        """
        constitution_data = loads(parent.constitution_json, {})
        delegable = constraints.delegable_constraints(constitution_data)
        if not delegable:
            return []
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all())
        missing = constraints.coverage_gap(
            delegable, [loads(child.acceptance_json, []) for child in children]
        )
        meta = loads(parent.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        cleared = constraints.settle_coverage_cleared(existing, still_missing=missing)
        incoming = [
            fact
            for fact in [
                constraints.coverage_gap_debt(
                    parent.id,
                    missing,
                    depth=parent.depth,
                    blocks=self.settings.constraint_coverage_gap_blocks,
                )
            ]
            if fact
        ]
        updated = obligations.upsert(cleared, incoming)
        if updated != existing:
            meta["obligations"] = updated
            parent.node_meta_json = dumps(meta)
        return missing

    def _refresh_upward_blocking_facts(self, db: Session, task: TaskNode) -> int:
        """把"某一支还有未清阻断事项"这条事实写到本节点上（§7.2.3），返回写了几条。

        一次一层：本节点记的是**它自己的直接子节点**是否还有未清事项。再往上由祖父那层跑同样
        的函数自然串起来——所以链是逐层长出来的，不是一次复制一堆。这也让"整棵树卡在第几层"
        可以直接按层读。

        清债同样按事实：某个子节点已经没有未清阻断事项了，那条上浮事实就不再成立，清掉。
        """
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
        blocked_child_ids: set[str] = set()
        incoming: list[dict[str, Any]] = []
        for child in children:
            child_blocking = self._unsettled_blocking_obligations(child)
            if not child_blocking:
                continue
            blocked_child_ids.add(child.id)
            fact = obligations.upward_blocking_debt(
                task.id,
                child.id,
                blocking_total=len(child_blocking),
                by_type=obligations.summarize(child_blocking)["by_type"],
                depth=task.depth,
            )
            if fact is not None:
                incoming.append(fact)
        cleared = obligations.settle_upward_cleared(existing, still_blocked_child_ids=blocked_child_ids)
        updated = obligations.upsert(cleared, incoming)
        if updated != existing:
            meta["obligations"] = updated
            task.node_meta_json = dumps(meta)
        return len(incoming)

    def _record_parent_integration_obligations(self, parent: TaskNode, meta: dict[str, Any], report) -> None:
        """把 R4 识别到的冲突/跳过转成父节点的**轮次域债务**，并结清本轮不再复现的那些。

        用 `report.conflicts` / `report.skipped` 的**完整**明细，而不是 `meta["parent_integration"]`：
        后者按 `integration_conflict_detail_limit`（默认 20 条）截断过，拿它建债务会让第 21 条
        之后的冲突**静默地不进债务表**——那正好是 R4 要消灭的那种静默。
        """
        incoming = obligations.obligations_from_integration(
            parent.id,
            {"conflicts": report.conflicts, "skipped": report.skipped},
            strategy=report.strategy,
            depth=parent.depth,
        )
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        meta["obligations"] = obligations.upsert(obligations.settle_round_cleared(existing, incoming), incoming)

    def _regression_scope(self, db: Session, task: TaskNode) -> tuple[Path, list[str]]:
        """R5 的"产物根 + 相对路径"。两处来源的**基准不同**，混用会让基线指到不存在的文件、
        快照变空、门禁静默失效（"接线看着在、实际是死的"）：

        - 集成明细（`integrated_files`）：相对**集成目标**（父节点自己的工作树；没有就是根目标路径）；
        - 叶子自己的 worker 产物声明（`changed_files`）：相对**它自己的工作树**。
        """
        fallback_root = Path(task.worktree_path or self.settings.workspace_dir)
        meta = loads(task.node_meta_json, {})
        integration_meta = meta.get("parent_integration")
        if isinstance(integration_meta, dict):
            integrated = integration_meta.get("integrated_files")
            if isinstance(integrated, list) and integrated:
                root = self._integration_target_path(db, task)
                paths = [str(item) for item in integrated if str(item).strip()]
                if root is not None and paths:
                    return Path(root), paths
        result = loads(task.result_json or "{}", {})
        changed = result.get("changed_files") if isinstance(result, dict) else None
        if not isinstance(changed, list):
            # 落库的形状是 `{"worker": {...}}`（见 `_record_worker_result`）；两种都认，
            # 不认的话叶子节点的基线会一直是空的 —— 空基线不报错、只是静默不设防。
            worker = result.get("worker") if isinstance(result, dict) else None
            changed = worker.get("changed_files") if isinstance(worker, dict) else None
        if isinstance(changed, list) and changed:
            return fallback_root, [str(item) for item in changed if str(item).strip()]
        return fallback_root, []

    def _regression_baseline(self, task: TaskNode) -> dict[str, Any]:
        meta = loads(task.node_meta_json, {})
        baseline = meta.get(regression.BASELINE_KEY)
        return baseline if isinstance(baseline, dict) else {}

    def _write_failure_retrospective(self, db: Session, parent: TaskNode) -> dict[str, Any]:
        """R6：把**子树**里失败/阻塞节点的原始证据写成父层复盘（父层主导写入）。

        刻意与 skill note 的开关解耦：复盘是"哪里出过错"的事实记录，
        不该因为"这一轮没开 skill note"就不写。

        没有失败条目时**不写空壳**，并把旧的一并摘掉——空复盘会让看板多出一段没有信息量的东西，
        也让人分不清"确实没失败"和"根本没采集"。
        """
        children_by_parent: dict[str | None, list[TaskNode]] = {}
        for node in db.scalars(select(TaskNode)).all():
            children_by_parent.setdefault(node.parent_id, []).append(node)
        descendants = self._descendants_for_task(parent.id, children_by_parent)
        retrospective = failure_retrospective.build_retrospective(
            parent_id=parent.id,
            children=descendants,
            generated_at=utcnow().isoformat(),
        )
        meta = loads(parent.node_meta_json, {})
        previous = meta.get(failure_retrospective.RETROSPECTIVE_KEY)
        total = int(retrospective["summary"]["total"])
        if total:
            meta[failure_retrospective.RETROSPECTIVE_KEY] = retrospective
        else:
            meta.pop(failure_retrospective.RETROSPECTIVE_KEY, None)
        parent.node_meta_json = dumps(meta)
        if total and previous != retrospective:
            self._add_event(
                db,
                parent.id,
                "parent.failure_retrospective",
                f"已把子树里 {total} 条失败/阻塞证据收进复盘（按类别：{retrospective['summary']['by_category']}）。",
                {"summary": retrospective["summary"], "entries": retrospective["entries"]},
            )
        if total:
            self._inject_failure_retrospective_into_note_children(db, parent, retrospective)
        return retrospective

    def _inject_failure_retrospective_into_note_children(
        self, db: Session, parent: TaskNode, retrospective: dict[str, Any]
    ) -> None:
        """把刚写好的失败复盘补进**还没开跑**的 skill-note 子任务描述（R6）。

        为什么需要它：note 子任务是在**规划期**创建的，那时复盘还不存在，
        `skill_notes.parent_skill_note_child` 拼出来的 description 里 `retrospective_block(...)` 是空串——
        于是"失败路径必须进经验库"这条在主链路上从来没落地（只有"父已有复盘 + 父无 note 子节点"
        的补建路径能带出来，那条路径本身罕见）。这里给未开跑的 note 子任务补上，
        已经跑起来的就不动它（改 prompt 改不动一个正在执行的进程）。
        """
        if not skill_notes.skill_note_tasks_enabled(self.settings):
            return
        block = failure_retrospective.retrospective_block(retrospective)
        if not block:
            return
        pending = {TaskStatus.PLANNED.value, TaskStatus.QUEUED.value}
        for child in db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all():
            if str(child.status or "") not in pending:
                continue
            if not skill_notes.is_skill_note_task(child, self.settings):
                continue
            if "FAILURE_RETROSPECTIVE" in str(child.description or ""):
                continue
            child.description = f"{child.description}\n\n{block}"
            child.updated_at = utcnow()
            self._add_event(
                db,
                child.id,
                "parent.failure_retrospective_injected",
                "父节点的失败复盘已补进本子任务的描述（它是在复盘产生之前创建的）。",
                {"parent_id": parent.id, "total": retrospective["summary"]["total"]},
            )

    def _record_regression_obligations(self, db: Session, task: TaskNode) -> None:
        """R5 跨版本回归门禁：拿**上一版**基线比现在，缺失/改变即登记债务。

        **必须在 `_obligation_gate` 之前调用**——门禁读的是 `node_meta["obligations"]`，
        晚一步登记就等于这一轮静默通过了。

        只在版本真的跨过去时比（`regression.version_advanced`）：同一版内的修复返工本来就要改文件，
        拿它当回退会把门禁变成"拦住自己的修复"。

        顺带清掉"已经回到基线"的旧债——清债条件是**可核对的事实**（产物又对上了），
        不是"谁完成了什么"（那会把清债绑死在一次具体运行上）。
        """
        if not self.settings.integration_regression_enabled:
            return
        baseline = self._regression_baseline(task)
        if not baseline:
            return
        current_version = task.constitution_version if isinstance(task.constitution_version, int) else None
        if not regression.version_advanced(
            baseline, current_version=current_version, current_hash=str(task.constitution_hash or "")
        ):
            return

        root, _unused = self._regression_scope(db, task)
        recorded = baseline.get("artifacts") if isinstance(baseline.get("artifacts"), dict) else {}
        # 读不出来的产物**不算** missing：`missing` 是无条件 blocking，而读失败（Windows 上被别的
        # 进程占着）不是"产物没了"。把它们单列出来进事件，别让一次瞬时占用把父节点打进修复。
        unreadable: list[str] = []
        current = regression.snapshot_artifacts(root, sorted(str(path) for path in recorded), unreadable)
        diff = regression.diff_baseline(baseline, current, unreadable)
        strategy = self.settings.integration_regression_strategy
        incoming = obligations.obligations_from_regression(
            node_id=task.id,
            diff=diff,
            current_version=current_version,
            strategy=strategy,
            depth=task.depth,
        )
        restored = {
            obligations.obligation_id(obligations.REGRESSION_BREAK, task.id, f"{kind}|{path}")
            for kind in ("missing", "changed")
            for path in diff.get("intact") or []
        }
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        unsettled_ids = {str(item.get("id") or "") for item in existing if not item.get("settled_at")}
        new_ids = sorted(str(item.get("id") or "") for item in incoming if str(item.get("id") or "") not in unsettled_ids)
        merged = obligations.upsert(obligations.settle_regression_restored(existing, restored_ids=restored), incoming)
        if merged == existing:
            return
        meta["obligations"] = merged
        task.node_meta_json = dumps(meta)
        if new_ids:
            self._add_event(
                db,
                task.id,
                "parent.regression_break",
                regression.diff_reason(diff, current_version=current_version),
                {"strategy": strategy, "obligations": incoming, "diff": diff},
            )

    def _refresh_regression_baseline(self, db: Session, task: TaskNode) -> None:
        """验收通过后重记基线（R5）。

        **必须在门禁通过之后、版本戳之后**记：被拦下的那一轮没有"通过"，拿它当基线等于把回退洗成
        正常状态；版本戳之前记则会把旧版本写进基线，下一轮又拿它比对。

        没有可指认的产物就不写基线——**不拿空基线冒充"已登记"**。
        """
        if not self.settings.integration_regression_enabled:
            return
        root, candidates = self._regression_scope(db, task)
        # 基线是**累加**的：上一版验证过的产物只要还在，就继续留在基线里。
        # 否则"这一轮集成没碰过它"就会被悄悄移出防线，下一版把它删掉都不算回退——
        # 那正好是 R5 要消灭的那种静默。已删除的产物自然掉出基线（它已不在交付面里，
        # 而删除这件事本身会在跨版本比对时先被拦一次）。
        previous = self._regression_baseline(task)
        recorded = list(previous.get("artifacts") or {}) if isinstance(previous.get("artifacts"), dict) else []
        paths = regression.normalize_artifact_paths(root, [*candidates, *recorded])
        artifacts = regression.snapshot_artifacts(root, paths)
        if not artifacts:
            return
        assertions = loads(task.acceptance_json, [])
        meta = loads(task.node_meta_json, {})
        meta[regression.BASELINE_KEY] = regression.build_baseline(
            constitution_version=task.constitution_version if isinstance(task.constitution_version, int) else None,
            constitution_hash=str(task.constitution_hash or ""),
            artifacts=artifacts,
            assertions=[str(item) for item in assertions] if isinstance(assertions, list) else [],
            root=str(root),
        )
        task.node_meta_json = dumps(meta)

    def _unsettled_blocking_obligations(
        self, task: TaskNode, *, types: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        meta = loads(task.node_meta_json, {})
        items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        blocking = obligations.blocking(items)
        if types is None:
            return blocking
        return [item for item in blocking if str(item.get("type") or "") in set(types)]

    def _settle_caught_up_version_obligations(self, db: Session, task: TaskNode) -> int:
        """把已经追平的版本类债务清掉（`version_catchup`），返回清掉的条数。

        条件是**可判的事实**："这个节点当前记着的版本已经不落后了"。所以它既不看谁完成了什么，
        也不看轮次——重试、中断、换人跑都不会让它说不清。
        """
        meta = loads(task.node_meta_json, {})
        items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        if not items:
            return 0
        nodes = self._subtree(
            list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all()),
            task.id,
        )
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all())
        versions = {node.id: constitution_versions.normalize_version(node.constitution_version) for node in [task, *nodes, *children]}
        updated = obligations.settle_version_caught_up(
            items,
            versions=versions,
            effective_version=self._constitution_head_version(db, task.constitution_id),
        )
        cleared = sum(
            1
            # `strict=True`：`settle_version_caught_up` 是**逐条映射**，两边必然等长。写死这个不变量，
            # 将来谁把它改成"可能少返回几条"，这里会当场报错——而不是静默少算几条已清的债
            # （那会让债永远清不掉，而且没人看得出来为什么）。
            for before, after in zip(items, updated, strict=True)
            if before.get("settled_at") is None and after.get("settled_at") is not None
        )
        if cleared:
            meta["obligations"] = updated
            task.node_meta_json = dumps(meta)
        return cleared

    def _revalidatable_stale_leaves(self, db: Session, task: TaskNode) -> list[TaskNode]:
        """落后、已完成、且**能只重跑 verifier** 就再验证的叶子。

        "能只重跑 verifier"的判据是 `_pending_worker_result` 有值——即它留有成功的 worker 产物。
        没有产物的节点重跑 verifier 无从判起；有子节点的（已完成的分支）不在 v1 范围里。
        """
        meta = loads(task.node_meta_json, {})
        items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        pending_ids = {
            str(item.get("source_node_id") or "")
            for item in items
            if item.get("type") == obligations.REVALIDATE_REQUIRED and not item.get("settled_at")
        }
        targets: list[TaskNode] = []
        for child in db.scalars(select(TaskNode).where(TaskNode.parent_id == task.id)).all():
            if child.id not in pending_ids or child.status != TaskStatus.DONE.value:
                continue
            if list(child.children):
                continue
            if self._revalidation_worker_result(child) is None:
                continue
            targets.append(child)
        return targets

    async def _revalidate_stale_leaves(
        self, task_id: str, runner: BaseCodexRunner, semaphore: asyncio.Semaphore
    ) -> list[str]:
        """在父节点验收之前，用**新宪法**把落后且可再验证的 done 叶子重跑一次 verifier。

        这是宪法 SPEC §6 的"`revalidate_required` 是一个显式队列，不是标记位"：父节点必须用新
        宪法对这些子节点过一遍真实判据。verdict 走**既有**的 `_apply_verifier_result` 通路——
        通过即标 done 并由 `_stamp_constitution_version_on_completion` 追平版本戳（于是债务被
        `version_catchup` 清掉）；不通过就走既有的修复/转父节点通路，而不是由门控自己发明子任务。

        返回：因此产生的子任务 id（调用方负责推进它们）。
        """
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
                return []
            targets = self._revalidatable_stale_leaves(db, task)
            pending = [
                (target.id, self._revalidation_worker_result(target), task.constitution_version)
                for target in targets
            ]
        created_child_ids: list[str] = []
        for target_id, worker_result, version in pending:
            if worker_result is None:
                continue
            # 先把快照换到**新宪法**，但**不动版本戳**：verifier 的 prompt 里嵌的是本节点的
            # `constitution_json`，不换快照就等于拿旧版判据复查（白跑一次）。
            # 换快照后若 verdict 不通过，节点会带着新快照进入修复通路，版本戳仍停在旧值——
            # "产物是在旧版下通过的"这个事实没有被抹掉。
            with SessionLocal() as db:
                node = db.get(TaskNode, target_id)
                if node is None:
                    continue
                head = self._constitution_head_record(db, node.constitution_id)
                if head is None:
                    continue
                node.constitution_id = head.id
                node.constitution_hash = head.content_hash
                node.constitution_json = head.data_json
                node.updated_at = utcnow()
                db.commit()
            try:
                async with semaphore:
                    if not self._try_mark_for_revalidation(target_id):
                        continue
                    if not await self._reserve_agent_start(target_id, "verifier"):
                        continue
                    envelope = await runner.verify(self._snapshot_task(target_id), worker_result)
                    if self._is_task_stopped(target_id):
                        return created_child_ids
                verifier_result = VerifierResult.model_validate(envelope.payload)
            except Exception as exc:
                if self._is_task_stopped(target_id):
                    return created_child_ids
                with SessionLocal() as db:
                    self._block_task(db, self._get_task(db, target_id), f"再验证失败：{exc}", "verifier.exception")
                    db.commit()
                continue
            with SessionLocal() as db:
                failed = verifier_result.verdict != "pass"
                self._add_event(
                    db,
                    target_id,
                    "task.constitution_revalidated",
                    f"宪法修订后的再验证{'未通过' if failed else '通过'}：{verifier_result.reason}",
                    {
                        "verdict": verifier_result.verdict,
                        "reason": verifier_result.reason,
                        "revalidated_from_version": version,
                    },
                )
                db.commit()
            created_child_ids.extend(
                self._record_verifier_result(target_id, worker_result, envelope, verifier_result)
            )
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            self._settle_caught_up_version_obligations(db, task)
            db.commit()
        return created_child_ids

    def _obligation_gate(self, db: Session, task: TaskNode) -> bool:
        """收尾处拒绝标 `done`：还有未清的阻断性债务。返回 `True` 表示已拦下。

        判据就是 §6.1 判据 2 的**一条无条件判据**：`blocking` 且未清的入站债务存在即拦。
        哪些债是 `blocking` 由**债务生成时的规则**决定，而不是由这里的开关决定：

        - `integration_conflict` —— 由集成策略决定（`repair` 阻断；`overwrite`/`keep_both`
          只上浮不阻断，见 `obligations.conflict_severity`）；
        - `constitution_stale` / `revalidate_required` —— 由 `amendment_direction` 决定
          （收紧阻断、放宽只上浮）。

        这一点是刻意的：如果严重度写死成 blocking，再由门控按配置过滤，看板就会出现
        "报阻断但树照跑"，`§9` 的用途（一眼看出树有没有被卡）当场失效。

        `constitution_revalidate_enabled` 不参与这条判据——它只决定**要不要花 token 去跑**
        再验证。关掉它的后果是那些版本债照旧阻断（这是事实：人选了收紧却拒绝复查），
        最终走下面的升级通路交给人，而不是被静默放行。

        **不创建子任务**（§6.4 硬约束 1）。第一版接线在拦下之后自己建"解决集成冲突"子任务，
        于是修复子任务本身成为新的产出方 → 下一轮生成新债 → 再建子任务，债务与子任务一起
        无限增长（实测 5993 秒后 RecursionError）。这里只做两件事：拒绝标记 done，并把裁决
        交回给会做决定的那一方——验收 prompt 里已经有冲突指令，agent 可以自己产出修复子任务，
        人也可以直接清债。

        **有界**（硬约束 2）：连续拦下 `obligation_gate_max_refusals` 次后升级为人工阻塞。
        计数**不按冲突签名分桶**：签名会随每轮冲突集合变化，用它当键的话"每轮换一个冲突"
        就能让计数器永远归零，那是看起来有界、实际无界。干净通过一轮才归零。
        """
        meta = loads(task.node_meta_json, {})
        gate = meta.get("obligation_gate") if isinstance(meta.get("obligation_gate"), dict) else {}
        blocking = self._unsettled_blocking_obligations(task)
        if not blocking:
            if gate:
                meta.pop("obligation_gate", None)
                task.node_meta_json = dumps(meta)
            return False

        refusals = int(gate.get("consecutive_refusals") or 0) + 1
        by_type = obligations.summarize(blocking)["by_type"]
        task.node_meta_json = dumps(
            {
                **meta,
                "obligation_gate": {
                    "consecutive_refusals": refusals,
                    "blocking_ids": sorted(str(item.get("id") or "") for item in blocking),
                    "by_type": by_type,
                    "last_refused_at": utcnow().isoformat(),
                },
            }
        )
        limit = max(1, int(self.settings.obligation_gate_max_refusals))
        labels = {
            obligations.INTEGRATION_CONFLICT: "集成冲突",
            obligations.INTEGRATION_SKIPPED: "集成跳过",
            obligations.CONSTITUTION_STALE: "宪法版本落后",
            obligations.REVALIDATE_REQUIRED: "待再验证",
            obligations.REGRESSION_BREAK: "跨版本回归",
        }
        kinds = "、".join(labels.get(str(name), str(name)) for name in sorted(by_type))
        paths = sorted({str((item.get("payload") or {}).get("path") or "") for item in blocking} - {""})
        detail = f"（{kinds}）" + ("：" + "、".join(paths[:5]) if paths else "")
        if refusals >= limit:
            reason = f"未清债务连续 {refusals} 次阻止父节点完成，已停止继续修复并升级为人工审查{detail}"
            self._add_event(
                db,
                task.id,
                "parent.obligation_gate_escalated",
                reason,
                {"obligations": blocking, "consecutive_refusals": refusals, "limit": limit, "by_type": by_type},
            )
            self._block_task(db, task, reason, "obligation.gate", {"obligations": blocking})
            db.commit()
            return True
        reason = f"存在未清的阻断性债务，父节点不得标记完成（第 {refusals}/{limit} 次）{detail}"
        self._add_event(
            db,
            task.id,
            "parent.obligation_gate_blocked",
            reason,
            {"obligations": blocking, "consecutive_refusals": refusals, "limit": limit, "by_type": by_type},
        )
        self._mark(db, task, TaskStatus.REPAIR, reason)
        db.commit()
        return True
        db.commit()
        return True

    def _safe_write_state_snapshot(self, root_id: str, reason: str, keep_history: bool = True) -> None:
        try:
            with SessionLocal() as db:
                self._write_state_snapshot(db, root_id, reason, keep_history=keep_history)
        except Exception:
            return

    def _watchdog_state_snapshot_due(self, root_id: str) -> bool:
        return checkpoint.state_snapshot_due(
            self.settings.workspace_dir,
            root_id,
            self.settings.watchdog_state_snapshot_interval_seconds,
        )

    def _write_state_snapshot(self, db: Session, root_id: str, reason: str, keep_history: bool = True) -> Path | None:
        self._sync_live_external_agent_statuses(db=db)
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        tasks = self._subtree(all_tasks, root_id)
        if not tasks:
            return None
        task_ids = {task.id for task in tasks}
        counts = Counter(task.status for task in tasks)
        blocker_counts = self._blocker_visibility_counts(tasks)
        active_agent_statuses = sum(1 for task in tasks if task.status in ACTIVE_AGENT_STATUSES)
        codex_slots_active = self._codex_slots_active()
        active_agents = max(active_agent_statuses, codex_slots_active)
        auto_concurrency_snapshot: dict[str, Any] = {}
        try:
            auto_concurrency_snapshot = self._auto_concurrency_decision(db, root_id)
        except KeyError:
            auto_concurrency_snapshot = {}
        reported_codex_capacity = self._reported_codex_slots_capacity(
            codex_slots_active=codex_slots_active,
            auto_concurrency_snapshot=auto_concurrency_snapshot,
        )
        constitution = self._constitution_for_tasks(db, tasks)
        events = list(
            db.scalars(
                select(EventRecord)
                .where(EventRecord.task_id.in_(task_ids))
                .order_by(EventRecord.created_at.desc())
                .limit(200)
            ).all()
        )
        payload = {
            "snapshot_version": 1,
            "root_id": root_id,
            "reason": reason,
            "created_at": utcnow().isoformat(),
            "stats": {
                "total": len(tasks),
                "done": counts[TaskStatus.DONE.value],
                "waiting_children": counts[TaskStatus.WAITING_CHILDREN.value],
                "hibernating": counts[TaskStatus.HIBERNATING.value],
                "blocked": counts[TaskStatus.BLOCKED.value],
                "blocked_active": blocker_counts["active_blocked"],
                "blocked_superseded": blocker_counts["superseded_blocked"],
                "blocked_under_repair": blocker_counts["repair_pending_blocked"],
                "blocked_repaired": blocker_counts["repair_done_blocked"],
                "blocked_inactive_branch": blocker_counts["inactive_branch_blocked"],
                "blocked_resource_wait": blocker_counts["resource_wait_blocked"],
                "failed": counts[TaskStatus.FAILED.value],
                "active_agents": active_agents,
                "active_agent_statuses": active_agent_statuses,
                "codex_slots_active": codex_slots_active,
                "codex_slots_capacity": reported_codex_capacity,
                "max_depth": max((task.depth for task in tasks), default=0),
            },
            "tasks": [self.serialize_task(task, False) for task in tasks],
            "edges": [{"source": task.parent_id, "target": task.id} for task in tasks if task.parent_id],
            "constitution": self.serialize_constitution(constitution) if constitution else None,
            "events": [self.serialize_event(event) for event in events],
        }
        snapshot_dir = Path(self.settings.workspace_dir) / "state_snapshots" / root_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        latest_path = snapshot_dir / "latest.json"
        # **原子写 + 留一份备份**（P5-6）：`latest.json` 是还原锚点，而进程可能在写它的
        # 中途被杀（断电 / taskkill / 容器被杀）。`write_text` 是"截断后写"，
        # 半截 JSON 解不出来 —— 于是"表被清空"那条通路恰好在最需要它的时候失效，
        # 而且**静默失效**。见 `checkpoint.atomic_write_json`。
        checkpoint.atomic_write_json(latest_path, payload)
        if keep_history:
            stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
            history_path = snapshot_dir / f"{stamp}-{reason.replace('.', '-')}.json"
            try:
                checkpoint.atomic_write_json(history_path, payload)
            except OSError:
                # 历史快照是**尽力而为**：它写不上不该让主锚点的写成功被抹掉。
                pass
        return latest_path

    def _maybe_write_full_checkpoint(self, db: Session, root_id: str, reason: str) -> Path | None:
        if not self.settings.full_checkpoint_enabled:
            return None
        interval = max(60, int(self.settings.full_checkpoint_interval_seconds))
        latest_manifest = self._full_checkpoint_root(root_id) / "latest.json"
        if latest_manifest.is_file():
            try:
                payload = json.loads(latest_manifest.read_text(encoding="utf-8"))
                created_at = datetime.fromisoformat(str(payload.get("created_at") or "").replace("Z", "+00:00"))
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                checkpoint_id = Path(str(payload.get("checkpoint_manifest") or latest_manifest)).parent.name
                if not checkpoint_id or checkpoint_id == str(latest_manifest.parent.name):
                    checkpoint_id = str(Path(str(payload.get("database_copy") or "")).parent.name or latest_manifest.parent.name)
                artifacts_fresh = (utcnow() - created_at).total_seconds() < interval and self._full_checkpoint_artifacts_exist(payload)
                if artifacts_fresh and self._latest_checkpoint_covers_current_battlefield_head(root_id, checkpoint_id):
                    return None
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return self._write_full_checkpoint(db, root_id, reason)

    def _latest_checkpoint_covers_current_battlefield_head(self, root_id: str, checkpoint_id: str) -> bool:
        current_head = self._git_commit_for_ref(ROOT_DIR, "HEAD")
        if not current_head:
            return True
        ledger_path = Path(self.settings.workspace_dir) / "checkpoint_ledger" / root_id / "latest.json"
        if not ledger_path.is_file():
            return False
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if str(ledger.get("checkpoint_id") or "") != str(checkpoint_id):
            return False

        candidate_refs: list[str] = []
        for key in (
            "battlefield_restore_ref",
            "battlefield_git_tag",
            "battlefield_git_commit_after",
            "battlefield_git_commit_before",
        ):
            value = ledger.get(key)
            if isinstance(value, str) and value.strip():
                candidate_refs.append(value.strip())
        battlefield_git = ledger.get("battlefield_git")
        if isinstance(battlefield_git, dict):
            for key in ("commit", "current_commit", "head", "head_commit"):
                value = battlefield_git.get(key)
                if isinstance(value, str) and value.strip():
                    candidate_refs.append(value.strip())

        for ref in candidate_refs:
            resolved = self._git_commit_for_ref(ROOT_DIR, ref)
            if resolved and resolved == current_head:
                return True
        return False

    def _git_commit_for_ref(self, path: Path | None, ref: str) -> str | None:
        if path is None or not path.exists() or not ref:
            return None
        timeout = max(1, int(self.settings.checkpoint_git_probe_timeout_seconds))
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout.strip()
        return value or None

    def _full_checkpoint_artifacts_exist(self, manifest: dict[str, Any]) -> bool:
        return checkpoint.full_checkpoint_artifacts_exist(manifest)

    def _write_full_checkpoint(self, db: Session, root_id: str, reason: str) -> Path | None:
        root = db.get(TaskNode, root_id)
        if root is None:
            return None
        checkpoint_root = self._full_checkpoint_root(root_id)
        checkpoint_root.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        checkpoint_dir = checkpoint_root / stamp
        checkpoint_dir.mkdir(parents=True, exist_ok=False)

        state_snapshot_path = self._write_state_snapshot(db, root_id, f"{reason}.full_checkpoint", keep_history=True)
        db_copy_path = checkpoint_dir / "battlefield.sqlite3"
        database_source = self._sqlite_database_path()
        database_copied = False
        if database_source and database_source.is_file():
            self._backup_sqlite_database(database_source, db_copy_path)
            database_copied = db_copy_path.is_file()

        target = self._root_target_path(db, root)
        target_copy_path = checkpoint_dir / "target"
        target_copied = False
        if target and target.is_dir():
            shutil.copytree(target, target_copy_path, ignore=self._checkpoint_ignore)
            target_copied = target_copy_path.is_dir()

        stats = self._full_checkpoint_stats(db, root_id)
        manifest = {
            "checkpoint_version": 1,
            "root_id": root_id,
            "reason": reason,
            "created_at": utcnow().isoformat(),
            "stats": stats,
            "database_source": str(database_source) if database_source else None,
            "database_copy": str(db_copy_path) if database_copied else None,
            "state_snapshot": str(state_snapshot_path) if state_snapshot_path else None,
            "target_source": str(target) if target else None,
            "target_copy": str(target_copy_path) if target_copied else None,
            "target_copied": target_copied,
        }
        manifest_path = checkpoint_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        (checkpoint_root / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._write_checkpoint_ledger(root_id, checkpoint_dir.name, manifest_path, manifest)
        self._prune_full_checkpoints(root_id)
        self._add_event(
            db,
            root_id,
            "tree.full_checkpoint",
            "已保存完整检查点：任务数据库与目标 Godot 工程目录可一起回滚。",
            manifest,
        )
        db.commit()
        return manifest_path

    def _write_checkpoint_ledger(
        self,
        root_id: str,
        checkpoint_id: str,
        manifest_path: Path,
        manifest: dict[str, Any],
    ) -> Path | None:
        if not self.settings.checkpoint_ledger_enabled:
            return None
        ledger_dir = Path(self.settings.workspace_dir) / "checkpoint_ledger" / root_id
        ledger_dir.mkdir(parents=True, exist_ok=True)
        database_copy = Path(str(manifest.get("database_copy") or "")) if manifest.get("database_copy") else None
        target_source = Path(str(manifest.get("target_source") or "")) if manifest.get("target_source") else None
        target_copy = Path(str(manifest.get("target_copy") or "")) if manifest.get("target_copy") else None
        ledger = {
            "ledger_version": 1,
            "created_at": utcnow().isoformat(),
            "root_id": root_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_reason": manifest.get("reason"),
            "checkpoint_created_at": manifest.get("created_at"),
            "checkpoint_manifest": str(manifest_path),
            "checkpoint_manifest_sha256": self._sha256_file(manifest_path),
            "database_copy": str(database_copy) if database_copy else None,
            "database_copy_sha256": self._sha256_file(database_copy) if database_copy else None,
            "state_snapshot": manifest.get("state_snapshot"),
            "target_source": str(target_source) if target_source else None,
            "target_copy": str(target_copy) if target_copy else None,
            "target_git": self._git_probe(target_source),
            "battlefield_git": self._git_probe(ROOT_DIR),
            "version_id": f"{root_id}-{checkpoint_id}",
            "publish_hint": "Commit and publish checkpoint_ledger with GitHub Desktop; full_checkpoints remain local recovery artifacts.",
        }
        ledger_path = ledger_dir / f"{checkpoint_id}.json"
        payload = json.dumps(ledger, ensure_ascii=False, indent=2, default=str) + "\n"
        ledger_path.write_text(payload, encoding="utf-8")
        (ledger_dir / "latest.json").write_text(payload, encoding="utf-8")
        return ledger_path

    def _sha256_file(self, path: Path | None) -> str | None:
        if path is None or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _git_probe(self, path: Path | None) -> dict[str, Any]:
        if path is None or not path.exists():
            return {"path": str(path) if path else None, "exists": False, "git_ready": False}
        timeout = max(1, int(self.settings.checkpoint_git_probe_timeout_seconds))

        def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", str(path), *args],
                cwd=str(ROOT_DIR),
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        try:
            inside = run_git(["rev-parse", "--is-inside-work-tree"])
        except (OSError, subprocess.SubprocessError) as exc:
            return {"path": str(path), "exists": True, "git_ready": False, "error": str(exc)}
        if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
            return {"path": str(path), "exists": True, "git_ready": False}
        result: dict[str, Any] = {"path": str(path), "exists": True, "git_ready": True}
        commands = {
            "commit": ["rev-parse", "--verify", "HEAD"],
            "branch": ["branch", "--show-current"],
            "status_porcelain": ["status", "--porcelain"],
        }
        for key, args in commands.items():
            try:
                completed = run_git(args)
            except (OSError, subprocess.SubprocessError) as exc:
                result[f"{key}_error"] = str(exc)
                continue
            if completed.returncode == 0:
                result[key] = completed.stdout.strip()
        result["dirty"] = bool(str(result.get("status_porcelain") or "").strip())
        return result

    def _full_checkpoint_root(self, root_id: str) -> Path:
        return checkpoint.full_checkpoint_root(self.settings.workspace_dir, root_id)

    def _sqlite_database_path(self) -> Path | None:
        return checkpoint.sqlite_database_path(self.settings.database_url)

    def _backup_sqlite_database(self, source: Path, destination: Path) -> None:
        checkpoint.backup_sqlite_database(source, destination)

    def _checkpoint_ignore(self, directory: str, names: list[str]) -> set[str]:
        return checkpoint.checkpoint_ignore(directory, names)

    def _full_checkpoint_stats(self, db: Session, root_id: str) -> dict[str, Any]:
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        tasks = self._subtree(all_tasks, root_id)
        counts = Counter(task.status for task in tasks)
        blocker_counts = self._blocker_visibility_counts(tasks)
        return {
            "total": len(tasks),
            "done": counts[TaskStatus.DONE.value],
            "queued": counts[TaskStatus.QUEUED.value],
            "waiting_children": counts[TaskStatus.WAITING_CHILDREN.value],
            "hibernating": counts[TaskStatus.HIBERNATING.value],
            "blocked": counts[TaskStatus.BLOCKED.value],
            "blocked_active": blocker_counts["active_blocked"],
            "blocked_superseded": blocker_counts["superseded_blocked"],
            "blocked_under_repair": blocker_counts["repair_pending_blocked"],
            "blocked_repaired": blocker_counts["repair_done_blocked"],
            "blocked_inactive_branch": blocker_counts["inactive_branch_blocked"],
            "blocked_resource_wait": blocker_counts["resource_wait_blocked"],
            "failed": counts[TaskStatus.FAILED.value],
            "active_agents": sum(1 for task in tasks if task.status in ACTIVE_AGENT_STATUSES),
            "max_depth": max((task.depth for task in tasks), default=0),
        }

    def _prune_full_checkpoints(self, root_id: str) -> None:
        checkpoint.prune_full_checkpoints(
            self.settings.workspace_dir,
            root_id,
            max(1, int(self.settings.full_checkpoint_max_count)),
        )

    def _restore_snapshot_if_missing(self, db: Session, root_id: str | None) -> str | None:
        requested_root_id = root_id or self._latest_snapshot_root_id()
        if not requested_root_id:
            return None
        existing = db.get(TaskNode, requested_root_id)
        if existing is not None:
            return requested_root_id
        snapshot = self._read_state_snapshot(requested_root_id)
        if not snapshot:
            return None
        restored_root_id = str(snapshot.get("root_id") or requested_root_id)
        if db.get(TaskNode, restored_root_id) is not None:
            return restored_root_id
        constitution_payload = snapshot.get("constitution") if isinstance(snapshot.get("constitution"), dict) else None
        constitution_id: str | None = None
        constitution_hash: str | None = None
        constitution_json = "{}"
        if constitution_payload:
            constitution_id = str(constitution_payload.get("id") or new_id("const"))
            constitution_hash = str(constitution_payload.get("content_hash") or "")
            constitution_json = dumps(constitution_payload.get("data") if isinstance(constitution_payload.get("data"), dict) else {})
            if db.get(ConstitutionRecord, constitution_id) is None:
                db.add(
                    ConstitutionRecord(
                        id=constitution_id,
                        root_id=restored_root_id,
                        goal=str(constitution_payload.get("goal") or ""),
                        target_path=constitution_payload.get("target_path"),
                        status=str(constitution_payload.get("status") or "locked"),
                        version=int(constitution_payload.get("version") or 1),
                        content_hash=constitution_hash,
                        markdown=str(constitution_payload.get("markdown") or ""),
                        data_json=constitution_json,
                        created_at=self._parse_snapshot_datetime(constitution_payload.get("created_at")) or utcnow(),
                        updated_at=self._parse_snapshot_datetime(constitution_payload.get("updated_at")) or utcnow(),
                        locked_at=self._parse_snapshot_datetime(constitution_payload.get("locked_at")),
                    )
                )
        tasks_payload = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
        for item in sorted((task for task in tasks_payload if isinstance(task, dict)), key=lambda task: int(task.get("depth") or 0)):
            task_id = str(item.get("id") or "")
            if not task_id or db.get(TaskNode, task_id) is not None:
                continue
            node_meta = item.get("node_meta") if isinstance(item.get("node_meta"), dict) else {}
            db.add(
                TaskNode(
                    id=task_id,
                    parent_id=item.get("parent_id"),
                    title=str(item.get("title") or task_id),
                    description=str(item.get("description") or ""),
                    depth=int(item.get("depth") or 0),
                    status=str(item.get("status") or TaskStatus.QUEUED.value),
                    scope_json=dumps(item.get("scope") if isinstance(item.get("scope"), list) else []),
                    acceptance_json=dumps(item.get("acceptance") if isinstance(item.get("acceptance"), list) else []),
                    node_meta_json=dumps({**node_meta, "restored_from_snapshot": True, "restored_at": utcnow().isoformat()}),
                    constitution_id=item.get("constitution_id") or constitution_id,
                    constitution_hash=item.get("constitution_hash") or constitution_hash,
                    constitution_json=constitution_json,
                    constitution_version=constitution_versions.normalize_version(item.get("constitution_version")),
                    leaf_attempts=int(item.get("leaf_attempts") or 0),
                    verification_failures=int(item.get("verification_failures") or 0),
                    converted_from_leaf=bool(item.get("converted_from_leaf")),
                    progress_current=int(item.get("progress_current") or 0),
                    progress_total=int(item.get("progress_total") or 0),
                    result_json=dumps(item.get("result")) if item.get("result") is not None else None,
                    error_message=item.get("error_message"),
                    branch=item.get("branch"),
                    worktree_path=item.get("worktree_path"),
                    created_at=self._parse_snapshot_datetime(item.get("created_at")) or utcnow(),
                    updated_at=self._parse_snapshot_datetime(item.get("updated_at")) or utcnow(),
                )
            )
        db.add(
            EventRecord(
                task_id=restored_root_id,
                type="tree.snapshot_restored",
                message="任务树数据库缺失时，系统已从最新状态快照恢复任务树。",
                payload_json=dumps({"snapshot_root_id": requested_root_id, "restored_root_id": restored_root_id}),
            )
        )
        db.commit()
        self._schedule_restored_hibernation_if_needed(db, restored_root_id)
        return restored_root_id

    def _read_state_snapshot(self, root_id: str) -> dict[str, Any] | None:
        return checkpoint.read_state_snapshot(self.settings.workspace_dir, root_id)

    def _latest_snapshot_root_id(self) -> str | None:
        return checkpoint.latest_snapshot_root_id(self.settings.workspace_dir)

    def _parse_snapshot_datetime(self, value: Any) -> datetime | None:
        return checkpoint.parse_snapshot_datetime(value)

    def _schedule_restored_hibernation_if_needed(self, db: Session, root_id: str) -> None:
        root = db.get(TaskNode, root_id)
        if root is None or root.status != TaskStatus.HIBERNATING.value:
            return
        meta = loads(root.node_meta_json, {})
        hibernation = meta.get("hibernation") if isinstance(meta.get("hibernation"), dict) else {}
        resume_at_raw = hibernation.get("resume_at")
        if not resume_at_raw:
            return
        try:
            resume_at = datetime.fromisoformat(str(resume_at_raw).replace("Z", "+00:00"))
        except ValueError:
            resume_at = utcnow()
        self._schedule_auto_resume(root_id, resume_at)

    def _compact_done_tree_view(
        self,
        tasks: list[TaskNode],
        children_by_parent: dict[str | None, list[TaskNode]],
        compact_done: bool,
        expanded_done_parent_ids: set[str],
    ) -> tuple[list[TaskNode], dict[str, dict[str, Any]], set[str]]:
        if not compact_done or not tasks:
            return tasks, {}, set()

        task_by_id = {task.id: task for task in tasks}
        done_subtree_cache: dict[str, bool] = {}
        subtree_size_cache: dict[str, int] = {}
        hidden_task_ids: set[str] = set()
        archive_summaries: dict[str, dict[str, Any]] = {}

        def is_done_subtree(task: TaskNode) -> bool:
            cached = done_subtree_cache.get(task.id)
            if cached is not None:
                return cached
            children = children_by_parent.get(task.id, [])
            is_done = task.status == TaskStatus.DONE.value and all(is_done_subtree(child) for child in children)
            done_subtree_cache[task.id] = is_done
            return is_done

        def subtree_size(task: TaskNode) -> int:
            cached = subtree_size_cache.get(task.id)
            if cached is not None:
                return cached
            count = 1 + sum(subtree_size(child) for child in children_by_parent.get(task.id, []))
            subtree_size_cache[task.id] = count
            return count

        def hide_subtree(task: TaskNode) -> None:
            hidden_task_ids.add(task.id)
            for child in children_by_parent.get(task.id, []):
                hide_subtree(child)

        expanded_done_subtree_ids: set[str] = set()

        def collect_subtree_ids(task: TaskNode) -> None:
            expanded_done_subtree_ids.add(task.id)
            for child in children_by_parent.get(task.id, []):
                collect_subtree_ids(child)

        for parent in tasks:
            if parent.id not in expanded_done_parent_ids:
                continue
            for child in children_by_parent.get(parent.id, []):
                if is_done_subtree(child):
                    collect_subtree_ids(child)

        for parent in tasks:
            if parent.id in expanded_done_subtree_ids and parent.id not in expanded_done_parent_ids:
                continue
            done_roots = [child for child in children_by_parent.get(parent.id, []) if is_done_subtree(child)]
            if not done_roots:
                continue
            compacted = parent.id not in expanded_done_parent_ids
            node_count = sum(subtree_size(child) for child in done_roots)
            archive_summaries[parent.id] = {
                "node_count": node_count,
                "root_count": len(done_roots),
                "compacted": compacted,
                "expanded": not compacted,
                "root_ids": [child.id for child in done_roots[:24]],
                "truncated_root_ids": max(0, len(done_roots) - 24),
            }
            if compacted:
                for child in done_roots:
                    hide_subtree(child)

        visible_tasks = [task for task in tasks if task.id in task_by_id and task.id not in hidden_task_ids]
        return visible_tasks, archive_summaries, hidden_task_ids

    def get_tree(
        self,
        db: Session,
        root_id: str | None = None,
        compact_done: bool = False,
        expanded_done_parent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self._sync_live_external_agent_statuses(db=db)
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        selected_root_id = root_id or self._latest_root_id(all_tasks)
        if selected_root_id is None or not self._subtree(all_tasks, selected_root_id):
            restored_root_id = self._restore_snapshot_if_missing(db, root_id)
            if restored_root_id:
                all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
                selected_root_id = restored_root_id
        else:
            selected_root_id = root_id or self._latest_root_id(all_tasks)
        tasks = self._subtree(all_tasks, selected_root_id) if selected_root_id else []
        children_by_parent = self._children_by_parent(tasks)
        expanded_done_parent_ids_set = set(expanded_done_parent_ids or [])
        visible_tasks, done_archive_summaries, archived_done_node_ids = self._compact_done_tree_view(
            tasks,
            children_by_parent,
            compact_done=compact_done,
            expanded_done_parent_ids=expanded_done_parent_ids_set,
        )
        child_parent_pairs = {(task.id, task.parent_id) for task in tasks if task.parent_id}
        parent_ids = {parent_id for _, parent_id in child_parent_pairs}
        serialized_tasks = [
            self.serialize_task(
                task,
                task.id not in parent_ids and task.status in ACTIVE_FRONTIER_STATUSES,
                self._waiting_report_for_task(task, children_by_parent),
                done_archive_summaries.get(task.id),
            )
            for task in visible_tasks
        ]
        counts = Counter(task.status for task in tasks)
        visible_counts = Counter(task.status for task in visible_tasks)
        blocker_counts = self._blocker_visibility_counts(tasks)
        active_agent_statuses = sum(1 for task in tasks if task.status in ACTIVE_AGENT_STATUSES)
        codex_slots_active = self._codex_slots_active()
        active_agents = max(active_agent_statuses, codex_slots_active)
        constitution = self._constitution_for_tasks(db, tasks)
        agent_burst_used = self._agent_burst_used_for_root(db, selected_root_id) if selected_root_id else 0
        agent_burst_limit = self._agent_burst_limit()
        agent_burst_reserve = self._agent_burst_effective_reserve()
        agent_burst_work_limit = self._agent_burst_work_limit()
        root_meta = loads(next((task.node_meta_json for task in tasks if task.id == selected_root_id), "{}"), {}) if selected_root_id else {}
        auto_concurrency = root_meta.get("auto_concurrency") if isinstance(root_meta.get("auto_concurrency"), dict) else {}
        auto_concurrency_snapshot = auto_concurrency
        auto_lower, auto_upper = self._auto_concurrency_bounds()
        auto_concurrency_mode = str(auto_concurrency.get("mode") or "pending")
        auto_concurrency_reason = str(auto_concurrency.get("reason") or "等待下一次调度评估。")
        auto_concurrency_pressure = int(auto_concurrency.get("resource_pressure") or 0)
        if selected_root_id:
            try:
                live_auto_concurrency = self._auto_concurrency_decision(db, selected_root_id)
            except KeyError:
                live_auto_concurrency = None
            if live_auto_concurrency:
                auto_concurrency_snapshot = live_auto_concurrency
                auto_concurrency_mode = str(live_auto_concurrency.get("mode") or auto_concurrency_mode)
                auto_concurrency_reason = str(live_auto_concurrency.get("reason") or auto_concurrency_reason)
                auto_concurrency_pressure = int(live_auto_concurrency.get("resource_pressure") or 0)
        if not self.settings.auto_concurrency_enabled:
            auto_concurrency_mode = "fixed_capacity"
            auto_concurrency_reason = f"普通速度模式：自动提速已关闭，同一时间最多运行 {self.settings.max_concurrency} 个真实 Codex CLI。"
            auto_concurrency_pressure = 0
        reported_codex_capacity = self._reported_codex_slots_capacity(
            codex_slots_active=codex_slots_active,
            auto_concurrency_snapshot=auto_concurrency_snapshot,
        )
        stats = {
            "total": len(tasks),
            "done": counts[TaskStatus.DONE.value],
            "running": counts[TaskStatus.RUNNING.value] + counts[TaskStatus.WORKING.value],
            "planning": counts[TaskStatus.PLANNING.value],
            "queued": counts[TaskStatus.QUEUED.value],
            "working": counts[TaskStatus.WORKING.value],
            "verifying": counts[TaskStatus.VERIFYING.value],
            "accepting": counts[TaskStatus.ACCEPTING.value],
            "waiting_children": counts[TaskStatus.WAITING_CHILDREN.value],
            "hibernating": counts[TaskStatus.HIBERNATING.value],
            "blocked": counts[TaskStatus.BLOCKED.value],
            "blocked_active": blocker_counts["active_blocked"],
            "blocked_superseded": blocker_counts["superseded_blocked"],
            "blocked_under_repair": blocker_counts["repair_pending_blocked"],
            "blocked_repaired": blocker_counts["repair_done_blocked"],
            "blocked_inactive_branch": blocker_counts["inactive_branch_blocked"],
            "blocked_resource_wait": blocker_counts["resource_wait_blocked"],
            "failed": counts[TaskStatus.FAILED.value],
            "active_agents": active_agents,
            "active_agent_statuses": active_agent_statuses,
            "codex_slots_active": codex_slots_active,
            "codex_slots_capacity": reported_codex_capacity,
            "active_frontier": sum(1 for task in serialized_tasks if task["is_active_frontier"]),
            "max_depth": max((task.depth for task in tasks), default=0),
            "max_concurrency": reported_codex_capacity,
            "configured_concurrency": self.settings.max_concurrency,
            "auto_concurrency_enabled": int(self.settings.auto_concurrency_enabled),
            "auto_concurrency_min": auto_lower,
            "auto_concurrency_max": auto_upper,
            "auto_concurrency_mode": auto_concurrency_mode,
            "auto_concurrency_reason": auto_concurrency_reason,
            "auto_concurrency_resource_pressure": auto_concurrency_pressure,
            "auto_concurrency_target": auto_concurrency_snapshot.get("target"),
            "auto_concurrency_desired": auto_concurrency_snapshot.get("desired"),
            "auto_concurrency_current_capacity": auto_concurrency_snapshot.get("current_capacity"),
            "auto_concurrency_fuel_phase": auto_concurrency_snapshot.get("fuel_phase"),
            "auto_concurrency_fuel_basis": auto_concurrency_snapshot.get("fuel_basis"),
            "auto_concurrency_fuel_recommended_target": auto_concurrency_snapshot.get("fuel_recommended_target"),
            "auto_concurrency_fuel_open_frontier": auto_concurrency_snapshot.get("fuel_open_frontier"),
            "auto_concurrency_fuel_time_pressure": auto_concurrency_snapshot.get("fuel_time_pressure"),
            "auto_concurrency_fuel_minutes_to_deadline": auto_concurrency_snapshot.get("fuel_minutes_to_deadline"),
            "auto_concurrency_fuel_deadline_local": auto_concurrency_snapshot.get("fuel_deadline_local"),
            "auto_concurrency_fuel_next_deadline_local": auto_concurrency_snapshot.get("fuel_next_deadline_local"),
            "auto_concurrency_fuel_deadline_elapsed": auto_concurrency_snapshot.get("fuel_deadline_elapsed"),
            "auto_concurrency_quota_observability": auto_concurrency_snapshot.get("quota_observability"),
            "auto_concurrency_quota_reset_at": auto_concurrency_snapshot.get("quota_reset_at"),
            "auto_concurrency_quota_minutes_to_reset": auto_concurrency_snapshot.get("quota_minutes_to_reset"),
            "codex_home": self.settings.codex_home or "default",
            "codex_profile": self.settings.codex_profile or "default",
            "codex_model": self._effective_codex_setting("model"),
            "codex_reasoning_effort": self._effective_codex_setting("model_reasoning_effort"),
            "codex_retry_attempts": self.settings.codex_retry_attempts,
            "goal_mode": int(self.settings.goal_mode),
            "agent_burst_used": agent_burst_used,
            "agent_burst_limit": agent_burst_limit,
            "agent_burst_reserve": agent_burst_reserve,
            "agent_burst_work_limit": agent_burst_work_limit,
            "agent_burst_remaining": max(0, agent_burst_work_limit - agent_burst_used) if agent_burst_work_limit else 0,
            "agent_burst_resume_after_seconds": self.settings.agent_burst_resume_after_seconds,
            "codex_timeout_seconds": self.settings.codex_timeout_seconds,
            "codex_idle_timeout_seconds": self.settings.codex_idle_timeout_seconds,
            "godot_timeout_seconds": self.settings.godot_timeout_seconds,
            "auto_hibernate_on_resource_block": int(self.settings.auto_hibernate_on_resource_block),
            "resource_block_resume_after_seconds": self.settings.resource_block_resume_after_seconds,
            "watchdog_orphan_resume_after_seconds": self.settings.watchdog_orphan_resume_after_seconds,
            "scheduled_auto_resumes": len(self._auto_resume_tasks),
            "scheduler_deferred_retry_seconds": self.settings.scheduler_deferred_retry_seconds,
            "scheduled_deferred_resumes": len(self._deferred_parent_tasks),
            "visible_total": len(visible_tasks),
            "visible_done": visible_counts[TaskStatus.DONE.value],
            "archived_done_nodes": len(archived_done_node_ids),
            "done_archive_parent_count": len(done_archive_summaries),
            "compact_done": int(compact_done),
            "obligations": self._tree_obligation_board(tasks),
            # B 部分：按角色分层的实际配比 + 自评校准读数（都是读数，不触发任何自动动作）
            "model_tiers": self._model_tier_board(),
            "self_assessment": calibration.summarize(tasks),
            "failures": self._tree_failure_board(tasks),
            "constitution": self._tree_constitution_board(db, tasks, selected_root_id or ""),
        }
        task_ids = {task.id for task in tasks}
        if task_ids:
            events = list(
                db.scalars(
                    select(EventRecord)
                    .where(EventRecord.task_id.in_(task_ids))
                    .order_by(EventRecord.created_at.desc())
                    .limit(100)
                ).all()
            )
        else:
            events = []
        return {
            "tasks": serialized_tasks,
            "edges": [
                {"source": task.parent_id, "target": task.id}
                for task in visible_tasks
                if task.parent_id and task.parent_id not in archived_done_node_ids
            ],
            "stats": stats,
            "events": [self.serialize_event(event) for event in events],
            "usage": self._usage_summary_for_task_ids(db, [task.id for task in tasks]),
            "constitution": self.serialize_constitution(constitution) if constitution else None,
        }

    def _tree_failure_board(self, tasks: list[TaskNode]) -> dict[str, Any]:
        """R6：整棵树的失败面（按 `category` / `retryable` / `parent_action` 聚合 + 有界明细）。

        与 `_tree_obligation_board` 并列放在 `stats` 里：看板要能一眼回答"这棵树在哪些类别上失败过"，
        而不是让人去翻每个节点的 `node_meta`。
        """
        return failure_retrospective.board(tasks)

    def _tree_constitution_board(self, db: Session, tasks: list[TaskNode], root_id: str) -> dict[str, Any]:
        """宪法传播看板（`docs/spec-constitution-amendment.md` §9 / K7）。

        三个数字来自三处不同的事实源，刻意不合并：节点 → 版本分布与落后分桶；
        债务表 → 再验证队列长度；事件流 → 最近一次修订。看板的意义就在于它们是**可核对的**，
        不是谁自己报的。
        """
        head = self._constitution_head_record_for_root(db, root_id)
        event = db.scalars(
            select(EventRecord)
            .where(EventRecord.task_id == root_id, EventRecord.type == "constitution.amended")
            .order_by(EventRecord.created_at.desc())
        ).first()
        payload = loads(event.payload_json, {}) if event is not None else {}
        last_amendment = (
            {
                "from_version": payload.get("from_version"),
                "to_version": payload.get("to_version"),
                "direction": payload.get("direction"),
                "actor": payload.get("actor"),
                "affected_by_status": payload.get("affected_by_status"),
                "at": event.created_at.isoformat() if event.created_at else None,
            }
            if event is not None
            else None
        )
        obligations_board = self._tree_obligation_board(tasks)
        return constitution_versions.version_board(
            tasks,
            effective_version=head.version if head else None,
            effective_hash=head.content_hash if head else None,
            revalidate_queue_depth=int(obligations_board.get("revalidate_queue_depth") or 0),
            last_amendment=last_amendment,
        )

    def _tree_obligation_board(self, tasks: list[TaskNode]) -> dict[str, Any]:
        """树级债务看板（`docs/spec-parent-child-obligations.md` §9）。

        债务是记在**父节点**的 `node_meta["obligations"]` 上的，所以"整棵树有没有被卡"必须
        逐节点汇总再合并——单个节点答不了这个问题，而逐个节点去翻 JSON 等于没有看板。

        字段取舍见 `obligations.summarize`：`oldest_blocking_age_seconds` 是"在干活"与"卡死"
        的唯一区分，`settled_by_human_count` 是"放宽"的可见计量。
        """
        summaries: list[dict[str, Any]] = []
        for task in tasks:
            meta = loads(task.node_meta_json, {})
            items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
            if items:
                summaries.append(obligations.summarize(items))
        return obligations.merge_summaries(summaries)

    def _usage_summary_for_task_ids(self, db: Session, task_ids: list[str]) -> dict[str, Any]:
        """汇总这些节点所有 run 的用量与成本。

        `usage_json` 里存的是**供应商原始负载**，规范化在读取时做（`app.usage`），
        因此改进规范化逻辑会对历史数据一并生效——切换供应商时这一点很关键。
        """
        if not task_ids:
            return {}
        raw_rows = db.scalars(select(RunRecord.usage_json).where(RunRecord.task_id.in_(task_ids))).all()
        payloads: list[Any] = []
        for raw in raw_rows:
            parsed = loads(raw, None) if raw else None
            if isinstance(parsed, dict) and parsed:
                payloads.append(parsed)
        return summarize_usages(payloads)

    def _effective_codex_setting(self, key: str) -> str:
        """报告**实际生效**的 Codex 配置值。

        `CODEX_BATTLEFIELD_CODEX_MODEL` 为空时，模型由隔离 `CODEX_HOME` 的 `config.toml`
        决定（Hub 模式正是如此）。若只报"默认"，看板就无法回答"现在跑在哪个模型上"，
        因此这里把 `config.toml` 里的值读出来。
        """
        env_value = self.settings.codex_model if key == "model" else self.settings.codex_reasoning_effort
        if env_value:
            return env_value
        home = (self.settings.codex_home or "").strip()
        if not home:
            return "默认（由 ~/.codex 决定）"
        try:
            text = (Path(home) / "config.toml").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "默认"
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _separator, value = stripped.partition("=")
            if name.strip() != key:
                continue
            cleaned = value.strip().strip('"').strip("'")
            if cleaned:
                return cleaned
        return "默认"

    def _godot_resource_state(self) -> dict[str, Any]:
        """真实 Godot 槽位的容量与排队情况，供看板与监护者显示。

        `capacity` 取自配置（权威）；`holders` / `waiters` 来自包装器写的状态文件，
        属尽力而为的观测值，可能滞后，故一并给出 `updated_at`。
        """
        capacity = max(1, int(self.settings.godot_max_concurrency))
        state_path = machine_root_of(self.settings) / "godot.resource.state.json"
        payload: dict[str, Any] = {
            "capacity": capacity,
            "holders": [],
            "waiters": [],
            "queue_policy": None,
            "holder_count": 0,
            "waiter_count": 0,
            "observed": False,
            "state_path": str(state_path),
            "updated_at": None,
            "stale_seconds": None,
        }
        try:
            raw = state_path.read_text(encoding="utf-8")
        except OSError:
            return payload
        state = loads(raw, {})
        if not isinstance(state, dict):
            return payload
        holders = state.get("holders")
        waiters = state.get("waiters")
        holders = holders if isinstance(holders, list) else []
        waiters = waiters if isinstance(waiters, list) else []
        updated_at = state.get("updated_at")
        stale_seconds = None
        try:
            if updated_at is not None:
                stale_seconds = max(0, int(utcnow().timestamp() - float(updated_at)))
        except (TypeError, ValueError, OSError):
            stale_seconds = None
        payload.update(
            {
                "capacity": max(1, int(state.get("capacity") or capacity)),
                "holders": holders,
                "waiters": waiters,
                # 包装器写的是"当前生效的队列策略"（优先级是否启用 + 老化秒数）；缺了就留 None，
                # 不要用默认值冒充事实。
                "queue_policy": state.get("queue_policy") if isinstance(state.get("queue_policy"), dict) else None,
                "holder_count": len(holders),
                "waiter_count": len(waiters),
                "observed": True,
                "updated_at": updated_at,
                "stale_seconds": stale_seconds,
            }
        )
        return payload

    def get_watchdog_state(self, db: Session, root_id: str | None = None) -> dict[str, Any]:
        self._sync_live_external_agent_statuses(db=db)
        all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        selected_root_id, _selection = self._select_watchdog_root(all_tasks, root_id)
        if selected_root_id is None and not self.settings.frozen_mode:
            restored_root_id = self._restore_snapshot_if_missing(db, root_id)
            if restored_root_id:
                all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        selected_root_id, selection = self._select_watchdog_root(all_tasks, root_id)
        tasks = self._subtree(all_tasks, selected_root_id) if selected_root_id else []
        counts = Counter(task.status for task in tasks)
        blocker_counts = self._blocker_visibility_counts(tasks)
        root = next((task for task in tasks if task.id == selected_root_id), None)
        parent_ids = {task.parent_id for task in tasks if task.parent_id}
        active_frontier = sum(1 for task in tasks if task.id not in parent_ids and task.status in ACTIVE_FRONTIER_STATUSES)
        active_agent_statuses = sum(1 for task in tasks if task.status in ACTIVE_AGENT_STATUSES)
        codex_slots_active = self._codex_slots_active()
        active_agents = max(active_agent_statuses, codex_slots_active)
        last_activity_at = max((self._coerce_utc(task.updated_at) for task in tasks if task.updated_at), default=None)
        last_activity_age_seconds = self._seconds_since(last_activity_at)
        resume_at: datetime | None = None
        reason = ""
        unreadable_hibernation_task_ids: list[str] = []
        repaired_hibernation_meta_task_ids: list[str] = []
        for task in tasks:
            if task.status != TaskStatus.HIBERNATING.value:
                continue
            meta = loads(task.node_meta_json, {})
            hibernation = meta.get("hibernation") if isinstance(meta.get("hibernation"), dict) else {}
            if selected_root_id and hibernation.get("owner_id") != selected_root_id:
                continue
            if str(hibernation.get("status") or "") == "resumed":
                hibernation["status"] = "scheduled"
                hibernation["status_repaired_at"] = utcnow().isoformat()
                meta["hibernation"] = hibernation
                task.node_meta_json = dumps(meta)
                repaired_hibernation_meta_task_ids.append(task.id)
            resume_at_raw = str(hibernation.get("resume_at") or "")
            if resume_at_raw:
                try:
                    parsed_resume_at = datetime.fromisoformat(resume_at_raw)
                except ValueError:
                    parsed_resume_at = utcnow()
                parsed_resume_at = self._coerce_utc(parsed_resume_at) or utcnow()
                if resume_at is None or parsed_resume_at < resume_at:
                    resume_at = parsed_resume_at
            if not reason:
                reason = repair_mojibake_text(str(hibernation.get("reason") or ""))
            if self._is_unreadable_reason(repair_mojibake_text(str(hibernation.get("reason") or ""))):
                unreadable_hibernation_task_ids.append(task.id)
        if repaired_hibernation_meta_task_ids and selected_root_id and not self.settings.frozen_mode:
            self._add_event(
                db,
                selected_root_id,
                "watchdog.hibernation_meta_repaired",
                "Watchdog normalized hibernation metadata whose status said resumed while the task row was still hibernating.",
                {
                    "repaired_count": len(repaired_hibernation_meta_task_ids),
                    "sample_task_ids": repaired_hibernation_meta_task_ids[:10],
                },
            )
            db.commit()

        resume_due = bool(resume_at and resume_at <= utcnow())
        root_is_resumable = bool(
            root
            and root.status not in {status.value for status in TERMINAL_STATUSES}
            and root.status not in STOPPED_STATUSES
        )
        orphaned_active_tree = bool(
            selected_root_id
            and root_is_resumable
            and active_agent_statuses == 0
            and codex_slots_active == 0
            and counts[TaskStatus.HIBERNATING.value] == 0
            and active_frontier > 0
            and last_activity_age_seconds >= max(0, self.settings.watchdog_orphan_resume_after_seconds)
        )
        machine_pressure, _machine_payload = self._machine_safety_pressure()
        budget_guard = self._active_budget_guard(selected_root_id)
        budget_guard_active = bool(budget_guard)
        budget_guard_resume_at = self._coerce_utc(budget_guard["resume_at"]) if budget_guard else None
        budget_guard_reason = str(budget_guard.get("reason") or "") if budget_guard else ""
        machine_safety_recovered = False
        hook_state = self._watchdog_hook_state(selected_root_id)
        if (
            counts[TaskStatus.HIBERNATING.value] > 0
            and reason
            and "machine safety guard" in reason.lower()
            and not machine_pressure
        ):
            machine_safety_recovered = True
            resume_due = True
            if resume_at is None or resume_at > utcnow():
                resume_at = utcnow()
            reason = "Machine safety guard recovered: local resources are back above the configured threshold; watchdog may resume early."
        if (
            budget_guard_active
            and selected_root_id
            and budget_guard_reason
            and "machine safety guard" in budget_guard_reason.lower()
            and not machine_pressure
            and not self.settings.frozen_mode
        ):
            self.clear_budget_guard(selected_root_id)
            budget_guard = None
            budget_guard_active = False
            budget_guard_resume_at = None
            budget_guard_reason = ""
            machine_safety_recovered = True
            resume_due = True
            if resume_at is None or resume_at > utcnow():
                resume_at = utcnow()
            reason = "Machine safety guard recovered: local resources are back above the configured threshold; watchdog may resume early."
        if (
            counts[TaskStatus.HIBERNATING.value] > 0
            and reason
            and not machine_pressure
            and not budget_guard_active
            and self._hibernation_reason_is_codex_resource_wait(reason)
            and self._codex_health_allows_resource_resume(hook_state)
        ):
            resume_due = True
            if resume_at is None or resume_at > utcnow():
                resume_at = utcnow()
            reason = "Codex resource hibernation recovered: the latest health check no longer reports an active quota/resource block; watchdog may resume early."
        should_resume = bool((resume_due or orphaned_active_tree) and active_agent_statuses == 0 and codex_slots_active == 0 and selected_root_id)
        if self.settings.frozen_mode:
            resume_due = False
            orphaned_active_tree = False
            should_resume = False
        machine_safety_waiting = bool(
            machine_pressure and active_agent_statuses == 0 and codex_slots_active == 0
        )
        if machine_safety_waiting:
            resume_due = False
            orphaned_active_tree = False
            should_resume = False
            if not reason or "machine safety guard" in reason.lower():
                reason = (
                    "Machine safety guard is still active: available memory remains below the configured threshold; "
                    "watchdog will resume the tree automatically after local memory recovers."
                )
        if budget_guard_active and active_agent_statuses == 0 and codex_slots_active == 0:
            should_resume = False
            resume_due = False
            orphaned_active_tree = False
            if budget_guard_resume_at and (resume_at is None or budget_guard_resume_at > resume_at):
                resume_at = budget_guard_resume_at
            if budget_guard_reason:
                reason = budget_guard_reason
        if orphaned_active_tree and not reason:
            reason = "任务树仍有可执行前线，但后台调度可能在服务重启后丢失；看门狗准备自动恢复。"
        agent_burst_used = self._agent_burst_used_for_root(db, selected_root_id) if selected_root_id else 0
        agent_burst_limit = self._agent_burst_limit()
        agent_burst_reserve = self._agent_burst_effective_reserve()
        agent_burst_work_limit = self._agent_burst_work_limit()
        active_agent_diagnostics = self._active_agent_diagnostics(db, tasks)
        active_resource_pressure, active_resource_pressure_reason = self._active_resource_pressure_from_logs(tasks)
        l4_fuel_snapshot: dict[str, Any] = {}
        if selected_root_id:
            try:
                l4_fuel_snapshot = self._auto_concurrency_decision(db, selected_root_id)
            except KeyError:
                l4_fuel_snapshot = {}
        reported_codex_capacity = self._reported_codex_slots_capacity(
            codex_slots_active=codex_slots_active,
            auto_concurrency_snapshot=l4_fuel_snapshot,
        )
        if not active_resource_pressure and hook_state.get("codex_health_status") == "resource_block":
            reset_at = self._coerce_utc(hook_state.get("codex_quota_reset_at"))
            reset_text = reset_at.isoformat() if reset_at else "unknown"
            active_resource_pressure = True
            active_resource_pressure_reason = f"Codex health observed a quota retry-after window; reset_at={reset_text}"
        hibernating_count = counts[TaskStatus.HIBERNATING.value]
        root_can_run = bool(
            root is not None
            and root.status not in {status.value for status in TERMINAL_STATUSES}
            and root.status not in STOPPED_STATUSES
        )
        has_topoff_headroom = bool(
            selected_root_id
            and root_can_run
            and not self.settings.frozen_mode
            and not machine_safety_waiting
            and not active_resource_pressure
            and not budget_guard_active
            and hibernating_count == 0
            and codex_slots_active < max(1, reported_codex_capacity)
            and active_frontier > codex_slots_active
        )
        if machine_safety_waiting:
            recommended_action = "machine_safety_sleep"
        elif budget_guard_active and active_agent_statuses == 0 and codex_slots_active == 0:
            recommended_action = "budget_guard_sleep"
        elif orphaned_active_tree:
            recommended_action = "resume_orphaned"
        elif should_resume:
            recommended_action = "resume"
        elif has_topoff_headroom:
            recommended_action = "top_off"
        elif active_agent_statuses > 0 or codex_slots_active > 0:
            recommended_action = "observe_running"
        elif hibernating_count > 0:
            recommended_action = "wait_for_resume_at"
        elif selected_root_id:
            recommended_action = "observe"
        else:
            recommended_action = "idle"
        if reason or hibernating_count > 0 or resume_due or orphaned_active_tree:
            reason = self._normalize_watchdog_reason(reason, agent_burst_used, agent_burst_work_limit, agent_burst_reserve)
        else:
            reason = ""
        if budget_guard_active:
            budget_warning_level = "sleeping"
            budget_warning_message = (
                f"预算守卫已开启：{budget_guard_reason or '余额不足，低功耗等待账户额度刷新。'}"
                f" 下一次允许恢复时间：{budget_guard_resume_at.isoformat() if budget_guard_resume_at else '未定'}。"
            )
        else:
            budget_warning_level, budget_warning_message = self._budget_warning_state(
                agent_burst_used,
                agent_burst_limit,
                agent_burst_work_limit,
                hibernating_count,
            )
        if active_resource_pressure and not budget_guard_active:
            budget_warning_level = "resource_pressure"
            budget_warning_message = (
                f"检测到活跃 Codex CLI 出现额度/限流信号：{active_resource_pressure_reason}"
                " 当前运行中的 agent 不会被强制中断；下一次调度会进入低功率冷却。"
            )
        if unreadable_hibernation_task_ids and not self.settings.frozen_mode and not self._is_unreadable_reason(reason):
            bad_ids = set(unreadable_hibernation_task_ids)
            for task in tasks:
                if task.id not in bad_ids:
                    continue
                meta = loads(task.node_meta_json, {})
                hibernation = meta.get("hibernation") if isinstance(meta.get("hibernation"), dict) else {}
                hibernation["reason"] = reason
                hibernation["reason_repaired_at"] = utcnow().isoformat()
                meta["hibernation"] = hibernation
                task.node_meta_json = dumps(meta)
            db.commit()
        if selected_root_id and not self.settings.frozen_mode:
            try:
                if self._watchdog_state_snapshot_due(selected_root_id):
                    self._write_state_snapshot(db, selected_root_id, "watchdog.state", keep_history=False)
                    if not (has_topoff_headroom or should_resume or orphaned_active_tree):
                        self._maybe_write_full_checkpoint(db, selected_root_id, "watchdog.hourly")
            except Exception:
                db.rollback()
                pass
        guardian_state = self._l4_guardian_state(
            selected_root_id=selected_root_id,
            root_status=root.status if root else None,
            counts=counts,
            active_agents=active_agents,
            active_agent_statuses=active_agent_statuses,
            codex_slots_active=codex_slots_active,
            codex_slots_capacity=reported_codex_capacity,
            active_frontier=active_frontier,
            hibernating_count=hibernating_count,
            last_activity_age_seconds=last_activity_age_seconds,
            budget_guard_active=budget_guard_active,
            budget_guard_resume_at=budget_guard_resume_at,
            active_resource_pressure=active_resource_pressure,
            active_resource_pressure_reason=active_resource_pressure_reason,
            orphaned_active_tree=orphaned_active_tree,
            should_resume=should_resume,
            recommended_action=recommended_action,
            active_agent_diagnostics=active_agent_diagnostics,
            blocker_counts=blocker_counts,
        )
        return {
            "root_id": selected_root_id,
            "requested_root_id": root_id,
            "selection": selection,
            "root_status": root.status if root else None,
            "active_agents": active_agents,
            "active_agent_statuses": active_agent_statuses,
            "codex_slots_active": codex_slots_active,
            "codex_slots_capacity": reported_codex_capacity,
            "hibernating": hibernating_count,
            "active_frontier": active_frontier,
            "resume_due": resume_due,
            "orphaned_active_tree": orphaned_active_tree,
            "should_resume": should_resume,
            "resume_at": resume_at,
            "last_activity_at": last_activity_at,
            "last_activity_age_seconds": last_activity_age_seconds,
            "reason": reason,
            "goal_mode": self.settings.goal_mode,
            "budget_warning_level": budget_warning_level,
            "budget_warning_message": budget_warning_message,
            "active_resource_pressure": active_resource_pressure,
            "active_resource_pressure_reason": active_resource_pressure_reason,
            "blocked_active": blocker_counts["active_blocked"],
            "blocked_superseded": blocker_counts["superseded_blocked"],
            "blocked_under_repair": blocker_counts["repair_pending_blocked"],
            "blocked_repaired": blocker_counts["repair_done_blocked"],
            "blocked_inactive_branch": blocker_counts["inactive_branch_blocked"],
            "blocked_resource_wait": blocker_counts["resource_wait_blocked"],
            "machine_safety_recovered": machine_safety_recovered,
            "budget_guard_active": budget_guard_active,
            "budget_guard_resume_at": budget_guard_resume_at,
            "budget_guard_reason": budget_guard_reason,
            "agent_burst_used": agent_burst_used,
            "agent_burst_limit": agent_burst_limit,
            "agent_burst_reserve": agent_burst_reserve,
            "agent_burst_work_limit": agent_burst_work_limit,
            "agent_burst_remaining": max(0, agent_burst_work_limit - agent_burst_used) if agent_burst_work_limit else 0,
            "acceptance_repair_cycle_limit": self._acceptance_repair_cycle_limit(),
            "godot_resource": self._godot_resource_state(),
            "worktrees": self._worktree_lifecycle_state(db, all_tasks),
            "model_tiers": self._model_tier_board(),
            "recommended_action": recommended_action,
            "l4_fuel_mode": l4_fuel_snapshot.get("mode"),
            "l4_fuel_target": l4_fuel_snapshot.get("target"),
            "l4_fuel_desired": l4_fuel_snapshot.get("desired"),
            "l4_fuel_current_capacity": l4_fuel_snapshot.get("current_capacity"),
            "l4_fuel_phase": l4_fuel_snapshot.get("fuel_phase"),
            "l4_fuel_score": l4_fuel_snapshot.get("fuel_score"),
            "l4_fuel_basis": l4_fuel_snapshot.get("fuel_basis"),
            "l4_fuel_recommended_target": l4_fuel_snapshot.get("fuel_recommended_target"),
            "l4_fuel_open_frontier": l4_fuel_snapshot.get("fuel_open_frontier"),
            "l4_fuel_time_pressure": l4_fuel_snapshot.get("fuel_time_pressure"),
            "l4_fuel_minutes_to_deadline": l4_fuel_snapshot.get("fuel_minutes_to_deadline"),
            "l4_fuel_deadline_local": l4_fuel_snapshot.get("fuel_deadline_local"),
            "l4_fuel_next_deadline_local": l4_fuel_snapshot.get("fuel_next_deadline_local"),
            "l4_fuel_deadline_elapsed": l4_fuel_snapshot.get("fuel_deadline_elapsed"),
            "l4_fuel_local_now": l4_fuel_snapshot.get("fuel_local_now"),
            "l4_fuel_sprint_window_minutes": l4_fuel_snapshot.get("fuel_sprint_window_minutes"),
            "l4_fuel_queue_pressure": l4_fuel_snapshot.get("fuel_queue_pressure"),
            "l4_fuel_frontier_pressure": l4_fuel_snapshot.get("fuel_frontier_pressure"),
            "l4_fuel_quota_observability": l4_fuel_snapshot.get("quota_observability"),
            "l4_fuel_quota_reset_at": l4_fuel_snapshot.get("quota_reset_at"),
            "l4_fuel_quota_minutes_to_reset": l4_fuel_snapshot.get("quota_minutes_to_reset"),
            "l4_fuel_reason": l4_fuel_snapshot.get("reason"),
            "active_agent_diagnostics": active_agent_diagnostics,
            **guardian_state,
            **hook_state,
        }

    def _l4_guardian_state(
        self,
        *,
        selected_root_id: str | None,
        root_status: str | None,
        counts: Counter[str],
        active_agents: int,
        active_agent_statuses: int,
        codex_slots_active: int,
        codex_slots_capacity: int,
        active_frontier: int,
        hibernating_count: int,
        last_activity_age_seconds: int,
        budget_guard_active: bool,
        budget_guard_resume_at: datetime | None,
        active_resource_pressure: bool,
        active_resource_pressure_reason: str,
        orphaned_active_tree: bool,
        should_resume: bool,
        recommended_action: str,
        active_agent_diagnostics: list[dict[str, Any]],
        blocker_counts: dict[str, int],
    ) -> dict[str, Any]:
        machine_pressure, machine_payload = self._machine_safety_pressure()
        stalled_agents = [item for item in active_agent_diagnostics if item.get("severity") == "stalled"]
        warning_agents = [item for item in active_agent_diagnostics if item.get("severity") == "warning"]
        external_slots = max(0, codex_slots_active - active_agent_statuses)
        blocked_count = blocker_counts.get("active_blocked", counts[TaskStatus.BLOCKED.value])
        superseded_blocked_count = blocker_counts.get("superseded_blocked", 0)
        repair_pending_blocked_count = blocker_counts.get("repair_pending_blocked", 0)
        repair_done_blocked_count = blocker_counts.get("repair_done_blocked", 0)
        queued_count = counts[TaskStatus.QUEUED.value] + counts[TaskStatus.PLANNED.value] + counts[TaskStatus.REPAIR.value]
        waiting_count = counts[TaskStatus.WAITING_CHILDREN.value]
        updated_at = utcnow()

        checks: list[dict[str, Any]] = []

        def add_check(key: str, label: str, status: str, detail: str, action: str = "observe") -> None:
            checks.append(
                {
                    "key": key,
                    "label": label,
                    "status": status,
                    "detail": repair_mojibake_text(detail),
                    "action": action,
                }
            )

        if selected_root_id:
            add_check(
                "mission",
                "目标守护",
                "ok" if root_status not in {TaskStatus.FAILED.value, TaskStatus.CANCELED.value} else "critical",
                f"正在监护根任务 {selected_root_id}；根状态 {root_status or 'unknown'}。",
            )
        else:
            add_check("mission", "目标守护", "idle", "当前没有可监护的根任务。")

        if machine_pressure:
            available = machine_payload.get("available_memory_mb")
            threshold = machine_payload.get("safe_min_available_memory_mb")
            add_check(
                "machine",
                "机器安全",
                "critical",
                f"可用内存 {available} MB，低于安全线 {threshold} MB；应暂停新启动并等待恢复。",
                "hibernate_low_power",
            )
        else:
            available = machine_payload.get("available_memory_mb")
            detail = "机器资源探测正常。" if available is None else f"可用内存约 {available} MB，未触发安全线。"
            add_check("machine", "机器安全", "ok", detail)

        if stalled_agents:
            add_check(
                "agent_progress",
                "Agent 进展",
                "critical",
                f"{len(stalled_agents)} 个活跃 agent 超过空闲阈值没有进展，需要等待 runner 重试或上报。",
                "watch_stalled_agent",
            )
        elif warning_agents:
            add_check(
                "agent_progress",
                "Agent 进展",
                "warning",
                f"{len(warning_agents)} 个活跃 agent 输出变慢；只观察，不强杀。",
                "observe_slow_agent",
            )
        elif active_agent_statuses > 0:
            add_check("agent_progress", "Agent 进展", "ok", f"{active_agent_statuses} 个节点处于 agent 执行状态，最近仍有可观测状态。")
        elif codex_slots_active > 0:
            add_check(
                "agent_progress",
                "Agent 进展",
                "ok",
                f"{codex_slots_active} 个真实 Codex CLI 进程正在占槽；节点状态可能处于跨重启恢复或排队占位，按真实槽位继续观察。",
            )
        else:
            add_check("agent_progress", "Agent 进展", "idle", "当前没有正在消耗模型的 agent。")

        if active_resource_pressure:
            add_check(
                "quota",
                "流量/额度",
                "warning",
                active_resource_pressure_reason or "活跃日志里出现额度或限流信号；下一轮调度应低功耗。",
                "low_power",
            )
        elif budget_guard_active:
            resume_text = budget_guard_resume_at.isoformat() if budget_guard_resume_at else "未定"
            add_check("quota", "流量/额度", "sleeping", f"预算守卫已生效，下一次允许恢复时间：{resume_text}。", "sleep")
        else:
            add_check("quota", "流量/额度", "ok", "没有检测到新的限流信号；Codex CLI 仍没有直接额度查询接口。")

        if codex_slots_active > max(1, codex_slots_capacity) or external_slots > 0:
            add_check(
                "process_slots",
                "进程槽位",
                "warning" if active_agents > 0 else "critical",
                f"Codex 槽位 {codex_slots_active}/{codex_slots_capacity}，其中外部遗留或跨重启进程 {external_slots} 个。",
                "observe_slots",
            )
        else:
            add_check(
                "process_slots",
                "进程槽位",
                "ok",
                f"Codex 槽位 {codex_slots_active}/{codex_slots_capacity}，仍有余量；外部遗留进程 {external_slots} 个。",
            )

        if orphaned_active_tree or should_resume:
            add_check(
                "scheduler",
                "续跑调度",
                "warning",
                "任务树有可执行前线但当前没有活跃 agent，watchdog 可以接力恢复。",
                "resume",
            )
        elif hibernating_count > 0:
            add_check("scheduler", "续跑调度", "sleeping", f"{hibernating_count} 个节点处于休眠，等待恢复时间。", "wait")
        elif active_frontier > 0:
            add_check(
                "scheduler",
                "续跑调度",
                "ok",
                f"前线 {active_frontier}，排队 {queued_count}，等待父节点 {waiting_count}；调度器仍有可推进工作。",
            )
        else:
            add_check("scheduler", "续跑调度", "idle", "暂无可推进前线。")

        if blocked_count > 0:
            add_check(
                "blockers",
                "Blockers",
                "warning",
                f"{blocked_count} active blocker(s) still need triage; parent nodes should split retryable, skill-recovery, and human-action cases.",
                "triage_blockers",
            )
        elif superseded_blocked_count > 0:
            add_check(
                "blockers",
                "Blockers",
                "ok",
                (
                    f"{superseded_blocked_count} historical blocker(s) are delegated to repair branches; "
                    f"{repair_pending_blocked_count} still wait for repair convergence, "
                    f"{repair_done_blocked_count} are covered by completed repair children."
                ),
            )
        else:
            add_check("blockers", "Blockers", "ok", "No active blocker nodes.")

        status_order = {"critical": 4, "warning": 3, "sleeping": 2, "ok": 1, "idle": 0}
        top_status = max((check["status"] for check in checks), key=lambda item: status_order.get(str(item), 0), default="idle")
        if machine_pressure or stalled_agents:
            guardian_status = "needs_attention"
            guardian_mode = "protect_machine" if machine_pressure else "watch_stalled_agent"
            risk_level = "critical"
            summary = "L4 监护者发现高风险信号：优先保护机器和任务状态，不扩大并发。"
        elif budget_guard_active or hibernating_count > 0:
            guardian_status = "sleeping"
            guardian_mode = "low_power_sleep"
            risk_level = "sleeping"
            summary = "L4 监护者处于低功耗等待：保存状态，等待额度或资源恢复。"
        elif active_resource_pressure:
            guardian_status = "low_power"
            guardian_mode = "resource_cooldown"
            risk_level = "warning"
            summary = "L4 监护者检测到限流/额度压力：继续观察当前 agent，下一轮降低并发。"
        elif orphaned_active_tree or should_resume:
            guardian_status = "recovery_ready"
            guardian_mode = "resume_ready"
            risk_level = "watch"
            summary = "L4 监护者发现可恢复前线：适合由 watchdog 接力恢复。"
        elif active_agents > 0 or codex_slots_active > 0:
            guardian_status = "watching"
            guardian_mode = "observe_running"
            risk_level = "watch" if top_status == "warning" else "ok"
            summary = "L4 监护者正在随车观察：有 agent 在跑，不打断，只看进展和资源。"
        elif active_frontier > 0:
            guardian_status = "ready"
            guardian_mode = "ready_to_schedule"
            risk_level = "ok"
            summary = "L4 监护者认为还有可推进前线，可以继续调度。"
        else:
            guardian_status = "idle"
            guardian_mode = "idle"
            risk_level = "ok"
            summary = "L4 监护者未发现需要动作的前线。"

        state = {
            "guardian_level": "L4",
            "guardian_status": guardian_status,
            "guardian_mode": guardian_mode,
            "guardian_risk_level": risk_level,
            "guardian_summary": summary,
            "guardian_recommended_action": recommended_action,
            "guardian_state_path": str(machine_root_of(self.settings) / "l4_guardian_state.json"),
            "guardian_updated_at": updated_at,
            "guardian_checks": checks,
            "last_activity_age_seconds": last_activity_age_seconds,
        }
        if not self.settings.frozen_mode:
            try:
                path = machine_root_of(self.settings) / "l4_guardian_state.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            except OSError:
                pass
        return state

    def _watchdog_hook_state(self, root_id: str | None) -> dict[str, Any]:
        return watchdog.watchdog_hook_state(ROOT_DIR, self.settings, root_id)

    def _codex_health_state(self) -> dict[str, Any]:
        return watchdog.codex_health_state(machine_root_of(self.settings))

    def _codex_health_summary_text(self, value: Any) -> str:
        return watchdog.codex_health_summary_text(value)

    def _powershell_exe(self) -> str:
        """看门狗的 PowerShell 入口（P6-2）。

        **为什么要有这个方法**：`install_watchdog_hook` / `run_watchdog_once` 原来直接写死
        `"powershell.exe"`。在 Linux/macOS 上那会抛 `FileNotFoundError: powershell.exe`
        —— 用户看到的是一句"找不到文件"，而不是"这个功能只有 Windows 有"。
        扫描（`scripts/_find_win_only.py` 那次）显示：仓库里只有这两处是**无平台守卫**的
        Windows-only 调用，其余（`taskkill` / `msvcrt`）都已经按平台分支。

        这里**不假装支持**非 Windows：看门狗由 PowerShell 脚本 + 计划任务组成，
        移植它属于另一件事。所以非 Windows 直接给出可行动的错误。
        """
        if os.name != "nt":
            raise RuntimeError(
                "看门狗钩子只有 Windows 支持：它由 PowerShell 脚本"
                "（`scripts/battlefield_watchdog.ps1`、`scripts/register_battlefield_watchdog_task.ps1`）"
                "与计划任务组成。Linux/macOS 上请用系统调度器（cron / systemd timer）"
                "跑等价的巡检入口。详见 `docs/cross-platform.md`。"
            )
        return "powershell.exe"

    def install_watchdog_hook(self, root_id: str | None = None) -> dict[str, Any]:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        selected_root_id = self._selected_watchdog_root_id(root_id)
        register_script = ROOT_DIR / "scripts" / "register_battlefield_watchdog_task.ps1"
        args = [
            self._powershell_exe(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(register_script),
        ]
        if selected_root_id:
            args.extend(["-RootId", selected_root_id])
        if self.settings.goal_mode:
            args.append("-GoalMode")
        if self.settings.codex_home:
            args.extend(["-CodexHome", self.settings.codex_home])
        if self.settings.codex_profile:
            args.extend(["-CodexProfile", self.settings.codex_profile])
        args.extend(
            [
                "-IntervalMinutes",
                str(self._watchdog_interval_minutes()),
                "-MaxConcurrency",
                str(self.settings.max_concurrency),
                "-AgentBurstLimit",
                str(self._agent_burst_limit()),
                "-AgentBurstReserve",
                str(self._agent_burst_effective_reserve()),
                "-ResumeAfterSeconds",
                str(self.settings.agent_burst_resume_after_seconds),
                "-CodexPreflightTimeoutSeconds",
                "25",
                "-AutoConcurrencyQueueThreshold",
                str(self.settings.auto_concurrency_queue_threshold),
                "-FuelDeadlineHour",
                str(self.settings.auto_concurrency_fuel_deadline_hour),
                "-FuelTimezoneOffsetHours",
                str(self.settings.auto_concurrency_fuel_timezone_offset_hours),
                "-FuelSprintWindowMinutes",
                str(self.settings.auto_concurrency_fuel_sprint_window_minutes),
                "-WithLogonTrigger",
            ]
        )
        return self._run_watchdog_command(args, selected_root_id, timeout_seconds=90)

    def run_watchdog_once(self, root_id: str | None = None) -> dict[str, Any]:
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        selected_root_id = self._selected_watchdog_root_id(root_id)
        watchdog_script = ROOT_DIR / "scripts" / "battlefield_watchdog.ps1"
        args = [
            self._powershell_exe(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(watchdog_script),
        ]
        if selected_root_id:
            args.extend(["-RootId", selected_root_id])
        if self.settings.goal_mode:
            args.append("-GoalMode")
        if self.settings.codex_home:
            args.extend(["-CodexHome", self.settings.codex_home])
        if self.settings.codex_profile:
            args.extend(["-CodexProfile", self.settings.codex_profile])
        args.extend(
            [
                "-MaxConcurrency",
                str(self.settings.max_concurrency),
                "-AgentBurstLimit",
                str(self._agent_burst_limit()),
                "-AgentBurstReserve",
                str(self._agent_burst_effective_reserve()),
                "-ResumeAfterSeconds",
                str(self.settings.agent_burst_resume_after_seconds),
                "-CodexPreflightTimeoutSeconds",
                "25",
                "-AutoConcurrencyQueueThreshold",
                str(self.settings.auto_concurrency_queue_threshold),
                "-FuelDeadlineHour",
                str(self.settings.auto_concurrency_fuel_deadline_hour),
                "-FuelTimezoneOffsetHours",
                str(self.settings.auto_concurrency_fuel_timezone_offset_hours),
                "-FuelSprintWindowMinutes",
                str(self.settings.auto_concurrency_fuel_sprint_window_minutes),
            ]
        )
        return self._run_watchdog_command(args, selected_root_id, timeout_seconds=90)

    def _selected_watchdog_root_id(self, root_id: str | None) -> str | None:
        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
        selected_root_id, _selection = self._select_watchdog_root(all_tasks, root_id)
        return selected_root_id or root_id

    def _run_watchdog_command(self, args: list[str], root_id: str | None, timeout_seconds: int) -> dict[str, Any]:
        command = subprocess.list2cmdline(args)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                args,
                cwd=ROOT_DIR,
                capture_output=True,
                timeout=timeout_seconds,
                creationflags=creationflags,
            )
            return {
                "status": "succeeded" if completed.returncode == 0 else "failed",
                "root_id": root_id,
                "command": command,
                "exit_code": completed.returncode,
                "stdout": self._command_output_text(completed.stdout),
                "stderr": self._command_output_text(completed.stderr),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "root_id": root_id,
                "command": command,
                "exit_code": None,
                "stdout": self._command_output_text(exc.stdout),
                "stderr": f"watchdog command timed out after {timeout_seconds}s\n{self._command_output_text(exc.stderr)}",
            }
        except OSError as exc:
            return {
                "status": "failed",
                "root_id": root_id,
                "command": command,
                "exit_code": None,
                "stdout": "",
                "stderr": str(exc),
            }

    def _command_output_text(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return repair_mojibake_text(value)
        encodings = [
            "utf-8",
            locale.getpreferredencoding(False),
            "gbk",
            "cp936",
            "mbcs",
        ]
        for encoding in dict.fromkeys(encodings):
            if not encoding:
                continue
            try:
                return repair_mojibake_text(value.decode(encoding))
            except (LookupError, UnicodeError):
                continue
        return repair_mojibake_text(value.decode("utf-8", errors="replace"))

    def _watchdog_interval_minutes(self) -> int:
        return watchdog.watchdog_interval_minutes(self.settings)

    def _select_watchdog_root(self, tasks: list[TaskNode], requested_root_id: str | None) -> tuple[str | None, str]:
        return watchdog.select_watchdog_root(tasks, requested_root_id)

    def _seconds_since(self, value: datetime | None) -> int:
        return runtime_logs.seconds_since(value)

    def _coerce_utc(self, value: datetime | None) -> datetime | None:
        return coerce_utc(value)

    def _active_agent_diagnostics(self, db: Session, tasks: list[TaskNode]) -> list[dict[str, Any]]:
        active_tasks = [task for task in tasks if task.status in ACTIVE_AGENT_STATUSES]
        if not active_tasks:
            return []

        task_ids = [task.id for task in active_tasks]
        latest_runs: dict[str, RunRecord] = {}
        for run in db.scalars(select(RunRecord).where(RunRecord.task_id.in_(task_ids)).order_by(RunRecord.id.desc())).all():
            latest_runs.setdefault(run.task_id, run)

        diagnostics: list[dict[str, Any]] = []
        for task in active_tasks:
            phase = self._agent_phase_for_status(task.status)
            run_dir = self.settings.workspace_dir / "runs" / task.id
            latest_run = latest_runs.get(task.id)
            prompt_path = run_dir / f"{phase}-prompt.txt"
            prompt_at = self._file_mtime_utc(prompt_path)
            progress_path, progress_at = self._latest_agent_progress_file(run_dir, phase, since=prompt_at)
            reference_at = progress_at or prompt_at or task.updated_at
            no_progress_seconds = self._seconds_since(reference_at)
            markers, tail_summary = self._agent_log_markers(run_dir / f"{phase}.jsonl", since=prompt_at)
            idle_timeout = max(0, int(self.settings.codex_idle_timeout_seconds))
            quiet_threshold = 120 if idle_timeout <= 0 else max(120, min(600, idle_timeout // 3))
            severity = "running"
            if progress_at is None and prompt_at is not None and no_progress_seconds >= 120:
                severity = "warning"
            if idle_timeout > 0 and no_progress_seconds >= idle_timeout:
                severity = "stalled"
            elif idle_timeout > 0 and no_progress_seconds >= int(idle_timeout * 0.8):
                severity = "warning"
            elif no_progress_seconds >= quiet_threshold:
                severity = "warning"
            elif any(marker in {"reconnecting", "http_fallback", "timeout"} for marker in markers) and no_progress_seconds >= 60:
                severity = "warning"

            if severity == "stalled":
                summary = "活跃 agent 已超过空闲看门狗阈值仍无新输出，可能是假活或模型/网络连接卡住；runner 会触发重试或上报。"
            elif progress_at is None and prompt_at is not None:
                summary = "Codex 进程已启动并写入 prompt，但当前阶段还没有产生日志或最终输出。"
            elif markers and severity == "warning":
                summary = "检测到 Codex 连接/重试信号，且最近输出放缓：" + " / ".join(markers)
            elif markers:
                summary = "最近仍有日志或输出文件活动；仅记录连接/重试信号：" + " / ".join(markers)
            elif no_progress_seconds >= quiet_threshold:
                if idle_timeout > 0:
                    remaining_seconds = max(0, idle_timeout - no_progress_seconds)
                    summary = (
                        f"进程仍在运行，但已有 {no_progress_seconds} 秒没有日志或输出文件更新；"
                        f"距离空闲看门狗重试阈值约 {remaining_seconds} 秒。"
                    )
                else:
                    summary = f"进程仍在运行，但已有 {no_progress_seconds} 秒没有日志或输出文件更新；当前未启用空闲重试阈值。"
            else:
                summary = "最近仍有日志或输出文件活动。"

            diagnostics.append(
                {
                    "task_id": task.id,
                    "title": repair_mojibake_text(task.title),
                    "status": task.status,
                    "depth": task.depth,
                    "phase": phase,
                    "severity": severity,
                    "summary": summary,
                    "last_task_update_at": task.updated_at,
                    "latest_run_id": latest_run.id if latest_run else None,
                    "latest_run_kind": latest_run.kind if latest_run else None,
                    "latest_run_status": latest_run.status if latest_run else None,
                    "latest_progress_path": str(progress_path) if progress_path else None,
                    "latest_progress_at": progress_at,
                    "prompt_path": str(prompt_path) if prompt_path.is_file() else None,
                    "prompt_at": prompt_at,
                    "no_progress_seconds": no_progress_seconds,
                    "idle_timeout_seconds": idle_timeout,
                    "markers": markers,
                    "tail_summary": tail_summary,
                }
            )
        return diagnostics

    def _agent_phase_for_status(self, status: str) -> str:
        return runtime_logs.agent_phase_for_status(status)

    def _latest_agent_progress_file(
        self,
        run_dir: Path,
        phase: str,
        *,
        since: datetime | None = None,
    ) -> tuple[Path | None, datetime | None]:
        since = self._coerce_utc(since)
        log_path = run_dir / f"{phase}.jsonl"
        candidates = [
            (run_dir / f"{phase}-activity.txt", self._file_mtime_utc(run_dir / f"{phase}-activity.txt")),
            (run_dir / f"{phase}-last-message.json", self._file_mtime_utc(run_dir / f"{phase}-last-message.json")),
            (log_path, self._latest_meaningful_agent_log_time(log_path)),
        ]
        existing = [(path, mtime) for path, mtime in candidates if path.is_file()]
        existing = [(path, mtime) for path, mtime in existing if mtime is not None]
        if since is not None:
            existing = [(path, mtime) for path, mtime in existing if self._coerce_utc(mtime) >= since]
        if not existing:
            return None, None
        return max(existing, key=lambda item: item[1])

    def _latest_meaningful_agent_log_time(self, log_path: Path) -> datetime | None:
        return runtime_logs.latest_meaningful_agent_log_time(log_path)

    @staticmethod
    def _codex_log_line_timestamp(line: str) -> datetime | None:
        return runtime_logs.codex_log_line_timestamp(line)

    def _file_mtime_utc(self, path: Path) -> datetime | None:
        return runtime_logs.file_mtime_utc(path)

    def _agent_log_markers(self, log_path: Path, *, since: datetime | None = None) -> tuple[list[str], str]:
        return runtime_logs.agent_log_markers(log_path, since=since)

    @staticmethod
    def _runtime_marker_candidate_lines(text: str) -> list[str]:
        return runtime_logs.runtime_marker_candidate_lines(text)

    @staticmethod
    def _runtime_marker_candidate_line(line: str) -> str:
        return runtime_logs.runtime_marker_candidate_line(line)

    @staticmethod
    def _has_timeout_marker(normalized: str) -> bool:
        return runtime_markers.has_timeout_marker(normalized)

    @staticmethod
    def _is_nonfatal_mcp_timeout_line(normalized_line: str) -> bool:
        return runtime_markers.is_nonfatal_mcp_timeout_line(normalized_line)

    @staticmethod
    def _has_resource_limit_marker(normalized: str) -> bool:
        return runtime_markers.has_resource_limit_marker(normalized)

    @staticmethod
    def _is_shell_tool_exit_output_line(normalized_line: str) -> bool:
        return runtime_markers.is_shell_tool_exit_output_line(normalized_line)

    @staticmethod
    def _shell_tool_exit_output_has_runtime_resource_error(normalized_line: str) -> bool:
        return runtime_markers.shell_tool_exit_output_has_runtime_resource_error(normalized_line)

    @staticmethod
    def _has_strong_resource_limit_marker(normalized_line: str) -> bool:
        return runtime_markers.has_strong_resource_limit_marker(normalized_line)

    @staticmethod
    def _is_local_codex_skill_failure_text(text: str) -> bool:
        return is_local_skill_failure(text)

    def _latest_root_id(self, tasks: list[TaskNode]) -> str | None:
        return watchdog.latest_root_id(tasks)

    def _runner_mode_for_task(self, task_id: str) -> str | None:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            cursor = task
            while cursor.parent_id:
                cursor = self._get_task(db, cursor.parent_id)
            meta = loads(cursor.node_meta_json, {})
            mode = meta.get("runner_mode")
            return str(mode) if mode in {"mock", "real"} else self.settings.runner_mode

    def _root_id_for_task(self, db: Session, task: TaskNode) -> str:
        cursor = task
        while cursor.parent_id:
            parent = db.get(TaskNode, cursor.parent_id)
            if parent is None:
                break
            cursor = parent
        return cursor.id

    def _resource_auto_hibernate_reason(self, reports: list[dict[str, Any]]) -> str:
        return watchdog.resource_auto_hibernate_reason(reports, self.settings.resource_block_resume_after_seconds)

    def _normalize_hibernation_reason(self, db: Session, root_id: str, reason: str) -> str:
        readable = repair_mojibake_text(str(reason or "").strip())
        if not self._is_unreadable_reason(readable):
            return readable
        used = self._agent_burst_used_for_root(db, root_id)
        return self._fallback_hibernation_reason(used, self._agent_burst_work_limit(), self._agent_burst_effective_reserve())

    def _normalize_watchdog_reason(self, reason: str, used: int, work_limit: int, reserve: int) -> str:
        return watchdog.normalize_watchdog_reason(reason, used, work_limit, reserve)

    def _budget_warning_state(self, used: int, limit: int, work_limit: int, hibernating: int) -> tuple[str, str]:
        return budget.budget_warning_state(self.settings, used, limit, work_limit, hibernating)

    def _is_unreadable_reason(self, text: str) -> bool:
        return watchdog.is_unreadable_reason(text)

    def _hibernation_reason_is_codex_resource_wait(self, reason: str) -> bool:
        return watchdog.hibernation_reason_is_codex_resource_wait(reason)

    def _codex_health_allows_resource_resume(self, hook_state: dict[str, Any]) -> bool:
        health_status = str(hook_state.get("codex_health_status") or "").lower()
        quota_observability = str(hook_state.get("codex_quota_observability") or "").lower()
        retry_after = hook_state.get("codex_quota_retry_after_seconds")
        if health_status == "resource_block":
            return False
        if "elapsed" in quota_observability:
            return True
        if health_status in {"ok", "warning"}:
            try:
                return retry_after is not None and int(retry_after) <= 0
            except (TypeError, ValueError):
                return True
        return False

    def _fallback_hibernation_reason(self, used: int, work_limit: int, reserve: int) -> str:
        return watchdog.fallback_hibernation_reason(used, work_limit, reserve)

    def _subtree(self, tasks: list[TaskNode], root_id: str) -> list[TaskNode]:
        by_parent: dict[str | None, list[TaskNode]] = {}
        by_id = {task.id: task for task in tasks}
        for task in tasks:
            by_parent.setdefault(task.parent_id, []).append(task)
        if root_id not in by_id:
            return []
        result: list[TaskNode] = []
        stack = [root_id]
        while stack:
            task_id = stack.pop()
            task = by_id[task_id]
            result.append(task)
            stack.extend(child.id for child in reversed(by_parent.get(task_id, [])))
        return sorted(result, key=lambda task: (task.depth, task.created_at))

    def _children_by_parent(self, tasks: list[TaskNode]) -> dict[str | None, list[TaskNode]]:
        children_by_parent: dict[str | None, list[TaskNode]] = {}
        for task in tasks:
            children_by_parent.setdefault(task.parent_id, []).append(task)
        return children_by_parent

    def _descendants_for_task(self, task_id: str, children_by_parent: dict[str | None, list[TaskNode]]) -> list[TaskNode]:
        descendants: list[TaskNode] = []
        stack = list(reversed(children_by_parent.get(task_id, [])))
        while stack:
            child = stack.pop()
            descendants.append(child)
            stack.extend(reversed(children_by_parent.get(child.id, [])))
        return descendants

    def _waiting_report_for_task(
        self,
        task: TaskNode,
        children_by_parent: dict[str | None, list[TaskNode]],
    ) -> dict[str, Any] | None:
        direct_children = children_by_parent.get(task.id, [])
        if task.status != TaskStatus.WAITING_CHILDREN.value or not direct_children:
            return None
        descendants = self._descendants_for_task(task.id, children_by_parent)
        direct_counts = Counter(child.status for child in direct_children)
        subtree_counts = Counter(child.status for child in descendants)
        active_descendants = [child for child in descendants if child.status in ACTIVE_AGENT_STATUSES]
        blocked_descendants = [
            child
            for child in descendants
            if child.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value, TaskStatus.REPAIR.value}
        ]
        queued_descendants = subtree_counts[TaskStatus.QUEUED.value]
        direct_total = len(direct_children)
        direct_done = direct_counts[TaskStatus.DONE.value]
        active_count = len(active_descendants)
        summary = (
            f"父节点等待子树收敛：直属子任务 {direct_done}/{direct_total} 完成，"
            f"真实运行叶子 Agent {active_count} 个，排队 {queued_descendants} 个，"
            f"阻塞/修复 {len(blocked_descendants)} 个。"
        )
        return {
            "role": "reporting_parent",
            "summary": summary,
            "direct_total": direct_total,
            "direct_done": direct_done,
            "direct_blocked": direct_counts[TaskStatus.BLOCKED.value]
            + direct_counts[TaskStatus.FAILED.value]
            + direct_counts[TaskStatus.REPAIR.value],
            "subtree_total": len(descendants),
            "subtree_done": subtree_counts[TaskStatus.DONE.value],
            "queued_descendants": queued_descendants,
            "waiting_parent_descendants": subtree_counts[TaskStatus.WAITING_CHILDREN.value],
            "blocked_descendants": len(blocked_descendants),
            "active_agents": active_count,
            "active_leaves": [
                {
                    "id": child.id,
                    "title": repair_mojibake_text(child.title),
                    "status": child.status,
                    "depth": child.depth,
                }
                for child in active_descendants[:6]
            ],
            "blocked_nodes": [
                {
                    "id": child.id,
                    "title": repair_mojibake_text(child.title),
                    "status": child.status,
                    "reason": repair_mojibake_text(child.error_message) if child.error_message else "",
                }
                for child in blocked_descendants[:6]
            ],
            "updated_at": task.updated_at.isoformat(),
        }

    def get_detail(self, db: Session, task_id: str) -> dict[str, Any]:
        task = self._get_task(db, task_id)
        all_tasks = list(db.scalars(select(TaskNode)).all())
        children_by_parent = self._children_by_parent(all_tasks)
        parent_ids = {node.parent_id for node in all_tasks if node.parent_id}
        is_frontier = task.id not in parent_ids and task.status in ACTIVE_FRONTIER_STATUSES
        return {
            "task": self.serialize_task(task, is_frontier, self._waiting_report_for_task(task, children_by_parent)),
            "runs": [self.serialize_run(run) for run in sorted(task.runs, key=lambda item: item.id)],
            "events": [self.serialize_event(event) for event in sorted(task.events, key=lambda item: item.created_at)],
            "obligations": self._task_obligations(task),
            "usage": summarize_usages([loads(run.usage_json, None) for run in task.runs if run.usage_json]),
        }

    def settle_obligation(self, task_id: str, obligation_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        """人显式清债（§8.2 的逃生舱）。返回该节点清债后的债务汇总。

        没有这条通路时，"门控拦下 + 升级为人工"就是一个死胡同：树被 `blocked` 之后，除了直接改库
        没有任何放行的办法。**放宽不是禁止的，无声的放宽才是**——所以 `reason` 与 `actor` 都必填，
        并写一条事件留痕。

        清债**不会**自动把节点重新跑起来：节点该不该继续由 `POST /api/tasks/{id}/retry` 决定。
        两件事分开，是因为"我确认这条债无害"和"继续干"是两个不同的判断。
        """
        if self.settings.frozen_mode:
            raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
        if not str(actor or "").strip():
            raise ValueError("人工清债必须标明发起者。")
        if not str(reason or "").strip():
            raise ValueError("人工清债必须给出 reason，否则放宽就是无声的。")
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            meta = loads(task.node_meta_json, {})
            items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
            settled = obligations.settle(
                items,
                obligation_id,
                settled_by=obligations.SETTLED_BY_HUMAN,
                reason=reason,
                actor=actor,
            )
            meta["obligations"] = settled
            # 人清债之后，把"连续被拦了几次"归零：否则下一次再被同类债拦下会立刻升级，等于
            # 人这次放行只换来一次机会。归零不代表债被忘掉——那条债本身是 settled 状态。
            meta.pop("obligation_gate", None)
            task.node_meta_json = dumps(meta)
            summary = obligations.summarize(settled)
            self._add_event(
                db,
                task.id,
                "obligation.settled_human",
                f"人工清债：{obligation_id}。理由：{reason}",
                {
                    "obligation_id": obligation_id,
                    "actor": actor,
                    "reason": reason,
                    "remaining": summary,
                },
            )
            db.commit()
        return {"task_id": task_id, "obligation_id": obligation_id, "obligations": summary}

    def _task_obligations(self, task: TaskNode) -> list[dict[str, Any]]:
        """本节点**自己**欠的债（人要看的是它，不是整棵树的汇总）。"""
        meta = loads(task.node_meta_json, {})
        items = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        return list(items)

    def get_run_artifact(self, db: Session, run_id: int, kind: str) -> dict[str, Any]:
        run = db.get(RunRecord, run_id)
        if run is None:
            raise KeyError(f"Run not found: {run_id}")
        raw_path = self._run_artifact_path(run, kind)
        response: dict[str, Any] = {
            "run_id": run_id,
            "kind": kind,
            "path": raw_path,
            "exists": False,
            "truncated": False,
            "content": "",
        }
        if raw_path is None:
            return response
        path = self._safe_workspace_path(raw_path)
        if path is None:
            response["content"] = "拒绝读取：该调试文件不在 battlefield 工作区内。"
            return response
        if not path.exists() or not path.is_file():
            return response
        content = repair_mojibake_text(path.read_text(encoding="utf-8", errors="replace"))
        limit = max(0, self.settings.artifact_preview_chars)
        if limit and len(content) > limit:
            response["content"] = content[:limit]
            response["truncated"] = True
        else:
            response["content"] = content
        response["exists"] = True
        return response

    def serialize_task(
        self,
        task: TaskNode,
        is_active_frontier: bool,
        waiting_report: dict[str, Any] | None = None,
        done_archive: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        node_meta = repair_mojibake(loads(task.node_meta_json, {}))
        if waiting_report is not None:
            node_meta["waiting_report"] = repair_mojibake(waiting_report)
        if done_archive is not None:
            node_meta["done_archive"] = repair_mojibake(done_archive)
        return {
            "id": task.id,
            "parent_id": task.parent_id,
            "title": repair_mojibake_text(task.title),
            "description": repair_mojibake_text(task.description),
            "depth": task.depth,
            "status": task.status,
            "scope": repair_mojibake(loads(task.scope_json, [])),
            "acceptance": repair_mojibake(loads(task.acceptance_json, [])),
            "node_meta": node_meta,
            "constitution_id": task.constitution_id,
            "constitution_hash": task.constitution_hash,
            "constitution_version": task.constitution_version,
            "leaf_attempts": task.leaf_attempts,
            "verification_failures": task.verification_failures,
            "converted_from_leaf": task.converted_from_leaf,
            "progress_current": task.progress_current,
            "progress_total": task.progress_total,
            "result": repair_mojibake(loads(task.result_json, None)),
            "error_message": repair_mojibake_text(task.error_message) if task.error_message else None,
            "branch": task.branch,
            "worktree_path": task.worktree_path,
            "is_active_frontier": is_active_frontier,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def serialize_constitution(self, record: ConstitutionRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "root_id": record.root_id,
            "goal": repair_mojibake_text(record.goal),
            "target_path": repair_mojibake_text(record.target_path) if record.target_path else None,
            "status": record.status,
            "version": record.version,
            "supersedes_id": record.supersedes_id,
            "amendment_direction": record.amendment_direction,
            "effective_at": record.effective_at,
            "content_hash": record.content_hash,
            "markdown": repair_mojibake_text(record.markdown),
            "data": repair_mojibake(loads(record.data_json, {})),
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "locked_at": record.locked_at,
        }

    def serialize_run(self, run: RunRecord) -> dict[str, Any]:
        return {
            "id": run.id,
            "task_id": run.task_id,
            "kind": run.kind,
            "status": run.status,
            "command": run.command,
            "sandbox": run.sandbox,
            "exit_code": run.exit_code,
            "result": repair_mojibake(loads(run.result_json, None)),
            "logs_path": run.logs_path,
            "prompt_path": run.prompt_path,
            "last_message_path": run.last_message_path,
            "workdir": run.workdir,
            "constitution_hash": run.constitution_hash,
            "constitution_version": run.constitution_version,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }

    def _run_artifact_path(self, run: RunRecord, kind: str) -> str | None:
        if kind == "prompt":
            return run.prompt_path
        if kind == "logs":
            return run.logs_path
        if kind == "last_message":
            return run.last_message_path
        raise ValueError(f"Unsupported artifact kind: {kind}")

    def _safe_workspace_path(self, raw_path: str) -> Path | None:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(self.settings.workspace_dir) / path
        try:
            resolved = path.resolve()
            workspace = Path(self.settings.workspace_dir).resolve()
            resolved.relative_to(workspace)
        except (OSError, ValueError):
            return None
        return resolved

    def serialize_event(self, event: EventRecord) -> dict[str, Any]:
        payload = repair_mojibake(loads(event.payload_json, {}))
        message = repair_mojibake_text(event.message)
        if self._is_unreadable_reason(message):
            message = self._fallback_event_message(event.type, payload)
        return {
            "id": event.id,
            "task_id": event.task_id,
            "type": event.type,
            "message": message,
            "payload": payload,
            "created_at": event.created_at,
        }

    def _fallback_event_message(self, event_type: str, payload: dict[str, Any]) -> str:
        return convergence.fallback_event_message(event_type, payload)

    def _worker_empty_decomposition_retry_count(self, task: TaskNode) -> int:
        meta = loads(task.node_meta_json, {})
        try:
            return max(0, int(meta.get("worker_empty_decomposition_retry_count") or 0))
        except (TypeError, ValueError):
            return 0

    def _should_retry_empty_worker_decomposition(self, task: TaskNode, worker_result: WorkerResult) -> bool:
        if worker_result.status not in {"needs_decomposition", "failed"}:
            return False
        if worker_result.suggested_children:
            return False
        if self._worker_empty_decomposition_retry_count(task) >= WORKER_EMPTY_DECOMPOSITION_RETRY_LIMIT:
            return False
        raw_text = " ".join([worker_result.summary, *worker_result.evidence, *worker_result.risks])
        normalized = self._normalize_signature_text(repair_mojibake_text(raw_text))
        if not normalized:
            return False
        if any(marker in normalized for marker in WORKER_EMPTY_DECOMPOSITION_SPLIT_HINTS):
            return False
        return any(marker in normalized for marker in WORKER_EMPTY_DECOMPOSITION_EXECUTION_HINTS)

    def _queue_empty_worker_decomposition_retry(
        self,
        db: Session,
        task: TaskNode,
        worker_result: WorkerResult,
        *,
        source: str,
    ) -> None:
        meta = loads(task.node_meta_json, {})
        retry_count = self._worker_empty_decomposition_retry_count(task) + 1
        meta["worker_empty_decomposition_retry_count"] = retry_count
        meta["worker_empty_decomposition_retry_source"] = source
        meta["worker_empty_decomposition_last_status"] = worker_result.status
        meta["worker_empty_decomposition_last_summary"] = worker_result.summary
        meta["worker_empty_decomposition_retry_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)
        task.result_json = dumps(
            {
                "worker": worker_result.model_dump(),
                "empty_decomposition_retry": {
                    "source": source,
                    "retry_count": retry_count,
                    "limit": WORKER_EMPTY_DECOMPOSITION_RETRY_LIMIT,
                },
            }
        )
        self._add_event(
            db,
            task.id,
            "worker.empty_decomposition_retry",
            "Worker returned an empty self-decomposition while its summary said the leaf was executable; requeueing once before fallback splitting.",
            {"source": source, "status": worker_result.status, "summary": worker_result.summary, "retry_count": retry_count},
        )
        self._mark(
            db,
            task,
            TaskStatus.QUEUED,
            "Worker returned contradictory empty self-decomposition; requeued once so the scheduler can retry the leaf before fallback splitting.",
        )

    def _constitution_for_tasks(self, db: Session, tasks: list[TaskNode]) -> ConstitutionRecord | None:
        constitution_ids = [task.constitution_id for task in tasks if task.constitution_id]
        if not constitution_ids:
            return None
        return db.get(ConstitutionRecord, constitution_ids[0])

    def _convert_leaf_to_parent(
        self,
        db: Session,
        task: TaskNode,
        children: list[ChildTaskSpec],
        event_type: str,
        reason: str,
        payload: dict[str, Any],
    ) -> list[str]:
        task.converted_from_leaf = True
        meta = loads(task.node_meta_json, {})
        meta["converted_from_leaf_reason"] = reason
        meta["conversion_payload"] = payload
        task.node_meta_json = dumps(meta)
        suggested = children or self._fallback_decomposition(task, reason)
        child_ids = self._create_children(db, task, suggested)
        self._add_event(
            db,
            task.id,
            event_type,
            "该节点未能作为叶子通过 300% 铁律，已转为父节点继续拆分。",
            {"reason": reason, "child_ids": child_ids, **payload},
        )
        if child_ids:
            self._mark(db, task, TaskStatus.WAITING_CHILDREN, "叶子误判已转父；父节点等待新的子任务完成。")
        else:
            self._mark(db, task, TaskStatus.REPAIR, reason)
        return child_ids

    def _fallback_decomposition(self, task: TaskNode, reason: str) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        return [
            ChildTaskSpec(
                title=f"{task.title}：实现收敛切片",
                description=f"根据失败原因缩小实现范围：{reason}",
                scope=scope,
                acceptance=["实现结果必须提供可检查证据，并且不突破父任务边界。"],
                coverage_ids=["implementation"],
                estimated_leaf=True,
            ),
            ChildTaskSpec(
                title=f"{task.title}：验证收敛切片",
                description=f"根据失败原因补齐验证与证据：{reason}",
                scope=scope,
                acceptance=["验证结果必须证明父任务可以被准确验收。"],
                coverage_ids=["verification"],
                estimated_leaf=True,
            ),
        ]

    def _planner_blocked_recovery_signature(self, task: TaskNode, reason: str) -> str:
        raw = {
            "source": "planner.blocked",
            "task": self._task_signature(task),
            "reason": self._normalize_signature_text(repair_mojibake_text(reason)),
        }
        return hashlib.sha256(dumps(raw).encode("utf-8")).hexdigest()[:16]

    def _planner_blocked_recovery_children(
        self,
        task: TaskNode,
        reason: str,
        payload: dict[str, Any],
        signature: str,
    ) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        acceptance = loads(task.acceptance_json, [])
        block_report = payload.get("block_report") if isinstance(payload.get("block_report"), dict) else {}
        category = str(block_report.get("category") or "planner")
        parent_action = str(block_report.get("parent_action") or "recursive_recovery")
        return [
            ChildTaskSpec(
                title=f"{task.title}：规划阻塞恢复",
                description=(
                    "规划器把当前节点判为 blocked，但该节点拥有明确目标，系统不能直接把递归树停在人工审查。"
                    "请把这个阻塞当作一个可执行的恢复任务：检查项目宪法、父节点目标、工作目录、可用工具和已有成果，"
                    "找出阻塞真正原因，并产出能让父节点继续收敛的恢复路径。"
                    "\n\n恢复要求：可以直接交付一个更小但不降级父目标的可验收切片；也可以证明需要继续拆分，"
                    "并给出相互独立、难度均衡、父节点可整合的下一层边界。"
                    f"\n\n原始规划阻塞原因：{reason}"
                    f"\n阻塞分类：{category}"
                    f"\n建议父层动作：{parent_action}"
                    f"\n恢复签名：{signature}"
                    f"{self._skill_lookup_recovery_text()}"
                ),
                scope=[*[str(item) for item in scope], f"planner-blocked:{signature}"],
                acceptance=[
                    "必须解释 planner.blocked 的真实原因，不能只重复“无法继续”。",
                    "必须给出可执行恢复路径：直接交付、修正边界或继续拆分；如果继续拆分，子任务必须相对独立且难度均衡。",
                    "必须产生 changed_files、验证证据或明确的父层可验收恢复报告，使父节点可以继续整合。",
                    "不得把父任务降级为更小 demo；恢复方案必须继承项目宪法的品质下限和规模边界。",
                    "如果发现工具流程本身导致误判，必须描述需要父层或工具层修复的具体机制。",
                    *[str(item) for item in acceptance[:4]],
                ],
                coverage_ids=["planner-blocked-recovery", signature],
                verification_kind="planner blocked recovery + recursive continuation evidence",
                estimated_leaf=False,
            )
        ]

    def _acceptance_blocked_recovery_signature(self, task: TaskNode, reason: str) -> str:
        raw = {
            "source": "acceptance.blocked",
            "task": self._task_signature(task),
            "reason": self._normalize_signature_text(repair_mojibake_text(reason)),
        }
        return hashlib.sha256(dumps(raw).encode("utf-8")).hexdigest()[:16]

    def _acceptance_blocked_recovery_children(
        self,
        task: TaskNode,
        reason: str,
        payload: dict[str, Any],
        signature: str,
    ) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        acceptance = loads(task.acceptance_json, [])
        block_report = payload.get("block_report") if isinstance(payload.get("block_report"), dict) else {}
        category = str(block_report.get("category") or "acceptance")
        parent_action = str(block_report.get("parent_action") or "recursive_recovery")
        return [
            ChildTaskSpec(
                title=f"{task.title}：整合阻塞恢复",
                description=(
                    "父节点 acceptance agent 在整合全部子成果时返回 blocked，但该阻塞不是资源/流量类可等待问题，"
                    "也不能直接把父目标停在人工审查。请检查所有已完成子节点的交付、父节点验收标准、项目宪法和实际工作目录，"
                    "找出父节点无法整合的具体缺口，并产出一个可被父节点继续验收的恢复交付。"
                    "\n\n恢复任务可以修复整合断链、补齐缺失入口/文档/验证证据、替代不可用子成果，"
                    "或提出下一层更清晰的独立子任务边界；但必须保持父目标规模和品质下限。"
                    f"\n\n原始整合阻塞原因：{reason}"
                    f"\n阻塞分类：{category}"
                    f"\n建议父层动作：{parent_action}"
                    f"\n恢复签名：{signature}"
                    f"{self._skill_lookup_recovery_text()}"
                ),
                scope=[*[str(item) for item in scope], f"acceptance-blocked:{signature}"],
                acceptance=[
                    "必须解释 acceptance.blocked 的真实原因，并指出阻塞发生在整合、验证、入口、依赖还是目标边界。",
                    "必须产出可被父节点整合的 changed_files、验证证据或明确恢复报告。",
                    "如果需要替代某个子成果，必须说明被替代的子节点、替代范围和验证方式。",
                    "不得降低父任务目标，不得把项目缩成更小 demo。",
                    "必须给出父节点下一次 acceptance 可以检查的具体证据位置或命令。",
                    *[str(item) for item in acceptance[:4]],
                ],
                coverage_ids=["acceptance-blocked-recovery", signature],
                verification_kind="acceptance blocked recovery + parent integration evidence",
                estimated_leaf=False,
            )
        ]

    def _is_foundation_missing_block(self, task: TaskNode, reason: str, payload: dict[str, Any] | None = None) -> bool:
        text = " ".join(
            [
                task.title,
                task.description,
                reason,
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ]
        ).lower()
        missing_markers = [
            "工作目录为空",
            "目录为空",
            "未发现 readme",
            "未发现 package",
            "未发现入口",
            "未发现资源",
            "没有启动命令",
            "没有入口文件",
            "empty working directory",
            "empty worktree",
            "no readme",
            "no package",
            "no entry",
            "no project files",
            "missing project",
            "missing scaffold",
        ]
        bootstrap_markers = [
            "工程骨架",
            "项目骨架",
            "启动入口",
            "标准启动",
            "最小可运行",
            "bootstrap",
            "scaffold",
            "startup",
            "entry",
        ]
        return any(marker in text for marker in missing_markers) and any(marker in text for marker in bootstrap_markers)

    def _foundation_missing_children(self, task: TaskNode, reason: str) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        return [
            ChildTaskSpec(
                title=f"{task.title}：创建最小工程骨架",
                description=(
                    "在当前 WORKING_DIRECTORY 内创建与父任务目标和项目宪法一致的最小可运行工程骨架，"
                    "包括项目配置、主入口、最小场景或主循环、资源目录约定和可扩展模块边界。"
                    f"触发原因：{reason}"
                ),
                scope=scope,
                acceptance=[
                    "WORKING_DIRECTORY 内必须出现可识别的项目配置或启动入口文件。",
                    "必须提供可继续扩展的源码/场景/资源目录边界。",
                    "必须说明标准启动命令或在缺少本机引擎时给出静态验证方式。",
                ],
                coverage_ids=["bootstrap", "project-structure"],
                verification_kind="project structure + startup contract",
                estimated_leaf=False,
            ),
            ChildTaskSpec(
                title=f"{task.title}：补齐启动说明与入口证据",
                description=(
                    "基于已创建或已发现的工程骨架，补齐 README/启动说明、入口文件定位、资源根路径约定和验证证据，"
                    "使父节点能够判断后续玩法、场景、UI、音频任务应接入哪里。"
                ),
                scope=scope,
                acceptance=[
                    "README 或等价说明必须列出安装、启动、验证命令。",
                    "必须明确主入口文件、主场景或主循环位置。",
                    "必须提供实际执行过的验证命令，或说明缺少本机工具时的静态验证证据。",
                ],
                depends_on=[f"{task.title}：创建最小工程骨架"],
                coverage_ids=["startup-docs", "verification"],
                verification_kind="startup documentation + evidence",
                estimated_leaf=True,
            ),
        ]

    def _create_children(self, db: Session, parent: TaskNode, children: list[ChildTaskSpec]) -> list[str]:
        if not children:
            self._block_task(db, parent, "规划器要求拆解，但没有返回任何子任务。", "planner.empty_children")
            return []
        children = self._with_parent_skill_note_child(db, parent, children)
        convergence_loop = self._convergence_slice_loop(parent, children)
        if convergence_loop:
            meta = loads(parent.node_meta_json, {})
            meta["convergence_slice_loop"] = convergence_loop
            attempted = bool(meta.get("convergence_slice_cycle_breaker_attempted"))
            if not attempted:
                meta["convergence_slice_cycle_breaker_attempted"] = True
                parent.node_meta_json = dumps(meta)
                child_ids = self._insert_child_nodes(db, parent, self._convergence_cycle_breaker_children(parent, convergence_loop))
                self._add_event(
                    db,
                    parent.id,
                    "task.convergence_loop_converted",
                    "Detected repeated convergence-slice decomposition; created a recursive cycle-breaker child instead of adding another fallback layer.",
                    {"convergence_slice_loop": convergence_loop, "child_ids": child_ids},
                )
                return child_ids
            meta["manual_review_reason"] = "convergence_slice_loop_after_cycle_breaker"
            parent.node_meta_json = dumps(meta)
            self._add_event(
                db,
                parent.id,
                "task.manual_review_required",
                "Repeated convergence-slice decomposition continued after a cycle-breaker attempt; stopping this branch to avoid unbounded recursion.",
                {"convergence_slice_loop": convergence_loop},
            )
            self._mark(db, parent, TaskStatus.REPAIR, "Repeated convergence-slice recursion continued after a cycle-breaker attempt.")
            return []
        repeated = self._repeated_child_signatures(parent, children)
        if repeated:
            meta = loads(parent.node_meta_json, {})
            meta["repeated_child_signatures"] = repeated
            parent.node_meta_json = dumps(meta)
            attempted = {str(item) for item in meta.get("cycle_breaker_signatures", []) if item}
            repeated_signatures = {str(item.get("signature")) for item in repeated if item.get("signature")}
            if repeated_signatures - attempted:
                meta["cycle_breaker_signatures"] = sorted(attempted | repeated_signatures)
                parent.node_meta_json = dumps(meta)
                child_ids = self._insert_child_nodes(db, parent, self._cycle_breaker_children(parent, repeated))
                self._add_event(
                    db,
                    parent.id,
                    "task.repeated_split_converted",
                    "检测到重复或无收敛的子任务拆分，系统已创建递归收敛破环子任务，而不是直接等待人工审查。",
                    {"repeated_child_signatures": repeated, "child_ids": child_ids},
                )
                return child_ids
            meta["manual_review_reason"] = "repeated_child_signature_after_cycle_breaker"
            parent.node_meta_json = dumps(meta)
            self._add_event(
                db,
                parent.id,
                "task.manual_review_required",
                "检测到重复或无收敛的子任务拆分，且收敛破环已尝试过一次；系统停止继续递归，避免无限拆分。",
                {"repeated_child_signatures": repeated},
            )
            self._mark(db, parent, TaskStatus.REPAIR, "重复拆分或无收敛风险，且收敛破环已尝试过一次。")
            return []
        return self._insert_child_nodes(db, parent, children)

    def _insert_child_nodes(self, db: Session, parent: TaskNode, children: list[ChildTaskSpec]) -> list[str]:
        child_ids = [new_id("node") for _ in children]
        existing_siblings_by_id = {child.id: child for child in parent.children}
        existing_by_title = {self._normalize_signature_text(child.title): child.id for child in parent.children}
        planned_by_title = {
            self._normalize_signature_text(child.title): child_id for child, child_id in zip(children, child_ids, strict=True)
        }
        for child, child_id in zip(children, child_ids, strict=True):
            signature = self._child_signature(child)
            depends_on = list(child.depends_on)
            depends_on_ids: list[str] = []
            unresolved_depends_on: list[str] = []
            dependency_id_by_raw: dict[str, str] = {}
            for dependency in depends_on:
                normalized_dependency = self._normalize_signature_text(dependency)
                dependency_id = existing_by_title.get(normalized_dependency) or planned_by_title.get(normalized_dependency)
                if dependency_id and dependency_id != child_id:
                    depends_on_ids.append(dependency_id)
                    dependency_id_by_raw[str(dependency)] = dependency_id
                else:
                    unresolved_depends_on.append(dependency)
            coverage_ids = list(child.coverage_ids)
            depends_on, depends_on_ids, dropped_repair_dependency_ids = self._drop_blocked_repair_dependencies(
                child,
                depends_on,
                depends_on_ids,
                dependency_id_by_raw,
                existing_siblings_by_id,
            )
            parent_is_final_gate = self._is_final_product_gate_task(parent)
            if parent_is_final_gate and "final-product-gate" not in {str(item) for item in coverage_ids}:
                coverage_ids = ["final-product-gate", *coverage_ids]
            node_meta = {
                "coverage_ids": coverage_ids,
                "verification_kind": child.verification_kind,
                "estimated_leaf": child.estimated_leaf,
                "depends_on": depends_on,
                "depends_on_ids": depends_on_ids,
                "unresolved_depends_on": unresolved_depends_on,
                "split_signature": signature,
                # §7.1.3：父节点自身的 must/never 与它继承来的链，一起下沉（逐层累积）
                "inherited_constraints": self._inherited_constraints_for_parent(parent),
            }
            if dropped_repair_dependency_ids:
                node_meta["dropped_blocked_repair_dependency_ids"] = dropped_repair_dependency_ids
            if parent_is_final_gate:
                node_meta["final_product_gate_lineage"] = True
                node_meta["final_product_gate_parent_id"] = parent.id
            node = TaskNode(
                id=child_id,
                parent_id=parent.id,
                title=child.title,
                description=child.description,
                depth=parent.depth + 1,
                status=TaskStatus.QUEUED.value,
                scope_json=dumps(child.scope),
                acceptance_json=dumps(child.acceptance),
                node_meta_json=dumps(node_meta),
                constitution_id=parent.constitution_id,
                constitution_hash=parent.constitution_hash,
                constitution_json=parent.constitution_json,
                constitution_version=constitution_versions.inherit_version(
                    parent.constitution_version,
                    self._constitution_head_version(db, parent.constitution_id),
                ),
            )
            db.add(node)
            self._add_event(db, child_id, "task.created", f"已创建子任务：{child.title}", {"parent_id": parent.id})
        gap = self._refresh_constraint_coverage(db, parent)
        if gap:
            self._add_event(
                db,
                parent.id,
                "parent.constraint_coverage_gap",
                f"子节点集合没有逐字继承下沉约束，共 {len(gap)} 条：这些约束在委托链上被改写或丢失了。",
                {"missing": gap, "child_ids": child_ids},
            )
        parent.progress_total = len(child_ids)
        return child_ids

    def _drop_blocked_repair_dependencies(
        self,
        child: ChildTaskSpec,
        depends_on: list[str],
        depends_on_ids: list[str],
        dependency_id_by_raw: dict[str, str],
        existing_siblings_by_id: dict[str, TaskNode],
    ) -> tuple[list[str], list[str], list[str]]:
        return convergence.drop_blocked_repair_dependencies(
            child, depends_on, depends_on_ids, dependency_id_by_raw, existing_siblings_by_id
        )

    def _is_blocked_child_repair_spec(self, child: ChildTaskSpec) -> bool:
        return convergence.is_blocked_child_repair_spec(child)

    def _with_parent_skill_note_child(self, db: Session, parent: TaskNode, children: list[ChildTaskSpec]) -> list[ChildTaskSpec]:
        specs, exemption = skill_notes.with_parent_skill_note_child_ex(parent, children, self.settings)
        if exemption:
            self._record_skill_note_readonly_exemption(db, parent, exemption)
        return specs

    def _record_skill_note_readonly_exemption(self, db: Session, parent: TaskNode, matched: str) -> None:
        """只读约束下豁免 skill note 子任务必须**留证据**。

        这是"少建一个子任务"，不写事件就成了静默放宽；命中的原文一起落库，人能自己核对判据。
        """
        self._add_event(
            db,
            parent.id,
            "parent.skill_note_readonly_exempted",
            "该节点处在“整份产物只读”的约束下：不建递归经验归拢子任务（其验收要求写文件，与约束冲突）。",
            {"matched_constraint": matched},
        )

    def _skill_note_tasks_enabled(self) -> bool:
        return skill_notes.skill_note_tasks_enabled(self.settings)

    def _is_skill_note_task(self, task: TaskNode) -> bool:
        return skill_notes.is_skill_note_task(task, self.settings)

    def _looks_like_skill_note_title(self, title: str) -> bool:
        return skill_notes.looks_like_skill_note_title(title)

    def _is_final_product_gate_task(self, task: TaskNode) -> bool:
        meta = loads(task.node_meta_json, {})
        if not isinstance(meta, dict):
            return False
        coverage_ids = {str(item) for item in (meta.get("coverage_ids") or [])}
        verification_kind = str(meta.get("verification_kind") or "")
        return (
            meta.get("proactive_final_gate") is True
            or bool(meta.get("final_product_gate_lineage"))
            or "final-product-gate" in coverage_ids
            or verification_kind.startswith("deterministic final")
            or str(task.title).startswith("最终成品门禁修复")
        )

    def _scheduler_ready_priority(self, task: TaskNode, has_pending_worker_result: bool = False) -> tuple[int, int, datetime, str]:
        meta = loads(task.node_meta_json, {})
        coverage_ids = {str(item) for item in (meta.get("coverage_ids") or [])} if isinstance(meta, dict) else set()
        is_final_gate = self._is_final_product_gate_task(task)
        is_skill_note = self._is_skill_note_task(task)
        is_blocked_child_repair = "blocked-child-repair" in coverage_ids
        if has_pending_worker_result:
            rank = 0
        elif is_final_gate and is_blocked_child_repair:
            rank = 1
        elif is_skill_note:
            rank = 4
        elif is_final_gate:
            rank = 2
        else:
            rank = 3
        return (rank, task.depth, task.created_at or datetime.max, task.id)

    def _is_skill_note_spec(self, child: ChildTaskSpec) -> bool:
        return skill_notes.is_skill_note_spec(child, self.settings)

    def _has_skill_note_child(self, parent: TaskNode) -> bool:
        return skill_notes.has_skill_note_child(parent, self.settings)

    def backfill_skill_note_children(self, root_id: str | None = None) -> dict[str, Any]:
        if not self._skill_note_tasks_enabled():
            return {"status": "disabled", "count": 0, "child_ids": []}
        with SessionLocal() as db:
            all_tasks = list(db.scalars(select(TaskNode).order_by(TaskNode.depth, TaskNode.created_at)).all())
            selected_root_id = root_id or self._latest_root_id(all_tasks)
            if selected_root_id is None:
                return {"status": "empty", "count": 0, "child_ids": []}
            tasks = self._subtree(all_tasks, selected_root_id)
            child_ids: list[str] = []
            for task in tasks:
                child_ids.extend(self._backfill_parent_skill_note_child(db, task))
            db.commit()
        if child_ids and selected_root_id:
            self._safe_write_state_snapshot(selected_root_id, "skill_note.backfill")
        return {"status": "backfilled", "count": len(child_ids), "child_ids": child_ids}

    def _backfill_parent_skill_note_child(self, db: Session, parent: TaskNode) -> list[str]:
        if not self._skill_note_tasks_enabled():
            return []
        if parent.status in TERMINAL_STATUSES or parent.status in STOPPED_STATUSES:
            return []
        if self._is_skill_note_task(parent) or self._has_skill_note_child(parent):
            return []
        main_children = [child for child in parent.children if not self._is_skill_note_task(child)]
        if not main_children:
            return []
        # 与 `_with_parent_skill_note_child` 同一条豁免（**两处都要**：改漏一处，螺旋/冲突就从那条路回来）
        exemption = skill_notes.readonly_constraint_reason(parent)
        if exemption:
            self._record_skill_note_readonly_exemption(db, parent, exemption)
            return []
        sibling_specs = [ChildTaskSpec(title=child.title, description=child.description) for child in main_children]
        child_ids = self._insert_child_nodes(db, parent, [self._parent_skill_note_child(parent, sibling_specs)])
        db.flush()
        parent.progress_total = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).count()
        self._add_event(
            db,
            parent.id,
            "parent.skill_note_backfilled",
            "系统已为既有父节点补齐递归经验归拢 Skill 子任务；该节点会汇总下层经验、阻塞报告和可提升规则。",
            {"child_ids": child_ids, "source_child_ids": [child.id for child in main_children]},
        )
        return child_ids

    def _parent_skill_note_child(self, parent: TaskNode, siblings: list[ChildTaskSpec]) -> ChildTaskSpec:
        return skill_notes.parent_skill_note_child(parent, siblings)

    def _cycle_breaker_children(self, parent: TaskNode, repeated: list[dict[str, Any]]) -> list[ChildTaskSpec]:
        return convergence.cycle_breaker_children(parent, repeated)

    def _convergence_slice_loop(self, parent: TaskNode, children: list[ChildTaskSpec]) -> dict[str, Any] | None:
        return convergence.convergence_slice_loop(parent, children, self.settings)

    def _convergence_slice_count(self, title: Any) -> int:
        return convergence.convergence_slice_count(title)

    def _is_convergence_cycle_breaker_task(self, task: TaskNode) -> bool:
        return convergence.is_convergence_cycle_breaker_task(task)

    def _is_redundant_convergence_cycle_breaker_child(self, parent: TaskNode, child: TaskNode) -> bool:
        return convergence.is_redundant_convergence_cycle_breaker_child(parent, child)

    def _convergence_cycle_breaker_base_title(self, title: Any) -> str:
        return convergence.convergence_cycle_breaker_base_title(title)

    def _is_legacy_convergence_slice_queued_child(self, task: TaskNode) -> bool:
        return convergence.is_legacy_convergence_slice_queued_child(task)

    def _settle_legacy_convergence_slice_children(
        self,
        db: Session,
        parent: TaskNode,
        siblings: list[TaskNode],
    ) -> tuple[set[str], set[str]]:
        stale_children = [child for child in siblings if self._is_legacy_convergence_slice_queued_child(child)]
        if not stale_children:
            return set(), set()
        loop = {
            "source": "legacy_queued_convergence_slice",
            "parent_title": repair_mojibake_text(parent.title),
            "limit": CONVERGENCE_SLICE_REPEAT_LIMIT,
            "stale_child_count": len(stale_children),
            "stale_child_ids": [child.id for child in stale_children],
            "stale_child_titles": [repair_mojibake_text(child.title) for child in stale_children[:20]],
        }
        breaker_id = self._ensure_legacy_convergence_breaker_child(db, parent, loop)
        settled_ids: set[str] = set()
        for child in stale_children:
            meta = loads(child.node_meta_json, {})
            meta["legacy_convergence_slice_superseded_by"] = breaker_id
            meta["legacy_convergence_slice_superseded_at"] = utcnow().isoformat()
            child.node_meta_json = dumps(meta)
            child.result_json = dumps(
                {
                    "system": {
                        "status": "superseded_by_convergence_cycle_breaker",
                        "breaker_child_id": breaker_id,
                        "reason": "Legacy queued convergence-slice task skipped before launching Codex CLI.",
                    }
                }
            )
            self._mark(
                db,
                child,
                TaskStatus.DONE,
                "旧的收敛切片队列已由父节点循环诊断子任务替代，跳过执行以避免无界递归。",
            )
            self._add_event(
                db,
                child.id,
                "task.legacy_convergence_slice_superseded",
                "旧的收敛切片排队节点已被父节点收口诊断子任务替代；该节点不再启动 Codex CLI。",
                {"breaker_child_id": breaker_id, "parent_id": parent.id},
            )
            settled_ids.add(child.id)
        breaker_ids = {breaker_id} if breaker_id else set()
        active_breaker_ids = {
            child_id
            for child_id in breaker_ids
            if (breaker := db.get(TaskNode, child_id)) is not None
            and breaker.status not in TERMINAL_STATUSES
            and breaker.status not in STOPPED_STATUSES
        }
        self._add_event(
            db,
            parent.id,
            "scheduler.legacy_convergence_slice_pruned",
            "调度器已批量熔断旧的收敛切片队列，避免历史 fallback 循环继续消耗 Codex CLI 槽位。",
            {"settled_child_ids": sorted(settled_ids), "breaker_child_ids": sorted(breaker_ids), "loop": loop},
        )
        return settled_ids, active_breaker_ids

    def _settle_redundant_convergence_cycle_breaker_children(
        self,
        db: Session,
        parent: TaskNode,
        siblings: list[TaskNode],
    ) -> set[str]:
        if not self._is_convergence_cycle_breaker_task(parent):
            return set()
        redundant_children = [
            child for child in siblings if self._is_redundant_convergence_cycle_breaker_child(parent, child)
        ]
        if not redundant_children:
            return set()
        settled_ids: set[str] = set()
        for child in redundant_children:
            meta = loads(child.node_meta_json, {})
            if not isinstance(meta, dict):
                meta = {}
            meta["redundant_convergence_cycle_breaker_superseded_by_parent"] = parent.id
            meta["redundant_convergence_cycle_breaker_superseded_at"] = utcnow().isoformat()
            child.node_meta_json = dumps(meta)
            child.result_json = dumps(
                {
                    "system": {
                        "status": "superseded_by_parent_convergence_cycle_breaker",
                        "parent_id": parent.id,
                        "reason": "Queued cycle-breaker child was redundant because the parent is already the convergence cycle-breaker.",
                    }
                }
            )
            self._mark(
                db,
                child,
                TaskStatus.DONE,
                "重复的递归循环诊断子任务已由父级 cycle-breaker 覆盖，跳过执行以避免继续套娃。",
            )
            self._add_event(
                db,
                child.id,
                "task.redundant_convergence_cycle_breaker_superseded",
                "重复的递归循环诊断子任务已被父级 cycle-breaker 熔断；该节点不会再启动 Codex CLI。",
                {"parent_id": parent.id},
            )
            settled_ids.add(child.id)
        self._add_event(
            db,
            parent.id,
            "scheduler.redundant_convergence_cycle_breaker_pruned",
            "调度器已清理 cycle-breaker 下重复排队的 cycle-breaker 子节点，避免递归诊断继续套娃。",
            {"settled_child_ids": sorted(settled_ids)},
        )
        return settled_ids

    def _ensure_legacy_convergence_breaker_child(self, db: Session, parent: TaskNode, loop: dict[str, Any]) -> str | None:
        children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == parent.id)).all())
        for child in children:
            meta = loads(child.node_meta_json, {})
            coverage_ids = {str(item) for item in meta.get("coverage_ids", [])} if isinstance(meta, dict) else set()
            if {"cycle-breaker", "convergence-slice-loop"}.issubset(coverage_ids):
                return child.id
        meta = loads(parent.node_meta_json, {})
        meta["convergence_slice_cycle_breaker_attempted"] = True
        meta["legacy_convergence_slice_loop"] = loop
        parent.node_meta_json = dumps(meta)
        child_ids = self._insert_child_nodes(db, parent, self._convergence_cycle_breaker_children(parent, loop))
        parent.progress_total = db.query(TaskNode).filter(TaskNode.parent_id == parent.id).count()
        self._add_event(
            db,
            parent.id,
            "task.legacy_convergence_loop_converted",
            "Detected legacy queued convergence-slice tasks; created or reused a cycle-breaker child before launching them.",
            {"convergence_slice_loop": loop, "child_ids": child_ids},
        )
        return child_ids[0] if child_ids else None

    def _convergence_cycle_breaker_children(self, parent: TaskNode, loop: dict[str, Any]) -> list[ChildTaskSpec]:
        return convergence.convergence_cycle_breaker_children(parent, loop)

    def _repeated_child_signatures(self, parent: TaskNode, children: list[ChildTaskSpec]) -> list[dict[str, Any]]:
        return convergence.repeated_child_signatures(parent, children, self.settings)

    def _child_signature(self, child: ChildTaskSpec) -> str:
        return convergence.child_signature(child)

    def _task_signature(self, task: TaskNode) -> str:
        return convergence.task_signature(task)

    def _normalize_signature_text(self, value: Any) -> str:
        return convergence.normalize_signature_text(value)

    def _acceptance_repair_cycle_limit(self) -> int:
        """父节点连续 acceptance repair 的上限。

        返回 0 表示**不限次**：父节点可以反复拆分修复子任务，只受其他防失控机制约束
        （重复子任务签名、资源阻塞、启动预算）。返回正数时，达到该次数即转入人工审查。
        """
        return max(0, self.settings.max_acceptance_repair_cycles)

    def _acceptance_repair_count(self, db: Session, task_id: str) -> int:
        runs = db.scalars(
            select(RunRecord).where(
                RunRecord.task_id == task_id,
                RunRecord.kind == RunKind.ACCEPTANCE.value,
            )
        ).all()
        count = 0
        for run in runs:
            result = loads(run.result_json, {})
            if isinstance(result, dict) and result.get("verdict") == "repair":
                count += 1
        return count

    def _parent_godot_integration_gate(self, db: Session, task: TaskNode) -> dict[str, Any]:
        if task.parent_id is None:
            return {"required": False, "passed": True, "checks": [], "missing": [], "reason": "根节点使用最终成品门禁。"}
        gate_text = " ".join(
            [
                task.title or "",
                task.description or "",
                task.acceptance_json or "",
            ]
        ).lower()
        if not self._requires_parent_godot_integration_gate(gate_text):
            return {"required": False, "passed": True, "checks": [], "missing": [], "reason": "该父节点不要求确定性 Godot 集成门禁。"}

        target = self._integration_target_path(db, task)
        checks: list[dict[str, Any]] = []
        missing: list[str] = []

        def add_check(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": passed, "detail": "通过" if passed else detail})
            if not passed:
                missing.append(detail)

        if target is None:
            add_check("integration_target", False, "父节点没有可检查的集成目标目录。")
            return {"required": True, "passed": False, "target": None, "checks": checks, "missing": missing}

        project_file = target / "project.godot"
        project_text = ""
        if project_file.is_file():
            project_text = project_file.read_text(encoding="utf-8", errors="replace")
        main_scene_resource = self._extract_godot_main_scene(project_text)
        main_scene_path = self._godot_resource_path(target, main_scene_resource) if main_scene_resource else None
        add_check("project.godot", project_file.is_file(), "集成目标缺少 project.godot。")
        add_check("main_scene_configured", bool(main_scene_resource), "project.godot 缺少 run/main_scene。")
        add_check("main_scene_exists", bool(main_scene_path and main_scene_path.is_file()), "run/main_scene 指向的主场景不存在。")
        add_check("scene_files", self._count_files(target, "*.tscn") > 0, "集成目标缺少 .tscn 场景文件。")
        add_check("script_files", self._count_files(target, "*.gd") > 0, "集成目标缺少 .gd 脚本文件。")
        missing_refs = self._missing_godot_res_references(target)
        add_check(
            "res_references_resolve",
            not missing_refs,
            f"集成目标存在无法解析的 res:// 资源引用：{', '.join(missing_refs[:12])}。",
        )
        invalid_assets = self._invalid_referenced_asset_files(target)
        add_check(
            "referenced_asset_files",
            not invalid_assets,
            f"集成目标存在空壳或格式无效的已引用图片/音频资源：{', '.join(invalid_assets[:12])}。",
        )
        text_integrity_issues = self._target_text_integrity_issues(target)
        add_check(
            "text_integrity",
            not text_integrity_issues,
            f"集成目标存在不可读文本乱码：{', '.join(text_integrity_issues[:12])}。",
        )
        return {
            "required": True,
            "passed": not missing,
            "target": str(target),
            "main_scene": main_scene_resource,
            "checks": checks,
            "missing": missing,
        }

    def _requires_parent_godot_integration_gate(self, gate_text: str) -> bool:
        return godot_gates.requires_parent_godot_integration_gate(gate_text)

    def _integration_gate_repair_children(self, task: TaskNode, integration_gate: dict[str, Any]) -> list[ChildTaskSpec]:
        target = str(integration_gate.get("target") or "")
        missing = [str(item) for item in integration_gate.get("missing", []) if item]
        missing_text = "；".join(missing) if missing else "父节点 Godot 集成门禁未通过。"
        title = "父节点 Godot 集成门禁修复：补齐入口与资源断链"
        if self._missing_contains_text_integrity_issue(missing):
            title = (
                "父节点 Godot 集成门禁修复：清理文本乱码"
                if self._missing_only_text_integrity_issues(missing)
                else "父节点 Godot 集成门禁修复：补齐入口/资源并清理文本乱码"
            )
        intro = "修复父节点集成目标中的 Godot 入口、主场景或 res:// 资源断链问题。"
        if self._missing_contains_text_integrity_issue(missing):
            intro = (
                "修复父节点集成目标中的不可读文本乱码，确保 JSON、README、Godot 文本资源保持真实可读。"
                if self._missing_only_text_integrity_issues(missing)
                else "修复父节点集成目标中的 Godot 入口、资源断链和不可读文本乱码问题。"
            )
        return [
            ChildTaskSpec(
                title=title,
                description=(
                    f"{intro}"
                    "必须在当前 WORKING_DIRECTORY 内完成可集成交付物，并让父节点重新整合后通过确定性集成门禁。"
                    f"当前缺口：{missing_text}"
                ),
                scope=[target] if target else [],
                acceptance=[
                    "集成目标必须存在 project.godot，且包含 run/main_scene。",
                    "run/main_scene 指向的主场景文件必须真实存在。",
                    "所有代码、场景、数据文件中的 res:// 文件引用必须能解析到真实项目文件；JSON 片段引用只检查 # 之前的文件路径。",
                    "所有用户可读文本、JSON、Godot 文本资源必须没有连续问号或 mojibake 乱码残留。",
                    "必须返回 changed_files，列出新增或修复的场景、脚本、数据或资源文件。",
                    "不得只在 README 中声明已修复，必须提供真实文件证据。",
                ],
                coverage_ids=["parent-godot-integration-gate", "resource-reference-closure"],
                verification_kind="deterministic Godot integration resource gate",
                estimated_leaf=False,
            )
        ]

    def _final_root_product_gate(self, db: Session, task: TaskNode) -> dict[str, Any]:
        if task.parent_id is not None:
            return {"required": False, "passed": True, "checks": [], "missing": [], "reason": "非根节点不运行最终成品门禁。"}
        gate_text = " ".join(
            [
                task.title or "",
                task.description or "",
                task.acceptance_json or "",
                task.constitution_json or "",
            ]
        ).lower()
        if "godot" not in gate_text and "project.godot" not in gate_text:
            return {"required": False, "passed": True, "checks": [], "missing": [], "reason": "根目标不是 Godot 成品。"}

        target = self._root_target_path(db, task)
        checks: list[dict[str, Any]] = []
        missing: list[str] = []

        def add_check(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": passed, "detail": "通过" if passed else detail})
            if not passed:
                missing.append(detail)

        if target is None:
            add_check("target_path", False, "根目标目录不存在或未绑定。")
            return {"required": True, "passed": False, "target": None, "checks": checks, "missing": missing}

        project_file = target / "project.godot"
        project_text = ""
        if project_file.is_file():
            project_text = project_file.read_text(encoding="utf-8", errors="replace")
        add_check("project.godot", project_file.is_file(), "缺少 Godot 项目文件 project.godot。")
        add_check("config_version", "config_version" in project_text, "project.godot 缺少 config_version。")

        main_scene_resource = self._extract_godot_main_scene(project_text)
        main_scene_path = self._godot_resource_path(target, main_scene_resource) if main_scene_resource else None
        add_check("main_scene_configured", bool(main_scene_resource), "project.godot 缺少 run/main_scene。")
        add_check("main_scene_exists", bool(main_scene_path and main_scene_path.is_file()), "Godot 主场景文件不存在。")

        scene_count = self._count_files(target, "*.tscn")
        script_count = self._count_files(target, "*.gd")
        readme_exists = (target / "README.md").is_file() or (target / "README.txt").is_file()
        add_check("scene_files", scene_count > 0, "目标目录缺少 .tscn 场景文件。")
        add_check("script_files", script_count > 0, "目标目录缺少 .gd 脚本文件。")
        add_check("readme", readme_exists, "目标目录缺少 README 运行与验证说明。")
        readme_completion_issues = self._readme_completion_issues(target)
        add_check(
            "readme_current_playable_scope",
            not readme_completion_issues,
            f"README 仍在描述骨架、占位或后续接入范围，无法作为最终成品说明：{', '.join(readme_completion_issues[:6])}。",
        )
        invalid_assets = self._invalid_referenced_asset_files(target)
        add_check(
            "referenced_asset_files",
            not invalid_assets,
            f"存在空壳或格式无效的已引用图片/音频资源：{', '.join(invalid_assets[:12])}。",
        )
        text_integrity_issues = self._target_text_integrity_issues(target)
        add_check(
            "text_integrity",
            not text_integrity_issues,
            f"存在不可读文本乱码：{', '.join(text_integrity_issues[:12])}。",
        )
        if self._requires_arcade_brawler_gate(gate_text):
            self._add_arcade_brawler_gate_checks(target, checks, missing)

        return {
            "required": True,
            "passed": not missing,
            "target": str(target),
            "main_scene": main_scene_resource,
            "checks": checks,
            "missing": missing,
        }

    def _final_gate_repair_children(self, task: TaskNode, final_gate: dict[str, Any]) -> list[ChildTaskSpec]:
        target = str(final_gate.get("target") or "")
        missing = [str(item) for item in final_gate.get("missing", []) if item]
        missing_text = "；".join(missing) if missing else "最终 Godot 成品门禁未通过。"
        targeted_children = self._targeted_final_gate_repair_children(target, missing, missing_text)
        if targeted_children:
            return targeted_children
        title = "最终成品门禁修复：补齐 Godot 可运行入口"
        if self._missing_contains_text_integrity_issue(missing):
            title = (
                "最终成品门禁修复：清理文本乱码"
                if self._missing_only_text_integrity_issues(missing)
                else "最终成品门禁修复：补齐 Godot 成品并清理文本乱码"
            )
        return [
            ChildTaskSpec(
                title=title,
                description=(
                    "修复根节点最终成品门禁未通过的问题。必须在当前 WORKING_DIRECTORY 内补齐 Godot 项目成品证据，"
                    "包括 project.godot、run/main_scene 指向的主场景、至少一个 .tscn 场景、至少一个 .gd 脚本，"
                    "README 运行与验证说明，以及街机格斗目标要求的玩家、敌人、Boss、三关、战斗、HUD/菜单和数据配置证据。"
                    f"当前缺失项：{missing_text}"
                ),
                scope=[target] if target else [],
                acceptance=[
                    "WORKING_DIRECTORY 内必须存在 project.godot，且包含 config_version 与 run/main_scene。",
                    "run/main_scene 指向的主场景文件必须真实存在。",
                    "WORKING_DIRECTORY 内必须至少存在一个 .tscn 场景文件和一个 .gd 脚本文件。",
                    "街机格斗目标必须具备真实玩家场景、至少 4 个普通敌人场景、1 个 Boss 场景、3 个关卡场景，以及战斗/命中/生命、HUD、菜单或流程脚本证据。",
                    "所有用户可读文本、JSON、Godot 文本资源必须没有连续问号或 mojibake 乱码残留。",
                    "README 必须说明启动入口、验证方式和当前可玩内容。",
                    "返回 changed_files 时必须列出新增或修复的 Godot 项目文件，供父节点整合回根目标目录。",
                ],
                coverage_ids=["final-product-gate", "godot-playable-entry"],
                verification_kind="deterministic final product gate",
                estimated_leaf=False,
            )
        ]

    def _targeted_final_gate_repair_children(
        self,
        target: str,
        missing: list[str],
        missing_text: str,
    ) -> list[ChildTaskSpec]:
        if self._final_gate_missing_baseline_entry(missing):
            return []
        specs: list[ChildTaskSpec] = []
        scope = [target] if target else []
        if any("brawler visual asset" in item or "brawler audio asset" in item for item in missing):
            asset_missing = " ; ".join(
                item
                for item in missing
                if "brawler visual asset" in item or "brawler audio asset" in item
            )
            specs.append(
                ChildTaskSpec(
                    title="Final product gate repair: add arcade brawler visual and audio assets",
                    description=(
                        "Repair only the final product gate asset richness gap. Add valid image and audio assets under the current "
                        "WORKING_DIRECTORY, then wire them through res:// references in scenes, scripts, data, or resources so the "
                        "Godot project uses them instead of leaving them as loose files. Current missing asset evidence: "
                        f"{asset_missing}"
                    ),
                    scope=scope,
                    acceptance=[
                        "The project must contain at least 8 valid image files for player, enemies, levels, projectiles, UI, or effects.",
                        "At least 4 valid image files must be referenced through runtime Godot res:// paths.",
                        "The project must contain at least 2 valid audio files for combat, UI, stage, victory, or defeat feedback.",
                        "At least 1 valid audio file must be referenced through a runtime Godot res:// path.",
                        "All new assets and every scene, script, data, or resource file that references them must be listed in changed_files.",
                    ],
                    coverage_ids=["final-product-gate", "brawler-visual-audio-assets"],
                    verification_kind="deterministic brawler asset richness gate",
                    estimated_leaf=False,
                )
            )
        enemy_missing_text = self._filtered_gate_missing_text(
            missing,
            ("缺少敌人/Boss 场景", "res://scenes/actors/enemies/"),
            missing_text,
        )
        level_missing_text = self._filtered_gate_missing_text(
            missing,
            ("缺少关卡场景", "res://scenes/levels/"),
            missing_text,
        )
        if any("缺少敌人/Boss 场景" in item or "res://scenes/actors/enemies/" in item for item in missing):
            specs.append(
                ChildTaskSpec(
                    title="最终成品门禁修复：补齐敌人和 Boss 场景",
                    description=(
                        "只修复最终成品门禁中的敌人/Boss 场景缺口。必须在当前 WORKING_DIRECTORY 内补齐 "
                        "StreetThug、KnifeRunner、BottleThrower、ShieldGuard、MetroBoss 等被数据目录引用的真实可实例化场景，"
                        "并接入敌人 AI、碰撞、攻击/受击区域、生命或死亡路径证据。"
                        f"当前敌人/Boss 缺失项：{enemy_missing_text}"
                    ),
                    scope=scope,
                    acceptance=[
                        "data/enemies/enemy_catalog.json 中引用的所有敌人/Boss scene path 必须存在真实 .tscn 文件。",
                        "每个敌人/Boss 场景必须包含脚本、碰撞体，以及攻击/受击/生命/AI 状态等运行时证据，不能只是空 Node2D 占位。",
                        "敌人/Boss 脚本必须能与玩家战斗循环衔接，包含 chase/patrol/target/attack/damage/death 或等价实现证据。",
                        "所有新增或修复的敌人/Boss 场景、脚本、数据文件必须列入 changed_files，供父节点整合。",
                    ],
                    coverage_ids=["final-product-gate", "brawler-enemy-scenes"],
                    verification_kind="deterministic brawler enemy scene gate",
                    estimated_leaf=False,
                )
            )
        if any("缺少关卡场景" in item or "res://scenes/levels/" in item for item in missing):
            specs.append(
                ChildTaskSpec(
                    title="最终成品门禁修复：补齐三关战斗场景",
                    description=(
                        "只修复最终成品门禁中的关卡场景缺口。必须在当前 WORKING_DIRECTORY 内补齐 "
                        "DockDawn、MarketRun、RooftopBoss 等 level_catalog 引用的真实可战斗关卡场景，"
                        "并提供出生点、敌人波次入口、边界/碰撞、镜头或战斗区域证据。"
                        f"当前关卡缺失项：{level_missing_text}"
                    ),
                    scope=scope,
                    acceptance=[
                        "data/levels/level_catalog.json 中引用的 3 个关卡 scene_path 必须存在真实 .tscn 文件。",
                        "每个关卡场景必须包含玩家出生点、敌人出生/波次入口、碰撞/边界/战斗区域等运行时证据，不能只是空场景。",
                        "关卡场景必须能与现有波次配置、主场景或流程脚本衔接，提供通关/推进路径证据。",
                        "所有新增或修复的关卡场景、脚本、数据文件必须列入 changed_files，供父节点整合。",
                    ],
                    coverage_ids=["final-product-gate", "brawler-level-scenes"],
                    verification_kind="deterministic brawler level scene gate",
                    estimated_leaf=False,
                )
            )
        missing_refs = self._missing_res_refs_from_gate_missing(missing)
        non_actor_level_refs = [
            ref
            for ref in missing_refs
            if "res://scenes/actors/enemies/" not in ref and "res://scenes/levels/" not in ref
        ]
        if non_actor_level_refs or (missing_refs and not specs):
            refs_text = "、".join((non_actor_level_refs or missing_refs)[:12])
            specs.append(
                ChildTaskSpec(
                    title="最终成品门禁修复：补齐断链资源与流程场景",
                    description=(
                        "只修复最终成品门禁中的 res:// 断链资源。重点补齐玩家头像、投射物、胜利/结算 UI 或其他非敌人/关卡断链，"
                        "并确保所有引用路径能解析到真实文件。"
                        f"需要处理的非敌人/非关卡引用：{refs_text}。"
                    ),
                    scope=scope,
                    acceptance=[
                        "所有非敌人/非关卡的 res:// 断链引用必须解析到真实文件。",
                        "图片资源必须是真实有效格式，不允许 0 字节或伪 PNG/JPG；Godot 场景资源必须是有效 .tscn 文本。",
                        "投射物、胜利/结算 UI 等场景必须能与现有战斗/流程脚本衔接，不能只创建空壳文件。",
                        "所有新增或修复的资源、场景、脚本、数据文件必须列入 changed_files，供父节点整合。",
                    ],
                    coverage_ids=["final-product-gate", "resource-reference-closure"],
                    verification_kind="deterministic final resource reference gate",
                    estimated_leaf=False,
                )
            )
        if self._missing_contains_text_integrity_issue(missing):
            specs.append(
                ChildTaskSpec(
                    title="最终成品门禁修复：清理文本乱码",
                    description=(
                        "只修复最终成品门禁中的不可读文本乱码。必须在当前 WORKING_DIRECTORY 内清理 README、JSON、Godot 文本资源里的 "
                        "`????` 或 mojibake 残留，并保持文件格式有效。"
                        f"当前缺失项：{missing_text}"
                    ),
                    scope=scope,
                    acceptance=[
                        "所有用户可读文本、JSON、Godot 文本资源必须没有连续问号或 mojibake 乱码残留。",
                        "修复中文文本时必须使用 ASCII-safe 写入方式，例如 Python `\\uXXXX` 转义或 base64 UTF-8 解码。",
                        "修复后必须验证 JSON/Markdown/Godot 文本格式仍然有效，并列出 changed_files。",
                    ],
                    coverage_ids=["final-product-gate", "text-integrity"],
                    verification_kind="deterministic final text integrity gate",
                    estimated_leaf=False,
                )
            )
        if self._missing_contains_readme_completion_issue(missing):
            specs.append(
                ChildTaskSpec(
                    title="最终成品门禁修复：刷新 README 成品说明",
                    description=(
                        "只修复最终成品门禁中的 README 自述一致性问题。必须基于当前 WORKING_DIRECTORY 内真实存在的 Godot 场景、脚本、"
                        "数据、资源和验证证据刷新 README；不得声称未实现的玩法已经完成，也不得继续保留项目骨架、占位范围或后续接入作为当前交付状态。"
                    ),
                    scope=scope,
                    acceptance=[
                        "README 必须描述当前真实可运行入口、玩家控制、战斗循环、敌人/Boss、关卡推进、HUD/菜单或胜负流程证据。",
                        "README 必须列出可复现验证方式；如果使用 Godot headless 或静态检查，应写明命令和结果来源。",
                        "README 不得继续包含项目骨架、当前只包含、后续接入、placeholder、skeleton、TODO 等把最终成品降级为未完成骨架的说法。",
                        "如果当前项目证据不足以支撑完整 README，必须上报真实缺口并建议父节点继续拆分，不能编造完成状态。",
                        "返回 changed_files，至少包含 README.md 或 README.txt。",
                    ],
                    coverage_ids=["final-product-gate", "readme-current-playable-scope"],
                    verification_kind="deterministic final README deliverable gate",
                    estimated_leaf=True,
                )
            )
        return specs

    def _filtered_gate_missing_text(self, missing: list[str], markers: tuple[str, ...], fallback: str) -> str:
        filtered = [item for item in missing if any(marker in item for marker in markers)]
        return "；".join(filtered) if filtered else fallback

    def _final_gate_missing_baseline_entry(self, missing: list[str]) -> bool:
        baseline_markers = (
            "根目标目录不存在",
            "缺少 Godot 项目文件",
            "缺少 config_version",
            "缺少 run/main_scene",
            "主场景文件不存在",
            "缺少 .tscn 场景文件",
            "缺少 .gd 脚本文件",
            "缺少 README",
        )
        return any(any(marker in item for marker in baseline_markers) for item in missing)

    def _missing_res_refs_from_gate_missing(self, missing: list[str]) -> list[str]:
        refs: list[str] = []
        for item in missing:
            refs.extend(re.findall(r"res://[A-Za-z0-9_./@+\-%~:#?=&]+", item))
        return list(dict.fromkeys(ref.rstrip(".,;。") for ref in refs))

    def _missing_contains_text_integrity_issue(self, missing: list[str]) -> bool:
        return godot_gates.missing_contains_text_integrity_issue(missing)

    def _missing_only_text_integrity_issues(self, missing: list[str]) -> bool:
        return godot_gates.missing_only_text_integrity_issues(missing)

    def _missing_contains_readme_completion_issue(self, missing: list[str]) -> bool:
        return any(
            "README" in item
            and (
                "骨架" in item
                or "占位" in item
                or "后续接入" in item
                or "placeholder" in item.lower()
                or "skeleton" in item.lower()
            )
            for item in missing
        )

    def _requires_arcade_brawler_gate(self, gate_text: str) -> bool:
        markers = ("街机", "格斗", "闯关", "brawler", "beat em up", "beat-em-up", "arcade")
        return any(marker in gate_text for marker in markers)

    def _add_arcade_brawler_gate_checks(self, target: Path, checks: list[dict[str, Any]], missing: list[str]) -> None:
        def add_check(name: str, passed: bool, detail: str) -> None:
            checks.append({"name": name, "passed": passed, "detail": "通过" if passed else detail})
            if not passed:
                missing.append(detail)

        player_data = self._read_json_dict(target / "data" / "characters" / "player_hero.json")
        enemy_catalog = self._read_json_dict(target / "data" / "enemies" / "enemy_catalog.json")
        level_catalog = self._read_json_dict(target / "data" / "levels" / "level_catalog.json")
        spawn_plan = self._read_json_dict(target / "data" / "spawns" / "arcade_wave_plan.json")

        player_scene = self._resource_value(player_data, ("resource_paths", "scene"))
        add_check("brawler_player_data", bool(player_data), "缺少玩家角色数据 data/characters/player_hero.json。")
        add_check("brawler_player_scene", self._resource_exists(target, player_scene), "缺少玩家可实例化场景。")
        add_check(
            "brawler_player_scene_behavior",
            self._actor_scene_has_runtime_content(target, player_scene),
            "玩家场景是空壳或缺少脚本、碰撞、动画、受击/攻击区域等运行时证据。",
        )
        attacks = player_data.get("attacks") if isinstance(player_data.get("attacks"), list) else []
        add_check("brawler_player_attacks", len(attacks) >= 3, "玩家攻击配置少于 3 种，无法证明街机格斗核心手感。")

        enemies = enemy_catalog.get("enemies") if isinstance(enemy_catalog.get("enemies"), list) else []
        ordinary_enemies = [enemy for enemy in enemies if isinstance(enemy, dict) and str(enemy.get("enemy_type", "")).lower() != "boss"]
        bosses = [enemy for enemy in enemies if isinstance(enemy, dict) and str(enemy.get("enemy_type", "")).lower() == "boss"]
        missing_enemy_scenes = [
            str(enemy.get("id") or "unknown")
            for enemy in enemies
            if isinstance(enemy, dict) and not self._resource_exists(target, self._resource_value(enemy, ("resource_paths", "scene")))
        ]
        add_check("brawler_enemy_catalog", len(ordinary_enemies) >= 4, "普通敌人配置少于 4 种。")
        add_check("brawler_boss_catalog", len(bosses) >= 1, "缺少 Boss 配置。")
        add_check("brawler_enemy_scenes", not missing_enemy_scenes, f"缺少敌人/Boss 场景：{', '.join(missing_enemy_scenes)}。")
        weak_enemy_scenes = [
            str(enemy.get("id") or "unknown")
            for enemy in enemies
            if isinstance(enemy, dict)
            and self._resource_exists(target, self._resource_value(enemy, ("resource_paths", "scene")))
            and not self._actor_scene_has_runtime_content(target, self._resource_value(enemy, ("resource_paths", "scene")))
        ]
        add_check(
            "brawler_enemy_scene_behavior",
            not weak_enemy_scenes,
            f"敌人/Boss 场景缺少脚本、碰撞、动画、攻击/受击区域等运行时证据：{', '.join(weak_enemy_scenes)}。",
        )

        levels = level_catalog.get("levels") if isinstance(level_catalog.get("levels"), list) else []
        missing_level_scenes = [
            str(level.get("id") or "unknown")
            for level in levels
            if isinstance(level, dict) and not self._resource_exists(target, str(level.get("scene_path") or ""))
        ]
        add_check("brawler_level_catalog", len(levels) >= 3, "关卡配置少于 3 个。")
        add_check("brawler_level_scenes", not missing_level_scenes, f"缺少关卡场景：{', '.join(missing_level_scenes)}。")
        weak_level_scenes = [
            str(level.get("id") or "unknown")
            for level in levels
            if isinstance(level, dict)
            and self._resource_exists(target, str(level.get("scene_path") or ""))
            and not self._level_scene_has_runtime_content(target, str(level.get("scene_path") or ""))
        ]
        add_check(
            "brawler_level_scene_content",
            not weak_level_scenes,
            f"关卡场景缺少出生点、波次、碰撞、地图、边界或可战斗区域证据：{', '.join(weak_level_scenes)}。",
        )

        wave_sets = spawn_plan.get("wave_sets") if isinstance(spawn_plan.get("wave_sets"), list) else []
        add_check("brawler_wave_sets", len(wave_sets) >= 3, "生成波次配置少于 3 个关卡波次。")

        text_index = self._target_text_index(target)
        script_index = self._target_script_index(target)
        add_check(
            "brawler_main_scene_behavior",
            self._main_scene_has_runtime_content(target),
            "主场景缺少脚本、玩家/关卡/HUD/波次入口或运行时编排证据，可能只是空入口。",
        )
        add_check("brawler_combat_scripts", self._contains_all_keyword_groups(text_index, [["combat", "attack", "hit"], ["health", "damage"]]), "缺少战斗、命中、伤害或生命实现证据。")
        add_check("brawler_player_scripts", self._contains_any_keyword(text_index, ["player", "hero"]), "缺少玩家控制/角色实现证据。")
        add_check("brawler_enemy_scripts", self._contains_any_keyword(text_index, ["enemy", "ai", "boss"]), "缺少敌人 AI 或 Boss 实现证据。")
        add_check("brawler_hud_menu_scripts", self._contains_all_keyword_groups(text_index, [["hud", "health_bar", "score"], ["menu", "restart", "pause"]]), "缺少 HUD、菜单、暂停/重开或分数表现证据。")
        add_check(
            "brawler_player_control_code",
            self._contains_all_keyword_groups(
                script_index,
                [["input", "move", "velocity", "direction", "left", "right"], ["attack", "combo", "hitbox", "hurtbox", "damage"]],
            ),
            "玩家脚本缺少移动输入与攻击/命中盒实现证据。",
        )
        add_check(
            "brawler_enemy_ai_code",
            self._contains_all_keyword_groups(script_index, [["enemy", "boss"], ["ai", "state", "chase", "patrol", "target", "attack"]]),
            "敌人脚本缺少 AI 状态、追击/巡逻、索敌或攻击实现证据。",
        )
        add_check(
            "brawler_progression_loop_code",
            self._contains_all_keyword_groups(
                script_index,
                [["wave", "spawn"], ["level", "stage"], ["victory", "clear", "complete", "next_level", "game_over", "defeat"]],
            ),
            "脚本缺少波次生成、关卡推进、胜负结算或下一关流程证据。",
        )
        image_assets = self._valid_asset_files(target, IMAGE_ASSET_SUFFIXES)
        referenced_image_assets = self._valid_referenced_asset_files(target, IMAGE_ASSET_SUFFIXES)
        audio_assets = self._valid_asset_files(target, AUDIO_ASSET_SUFFIXES)
        referenced_audio_assets = self._valid_referenced_asset_files(target, AUDIO_ASSET_SUFFIXES)
        add_check(
            "brawler_visual_asset_set",
            len(image_assets) >= 8,
            f"brawler visual asset set is too thin: found {len(image_assets)} valid image files, need at least 8 player/enemy/level/UI images.",
        )
        add_check(
            "brawler_visual_assets_referenced",
            len(referenced_image_assets) >= 4,
            f"brawler visual assets are not wired into Godot resources: found {len(referenced_image_assets)} referenced valid image assets, need at least 4.",
        )
        add_check(
            "brawler_audio_asset_set",
            len(audio_assets) >= 2,
            f"brawler audio asset set is too thin: found {len(audio_assets)} valid audio files, need at least 2 combat/UI/stage sound assets.",
        )
        add_check(
            "brawler_audio_assets_referenced",
            len(referenced_audio_assets) >= 1,
            f"brawler audio assets are not wired into Godot resources: found {len(referenced_audio_assets)} referenced valid audio assets, need at least 1.",
        )
        missing_refs = self._missing_godot_res_references(target)
        add_check(
            "brawler_res_references_resolve",
            not missing_refs,
            f"存在无法解析的 res:// 资源引用：{', '.join(missing_refs[:12])}。",
        )

    def _read_json_dict(self, path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _resource_value(self, value: dict[str, Any], keys: tuple[str, ...]) -> str:
        return godot_files.resource_value(value, keys)

    def _resource_exists(self, root: Path, resource: str | None) -> bool:
        return godot_files.resource_exists(root, resource)

    def _readme_completion_issues(self, root: Path) -> list[str]:
        return godot_gates.readme_completion_issues(root)

    def _target_text_integrity_issues(self, root: Path) -> list[str]:
        return godot_gates.target_text_integrity_issues(root)

    def _target_text_index(self, root: Path) -> str:
        return godot_gates.target_text_index(root)

    def _target_script_index(self, root: Path) -> str:
        return godot_gates.target_script_index(root)

    def _resource_text(self, root: Path, resource: str | None) -> str:
        return godot_files.resource_text(root, resource)

    def _godot_scene_node_count(self, scene_text: str) -> int:
        return godot_files.godot_scene_node_count(scene_text)

    def _godot_scene_has_script_reference(self, scene_text: str) -> bool:
        return godot_files.godot_scene_has_script_reference(scene_text)

    def _actor_scene_has_runtime_content(self, root: Path, resource: str | None) -> bool:
        scene_text = self._resource_text(root, resource)
        if not scene_text:
            return False
        has_runtime_node = self._contains_any_keyword(
            scene_text,
            [
                "characterbody2d",
                "kinematicbody2d",
                "area2d",
                "collisionshape2d",
                "animatedsprite2d",
                "animationplayer",
                "hitbox",
                "hurtbox",
            ],
        )
        return self._godot_scene_node_count(scene_text) >= 1 and self._godot_scene_has_script_reference(scene_text) and has_runtime_node

    def _level_scene_has_runtime_content(self, root: Path, resource: str | None) -> bool:
        scene_text = self._resource_text(root, resource)
        if not scene_text:
            return False
        has_level_marker = self._contains_any_keyword(
            scene_text,
            ["playerspawn", "enemyspawn", "spawn", "wave", "tilemap", "collision", "arena", "boundary", "camera2d", "marker2d"],
        )
        return self._godot_scene_node_count(scene_text) >= 3 and has_level_marker

    def _main_scene_has_runtime_content(self, root: Path) -> bool:
        project_file = root / "project.godot"
        if not project_file.is_file():
            return False
        try:
            project_text = project_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        main_scene = self._extract_godot_main_scene(project_text)
        scene_text = self._resource_text(root, main_scene)
        if not scene_text:
            return False
        has_entry_marker = self._contains_any_keyword(scene_text, ["player", "level", "stage", "hud", "spawn", "wave", "gamecontroller"])
        return self._godot_scene_has_script_reference(scene_text) and has_entry_marker

    def _missing_godot_res_references(self, root: Path) -> list[str]:
        return godot_gates.missing_godot_res_references(root)

    def _valid_asset_files(self, root: Path, suffixes: set[str]) -> list[Path]:
        return godot_gates.valid_asset_files(root, suffixes)

    def _valid_referenced_asset_files(self, root: Path, suffixes: set[str]) -> list[Path]:
        return godot_gates.valid_referenced_asset_files(root, suffixes)

    def _invalid_referenced_asset_files(self, root: Path) -> list[str]:
        return godot_gates.invalid_referenced_asset_files(root)

    def _asset_file_is_valid(self, path: Path) -> bool:
        return godot_gates.asset_file_is_valid(path)

    def _asset_header_is_valid(self, suffix: str, head: bytes) -> bool:
        return godot_gates.asset_header_is_valid(suffix, head)

    def _godot_res_references(self, root: Path) -> set[str]:
        return godot_files.godot_res_references(root)

    def _contains_any_keyword(self, text: str, keywords: list[str]) -> bool:
        return any(keyword.lower() in text for keyword in keywords)

    def _contains_all_keyword_groups(self, text: str, keyword_groups: list[list[str]]) -> bool:
        return all(self._contains_any_keyword(text, group) for group in keyword_groups)

    def _extract_godot_main_scene(self, project_text: str) -> str | None:
        return godot_files.extract_godot_main_scene(project_text)

    def _godot_resource_path(self, root: Path, resource: str | None) -> Path | None:
        return godot_files.godot_resource_path(root, resource)

    def _count_files(self, root: Path, pattern: str) -> int:
        return godot_files.count_files(root, pattern)

    def _integrate_child_outputs(
        self,
        db: Session,
        parent: TaskNode,
        children: list[TaskNode],
    ) -> integration.IntegrationReport | None:
        target = self._integration_target_path(db, parent)
        if target is None:
            return None
        target.mkdir(parents=True, exist_ok=True)
        target_kind = "parent_worktree" if parent.worktree_path else "root_target"
        strategy = integration.normalize_strategy(self.settings.integration_conflict_strategy)
        report = integration.IntegrationReport(
            target=str(target),
            target_kind=target_kind,
            parent_worktree_path=parent.worktree_path,
            strategy=strategy,
        )
        # 本次集成内已被谁写过，用于 keep_both 策略下把后写者落到独立文件名。
        written_this_run: dict[str, str] = {}
        # 上一轮集成写进目标的路径。这些文件是**我们自己**放的，不是项目原有文件——
        # 不排除掉的话，同一路径在第二轮会被读成"破坏了既有文件"，于是每重跑一次集成就
        # 重新报一次冲突：债务永远清不掉、门控永远关着（§6.4 硬约束 2 说的"有界但不清"）。
        prior_integrated_paths = {
            str(item)
            for item in (loads(parent.node_meta_json, {}).get("parent_integration") or {}).get("integrated_files") or []
        }
        for child in children:
            output_nodes = self._task_output_nodes(child)
            if not output_nodes and child.status == TaskStatus.DONE.value and child.worktree_path:
                output_nodes = [child]
            for output_node in output_nodes:
                if not output_node.worktree_path:
                    continue
                child_root = Path(output_node.worktree_path)
                worker = loads(output_node.result_json, {}).get("worker", {})
                changed_files = worker.get("changed_files", []) if isinstance(worker, dict) else []
                for raw_rel in changed_files:
                    rel = Path(str(raw_rel))
                    if rel.is_absolute():
                        report.skipped.append(
                            {
                                "child_id": child.id,
                                "output_node_id": output_node.id,
                                "path": str(raw_rel),
                                "reason": "absolute path",
                            }
                        )
                        continue
                    source = (child_root / rel).resolve()
                    destination = (target / rel).resolve()
                    try:
                        source.relative_to(child_root.resolve())
                        destination.relative_to(target.resolve())
                    except ValueError:
                        report.skipped.append(
                            {
                                "child_id": child.id,
                                "output_node_id": output_node.id,
                                "path": str(raw_rel),
                                "reason": "outside integration boundary",
                            }
                        )
                        continue
                    if not source.exists():
                        report.skipped.append(
                            {
                                "child_id": child.id,
                                "output_node_id": output_node.id,
                                "path": str(raw_rel),
                                "reason": "source missing",
                            }
                        )
                        continue
                    if source == destination:
                        continue
                    rel_key = rel.as_posix()
                    previous_writer = written_this_run.get(rel_key)
                    renamed = False
                    if strategy == integration.STRATEGY_KEEP_BOTH and previous_writer and previous_writer != output_node.id:
                        # 同一次集成里同一路径的第二个产出方：两份都留，改名而不是覆盖。
                        destination = destination.with_name(
                            f"{destination.stem}.{output_node.id}{destination.suffix}"
                        )
                        renamed = True
                    destination_existed = destination.exists() and rel_key not in prior_integrated_paths
                    mode = self._copy_integrated_path(source, destination, rel, output_node.id)
                    written_this_run[rel_key] = output_node.id
                    overwrite_conflict = integration.destructive_overwrite_conflict(
                        rel=rel_key,
                        destination=str(destination),
                        mode=mode,
                        output_node_id=output_node.id,
                        destination_existed=destination_existed,
                    )
                    if overwrite_conflict is not None:
                        report.conflicts.append(overwrite_conflict)
                    report.integrated.append(
                        {
                            "child_id": child.id,
                            "output_node_id": output_node.id,
                            "rel": rel_key,
                            "source": str(source),
                            "destination": str(destination),
                            "mode": "keep_both_renamed" if renamed else mode,
                        }
                    )
        # 路径冲突在整轮结束后统一判定：只有看到全部产出方才能说出"谁覆盖了谁"。
        report.conflicts.extend(integration.detect_path_collisions(report.integrated))
        meta = loads(parent.node_meta_json, {})
        meta["parent_integration"] = report.to_meta(self.settings.integration_conflict_detail_limit)
        self._record_parent_integration_obligations(parent, meta, report)
        parent.node_meta_json = dumps(meta)
        if report.integrated or report.skipped or report.conflicts:
            self._add_event(
                db,
                parent.id,
                "parent.integration",
                f"父节点已在整合验收前汇总子节点变更文件。{report.summary_text()}",
                {
                    "target": report.target,
                    "target_kind": report.target_kind,
                    "strategy": report.strategy,
                    "integrated": report.integrated,
                    "conflicts": report.conflicts,
                    "skipped": report.skipped,
                },
            )
        if report.conflicts:
            self._add_event(
                db,
                parent.id,
                "parent.integration_conflict",
                f"集成检测到 {len(report.conflicts)} 处路径冲突：被覆盖的产物不会出现在集成目标里，"
                "已写入 node_meta 并要求验收逐条裁决。",
                {"conflicts": report.conflicts, "blocking": report.blocking},
            )
        return report

    def _consolidate_parent_skill_notes(self, db: Session, parent: TaskNode) -> dict[str, Any] | None:
        if not self._skill_note_tasks_enabled():
            return None
        target = self._integration_target_path(db, parent)
        if target is None:
            return None
        target.mkdir(parents=True, exist_ok=True)
        notes_dir = target / "docs" / "skill_notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        output_path = notes_dir / f"{parent.id}_merged.json"
        source_files = self._skill_note_source_files(target, output_path)
        entries: list[dict[str, Any]] = []
        parse_errors: list[dict[str, str]] = []
        for path in source_files:
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError) as exc:
                parse_errors.append({"path": path.relative_to(target).as_posix(), "error": str(exc)})
                continue
            entries.extend(self._skill_note_entries_from_data(data, path.relative_to(target).as_posix()))

        merged_rules = self._merge_skill_note_entries(entries)
        promote_to_project_skill = [
            {
                "category": rule["category"],
                "problem": rule["problem"],
                "solution": rule["solution"],
                "source_count": rule["source_count"],
            }
            for rule in merged_rules
            if rule.get("promote")
        ]
        meta_before = loads(parent.node_meta_json, {})
        payload = {
            "schema": "codex-battlefield.skill_notes.merged.v1",
            "parent_task_id": parent.id,
            "parent_title": repair_mojibake_text(parent.title),
            "generated_at": utcnow().isoformat(),
            "source_files": [path.relative_to(target).as_posix() for path in source_files],
            "source_entry_count": len(entries),
            "merged_rule_count": len(merged_rules),
            "duplicates_or_similar_merged": max(0, len(entries) - len(merged_rules)),
            "parse_errors": parse_errors,
            "merged_rules": merged_rules,
            # R6：失败/阻塞子树的原始证据随复盘产物一起落盘——"复盘产物里必须出现该失败的经验条目"
            # 靠的就是这一段（`_write_failure_retrospective` 在归拢之前已写好）。
            "failure_retrospective": meta_before.get(failure_retrospective.RETROSPECTIVE_KEY),
            "promote_to_project_skill": promote_to_project_skill,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        meta = loads(parent.node_meta_json, {})
        meta["skill_note_digest"] = {
            "path": output_path.relative_to(target).as_posix(),
            "source_entry_count": len(entries),
            "merged_rule_count": len(merged_rules),
            "updated_at": payload["generated_at"],
        }
        parent.node_meta_json = dumps(meta)
        self._add_event(
            db,
            parent.id,
            "parent.skill_notes_consolidated",
            "父节点已归拢下层 skill notes，重复和相似经验已合并，供上一层继续整合。",
            meta["skill_note_digest"],
        )
        return payload

    def _skill_note_source_files(self, root: Path, output_path: Path) -> list[Path]:
        candidates: list[Path] = []
        patterns = [
            "docs/skill_notes/*.json",
            "docs/**/skill_notes/*.json",
            "docs/codex_battlefield_skill_notes.json",
        ]
        for pattern in patterns:
            try:
                candidates.extend(path for path in root.glob(pattern) if path.is_file())
            except OSError:
                continue
        resolved_output = output_path.resolve()
        unique: dict[str, Path] = {}
        for path in candidates:
            try:
                resolved = path.resolve()
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            if resolved == resolved_output:
                continue
            unique[str(resolved).lower()] = path
        return sorted(unique.values(), key=lambda item: item.as_posix().lower())

    def _skill_note_entries_from_data(self, data: Any, source_file: str) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        def add_entry(item: Any, fallback_category: str) -> None:
            if not isinstance(item, dict):
                text = repair_mojibake_text(str(item)).strip()
                if text:
                    entries.append(
                        self._normalize_skill_note_entry(
                            {
                                "category": fallback_category,
                                "problem": text,
                                "solution": "",
                                "evidence": [],
                                "source_files": [source_file],
                                "source_nodes": [],
                                "promote": False,
                            }
                        )
                    )
                return
            category = repair_mojibake_text(str(item.get("category") or item.get("type") or fallback_category)).strip()
            problem = repair_mojibake_text(
                str(item.get("problem") or item.get("title") or item.get("issue") or item.get("symptom") or item.get("summary") or "")
            ).strip()
            solution = repair_mojibake_text(
                str(item.get("solution") or item.get("rule") or item.get("fix") or item.get("recommendation") or item.get("action") or "")
            ).strip()
            evidence = self._string_list(item.get("evidence") or item.get("examples") or item.get("commands") or [])
            source_nodes = self._string_list(item.get("source_nodes") or item.get("source_child_ids") or item.get("task_ids") or [])
            promote = bool(item.get("promote") or item.get("promote_to_project_skill") or item.get("global"))
            if problem or solution or evidence:
                entries.append(
                    self._normalize_skill_note_entry(
                        {
                            "category": category or fallback_category,
                            "problem": problem,
                            "solution": solution,
                            "evidence": evidence,
                            "source_files": [source_file],
                            "source_nodes": source_nodes,
                            "promote": promote,
                        }
                    )
                )

        if isinstance(data, list):
            for item in data:
                add_entry(item, "general")
            return entries
        if not isinstance(data, dict):
            add_entry(data, "general")
            return entries

        for key, fallback in (
            ("merged_rules", "merged_rule"),
            ("rules", "rule"),
            ("pitfalls", "pitfall"),
            ("observations", "observation"),
            ("lessons", "lesson"),
            ("promote_to_project_skill", "promote"),
            ("blockers", "blocker"),
        ):
            value = data.get(key)
            if isinstance(value, list):
                for item in value:
                    add_entry(item, fallback)
        if not entries:
            add_entry(data, "summary")
        return entries

    def _merge_skill_note_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for raw_entry in entries:
            entry = self._normalize_skill_note_entry(raw_entry)
            match = next((item for item in merged if self._skill_note_entries_similar(item, entry)), None)
            if match is None:
                merged.append(
                    {
                        "category": entry.get("category") or "general",
                        "problem": entry.get("problem") or "",
                        "solution": entry.get("solution") or "",
                        "evidence": list(entry.get("evidence") or [])[:12],
                        "source_files": sorted(set(entry.get("source_files") or [])),
                        "source_nodes": sorted(set(entry.get("source_nodes") or [])),
                        "source_count": 1,
                        "promote": bool(entry.get("promote")),
                    }
                )
                continue
            match["evidence"] = list(dict.fromkeys([*match.get("evidence", []), *entry.get("evidence", [])]))[:20]
            match["source_files"] = sorted(set([*match.get("source_files", []), *entry.get("source_files", [])]))
            match["source_nodes"] = sorted(set([*match.get("source_nodes", []), *entry.get("source_nodes", [])]))
            match["source_count"] = int(match.get("source_count") or 1) + 1
            match["promote"] = bool(match.get("promote") or entry.get("promote"))
            if len(str(entry.get("solution") or "")) > len(str(match.get("solution") or "")):
                match["solution"] = entry.get("solution") or ""
            if len(str(entry.get("problem") or "")) > len(str(match.get("problem") or "")):
                match["problem"] = entry.get("problem") or ""
            match.update(self._normalize_skill_note_entry(match))
        return sorted(merged, key=lambda item: (str(item.get("category") or ""), str(item.get("problem") or "")))

    def _normalize_skill_note_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(entry)
        category = str(normalized.get("category") or "").lower()
        if category in {"windows_command", "windows_powershell_command_pitfalls", "powershell", "powershell_command"}:
            normalized["category"] = "windows_command_validation"
        evidence = self._string_list(normalized.get("evidence") or [])
        text = " ".join(
            [
                str(normalized.get("category") or ""),
                str(normalized.get("problem") or ""),
                str(normalized.get("solution") or ""),
                " ".join(evidence),
            ]
        )
        if not self._skill_note_mentions_stale_windows_shell_rule(text):
            return normalized
        evidence.append("Normalized stale powershell.exe -NoProfile -Command guidance to the current direct-command rule.")
        normalized["category"] = "windows_command_validation"
        normalized["problem"] = normalized.get("problem") or "Stale nested PowerShell command guidance."
        normalized["solution"] = self._current_windows_shell_skill_note_solution()
        normalized["evidence"] = list(dict.fromkeys(evidence))[:20]
        normalized["promote"] = True
        return normalized

    def _skill_note_mentions_stale_windows_shell_rule(self, text: str) -> bool:
        lowered = repair_mojibake_text(text).lower()
        return "powershell.exe -noprofile -command" in lowered or "powershell -noprofile -command" in lowered

    def _current_windows_shell_skill_note_solution(self) -> str:
        return (
            "Codex CLI already runs commands through the Windows shell; do not wrap routine checks in "
            "powershell.exe -Command or cmd /c. Run direct commands such as rg --files, rg -n -F token path, "
            "Test-Path -LiteralPath path, or python -m json.tool file.json. Use powershell.exe -NoProfile -File "
            "script.ps1 only for standalone scripts, and do not redirect validation output to NUL."
        )

    def _skill_note_entries_similar(self, left: dict[str, Any], right: dict[str, Any]) -> bool:
        if str(left.get("category") or "").lower() != str(right.get("category") or "").lower():
            return False
        left_text = f"{left.get('problem') or ''} {left.get('solution') or ''}"
        right_text = f"{right.get('problem') or ''} {right.get('solution') or ''}"
        left_tokens = self._skill_note_tokens(self._normalize_signature_text(repair_mojibake_text(left_text)))
        right_tokens = self._skill_note_tokens(self._normalize_signature_text(repair_mojibake_text(right_text)))
        shared_technical_tokens = {
            token
            for token in left_tokens & right_tokens
            if token in {"powershell", "python", "python-c", "json.tool", "godot", "utf-8", "rg", "$null", "$lastexitcode"}
            or len(token) >= 4
        }
        if len(shared_technical_tokens) >= 2:
            return True
        return self._skill_note_similarity(left_text, right_text) >= 0.5

    def _skill_note_similarity(self, left: str, right: str) -> float:
        left_norm = self._normalize_signature_text(repair_mojibake_text(left))
        right_norm = self._normalize_signature_text(repair_mojibake_text(right))
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0
        left_tokens = self._skill_note_tokens(left_norm)
        right_tokens = self._skill_note_tokens(right_norm)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

    def _skill_note_tokens(self, text: str) -> set[str]:
        tokens = set(re.findall(r"[a-z0-9_./\\$-]+", text.lower()))
        cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
        if cjk:
            if len(cjk) <= 3:
                tokens.add(cjk)
            else:
                tokens.update(cjk[index : index + 3] for index in range(0, len(cjk) - 2))
        if not tokens and len(text) >= 3:
            tokens.update(text[index : index + 3] for index in range(0, len(text) - 2))
        return tokens

    def _string_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [repair_mojibake_text(str(item)) for item in value if str(item).strip()]
        if value is None:
            return []
        text = repair_mojibake_text(str(value)).strip()
        return [text] if text else []

    def _integration_target_path(self, db: Session, parent: TaskNode) -> Path | None:
        if parent.worktree_path:
            return Path(parent.worktree_path)
        return self._root_target_path(db, parent)

    def _worker_changed_file_issues(
        self,
        task: TaskNode,
        worker_result: WorkerResult,
        worker_envelope: RunnerEnvelope,
    ) -> list[dict[str, str]]:
        if not self._should_validate_worker_changed_files(task, worker_envelope):
            return []
        root_raw = task.worktree_path or worker_envelope.workdir
        if not root_raw:
            return []
        root = Path(root_raw).resolve()
        issues: list[dict[str, str]] = []
        for raw_rel in worker_result.changed_files:
            rel_text = str(raw_rel)
            rel = Path(rel_text)
            if rel.is_absolute():
                issues.append({"path": rel_text, "reason": "absolute path"})
                continue
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                issues.append({"path": rel_text, "reason": "outside worktree"})
                continue
            if not target.exists():
                issues.append({"path": rel.as_posix(), "reason": "missing"})
        return issues

    def _should_validate_worker_changed_files(self, task: TaskNode, worker_envelope: RunnerEnvelope) -> bool:
        if not task.worktree_path or not worker_envelope.workdir:
            return False
        try:
            return Path(worker_envelope.workdir).resolve() == Path(task.worktree_path).resolve()
        except OSError:
            return False

    def _worker_changed_file_issue_reason(self, issues: list[dict[str, str]]) -> str:
        preview = "、".join(f"{issue['path']}({issue['reason']})" for issue in issues[:8])
        suffix = " 等" if len(issues) > 8 else ""
        return f"worker 声明了 changed_files，但这些交付文件不存在或越界：{preview}{suffix}。叶子不能作为已完成交付，必须转父节点递归补齐。"

    def _changed_file_repair_children(self, task: TaskNode, issues: list[dict[str, str]]) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        issue_lines = "\n".join(f"- {issue['path']}：{issue['reason']}" for issue in issues)
        missing_paths = [issue["path"] for issue in issues if issue.get("reason") == "missing"]
        acceptance = [
            "必须在当前 WORKING_DIRECTORY 内真实创建或修复缺失的 changed_files 文件，路径必须是相对路径且不得越界。",
            "返回的 changed_files 必须逐项对应真实存在的文件或目录，供父节点整合复制。",
            "如果原路径确实不应存在，必须用等价交付文件替代，并在 evidence 中说明替代关系和父节点可验收原因。",
        ]
        if missing_paths:
            acceptance.append(f"至少补齐这些缺失路径：{', '.join(missing_paths[:8])}。")
        return [
            ChildTaskSpec(
                title=f"{task.title}：补齐缺失 changed_files 交付",
                description=(
                    "父节点检测到该叶子 worker 声称交付了 changed_files，但工作目录中找不到对应文件或路径越界。"
                    "请把这个叶子重新拆成可验证的修复任务，补齐真实文件交付，让父节点整合不再空转。"
                    f"\n\n问题清单：\n{issue_lines}"
                ),
                scope=scope,
                acceptance=acceptance,
                coverage_ids=["worker-changed-files-existence"],
                verification_kind="changed_files existence + recursive repair evidence",
                estimated_leaf=False,
            )
        ]

    def _repair_changed_text_files(self, task: TaskNode, worker_result: WorkerResult) -> list[str]:
        if not task.worktree_path:
            return []
        root = Path(task.worktree_path).resolve()
        repaired: list[str] = []
        for raw_rel in worker_result.changed_files:
            rel = Path(str(raw_rel))
            if rel.is_absolute() or rel.suffix.lower() not in TEXT_DELIVERABLE_SUFFIXES:
                continue
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            repaired_text = repair_mojibake_text(text)
            if repaired_text != text:
                path.write_text(repaired_text, encoding="utf-8")
                repaired.append(rel.as_posix())
        return repaired

    def _worker_text_file_issues(self, task: TaskNode, worker_result: WorkerResult) -> list[dict[str, str]]:
        if not task.worktree_path:
            return []
        root = Path(task.worktree_path).resolve()
        issues: list[dict[str, str]] = []
        for raw_rel in worker_result.changed_files:
            rel_text = str(raw_rel)
            rel = Path(rel_text)
            if rel.is_absolute() or rel.suffix.lower() not in TEXT_DELIVERABLE_SUFFIXES:
                continue
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if has_irreversible_question_mark_corruption(text):
                issues.append({"path": rel.as_posix(), "reason": "irreversible question-mark text corruption"})
            elif has_mojibake_corruption(text):
                issues.append({"path": rel.as_posix(), "reason": "mojibake text corruption"})
        return issues

    def _worker_text_file_issue_reason(self, issues: list[dict[str, str]]) -> str:
        preview = "、".join(f"{issue['path']}({issue['reason']})" for issue in issues[:8])
        suffix = " 等" if len(issues) > 8 else ""
        return f"worker 产出的用户可读文本存在问号或 mojibake 乱码：{preview}{suffix}。叶子不能作为已完成交付，必须转父节点递归修复为真实可读的 UTF-8 中文。"

    def _text_file_repair_children(self, task: TaskNode, issues: list[dict[str, str]]) -> list[ChildTaskSpec]:
        scope = loads(task.scope_json, [])
        issue_lines = "\n".join(f"- {issue['path']}：{issue['reason']}" for issue in issues)
        corrupted_paths = [issue["path"] for issue in issues]
        acceptance = [
            "必须把问号或 mojibake 乱码文本修复为真实可读的 UTF-8 中文或清晰英文，不能保留连续 `???`/`????`、`鐖跺眰`、`鑺傜偣`、`楠岃瘉`、`銆` 等乱码作为说明内容。",
            "修复文件时必须使用 ASCII-safe 写入方式：Python 字符串使用 `\\uXXXX` 转义、base64 UTF-8 内容解码，或其他不会经过 PowerShell 中文字面量转码的办法。",
            "验证必须读取修复后的文件并证明不存在连续问号乱码或 mojibake 残留，同时 JSON/Markdown/Godot 文本格式仍然有效。",
            "返回 changed_files 必须列出所有修复过的文本文件，供父节点整合复制。",
        ]
        if corrupted_paths:
            acceptance.append(f"至少修复这些路径：{', '.join(corrupted_paths[:8])}。")
        return [
            ChildTaskSpec(
                title=f"{task.title}：修复文本乱码",
                description=(
                    "父节点检测到该叶子 worker 产出的用户可读文本被 Windows 命令行转码破坏，出现不可逆的 `????` 或 mojibake 乱码片段。"
                    "请只修复这些文本产物，使父节点和人类能读懂证据；不要扩张到实现其他游戏功能。"
                    "写入中文时不要把中文字面量直接放进 PowerShell 命令，改用 `\\uXXXX` 转义或 base64 UTF-8 解码后写文件。"
                    f"\n\n问题清单：\n{issue_lines}"
                ),
                scope=scope,
                acceptance=acceptance,
                coverage_ids=["worker-text-encoding-readable"],
                verification_kind="utf8 text integrity + recursive repair evidence",
                estimated_leaf=False,
            )
        ]

    def _seed_worktree_from_parent(self, db: Session, task: TaskNode) -> None:
        if not task.worktree_path or not task.parent_id:
            return
        parent = db.get(TaskNode, task.parent_id)
        if parent is None or not parent.worktree_path:
            return
        parent_root = Path(parent.worktree_path).resolve()
        target = Path(task.worktree_path).resolve()
        if not parent_root.exists() or parent_root == target:
            return
        ignored_dirs = {".git", ".godot", "node_modules", ".venv", "venv", "__pycache__", ".codex_pycache"}
        copied: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        target.mkdir(parents=True, exist_ok=True)
        for source in parent_root.rglob("*"):
            rel = source.relative_to(parent_root)
            if any(part in ignored_dirs for part in rel.parts):
                continue
            destination = (target / rel).resolve()
            try:
                destination.relative_to(target)
            except ValueError:
                skipped.append({"path": str(rel), "reason": "outside child worktree"})
                continue
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.exists() and rel.as_posix().lower() not in {"project.godot", "readme.md", "readme.txt"}:
                skipped.append({"path": str(rel), "reason": "child already has file"})
                continue
            mode = self._copy_integrated_path(source, destination, rel, parent.id)
            copied.append({"path": str(rel), "mode": mode})
        if copied or skipped:
            self._add_event(
                db,
                task.id,
                "worktree.parent_seeded",
                "已把父节点 worktree 的当前上下文播种到子节点，保证深层递归不会脱离父层产物。",
                {"parent_id": parent.id, "copied": copied, "skipped": skipped},
            )

    def _seed_worktree_from_dependencies(self, db: Session, task: TaskNode) -> None:
        if not task.worktree_path or not task.parent_id:
            return
        parent = db.get(TaskNode, task.parent_id)
        if parent is None:
            return
        siblings = list(parent.children)
        siblings_by_id = {sibling.id: sibling for sibling in siblings}
        dependency_ids = self._dependency_ids(task, siblings)
        if not dependency_ids:
            return
        target = Path(task.worktree_path)
        target.mkdir(parents=True, exist_ok=True)
        integrated: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        for dependency_id in dependency_ids:
            dependency = siblings_by_id.get(dependency_id)
            if dependency is None or dependency.status != TaskStatus.DONE.value:
                continue
            for output_node in self._task_output_nodes(dependency):
                if not output_node.worktree_path:
                    continue
                child_root = Path(output_node.worktree_path)
                worker = loads(output_node.result_json, {}).get("worker", {})
                changed_files = worker.get("changed_files", []) if isinstance(worker, dict) else []
                for raw_rel in changed_files:
                    rel = Path(str(raw_rel))
                    if rel.is_absolute():
                        skipped.append({"dependency_id": output_node.id, "path": str(raw_rel), "reason": "absolute path"})
                        continue
                    source = (child_root / rel).resolve()
                    destination = (target / rel).resolve()
                    try:
                        source.relative_to(child_root.resolve())
                        destination.relative_to(target.resolve())
                    except ValueError:
                        skipped.append({"dependency_id": output_node.id, "path": str(raw_rel), "reason": "outside integration boundary"})
                        continue
                    if not source.exists():
                        skipped.append({"dependency_id": output_node.id, "path": str(raw_rel), "reason": "source missing"})
                        continue
                    if source == destination:
                        continue
                    mode = self._copy_integrated_path(source, destination, rel, output_node.id)
                    integrated.append(
                        {
                            "dependency_id": output_node.id,
                            "source": str(source),
                            "destination": str(destination),
                            "mode": mode,
                        }
                    )
        if integrated or skipped:
            self._add_event(
                db,
                task.id,
                "worktree.dependencies_seeded",
                "已把已完成依赖节点的产物合入当前叶子 worktree，供后续递归开发继续使用。",
                {"integrated": integrated, "skipped": skipped},
            )

    def _seed_planner_worktree_context(self, db: Session, task: TaskNode) -> None:
        if not task.parent_id:
            return
        parent = db.get(TaskNode, task.parent_id)
        if parent is None:
            return
        siblings = list(parent.children)
        if not parent.worktree_path and not self._dependency_ids(task, siblings):
            return
        self._assign_worktree(db, task)
        self._seed_worktree_from_parent(db, task)
        self._seed_worktree_from_dependencies(db, task)

    def _task_output_nodes(self, task: TaskNode) -> list[TaskNode]:
        nodes: list[TaskNode] = []
        stack = [task]
        while stack:
            current = stack.pop(0)
            if current.status != TaskStatus.DONE.value:
                continue
            worker = loads(current.result_json, {}).get("worker", {})
            changed_files = worker.get("changed_files", []) if isinstance(worker, dict) else []
            if current.worktree_path and changed_files:
                nodes.append(current)
            stack.extend(current.children)
        return nodes

    def _copy_integrated_path(self, source: Path, destination: Path, rel: Path, source_id: str) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            return "copytree"
        rel_key = rel.as_posix().lower()
        if destination.exists() and rel_key == "project.godot":
            merged = self._merge_godot_project_text(
                destination.read_text(encoding="utf-8", errors="replace"),
                source.read_text(encoding="utf-8", errors="replace"),
            )
            destination.write_text(merged, encoding="utf-8")
            return "merge_project_godot"
        if destination.exists() and Path(rel_key).name in {"readme.md", "readme.txt"}:
            existing = destination.read_text(encoding="utf-8", errors="replace")
            incoming = source.read_text(encoding="utf-8", errors="replace")
            if incoming.strip() and incoming.strip() not in existing:
                merged = f"{existing.rstrip()}\n\n---\n\n## Integrated notes from {source_id}\n\n{incoming.strip()}\n"
                destination.write_text(merged, encoding="utf-8")
                return "append_readme"
            return "unchanged"
        shutil.copy2(source, destination)
        return "copy"

    def _merge_godot_project_text(self, existing: str, incoming: str) -> str:
        existing_sections = self._parse_godot_project_sections(existing)
        incoming_sections = self._parse_godot_project_sections(incoming)
        order = list(existing_sections)
        for section, lines in incoming_sections.items():
            if section not in existing_sections:
                existing_sections[section] = list(lines)
                order.append(section)
                continue
            existing_lines = self._merge_godot_section_lines(section, existing_sections[section], lines)
            existing_sections[section] = existing_lines
        rendered: list[str] = []
        for section in order:
            if section:
                if rendered and rendered[-1] != "":
                    rendered.append("")
                rendered.append(section)
                rendered.append("")
            rendered.extend(existing_sections[section])
        return "\n".join(line.rstrip("\n") for line in rendered).rstrip() + "\n"

    def _merge_godot_section_lines(self, section: str, existing_lines: list[str], incoming_lines: list[str]) -> list[str]:
        if section == "[input]":
            return self._merge_godot_input_lines(existing_lines, incoming_lines)
        merged = list(existing_lines)
        key_index = {
            line.split("=", 1)[0].strip(): index
            for index, line in enumerate(merged)
            if "=" in line and not line.lstrip().startswith((";", "#"))
        }
        for line in incoming_lines:
            key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith((";", "#")) else None
            if key and key in key_index:
                merged[key_index[key]] = line
            elif line.strip() and line not in merged:
                merged.append(line)
        return merged

    def _merge_godot_input_lines(self, existing_lines: list[str], incoming_lines: list[str]) -> list[str]:
        existing_blocks = self._split_godot_input_blocks(existing_lines)
        incoming_blocks = self._split_godot_input_blocks(incoming_lines)
        incoming_by_key = {key: block for key, block, complete in incoming_blocks if key and complete}
        merged: list[str] = []
        seen_keys: set[str] = set()
        raw_lines: set[str] = set()
        for key, block, complete in existing_blocks:
            if key:
                replacement = incoming_by_key.get(key)
                if replacement is not None:
                    merged.extend(replacement)
                    seen_keys.add(key)
                elif complete:
                    merged.extend(block)
                    seen_keys.add(key)
                continue
            for line in block:
                if line.strip() and line not in raw_lines:
                    merged.append(line)
                    raw_lines.add(line)
                elif not line.strip():
                    merged.append(line)
        for key, block, complete in incoming_blocks:
            if key:
                if key not in seen_keys and complete:
                    merged.extend(block)
                    seen_keys.add(key)
                continue
            for line in block:
                if line.strip() and line not in raw_lines and line not in merged:
                    merged.append(line)
                    raw_lines.add(line)
        return merged

    def _split_godot_input_blocks(self, lines: list[str]) -> list[tuple[str | None, list[str], bool]]:
        blocks: list[tuple[str | None, list[str], bool]] = []
        index = 0
        while index < len(lines):
            line = lines[index]
            key = self._godot_line_key(line)
            if key is None:
                blocks.append((None, [line], True))
                index += 1
                continue
            block = [line]
            index += 1
            if not line.strip().endswith("{"):
                blocks.append((key, block, True))
                continue
            complete = False
            while index < len(lines):
                next_line = lines[index]
                if self._godot_line_key(next_line) is not None and next_line.strip().endswith("{"):
                    break
                block.append(next_line)
                index += 1
                if next_line.strip() == "}":
                    complete = True
                    break
            blocks.append((key, block, complete))
        return blocks

    def _godot_line_key(self, line: str) -> str | None:
        return godot_files.godot_line_key(line)

    def _parse_godot_project_sections(self, content: str) -> dict[str, list[str]]:
        return godot_files.parse_godot_project_sections(content)

    def _is_false_missing_objective(self, decision: PlannerDecision, task: TaskNode) -> bool:
        if decision.decision not in {"blocked", "repair"} or not task.description.strip():
            return False
        reason = decision.reason.lower()
        return (
            "no concrete task" in reason
            or "no actionable task" in reason
            or ("description" in reason and "missing" in reason)
            or ("objective" in reason and "provided" in reason)
        )

    def _model_tier_board(self) -> dict[str, Any]:
        """按角色分层的"现在的实际配比"（B 部分）。

        `effective` 给的是**已解析**的档位（空档显示成"全局默认档"而不是空串——空串会被读成"没有模型"）。
        这条读数是"用便宜模型干活、用强模型验收"能不能被验证的唯一入口：没有它，分层就只是几个 env 变量。
        """
        effective: dict[str, str] = {}
        for kind in CODEX_KINDS:
            effective[kind] = self.settings.model_for(kind) or "全局默认档"
        tiered = [
            kind
            for kind in CODEX_KINDS
            if self.settings.model_for(kind) and self.settings.model_for(kind) != self.settings.codex_model
        ]
        return {"effective": effective, "tiered": tiered, "warnings": self.settings.model_tier_warnings()}

    def _root_node(self, db: Session, task: TaskNode) -> TaskNode:
        cursor = task
        while cursor.parent_id:
            parent = db.get(TaskNode, cursor.parent_id)
            if parent is None:
                break
            cursor = parent
        return cursor

    def _freeze_acceptance_surface(
        self, db: Session, root: TaskNode, *, data: dict[str, Any], version: int | None, frozen_by: str
    ) -> dict[str, Any] | None:
        """把宪法声明的**验收面**指纹冻到根节点 meta 上（B 部分）。

        只在**人**的动作上冻结：根节点创建（= 宪法已锁定）与人工修订宪法。产出方没有任何路径能重新冻结，
        所以"改考卷"要么被阻断债抓住，要么得人自己动手——那时 `frozen_by` 会留下是谁批的。
        """
        paths, invalid = verification_surface.declared_paths(data.get(verification_surface.DECLARED_KEY))
        if not paths and not invalid:
            return None  # 未声明 → 不判（不许把"没声明"读成"已通过"）
        target = self._root_target_path(db, root)
        if target is None:
            self._add_event(
                db,
                root.id,
                "constitution.acceptance_surface_skipped",
                "宪法声明了验收面，但根目标路径缺失，无法冻结指纹（本次不判）。",
                {"invalid": invalid},
            )
            return None
        frozen = verification_surface.freeze(
            target, paths, version=version, frozen_by=frozen_by, now_iso=utcnow().isoformat()
        )
        meta = loads(root.node_meta_json, {})
        meta[verification_surface.SURFACE_KEY] = frozen
        root.node_meta_json = dumps(meta)
        self._add_event(
            db,
            root.id,
            "constitution.acceptance_surface_frozen",
            f"验收面已冻结：{len(frozen['digests'])} 个文件（{frozen_by} 批）。",
            {"frozen": frozen, "invalid": invalid},
        )
        return frozen

    def _acceptance_surface_frozen(self, db: Session, task: TaskNode) -> dict[str, Any] | None:
        meta = loads(self._root_node(db, task).node_meta_json, {})
        frozen = meta.get(verification_surface.SURFACE_KEY)
        return frozen if isinstance(frozen, dict) and frozen.get("digests") else None

    def _record_acceptance_surface_obligations(self, db: Session, task: TaskNode) -> None:
        """验收面篡改门禁（B 部分）。**必须在 `_obligation_gate` 之前调用**。

        与 R5 的跨版本回归门禁刻意有两点不同：

        1. **不设版本门**。改考卷在任何版本下都是改考卷——"同一版内的正常返工"这个理由在**判据文件**上
           不成立（返工不需要改判据）。
        2. **严重度不接受档位**（`obligations.surface_severity` 恒为 blocking）。
        未声明 / 未冻结 / 目标路径缺失 → **不判**：判不了就说判不了，不许拿"没声明"当通过。
        """
        frozen = self._acceptance_surface_frozen(db, task)
        if not frozen:
            return
        target = self._root_target_path(db, task)
        if target is None:
            return
        result = verification_surface.diff(frozen, target)
        incoming = obligations.obligations_from_acceptance_surface(
            node_id=task.id, diff=result, current_version=task.constitution_version, depth=task.depth
        )
        restored = {
            obligations.obligation_id(obligations.ACCEPTANCE_SURFACE_TAMPERED, task.id, f"{kind}|{path}")
            for kind in ("changed", "missing")
            for path in result.get("intact") or []
        }
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        unsettled_ids = {str(item.get("id") or "") for item in existing if not item.get("settled_at")}
        new_ids = sorted(
            str(item.get("id") or "") for item in incoming if str(item.get("id") or "") not in unsettled_ids
        )
        merged = obligations.upsert(obligations.settle_surface_restored(existing, restored_ids=restored), incoming)
        # "读不了"要留痕（**不**阻断，但也不能没记录）：按**集合是否变化**去重，免得每轮验收都刷一条事件
        unreadable = sorted(str(path) for path in result.get("unreadable") or [])
        previous_unreadable = meta.get("acceptance_surface_unreadable")
        if merged == existing and unreadable == (previous_unreadable or []):
            return
        meta["obligations"] = merged
        if unreadable:
            meta["acceptance_surface_unreadable"] = unreadable
        else:
            meta.pop("acceptance_surface_unreadable", None)
        task.node_meta_json = dumps(meta)
        if new_ids:
            self._add_event(
                db,
                task.id,
                "parent.acceptance_surface_tampered",
                verification_surface.reason(result, current_version=task.constitution_version),
                {"obligations": incoming, "diff": result},
            )
        if unreadable and unreadable != (previous_unreadable or []):
            self._add_event(
                db,
                task.id,
                "parent.acceptance_surface_unjudged",
                f"验收面有 {len(unreadable)} 个文件读盘失败，这一轮**未判定**（不计入篡改）：{unreadable[:5]}",
                {"unreadable": unreadable},
            )

    def _record_requirement_obligations(self, db: Session, task: TaskNode) -> None:
        """需求→证据追溯门禁（A 部分）。**必须在 `_obligation_gate` 之前调用**。

        它回答的是"宪法**符合目标**吗"：每条声明的原始要求都必须至少有一条可核对判据，且判据真的过。
        没有判据 = 漏需求（blocking）；判据没过 = 没做到（blocking）；判据**判不了** = 不判定（单列 + 事件）。

        未声明 `requirements` → **不判**（但会在节点 meta 里留下"未声明"，不许把它读成"已覆盖"）。
        """
        root = self._root_node(db, task)
        data = loads(root.constitution_json, {})
        declared, problems = requirements.declared(data)
        if not declared:
            return
        target = self._root_target_path(db, task)
        if target is None:
            return
        result = requirements.evaluate(declared, target, python_executable=sys.executable)
        missing = requirements.uncovered(declared)
        incoming = obligations.obligations_from_requirements(
            node_id=task.id,
            uncovered=missing,
            unmet=result["unmet"],
            current_version=task.constitution_version,
            depth=task.depth,
        )
        covered = {str(item["id"]) for item in declared if item.get("criteria")}
        restored = {
            obligations.obligation_id(obligations.REQUIREMENT_UNCOVERED, task.id, requirement_id)
            for requirement_id in covered
        } | {
            obligations.obligation_id(
                obligations.REQUIREMENT_UNMET, task.id, f"{item['requirement_id']}|{item['criterion_id']}"
            )
            for item in result["met"]
        }
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        unsettled_ids = {str(item.get("id") or "") for item in existing if not item.get("settled_at")}
        new_ids = sorted(
            str(item.get("id") or "") for item in incoming if str(item.get("id") or "") not in unsettled_ids
        )
        merged = obligations.upsert(obligations.settle_surface_restored(existing, restored_ids=restored), incoming)
        unjudged = sorted(f"{item['requirement_id']}|{item['criterion_id']}" for item in result["unjudged"])
        matrix = {
            "declared": len(declared),
            "uncovered": len(missing),
            "unmet": len(result["unmet"]),
            "unjudged": unjudged,
            "problems": problems,
            "checked_at": utcnow().isoformat(),
        }
        # 去重只看**可比字段**：`checked_at` 每轮都变，拿它参与比较等于每轮都写一遍库
        previous_matrix = meta.get("requirement_matrix") if isinstance(meta.get("requirement_matrix"), dict) else {}
        comparable = {key: value for key, value in matrix.items() if key != "checked_at"}
        previous_comparable = {key: value for key, value in previous_matrix.items() if key != "checked_at"}
        if merged == existing and comparable == previous_comparable:
            return
        meta["obligations"] = merged
        meta["requirement_matrix"] = matrix
        task.node_meta_json = dumps(meta)
        if new_ids:
            self._add_event(
                db,
                task.id,
                "parent.requirement_gap",
                requirements.summary_line(declared, result),
                {"obligations": incoming, "matrix": matrix},
            )
        if unjudged and unjudged != (previous_matrix.get("unjudged") or []):
            self._add_event(
                db,
                task.id,
                "parent.requirement_unjudged",
                f"需求判据有 {len(unjudged)} 条**没判定成**（读不到 / 超时 / 越界），本轮不计入通过也不计入失败。",
                {"unjudged": unjudged},
            )

    def _flag_cross_node_leaks(self, db: Session, task: TaskNode, text: str) -> None:
        """节点产物里出现兄弟节点路径 → 记 **advisory** 债（`docs/spec-task-isolation.md` §2.3）。

        这是**侦探**不是防线：同账号下读隔离做不到（ACL 的粒度是用户不是进程），所以能做的就是
        "越权留下痕迹、痕迹可核对"。排除名单必须包含自己的 worktree、根目标与集成目标——
        它们出现在自己的产物里是正常的，误报会把真泄露淹掉。
        """
        if not str(text or "").strip():
            return
        nodes = list(db.scalars(select(TaskNode)).all())
        paths = isolation.worktree_paths(str(self.settings.workspace_dir), [node.id for node in nodes])
        own = [
            str(task.worktree_path or ""),
            str(self._integration_target_path(db, task) or ""),
            str(self._root_target_path(db, task) or ""),
        ]
        siblings = [path for node_id, path in paths.items() if node_id != task.id]
        found = isolation.leaks(text, own_paths=own, sibling_paths=siblings)
        if not found:
            return
        meta = loads(task.node_meta_json, {})
        existing = meta.get("obligations") if isinstance(meta.get("obligations"), list) else []
        incoming = obligations.obligations_from_leaks(node_id=task.id, found=found, depth=task.depth)
        merged = obligations.upsert(existing, incoming)
        if merged == existing:
            return
        meta["obligations"] = merged
        task.node_meta_json = dumps(meta)
        self._add_event(
            db,
            task.id,
            "task.cross_node_leak",
            isolation.summary_line(found),
            {"leaks": found, "obligations": incoming},
        )

    def _worktree_declared_outputs(self, task: TaskNode) -> list[str]:
        """节点**自己声明**的产物：叶子看 worker 的 `changed_files`，父节点看集成明细。"""
        meta = loads(task.node_meta_json, {})
        integration_meta = meta.get("parent_integration")
        if isinstance(integration_meta, dict):
            integrated = integration_meta.get("integrated_files")
            if isinstance(integrated, list) and integrated:
                return [str(item) for item in integrated if str(item).strip()]
        result = loads(task.result_json or "{}", {})
        changed = result.get("changed_files") if isinstance(result, dict) else None
        if not isinstance(changed, list):
            worker = result.get("worker") if isinstance(result, dict) else None
            changed = worker.get("changed_files") if isinstance(worker, dict) else None
        return [str(item) for item in (changed or []) if str(item).strip()]

    def _worktree_archive_root(self, db: Session, task: TaskNode) -> Path | None:
        """这个节点的产物会被谁归档：**它父节点的集成目标**；没有父节点 → `None`（未判定，不回收）。"""
        if not task.parent_id:
            return None
        parent = db.get(TaskNode, task.parent_id)
        if parent is None:
            return None
        return self._integration_target_path(db, parent)

    def _worktree_recycle_decision(self, db: Session, task: TaskNode, *, now: float | None = None) -> dict[str, Any]:
        # 时间换算必须走 `coerce_utc`：数据库里存的是 **naive UTC**，直接 `.timestamp()` 会按本地时区解释，
        # 在 UTC+8 下"刚完成"就变成 8 小时前 —— 保留期当场失效（测试抓到的真 bug）。
        finished = coerce_utc(task.updated_at)
        finished_at = finished.timestamp() if finished is not None else None
        # 父节点是否已定格：整条证据链的关键。父节点还在跑，它就要读这个 worktree 里的产物
        # （`_integrate_child_outputs` 的源就是这个目录），此时删掉等于把未整合的产物丢了。
        parent = db.get(TaskNode, task.parent_id) if task.parent_id else None
        parent_terminal = None if parent is None else str(parent.status or "") in worktrees.RECYCLABLE_STATUSES
        return worktrees.decide(
            enabled=bool(self.settings.worktree_recycle_enabled),
            status=str(task.status or ""),
            worktree_path=task.worktree_path,
            workspace_dir=self.settings.workspace_dir,
            changed_files=self._worktree_declared_outputs(task),
            archive_root=self._worktree_archive_root(db, task),
            source_root=task.worktree_path,
            parent_terminal=parent_terminal,
            finished_at=finished_at,
            now=now,
            retention_seconds=max(0, int(self.settings.worktree_retention_seconds)),
        )

    def _recycled_worktree_meta(self, task: TaskNode) -> dict[str, Any]:
        meta = loads(task.node_meta_json, {})
        record = meta.get("worktree_recycled")
        return record if isinstance(record, dict) else {}

    def _worktree_lifecycle_state(self, db: Session, tasks: list[TaskNode]) -> dict[str, Any]:
        """R3：worktree 生命周期看板——有多少、多少已回收、以及**为什么还被留着**。

        与清扫共用同一套判据：看板说"该回收"而清扫没动它，那才是 bug。
        """
        state: dict[str, Any] = {"with_worktree": 0, "recycled": 0, "recyclable_now": 0, "by_skip_reason": {}}
        now = time.time()
        for task in tasks:
            if not task.worktree_path:
                continue
            state["with_worktree"] += 1
            if self._recycled_worktree_meta(task):
                state["recycled"] += 1
                continue
            decision = self._worktree_recycle_decision(db, task, now=now)
            if decision.get("recycle"):
                state["recyclable_now"] += 1
                continue
            reason = str(decision.get("reason") or "unknown")
            state["by_skip_reason"][reason] = int(state["by_skip_reason"].get(reason, 0)) + 1
        return state

    def _run_git_worktree_command(
        self, db: Session, root_target: Path, args: list[str], task: TaskNode
    ) -> tuple[bool, str]:
        result = subprocess.run(["git", "-C", str(root_target), *args], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            self._add_event(
                db,
                task.id,
                "worktree.git_command_failed",
                f"git {' '.join(args[:2])} 失败：{result.stderr.strip()[-300:]}",
                {"args": args, "returncode": result.returncode},
            )
        return result.returncode == 0, result.stderr

    def _recycle_one_worktree(self, db: Session, task: TaskNode) -> dict[str, Any]:
        """真删。git worktree 走 `remove --force` + `prune` + 删**我们自己那条**分支；拷贝式直接删目录。

        判据已在外面过完（终态 / 证据已归档 / 保留期 / 路径守卫），这里只做动作与留痕。
        删完路径还**在**就当失败——不谎报"已回收"。
        """
        path = Path(str(task.worktree_path))
        root_target = self._root_target_path(db, task)
        # 判"这是不是 git worktree"要看**这个目录自己**，而不是"根目标现在是不是 git 仓库"。
        # `_assign_worktree` 在拿不到租约或 `git worktree add` 失败时会退化成**拷贝目录**，
        # 那种目录从没被 git 注册过；按 git 走只会拿到 `fatal: not a working tree`，
        # 回收永久失败、目录永远留着（每轮还多一条失败事件）。
        is_git = bool(root_target) and (path / ".git").exists()
        branch = str(task.branch or "")
        branch_deleted = False
        if is_git:
            with worktrees.resource_lease(
                self.settings.workspace_dir, worktrees.lease_key(root_target), wait_seconds=30
            ) as acquired:
                if not acquired:
                    return {"recycled": False, "reason": "lease"}
                removed, _stderr = self._run_git_worktree_command(
                    db, root_target, ["worktree", "remove", "--force", str(path)], task
                )
                if not removed:
                    # remove 失败就别再动 prune 与分支：删一半比不删更糟（留下说不清的现场）
                    return {"recycled": False, "reason": "git_remove_failed"}
                self._run_git_worktree_command(db, root_target, ["worktree", "prune"], task)
                if branch.startswith("codex/node-"):
                    listed, _ = self._run_git_worktree_command(db, root_target, ["branch", "--list", branch], task)
                    if listed:
                        branch_deleted, _ = self._run_git_worktree_command(
                            db, root_target, ["branch", "-D", branch], task
                        )
        else:
            shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            return {"recycled": False, "reason": "still_present"}
        meta = loads(task.node_meta_json, {})
        meta["worktree_recycled"] = {
            "path": str(path),
            "branch": branch,
            "branch_deleted": branch_deleted,
            "at": utcnow().isoformat(),
        }
        task.node_meta_json = dumps(meta)
        self._add_event(
            db,
            task.id,
            "worktree.recycled",
            "终态节点的 worktree 已回收（产物已归档、保留期已过）。",
            {"path": str(path), "branch": branch, "branch_deleted": branch_deleted},
        )
        return {"recycled": True, "branch_deleted": branch_deleted}

    def _recycle_finished_worktrees(self, db: Session, root_id: str) -> dict[str, Any]:
        """R3：回收"终态 + 证据已归档 + 过了保留期 + 在受管根下"的 worktree（含 git 分支）。

        保守三原则都在 `worktrees.decide` 里；这里只按判据执行、留痕、计数。
        每次最多回收 `worktree_recycle_limit_per_sweep` 个；已回收过的直接跳过（幂等）。
        """
        limit = max(1, int(self.settings.worktree_recycle_limit_per_sweep))
        all_tasks = list(db.scalars(select(TaskNode)).all())
        summary: dict[str, Any] = {"scanned": 0, "recycled": 0, "skipped": 0, "failed": 0, "by_skip_reason": {}}
        # **深先浅后**：父节点的 worktree 同时是它子节点的归档处（`_worktree_archive_root` 就是父的
        # worktree）。先删父等于把子的证据一起删掉——子的证据判据随即失败、从此永久不可回收，
        # 而且看板会把它报成"证据找不到（那本身就是要处理的问题）"，一条我们自己制造的假警报。
        ordered = sorted(
            self._subtree(all_tasks, root_id),
            key=lambda item: (-int(item.depth or 0), item.created_at),
        )
        for task in ordered:
            if not task.worktree_path or self._recycled_worktree_meta(task):
                continue
            summary["scanned"] += 1
            decision = self._worktree_recycle_decision(db, task)
            if not decision.get("recycle"):
                reason = str(decision.get("reason") or "unknown")
                summary["by_skip_reason"][reason] = int(summary["by_skip_reason"].get(reason, 0)) + 1
                summary["skipped"] += 1
                continue
            if summary["recycled"] >= limit:
                break
            outcome = self._recycle_one_worktree(db, task)
            if outcome.get("recycled"):
                summary["recycled"] += 1
            else:
                summary["failed"] += 1
                summary["by_skip_reason"]["recycle_failed"] = (
                    int(summary["by_skip_reason"].get("recycle_failed", 0)) + 1
                )
        if summary["recycled"] or summary["failed"]:
            self._add_event(
                db,
                root_id,
                "worktree.swept",
                f"worktree 清扫：回收 {summary['recycled']} 个，失败 {summary['failed']} 个，跳过 {summary['skipped']} 个。",
                summary,
            )
        return summary

    def _assign_worktree(self, db: Session, task: TaskNode) -> None:
        if task.worktree_path:
            return
        safe_id = worktrees.safe_node_id(task.id)
        task.branch = f"codex/node-{safe_id}"
        # 工作树路径由 `worktrees.worktree_path` 唯一构造——删除守卫
        #（`is_managed_worktree`）与泄漏检测（`isolation.worktree_paths`）都委托同一个函数。
        # 以前这里是同一约定的第三份副本，三处漂移会静默地让守卫/检测失准。
        task.worktree_path = str(worktrees.worktree_path(self.settings.workspace_dir, task.id))
        worktree_path = Path(task.worktree_path)
        target_path = self._root_target_path(db, task)
        if target_path and self._is_git_repo(target_path):
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
            # R3：同一个目标仓库上的 `git worktree` 必须串行——并发创建会互相踩。
            # 拿不到租约就**不硬上**：退化成拷贝目录（下面那段），比两个 git 命令打架便宜得多。
            with worktrees.resource_lease(
                self.settings.workspace_dir, worktrees.lease_key(target_path), wait_seconds=60
            ) as acquired:
                if acquired:
                    command = ["git", "-C", str(target_path), "worktree", "add", "-b", task.branch, str(worktree_path)]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                    if result.returncode == 0:
                        self._add_event(
                            db, task.id, "worktree.created", "已为叶子执行器创建 Git worktree。", {"path": str(worktree_path)}
                        )
                        return
                    self._add_event(
                        db,
                        task.id,
                        "worktree.fallback",
                        "Git worktree 创建失败，改用隔离文件夹。",
                        {"stderr": result.stderr[-800:]},
                    )
                else:
                    self._add_event(
                        db,
                        task.id,
                        "worktree.lease_timeout",
                        "未拿到 worktree 创建租约，本轮改用隔离文件夹（不阻塞调度）。",
                        {"target": str(target_path)},
                    )
        worktree_path.mkdir(parents=True, exist_ok=True)
        if target_path and target_path.exists() and target_path.resolve() != worktree_path.resolve():
            copied = self._seed_worktree_from_target(target_path, worktree_path)
            if copied:
                self._add_event(
                    db,
                    task.id,
                    "worktree.seeded",
                    "已将非 Git 目标目录的现有文件复制到隔离 worktree，供叶子节点继续递归开发。",
                    {"source": str(target_path), "worktree": str(worktree_path), "copied_files": copied[:50], "copied_count": len(copied)},
                )

    def _seed_worktree_from_target(self, target_path: Path, worktree_path: Path) -> list[str]:
        copied: list[str] = []
        ignored_dirs = {".git", ".godot", "node_modules", ".venv", "venv", "__pycache__", ".codex_pycache"}
        for source in target_path.rglob("*"):
            rel = source.relative_to(target_path)
            if any(part in ignored_dirs for part in rel.parts):
                continue
            destination = worktree_path / rel
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(str(rel))
        return copied

    def _root_target_path(self, db: Session, task: TaskNode) -> Path | None:
        cursor = task
        while cursor.parent_id:
            parent = db.get(TaskNode, cursor.parent_id)
            if parent is None:
                break
            cursor = parent
        scope = loads(cursor.scope_json, [])
        if not scope:
            return None
        path = Path(scope[0])
        return path

    def _is_git_repo(self, path: Path) -> bool:
        """这个路径是不是一个 Git 工作树。

        **探针的"否"包含三种情况**，而它们都必须让调用方走降级路径（拷贝目录），不能让异常逃出去：

        | 情况 | 返回值 |
        |---|---|
        | git 不存在（`PATH` 上没有） | `False` |
        | git 在、但这个路径不是仓库 | `False` |
        | 超时、权限、其它 OSError | `False` |

        为什么必须吞掉：git 缺失时 `subprocess.run(["git", ...])` 抛 `FileNotFoundError`。
        原先它从这里一路逃到 `run_tree`（无人接管），树**静默死在 planning 态**——任务既不是
        `blocked` 也没有事件，看门狗因此看不见它。降级逻辑本身没问题，只是从来没机会执行：
        它写在 `_assign_worktree` 里 `_is_git_repo` **之后**（实测 2026-10-14）。

        探针的"否"不区分具体原因是有意的：调用方对三种情况的处理完全相同（改用隔离文件夹），
        真正的诊断由降级时发的 `worktree.fallback` / `worktree.seeded` 事件承担。

        仍然**必须真的调用 git**，不能只查 `.git` 存在性：一个嵌套在别的仓库里、但自己没有
        `.git` 的目录，两者给出的答案不同，而这里问的是"能不能对它跑 `git worktree add`"。
        """
        try:
            result = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.SubprocessError):
            # OSError 覆盖 FileNotFoundError（git 不在）、PermissionError、一般 OSError；
            # SubprocessError 覆盖 TimeoutExpired。探测失败一律按"不是 Git 仓库"处理。
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _record_run(self, db: Session, task: TaskNode, kind: RunKind, envelope, sandbox: str) -> None:
        status = RunStatus.SUCCEEDED.value if envelope.exit_code == 0 else RunStatus.FAILED.value
        db.add(
            RunRecord(
                task_id=task.id,
                kind=kind.value,
                status=status,
                command=envelope.command,
                sandbox=sandbox,
                exit_code=envelope.exit_code,
                result_json=dumps(envelope.payload),
                logs_path=envelope.logs_path,
                prompt_path=envelope.prompt_path,
                last_message_path=envelope.last_message_path,
                workdir=envelope.workdir or task.worktree_path,
                constitution_hash=task.constitution_hash,
                constitution_version=task.constitution_version,
                usage_json=dumps(getattr(envelope, "usage", None)),
                # P5-5：发起这次运行的请求 id。调度触发的运行为 None（**不是忘了记**）。
                request_id=trace.request_id_or_none(),
                started_at=utcnow(),
                finished_at=utcnow(),
            )
        )
        # R2：顺手做一次"越权留痕"的侦探（advisory，不阻断）——产物里出现兄弟节点路径就记一条债
        self._flag_cross_node_leaks(db, task, self._run_text_for_leak_scan(envelope))

    def _run_text_for_leak_scan(self, envelope) -> str:
        """拿去扫泄露的文本：结果载荷 + 终局消息（**有界读取**，最多 256KB）。

        刻意不扫完整 JSONL 日志：里面本来就可能有父节点提示词复述过的路径，误报会把真泄露淹掉。
        """
        chunks = [json.dumps(getattr(envelope, "payload", None), ensure_ascii=False, default=str)]
        path = str(getattr(envelope, "last_message_path", "") or "")
        try:
            target = Path(path)
            if path and target.is_file():
                with target.open("r", encoding="utf-8", errors="replace") as handle:
                    chunks.append(handle.read(262144))
        except OSError:
            pass
        return "\n".join(chunks)

    def _record_constitution_run(self, db: Session, constitution_id: str, envelope) -> None:
        self._add_event(
            db,
            None,
            "constitution.run",
            "项目宪法生成器运行完成。",
            {
                "constitution_id": constitution_id,
                "command": envelope.command,
                "exit_code": envelope.exit_code,
                "logs_path": envelope.logs_path,
                "prompt_path": envelope.prompt_path,
                "last_message_path": envelope.last_message_path,
                "workdir": envelope.workdir,
            },
        )

    def _record_synthetic_run(self, db: Session, task: TaskNode, kind: RunKind, message: str) -> None:
        db.add(
            RunRecord(
                task_id=task.id,
                kind=kind.value,
                status=RunStatus.SUCCEEDED.value,
                command="内部父节点验收",
                sandbox="read-only",
                exit_code=0,
                result_json=dumps({"message": message}),
                workdir=task.worktree_path,
                constitution_hash=task.constitution_hash,
                constitution_version=task.constitution_version,
                # 合成运行（内部父节点验收）也要能关联到触发它的请求（若有）。
                request_id=trace.request_id_or_none(),
                started_at=utcnow(),
                finished_at=utcnow(),
            )
        )

    def _constitution_head_record(self, db: Session, constitution_id: str | None) -> ConstitutionRecord | None:
        """同一条宪法链上当前生效的那一版（`root_id` 相同的最大 `version`）。"""
        if not constitution_id:
            return None
        record = db.get(ConstitutionRecord, constitution_id)
        if record is None:
            return None
        if not record.root_id:
            return record
        return self._constitution_head_record_for_root(db, record.root_id) or record

    def _constitution_head_version(self, db: Session, constitution_id: str | None) -> int | None:
        """当前生效的宪法版本号（下沉继承的"头版本"）。

        一次修订会**新增一行**而不是改旧行（`supersedes_id` 串成链），所以头版本必须按
        `root_id` 取同一条链上的最大版本，而不是取手头这一行的 `version`——否则修订之后
        新建的子节点会拿到**旧**版本号，版本下沉在这里断掉。
        """
        head = self._constitution_head_record(db, constitution_id)
        return head.version if head else None

    def _mark(self, db: Session, task: TaskNode, status: TaskStatus, message: str) -> None:
        task.status = status.value
        task.error_message = message if status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.REPAIR} else None
        task.updated_at = utcnow()
        self._add_event(db, task.id, f"task.{status.value}", message, {"status": status.value})

    def _add_event(self, db: Session, task_id: str | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
        event = EventRecord(task_id=task_id, type=event_type, message=message, payload_json=dumps(payload))
        db.add(event)
        db.flush()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(event_bus.publish(json.dumps(self.serialize_event(event), default=str)))

    def _snapshot_task(self, task_id: str) -> TaskNode:
        with SessionLocal() as db:
            task = self._get_task(db, task_id)
            db.expunge(task)
            return task

    def get_constitution(
        self, db: Session, constitution_id: str | None = None, *, root_id: str | None = None
    ) -> dict[str, Any] | None:
        """读宪法。三种查法，**都不猜**（`docs/spec-constitution-amendment.md` §4 P2）。

        原先没传 id 时取"全库最近更新的一条"——同一台机器上跑两棵树时，这会静默拿到**另一棵树**
        的宪法。后果不只是显示错：前端会据此判定"当前宪法与树上的宪法不一致"从而把树标成陈旧，
        宪法传播的版本判据也会整体判错。所以这里要求**要么显式给 id，要么能用 root 唯一定位**。
        """
        if constitution_id:
            record = db.get(ConstitutionRecord, constitution_id)
            return self.serialize_constitution(record) if record else None
        if root_id:
            head = self._constitution_head_record_for_root(db, root_id)
            return self.serialize_constitution(head) if head else None
        candidates = list(db.scalars(select(ConstitutionRecord).order_by(ConstitutionRecord.updated_at.desc())).all())
        if not candidates:
            return None
        roots = {record.root_id for record in candidates}
        if len(candidates) == 1:
            return self.serialize_constitution(candidates[0])
        if len(roots) == 1:
            # 同一个 root 的多版宪法链：返回当前生效的那一版，而不是"最近更新的那条"
            head = self._constitution_head_record_for_root(db, next(iter(roots)) or "")
            return self.serialize_constitution(head or candidates[0])
        raise ValueError(
            "存在多个任务树的宪法，无法安全推断要读哪一份；请显式给出 root_id 或 constitution_id。"
            f"候选：{self._constitution_candidate_summary(candidates[:5])}"
        )

    def _constitution_head_record_for_root(self, db: Session, root_id: str) -> ConstitutionRecord | None:
        if not root_id:
            return None
        return db.scalars(
            select(ConstitutionRecord)
            .where(ConstitutionRecord.root_id == root_id)
            .order_by(ConstitutionRecord.version.desc())
        ).first()

    def _get_task(self, db: Session, task_id: str) -> TaskNode:
        task = db.get(TaskNode, task_id)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        return task

    def _get_constitution(self, db: Session, constitution_id: str) -> ConstitutionRecord:
        record = db.get(ConstitutionRecord, constitution_id)
        if record is None:
            raise KeyError(f"Constitution not found: {constitution_id}")
        return record

    def _resolve_locked_constitution(self, db: Session, constitution_id: str | None) -> ConstitutionRecord:
        """解析启动任务树要绑定的宪法。

        **不做"猜最近一条"**：原实现取全库最近更新的 locked 记录，于是当同一机器上有两个项目
        各自锁定宪法后，后启动的那个会静默绑定到**另一个项目的宪法**上——这类"静默错绑"在
        宪法变更传播里会直接导致版本判错，属于必须掐掉的根因。

        规则改为显式或不歧义：
        1. 传了 `constitution_id` → 用它（未锁定则报错）；
        2. 没传 → 取全部 locked 候选：
           - 0 条 → 提示先生成并锁定；
           - 1 条 → 用它（单项目流程与原先完全一致）；
           - 多条 → 只在「尚未绑定任何任务树（`root_id` 为空）」的候选里挑：
             · 恰好 1 条 → 用它（支持"先草拟多个、再启动其中一个"）；
             · 否则 → **报错并列出候选**，要求调用方显式指定，绝不替调用方猜。
        """
        if constitution_id:
            record = self._get_constitution(db, constitution_id)
            if record.status != "locked":
                raise ValueError("项目宪法尚未锁定，不能启动正式任务树。")
            return record

        candidates = list(
            db.scalars(
                select(ConstitutionRecord)
                .where(ConstitutionRecord.status == "locked")
                .order_by(ConstitutionRecord.updated_at.desc())
            ).all()
        )
        if not candidates:
            raise ValueError("启动任务树前必须先生成并锁定项目宪法。")
        if len(candidates) == 1:
            return candidates[0]

        unbound = [record for record in candidates if not record.root_id]
        if len(unbound) == 1:
            return unbound[0]
        raise ValueError(
            "存在多个已锁定项目宪法，无法安全推断要绑定哪一个；请显式传入 constitution_id。"
            f"候选：{self._constitution_candidate_summary(candidates[:5])}"
        )

    @staticmethod
    def _constitution_candidate_summary(records: list[ConstitutionRecord]) -> str:
        return "；".join(
            f"{record.id}(v{record.version}, goal={repair_mojibake_text(record.goal or '')[:40] or '无'}"
            f"{', 已绑定 ' + record.root_id if record.root_id else ', 未绑定'})"
            for record in records
        )

    def _constitution_hash(self, markdown: str, data: dict[str, Any]) -> str:
        raw = dumps({"markdown": markdown, "data": data})
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _open_preview(self, root_id: str) -> None:
        if not self.settings.open_preview_on_start:
            return
        base_url = self.settings.preview_url.rstrip("/") + "/"
        url = f"{base_url}?{urlencode({'root_id': root_id})}"

        def _open() -> None:
            try:
                webbrowser.open(url, new=1, autoraise=True)
            except Exception:
                return

        threading.Thread(target=_open, daemon=True).start()


orchestrator = Orchestrator()
