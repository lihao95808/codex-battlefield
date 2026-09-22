"""Codex 失败重试与阻塞报告：重试窗口解析、退避等待、失败分类、块报告装配。

搬移自 `api/app/runner.py`（R-c 波，只搬移不改行为）。判据是
`scripts/audit_orchestrator_movable_mass.py` 的闭包实测：

    python scripts/audit_orchestrator_movable_mass.py api/app/runner.py \
      --closure _sleep_before_retry,_is_transient_failure,_has_http_429_marker,_transient_failure_category,_retry_after_seconds_from_message,_clock_retry_after_seconds_from_message,_observed_local_time_from_message,_payload_message,_attach_retryable_block_report,_attach_tool_internal_block_report,_is_tool_internal_failure,_is_local_codex_skill_failure,_read_payload,_failure_payload \
      --state

宽闭包实测 **15 方法 / 253 行**、15/15 零实例状态、不触达不可搬。其中本波只搬 **11 个真身**：
另外三个两行"别名"（`_is_transient_failure` / `_has_http_429_marker` / `_is_local_codex_skill_failure`）
留在 runner.py——它们本来就是 `codex_signals.py` 单一定义的皮，搬过来会变成自己调自己（见下）；
还有一个 2 行委托是 R-b 的。

职责边界：这一簇只管"失败之后怎么办"——等多久、算不算瞬态、写进块报告的字段怎么填。
它**不**拉起 Codex、不解析 CLI 输出、不碰任务树；判定用的信号单一定义在 `codex_signals.py`，
本模块只做调用，不重复实现。

依赖方向：只依赖 `config` / `models` / `utils` / `codex_signals` / `prompts`，
**不得**导入 `runner` 或 `orchestrator`。
"""

from __future__ import annotations

import asyncio
import json
import re

from .codex_signals import TIME_UNIT_SECONDS, has_http_429_marker, is_local_skill_failure
from .config import Settings
from datetime import datetime, timedelta
from pathlib import Path
from . import prompts
from .utils import repair_mojibake

async def sleep_before_retry(settings: Settings, 
        logs_path: Path,
        log_chunks: list[bytes],
        kind: str,
        attempt: int,
        attempts: int,
        reason: str,
        observed_text: str = "",) -> bool:
    """等到点再重试；返回是否还要在进程内重试。

        CLI 已经明说"X 分钟后重试"时，进程内 8s/16s 的指数退避是**白跑一次真实启动**：
        额度不会因为等 8 秒就回来，而每次尝试都要真的拉起一次 Codex CLI（也真的花额度）。
        窗口不长就在原地等到点——比让整棵树休眠划算；窗口很长就放弃进程内重试，
        把窗口写进块报告交给上层，那本来就是为这种情况设计的路径。
        """
    delay = max(0.0, settings.codex_retry_base_delay_seconds) * (2 ** max(0, attempt - 1))
    observed_wait = retry_after_seconds_from_message(observed_text or reason)
    if observed_wait is not None:
        max_in_process = max(0, int(settings.codex_retry_max_in_process_wait_seconds))
        if observed_wait > max_in_process:
            log_chunks.append(
                (
                    f"\n--- not retrying codex {kind} in-process: codex reported a "
                    f"{observed_wait}s window (> {max_in_process}s), handing it to the caller "
                    f"({attempt}/{attempts}): {reason} ---\n"
                ).encode("utf-8")
            )
            logs_path.write_bytes(b"".join(log_chunks))
            return False
        delay = max(delay, float(observed_wait))
    log_chunks.append(
        f"\n--- retrying codex {kind} after {delay:.1f}s ({attempt}/{attempts}): {reason} ---\n".encode("utf-8")
    )
    logs_path.write_bytes(b"".join(log_chunks))
    await asyncio.sleep(delay)
    return True


def transient_failure_category(message: str) -> str:
    normalized = message.lower()
    if has_http_429_marker(normalized) or any(
        marker in normalized for marker in ("quota", "rate limit", "too many requests", "限流", "额度", "余额", "流量")
    ):
        return "rate_limit"
    if any(marker in normalized for marker in ("timeout", "timed out", "hard timeout", "idle watchdog", "超时")):
        return "timeout"
    if any(
        marker in normalized
        for marker in ("transport channel closed", "unexpectedcontenttype", "missing-content-type", "post_message failed")
    ):
        return "transport"
    return "capacity"


def retry_after_seconds_from_message(message: str) -> int | None:
    normalized = message.lower()
    candidates: list[int] = []
    retry_patterns = (
        r"(?:retry-after|retry after|try again in|available in|reset in|resets in|wait(?: for)?|等待)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|秒|分钟|小时)",
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|秒|分钟|小时)\s*(?:later|后|之后|以后|until reset|to reset)",
    )
    for pattern in retry_patterns:
        for match in re.finditer(pattern, normalized, flags=re.IGNORECASE):
            amount = float(match.group(1))
            unit = match.group(2).lower()
            multiplier = TIME_UNIT_SECONDS.get(unit)
            if multiplier:
                candidates.append(int(amount * multiplier))

    retry_after_header = re.search(r"retry-after\s*[:=]\s*(\d+)", normalized, flags=re.IGNORECASE)
    if retry_after_header:
        candidates.append(int(retry_after_header.group(1)))

    clock_retry_after = clock_retry_after_seconds_from_message(message)
    if clock_retry_after is not None:
        candidates.append(clock_retry_after)

    quota_context_markers = (
        "quota",
        "rate limit",
        "too many requests",
        "usage",
        "limit",
        "reset",
        "retry",
        "try again",
        "available",
        "wait",
        "额度",
        "余额",
        "流量",
        "限流",
        "重置",
        "刷新",
        "等待",
        "稍后重试",
    )
    if has_http_429_marker(normalized) or any(marker in normalized for marker in quota_context_markers):
        bare_window_pattern = r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m(?!s\b)|hours?|hrs?|h|秒|分钟|小时)"
        for match in re.finditer(bare_window_pattern, normalized, flags=re.IGNORECASE):
            amount = float(match.group(1))
            unit = match.group(2).lower()
            multiplier = TIME_UNIT_SECONDS.get(unit)
            if multiplier:
                candidates.append(int(amount * multiplier))

    if not candidates:
        return None
    return min(max(max(candidates), 60), 604_800)


def clock_retry_after_seconds_from_message(message: str) -> int | None:
    match = re.search(r"(?i)(?:try again at|available at|reset at|resets at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", message)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if hour > 23 or minute > 59:
        return None
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    observed_local = observed_local_time_from_message(message)
    now = datetime.now().astimezone()
    base = observed_local or now
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if observed_local is not None and candidate < observed_local - timedelta(minutes=2):
        candidate += timedelta(days=1)
    elif observed_local is None and candidate < now - timedelta(hours=12):
        candidate += timedelta(days=1)
    if candidate <= now:
        return 0
    return int((candidate - now).total_seconds())


def observed_local_time_from_message(message: str) -> datetime | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", message)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def payload_message(payload: dict) -> str:
    for key in ("reason", "summary", "markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value[:2000]
    risks = payload.get("risks")
    if isinstance(risks, list) and risks:
        return " ".join(str(item) for item in risks[:3])[:2000]
    return json.dumps(payload, ensure_ascii=False)[:2000]


def attach_retryable_block_report(settings: Settings, payload: dict, kind: str, attempts: int) -> dict:
    message = payload_message(payload)
    parsed_retry_after_seconds = retry_after_seconds_from_message(message)
    retry_after_seconds = parsed_retry_after_seconds
    if retry_after_seconds is None:
        retry_after_seconds = max(60, min(int(settings.resource_block_resume_after_seconds), 604_800))
    next_payload = dict(payload)
    report = {
        "category": transient_failure_category(message),
        "retryable": True,
        "source": kind,
        "reason": message,
        "attempts": attempts,
        "parent_action": "wait_and_retry",
        "retry_after_seconds": retry_after_seconds,
        "quota_window_seconds": retry_after_seconds,
        "retry_after_source": "codex_output" if parsed_retry_after_seconds is not None else "fallback_resource_window",
    }
    next_payload["block_report"] = report
    if "reason" in next_payload and isinstance(next_payload["reason"], str):
        next_payload["reason"] = f"资源/流量类阻塞，可等待后重试：{next_payload['reason']}"
    if "summary" in next_payload and isinstance(next_payload["summary"], str):
        next_payload["summary"] = f"资源/流量类阻塞，可等待后重试：{next_payload['summary']}"
    return next_payload


def attach_tool_internal_block_report(payload: dict, kind: str, attempts: int) -> dict:
    message = payload_message(payload)
    next_payload = dict(payload)
    if is_local_skill_failure(message):
        report = {
            "category": "local_skill_metadata",
            "retryable": True,
            "source": kind,
            "reason": message,
            "attempts": attempts,
            "parent_action": "wait_and_retry",
            "recovery": "retry_with_battlefield_child_codex_user_config_isolation",
        }
        next_payload["block_report"] = report
        prefix = "本地 Codex skill 元数据错误，Battlefield 将用隔离用户配置/禁用插件的子进程重试："
        if "reason" in next_payload and isinstance(next_payload["reason"], str):
            next_payload["reason"] = f"{prefix}{next_payload['reason']}"
        if "summary" in next_payload and isinstance(next_payload["summary"], str):
            next_payload["summary"] = f"{prefix}{next_payload['summary']}"
        return next_payload
    report = {
        "category": "tool_internal",
        "retryable": False,
        "source": kind,
        "reason": message,
        "attempts": attempts,
        "parent_action": "pause_tool_fix",
    }
    next_payload["block_report"] = report
    prefix = "工具内部错误，已阻止递归拆分并等待工具修复："
    if "reason" in next_payload and isinstance(next_payload["reason"], str):
        next_payload["reason"] = f"{prefix}{next_payload['reason']}"
    if "summary" in next_payload and isinstance(next_payload["summary"], str):
        next_payload["summary"] = f"{prefix}{next_payload['summary']}"
    return next_payload


def is_tool_internal_failure(text: str) -> bool:
    normalized = text.lower()
    return any(
        marker in normalized
        for marker in (
            "unexpected argument",
            "usage: codex exec",
            "invalid value",
            "unknown option",
            "unrecognized option",
            "failed to parse",
            "failed to load skill",
            "invalid description",
            "exceeds maximum length",
        )
    )


def read_payload(output_path: Path, kind: str) -> dict:
    if not output_path.exists():
        return failure_payload(kind, "Codex 没有写出最终响应文件。")
    raw = output_path.read_text(encoding="utf-8").strip()
    try:
        return repair_mojibake(json.loads(raw))
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return repair_mojibake(json.loads(raw[start : end + 1]))
        raise


def failure_payload(kind: str, message: str) -> dict:
    if kind == "constitution":
        return {"markdown": f"# 项目宪法生成失败\n\n{message}", "data": prompts.default_constitution_data(message)}
    if kind == "planner":
        return {"decision": "blocked", "reason": message, "children": [], "risks": [message]}
    if kind == "worker":
        return {"status": "blocked", "summary": message, "changed_files": [], "tests": [], "evidence": [], "risks": [message], "suggested_children": []}
    if kind == "acceptance":
        return {"verdict": "blocked", "reason": message, "checks": [], "evidence": [], "suggested_children": [], "constitution_verdict": "not_checked"}
    return {"verdict": "blocked", "reason": message, "checks": [], "evidence": [], "suggested_children": [], "leaf_misclassified": False, "constitution_verdict": "not_checked", "failure_to_decomposition_reason": ""}
