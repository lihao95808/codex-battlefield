from __future__ import annotations

import json

from app import convergence
from app.models import TaskNode
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.max_repeated_child_signature = 3
        self.parent_skill_note_task_enabled = False
        self.runner_mode = "mock"
        for key, value in overrides.items():
            setattr(self, key, value)


def _task(
    task_id: str = "node-1",
    *,
    title: str = "普通节点",
    status: str = "queued",
    meta: dict | None = None,
    scope: list[str] | None = None,
    acceptance: list[str] | None = None,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        title=title,
        status=status,
        node_meta_json=json.dumps(meta or {}),
        scope_json=json.dumps(scope or []),
        acceptance_json=json.dumps(acceptance or []),
    )


def _spec(title: str = "子任务", **overrides: object) -> ChildTaskSpec:
    return ChildTaskSpec(title=title, **overrides)


def test_fallback_event_message_known_types() -> None:
    assert "目标目录" in convergence.fallback_event_message("tree.reconstructed", {"target_path": "D:/x"})
    assert "D:/x" in convergence.fallback_event_message("tree.reconstructed", {"target_path": "D:/x"})
    assert "root-9" in convergence.fallback_event_message("tree.snapshot_restored", {"restored_root_id": "root-9"})
    assert convergence.fallback_event_message("tree.hibernate", {}) == "任务树已保存状态并进入休眠，等待后续自动恢复。"
    budget = convergence.fallback_event_message("budget.guard_hibernate", {"used": 5, "work_limit": 6, "reserve": 1})
    assert "5/6" in budget
    assert "保留 1 次预算" in budget
    assert convergence.fallback_event_message("budget.guard_hibernate", {}) == "达到本轮 Codex agent 工作启动额度，已保存状态并休眠。"


def test_fallback_event_message_unknown_type_keeps_payload_hint() -> None:
    message = convergence.fallback_event_message("task.unknown", {})
    assert "task.unknown" in message
    assert "结构化 payload" in message


def test_is_blocked_child_repair_spec() -> None:
    assert convergence.is_blocked_child_repair_spec(_spec(coverage_ids=["blocked-child-repair"])) is True
    assert convergence.is_blocked_child_repair_spec(_spec("Blocked Child Repair 修复")) is True
    assert convergence.is_blocked_child_repair_spec(_spec("普通任务")) is False


def test_drop_blocked_repair_dependencies_gate() -> None:
    # 不是 blocked-child-repair → 原样返回
    assert convergence.drop_blocked_repair_dependencies(_spec(), ["甲"], ["id-1"], {}, {}) == (["甲"], ["id-1"], [])


def test_drop_blocked_repair_dependencies_drops_dead_dependencies() -> None:
    child = _spec(coverage_ids=["blocked-child-repair"])
    by_raw = {"甲": "id-1", "乙": "id-2"}
    siblings = {
        "id-1": _task("id-1", status="blocked"),
        "id-2": _task("id-2", status="done"),
    }
    dropped_on, dropped_ids, dropped = convergence.drop_blocked_repair_dependencies(
        child, ["甲", "乙"], ["id-1", "id-2"], by_raw, siblings
    )
    assert dropped == ["id-1"]
    assert dropped_on == ["乙"]
    assert dropped_ids == ["id-2"]

    # 没有已死依赖 → 原样返回，dropped 为空
    assert convergence.drop_blocked_repair_dependencies(child, ["甲"], ["id-1"], by_raw, {}) == (["甲"], ["id-1"], [])


def test_convergence_slice_count_takes_max_of_both_languages() -> None:
    assert convergence.convergence_slice_count("实现收敛切片") == 1
    assert convergence.convergence_slice_count("convergence slice x2 convergence slice") == 2
    assert convergence.convergence_slice_count("普通标题") == 0
    assert convergence.convergence_slice_count(None) == 0


def test_convergence_slice_loop_gates(tmp_path_unused=None) -> None:
    settings = _Settings()
    # 低于上限 → 没有循环
    assert convergence.convergence_slice_loop(_task("普通父"), [_spec("普通子")], settings) is None

    parent = _task(title="父：收敛切片 收敛切片 收敛切片")
    loop = convergence.convergence_slice_loop(parent, [_spec("子")], settings)
    assert loop is not None
    assert loop["limit"] == convergence.CONVERGENCE_SLICE_REPEAT_LIMIT
    assert loop["parent_count"] == 3
    assert loop["child_titles"] == ["子"]


def test_convergence_slice_loop_exempts_cycle_breaker_parent() -> None:
    settings = _Settings()
    meta = {"coverage_ids": ["cycle-breaker", "convergence-slice-loop"]}
    parent = _task(title="父：收敛切片 收敛切片 收敛切片", meta=meta)
    # 父节点是 cycle-breaker 且子节点计数不足 → 直接放行
    assert convergence.convergence_slice_loop(parent, [_spec("子")], settings) is None


def test_is_convergence_cycle_breaker_task() -> None:
    assert convergence.is_convergence_cycle_breaker_task(
        _task(meta={"coverage_ids": ["cycle-breaker", "convergence-slice-loop"]})
    ) is True
    assert convergence.is_convergence_cycle_breaker_task(
        _task(meta={"verification_kind": "convergence loop breaker + parent closure evidence"})
    ) is True
    # 只有其中一个 coverage_id 不算
    assert convergence.is_convergence_cycle_breaker_task(_task(meta={"coverage_ids": ["cycle-breaker"]})) is False
    assert convergence.is_convergence_cycle_breaker_task(_task()) is False


def test_convergence_cycle_breaker_base_title_strips_suffixes() -> None:
    assert convergence.convergence_cycle_breaker_base_title("战斗：递归循环诊断与收口") == "战斗"
    assert convergence.convergence_cycle_breaker_base_title("战斗: 递归循环诊断与收口") == "战斗"
    assert convergence.convergence_cycle_breaker_base_title("战斗:递归循环诊断与收口") == "战斗"
    # 没有冒号前缀时后缀不匹配，原样返回（后缀本身带冒号）
    assert convergence.convergence_cycle_breaker_base_title("递归循环诊断与收口") == "递归循环诊断与收口"
    assert convergence.convergence_cycle_breaker_base_title(None) == "递归循环诊断"


def test_is_redundant_convergence_cycle_breaker_child() -> None:
    cycle_meta = {"coverage_ids": ["cycle-breaker", "convergence-slice-loop"]}
    parent = _task(title="战斗")
    # 状态不符 → False
    assert convergence.is_redundant_convergence_cycle_breaker_child(
        parent, _task(title="战斗：递归循环诊断与收口", status="working", meta=cycle_meta)
    ) is False
    # 子节点不是 cycle-breaker → False
    assert convergence.is_redundant_convergence_cycle_breaker_child(parent, _task(status="queued")) is False
    # 基础标题相同 → True
    assert convergence.is_redundant_convergence_cycle_breaker_child(
        parent, _task(title="战斗：递归循环诊断与收口", meta=cycle_meta)
    ) is True
    # 基础标题不同但收敛切片计数达上限 → True
    assert convergence.is_redundant_convergence_cycle_breaker_child(
        parent, _task(title="另一路：收敛切片 收敛切片 收敛切片", meta=cycle_meta)
    ) is True


def test_is_legacy_convergence_slice_queued_child() -> None:
    assert convergence.is_legacy_convergence_slice_queued_child(_task(status="working", title="收敛切片 收敛切片 收敛切片")) is False
    assert convergence.is_legacy_convergence_slice_queued_child(_task(title="普通标题")) is False
    # 计数够但已经是新版 cycle-breaker → False
    assert convergence.is_legacy_convergence_slice_queued_child(
        _task(
            title="收敛切片 收敛切片 收敛切片",
            meta={"coverage_ids": ["cycle-breaker", "convergence-slice-loop"]},
        )
    ) is False
    # 计数够且不是新版 → True
    assert convergence.is_legacy_convergence_slice_queued_child(_task(title="收敛切片 收敛切片 收敛切片")) is True


def test_cycle_breaker_children_builds_spec() -> None:
    parent = _task(title="战斗", scope=["art"], acceptance=["必须整合"])
    repeated = [{"signature": "sig-1", "title": "子", "existing_or_planned_count": 3, "limit": 3}]
    specs = convergence.cycle_breaker_children(parent, repeated)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.title == "战斗：重复拆分收敛破环"
    assert spec.coverage_ids[0] == "cycle-breaker"
    assert "sig-1" in spec.coverage_ids
    assert spec.verification_kind == "cycle breaker + integration evidence"
    assert spec.estimated_leaf is False
    assert spec.scope[0] == "art"
    # 尾部追加父节点验收前 3 条
    assert spec.acceptance[-1] == "必须整合"
    assert "sig-1" in spec.description


def test_convergence_cycle_breaker_children_builds_spec() -> None:
    parent = _task(title="战斗：递归循环诊断与收口", scope=["art"], acceptance=["a1", "a2", "a3", "a4", "a5"])
    specs = convergence.convergence_cycle_breaker_children(parent, {"parent_count": 3})
    assert len(specs) == 1
    spec = specs[0]
    # 标题使用剥掉后缀后的基础标题
    assert spec.title == "战斗：递归循环诊断与收口"
    assert spec.coverage_ids == ["cycle-breaker", "convergence-slice-loop"]
    assert spec.verification_kind == "convergence loop breaker + parent closure evidence"
    assert len(spec.acceptance) == 8  # 4 条固定 + 父节点前 4 条
    assert spec.acceptance[-1] == "a4"


def test_signatures_are_deterministic_and_normalized() -> None:
    assert convergence.child_signature(_spec("甲")) == convergence.child_signature(_spec("甲"))
    # 大小写与空白差异被归一化
    assert convergence.child_signature(_spec("Ab  C")) == convergence.child_signature(_spec("ab c"))
    assert convergence.child_signature(_spec("甲")) != convergence.child_signature(_spec("乙"))
    assert convergence.task_signature(_task(title="甲")) == convergence.task_signature(_task(title="甲"))
    assert convergence.task_signature(_task(title="甲")) != convergence.task_signature(_task(title="乙"))


def test_repeated_child_signatures_gate_and_detection() -> None:
    # 上限 <= 0 → 直接关闭
    assert convergence.repeated_child_signatures(_task(), [_spec()], _Settings(max_repeated_child_signature=0)) == []

    parent = _task()
    parent.children = []
    settings = _Settings(max_repeated_child_signature=2)
    # 前两个同签名子任务都还不到上限，第三个才触发
    assert convergence.repeated_child_signatures(parent, [_spec("甲"), _spec("甲")], settings) == []
    repeated = convergence.repeated_child_signatures(parent, [_spec("甲"), _spec("甲"), _spec("甲")], settings)
    assert len(repeated) == 1
    assert repeated[0]["existing_or_planned_count"] == 2
    assert repeated[0]["limit"] == 2


def test_repeated_child_signatures_counts_existing_children() -> None:
    spec = _spec("甲")
    # 既有子节点只有带 `split_signature` 元数据时才会与「计划中的子任务签名」匹配；
    # 否则 task_signature（含 description/scope/acceptance）与 child_signature 天然不同。
    existing = _task("c1", title="甲", meta={"split_signature": convergence.child_signature(spec)})
    parent = _task()
    parent.children = [existing]
    repeated = convergence.repeated_child_signatures(parent, [spec], _Settings(max_repeated_child_signature=1))
    assert len(repeated) == 1
    assert repeated[0]["existing_or_planned_count"] == 1

    # 不带 split_signature 时不会匹配 → 不触发
    plain_parent = _task()
    plain_parent.children = [_task("c2", title="甲")]
    assert convergence.repeated_child_signatures(plain_parent, [spec], _Settings(max_repeated_child_signature=1)) == []


def test_orchestrator_delegates_agree_with_module() -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings()

    parent = _task("战斗")
    parent.children = []
    spec = _spec("甲")

    assert orchestrator._fallback_event_message("tree.hibernate", {}) == convergence.fallback_event_message("tree.hibernate", {})
    assert orchestrator._is_blocked_child_repair_spec(spec) == convergence.is_blocked_child_repair_spec(spec)
    assert orchestrator._convergence_slice_count("收敛切片") == convergence.convergence_slice_count("收敛切片")
    assert orchestrator._convergence_cycle_breaker_base_title("战斗") == convergence.convergence_cycle_breaker_base_title("战斗")
    assert orchestrator._normalize_signature_text(" Ab ") == convergence.normalize_signature_text(" Ab ")
    assert orchestrator._child_signature(spec) == convergence.child_signature(spec)
    assert orchestrator._task_signature(parent) == convergence.task_signature(parent)
    assert orchestrator._is_convergence_cycle_breaker_task(parent) == convergence.is_convergence_cycle_breaker_task(parent)
    assert orchestrator._is_legacy_convergence_slice_queued_child(parent) == (
        convergence.is_legacy_convergence_slice_queued_child(parent)
    )
    assert orchestrator._is_redundant_convergence_cycle_breaker_child(parent, parent) == (
        convergence.is_redundant_convergence_cycle_breaker_child(parent, parent)
    )
    assert orchestrator._cycle_breaker_children(parent, []) == convergence.cycle_breaker_children(parent, [])
    assert orchestrator._convergence_cycle_breaker_children(parent, {}) == (
        convergence.convergence_cycle_breaker_children(parent, {})
    )
    assert orchestrator._convergence_slice_loop(parent, []) == convergence.convergence_slice_loop(
        parent, [], orchestrator.settings
    )
    assert orchestrator._repeated_child_signatures(parent, [spec]) == convergence.repeated_child_signatures(
        parent, [spec], orchestrator.settings
    )
    assert orchestrator._drop_blocked_repair_dependencies(spec, ["甲"], ["id-1"], {}, {}) == (
        convergence.drop_blocked_repair_dependencies(spec, ["甲"], ["id-1"], {}, {})
    )
