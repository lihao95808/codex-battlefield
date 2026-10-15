from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import runtime_logs
from app.models import TaskNode
from app.orchestrator import Orchestrator


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.workspace_dir = "."
        self.machine_safety_min_available_memory_mb = 0
        self.auto_concurrency_resource_cooldown_seconds = 0
        for key, value in overrides.items():
            setattr(self, key, value)


def _task(task_id: str, *, status: str = "working", meta: dict | None = None, updated_at: datetime | None = None) -> TaskNode:
    return TaskNode(
        id=task_id,
        title=task_id,
        status=status,
        node_meta_json=json.dumps(meta or {}),
        updated_at=updated_at,
    )


def _write_log(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_seconds_since() -> None:
    assert runtime_logs.seconds_since(None) == 0
    # naive 视为 UTC；未来时间夹到 0
    past = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert 30 <= runtime_logs.seconds_since(past) <= 60
    assert runtime_logs.seconds_since(past.replace(tzinfo=None)) >= 30
    assert runtime_logs.seconds_since(datetime.now(timezone.utc) + timedelta(hours=1)) == 0


def test_agent_phase_for_status() -> None:
    assert runtime_logs.agent_phase_for_status("planning") == "planner"
    assert runtime_logs.agent_phase_for_status("decomposing") == "planner"
    assert runtime_logs.agent_phase_for_status("verifying") == "verifier"
    assert runtime_logs.agent_phase_for_status("accepting") == "acceptance"
    assert runtime_logs.agent_phase_for_status("working") == "worker"
    assert runtime_logs.agent_phase_for_status("whatever") == "worker"


def test_codex_log_line_timestamp(tmp_path: Path) -> None:
    stamp = runtime_logs.codex_log_line_timestamp("2026-01-02T03:04:05Z hello")
    assert stamp == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert runtime_logs.codex_log_line_timestamp("no timestamp here") is None


def test_file_mtime_utc(tmp_path: Path) -> None:
    target = tmp_path / "x.jsonl"
    target.write_text("x", encoding="utf-8")
    assert runtime_logs.file_mtime_utc(target) is not None
    assert runtime_logs.file_mtime_utc(tmp_path / "missing.jsonl") is None


def test_runtime_marker_candidate_line() -> None:
    assert runtime_logs.runtime_marker_candidate_line("") == ""
    assert runtime_logs.runtime_marker_candidate_line("   ") == ""
    # 非 JSON：时间戳行保留
    assert runtime_logs.runtime_marker_candidate_line("2026-01-02T03:04:05Z stuff") == "2026-01-02T03:04:05Z stuff"
    # 非 JSON：命中关键词
    assert runtime_logs.runtime_marker_candidate_line("HTTP 429 too many requests") != ""
    # 非 JSON：无标记不保留
    assert runtime_logs.runtime_marker_candidate_line("all good") == ""
    # JSON error 事件保留
    assert runtime_logs.runtime_marker_candidate_line(json.dumps({"type": "turn.failed"})) != ""
    # JSON item 事件丢弃
    assert runtime_logs.runtime_marker_candidate_line(json.dumps({"type": "item.done", "item": {}})) == ""
    # JSON 含 error 键保留
    assert runtime_logs.runtime_marker_candidate_line(json.dumps({"error": "boom"})) != ""
    # JSON 其它一律丢弃
    assert runtime_logs.runtime_marker_candidate_line(json.dumps({"type": "noise"})) == ""
    # 非法 JSON 原样保留（注意 strip 会去掉尾部空格）
    assert runtime_logs.runtime_marker_candidate_line('{"broken": ') == '{"broken":'


def test_runtime_marker_candidate_lines() -> None:
    text = "all good\n2026-01-02T03:04:05Z ok\nHTTP 429 too many requests\n"
    assert runtime_logs.runtime_marker_candidate_lines(text) == [
        "2026-01-02T03:04:05Z ok",
        "HTTP 429 too many requests",
    ]


def test_latest_meaningful_agent_log_time(tmp_path: Path) -> None:
    assert runtime_logs.latest_meaningful_agent_log_time(tmp_path / "missing.jsonl") is None

    log = tmp_path / "worker.jsonl"
    _write_log(log, ["noise", "2026-01-02T03:04:05Z work"])
    assert runtime_logs.latest_meaningful_agent_log_time(log) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    # 无任何有意义行 → None（不是 mtime 回退）
    _write_log(log, ["", "   "])
    assert runtime_logs.latest_meaningful_agent_log_time(log) is None


def test_agent_log_markers(tmp_path: Path) -> None:
    assert runtime_logs.agent_log_markers(tmp_path / "missing.jsonl") == ([], "")

    log = tmp_path / "worker.jsonl"
    # reconnecting / http_fallback 只在**候选行**里被识别，因此必须带时间戳才会进入候选
    _write_log(
        log,
        [
            "2026-01-02T03:04:05Z reconnecting to stream",
            "2026-01-02T03:04:06Z falling back to http",
            "HTTP 429 too many requests",
        ],
    )
    markers, summary = runtime_logs.agent_log_markers(log)
    assert markers == ["reconnecting", "http_fallback", "resource_limit"]
    assert summary != ""

    # since 晚于日志 → 直接判空
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert runtime_logs.agent_log_markers(log, since=future) == ([], "")
    # since 必须早于日志里的时间戳（日志写的是 2026-01-02）才会返回标记
    assert runtime_logs.agent_log_markers(log, since=datetime(2025, 1, 1, tzinfo=timezone.utc))[0] == markers
    assert runtime_logs.agent_log_markers(log, since=datetime(2026, 6, 1, tzinfo=timezone.utc)) == ([], "")


def test_task_id_from_codex_command_line() -> None:
    assert runtime_logs.task_id_from_codex_command_line(r"--cd workspace\runs\node-7\x") == "node-7"
    assert runtime_logs.task_id_from_codex_command_line(r"--cd workspace\worktrees\node-8\x") == "node-8"
    assert runtime_logs.task_id_from_codex_command_line("no path here") == ""
    # 斜杠形式同样识别
    assert runtime_logs.task_id_from_codex_command_line("workspace/runs/node-9/x") == "node-9"


def test_agent_status_from_codex_command_line() -> None:
    assert runtime_logs.agent_status_from_codex_command_line("planner-decision.schema.json") == "planning"
    assert runtime_logs.agent_status_from_codex_command_line("worker-result.schema.json") == "working"
    assert runtime_logs.agent_status_from_codex_command_line("verifier-result.schema.json") == "verifying"
    assert runtime_logs.agent_status_from_codex_command_line("acceptance-result.schema.json") == "accepting"
    assert runtime_logs.agent_status_from_codex_command_line("nothing") is None


def test_is_battlefield_agent_codex_command_line() -> None:
    # 阶段识别要求 `\worker-last-message.json` 这种带分隔符的路径形态
    good = r"--output-last-message C:\tmp\worker-last-message.json"
    assert runtime_logs.is_battlefield_agent_codex_command_line(good) is True
    # 缺 --output-last-message 不算
    assert runtime_logs.is_battlefield_agent_codex_command_line("worker-last-message.json") is False
    # 有参数但认不出阶段也不算（纯名字没有分隔符）
    assert runtime_logs.is_battlefield_agent_codex_command_line("--output-last-message worker-last-message.json") is False


def test_available_memory_mb_returns_positive_or_none() -> None:
    value = runtime_logs.available_memory_mb()
    assert value is None or value > 0


def test_machine_safety_pressure_probe_injection() -> None:
    # 阈值 0 → 直接关闭，且不调用探针
    calls: list[int] = []

    def probe() -> float:
        calls.append(1)
        return 1.0

    assert runtime_logs.machine_safety_pressure(_Settings(), probe) == (False, {"enabled": False})
    assert calls == []

    low = runtime_logs.machine_safety_pressure(_Settings(machine_safety_min_available_memory_mb=4096), probe)
    assert low[0] is True
    assert low[1]["available_memory_mb"] == 1.0
    assert low[1]["safe_min_available_memory_mb"] == 4096

    high = runtime_logs.machine_safety_pressure(
        _Settings(machine_safety_min_available_memory_mb=1024), lambda: 8192.0
    )
    assert high[0] is False

    # 探针失败 → probe_failed 且不判定为压力
    failed = runtime_logs.machine_safety_pressure(
        _Settings(machine_safety_min_available_memory_mb=4096), lambda: None
    )
    assert failed[0] is False
    assert failed[1]["probe_failed"] is True


def test_is_resource_pressure_block_report() -> None:
    assert runtime_logs.is_resource_pressure_block_report({"category": "rate_limit", "retryable": True, "reason": "429"}) is True
    assert runtime_logs.is_resource_pressure_block_report({"category": "human_auth", "retryable": False, "reason": "权限不足"}) is False


def test_active_resource_pressure_from_logs(tmp_path: Path) -> None:
    settings = _Settings(workspace_dir=tmp_path)
    assert runtime_logs.active_resource_pressure_from_logs([], settings) == (False, "")

    _write_log(tmp_path / "runs" / "node-1" / "worker.jsonl", ["HTTP 429 too many requests"])
    active = runtime_logs.active_resource_pressure_from_logs([_task("node-1")], settings)
    assert active == (True, "活跃节点 node-1 日志检测到额度/限流信号。")

    # 非活跃状态不参与判定
    assert runtime_logs.active_resource_pressure_from_logs([_task("node-1", status="queued")], settings) == (False, "")
    # 无日志文件的活跃节点不判定
    assert runtime_logs.active_resource_pressure_from_logs([_task("node-2")], settings) == (False, "")

    # cooldown 语义是「只认比 cooldown 更新的标记」，因此刚写入的日志即使 cooldown 巨大也照样命中
    assert runtime_logs.active_resource_pressure_from_logs(
        [_task("node-1")], _Settings(workspace_dir=tmp_path, auto_concurrency_resource_cooldown_seconds=10**6)
    ) == (True, "活跃节点 node-1 日志检测到额度/限流信号。")

    # 把日志改成「很久以前」后，同样的 cooldown 才会把它过滤掉
    stale = tmp_path / "runs" / "node-1" / "worker.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(stale, (old, old))
    assert runtime_logs.active_resource_pressure_from_logs(
        [_task("node-1")], _Settings(workspace_dir=tmp_path, auto_concurrency_resource_cooldown_seconds=3600)
    ) == (False, "")


def test_resource_pressure_state_without_db() -> None:
    settings = _Settings()
    # 没有任务 → 不触库
    assert runtime_logs.resource_pressure_state(None, [], settings) == (False, "")
    # 阻塞节点的可重试资源上报 → 不触库即返回
    blocked = _task(
        "node-3",
        status="blocked",
        meta={"block_report": {"category": "rate_limit", "retryable": True, "source": "worker.blocked", "reason": "429"}},
    )
    pressure, reason = runtime_logs.resource_pressure_state(None, [blocked], settings)
    assert pressure is True
    assert "node-3" in reason

    # 本地技能失败被排除，且因为没有其它阻塞会走到事件查询 → 用空任务规避
    skill_blocked = _task(
        "node-4",
        status="blocked",
        meta={"block_report": {"category": "rate_limit", "retryable": True, "source": "worker.blocked", "reason": "failed to load skill x"}},
    )
    assert runtime_logs.resource_pressure_state(None, [], settings)[0] is False
    assert skill_blocked is not None


def test_orchestrator_delegates_agree_with_module(tmp_path: Path, monkeypatch) -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings(workspace_dir=tmp_path)

    assert orchestrator._seconds_since(None) == runtime_logs.seconds_since(None)
    assert orchestrator._agent_phase_for_status("verifying") == runtime_logs.agent_phase_for_status("verifying")
    assert orchestrator._codex_log_line_timestamp("2026-01-02T03:04:05Z") == (
        runtime_logs.codex_log_line_timestamp("2026-01-02T03:04:05Z")
    )
    assert orchestrator._runtime_marker_candidate_line("HTTP 429") == runtime_logs.runtime_marker_candidate_line("HTTP 429")
    assert orchestrator._runtime_marker_candidate_lines("a\nb") == runtime_logs.runtime_marker_candidate_lines("a\nb")
    assert orchestrator._task_id_from_codex_command_line(r"workspace\runs\n1") == (
        runtime_logs.task_id_from_codex_command_line(r"workspace\runs\n1")
    )
    assert orchestrator._agent_status_from_codex_command_line("worker-result.schema.json") == (
        runtime_logs.agent_status_from_codex_command_line("worker-result.schema.json")
    )
    assert orchestrator._is_battlefield_agent_codex_command_line("--output-last-message worker-last-message.json") == (
        runtime_logs.is_battlefield_agent_codex_command_line("--output-last-message worker-last-message.json")
    )
    # 这一条不能比"两次实时读数相等"：可用内存一直在动，那不是漂移而是瞬时差。
    # 把模块探针钉住，测的才是"委托真的走了模块"。
    monkeypatch.setattr(runtime_logs, "available_memory_mb", lambda: 4242.5)
    assert orchestrator._available_memory_mb() == 4242.5

    log = tmp_path / "worker.jsonl"
    _write_log(log, ["HTTP 429 too many requests"])
    assert orchestrator._agent_log_markers(log) == runtime_logs.agent_log_markers(log)
    assert orchestrator._latest_meaningful_agent_log_time(log) == runtime_logs.latest_meaningful_agent_log_time(log)
    assert orchestrator._file_mtime_utc(log) == runtime_logs.file_mtime_utc(log)

    tasks = [_task("node-5")]
    assert orchestrator._active_resource_pressure_from_logs(tasks) == (
        runtime_logs.active_resource_pressure_from_logs(tasks, orchestrator.settings)
    )
    assert orchestrator._is_resource_pressure_block_report({"category": "rate_limit", "retryable": True}) == (
        runtime_logs.is_resource_pressure_block_report({"category": "rate_limit", "retryable": True})
    )
    assert orchestrator._machine_safety_pressure() == runtime_logs.machine_safety_pressure(
        orchestrator.settings, orchestrator._available_memory_mb
    )


def test_machine_safety_seam_still_patchable(tmp_path: Path) -> None:
    """搬移后 `_available_memory_mb` 仍是有效接缝：实例级 monkeypatch 必须继续生效。

    这是 W4g-c1 的真实教训——若模块内直接调用 storage 探针，这层接缝会消失，
    现有 5 处 monkeypatch 会静默失效。
    """
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings(workspace_dir=tmp_path, machine_safety_min_available_memory_mb=4096)
    orchestrator._available_memory_mb = lambda: 512.0  # type: ignore[method-assign]

    pressure, payload = orchestrator._machine_safety_pressure()
    assert pressure is True
    assert payload["available_memory_mb"] == 512.0
