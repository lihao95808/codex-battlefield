from __future__ import annotations

from app import runtime_markers
from app.orchestrator import Orchestrator


def low(text: str) -> str:
    """这些判定接收**已小写化**的文本（调用方负责 lower()），测试统一走这里。"""
    return text.lower()


def test_predicates_require_already_lowercased_input() -> None:
    """记录一个隐性契约：传入混合大小写会**静默**得到 False。

    原实现的调用方都先做了 `normalized = something.lower()`，因此本模块沿用该约定。
    这条测试把它写死，避免日后误用。
    """
    mixed = "MCP startup failed: timed out awaiting tools/list after 30s"
    assert runtime_markers.is_nonfatal_mcp_timeout_line(mixed) is False
    assert runtime_markers.is_nonfatal_mcp_timeout_line(low(mixed)) is True


def test_has_timeout_marker_basic() -> None:
    assert runtime_markers.has_timeout_marker(low("request timed out after 30s")) is True
    assert runtime_markers.has_timeout_marker(low("gateway timeout")) is True
    assert runtime_markers.has_timeout_marker(low("godot_timeout: something")) is True
    assert runtime_markers.has_timeout_marker(low("all good, compiled fine")) is False


def test_mcp_shutdown_tools_list_timeout_is_not_a_timeout() -> None:
    """既有的特例：MCP 关闭期间等 tools/list 超时，不算运行失败。"""
    line = low("MCP startup failed: timed out awaiting tools/list after 30s")
    assert runtime_markers.is_nonfatal_mcp_timeout_line(line) is True
    assert runtime_markers.has_timeout_marker(line) is False


def test_shell_tool_exit_line_detection() -> None:
    line = low('ERROR codex_core::tools::router: error=Exit code: 1 something')
    assert runtime_markers.is_shell_tool_exit_output_line(line) is True
    assert runtime_markers.is_shell_tool_exit_output_line(low("some other line")) is False


def test_strong_resource_limit_marker() -> None:
    assert runtime_markers.has_strong_resource_limit_marker(low('"status": 429')) is True
    assert runtime_markers.has_strong_resource_limit_marker(low("you've hit your usage limit")) is True
    assert runtime_markers.has_strong_resource_limit_marker(low("nothing relevant")) is False


def test_resource_limit_ignores_shell_tool_noise_without_runtime_error() -> None:
    """shell 工具退出码行只有在**同时**带运行时错误标记时才算资源限制。"""
    noisy = low('codex_core::tools::router: error=Exit code: 1 ... "status": 429 ...')
    assert runtime_markers.is_shell_tool_exit_output_line(noisy) is True
    # 有强标记但缺 "type":"error" / turn.failed → 不视为运行时资源错误
    assert runtime_markers.shell_tool_exit_output_has_runtime_resource_error(noisy) is False
    assert runtime_markers.has_resource_limit_marker(noisy) is False

    real = noisy + low(' "type":"turn.failed"')
    assert runtime_markers.shell_tool_exit_output_has_runtime_resource_error(real) is True
    assert runtime_markers.has_resource_limit_marker(real) is True


def test_resource_limit_plain_markers() -> None:
    assert runtime_markers.has_resource_limit_marker(low("You have hit a rate limit")) is True
    assert runtime_markers.has_resource_limit_marker(low("429 too many requests")) is True
    assert runtime_markers.has_resource_limit_marker(low("nothing relevant")) is False


def test_marker_tables_are_non_empty() -> None:
    for table in (
        runtime_markers.FALSE_READONLY_SANDBOX_MARKERS,
        runtime_markers.FALSE_TOOL_CAPABILITY_BLOCK_MARKERS,
        runtime_markers.TRANSPORT_TRANSIENT_MARKERS,
        runtime_markers.PROTOCOL_TRANSIENT_MARKERS,
        runtime_markers.MODEL_ACTION_PLAN_MARKERS,
        runtime_markers.MODEL_EXECUTION_PROMISE_MARKERS,
    ):
        assert table, "标记表搬移后不应为空"


def test_orchestrator_delegates_match_the_module() -> None:
    """反漂移：搬移后 Orchestrator 的同名判定必须与模块函数一致。"""
    samples = [
        low("request timed out"),
        low("MCP startup failed: timed out awaiting tools/list after 30s"),
        low("429 too many requests"),
        low("codex_core::tools::router: error=Exit code: 1"),
        low("nothing relevant here"),
    ]
    for sample in samples:
        assert Orchestrator._has_timeout_marker(sample) == runtime_markers.has_timeout_marker(sample), sample
        assert Orchestrator._has_resource_limit_marker(sample) == runtime_markers.has_resource_limit_marker(sample), sample
        assert (
            Orchestrator._has_strong_resource_limit_marker(sample)
            == runtime_markers.has_strong_resource_limit_marker(sample)
        ), sample
