"""真实 runner 的失败重试链：CLI 已经给出的窗口必须被当回事。

原来的写法是无脑指数退避（8s、16s）。额度耗尽时 CLI 会明说"X 分钟后重试"，
等 8 秒再拉起一次真实 Codex 是白花额度——每次尝试都要真的执行一次 CLI。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import blockers, watchdog
from app.config import get_settings
from app.models import TaskNode
from app.runner import CodexExecRunner

QUOTA_WINDOW_SECONDS = 10_800
SHORT_WINDOW_SECONDS = 90


def _settings(tmp_path: Path, **overrides):
    return get_settings().__class__(
        workspace_dir=tmp_path,
        codex_retry_attempts=3,
        codex_retry_base_delay_seconds=8,
        **overrides,
    )


def _install_fake_codex(monkeypatch, runner, outcomes: list[tuple[int, bytes]], launches: list[int], sleeps: list[float]):
    state = {"index": 0}

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        launches.append(1)
        return object()

    async def fake_communicate(process, prompt_bytes, output_path, logs_path, activity_path, log_chunks, kind):
        assert prompt_bytes
        index = state["index"]
        state["index"] += 1
        exit_code, stdout = outcomes[min(index, len(outcomes) - 1)]
        if exit_code == 0:
            output_path.write_text(json.dumps({"status": "succeeded"}), encoding="utf-8")
        return exit_code, stdout, None

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_communicate_with_watchdog", fake_communicate)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)


async def _run(runner, tmp_path: Path):
    return await runner._run_codex_io(
        run_id="retry-backoff-node",
        cwd=tmp_path,
        kind="worker",
        prompt="Do the work.",
        schema_name="worker-result.schema.json",
        sandbox="workspace-write",
    )


@pytest.mark.asyncio
async def test_long_quota_window_is_not_retried_in_process(monkeypatch, tmp_path: Path) -> None:
    """CLI 说 3 小时后再来，进程内就不该再拉起一次 Codex。"""
    runner = CodexExecRunner(_settings(tmp_path, codex_retry_max_in_process_wait_seconds=300))
    launches: list[int] = []
    sleeps: list[float] = []
    stdout = f"ERROR: rate limit exceeded (HTTP 429 too many requests); retry after {QUOTA_WINDOW_SECONDS} seconds\n".encode()
    _install_fake_codex(monkeypatch, runner, [(1, stdout)], launches, sleeps)

    envelope = await _run(runner, tmp_path)

    assert len(launches) == 1, "长窗口下进程内不得再启动一次真实 Codex"
    assert sleeps == []
    assert envelope.exit_code == 1
    report = envelope.payload["block_report"]
    assert report["retry_after_seconds"] == QUOTA_WINDOW_SECONDS
    assert report["retry_after_source"] == "codex_output"


@pytest.mark.asyncio
async def test_short_quota_window_is_waited_in_process(monkeypatch, tmp_path: Path) -> None:
    """短窗口（90s）在原地等到点，比让整棵树休眠划算。"""
    runner = CodexExecRunner(_settings(tmp_path, codex_retry_max_in_process_wait_seconds=300))
    launches: list[int] = []
    sleeps: list[float] = []
    stdout = f"HTTP 429 too many requests; retry after {SHORT_WINDOW_SECONDS} seconds\n".encode()
    _install_fake_codex(monkeypatch, runner, [(1, stdout), (0, b"")], launches, sleeps)

    envelope = await _run(runner, tmp_path)

    assert len(launches) == 2
    assert sleeps == [float(SHORT_WINDOW_SECONDS)], "应按 CLI 给的窗口等，而不是 8 秒"
    assert envelope.exit_code == 0


@pytest.mark.asyncio
async def test_without_a_window_the_exponential_backoff_still_applies(monkeypatch, tmp_path: Path) -> None:
    """没有窗口信息时保持原样：8s、16s 指数退避。"""
    runner = CodexExecRunner(_settings(tmp_path, codex_retry_max_in_process_wait_seconds=300))
    launches: list[int] = []
    sleeps: list[float] = []
    _install_fake_codex(monkeypatch, runner, [(1, b"ERROR: service unavailable\n")], launches, sleeps)

    envelope = await _run(runner, tmp_path)

    assert len(launches) == 4
    assert sleeps == [8.0, 16.0, 32.0]
    assert envelope.payload["block_report"]["retryable"] is True


@pytest.mark.asyncio
async def test_zero_max_in_process_wait_disables_in_process_retry(monkeypatch, tmp_path: Path) -> None:
    runner = CodexExecRunner(_settings(tmp_path, codex_retry_max_in_process_wait_seconds=0))
    launches: list[int] = []
    sleeps: list[float] = []
    stdout = f"HTTP 429 too many requests; retry after {SHORT_WINDOW_SECONDS} seconds\n".encode()
    _install_fake_codex(monkeypatch, runner, [(1, stdout)], launches, sleeps)

    envelope = await _run(runner, tmp_path)

    assert len(launches) == 1
    assert sleeps == []
    assert envelope.payload["block_report"]["retry_after_seconds"] == SHORT_WINDOW_SECONDS


def test_runner_block_report_window_reaches_the_hibernate_duration(tmp_path: Path) -> None:
    """窗口必须走完整条路：runner 信封 → 节点块报告 → 休眠时长。

    半路掉键不会报错，只会让整棵树按兜底窗口睡去（额度还没回来，或者白等一整天）。
    这条路上有两个可以"顺手简化掉"的点：`blockers.build_block_report` 显式搬运
    `retry_after_seconds`，`watchdog.retry_after_seconds_from_reports` 把它读成休眠时长。
    """
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace", resource_block_resume_after_seconds=600)
    runner = CodexExecRunner(settings)
    payload = runner._attach_retryable_block_report(
        {"status": "blocked", "summary": f"rate limit exceeded; retry after {QUOTA_WINDOW_SECONDS} seconds"},
        "worker",
        3,
    )
    assert payload["block_report"]["retry_after_seconds"] == QUOTA_WINDOW_SECONDS

    task = TaskNode(id="node-window", title="叶子", status="blocked", node_meta_json="{}")
    report = blockers.build_block_report(task, "资源/流量类阻塞", "worker", payload, settings)
    assert report["retry_after_seconds"] == QUOTA_WINDOW_SECONDS
    assert report["quota_window_seconds"] == QUOTA_WINDOW_SECONDS

    assert (
        watchdog.retry_after_seconds_from_reports([report], settings.resource_block_resume_after_seconds)
        == QUOTA_WINDOW_SECONDS
    )
