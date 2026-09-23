"""阻塞分类与阻塞报告构造。

搬移自 `orchestrator.Orchestrator`。判据是 `scripts/audit_orchestrator_movable_mass.py`
的闭包实测，而不是方法组的表面行数：

- **可搬**：`_build_block_report` / `_normalize_block_report` / `_block_category` /
  `_dependency_report_context` / `_child_block_report` 等 16 个方法的实测闭包 =
  **16 个方法 / 495 行**，不触达任何不可搬方法。这是 W4 剩余最大的一块。
- 分类词表一并搬入（`RETRYABLE_BLOCK_CATEGORIES` / `RESOURCE_PRESSURE_BLOCK_CATEGORIES` /
  `FAST_RETRYABLE_BLOCK_CATEGORIES` / `WORKSPACE_WRITE_BLOCK_SOURCES` / `PROTOCOL_FAILURE_SOURCES`），
  它们只在阻塞分类里被消费；orchestrator 反向 import 这几个名字，因此 `:2752` / `:2756` 的
  两处判断零改动。
- **前提条件已核实**：`_can_hibernate_task` 自己只有 4 行，但它调 `_child_block_report`。
  在 W4f 实测到这条边会把本模块整簇拖进闭包（115 行 → 610 行），**这正是本步先做的原因**；
  本步落地后，W4f 未搬的那部分即可解锁。
- **没有搬的兄弟方法**：`_skill_lookup_recovery_text` / `_is_human_required_blocker` /
  `_visual_uncertain_payload` 实测**不在**该闭包内，故留在 orchestrator，不做顺手搬移。

依赖方向：本模块只依赖 `models` / `utils` / `codex_signals` / `runtime_markers`，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .codex_signals import TRANSIENT_CODEX_FAILURE_MARKERS, is_local_skill_failure
from .models import TERMINAL_STATUSES, TaskNode, TaskStatus, utcnow
from .runtime_markers import (
    FALSE_READONLY_SANDBOX_MARKERS,
    FALSE_TOOL_CAPABILITY_BLOCK_MARKERS,
    MODEL_ACTION_PLAN_MARKERS,
    MODEL_EXECUTION_PROMISE_MARKERS,
    PROTOCOL_TRANSIENT_MARKERS,
    TRANSPORT_TRANSIENT_MARKERS,
)
from .utils import loads, repair_mojibake, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


RETRYABLE_BLOCK_CATEGORIES = {
    "capacity",
    "timeout",
    "rate_limit",
    "transient",
    "transport",
    "protocol",
    "local_skill_metadata",
    "workspace_sandbox",
}
RESOURCE_PRESSURE_BLOCK_CATEGORIES = {"capacity", "timeout", "rate_limit", "transient"}
FAST_RETRYABLE_BLOCK_CATEGORIES = {"transport", "protocol", "local_skill_metadata", "workspace_sandbox"}
WORKSPACE_WRITE_BLOCK_SOURCES = {
    "worker",
    "worker.blocked",
    "worker.exception",
    "worker.failed",
    "verifier",
    "verifier.blocked",
    "verifier.exception",
    "verifier.failed",
    "acceptance",
    "acceptance.blocked",
    "acceptance.exception",
    "acceptance.failed",
}
PROTOCOL_FAILURE_SOURCES = {
    "acceptance.blocked",
    "acceptance.failed",
    "planner.blocked",
    "planner.failed",
    "verifier.blocked",
    "verifier.failed",
    "worker.blocked",
    "worker.failed",
}


def can_hibernate_task(task: TaskNode, settings: Settings) -> bool:
    if task.status == TaskStatus.BLOCKED.value:
        return bool(child_block_report(task, settings).get("retryable"))
    return task.status not in TERMINAL_STATUSES


def build_block_report(
    task: TaskNode,
    reason: str,
    source: str,
    payload: dict[str, Any] | None,
    settings: Settings,
) -> dict[str, Any]:
    payload_report = payload.get("block_report") if isinstance(payload, dict) else None
    payload_reason = str(payload_report.get("reason") or "") if isinstance(payload_report, dict) else ""
    if isinstance(payload_report, dict):
        category = str(payload_report.get("category") or "external")
        retryable = bool(payload_report.get("retryable"))
        parent_action = str(payload_report.get("parent_action") or ("wait_and_retry" if retryable else "manual_review"))
        transient_category = transient_block_category(str(payload_report.get("reason") or reason))
        if transient_category and (not retryable or category == "tool_internal" or parent_action == "pause_tool_fix"):
            category = transient_category
            retryable = True
            parent_action = "wait_and_retry"
    elif source == "dependency.blocked" and (dependency_context := dependency_block_context(payload)):
        category = dependency_context["category"]
        retryable = dependency_context["retryable"]
        parent_action = dependency_context["parent_action"]
    else:
        category = block_category(reason)
        retryable = category in RETRYABLE_BLOCK_CATEGORIES
        if retryable:
            parent_action = "wait_and_retry"
        elif category == "human_auth":
            parent_action = "human_auth_required"
        elif category == "visual_uncertain":
            parent_action = "human_visual_review"
        elif category == "tool_internal":
            parent_action = "pause_tool_fix"
        elif category == "validation_environment":
            parent_action = "recursive_recovery_static_wrapper"
        elif settings.skill_lookup_enabled and settings.skill_km_url.strip():
            parent_action = "skill_lookup_recovery"
        else:
            parent_action = "manual_review"
    false_readonly_category = false_readonly_sandbox_category(f"{reason}\n{payload_reason}", source)
    if false_readonly_category:
        category = false_readonly_category
        retryable = True
        parent_action = "wait_and_retry"
    false_tool_protocol_category = false_tool_capability_protocol_category(f"{reason}\n{payload_reason}", source)
    if false_tool_protocol_category:
        category = false_tool_protocol_category
        retryable = True
        parent_action = "wait_and_retry"
    report = {
        "task_id": task.id,
        "title": task.title,
        "status": task.status,
        "source": source,
        "category": category,
        "retryable": retryable,
        "parent_action": parent_action,
        "reason": reason,
        "reported_at": utcnow().isoformat(),
    }
    if isinstance(payload_report, dict):
        for key in ("retry_after_seconds", "retry_at", "quota_window_seconds", "retry_after_source", "attempts"):
            if key in payload_report:
                report[key] = payload_report[key]
    skill_lookup = skill_lookup_report_context(category, parent_action, settings)
    if skill_lookup:
        report["skill_lookup"] = skill_lookup
    if category in {"human_auth", "visual_uncertain"} or parent_action in {"human_auth_required", "human_visual_review"}:
        report["human_action_required"] = True
    if isinstance(payload, dict):
        dependencies = payload.get("dependencies")
        if isinstance(dependencies, list):
            report["dependencies"] = [str(dependency) for dependency in dependencies]
        dependency_reports = payload.get("dependency_reports")
        if isinstance(dependency_reports, list):
            report["dependency_reports"] = repair_mojibake(dependency_reports)
    return normalize_block_report(report)


BLOCKED_RECOVERY_SCOPE_PREFIXES = ("planner-blocked:", "acceptance-blocked:")


def blocked_recovery_chain(scope: list[str] | tuple[str, ...] | None) -> list[str]:
    """该节点是不是"阻塞恢复"链上的一环（scope 里带恢复子任务标记）。

    为什么看 scope 而不是标题：scope 是**建恢复子任务时写进去的结构化标记**，会沿委托链继承；
    而标题既能被模型改写，也会叠成一长串"…：规划阻塞恢复：规划阻塞恢复：…"（2026-09-22 实测如此）。
    """
    return [str(item) for item in (scope or []) if str(item).startswith(BLOCKED_RECOVERY_SCOPE_PREFIXES)]


def blocked_recovery_suppression(report: dict[str, Any], *, scope: list[str] | tuple[str, ...] | None, has_description: bool) -> str:
    """blocked 时**不给**递归恢复子任务的理由；返回空字符串 = 可以转换。

    失效方向写明（这条判据坏掉会怎样）：
    - **工具内部问题先修工具**：`pause_tool_fix` 拆子任务解决不了。
    - **没有 description**：构造不出可执行的恢复目标，拆出来只是空转（原行为）。
    - **已经拆过一次就别再拆**：环境性阻塞递归解决不了。2026-09-22 实测：模型报告
      "只读命令在进程创建时被执行策略以 `blocked by policy` 拒绝"，系统每层都转成"规划阻塞恢复"
      子任务，3-5 分钟就到深度 4、并发油门 1→3，最后把整批跑批拖到冻住。**停在上报（blocked）
      比无限拆更接近事实**：树会说"我阻塞了"，而不是假装在推进。
    - **能等就别拆**：可等待重试的资源类（额度/传输/沙箱误判）该由父层等待重试，拆分只会白烧预算。

    反面自检：任务内部的、非重试的阻塞**必须仍然能**转成恢复子任务
    （`api/tests/test_blockers.py::test_blocked_recovery_suppression_allows_only_task_internal_blocks`
    与 `api/tests/test_orchestrator.py::test_environmental_planner_blocked_stops_after_one_recovery_level`
    一正一反两例）。
    """
    if report.get("category") == "environment" or report.get("parent_action") == "human_review":
        return "阻塞来源是**执行环境**（策略/沙箱/权限/工具不可用），递归拆子任务撞的是同一堵墙，应当停下来报给人"
    if report.get("category") == "tool_internal" or report.get("parent_action") == "pause_tool_fix":
        return "工具内部问题需要先修工具（pause_tool_fix），拆子任务解决不了"
    if not has_description:
        return "节点没有 description，构造不出可执行的恢复任务"
    chain = blocked_recovery_chain(scope)
    if chain:
        return f"该节点已在阻塞恢复链上（{chain[0]}），同类阻塞不再递归第二次"
    if report.get("retryable"):
        return (
            f"阻塞分类 {report.get('category') or 'unknown'} 属可等待重试"
            f"（parent_action={report.get('parent_action') or 'wait_and_retry'}），应当等待而不是拆分"
        )
    return ""


def dependency_reports_are_retryable(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    dependency_reports = payload.get("dependency_reports")
    if not isinstance(dependency_reports, list) or not dependency_reports:
        return False
    for report in dependency_reports:
        if not isinstance(report, dict):
            return False
        report = normalize_block_report(report)
        if bool(report.get("retryable")):
            continue
        reason = str(report.get("reason") or "")
        category = str(report.get("category") or "")
        parent_action = str(report.get("parent_action") or "")
        if parent_action == "wait_and_retry" or category in RETRYABLE_BLOCK_CATEGORIES:
            continue
        if block_category(reason) in RETRYABLE_BLOCK_CATEGORIES:
            continue
        return False
    return True


def dependency_block_context(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if dependency_reports_are_retryable(payload):
        return {"category": "rate_limit", "retryable": True, "parent_action": "wait_and_retry"}
    dependency_reports = payload.get("dependency_reports")
    if not isinstance(dependency_reports, list) or not dependency_reports:
        return None
    contexts: list[dict[str, Any]] = []
    for report in dependency_reports:
        if not isinstance(report, dict):
            continue
        context = dependency_report_context(report)
        if context is not None:
            contexts.append(context)
    if not contexts:
        return None
    priority = {
        "tool_internal": 0,
        "validation_environment": 1,
        "human_auth": 2,
        "visual_uncertain": 3,
        "external": 4,
        "manual_review": 5,
    }
    return min(contexts, key=lambda item: priority.get(str(item.get("category") or ""), 99))


def dependency_report_context(report: dict[str, Any]) -> dict[str, Any] | None:
    report = normalize_block_report(report)
    if bool(report.get("retryable")):
        category = str(report.get("category") or "rate_limit")
        if category not in RETRYABLE_BLOCK_CATEGORIES:
            category = "rate_limit"
        return {"category": category, "retryable": True, "parent_action": "wait_and_retry"}
    category = str(report.get("category") or "").strip()
    parent_action = str(report.get("parent_action") or "").strip()
    if not category:
        category = {
            "wait_and_retry": "rate_limit",
            "pause_tool_fix": "tool_internal",
            "recursive_recovery_static_wrapper": "validation_environment",
            "human_auth_required": "human_auth",
            "human_visual_review": "visual_uncertain",
            "manual_review": "external",
        }.get(parent_action, "")
    if not category:
        nested = report.get("dependency_reports")
        if isinstance(nested, list):
            nested_contexts = [dependency_report_context(item) for item in nested if isinstance(item, dict)]
            nested_contexts = [item for item in nested_contexts if item is not None]
            if nested_contexts:
                priority = {
                    "tool_internal": 0,
                    "validation_environment": 1,
                    "human_auth": 2,
                    "visual_uncertain": 3,
                    "external": 4,
                    "manual_review": 5,
                }
                return min(nested_contexts, key=lambda item: priority.get(str(item.get("category") or ""), 99))
    if not category:
        category = block_category(str(report.get("reason") or ""))
    if not parent_action:
        parent_action = {
            "capacity": "wait_and_retry",
            "timeout": "wait_and_retry",
            "rate_limit": "wait_and_retry",
            "transient": "wait_and_retry",
            "transport": "wait_and_retry",
            "protocol": "wait_and_retry",
            "tool_internal": "pause_tool_fix",
            "validation_environment": "recursive_recovery_static_wrapper",
            "human_auth": "human_auth_required",
            "visual_uncertain": "human_visual_review",
        }.get(category, "manual_review")
    retryable = category in RETRYABLE_BLOCK_CATEGORIES or parent_action == "wait_and_retry"
    return {"category": category, "retryable": retryable, "parent_action": parent_action}


def normalize_block_report(report: dict[str, Any]) -> dict[str, Any]:
    reason = str(report.get("reason") or "")
    source = str(report.get("source") or "")
    if is_local_skill_failure(
        " ".join(
            repair_mojibake_text(str(report.get(key) or ""))
            for key in ("reason", "summary", "category", "source", "parent_action", "title")
        )
    ):
        category = str(report.get("category") or "")
        parent_action = str(report.get("parent_action") or "")
        if bool(report.get("retryable")) and category == "local_skill_metadata" and parent_action == "wait_and_retry":
            return report
        normalized = dict(report)
        normalized.setdefault(
            "reclassified_from",
            {"category": category, "parent_action": parent_action, "retryable": bool(report.get("retryable"))},
        )
        normalized["category"] = "local_skill_metadata"
        normalized["retryable"] = True
        normalized["parent_action"] = "wait_and_retry"
        normalized["recovery"] = "retry_with_battlefield_child_codex_user_config_isolation"
        normalized.pop("skill_lookup", None)
        normalized.pop("human_action_required", None)
        return normalized
    false_readonly_category = false_readonly_sandbox_category(reason, source)
    if false_readonly_category:
        category = str(report.get("category") or "")
        parent_action = str(report.get("parent_action") or "")
        if bool(report.get("retryable")) and category == false_readonly_category and parent_action == "wait_and_retry":
            return report
        normalized = dict(report)
        normalized.setdefault(
            "reclassified_from",
            {"category": category, "parent_action": parent_action, "retryable": bool(report.get("retryable"))},
        )
        normalized["category"] = false_readonly_category
        normalized["retryable"] = True
        normalized["parent_action"] = "wait_and_retry"
        normalized["recovery"] = "retry_after_workspace_write_codex_config_fix"
        normalized.pop("skill_lookup", None)
        normalized.pop("human_action_required", None)
        return normalized
    false_tool_protocol_category = false_tool_capability_protocol_category(reason, source)
    if false_tool_protocol_category:
        category = str(report.get("category") or "")
        parent_action = str(report.get("parent_action") or "")
        if bool(report.get("retryable")) and category == false_tool_protocol_category and parent_action == "wait_and_retry":
            return report
        normalized = dict(report)
        normalized.setdefault(
            "reclassified_from",
            {"category": category, "parent_action": parent_action, "retryable": bool(report.get("retryable"))},
        )
        normalized["category"] = false_tool_protocol_category
        normalized["retryable"] = True
        normalized["parent_action"] = "wait_and_retry"
        normalized["recovery"] = "retry_after_child_codex_tool_capability_false_block"
        normalized.pop("skill_lookup", None)
        normalized.pop("human_action_required", None)
        return normalized
    reclassified_category = protocol_block_category(reason, source) or transient_block_category(reason)
    if not reclassified_category:
        return report
    category = str(report.get("category") or "")
    parent_action = str(report.get("parent_action") or "")
    if bool(report.get("retryable")) and category == reclassified_category and parent_action == "wait_and_retry":
        return report
    normalized = dict(report)
    normalized.setdefault(
        "reclassified_from",
        {"category": category, "parent_action": parent_action, "retryable": bool(report.get("retryable"))},
    )
    normalized["category"] = reclassified_category
    normalized["retryable"] = True
    normalized["parent_action"] = "wait_and_retry"
    if reclassified_category in FAST_RETRYABLE_BLOCK_CATEGORIES:
        normalized.pop("skill_lookup", None)
        normalized.pop("human_action_required", None)
    return normalized


def false_readonly_sandbox_category(reason: str, source: str) -> str:
    normalized_source = repair_mojibake_text(str(source or "")).lower().strip()
    if normalized_source not in WORKSPACE_WRITE_BLOCK_SOURCES:
        return ""
    normalized = repair_mojibake_text(str(reason or "")).lower()
    if not any(marker in normalized for marker in ("read-only", "read only", "只读", "restricted read")):
        return ""
    if any(marker in normalized for marker in FALSE_READONLY_SANDBOX_MARKERS):
        return "workspace_sandbox"
    return ""


def false_tool_capability_protocol_category(reason: str, source: str) -> str:
    normalized_source = repair_mojibake_text(str(source or "")).lower().strip()
    if normalized_source not in WORKSPACE_WRITE_BLOCK_SOURCES:
        return ""
    normalized = repair_mojibake_text(str(reason or "")).lower()
    if any(marker in normalized for marker in FALSE_TOOL_CAPABILITY_BLOCK_MARKERS):
        return "protocol"
    return ""


def block_category(reason: str) -> str:
    normalized = reason.lower()
    if is_local_skill_failure(reason):
        return "local_skill_metadata"
    protocol_category = protocol_block_category(reason, "")
    if protocol_category:
        return protocol_category
    if explicit_retryable_resource_reason(normalized):
        return "rate_limit"
    transient_category = transient_block_category(reason)
    if transient_category in {"transport", "rate_limit", "timeout"}:
        return transient_category
    if any(
        marker in normalized
        for marker in (
            "需要登录",
            "请登录",
            "登录后",
            "人工登录",
            "人工授权",
            "权限不足",
            "无权限",
            "未授权",
            "验证码",
            "captcha",
            "sso",
            "sign in",
            "log in",
            "login required",
            "authentication required",
            "authorization required",
            "unauthorized",
            "forbidden",
        )
    ) or re.search(r"(?<![\w-])(?:401|403)(?![\w-])", normalized):
        return "human_auth"
    if any(
        marker in normalized
        for marker in (
            "visual_uncertain",
            "视觉不确定",
            "视觉无法判断",
            "无法可靠判断画面",
            "无法可靠判断动作",
            "无法可靠判断特效",
            "无法可靠判断打击感",
            "人工视觉",
            "人工审美",
            "human visual review",
        )
    ):
        return "visual_uncertain"
    if any(
        marker in normalized
        for marker in (
            "unexpected argument",
            "usage: codex exec",
            "unknown option",
            "unrecognized option",
            "failed to load skill",
            "invalid description",
            "exceeds maximum length",
            "工具内部错误",
        )
    ):
        return "tool_internal"
    if any(
        marker in normalized
        for marker in (
            "crashhandlerexception",
            "program crashed with signal",
            "could not create directory: 'user://logs'",
            "error: access denied",
            "godot_timeout",
            "memory could not be read",
            "内存不能 read",
            "该内存不能为 read",
        )
    ):
        return "validation_environment"
    if any(marker in normalized for marker in TRANSIENT_CODEX_FAILURE_MARKERS):
        if transient_category:
            return transient_category
        if (
            "rate limit" in normalized
            or "429" in normalized
            or "quota" in normalized
            or "usage limit" in normalized
            or "retry after" in normalized
            or "too many requests" in normalized
            or "限流" in normalized
            or "额度" in normalized
            or "余额" in normalized
            or "流量" in normalized
            or "资源/流量" in normalized
        ):
            return "rate_limit"
        if "timeout" in normalized or "timed out" in normalized or "超时" in normalized:
            return "timeout"
        return "capacity"
    if any(
        marker in normalized
        for marker in (
            "godot_resource_queue",
            "godot_resource_queue_timeout",
            "资源/流量类阻塞",
            "资源/流量",
            "可等待后重试",
            "限流",
            "额度",
            "余额",
            "流量",
            "retry after",
            "usage limit",
            "too many requests",
        )
    ):
        return "rate_limit"
    return "external"


def protocol_block_category(reason: str, source: str) -> str:
    normalized = repair_mojibake_text(str(reason or "")).lower()
    if not normalized:
        return ""
    if any(marker in normalized for marker in PROTOCOL_TRANSIENT_MARKERS):
        return "protocol"
    if source not in PROTOCOL_FAILURE_SOURCES:
        return ""
    if obvious_non_protocol_block_text(normalized):
        return ""
    has_action_plan = any(marker in normalized for marker in MODEL_ACTION_PLAN_MARKERS)
    has_execution_promise = any(marker in normalized for marker in MODEL_EXECUTION_PROMISE_MARKERS)
    if has_action_plan and has_execution_promise:
        return "protocol"
    return ""


def obvious_non_protocol_block_text(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "401",
            "403",
            "429",
            "access denied",
            "authentication required",
            "authorization required",
            "captcha",
            "crashhandlerexception",
            "forbidden",
            "godot_timeout",
            "human visual review",
            "login required",
            "memory could not be read",
            "rate limit",
            "retry after",
            "sso",
            "too many requests",
            "unauthorized",
            "usage limit",
            "visual_uncertain",
            "限流",
            "额度",
            "登录",
            "权限",
            "授权",
            "验证码",
            "视觉不确定",
            "人工视觉",
        )
    )


def transient_block_category(reason: str) -> str:
    normalized = reason.lower()
    if not any(marker in normalized for marker in TRANSIENT_CODEX_FAILURE_MARKERS):
        return ""
    if (
        "rate limit" in normalized
        or "429" in normalized
        or "quota" in normalized
        or "usage limit" in normalized
        or "retry after" in normalized
        or "too many requests" in normalized
        or "闄愭祦" in normalized
        or "棰濆害" in normalized
        or "浣欓" in normalized
        or "娴侀噺" in normalized
        or "璧勬簮/娴侀噺" in normalized
    ):
        return "rate_limit"
    if any(marker in normalized for marker in TRANSPORT_TRANSIENT_MARKERS):
        return "transport"
    if "timeout" in normalized or "timed out" in normalized or "瓒呮椂" in normalized:
        return "timeout"
    return "capacity"


def explicit_retryable_resource_reason(normalized_reason: str) -> bool:
    return any(
        marker in normalized_reason
        for marker in (
            "资源/流量",
            "资源类阻塞",
            "流量类阻塞",
            "可等待后重试",
            "可重试的 codex 资源",
            "retryable resource",
            "wait_and_retry",
            "璧勬簮/娴侀噺",
            "鍙瓑寰呭悗閲嶈瘯",
        )
    )


def skill_lookup_report_context(category: str, parent_action: str, settings: Settings) -> dict[str, Any] | None:
    url = settings.skill_km_url.strip()
    if not settings.skill_lookup_enabled or not url:
        return None
    return {
        "enabled": True,
        "url": url,
        "recommended": category not in {"capacity", "rate_limit", "timeout", "tool_internal"} and parent_action != "wait_and_retry",
        "human_auth_boundary": "如果 Skill/KM 需要登录、SSO、权限或验证码，则上报 human_auth，父层记录人工卡点并继续其他可运行分支。",
    }


def child_block_report(child: TaskNode, settings: Settings) -> dict[str, Any]:
    meta = loads(child.node_meta_json, {})
    report = meta.get("block_report")
    if isinstance(report, dict):
        return normalize_block_report(report)
    reason = child.error_message or f"子节点状态为 {child.status}。"
    return build_block_report(child, reason, "child.status", None, settings)
