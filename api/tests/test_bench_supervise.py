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


def test_py_spy_is_found_next_to_the_interpreter_when_path_has_none(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`py-spy` 装在 venv 里 ≠ 在 `PATH` 里：必须去**跑本脚本的解释器旁边**找。

    实测 2026-09-24：只靠 `shutil.which` 时，取证报告里每个 pid 都写"没有 py-spy，跳过"，
    而机器上装着 0.4.2（支持 `--native`）—— 于是"冻结现场没有栈"这件事纯属自找。
    反面自检：两处都没有时必须返回空串（否则这条断言只是证明"总能找到"）。
    """
    supervisor = _load()
    fake_bin = tmp_path / "Scripts"
    fake_bin.mkdir()
    exe = fake_bin / ("py-spy.exe" if os.name == "nt" else "py-spy")
    exe.write_bytes(b"")
    monkeypatch.setattr(supervisor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(supervisor.sys, "executable", str(fake_bin / "python.exe"))
    assert supervisor.find_py_spy() == str(exe)

    exe.unlink()
    assert supervisor.find_py_spy() == ""


def test_dump_capture_judges_by_files_on_disk_not_by_exit_code(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dump 抓没抓到，只能看**目录里新增了哪些非空 `.dmp`**。

    两个实测的坑（2026-09-24）：
    1. ProcDump **抓成功也返回 exit=1**（"Dump count reached"）→ 看退出码会把成功读成失败；
    2. ProcDump 把文件名里的 `PID` 当**宏**展开：给 `pid-123.dmp` 落成 `123-123.dmp`
       → 只查"我以为的那个路径"会判成"没抓到"，而 dump 就在旁边。
    失效方向：这两条都让**有证据**被读成**没证据**，于是没人去读真的抓到的那份。
    """
    supervisor = _load()
    dump_dir = tmp_path / "dumps"

    def fake_run(command: list[str], *, timeout: float) -> tuple[bool, str, str]:
        # 模拟 ProcDump：换个名字落盘 + 返回非 0
        target = pathlib.Path(command[-1])
        (target.parent / f"{target.stem}-expanded.dmp").write_bytes(b"x" * 16)
        return False, "", "exit=1 输出尾部=Dump count reached."

    monkeypatch.setattr(supervisor, "_run_capture", fake_run)
    state, detail = supervisor._procdump_capture("procdump64.exe", 4321, dump_dir, timeout=5)
    assert state == "ok", "退出码非 0 但文件落了 → 必须算抓到"
    assert "expanded.dmp" in detail and "16 字节" in detail
    # 给 procdump 的文件名里不许带 `pid` 字样（它会展开成实际 PID）
    assert not any(item.name.lower().startswith("pid-") for item in dump_dir.glob("*.dmp"))

    # 反面自检：什么都没落盘时必须判成 failed，否则上面那条只是"永远说 ok"
    monkeypatch.setattr(supervisor, "_run_capture", lambda command, *, timeout: (True, "ok", ""))
    empty_state, empty_detail = supervisor._procdump_capture("procdump64.exe", 4321, tmp_path / "empty", timeout=5)
    assert empty_state == "failed"
    assert "没有新 dump" in empty_detail


def test_missing_tools_and_spent_budget_are_written_down_not_swallowed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取证失败 = **写清楚**，不是安静地少一段。

    最贵的误读方向：证据文件里少一段栈却什么都不说 → 读的人把它当"现场很干净"。
    所以工具没找到、预算用尽都必须在文件里有名有姓；同时取证本身**不许抛**（它一抛就挡住杀）。
    """
    supervisor = _load()
    monkeypatch.setattr(supervisor, "find_py_spy", lambda: "")
    monkeypatch.setattr(supervisor, "find_procdump", lambda: "")
    monkeypatch.setattr(supervisor, "_process_table", lambda: [])
    monkeypatch.setattr(supervisor, "_thread_states", lambda _pid: [])
    out = tmp_path / "evidence.txt"
    lines = supervisor.capture_hang_evidence(999999, out, dump_dir=tmp_path / "dumps", budget_seconds=30)
    text = out.read_text(encoding="utf-8")
    assert "py-spy：未找到" in text and "procdump：未找到" in text
    assert "栈=无 py-spy dump=无 procdump" in text
    assert lines, "返回值也要给出去（调用方要把一览写进监督说明）"

    # 预算为 0：一个 pid 都不取证，但必须**说出来**，而且照样返回（不抛 → 不挡杀）
    spent = tmp_path / "spent.txt"
    supervisor.capture_hang_evidence(999999, spent, dump_dir=tmp_path / "dumps", budget_seconds=0)
    spent_text = spent.read_text(encoding="utf-8")
    assert "取证预算用尽" in spent_text
    assert "999999" in spent_text
