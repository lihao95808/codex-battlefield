"""R-c 反漂移：`runner.py` 的重试/阻塞报告委托必须与 `retry` 模块给出同一答案。

这一簇是"失败之后怎么办"的决策层：等多久、算不算瞬态、块报告字段怎么填。写错不会抛异常，
只会让**真实 Codex 启动额度**白烧或让整棵树提前休眠——所以断言的是可观测结果（返回值、
写进日志的字节），而不是"函数被调用过"。

另有一条依赖方向断言：`retry` 不得反向依赖 `runner`（搬出去又被核心绑住 = 白搬）。
"""

from __future__ import annotations

import ast
import asyncio
import json
import pathlib

from app import retry, runner
from app.config import Settings

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

SHORT_WINDOW_MESSAGE = "rate limit exceeded; retry after 90 seconds"
LONG_WINDOW_MESSAGE = "rate limit exceeded; retry after 10800 seconds"


def _settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


def _executor(tmp_path: pathlib.Path) -> runner.CodexExecRunner:
    return runner.CodexExecRunner(_settings(tmp_path))


def test_pure_retry_helpers_agree_with_module(tmp_path: pathlib.Path) -> None:
    executor = _executor(tmp_path)
    payload = {"status": "blocked", "summary": "HTTP 429 too many requests"}

    assert executor._retry_after_seconds_from_message(LONG_WINDOW_MESSAGE) == retry.retry_after_seconds_from_message(
        LONG_WINDOW_MESSAGE
    )
    assert executor._clock_retry_after_seconds_from_message("retry after 12:34") == (
        retry.clock_retry_after_seconds_from_message("retry after 12:34")
    )
    assert executor._observed_local_time_from_message("observed local time 09:10") == (
        retry.observed_local_time_from_message("observed local time 09:10")
    )
    assert executor._transient_failure_category("HTTP 429 too many requests") == (
        retry.transient_failure_category("HTTP 429 too many requests")
    )
    assert executor._is_tool_internal_failure("unexpected argument '--foo'") == (
        retry.is_tool_internal_failure("unexpected argument '--foo'")
    )
    assert executor._payload_message(payload) == retry.payload_message(payload)
    assert executor._failure_payload("worker", "boom") == retry.failure_payload("worker", "boom")
    # 委托保持原签名（settings 由委托内部取），模块函数才收显式 settings
    assert executor._attach_retryable_block_report(payload, "worker", 3) == (
        retry.attach_retryable_block_report(_settings(tmp_path), payload, "worker", 3)
    )
    assert executor._attach_tool_internal_block_report(payload, "worker", 3) == (
        retry.attach_tool_internal_block_report(payload, "worker", 3)
    )


def test_read_payload_delegate_agrees_with_module(tmp_path: pathlib.Path) -> None:
    executor = _executor(tmp_path)
    output = tmp_path / "worker.json"
    output.write_text(json.dumps({"status": "succeeded", "summary": "ok"}), encoding="utf-8")

    assert executor._read_payload(output, "worker") == retry.read_payload(output, "worker")


def test_sleep_before_retry_delegate_agrees_with_module(tmp_path: pathlib.Path, monkeypatch) -> None:
    """async 委托要 `await`；两边都必须给出同一「睡多久 + 写什么日志 + 是否继续」。"""
    settings = _settings(tmp_path)
    executor = _executor(tmp_path)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    counter = {"n": 0}

    def run(handler) -> tuple[bool, bytes]:
        counter["n"] += 1
        log_path = tmp_path / f"retry-{counter['n']}.log"
        log_path.write_bytes(b"head")
        chunks = [b"head"]
        returned = asyncio.run(handler(log_path, chunks))
        return returned, log_path.read_bytes()

    delegated = run(
        lambda logs, chunks: executor._sleep_before_retry(logs, chunks, "worker", 1, 3, SHORT_WINDOW_MESSAGE)
    )
    direct = run(lambda logs, chunks: retry.sleep_before_retry(settings, logs, chunks, "worker", 1, 3, SHORT_WINDOW_MESSAGE))
    assert delegated == direct
    assert delegated[0] is True
    assert sleeps == [90.0, 90.0]

    # 长窗口：两边都应放弃进程内重试，把窗口交给上层
    handed_off = run(
        lambda logs, chunks: executor._sleep_before_retry(logs, chunks, "worker", 1, 3, LONG_WINDOW_MESSAGE)
    )
    handed_off_direct = run(
        lambda logs, chunks: retry.sleep_before_retry(settings, logs, chunks, "worker", 1, 3, LONG_WINDOW_MESSAGE)
    )
    assert handed_off == handed_off_direct
    assert handed_off[0] is False
    assert "not retrying codex worker in-process" in handed_off[1].decode("utf-8")
    assert len(sleeps) == 2  # 长窗口那次不该睡


def test_retry_does_not_depend_on_runner() -> None:
    tree = ast.parse((APP_DIR / "retry.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.module:
                    relative.add(node.module.split(".")[0])
                else:
                    # `from . import prompts`
                    relative.update(alias.name.split(".")[0] for alias in node.names)
                continue
            if node.module:
                imported.add(node.module.split(".")[0])
    assert "runner" not in imported and "orchestrator" not in imported, "retry 反向依赖了核心"
    assert relative <= {"codex_signals", "config", "models", "utils", "prompts"}, f"出现了意外依赖：{sorted(relative)}"
