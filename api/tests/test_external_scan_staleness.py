"""看板统计那条路**不许**把 WMI 扫描拽回来（P3-6 首屏的抖动根因）。

真实事故（2026-10-18，同一条首屏判据连着跑）：

    TTFB `412ms`（缓存热）vs `979 / 1223ms`（请求正好命中一次进程扫描）

原因：`_codex_slots_active` 的缓存 TTL 是 **5 秒**，而后台刷新是 **15 秒**一次 ——
于是每 15 秒里有一半时间缓存"过期"，`/api/tree` 就会**同步**去跑一次
`Get-CimInstance … WHERE Name='codex.exe'`（实测每次 ~0.7 秒）。

这组用例钉住三件事：

1. 缓存 10 秒（比旧 TTL 老、比新 TTL 新）时，统计**复用缓存**、不扫描；
2. 缓存 30 秒（超过 20 秒窗口）时，**仍然会扫描** —— 证明它是"放宽陈旧度"，不是"永不刷新"；
3. 真正决定"能不能起 agent"的那条路（`_hold_agent_start_for_external_codex_slots`）
   **不受影响**：缓存再新它也会现场扫（`max_cache_age_seconds=0.5`）。

变异：把默认值改回 `5.0` → S1 立刻红；把第 3 条改成读缓存 → S3 红。
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from app.config import get_settings
from app.models import utcnow
from app.orchestrator import Orchestrator


def _orchestrator(tmp_path) -> Orchestrator:
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'staleness.sqlite3').as_posix()}",
        runner_mode="mock",
        mock_delay_seconds=0,
        max_concurrency=1,
    )
    return Orchestrator(settings)


def _prime_cache(orchestrator: Orchestrator, age_seconds: float, rows: list[dict] | None = None) -> None:
    orchestrator._external_codex_process_cache = (
        utcnow() - timedelta(seconds=age_seconds),
        rows if rows is not None else [{"pid": 4242, "task_id": "node-x", "status": "working"}],
    )


@pytest.fixture()
def scan_counter(monkeypatch):
    calls: list[float] = []

    def fake_rows(self):
        calls.append(time.monotonic())
        return [{"ProcessId": 4242, "CommandLine": "codex.exe exec --cd <workspace>"}]

    monkeypatch.setattr(Orchestrator, "_codex_process_rows", fake_rows)
    return calls


def test_s1_stats_reuse_a_ten_second_old_cache_without_scanning(tmp_path, scan_counter) -> None:
    orchestrator = _orchestrator(tmp_path)
    _prime_cache(orchestrator, age_seconds=10.0)

    assert orchestrator._codex_slots_active() == 1
    assert scan_counter == [], "统计口径不该为了一个看板数字同步扫 WMI（那正是 ~0.7 秒的来源）"


def test_s2_stale_beyond_the_window_still_refreshes(tmp_path, scan_counter) -> None:
    orchestrator = _orchestrator(tmp_path)
    _prime_cache(orchestrator, age_seconds=30.0)

    orchestrator._codex_slots_active()

    assert len(scan_counter) == 1, "超过 20 秒窗口就该刷新 —— 放宽陈旧度不等于永不刷新"


def test_s3_agent_start_gate_keeps_scanning_fresh(tmp_path, scan_counter) -> None:
    """调度决策不能吃 20 秒的陈旧度：它只容忍 0.5 秒，超了就现场扫（否则会起重）。"""

    orchestrator = _orchestrator(tmp_path)
    # 1.0 秒 > 0.5 秒窗口 —— 这里必须真的去扫，而不是拿这份 1 秒前的快照做决定
    _prime_cache(orchestrator, age_seconds=1.0, rows=[])

    held = orchestrator._hold_agent_start_for_external_codex_slots("node-y", "planner")

    assert scan_counter, "起 agent 之前必须现场看一下有没有外部 codex 在跑"
    assert held is False, "没有外部进程时不该拦住启动"
