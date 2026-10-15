from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import watchdog
from app.config import ROOT_DIR
from app.models import TaskNode
from app.orchestrator import Orchestrator


def _task(
    task_id: str,
    *,
    parent_id: str | None = None,
    status: str = "running",
    created_at: datetime | None = None,
) -> TaskNode:
    return TaskNode(
        id=task_id,
        parent_id=parent_id,
        title=task_id,
        status=status,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_latest_root_id_prefers_active_over_terminal() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        _task("root-done", status="done", created_at=base + timedelta(hours=5)),
        _task("root-live", status="running", created_at=base),
        _task("child", parent_id="root-live"),
    ]
    assert watchdog.latest_root_id(tasks) == "root-live"


def test_latest_root_id_skips_debug_ids() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        _task("debug-root", status="running", created_at=base + timedelta(hours=5)),
        _task("root-live", status="running", created_at=base),
    ]
    assert watchdog.latest_root_id(tasks) == "root-live"


def test_latest_root_id_falls_back_to_terminal_when_all_terminal() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [
        _task("root-old", status="done", created_at=base),
        _task("root-new", status="blocked", created_at=base + timedelta(hours=1)),
    ]
    assert watchdog.latest_root_id(tasks) == "root-new"


def test_latest_root_id_handles_no_roots() -> None:
    assert watchdog.latest_root_id([]) is None
    assert watchdog.latest_root_id([_task("child", parent_id="missing")]) is None


def test_select_watchdog_root_requested_paths() -> None:
    tasks = [_task("root-1", status="running"), _task("root-2", status="done")]
    assert watchdog.select_watchdog_root(tasks, "root-1") == ("root-1", "requested")
    assert watchdog.select_watchdog_root(tasks, "root-2") == ("root-2", "requested_terminal")
    assert watchdog.select_watchdog_root(tasks, "nope") == (None, "requested_missing")
    # 只有根节点能被选中：子节点 id 即便存在也视为 missing
    child = _task("child-1", parent_id="root-1")
    assert watchdog.select_watchdog_root([*tasks, child], "child-1") == (None, "requested_missing")


def test_select_watchdog_root_auto_paths() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [_task("root-1", created_at=base), _task("root-2", created_at=base + timedelta(hours=1))]
    assert watchdog.select_watchdog_root(tasks, None) == ("root-2", "auto_latest")
    assert watchdog.select_watchdog_root([], None) == (None, "none")


def test_resume_status_from_hibernation_preserves_waiting_children() -> None:
    assert watchdog.resume_status_from_hibernation({"previous_status": "waiting_children"}) == "waiting_children"
    assert watchdog.resume_status_from_hibernation({"previous_status": "running"}) == "queued"
    assert watchdog.resume_status_from_hibernation({}) == "queued"


def test_retry_after_seconds_from_reports_clamps() -> None:
    assert watchdog.retry_after_seconds_from_reports([], 120) == 120
    # 下限 60：即使报告里写的是 10 秒
    assert watchdog.retry_after_seconds_from_reports([{"retry_after_seconds": 10}], 120) == 60
    # 取所有正值里的最大值，quota_window_seconds 与 retry_after_seconds 等价参与
    assert (
        watchdog.retry_after_seconds_from_reports(
            [{"retry_after_seconds": 300}, {"quota_window_seconds": 900}],
            120,
        )
        == 900
    )
    # 上限 7 天
    assert watchdog.retry_after_seconds_from_reports([{"retry_after_seconds": 10**9}], 120) == 604_800
    # 非法值被忽略，退回 fallback
    assert watchdog.retry_after_seconds_from_reports([{"retry_after_seconds": "abc"}, {"retry_after_seconds": -5}], 180) == 180
    assert watchdog.retry_after_seconds_from_reports([{"retry_after_seconds": "600"}], 120) == 600


def test_resource_auto_hibernate_reason_formats_wait_text() -> None:
    hours = watchdog.resource_auto_hibernate_reason([{"title": "节点甲", "retry_after_seconds": 7200}], 120)
    assert "约 2.0 小时" in hours
    assert "节点甲" in hours
    # 只有 1 条报告时不加"等"后缀
    assert "节点甲。" in hours

    minutes = watchdog.resource_auto_hibernate_reason([{"retry_after_seconds": 300}], 120)
    assert "约 5 分钟" in minutes

    # 秒分支实际不可达：时长经 retry_after_seconds_from_reports 已被夹到 >= 60。
    # 这里锁定现状（60 秒走分钟分支），而不是改写语义。
    clamped = watchdog.resource_auto_hibernate_reason([{"retry_after_seconds": 59}], 120)
    assert "约 1 分钟" in clamped


def test_resource_auto_hibernate_reason_truncates_and_suffixes() -> None:
    reports = [{"title": f"节点{i}"} for i in range(4)]
    reason = watchdog.resource_auto_hibernate_reason(reports, 120)
    assert "节点0" in reason and "节点2" in reason
    assert "节点3" not in reason
    assert "等" in reason

    unnamed = watchdog.resource_auto_hibernate_reason([{"task_id": "node-9"}], 120)
    assert "node-9" in unnamed
    assert "未知节点" in watchdog.resource_auto_hibernate_reason([], 120)


def test_is_unreadable_reason() -> None:
    assert watchdog.is_unreadable_reason("") is True
    assert watchdog.is_unreadable_reason("   ") is True
    assert watchdog.is_unreadable_reason("一切正常") is False
    # 少于 4 个问号不算损坏
    assert watchdog.is_unreadable_reason("ok?") is False
    assert watchdog.is_unreadable_reason("????????") is True
    # 有 CJK 就不算损坏
    assert watchdog.is_unreadable_reason("????正常????") is False


def test_fallback_hibernation_reason_mentions_budget_when_exhausted() -> None:
    exhausted = watchdog.fallback_hibernation_reason(5, 5, 1)
    assert "5/5" in exhausted
    assert "额外保留 1 次预算" in exhausted
    # 未耗尽时给通用文案，且不得声称额度用尽
    generic = watchdog.fallback_hibernation_reason(1, 5, 1)
    assert "额度" not in generic
    assert "自动恢复" in generic
    # 文案打印的是 used/work_limit 原值，不做截断
    assert "9/5" in watchdog.fallback_hibernation_reason(9, 5, 1)


def test_normalize_watchdog_reason_passes_readable_and_repairs_unreadable() -> None:
    assert watchdog.normalize_watchdog_reason("正常原因", 1, 5, 1) == "正常原因"
    repaired = watchdog.normalize_watchdog_reason("????????", 5, 5, 1)
    assert "5/5" in repaired
    # 空白原因同样视为不可读
    assert "5/5" in watchdog.normalize_watchdog_reason("", 5, 5, 1)


def test_hibernation_reason_is_codex_resource_wait() -> None:
    assert watchdog.hibernation_reason_is_codex_resource_wait("") is False
    assert watchdog.hibernation_reason_is_codex_resource_wait("Codex quota exceeded") is True
    assert watchdog.hibernation_reason_is_codex_resource_wait("额度不足，等待刷新") is True
    assert watchdog.hibernation_reason_is_codex_resource_wait("全部通过，无阻塞") is False
    # 机器安全守卫不是 Codex 资源问题，必须排除
    assert watchdog.hibernation_reason_is_codex_resource_wait("Machine safety guard recovered: quota ok") is False


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.resource_block_resume_after_seconds = 120
        self.workspace_dir = "."
        self.watchdog_interval_minutes = 10
        self.agent_burst_resume_after_seconds = 900
        self.goal_mode = False
        self.codex_home = ""
        self.codex_profile = ""
        self.max_concurrency = 4
        self.agent_burst_limit = 0
        self.agent_burst_reserve = 0
        self.auto_concurrency_queue_threshold = 3
        self.auto_concurrency_fuel_deadline_hour = 6
        self.auto_concurrency_fuel_timezone_offset_hours = 8
        self.auto_concurrency_fuel_sprint_window_minutes = 45
        for key, value in overrides.items():
            setattr(self, key, value)


def test_orchestrator_delegates_agree_with_module() -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings()

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tasks = [_task("root-1", created_at=base), _task("root-2", created_at=base + timedelta(hours=1))]

    assert orchestrator._latest_root_id(tasks) == watchdog.latest_root_id(tasks)
    assert orchestrator._select_watchdog_root(tasks, None) == watchdog.select_watchdog_root(tasks, None)
    assert orchestrator._resume_status_from_hibernation({"previous_status": "running"}) == (
        watchdog.resume_status_from_hibernation({"previous_status": "running"})
    )
    assert orchestrator._retry_after_seconds_from_reports([{"retry_after_seconds": 600}]) == (
        watchdog.retry_after_seconds_from_reports([{"retry_after_seconds": 600}], 120)
    )
    assert orchestrator._resource_auto_hibernate_reason([{"retry_after_seconds": 7200}]) == (
        watchdog.resource_auto_hibernate_reason([{"retry_after_seconds": 7200}], 120)
    )
    assert orchestrator._is_unreadable_reason("????????") == watchdog.is_unreadable_reason("????????")
    assert orchestrator._fallback_hibernation_reason(5, 5, 1) == watchdog.fallback_hibernation_reason(5, 5, 1)
    assert orchestrator._normalize_watchdog_reason("????????", 5, 5, 1) == (
        watchdog.normalize_watchdog_reason("????????", 5, 5, 1)
    )
    assert orchestrator._hibernation_reason_is_codex_resource_wait("Codex quota") == (
        watchdog.hibernation_reason_is_codex_resource_wait("Codex quota")
    )


def test_watchdog_interval_minutes_has_floor_of_one() -> None:
    assert watchdog.watchdog_interval_minutes(_Settings(watchdog_interval_minutes=10)) == 10
    assert watchdog.watchdog_interval_minutes(_Settings(watchdog_interval_minutes=0)) == 1
    assert watchdog.watchdog_interval_minutes(_Settings(watchdog_interval_minutes=-5)) == 1


def test_api_restart_flag_paths(tmp_path: Path) -> None:
    assert watchdog.api_restart_flag_path(tmp_path) == tmp_path / "restart_api_when_idle.flag"
    assert watchdog.api_restart_drain_flag_path(tmp_path) == tmp_path / "restart_api_drain_when_safe.flag"


def test_codex_health_summary_text_maps_known_codes() -> None:
    assert watchdog.codex_health_summary_text("codex_command_missing").startswith("找不到 codex 命令")
    assert watchdog.codex_health_summary_text("codex_quota_reset_window_elapsed").startswith("The last observed")
    # 未知码原样返回（经由 mojibake 修复）
    assert watchdog.codex_health_summary_text("raw_free_text") == "raw_free_text"
    assert watchdog.codex_health_summary_text(None) == ""


def test_codex_health_state_without_file(tmp_path: Path) -> None:
    state = watchdog.codex_health_state(tmp_path)
    assert state["codex_health_exists"] is False
    assert state["codex_health_status"] == "unknown"
    assert state["codex_health_checked_at"] is None
    assert state["codex_quota_retry_after_seconds"] is None
    assert state["codex_quota_observability"] == "codex_cli_has_no_direct_quota_or_5h_reset_api"


def test_codex_health_state_handles_broken_payloads(tmp_path: Path) -> None:
    health = tmp_path / "codex_health.json"

    health.write_text("{not json", encoding="utf-8")
    broken = watchdog.codex_health_state(tmp_path)
    assert broken["codex_health_status"] == "unreadable"
    assert "读取失败" in broken["codex_health_summary"]

    health.write_text("[1, 2, 3]", encoding="utf-8")
    listing = watchdog.codex_health_state(tmp_path)
    assert listing["codex_health_status"] == "invalid"
    assert listing["codex_health_summary"] == "Codex 预检状态文件不是 JSON 对象。"


def test_codex_health_state_uses_quota_reset_window(tmp_path: Path) -> None:
    health = tmp_path / "codex_health.json"

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    health.write_text(
        json.dumps(
            {
                "status": "resource_block",
                "quota_reset_at": future,
                "summary": "codex_runtime_retry_after_observed",
                "quota_signal": "limit",
            }
        ),
        encoding="utf-8",
    )
    blocked = watchdog.codex_health_state(tmp_path)
    assert blocked["codex_health_status"] == "resource_block"
    assert blocked["codex_health_checked_at"] is None
    assert blocked["codex_quota_reset_at"] is not None
    assert 0 < blocked["codex_quota_retry_after_seconds"] <= 7200
    assert blocked["codex_quota_signal"] == "limit"

    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    health.write_text(json.dumps({"status": "resource_block", "quota_reset_at": past}), encoding="utf-8")
    elapsed = watchdog.codex_health_state(tmp_path)
    # 观察窗口已过 → 降级为 warning、retry_after 归零、observability 改写
    assert elapsed["codex_health_status"] == "warning"
    assert elapsed["codex_quota_retry_after_seconds"] == 0
    assert elapsed["codex_quota_observability"] == "retry_after_elapsed_from_codex_output"
    assert elapsed["codex_health_summary"].startswith("The last observed")


def test_codex_health_state_ignores_unparseable_timestamps(tmp_path: Path) -> None:
    (tmp_path / "codex_health.json").write_text(
        json.dumps({"status": "ok", "checked_at": "not-a-time", "quota_reset_at": "not-a-time"}),
        encoding="utf-8",
    )
    state = watchdog.codex_health_state(tmp_path)
    assert state["codex_health_status"] == "ok"
    assert state["codex_health_checked_at"] is None
    assert state["codex_quota_reset_at"] is None


def test_watchdog_hook_state_wires_commands_and_health(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    settings = _Settings(workspace_dir=tmp_path, codex_home="C:/codex-home", codex_profile="hub", agent_burst_limit=6, agent_burst_reserve=1)

    state = watchdog.watchdog_hook_state(tmp_path, settings, "root-9")

    assert state["watchdog_interval_minutes"] == 10
    assert state["scheduled_task_name"] == "CodexBattlefieldWatchdog"
    assert state["logon_task_name"] == "CodexBattlefieldWatchdog-Logon"
    assert ' -RootId "root-9"' in state["watchdog_run_command"]
    assert " -CodexHome \"C:/codex-home\"" in state["watchdog_run_command"]
    assert ' -CodexProfile "hub"' in state["watchdog_run_command"]
    assert " -IntervalMinutes 10" in state["watchdog_install_command"]
    assert state["watchdog_install_command"].endswith(" -WithLogonTrigger")
    assert state["api_restart_pending"] is False
    assert state["api_restart_drain_armed"] is False
    assert state["startup_hook_mode"] == "not_installed"
    assert state["logon_task_exists"] is False
    # 健康状态字段被展开到同一层
    assert state["codex_health_status"] == "unknown"
    assert state["codex_health_path"] == str(tmp_path / "codex_health.json")
    assert "本轮最多启动 5 个真实 Codex agent" in state["token_reserve_summary"]


def test_watchdog_hook_state_detects_installed_hooks(tmp_path: Path, monkeypatch) -> None:
    system_root = tmp_path / "win"
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv("APPDATA", str(appdata))
    tasks_dir = system_root / "System32" / "Tasks"
    tasks_dir.mkdir(parents=True)
    (tasks_dir / "CodexBattlefieldWatchdog").write_text("x", encoding="utf-8")

    state = watchdog.watchdog_hook_state(tmp_path, _Settings(workspace_dir=tmp_path), None)
    assert state["scheduled_task_exists"] is True
    assert state["logon_task_exists"] is False
    # 未装登录任务时落入 startup 文件夹判定
    assert state["startup_hook_mode"] == "not_installed"
    assert " -RootId" not in state["watchdog_run_command"]

    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    (startup / "CodexBattlefieldWatchdog-Logon.cmd").write_text("x", encoding="utf-8")
    assert watchdog.watchdog_hook_state(tmp_path, _Settings(workspace_dir=tmp_path), None)["startup_hook_mode"] == "startup_folder"

    (tasks_dir / "CodexBattlefieldWatchdog-Logon").write_text("x", encoding="utf-8")
    assert watchdog.watchdog_hook_state(tmp_path, _Settings(workspace_dir=tmp_path), None)["startup_hook_mode"] == "scheduled_task"


def test_watchdog_hook_state_reserve_summary_variants(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    goal = watchdog.watchdog_hook_state(tmp_path, _Settings(workspace_dir=tmp_path, goal_mode=True), None)
    assert goal["token_reserve_summary"].startswith("Goal 长跑模式已开启")
    assert " -GoalMode" in goal["watchdog_run_command"]

    none = watchdog.watchdog_hook_state(tmp_path, _Settings(workspace_dir=tmp_path), None)
    assert none["token_reserve_summary"].startswith("未启用 agent 启动预算上限")


def test_orchestrator_hook_delegates_agree_with_module(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SystemRoot", str(tmp_path / "win"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings(workspace_dir=tmp_path)

    assert orchestrator._watchdog_interval_minutes() == watchdog.watchdog_interval_minutes(orchestrator.settings)
    assert orchestrator._api_restart_flag_path() == watchdog.api_restart_flag_path(tmp_path)
    assert orchestrator._api_restart_drain_flag_path() == watchdog.api_restart_drain_flag_path(tmp_path)
    assert orchestrator._codex_health_state() == watchdog.codex_health_state(tmp_path)
    assert orchestrator._codex_health_summary_text("codex_command_missing") == (
        watchdog.codex_health_summary_text("codex_command_missing")
    )
    assert orchestrator._watchdog_hook_state("root-1") == watchdog.watchdog_hook_state(
        ROOT_DIR, orchestrator.settings, "root-1"
    )
