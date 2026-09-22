"""收敛破环：重复拆分签名、收敛切片循环与 cycle-breaker 子任务构造。

搬移自 `orchestrator.Orchestrator`。属 W4g-e 实测簇（**70 方法 / 1239 行、70/70 零实例状态**）
里职责独立的一支；本步只取其中的收敛/破环与签名部分。

**比原闭包多收了两个方法（按职责而非按闭包）**：`_is_redundant_convergence_cycle_breaker_child`(8)
与 `_is_legacy_convergence_slice_queued_child`(9) 都只依赖本模块内的函数，是同一职责的成员，
但它们不在以 final-gate 为入口测出的闭包里。闭包回答的是"从某入口出发可达什么"，
不是"这个职责包含什么"——两者必须分开判断（PLAN-STATUS §5 已记录该陷阱）。

`_settle_legacy_convergence_slice_children` 虽然名字相近，但它要写节点状态与事件，
属 orchestrator 的消费者，**留在原处**。

依赖方向：本模块只依赖 `models` / `schemas` / `utils` / `skill_notes` 与标准库，
**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import TYPE_CHECKING, Any

from .models import TaskNode, TaskStatus
from .schemas import ChildTaskSpec
from .skill_notes import is_skill_note_spec
from .utils import dumps, loads, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


CONVERGENCE_SLICE_REPEAT_LIMIT = 3


def fallback_event_message(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "tree.reconstructed":
        target_path = str(payload.get("target_path") or "未知目标目录")
        return f"任务树已从本地状态快照重建；目标目录：{target_path}。"
    if event_type == "tree.snapshot_restored":
        restored_root_id = str(payload.get("restored_root_id") or payload.get("snapshot_root_id") or "未知根节点")
        return f"任务树已从本地状态快照恢复；根节点：{restored_root_id}。"
    if event_type == "tree.hibernate":
        return "任务树已保存状态并进入休眠，等待后续自动恢复。"
    if event_type == "budget.guard_hibernate":
        used = payload.get("used")
        work_limit = payload.get("work_limit")
        reserve = payload.get("reserve")
        if used is not None and work_limit is not None:
            return f"达到本轮 Codex agent 工作启动额度 {used}/{work_limit}，已保存状态并休眠；保留 {reserve or 0} 次预算给自动恢复。"
        return "达到本轮 Codex agent 工作启动额度，已保存状态并休眠。"
    return f"{event_type} 事件的原始消息不可读，已保留结构化 payload 供排查。"


def is_blocked_child_repair_spec(child: ChildTaskSpec) -> bool:
    coverage_ids = {str(item).strip().lower() for item in child.coverage_ids}
    if "blocked-child-repair" in coverage_ids:
        return True
    marker_text = f"{child.title} {child.verification_kind}".lower()
    return "blocked child repair" in marker_text or "blocked-child-repair" in marker_text


def drop_blocked_repair_dependencies(
    child: ChildTaskSpec,
    depends_on: list[str],
    depends_on_ids: list[str],
    dependency_id_by_raw: dict[str, str],
    existing_siblings_by_id: dict[str, TaskNode],
) -> tuple[list[str], list[str], list[str]]:
    if not is_blocked_child_repair_spec(child):
        return depends_on, depends_on_ids, []
    dropped = [
        dependency_id
        for dependency_id in depends_on_ids
        if (dependency := existing_siblings_by_id.get(dependency_id)) is not None
        and dependency.status in {TaskStatus.BLOCKED.value, TaskStatus.FAILED.value, TaskStatus.CANCELED.value}
    ]
    if not dropped:
        return depends_on, depends_on_ids, []
    dropped_set = set(dropped)
    filtered_depends_on = [
        dependency
        for dependency in depends_on
        if dependency_id_by_raw.get(str(dependency)) not in dropped_set
    ]
    filtered_depends_on_ids = [dependency_id for dependency_id in depends_on_ids if dependency_id not in dropped_set]
    return filtered_depends_on, filtered_depends_on_ids, dropped


def cycle_breaker_children(parent: TaskNode, repeated: list[dict[str, Any]]) -> list[ChildTaskSpec]:
    scope = loads(parent.scope_json, [])
    acceptance = loads(parent.acceptance_json, [])
    repeated_lines = [
        f"- signature={item.get('signature')} title={repair_mojibake_text(str(item.get('title') or ''))} "
        f"count={item.get('existing_or_planned_count')}/{item.get('limit')}"
        for item in repeated
    ]
    return [
        ChildTaskSpec(
            title=f"{parent.title}：重复拆分收敛破环",
            description=(
                "父节点检测到新的子任务拆分与已有拆分重复，说明当前递归路线可能在原地打转。"
                "请不要继续生成同构子任务；改为审计已有子成果、找出缺口，"
                "产出一个能让父节点继续前进的整合/替代交付或更明确的验收证据。"
                f"\n\n重复签名：\n{chr(10).join(repeated_lines)}"
            ),
            scope=[*[str(item) for item in scope], "cycle-breaker: repeated child signatures"],
            acceptance=[
                "必须解释为什么此前拆分重复，以及本次如何打破循环。",
                "必须复用或整合已有子成果，不能再次提出同构子任务。",
                "必须产出可被父节点验收的 changed_files、验证证据或明确缺口清单。",
                "如果必须继续拆分，新的边界必须和重复签名所代表的旧边界明显不同。",
                *[str(item) for item in acceptance[:3]],
            ],
            coverage_ids=["cycle-breaker", *[str(item.get("signature") or "") for item in repeated]],
            verification_kind="cycle breaker + integration evidence",
            estimated_leaf=False,
        )
    ]


def convergence_slice_count(title: Any) -> int:
    text = repair_mojibake_text(str(title or ""))
    return max(text.count("收敛切片"), text.lower().count("convergence slice"))


def convergence_slice_loop(parent: TaskNode, children: list[ChildTaskSpec], settings: Settings) -> dict[str, Any] | None:
    non_skill_children = [child for child in children if not is_skill_note_spec(child, settings)]
    child_counts = [convergence_slice_count(child.title) for child in non_skill_children]
    parent_count = convergence_slice_count(parent.title)
    max_child_count = max(child_counts, default=0)
    if is_convergence_cycle_breaker_task(parent) and max_child_count < CONVERGENCE_SLICE_REPEAT_LIMIT:
        return None
    max_count = max([parent_count, *child_counts], default=0)
    if max_count < CONVERGENCE_SLICE_REPEAT_LIMIT:
        return None
    return {
        "parent_title": repair_mojibake_text(parent.title),
        "parent_count": parent_count,
        "max_child_count": max_child_count,
        "limit": CONVERGENCE_SLICE_REPEAT_LIMIT,
        "child_titles": [repair_mojibake_text(child.title) for child in non_skill_children],
    }


def is_convergence_cycle_breaker_task(task: TaskNode) -> bool:
    meta = loads(task.node_meta_json, {})
    if not isinstance(meta, dict):
        return False
    coverage_ids = {str(item) for item in meta.get("coverage_ids", [])}
    verification_kind = str(meta.get("verification_kind") or "")
    return {"cycle-breaker", "convergence-slice-loop"}.issubset(coverage_ids) or (
        verification_kind == "convergence loop breaker + parent closure evidence"
    )


def is_redundant_convergence_cycle_breaker_child(parent: TaskNode, child: TaskNode) -> bool:
    if child.status not in {TaskStatus.PLANNED.value, TaskStatus.QUEUED.value}:
        return False
    if not is_convergence_cycle_breaker_task(child):
        return False
    parent_base = convergence_cycle_breaker_base_title(parent.title)
    child_base = convergence_cycle_breaker_base_title(child.title)
    return child_base == parent_base or convergence_slice_count(child.title) >= CONVERGENCE_SLICE_REPEAT_LIMIT


def convergence_cycle_breaker_base_title(title: Any) -> str:
    text = repair_mojibake_text(str(title or "")).strip()
    suffixes = ("：递归循环诊断与收口", ": 递归循环诊断与收口", ":递归循环诊断与收口")
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text or "递归循环诊断"


def is_legacy_convergence_slice_queued_child(task: TaskNode) -> bool:
    if task.status not in {TaskStatus.PLANNED.value, TaskStatus.QUEUED.value}:
        return False
    if convergence_slice_count(task.title) < CONVERGENCE_SLICE_REPEAT_LIMIT:
        return False
    meta = loads(task.node_meta_json, {})
    coverage_ids = {str(item) for item in meta.get("coverage_ids", [])} if isinstance(meta, dict) else set()
    if {"cycle-breaker", "convergence-slice-loop"}.issubset(coverage_ids):
        return False
    return True


def convergence_cycle_breaker_children(parent: TaskNode, loop: dict[str, Any]) -> list[ChildTaskSpec]:
    scope = loads(parent.scope_json, [])
    acceptance = loads(parent.acceptance_json, [])
    base_title = convergence_cycle_breaker_base_title(parent.title)
    return [
        ChildTaskSpec(
            title=f"{base_title}：递归循环诊断与收口",
            description=(
                "当前分支正在反复追加实现/验证收敛切片，说明普通 fallback decomposition 没有继续缩小问题。"
                "请停止继续追加同类收敛切片，直接诊断父任务为何不能完成：缺少权威输入、证据路径错误、验收标准矛盾、"
                "还是已有成果没有被父层整合。交付可被父节点验收的修复、证据报告或明确的父层收口建议。"
                f"\n\n循环证据：{json.dumps(loop, ensure_ascii=False)}"
            ),
            scope=[*[str(item) for item in scope], "cycle-breaker: convergence slice loop"],
            acceptance=[
                "不得再创建标题继续追加“实现收敛切片”或“验证收敛切片”的子任务。",
                "必须定位导致反复 fallback 的真实原因，并说明它发生在输入、实现、验证、整合还是验收边界。",
                "必须交付可检查的 changed_files、证据报告或父层可执行收口建议。",
                "如果已有成果足以满足父任务，必须给出父层 acceptance 可以复核的具体文件路径和命令。",
                *[str(item) for item in acceptance[:4]],
            ],
            coverage_ids=["cycle-breaker", "convergence-slice-loop"],
            verification_kind="convergence loop breaker + parent closure evidence",
            estimated_leaf=False,
        )
    ]


def normalize_signature_text(value: Any) -> str:
    return " ".join(str(value).lower().split())


def child_signature(child: ChildTaskSpec) -> str:
    raw = {
        "title": normalize_signature_text(child.title),
        "description": normalize_signature_text(child.description),
        "scope": [normalize_signature_text(item) for item in child.scope],
        "acceptance": [normalize_signature_text(item) for item in child.acceptance],
        "coverage_ids": [normalize_signature_text(item) for item in child.coverage_ids],
    }
    return hashlib.sha256(dumps(raw).encode("utf-8")).hexdigest()[:16]


def task_signature(task: TaskNode) -> str:
    raw = {
        "title": normalize_signature_text(task.title),
        "description": normalize_signature_text(task.description),
        "scope": [normalize_signature_text(item) for item in loads(task.scope_json, [])],
        "acceptance": [normalize_signature_text(item) for item in loads(task.acceptance_json, [])],
        "coverage_ids": [normalize_signature_text(item) for item in loads(task.node_meta_json, {}).get("coverage_ids", [])],
    }
    return hashlib.sha256(dumps(raw).encode("utf-8")).hexdigest()[:16]


def repeated_child_signatures(
    parent: TaskNode,
    children: list[ChildTaskSpec],
    settings: Settings,
) -> list[dict[str, Any]]:
    if settings.max_repeated_child_signature <= 0:
        return []
    existing_counts: Counter[str] = Counter()
    for child in parent.children:
        signature = loads(child.node_meta_json, {}).get("split_signature") or task_signature(child)
        existing_counts[signature] += 1
    planned_counts: Counter[str] = Counter()
    repeated: list[dict[str, Any]] = []
    for child in children:
        signature = child_signature(child)
        current_count = existing_counts[signature] + planned_counts[signature]
        if current_count >= settings.max_repeated_child_signature:
            repeated.append(
                {
                    "signature": signature,
                    "title": child.title,
                    "existing_or_planned_count": current_count,
                    "limit": settings.max_repeated_child_signature,
                }
            )
        planned_counts[signature] += 1
    return repeated
