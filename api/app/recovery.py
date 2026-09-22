"""崩溃恢复：从已完成的 last-message 文件把 planner / worker / verifier / acceptance 结果重新落库。

搬移自 `orchestrator.Orchestrator`。判据是 `scripts/audit_orchestrator_movable_mass.py --closure`
实测：入口集 `_recover_finished_agent_messages` / `_recover_finished_{planner,worker,verifier}_message` /
`_recover_finished_acceptance_messages` / `_read_recoverable_last_message` 闭包
**58 方法 / 1270 行**，不触达任何不可搬方法；`--state` 显示 **56/58 零实例状态**，
只有 `_external_codex_processes` 与 `_recover_finished_agent_messages` 读那个进程扫描缓存。

**范围收窄（实测决定，不是省略）**：本模块只搬 5 个 `recover_finished_*` 入口（约 325 行）。
闭包里其余的 `_create_children` / `_apply_verifier_result` / `_settle_recovered_acceptance_state` /
`serialize_task` / `_record_run` / `_add_event` / `_mark` 等是**通用基础设施与被多处共用的变更原语**，
把它们塞进"恢复"模块等于按调用图切分而不是按职责切分。因此这里沿用 W4g-c2 的 **ports 端口对象**：
按 orchestrator 的原名声明这些入口，orchestrator 直接传 `self`，函数体只做 `self.` → `ports.` 的机械替换，
状态与基础设施的所有权不变、调用点不变、现有实例级 monkeypatch 继续生效。

依赖方向：本模块只依赖 `blockers` / `models` / `utils` / `schemas` / `runner` 与标准库，**不得**导入 `orchestrator`。
（`blockers` 只提供纯函数：阻塞分类与"该不该转递归恢复"的处置策略——两条路径共用一处，避免口径漂移。）
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import blockers
from .models import (
    RunKind,
    RunRecord,
    STOPPED_STATUSES,
    TaskNode,
    TaskStatus,
    TERMINAL_STATUSES,
    utcnow,
)
from .runtime_logs import ACTIVE_AGENT_STATUSES
from .runner import RunnerEnvelope, usage_from_codex_logs
from .schemas import AcceptanceResult, PlannerDecision, VerifierResult, WorkerResult
from .utils import dumps, loads, repair_mojibake

if TYPE_CHECKING:
    from .config import Settings


class RecoveryPorts(Protocol):
    """orchestrator 侧需要提供的读取与变更入口（按原名，便于直接传 `self`）。"""

    settings: Settings
    _external_codex_process_scan_failed: bool

    def _external_codex_processes(self, max_cache_age_seconds: float = 2.0) -> list[dict[str, Any]]: ...

    def _read_recoverable_last_message(
        self, task_id: str, kind: RunKind, min_age_seconds: int
    ) -> tuple[Path, bytes, dict[str, Any]] | None: ...

    def _existing_recovered_run(self, db: Session, task: TaskNode, kind: RunKind, last_message_path: Path) -> RunRecord | None: ...

    def _recovered_envelope(self, task: TaskNode, kind: RunKind, payload: dict[str, Any], last_message_path: Path) -> Any: ...

    def _worker_result_for_recovery(self, db: Session, task: TaskNode) -> WorkerResult | None: ...

    def _pending_worker_result(self, task: TaskNode) -> WorkerResult | None: ...

    def _record_run(self, db: Session, task: TaskNode, kind: RunKind, envelope: Any, sandbox: str) -> Any: ...

    def _add_event(self, db: Session, task_id: str | None, event_type: str, message: str, payload: dict[str, Any] | None = None) -> Any: ...

    def _mark(self, db: Session, task: TaskNode, status: TaskStatus, reason: str) -> Any: ...

    def _block_task(self, db: Session, task: TaskNode, reason: str, source: str, payload: dict[str, Any] | None = None) -> Any: ...

    def _create_children(self, db: Session, parent: TaskNode, specs: Any) -> list[str]: ...

    def _fallback_decomposition(self, task: TaskNode, reason: str) -> Any: ...

    def _planner_blocked_recovery_signature(self, task: TaskNode, reason: str) -> str: ...

    def _planner_blocked_recovery_children(self, task: TaskNode, reason: str, payload: dict[str, Any], signature: str) -> Any: ...

    def _should_retry_empty_worker_decomposition(self, task: TaskNode, worker_result: WorkerResult) -> bool: ...

    def _queue_empty_worker_decomposition_retry(self, db: Session, task: TaskNode, worker_result: WorkerResult, *, source: str) -> Any: ...

    def _convert_leaf_to_parent(self, db: Session, task: TaskNode, specs: Any, source: str, reason: str, result: dict[str, Any]) -> Any: ...

    def _apply_verifier_result(self, db: Session, task: TaskNode, worker_result: WorkerResult, envelope: Any, verifier_result: VerifierResult) -> Any: ...

    def _settle_recovered_acceptance_state(self, db: Session, task: TaskNode, acceptance_result: AcceptanceResult, last_message_path: Path) -> bool: ...

    def _file_mtime_utc(self, path: Path) -> Any: ...

    def _seconds_since(self, value: Any) -> int: ...

    def serialize_task(self, task: TaskNode, include_children: bool = True) -> dict[str, Any]: ...


def recover_finished_agent_messages(
    ports: RecoveryPorts,
    db: Session,
    tasks: list[TaskNode],
    min_age_seconds: int = 120,
) -> int:
    recovered = 0
    external_processes = ports._external_codex_processes(max_cache_age_seconds=0.5)
    if ports._external_codex_process_scan_failed:
        task_ids_with_external_process = {task.id for task in tasks if task.status in ACTIVE_AGENT_STATUSES}
    else:
        task_ids_with_external_process = {
            str(process.get("task_id") or "")
            for process in external_processes
            if process.get("task_id")
        }
    for task in tasks:
        if task.status in TERMINAL_STATUSES or task.status in STOPPED_STATUSES:
            continue
        if task.id in task_ids_with_external_process:
            continue
        task_min_age_seconds = (
            0
            if task.status in ACTIVE_AGENT_STATUSES or task.status == TaskStatus.QUEUED.value
            else min_age_seconds
        )
        planner_recovered = recover_finished_planner_message(ports, db, task, task_min_age_seconds)
        if planner_recovered:
            recovered += planner_recovered
            continue
        if task.children:
            continue
        recovered += recover_finished_worker_message(ports, db, task, task_min_age_seconds)
        recovered += recover_finished_verifier_message(ports, db, task, task_min_age_seconds)
    return recovered


def recover_finished_planner_message(ports: RecoveryPorts, db: Session, task: TaskNode, min_age_seconds: int) -> int:
    if task.status not in {TaskStatus.PLANNING.value, TaskStatus.DECOMPOSING.value, TaskStatus.QUEUED.value}:
        return 0
    message = ports._read_recoverable_last_message(task.id, RunKind.PLANNER, min_age_seconds)
    if message is None:
        return 0
    last_message_path, raw, payload = message
    try:
        decision = PlannerDecision.model_validate(payload)
    except Exception:
        return 0
    existing = ports._existing_recovered_run(db, task, RunKind.PLANNER, last_message_path)
    if existing is None:
        envelope = ports._recovered_envelope(task, RunKind.PLANNER, payload, last_message_path)
        ports._record_run(db, task, RunKind.PLANNER, envelope, "read-only")

    digest = hashlib.sha1(raw).hexdigest()
    meta = loads(task.node_meta_json, {})
    if not isinstance(meta, dict):
        meta = {}
    meta["recovered_planner_last_message_sha"] = digest
    meta["recovered_planner_last_message_at"] = utcnow().isoformat()
    meta["recovered_planner_decision"] = decision.decision
    task.node_meta_json = dumps(meta)
    ports._add_event(
        db,
        task.id,
        "planner.last_message_recovered",
        "Detected a completed planner last-message without settled task state; recovered the planner decision so the tree can continue.",
        {"last_message_path": str(last_message_path), "decision": decision.decision},
    )

    if task.children:
        ports._mark(db, task, TaskStatus.WAITING_CHILDREN, "Recovered planner result; existing children will continue recursively.")
        return 1
    if decision.decision == "execute":
        if not task.result_json:
            task.result_json = dumps({"planner": decision.model_dump(), "recovered_from_planner_last_message": True})
        ports._mark(db, task, TaskStatus.QUEUED, "Recovered planner execute result; queued directly for leaf worker.")
        return 1
    if decision.decision == "decompose":
        child_ids = ports._create_children(db, task, decision.children)
        if child_ids:
            ports._mark(db, task, TaskStatus.WAITING_CHILDREN, "Recovered planner decomposition; waiting for child tasks.")
        else:
            ports._mark(db, task, TaskStatus.QUEUED, "Recovered empty planner decomposition; queued for conservative replanning.")
        return 1
    if decision.decision == "repair":
        child_ids = ports._create_children(db, task, decision.children or ports._fallback_decomposition(task, decision.reason))
        if child_ids:
            ports._mark(db, task, TaskStatus.WAITING_CHILDREN, "Recovered planner repair; waiting for recursive repair children.")
        else:
            ports._mark(db, task, TaskStatus.QUEUED, "Recovered empty planner repair; queued for conservative replanning.")
        return 1

    block_report = payload.get("block_report") if isinstance(payload.get("block_report"), dict) else {}
    # 恢复路径与正常路径**共用同一条处置策略**（`blockers.blocked_recovery_suppression`）：
    # 两边各写一份的代价是口径漂移——实测的"规划阻塞恢复"螺旋只要有一条路还无条件递归就会重现。
    # 这里拿不到工具侧构造器（本模块没有 settings），所以按 payload 里已有的报告判；链深与
    # description 这两条与正常路径完全一致。
    suppression = blockers.blocked_recovery_suppression(
        block_report,
        scope=loads(task.scope_json, []),
        has_description=bool(task.description.strip()),
    )
    if suppression:
        meta["planner_blocked_recovery_suppressed"] = suppression
        meta["planner_blocked_recovery_suppressed_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)
        ports._block_task(
            db,
            task,
            decision.reason,
            "planner.blocked",
            {**payload, "block_report": block_report},
        )
        return 1
    signature = ports._planner_blocked_recovery_signature(task, decision.reason)
    attempted = {str(item) for item in meta.get("planner_blocked_recovery_signatures", []) if item}
    if signature not in attempted:
        meta["planner_blocked_recovery_signatures"] = sorted(attempted | {signature})
        meta["planner_blocked_recovery_reason"] = decision.reason
        meta["planner_blocked_recovery_updated_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)
        child_ids = ports._create_children(
            db,
            task,
            ports._planner_blocked_recovery_children(task, decision.reason, payload, signature),
        )
        if child_ids:
            ports._mark(db, task, TaskStatus.WAITING_CHILDREN, "Recovered planner block as recursive recovery children.")
            return 1
    ports._block_task(db, task, decision.reason, "planner.blocked", payload)
    return 1


def recover_finished_worker_message(ports: RecoveryPorts, db: Session, task: TaskNode, min_age_seconds: int) -> int:
    if ports._pending_worker_result(task) is not None:
        return 0
    if "verifier" in loads(task.result_json, {}):
        return 0
    message = ports._read_recoverable_last_message(task.id, RunKind.WORKER, min_age_seconds)
    if message is None:
        return 0
    last_message_path, raw, payload = message
    try:
        worker_result = WorkerResult.model_validate(payload)
    except Exception:
        return 0
    existing = ports._existing_recovered_run(db, task, RunKind.WORKER, last_message_path)
    if existing is None:
        envelope = ports._recovered_envelope(task, RunKind.WORKER, payload, last_message_path)
        ports._record_run(db, task, RunKind.WORKER, envelope, "workspace-write")
    meta = loads(task.node_meta_json, {})
    digest = hashlib.sha1(raw).hexdigest()
    if meta.get("recovered_worker_last_message_sha") == digest and task.result_json:
        return 0
    meta["recovered_worker_last_message_sha"] = digest
    meta["recovered_worker_last_message_at"] = utcnow().isoformat()
    task.node_meta_json = dumps(meta)
    task.result_json = dumps({"worker": worker_result.model_dump(), "recovered_from_worker_last_message": True})
    if worker_result.status == "succeeded":
        task.progress_current = 1
        task.progress_total = 2
        ports._add_event(
            db,
            task.id,
            "worker.last_message_recovered",
            "检测到 worker last-message 已完成但后处理未落库；系统已恢复 worker 结果并重新排队进入 verifier。",
            {"last_message_path": str(last_message_path)},
        )
        ports._mark(db, task, TaskStatus.QUEUED, "worker 完成文件已恢复；重新排队进入叶子验收，避免重复执行 worker。")
    else:
        child_specs = worker_result.suggested_children
        if ports._should_retry_empty_worker_decomposition(task, worker_result):
            ports._queue_empty_worker_decomposition_retry(db, task, worker_result, source="worker.last_message_recovered_non_success")
        elif child_specs or worker_result.status in {"needs_decomposition", "failed"}:
            ports._convert_leaf_to_parent(
                db,
                task,
                child_specs,
                "worker.last_message_recovered_non_success",
                worker_result.summary,
                {"worker": worker_result.model_dump(), "recovered_from_worker_last_message": True},
            )
        elif worker_result.status == "blocked":
            ports._block_task(db, task, worker_result.summary, "worker.blocked", {"worker": worker_result.model_dump()})
        else:
            ports._block_task(db, task, worker_result.summary, "worker.failed", {"worker": worker_result.model_dump()})
    return 1


def recover_finished_verifier_message(ports: RecoveryPorts, db: Session, task: TaskNode, min_age_seconds: int) -> int:
    result = repair_mojibake(loads(task.result_json, {}))
    if isinstance(result, dict) and "verifier" in result:
        return 0
    worker_result = ports._worker_result_for_recovery(db, task)
    if worker_result is None:
        return 0
    message = ports._read_recoverable_last_message(task.id, RunKind.VERIFIER, min_age_seconds)
    if message is None:
        return 0
    last_message_path, raw, payload = message
    try:
        verifier_result = VerifierResult.model_validate(payload)
    except Exception:
        return 0
    existing = ports._existing_recovered_run(db, task, RunKind.VERIFIER, last_message_path)
    if existing is not None:
        if task.status in ACTIVE_AGENT_STATUSES or task.status == TaskStatus.QUEUED.value:
            envelope = ports._recovered_envelope(task, RunKind.VERIFIER, payload, last_message_path)
            ports._add_event(
                db,
                task.id,
                "verifier.recovered_state_settled",
                "Verifier last-message and run record already existed, but task state was still active; settled it from the recorded verifier result.",
                {"last_message_path": str(last_message_path), "verdict": verifier_result.verdict},
            )
            ports._apply_verifier_result(db, task, worker_result, envelope, verifier_result)
            return 1
        return 0
    envelope = ports._recovered_envelope(task, RunKind.VERIFIER, payload, last_message_path)
    meta = loads(task.node_meta_json, {})
    meta["recovered_verifier_last_message_sha"] = hashlib.sha1(raw).hexdigest()
    meta["recovered_verifier_last_message_at"] = utcnow().isoformat()
    task.node_meta_json = dumps(meta)
    ports._add_event(
        db,
        task.id,
        "verifier.last_message_recovered",
        "检测到 verifier last-message 已完成但后处理未落库；系统已恢复 verifier 结果并按正常叶子验收落库。",
        {"last_message_path": str(last_message_path), "verdict": verifier_result.verdict},
    )
    ports._apply_verifier_result(db, task, worker_result, envelope, verifier_result)
    return 1


def recover_finished_acceptance_messages(
    ports: RecoveryPorts,
    db: Session,
    tasks: list[TaskNode],
    min_age_seconds: int = 120,
) -> int:
    recovered = 0
    for task in tasks:
        if task.status != TaskStatus.ACCEPTING.value:
            continue
        run_dir = ports.settings.workspace_dir / "runs" / task.id
        last_message_path = run_dir / "acceptance-last-message.json"
        if not last_message_path.is_file():
            continue
        last_message_at = ports._file_mtime_utc(last_message_path)
        if last_message_at is None or ports._seconds_since(last_message_at) < max(0, min_age_seconds):
            continue
        try:
            raw = last_message_path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        digest = hashlib.sha1(raw).hexdigest()
        meta = loads(task.node_meta_json, {})
        existing = db.scalar(
            select(RunRecord).where(
                RunRecord.task_id == task.id,
                RunRecord.kind == RunKind.ACCEPTANCE.value,
                RunRecord.last_message_path == str(last_message_path),
            )
        )
        try:
            acceptance_result = AcceptanceResult.model_validate(payload)
        except Exception:
            continue
        if meta.get("recovered_acceptance_last_message_sha") == digest or existing is not None:
            if ports._settle_recovered_acceptance_state(db, task, acceptance_result, last_message_path):
                recovered += 1
            continue

        children = [
            child
            for child in task.children
            if child.status not in STOPPED_STATUSES
        ]
        child_payloads = [ports.serialize_task(child, False) for child in children]
        acceptance_logs_path = run_dir / "acceptance.jsonl"
        acceptance_logs_text = str(acceptance_logs_path) if acceptance_logs_path.is_file() else None
        acceptance_prompt_path = run_dir / "acceptance-prompt.txt"
        envelope = RunnerEnvelope(
            command="recovered completed acceptance last-message",
            exit_code=0,
            logs_path=acceptance_logs_text,
            payload=payload,
            prompt_path=str(acceptance_prompt_path) if acceptance_prompt_path.is_file() else None,
            last_message_path=str(last_message_path),
            workdir=task.worktree_path,
            usage=usage_from_codex_logs(acceptance_logs_text),
        )
        ports._record_run(db, task, RunKind.ACCEPTANCE, envelope, "workspace-write")
        task.result_json = dumps(
            {
                "accepted_children": [child["id"] for child in child_payloads],
                "acceptance": acceptance_result.model_dump(),
                "recovered_from_last_message": True,
            }
        )
        meta["recovered_acceptance_last_message_sha"] = digest
        meta["recovered_acceptance_last_message_at"] = utcnow().isoformat()
        task.node_meta_json = dumps(meta)

        child_ids: list[str] = []
        if acceptance_result.verdict == "repair" and acceptance_result.suggested_children:
            child_ids = ports._create_children(db, task, acceptance_result.suggested_children)
        if child_ids:
            ports._add_event(
                db,
                task.id,
                "acceptance.last_message_recovered",
                "检测到 acceptance last-message 已完成但后处理未落库；系统已恢复结果并创建递归修复子任务。",
                {"child_ids": child_ids, "last_message_path": str(last_message_path), "verdict": acceptance_result.verdict},
            )
            ports._mark(db, task, TaskStatus.WAITING_CHILDREN, "已从完成的 acceptance last-message 恢复 repair 结果，等待递归修复子任务。")
        else:
            ports._add_event(
                db,
                task.id,
                "acceptance.last_message_requeued",
                "检测到 acceptance last-message 已完成但后处理未落库；系统已记录证据并重新排队做保守恢复。",
                {"last_message_path": str(last_message_path), "verdict": acceptance_result.verdict},
            )
            ports._mark(db, task, TaskStatus.QUEUED, "acceptance 完成文件已恢复；为避免误判完成，重新排队做保守验收。")
        recovered += 1
    return recovered
