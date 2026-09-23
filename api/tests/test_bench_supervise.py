"""外部监督器（`scripts/bench_supervise.py`）的回归测试。

**它存在的理由**：2026-09-22 真实档跑 unsolvable 任务时两次冻住，进程内兜底全部失效
（心跳 / CLI 看门狗 / `--task-timeout` / `--hard-stop` 线程都没执行），只剩"外部杀"这一条路。
所以这里要钉住：进度判据的正反两面 + 真的能杀掉一个卡住的子进程树。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUPERVISOR = ROOT / "scripts" / "bench_supervise.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("bench_supervise_under_test", SUPERVISOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_supervise_decision_reports_running_while_report_is_fresh(tmp_path: pathlib.Path) -> None:
    """进度在动（心跳一直在重写报告）→ 绝不判卡住。反面自检：这条判据不许恒真。"""
    supervisor = _load()
    report = tmp_path / "r.json"
    report.write_text("{}", encoding="utf-8")
    fresh = report.stat().st_mtime
    decision = supervisor.supervise_decision(
        report, child_started=fresh - 10, now=fresh + 1, stall_seconds=420, max_seconds=0, alive=True
    )
    assert decision == "running"


def test_supervise_decision_uses_child_start_when_report_missing(tmp_path: pathlib.Path) -> None:
    """报告还没落盘时用子进程启动时刻当参考——否则"刚开跑"会被误判成卡住。"""
    supervisor = _load()
    missing = tmp_path / "never-written.json"
    started = time.time() - 30
    assert (
        supervisor.supervise_decision(missing, child_started=started, now=time.time(), stall_seconds=420, max_seconds=0, alive=True)
        == "running"
    )
    assert (
        supervisor.supervise_decision(missing, child_started=time.time() - 999, now=time.time(), stall_seconds=420, max_seconds=0, alive=True)
        == "stalled"
    )


def test_supervise_decision_covers_stall_max_seconds_and_exit(tmp_path: pathlib.Path) -> None:
    """三条边界各自单独测：停滞、总时长、子进程已退出（退出必须优先，正常路径不能被判成卡住）。"""
    supervisor = _load()
    report = tmp_path / "r.json"
    report.write_text("{}", encoding="utf-8")
    # 真把文件时间戳做旧（不能用"child_started 很旧"来模拟：参考值是 max(mtime, child_started)，
    # 新报告永远赢——这正是"进度在动就别杀"的语义）
    stale_at = time.time() - 1000
    os.utime(report, (stale_at, stale_at))
    assert (
        supervisor.supervise_decision(report, child_started=stale_at - 100, now=time.time(), stall_seconds=420, max_seconds=0, alive=True)
        == "stalled"
    )
    assert (
        supervisor.supervise_decision(report, child_started=time.time() - 100, now=time.time(), stall_seconds=0, max_seconds=60, alive=True)
        == "max_seconds"
    )
    assert (
        supervisor.supervise_decision(report, child_started=stale_at, now=time.time(), stall_seconds=1, max_seconds=1, alive=False)
        == "exited"
    )


@pytest.mark.skipif(sys.platform != "win32" and sys.platform != "linux", reason="需要能杀进程树")
def test_supervisor_kills_a_stalled_child_and_writes_a_note(tmp_path: pathlib.Path) -> None:
    """端到端：子进程不写报告也不退出 → 监督器杀掉它、落一份说明、退出码 4。

    用"睡 60 秒的 python"当被测子进程：它永远不会写报告，正好像一次解释器级冻结。
    """
    report = tmp_path / "stalled.json"
    started = time.time()
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--report",
            str(report),
            "--stall-seconds",
            "2",
            "--poll-seconds",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert time.time() - started < 60, "卡住的子进程必须在阈值后被提前杀掉，而不是等它自己结束"
    note = pathlib.Path(str(report) + ".supervisor.txt")
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert "杀" in text and "卡住" in text


def test_descendant_pids_walks_the_whole_tree() -> None:
    """要 dump 的是**整棵**进程树：跑批进程是 venv 转发器，真跑事件循环的是它的子进程。"""
    supervisor = _load()
    table = [(1, 0), (2, 1), (3, 2), (4, 2), (5, 0)]
    assert sorted(supervisor.descendant_pids(table, 1)) == [2, 3, 4]
    assert supervisor.descendant_pids(table, 5) == []
    # 脏数据成环时不许转不出来
    assert sorted(supervisor.descendant_pids([(1, 2), (2, 1)], 1)) == [2]


@pytest.mark.skipif(sys.platform != "win32" and sys.platform != "linux", reason="需要能杀进程树")
def test_supervisor_captures_hang_evidence_before_killing(tmp_path: pathlib.Path) -> None:
    """判停时必须**先落现场证据**再杀：冻住时进程内一切定时器都不执行，能问的只有进程外。"""
    report = tmp_path / "stalled.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--report",
            str(report),
            "--stall-seconds",
            "2",
            "--poll-seconds",
            "1",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 4, result.stdout + result.stderr
    evidence = pathlib.Path(str(report) + ".hang-evidence.txt")
    assert evidence.is_file()
    text = evidence.read_text(encoding="utf-8")
    assert "冻结现场取证" in text
    assert "py-spy" in text and "线程等待态" in text
    sidecar = pathlib.Path(str(report) + ".supervisor.txt").read_text(encoding="utf-8")
    assert "冻结现场证据" in sidecar


def test_supervisor_forwards_child_exit_code(tmp_path: pathlib.Path) -> None:
    """正常路径语义不变：子进程退出码 0/非 0 原样转发（监督器不解释跑批的结论）。"""
    report = tmp_path / "ok.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SUPERVISOR),
            "--report",
            str(report),
            "--stall-seconds",
            "5",
            "--poll-seconds",
            "1",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(3)",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 3
    assert not pathlib.Path(str(report) + ".supervisor.txt").exists()
