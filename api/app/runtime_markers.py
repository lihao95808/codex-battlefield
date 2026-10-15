"""运行期文本标记与阻塞分类用的判定。

搬移自 `orchestrator.Orchestrator`：这些是纯字符串表与纯文本判定，不依赖任何实例状态，
也**不依赖 orchestrator 的模块常量**，因此可以独立成模块而不产生循环导入。

原先它们散落在两个位置：标记表在文件头部（约 100–207 行），判定函数在类中部（约 3680 行），
彼此相距数千行却只服务于同一件事——阻塞分类。集中到这里后，改词表只需看一个文件。

与 `codex_signals.py` 的分工：

- `codex_signals` 放 **runner 与 orchestrator 共用**的信号（两边曾各持一份并已漂移）；
- 本模块放**只服务于 orchestrator 阻塞分类**的词表与判定。

`Orchestrator` 保留同名的一行委托，调用点零改动。

**隐性契约**：本模块的判定接收**已小写化**的文本（参数名沿用原实现的 `normalized`），
调用方负责先 `lower()`。传入混合大小写不会报错，而是**静默返回 False**——这是原行为，
搬移时保留原样，并由 `api/tests/test_runtime_markers.py` 写死该契约。
"""

from __future__ import annotations

from .codex_signals import HTTP_429_RE

FALSE_READONLY_SANDBOX_MARKERS = (
    "current filesystem sandbox is read-only",
    "filesystem sandbox is read-only",
    "filesystem is read-only",
    "read-only sandbox",
    "restricted read",
    "writing is blocked by read-only sandbox",
    "filesystem permission profile grants read access only",
    "read access only to the workspace root",
    "文件系统权限为 read-only",
    "文件系统权限是 read-only",
    "文件系统沙箱为 read-only",
    "文件系统为只读",
    "文件系统是只读",
    "文件系统沙箱为只读",
    "工作区沙箱为只读",
    "只读沙箱",
    "当前会话文件系统权限",
    "apply_patch 被沙箱拒绝",
    "apply_patch 写入被拒绝",
    "写入补丁被拒绝",
    "写入被拒绝",
    "被沙箱拒绝",
)

FALSE_TOOL_CAPABILITY_BLOCK_MARKERS = (
    "current available tools",
    "available tools",
    "no command execution capability",
    "does not provide command execution",
    "response format requires",
    "codex_output_schema",
    "没有提供命令执行能力",
    "无法执行验证命令",
    "无法执行必要的独立验证命令",
    "拒绝运行必需",
    "工具调用只允许读权限",
    "不能独立核验证据",
)

TRANSPORT_TRANSIENT_MARKERS = (
    "transport channel closed",
    "unexpectedcontenttype",
    "missing-content-type",
    "post_message failed",
)

PROTOCOL_TRANSIENT_MARKERS = (
    "did not write final response",
    "failed to write final response",
    "final response file",
    "no final response",
    "without a final response",
    "invalid final json",
    "invalid structured output",
    "schema validation failed",
    "structured output",
    "output schema",
    "没有写出最终响应",
    "未写出最终响应",
    "最终响应文件",
    "输出协议",
    "只返回最终 json",
)

MODEL_ACTION_PLAN_MARKERS = (
    "i will ",
    "i'll ",
    "i am going to ",
    "我将",
    "我会",
)

MODEL_EXECUTION_PROMISE_MARKERS = (
    "can safely complete",
    "can complete",
    "directly execute",
    "directly deliver",
    "300% complete",
    "可以安全完成",
    "可以完成",
    "可完成",
    "直接执行",
    "直接交付",
    "当前工作树",
    "边界清晰",
    "300% 完成",
    "300%完成",
)


def is_nonfatal_mcp_timeout_line(normalized_line: str) -> bool:
    return "tools/list" in normalized_line and (
        "codex_mcp" in normalized_line
        or "codex_rmcp" in normalized_line
        or "mcp startup failed" in normalized_line
        or "during shutdown" in normalized_line
    )


def has_timeout_marker(normalized: str) -> bool:
    if any(
        marker in normalized
        for marker in (
            "request timed out",
            "gateway timeout",
            "hard timeout after",
            "idle watchdog fired",
            "timeout waiting for child process",
            "godot_timeout",
        )
    ):
        return True
    return any(
        "timed out" in line and not is_nonfatal_mcp_timeout_line(line)
        for line in normalized.splitlines()
    )


def is_shell_tool_exit_output_line(normalized_line: str) -> bool:
    return "codex_core::tools::router" in normalized_line and "error=exit code:" in normalized_line


def has_strong_resource_limit_marker(normalized_line: str) -> bool:
    return any(
        marker in normalized_line
        for marker in (
            "you've hit your usage limit",
            "you have hit your usage limit",
            "too many requests",
            "http status 429",
            '"status":429',
            '"status": 429',
            "insufficient_quota",
            "resource exhausted",
            "retry-after",
            "retry after",
            "try again at",
            "try again in",
            "available at",
            "available in",
            "reset at",
            "resets at",
        )
    )


def shell_tool_exit_output_has_runtime_resource_error(normalized_line: str) -> bool:
    if not has_strong_resource_limit_marker(normalized_line):
        return False
    return any(
        marker in normalized_line
        for marker in (
            '"type":"error"',
            '"type": "error"',
            '\\"type\\":\\"error\\"',
            '\\"type\\": \\"error\\"',
            '"type":"turn.failed"',
            '"type": "turn.failed"',
            '\\"type\\":\\"turn.failed\\"',
            '\\"type\\": \\"turn.failed\\"',
        )
    )


def has_resource_limit_marker(normalized: str) -> bool:
    for line in normalized.splitlines():
        if is_shell_tool_exit_output_line(line):
            if not shell_tool_exit_output_has_runtime_resource_error(line):
                continue
        if any(
            marker in line
            for marker in (
                "rate limit",
                "quota",
                "too many requests",
                "resource exhausted",
                "insufficient_quota",
                "usage limit",
            )
        ):
            return True
        if HTTP_429_RE.search(line):
            return True
    return False
