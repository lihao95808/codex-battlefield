"""planner/worker 输出"多一个 `}`"时的自纠重试：一次解析失败不该污染整棵树。

真实事故（2026-10-18，`deepseek-flash` 走本机 OpenAI 兼容代理）：

    sub-planner 的 `--output-last-message` 末尾是 `…证据。"},"block_report":null}`，
    `json.loads` 抛 `Extra data: line 1 column 1043 (char 1042)`；
    旧代码把它当成 `decision=blocked`，父节点于是转成"递归指导"子任务，
    整棵树形状走偏（`max_depth=2`、叶子没跑、probe 文件不存在）。

这组用例钉住三件事：

1. 结构漂移会触发**一次**自纠重试，第二次拿到合法 JSON 就成功；
2. 重试的提示词里**真的带上了反馈**（否则等于盲重发，模型没有理由改）；
3. 重试次数由 `codex_retry_attempts` 说了算 —— 配 0 时就一次都不多重试，
   而两次都坏时**报错原文**仍然留在 payload 里（不吞、不改写成别的结论）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.runner import CodexExecRunner
from app.schemas import PlannerDecision

# 形状照事故现场抄：合法对象已经完整，后面又跟了一段 `,"block_report":null}`。
MALFORMED_LAST_MESSAGE = '{"decision":"execute","reason":"ok","children":[]},"block_report":null}'

VALID_PAYLOAD = {
    "decision": "execute",
    "reason": "目标目录只有一个 README.md，单个 CLI 即可完成并自验。",
    "children": [],
    "risks": [],
    "leaf_certainty": "300%",
    "why_not_leaf": "",
    "coverage_ids": ["probe-doc"],
    "integration_plan": "单叶子，无需整合。",
    "verification_plan": "检查文件存在、UTF-8 可读、内容含六个小节。",
    "block_report": None,
}


def _settings(tmp_path: Path, **overrides):
    return get_settings().__class__(
        workspace_dir=tmp_path,
        codex_timeout_seconds=30,
        **overrides,
    )


def _install_fake_codex(monkeypatch, runner, outputs: list[str], exits: list[int], launches: list[int]):
    state = {"index": 0}

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        launches.append(1)
        return object()

    async def fake_communicate(process, prompt_bytes, output_path, logs_path, activity_path, log_chunks, kind):
        assert prompt_bytes
        index = min(state["index"], len(outputs) - 1)
        state["index"] += 1
        output_path.write_text(outputs[index], encoding="utf-8")
        return exits[index], b"", None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_communicate_with_watchdog", fake_communicate)


async def _run_planner(runner, tmp_path: Path):
    return await runner._run_codex_io(
        run_id="parse-retry-node",
        cwd=tmp_path,
        kind="planner",
        prompt="只输出一个 JSON 对象。",
        schema_name="planner-decision.schema.json",
        sandbox="read-only",
    )


@pytest.mark.asyncio
async def test_p1_invalid_json_triggers_one_self_correction_retry(monkeypatch, tmp_path: Path) -> None:
    runner = CodexExecRunner(_settings(tmp_path, codex_retry_attempts=2))
    launches: list[int] = []
    _install_fake_codex(
        monkeypatch,
        runner,
        [MALFORMED_LAST_MESSAGE, json.dumps(VALID_PAYLOAD, ensure_ascii=False)],
        [0, 0],
        launches,
    )

    envelope = await _run_planner(runner, tmp_path)

    assert len(launches) == 2, "结构漂移必须触发一次自纠重试，而不是直接把整棵树判成 blocked"
    assert envelope.exit_code == 0
    payload = PlannerDecision.model_validate(envelope.payload)
    assert payload.decision == "execute"

    prompt_text = (tmp_path / "runs" / "parse-retry-node" / "planner-prompt.txt").read_text(encoding="utf-8")
    assert "重试反馈：上一次输出不是合法 JSON" in prompt_text, "重发时提示词里必须带着解析错误"
    assert "Extra data" in prompt_text, "反馈里要有可行动的原始错误，而不是'请重试'"
    assert "只输出一个 JSON 对象" in prompt_text

    logs_text = (tmp_path / "runs" / "parse-retry-node" / "planner.jsonl").read_text(encoding="utf-8")
    assert "attempt 2/3" in logs_text
    assert "after invalid JSON" in logs_text


@pytest.mark.asyncio
async def test_p2_first_attempt_prompt_has_no_feedback_block(monkeypatch, tmp_path: Path) -> None:
    """反馈只能出现在**重发**上：第一次就带着"上次输出不合法"是凭空指责。"""

    runner = CodexExecRunner(_settings(tmp_path, codex_retry_attempts=2))
    seen_prompts: list[str] = []

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return object()

    async def fake_communicate(process, prompt_bytes, output_path, logs_path, activity_path, log_chunks, kind):
        seen_prompts.append(prompt_bytes.decode("utf-8"))
        if len(seen_prompts) == 1:
            output_path.write_text(MALFORMED_LAST_MESSAGE, encoding="utf-8")
        else:
            output_path.write_text(json.dumps(VALID_PAYLOAD, ensure_ascii=False), encoding="utf-8")
        return 0, b"", None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(runner, "_communicate_with_watchdog", fake_communicate)

    await _run_planner(runner, tmp_path)

    assert len(seen_prompts) == 2
    assert "重试反馈" not in seen_prompts[0]
    assert "重试反馈" in seen_prompts[1]


@pytest.mark.asyncio
async def test_p3_retry_budget_is_respected_and_error_is_kept(monkeypatch, tmp_path: Path) -> None:
    """配了 0 次重试就不许偷偷再跑一次；两次都坏时报错原文必须留下。"""

    runner = CodexExecRunner(_settings(tmp_path, codex_retry_attempts=0))
    launches: list[int] = []
    _install_fake_codex(monkeypatch, runner, [MALFORMED_LAST_MESSAGE], [0], launches)

    envelope = await _run_planner(runner, tmp_path)

    assert len(launches) == 1, "codex_retry_attempts=0 时第 1 次失败就是最后一次"
    assert envelope.exit_code == 1
    assert "output parse failed" in str(envelope.payload.get("reason"))
    assert "Extra data" in str(envelope.payload.get("reason"))


@pytest.mark.asyncio
async def test_p4_exhausted_retries_still_surface_the_parse_error(monkeypatch, tmp_path: Path) -> None:
    """一直坏下去要有终点：跑满预算后返回失败，且理由仍是解析错误本身。"""

    runner = CodexExecRunner(_settings(tmp_path, codex_retry_attempts=1))
    launches: list[int] = []
    _install_fake_codex(monkeypatch, runner, [MALFORMED_LAST_MESSAGE], [0], launches)

    envelope = await _run_planner(runner, tmp_path)

    assert len(launches) == 2
    assert envelope.exit_code == 1
    assert "output parse failed" in str(envelope.payload.get("reason"))
    logs_text = (tmp_path / "runs" / "parse-retry-node" / "planner.jsonl").read_text(encoding="utf-8")
    assert "attempt 2/2" in logs_text
