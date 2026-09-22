from __future__ import annotations

import json
from pathlib import Path

from app import recovery
from app.models import TaskNode
from app.schemas import AcceptanceResult, ChildTaskSpec, PlannerDecision, VerifierResult, WorkerResult


class _Settings:
    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir


class _Ports:
    """`recover_finished_*` 的端口替身：记录所有变更调用，便于断言分支走到哪。"""

    def __init__(self, workspace_dir: Path, **overrides: object) -> None:
        self.settings = _Settings(workspace_dir)
        self._external_codex_process_scan_failed = False
        self.external_processes: list[dict[str, object]] = []
        self.message: tuple[Path, bytes, dict] | None = None
        self.existing_run: object | None = None
        self.worker_result: WorkerResult | None = None
        self.pending_worker: WorkerResult | None = None
        self.created_children: list[str] = []
        self.settle_result = False
        self.retry_empty_decomposition = False
        self.mtime: object = None
        self.age_seconds = 99999
        self.marks: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.recorded_runs: list[str] = []
        self.converted: list[str] = []
        self.settled: list[str] = []
        self.applied_verifier: list[str] = []
        for key, value in overrides.items():
            setattr(self, key, value)

    def _external_codex_processes(self, max_cache_age_seconds: float = 2.0) -> list[dict[str, object]]:
        return self.external_processes

    def _read_recoverable_last_message(self, task_id: str, kind: object, min_age_seconds: int) -> object:
        return self.message

    def _existing_recovered_run(self, db: object, task: TaskNode, kind: object, last_message_path: Path) -> object:
        return self.existing_run

    def _recovered_envelope(self, task: TaskNode, kind: object, payload: dict, last_message_path: Path) -> object:
        return {"kind": kind, "path": str(last_message_path)}

    def _worker_result_for_recovery(self, db: object, task: TaskNode) -> WorkerResult | None:
        return self.worker_result

    def _pending_worker_result(self, task: TaskNode) -> WorkerResult | None:
        return self.pending_worker

    def _record_run(self, db: object, task: TaskNode, kind: object, envelope: object, sandbox: str) -> None:
        self.recorded_runs.append(str(getattr(kind, "value", kind)))

    def _add_event(self, db: object, task_id: object, event_type: str, message: str, payload: dict | None = None) -> None:
        self.events.append(event_type)

    def _mark(self, db: object, task: TaskNode, status: object, reason: str) -> None:
        self.marks.append((task.id, str(getattr(status, "value", status))))

    def _block_task(self, db: object, task: TaskNode, reason: str, source: str, payload: dict | None = None) -> None:
        self.blocks.append((task.id, source))

    def _create_children(self, db: object, parent: TaskNode, specs: object) -> list[str]:
        return list(self.created_children)

    def _fallback_decomposition(self, task: TaskNode, reason: str) -> list:
        return []

    def _planner_blocked_recovery_signature(self, task: TaskNode, reason: str) -> str:
        return "sig-1"

    def _planner_blocked_recovery_children(self, task: TaskNode, reason: str, payload: dict, signature: str) -> list:
        return []

    def _should_retry_empty_worker_decomposition(self, task: TaskNode, worker_result: WorkerResult) -> bool:
        return self.retry_empty_decomposition

    def _queue_empty_worker_decomposition_retry(self, db: object, task: TaskNode, worker_result: WorkerResult, *, source: str) -> None:
        self.converted.append(f"retry:{source}")

    def _convert_leaf_to_parent(self, db: object, task: TaskNode, specs: object, source: str, reason: str, result: dict) -> None:
        self.converted.append(source)

    def _apply_verifier_result(self, db: object, task: TaskNode, worker_result: WorkerResult, envelope: object, verifier_result: VerifierResult) -> None:
        self.applied_verifier.append(task.id)

    def _settle_recovered_acceptance_state(self, db: object, task: TaskNode, acceptance_result: AcceptanceResult, last_message_path: Path) -> bool:
        self.settled.append(task.id)
        return self.settle_result

    def _file_mtime_utc(self, path: Path) -> object:
        return self.mtime

    def _seconds_since(self, value: object) -> int:
        return self.age_seconds

    def serialize_task(self, task: TaskNode, include_children: bool = True) -> dict:
        return {"id": task.id}


def _task(task_id: str = "node-1", *, status: str = "queued", result: dict | None = None, description: str = "") -> TaskNode:
    return TaskNode(
        id=task_id,
        title=task_id,
        description=description,
        status=status,
        node_meta_json="{}",
        result_json=json.dumps(result) if result is not None else None,
    )


class _Db:
    """acceptance 恢复会查一次 RunRecord。默认返回 None（没有既有 run）；
    传 `existing` 可模拟「已有 run 记录」从而走到 settle 分支。"""

    def __init__(self, existing: object | None = None) -> None:
        self._existing = existing

    def scalar(self, statement: object) -> object | None:
        return self._existing


def _planner_payload(decision: str, children: list[dict] | None = None) -> dict:
    return PlannerDecision(decision=decision, reason="r", children=children or []).model_dump()  # type: ignore[arg-type]


def test_recover_finished_agent_messages_skips_terminal_and_external_tasks(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    done = _task("done-1", status="done")
    paused = _task("paused-1", status="paused")
    external = _task("ext-1", status="working")
    ports.external_processes = [{"task_id": "ext-1"}]
    assert recovery.recover_finished_agent_messages(ports, None, [done, paused, external]) == 0


def test_recover_finished_agent_messages_scan_failure_falls_back_to_active_statuses(tmp_path: Path) -> None:
    """扫描失败时必须按「活跃状态的节点都视为有外部进程」兜底，否则会误判重复执行。"""
    ports = _Ports(tmp_path, _external_codex_process_scan_failed=True)
    working = _task("w-1", status="working")
    # 扫描失败 → working 被计入「有外部进程」→ 整个循环跳过它，不产生任何恢复
    assert recovery.recover_finished_agent_messages(ports, None, [working]) == 0

    # 同一节点在扫描成功但进程列表为空时会被处理（无 last-message → 仍返回 0，但走过了分支）
    ports2 = _Ports(tmp_path)
    assert recovery.recover_finished_agent_messages(ports2, None, [_task("w-1", status="working")]) == 0


def test_recover_finished_agent_messages_stops_at_planner_recovery(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "planner-last-message.json", b"{}", _planner_payload("execute"))
    assert recovery.recover_finished_agent_messages(ports, None, [_task()]) == 1
    assert ports.marks == [("node-1", "queued")]


def test_recover_finished_worker_not_attempted_when_children_exist(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    task = _task()
    task.children = [_task("child-1", status="queued")]
    # planner 未恢复 → 有子节点 → 不再尝试 worker/verifier
    assert recovery.recover_finished_agent_messages(ports, None, [task]) == 0


def test_planner_recovery_gates(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    # 状态不在 planning/decomposing/queued
    assert recovery.recover_finished_planner_message(ports, None, _task(status="working"), 0) == 0
    # 无 last-message
    assert recovery.recover_finished_planner_message(ports, None, _task(), 0) == 0
    # payload 不合法
    ports.message = (tmp_path / "x.json", b"{}", {"decision": "not-a-valid-decision", "reason": "r"})
    assert recovery.recover_finished_planner_message(ports, None, _task(), 0) == 0


def test_planner_recovery_execute_and_event(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    path = tmp_path / "planner-last-message.json"
    ports.message = (path, b"{}", _planner_payload("execute"))
    task = _task()
    assert recovery.recover_finished_planner_message(ports, None, task, 0) == 1
    assert ports.events == ["planner.last_message_recovered"]
    assert ports.recorded_runs == ["planner"]
    assert ports.marks == [("node-1", "queued")]
    assert json.loads(task.result_json or "{}")["recovered_from_planner_last_message"] is True


def test_planner_recovery_existing_children_waits(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "p.json", b"{}", _planner_payload("execute"))
    task = _task()
    task.children = [_task("c", status="queued")]
    assert recovery.recover_finished_planner_message(ports, None, task, 0) == 1
    assert ports.marks == [("node-1", "waiting_children")]
    # run 记录在 children 判断之前落库，因此这里仍然会记一次
    assert ports.recorded_runs == ["planner"]


def test_planner_recovery_decompose_empty_and_filled(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "p.json", b"{}", _planner_payload("decompose"))
    assert recovery.recover_finished_planner_message(ports, None, _task(), 0) == 1
    assert ports.marks == [("node-1", "queued")]

    ports2 = _Ports(tmp_path, created_children=["c1", "c2"])
    ports2.message = (tmp_path / "p.json", b"{}", _planner_payload("decompose"))
    assert recovery.recover_finished_planner_message(ports2, None, _task(), 0) == 1
    assert ports2.marks == [("node-1", "waiting_children")]


def test_planner_recovery_repair_and_blocked(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "p.json", b"{}", _planner_payload("repair"))
    assert recovery.recover_finished_planner_message(ports, None, _task(), 0) == 1
    assert ports.marks == [("node-1", "queued")]

    # blocked + tool_internal → 走 tool_internal 的 pause 分支，不尝试自救子任务
    payload = _planner_payload("blocked")
    payload["block_report"] = {"category": "tool_internal", "parent_action": "pause_tool_fix"}
    ports2 = _Ports(tmp_path)
    ports2.message = (tmp_path / "p.json", b"{}", payload)
    assert recovery.recover_finished_planner_message(ports2, None, _task(description="有目标"), 0) == 1
    assert ports2.blocks == [("node-1", "planner.blocked")]
    assert ports2.converted == []

    # blocked + 有描述 + 首次签名 → 尝试自救子任务；子任务为空则退回 block
    ports3 = _Ports(tmp_path)
    ports3.message = (tmp_path / "p.json", b"{}", _planner_payload("blocked"))
    assert recovery.recover_finished_planner_message(ports3, None, _task(description="有目标"), 0) == 1
    assert ports3.blocks == [("node-1", "planner.blocked")]

    # blocked + 无描述 → 直接 block
    ports4 = _Ports(tmp_path)
    ports4.message = (tmp_path / "p.json", b"{}", _planner_payload("blocked"))
    recovery.recover_finished_planner_message(ports4, None, _task(), 0)
    assert ports4.blocks == [("node-1", "planner.blocked")]


def test_worker_recovery_gates(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.pending_worker = WorkerResult(status="succeeded", summary="s")
    assert recovery.recover_finished_worker_message(ports, None, _task(), 0) == 0

    ports2 = _Ports(tmp_path)
    assert recovery.recover_finished_worker_message(ports2, None, _task(result={"verifier": {}}), 0) == 0

    ports3 = _Ports(tmp_path)
    assert recovery.recover_finished_worker_message(ports3, None, _task(), 0) == 0


def test_worker_recovery_succeeded_requeues(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "w.json", b"{}", WorkerResult(status="succeeded", summary="s").model_dump())
    task = _task()
    assert recovery.recover_finished_worker_message(ports, None, task, 0) == 1
    assert ports.marks == [("node-1", "queued")]
    assert ports.events == ["worker.last_message_recovered"]
    assert task.progress_current == 1
    assert task.progress_total == 2


def test_worker_recovery_non_success_paths(tmp_path: Path) -> None:
    # needs_decomposition → 转父节点
    ports = _Ports(tmp_path)
    ports.message = (tmp_path / "w.json", b"{}", WorkerResult(status="needs_decomposition", summary="s").model_dump())
    assert recovery.recover_finished_worker_message(ports, None, _task(), 0) == 1
    assert ports.converted == ["worker.last_message_recovered_non_success"]

    # blocked → block_task(worker.blocked)
    ports2 = _Ports(tmp_path)
    ports2.message = (tmp_path / "w.json", b"{}", WorkerResult(status="blocked", summary="s").model_dump())
    assert recovery.recover_finished_worker_message(ports2, None, _task(), 0) == 1
    assert ports2.blocks == [("node-1", "worker.blocked")]

    # failed（无建议子任务）→ 仍然走「转父节点」：status=failed 也在该分支的集合里
    ports3 = _Ports(tmp_path)
    ports3.message = (tmp_path / "w.json", b"{}", WorkerResult(status="failed", summary="s").model_dump())
    assert recovery.recover_finished_worker_message(ports3, None, _task(), 0) == 1
    assert ports3.converted == ["worker.last_message_recovered_non_success"]
    assert ports3.blocks == []

    # 空分解重试优先于转父节点
    ports4 = _Ports(tmp_path, retry_empty_decomposition=True)
    ports4.message = (tmp_path / "w.json", b"{}", WorkerResult(status="failed", summary="s").model_dump())
    assert recovery.recover_finished_worker_message(ports4, None, _task(), 0) == 1
    assert ports4.converted == ["retry:worker.last_message_recovered_non_success"]


def test_worker_recovery_skips_duplicate_digest(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    raw = b"{}"
    import hashlib

    ports.message = (tmp_path / "w.json", raw, WorkerResult(status="succeeded", summary="s").model_dump())
    task = _task(result={"worker": {}})
    task.node_meta_json = json.dumps({"recovered_worker_last_message_sha": hashlib.sha1(raw).hexdigest()})
    assert recovery.recover_finished_worker_message(ports, None, task, 0) == 0


def test_verifier_recovery_gates_and_settle(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    # 已有 verifier 结果
    assert recovery.recover_finished_verifier_message(ports, None, _task(result={"verifier": {}}), 0) == 0

    # 没有可用的 worker 结果
    assert recovery.recover_finished_verifier_message(_Ports(tmp_path), None, _task(), 0) == 0

    # 有 worker 结果但无 last-message
    ports2 = _Ports(tmp_path, worker_result=WorkerResult(status="succeeded", summary="s"))
    assert recovery.recover_finished_verifier_message(ports2, None, _task(), 0) == 0


def test_verifier_recovery_settles_active_task_with_existing_run(tmp_path: Path) -> None:
    payload = VerifierResult(verdict="pass", reason="r").model_dump()
    ports = _Ports(tmp_path, worker_result=WorkerResult(status="succeeded", summary="s"), existing_run=object())
    ports.message = (tmp_path / "v.json", b"{}", payload)
    assert recovery.recover_finished_verifier_message(ports, None, _task(status="queued"), 0) == 1
    assert ports.events == ["verifier.recovered_state_settled"]
    assert ports.applied_verifier == ["node-1"]


def test_verifier_recovery_ignores_existing_run_for_inactive_task(tmp_path: Path) -> None:
    payload = VerifierResult(verdict="pass", reason="r").model_dump()
    ports = _Ports(tmp_path, worker_result=WorkerResult(status="succeeded", summary="s"), existing_run=object())
    ports.message = (tmp_path / "v.json", b"{}", payload)
    assert recovery.recover_finished_verifier_message(ports, None, _task(status="done"), 0) == 0
    assert ports.applied_verifier == []


def test_verifier_recovery_fresh_path_applies_result(tmp_path: Path) -> None:
    payload = VerifierResult(verdict="repair", reason="r").model_dump()
    ports = _Ports(tmp_path, worker_result=WorkerResult(status="succeeded", summary="s"))
    ports.message = (tmp_path / "v.json", b"{}", payload)
    task = _task(status="verifying")
    assert recovery.recover_finished_verifier_message(ports, None, task, 0) == 1
    assert ports.events == ["verifier.last_message_recovered"]
    # 全新恢复路径不落 run：既有 run 的缺失由 last-message 直接兜底
    assert ports.recorded_runs == []
    assert ports.applied_verifier == ["node-1"]


def test_acceptance_recovery_gates(tmp_path: Path) -> None:
    ports = _Ports(tmp_path)
    # 非 accepting 状态直接跳过
    assert recovery.recover_finished_acceptance_messages(ports, None, [_task(status="working")]) == 0


def test_acceptance_recovery_skips_fresh_or_broken_messages(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "node-1"
    run_dir.mkdir(parents=True)
    target = run_dir / "acceptance-last-message.json"
    target.write_text(json.dumps(AcceptanceResult(verdict="pass", reason="r").model_dump()), encoding="utf-8")

    # mtime 太新 → 跳过
    ports = _Ports(tmp_path, mtime=object(), age_seconds=0)
    assert recovery.recover_finished_acceptance_messages(ports, None, [_task(status="accepting")]) == 0

    # 内容不是合法 JSON → 跳过
    target.write_text("{not json", encoding="utf-8")
    ports2 = _Ports(tmp_path, mtime=object(), age_seconds=99999)
    assert recovery.recover_finished_acceptance_messages(ports2, None, [_task(status="accepting")]) == 0


def test_acceptance_recovery_settle_branch_counts(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "node-1"
    run_dir.mkdir(parents=True)
    (run_dir / "acceptance-last-message.json").write_text(
        json.dumps(AcceptanceResult(verdict="pass", reason="r").model_dump()), encoding="utf-8"
    )
    ports = _Ports(tmp_path, mtime=object(), age_seconds=99999, settle_result=True)
    # 已有 run 记录（或 sha 已记录）时走 settle 分支
    assert recovery.recover_finished_acceptance_messages(ports, _Db(existing=object()), [_task(status="accepting")]) == 1
    assert ports.settled == ["node-1"]
    assert ports.created_children == []

    # settle 返回 False → 不计数
    ports2 = _Ports(tmp_path, mtime=object(), age_seconds=99999, settle_result=False)
    assert recovery.recover_finished_acceptance_messages(ports2, _Db(existing=object()), [_task(status="accepting")]) == 0


def test_acceptance_recovery_repair_creates_children(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "node-1"
    run_dir.mkdir(parents=True)
    # repair 必须带 suggested_children 才会创建子任务，否则退回保守重排
    payload = AcceptanceResult(
        verdict="repair",
        reason="r",
        suggested_children=[ChildTaskSpec(title="修复子任务")],
    ).model_dump()
    (run_dir / "acceptance-last-message.json").write_text(json.dumps(payload), encoding="utf-8")

    task = _task(status="accepting")
    ports = _Ports(tmp_path, mtime=object(), age_seconds=99999, created_children=["fix-1"])
    assert recovery.recover_finished_acceptance_messages(ports, _Db(), [task]) == 1
    assert ports.events == ["acceptance.last_message_recovered"]
    assert ports.marks == [("node-1", "waiting_children")]
    assert ports.recorded_runs == ["acceptance"]
    assert json.loads(task.result_json or "{}")["recovered_from_last_message"] is True

    # 无建议子任务 → 重新排队做保守验收
    task2 = _task(status="accepting")
    ports2 = _Ports(tmp_path, mtime=object(), age_seconds=99999)
    assert recovery.recover_finished_acceptance_messages(ports2, _Db(), [task2]) == 1
    assert ports2.events == ["acceptance.last_message_requeued"]
    assert ports2.marks == [("node-1", "queued")]
