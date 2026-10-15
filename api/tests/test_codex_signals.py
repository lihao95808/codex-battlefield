from __future__ import annotations

from app import codex_signals
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner


def test_http_429_matches_genuine_rate_limit_signals() -> None:
    samples = [
        "HTTP status: 429",
        "status_code=429",
        '{"status": 429}',
        "429 too many requests",
        "429 rate limit",
        "429 quota",
        "server returned 429 resource exhausted",
    ]
    for sample in samples:
        assert codex_signals.has_http_429_marker(sample), sample


def test_http_429_does_not_match_timing_strings() -> None:
    """回归：`429ms` / `429 milliseconds` 是计时串，不是 HTTP 429。

    runner 与 orchestrator 曾各持一份正则，其中 orchestrator 的副本没有排除计时串，
    导致同一条文本在两边得到不同判定。统一到 codex_signals 后此差异消失，
    本测试锁定正确的那一侧。
    """
    samples = [
        "took 429ms",
        "elapsed 429 ms",
        "latency 429milliseconds",
        "duration: 429ms",
        "429ms",
    ]
    for sample in samples:
        assert not codex_signals.has_http_429_marker(sample), sample


def test_is_local_skill_failure_signature() -> None:
    assert codex_signals.is_local_skill_failure(
        "ERROR codex_core::session: failed to load skill "
        "C:/x/.codex/plugins/cache/heygen/skills/heygen-avatar/SKILL.md: "
        "invalid description: exceeds maximum length of 1024 characters"
    )
    assert codex_signals.is_local_skill_failure("failed to load skill: invalid description")
    assert not codex_signals.is_local_skill_failure("failed to load skill")
    assert not codex_signals.is_local_skill_failure("invalid description exceeds maximum length")
    assert not codex_signals.is_local_skill_failure("")


def test_is_transient_failure_text_covers_markers_and_429() -> None:
    assert codex_signals.is_transient_failure_text("503 service unavailable")
    assert codex_signals.is_transient_failure_text("transport channel closed")
    assert codex_signals.is_transient_failure_text("429 too many requests")
    assert codex_signals.is_transient_failure_text("请求超时，请稍后重试")
    assert not codex_signals.is_transient_failure_text("code compiled successfully")


def test_runner_and_orchestrator_delegate_to_the_single_definition() -> None:
    """反漂移：两边对同一输入的判定必须一致。"""
    samples = [
        "429 too many requests",
        "took 429ms",
        "HTTP status: 429",
        "failed to load skill: invalid description",
        "nothing relevant here",
    ]
    for sample in samples:
        assert CodexExecRunner._has_http_429_marker(sample) == codex_signals.has_http_429_marker(sample), sample
        assert CodexExecRunner._is_local_codex_skill_failure(sample) == codex_signals.is_local_skill_failure(sample), sample
        assert (
            Orchestrator._is_local_codex_skill_failure_text(sample)
            == codex_signals.is_local_skill_failure(sample)
        ), sample
