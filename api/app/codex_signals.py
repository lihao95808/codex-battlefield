"""Codex CLI 输出里的信号识别 —— 单一定义处。

这些词表与正则原先在 `runner.py` 与 `orchestrator.py` 各持一份，并且**已经漂移过一次**：

- `runner` 的 429 正则排除了 `429ms` / `429 milliseconds` 这类**计时串**（避免把耗时误判成限流）；
- `orchestrator` 的内联副本没有排除，于是同一条文本在两边可能得到不同判定。

集中到这里后，两边共用同一份定义。新增或修改信号词表只改本文件。

注意：这里只放**跨模块共享**的判定。各模块**专有**的判定（例如 orchestrator 里带 MCP
特例与 shell 工具退出码处理的 `_has_timeout_marker`、`_has_resource_limit_marker`）语义
并不相同，不属于重复，仍留在各自模块内。
"""

from __future__ import annotations

import re

TRANSIENT_CODEX_FAILURE_MARKERS = (
    "502",
    "503",
    "504",
    "capacity",
    "gateway timeout",
    "godot_resource_queue",
    "godot_resource_queue_timeout",
    "hard timeout",
    "idle watchdog",
    "model overloaded",
    "no output",
    "no stdout",
    "overloaded",
    "rate limit",
    "server error",
    "service unavailable",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "transport channel closed",
    "too many requests",
    "try again",
    "unexpectedcontenttype",
    "missing-content-type",
    "post_message failed",
    "稍后重试",
    "暂时不可用",
    "繁忙",
    "限流",
    "超时",
    "超载",
    "过载",
)

# 第 4 组刻意排除 `429ms` / `429 milliseconds`：那是计时串，不是 HTTP 429。
HTTP_429_RE = re.compile(
    r"(?:http\s*(?:status|response)?\s*(?:code)?\s*[:=]?\s*429"
    r"|(?:status|status_code|status code|response status)\s*[:=]\s*429"
    r"|[\"']status[\"']\s*:\s*429"
    r"|(?<![\d.])429(?![\d.]|ms\b|milliseconds?\b)\s*[:,-]?\s*"
    r"(?:too many requests|rate limit|quota|resource exhausted|usage limit|retry after|overloaded))",
    re.IGNORECASE,
)

TIME_UNIT_SECONDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "秒": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "分钟": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "小时": 3600,
}


def has_http_429_marker(text: str) -> bool:
    return bool(HTTP_429_RE.search(text))


def is_transient_failure_text(message: str) -> bool:
    normalized = message.lower()
    if has_http_429_marker(normalized):
        return True
    return any(marker in normalized for marker in TRANSIENT_CODEX_FAILURE_MARKERS)


def is_local_skill_failure(text: str) -> bool:
    """本地插件 skill 元数据错误。

    这类失败是**确定性**的：重试不会好，也不该被当成资源问题把整棵树休眠。
    因此它的判定优先级高于资源类分类（见 `_block_category` 的注释）。
    """
    normalized = text.lower()
    return "failed to load skill" in normalized and (
        "invalid description" in normalized
        or "exceeds maximum length" in normalized
        or "skill.md" in normalized
    )
