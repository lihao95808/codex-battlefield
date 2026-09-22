from __future__ import annotations

import json

from app import blockers
from app.models import TaskNode
from app.orchestrator import Orchestrator


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.skill_lookup_enabled = False
        self.skill_km_url = ""
        for key, value in overrides.items():
            setattr(self, key, value)


def _settings() -> _Settings:
    return _Settings()


def _task(task_id: str = "node-1", *, status: str = "blocked", meta: dict | None = None, error: str | None = None) -> TaskNode:
    return TaskNode(
        id=task_id,
        title=f"标题 {task_id}",
        status=status,
        node_meta_json=json.dumps(meta or {}),
        error_message=error,
    )


def test_block_category_classifies_transient_families() -> None:
    assert blockers.block_category("HTTP 429 too many requests") == "rate_limit"
    assert blockers.block_category("request timed out after 300s") == "timeout"
    assert blockers.block_category("transport channel closed") == "transport"
    # 未命中限流/传输/超时关键词的瞬态错误归 capacity
    assert blockers.block_category("server error 502 bad gateway") == "capacity"


def test_block_category_classifies_human_and_visual_blocks() -> None:
    assert blockers.block_category("权限不足，需要人工授权") == "human_auth"
    assert blockers.block_category("login required") == "human_auth"
    # 401/403 需要词边界，不能命中 4012 这类数字
    assert blockers.block_category("gateway returned 403") == "human_auth"
    assert blockers.block_category("visual_uncertain: 无法可靠判断打击感") == "visual_uncertain"


def test_block_category_classifies_tool_and_environment_blocks() -> None:
    assert blockers.block_category("unexpected argument '--foo'") == "tool_internal"
    assert blockers.block_category("CrashHandlerException: program crashed with signal 11") == "validation_environment"
    assert blockers.block_category("一切顺利") == "external"


def test_block_category_detects_protocol_from_source() -> None:
    # 仅有 action plan 不算协议问题；需要同时给出执行承诺
    reason = "I will implement the level now."
    assert blockers.block_category(reason) == "external"
    assert blockers.protocol_block_category(reason, "worker.blocked") == ""

    promise = "I will implement the level now. This can safely complete the task."
    assert blockers.protocol_block_category(promise, "worker.blocked") == "protocol"
    # 来源不在协议失败名单里时不判定
    assert blockers.protocol_block_category(promise, "child.status") == ""
    # block_category 内部以空来源调用协议判定，因此它必须靠 PROTOCOL_TRANSIENT_MARKERS 才能判出 protocol；
    # 这里锁定现状：无来源的 protocol 承诺不会被 block_category 归类为 protocol。
    assert blockers.block_category(promise) == "external"


def test_protocol_category_skips_obvious_non_protocol_text() -> None:
    assert blockers.obvious_non_protocol_block_text("rate limit exceeded") is True
    assert blockers.obvious_non_protocol_block_text("all good") is False
    mixed = "I will deliver this and it can complete. rate limit exceeded"
    assert blockers.protocol_block_category(mixed, "worker.blocked") == ""


def test_normalize_block_report_reclassifies_false_readonly() -> None:
    report = {
        "reason": "current filesystem sandbox is read-only",
        "source": "worker.blocked",
        "category": "external",
        "retryable": False,
        "parent_action": "manual_review",
    }
    normalized = blockers.normalize_block_report(report)
    assert normalized["category"] == "workspace_sandbox"
    assert normalized["retryable"] is True
    assert normalized["parent_action"] == "wait_and_retry"
    assert normalized["recovery"] == "retry_after_workspace_write_codex_config_fix"
    assert normalized["reclassified_from"]["category"] == "external"
    # 已经是正确分类且可重试时原样返回，不重复包装
    assert blockers.normalize_block_report(normalized) is normalized


def test_normalize_block_report_is_noop_for_clean_report() -> None:
    report = {"reason": "一切顺利", "source": "child.status", "category": "external", "retryable": False}
    assert blockers.normalize_block_report(report) is report


def test_build_block_report_uses_dependency_context() -> None:
    report = blockers.build_block_report(
        _task("child-1"),
        "依赖未完成",
        "dependency.blocked",
        {"dependency_reports": [{"reason": "HTTP 429 too many requests", "source": "worker.blocked"}]},
        _settings(),
    )
    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"
    assert report["task_id"] == "child-1"
    assert "reported_at" in report


def test_build_block_report_reclassifies_false_retryable_payload() -> None:
    report = blockers.build_block_report(
        _task("child-2"),
        "任意原因",
        "worker.blocked",
        {"block_report": {"category": "tool_internal", "retryable": False, "parent_action": "pause_tool_fix", "reason": "429 too many requests"}},
        _settings(),
    )
    # tool_internal + pause_tool_fix 会被瞬态分类覆盖成 wait_and_retry
    assert report["category"] == "rate_limit"
    assert report["retryable"] is True
    assert report["parent_action"] == "wait_and_retry"


def test_build_block_report_marks_human_action_required() -> None:
    report = blockers.build_block_report(_task("child-3"), "权限不足，需要人工授权", "worker.blocked", None, _settings())
    assert report["category"] == "human_auth"
    assert report["human_action_required"] is True
    assert report["parent_action"] == "human_auth_required"


def test_skill_lookup_context_is_gated_by_settings() -> None:
    disabled = blockers.build_block_report(_task("child-4"), "未知第三方工具报错", "worker.blocked", None, _settings())
    assert "skill_lookup" not in disabled

    enabled = blockers.build_block_report(
        _task("child-5"),
        "未知第三方工具报错",
        "worker.blocked",
        None,
        _Settings(skill_lookup_enabled=True, skill_km_url="https://km.example/x"),
    )
    assert enabled["skill_lookup"]["enabled"] is True
    assert enabled["skill_lookup"]["recommended"] is True
    assert enabled["parent_action"] == "skill_lookup_recovery"

    # 可重试分类不推荐查 KM
    assert blockers.skill_lookup_report_context("rate_limit", "wait_and_retry", _Settings(skill_lookup_enabled=True, skill_km_url="https://km.example/x"))["recommended"] is False


def test_dependency_reports_are_retryable() -> None:
    assert blockers.dependency_reports_are_retryable(None) is False
    assert blockers.dependency_reports_are_retryable({}) is False
    assert blockers.dependency_reports_are_retryable({"dependency_reports": []}) is False
    assert blockers.dependency_reports_are_retryable({"dependency_reports": ["not-a-dict"]}) is False
    assert blockers.dependency_reports_are_retryable({"dependency_reports": [{"retryable": True}]}) is True
    # 未标 retryable 但分类可重试，仍算可重试
    assert blockers.dependency_reports_are_retryable({"dependency_reports": [{"category": "rate_limit", "retryable": False}]}) is True
    assert blockers.dependency_reports_are_retryable({"dependency_reports": [{"category": "human_auth", "retryable": False}]}) is False


def test_dependency_report_context_falls_back_to_nested_and_reason() -> None:
    nested = blockers.dependency_report_context(
        {"dependency_reports": [{"category": "human_auth", "retryable": False, "parent_action": "manual_review", "reason": "权限不足"}]}
    )
    # 嵌套报告自带 parent_action，因此沿用原值而不是按 category 重新推导
    assert nested == {"category": "human_auth", "retryable": False, "parent_action": "manual_review"}

    from_reason = blockers.dependency_report_context({"reason": "HTTP 429 too many requests"})
    assert from_reason["category"] == "rate_limit"
    assert from_reason["retryable"] is True

    from_action = blockers.dependency_report_context({"parent_action": "pause_tool_fix"})
    assert from_action == {"category": "tool_internal", "retryable": False, "parent_action": "pause_tool_fix"}


def test_child_block_report_prefers_stored_meta() -> None:
    stored = _task("child-6", meta={"block_report": {"reason": "一切顺利", "source": "child.status", "category": "external", "retryable": False}})
    assert blockers.child_block_report(stored, _settings())["category"] == "external"

    fallback = _task("child-7", error="权限不足，需要人工授权")
    assert blockers.child_block_report(fallback, _settings())["category"] == "human_auth"
    assert blockers.child_block_report(fallback, _settings())["source"] == "child.status"

    no_error = _task("child-8", status="failed")
    report = blockers.child_block_report(no_error, _settings())
    assert report["reason"] == "子节点状态为 failed。"
    assert report["category"] == "external"


def test_can_hibernate_task() -> None:
    retryable = _task("child-9", meta={"block_report": {"reason": "HTTP 429 too many requests", "source": "worker.blocked", "category": "rate_limit", "retryable": True}})
    assert blockers.can_hibernate_task(retryable, _settings()) is True

    fatal = _task("child-10", meta={"block_report": {"reason": "权限不足，需要人工授权", "source": "worker.blocked", "category": "human_auth", "retryable": False, "parent_action": "human_auth_required"}})
    assert blockers.can_hibernate_task(fatal, _settings()) is False

    assert blockers.can_hibernate_task(_task("child-11", status="running"), _settings()) is True
    assert blockers.can_hibernate_task(_task("child-12", status="done"), _settings()) is False


def test_orchestrator_delegates_agree_with_module() -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings()

    reason = "I will deliver this and it can safely complete the task."
    assert orchestrator._block_category(reason) == blockers.block_category(reason)
    assert orchestrator._protocol_block_category(reason, "worker.blocked") == blockers.protocol_block_category(reason, "worker.blocked")
    assert orchestrator._transient_block_category(reason) == blockers.transient_block_category(reason)
    assert orchestrator._explicit_retryable_resource_reason(reason) == blockers.explicit_retryable_resource_reason(reason)
    assert orchestrator._obvious_non_protocol_block_text(reason) == blockers.obvious_non_protocol_block_text(reason)
    assert orchestrator._false_readonly_sandbox_category("filesystem sandbox is read-only", "worker.blocked") == (
        blockers.false_readonly_sandbox_category("filesystem sandbox is read-only", "worker.blocked")
    )
    assert orchestrator._false_tool_capability_protocol_category("available tools", "worker.blocked") == (
        blockers.false_tool_capability_protocol_category("available tools", "worker.blocked")
    )

    task = _task("child-13")
    delegated = orchestrator._build_block_report(task, "HTTP 429 too many requests", "worker.blocked", None)
    direct = blockers.build_block_report(task, "HTTP 429 too many requests", "worker.blocked", None, orchestrator.settings)
    # reported_at 是调用时刻，两次调用必然不同；其余字段必须逐项一致
    assert delegated.pop("reported_at") != ""
    assert direct.pop("reported_at") != ""
    assert delegated == direct
    assert orchestrator._normalize_block_report({"reason": "一切顺利"}) == blockers.normalize_block_report({"reason": "一切顺利"})
    assert orchestrator._dependency_reports_are_retryable({"dependency_reports": [{"retryable": True}]}) == (
        blockers.dependency_reports_are_retryable({"dependency_reports": [{"retryable": True}]})
    )
    assert orchestrator._dependency_block_context({"dependency_reports": [{"retryable": True}]}) == (
        blockers.dependency_block_context({"dependency_reports": [{"retryable": True}]})
    )
    assert orchestrator._dependency_report_context({"reason": "HTTP 429 too many requests"}) == (
        blockers.dependency_report_context({"reason": "HTTP 429 too many requests"})
    )
    child_delegated = orchestrator._child_block_report(task)
    child_direct = blockers.child_block_report(task, orchestrator.settings)
    assert child_delegated.pop("reported_at") != ""
    assert child_direct.pop("reported_at") != ""
    assert child_delegated == child_direct
    assert orchestrator._can_hibernate_task(task) == blockers.can_hibernate_task(task, orchestrator.settings)
    assert orchestrator._skill_lookup_report_context("external", "manual_review") == (
        blockers.skill_lookup_report_context("external", "manual_review", orchestrator.settings)
    )


def test_blocked_recovery_chain_reads_structural_markers() -> None:
    """恢复链标记只认 scope 里的 `planner-blocked:` / `acceptance-blocked:` 前缀。

    反面自检：普通 scope（工程目录之类的自由文本）**不许**被当成恢复链——
    否则任何节点都会被判成"已经在链上"，那条判据就恒真了。
    """
    assert blockers.blocked_recovery_chain(["src/", "planner-blocked:abc", "acceptance-blocked:def"]) == [
        "planner-blocked:abc",
        "acceptance-blocked:def",
    ]
    assert blockers.blocked_recovery_chain(["src/", "docs/", ""]) == []
    assert blockers.blocked_recovery_chain(None) == []
    assert blockers.blocked_recovery_chain([]) == []


def test_blocked_recovery_suppression_allows_only_task_internal_blocks() -> None:
    """blocked → 递归恢复的**唯一放行条件**：任务内部、非重试、有 description、且不在恢复链上。

    正反两例都在这里：放行那一例是防"静默死停"（另一种坏），抑制那几例各自写明理由。
    """
    internal = blockers.build_block_report(_task("node-30"), "无法确定安全的执行边界", "planner.blocked", None, _settings())
    assert blockers.blocked_recovery_suppression(internal, scope=["src/"], has_description=True) == ""

    tool_internal = blockers.build_block_report(_task("node-31"), "工具内部错误", "planner.blocked", None, _settings())
    assert tool_internal["category"] == "tool_internal"
    assert "pause_tool_fix" in blockers.blocked_recovery_suppression(tool_internal, scope=[], has_description=True)

    retryable = blockers.build_block_report(
        _task("node-32"), "HTTP 429 too many requests, retry after 5 hours", "planner.blocked", None, _settings()
    )
    assert retryable["retryable"] is True
    assert "可等待重试" in blockers.blocked_recovery_suppression(retryable, scope=[], has_description=True)

    assert "没有 description" in blockers.blocked_recovery_suppression(internal, scope=[], has_description=False)
    assert "阻塞恢复链" in blockers.blocked_recovery_suppression(internal, scope=["planner-blocked:abc123"], has_description=True)
    assert "阻塞恢复链" in blockers.blocked_recovery_suppression(internal, scope=["acceptance-blocked:abc123"], has_description=True)
