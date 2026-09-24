""""已经决定要停"之后**不许无界等待**的回归测试。

**它存在的理由**（2026-09-24 零 token 确定性复现，`workspace/_d4_repro.py --behaviors orphan`）：
asyncio 的 `Process.wait()` 不是"进程退出就返回"——`BaseSubprocessTransport._try_finish()` 要求
**进程已退出且所有管道都 disconnected**，唤醒 `wait()` 的那步在 `_call_connection_lost` 里。
所以只要有任何一个**后代**进程还攥着 stdout 写端（Windows 上 codex 是 `.cmd` → cmd.exe → node，
node 再起孙进程；`taskkill /T /F` 杀不到已脱离的那个），EOF 永远不来，
`_communicate_with_watchdog` 里原先那句无超时的 `await wait_task` 就一直挂着。

实测现场：空闲看门狗 6s 开火 → 之后**静默 55s**，直到那个孙进程自己退出；
期间进程树里**一个后代都没有**，`py-spy` 显示事件循环健康（`_poll(ms=INFINITE)` = 没有任何定时器）。
这与 2026-09-22 真实档"静默 30-60 分钟、DB 停在 verifying"的现场同形。

判据的失效方向：宁可"提前收手并写明"，也不要"为了一个准确退出码无限挂着"。
所以下面三条一起测：有界（正向）、**正常退出必须给真退出码**（反面自检：不许恒 124）、
真实形状（孤儿持管道）下 `_run_codex_io` 必须有界返回。
"""

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
import subprocess
import sys
import time

import pytest

from app.config import Settings
from app.runner import CodexExecRunner


class _FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, transport: _FakeTransport) -> None:
        self._transport = transport


def _bare_runner() -> CodexExecRunner:
    """只测 `_await_exit_bounded` 这段纯逻辑，不需要真 Settings / 真 codex。"""
    return object.__new__(CodexExecRunner)


@pytest.mark.asyncio
async def test_exit_wait_gives_up_and_closes_our_end(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`process.wait()` 永不完成时：必须**有界**返回 124、写明原因、并关掉本端管道。

    外层再套一个 5s 的 `wait_for`：这样"撒回修复"（改回 `await wait_task`）会得到一条
    **失败**而不是一个挂住的测试——挂住的测试等于没有测试。
    """
    monkeypatch.setattr("app.runner.EXIT_WAIT_AFTER_STOP_SECONDS", 0.2)
    runner = _bare_runner()
    transport = _FakeTransport()
    never: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    wait_task = asyncio.ensure_future(asyncio.shield(never))
    logs_path = tmp_path / "planner.jsonl"
    log_chunks = [b"head\n"]
    try:
        exit_code = await asyncio.wait_for(
            runner._await_exit_bounded(_FakeProcess(transport), wait_task, logs_path, log_chunks, "planner"),
            timeout=5,
        )
    finally:
        never.cancel()
        wait_task.cancel()
    assert exit_code == 124
    assert transport.closed, "放弃等待后必须关掉本端管道，否则每次放弃都漏一副句柄"
    text = logs_path.read_text(encoding="utf-8")
    assert "exit wait gave up" in text and "所有管道 EOF" in text


@pytest.mark.asyncio
async def test_normal_exit_still_reports_the_real_code(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """反面自检：正常退出的子进程必须拿到**真**退出码，日志不许多出放弃说明，管道不许被强关。

    没有这一条，上面那条判据可以靠"永远返回 124"通过——那就把正常路径也一起毁了。
    """
    monkeypatch.setattr("app.runner.EXIT_WAIT_AFTER_STOP_SECONDS", 5.0)
    runner = _bare_runner()
    transport = _FakeTransport()

    async def exits_soon() -> int:
        await asyncio.sleep(0.05)
        return 7

    wait_task = asyncio.ensure_future(exits_soon())
    logs_path = tmp_path / "planner.jsonl"
    exit_code = await runner._await_exit_bounded(_FakeProcess(transport), wait_task, logs_path, [b""], "planner")
    assert exit_code == 7
    assert not transport.closed
    assert not logs_path.exists()


def _count_pipe_holders(stop_flag: pathlib.Path) -> int:
    """数一下"攥着管道的孤儿"还有几个（按命令行里的 stop 标记路径认）。

    端到端用例需要它来把两种失败分开：**现场没搭起来**（孤儿没起来 → 本来就不会卡 → 那条
    用例会恒真）和**修复失效**（孤儿在、却没走放弃路径）。混在一起的话，前者会被读成"通过"。
    """
    listing = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{stop_flag.name}*' }}).Count",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [line.strip() for line in (listing.stdout or "").splitlines() if line.strip()]
    return int(lines[-1]) if lines and lines[-1].isdigit() else 0


FAKE_CODEX = '''\
import os, subprocess, sys, time
sys.stdin.buffer.read()
hold = sys.argv[1]
# 孙进程继承 stdout 并**脱离**父进程：父与 cmd.exe 都退干净了，管道写端却还在它手上
subprocess.Popen(
    [sys.executable, "-c",
     "import os,time,sys\\nstop=sys.argv[1]\\nfor _ in range(600):\\n    if os.path.exists(stop): break\\n    time.sleep(0.2)",
     hold],
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    close_fds=False,
)
print("fake codex line", flush=True)
'''


@pytest.mark.skipif(sys.platform != "win32", reason="脱离式孙进程持管道是 Windows 形状（codex 是 .cmd）")
@pytest.mark.asyncio
async def test_run_codex_io_returns_even_when_an_orphan_holds_the_pipe(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实形状端到端：假 codex 留下一个攥着 stdout 的孤儿 → `_run_codex_io` 仍须**有界**返回。

    形状照真实通道抄：`.cmd` 转发器（`_resolve_codex_executable` 解析出来的就是 `.cmd`）→ cmd.exe →
    python → 脱离的孙进程。修复前这里会一直等到孙进程自己退出（实测 55s+，真实档是 30-60 分钟）。
    """
    monkeypatch.setattr("app.runner.EXIT_WAIT_AFTER_STOP_SECONDS", 1.0)
    stop_flag = tmp_path / "stop.txt"
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    forwarder = tmp_path / "fake_codex.cmd"
    forwarder.write_text(f'@echo off\r\n"{sys.executable}" "{script}" "{stop_flag}" %*\r\n', encoding="utf-8")

    settings = dataclasses.replace(
        Settings(),
        runner_mode="codex-exec",
        codex_bin=str(forwarder),
        workspace_dir=tmp_path / "ws",
        codex_timeout_seconds=0,
        codex_idle_timeout_seconds=1,
        codex_retry_attempts=0,
    )
    runner = CodexExecRunner(settings)
    started = time.monotonic()
    try:
        envelope = await asyncio.wait_for(
            runner._run_codex_io(
                run_id="orphan-pipe",
                cwd=tmp_path,
                kind="planner",
                prompt="x" * 4096,
                schema_name="planner-decision.schema.json",
                sandbox="read-only",
            ),
            timeout=60,
        )
        elapsed = time.monotonic() - started
        holders = _count_pipe_holders(stop_flag)  # **写 stop 标记之前**数，否则孤儿早没了，什么都证明不了
    finally:
        stop_flag.write_text("stop", encoding="utf-8")  # 让孤儿立刻收工，别给别的用例留个占管道的进程
    assert holders >= 1, "返回时孤儿必须还活着；它不在就说明现场没搭起来（这条用例会恒真）"
    assert envelope.exit_code == 124, "看门狗开火后应按超时收尾"
    assert elapsed < 30, f"必须有界返回，实测 {elapsed:.1f}s（修复前会等到孤儿自己退出）"
    logs = pathlib.Path(envelope.logs_path).read_text(encoding="utf-8", errors="replace")
    assert "idle watchdog" in logs, logs[-800:]
    assert "exit wait gave up" in logs, logs[-800:]


@pytest.mark.asyncio
async def test_second_chance_after_closing_the_pipe_yields_the_real_code(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉本端管道之后 `wait()` 才解开时，必须拿到**真**退出码（这里 3），不是 124。

    这条专门钉住"第二次机会用 `asyncio.wait` 而不是 `wait_for`"：`wait_for` 超时会把
    `wait_task` **取消**，于是关管道解开的那个退出码永远取不到 → 判据恒定返回 124，
    "关了管道还是拿不到码"与"真的拿不到"再也分不开。
    """
    monkeypatch.setattr("app.runner.EXIT_WAIT_AFTER_STOP_SECONDS", 0.2)
    runner = _bare_runner()
    resolved: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    class _ClosingTransport(_FakeTransport):
        def close(self) -> None:
            super().close()
            if not resolved.done():
                resolved.set_result(3)

    transport = _ClosingTransport()
    wait_task = asyncio.ensure_future(asyncio.shield(resolved))
    exit_code = await asyncio.wait_for(
        runner._await_exit_bounded(_FakeProcess(transport), wait_task, tmp_path / "planner.jsonl", [b""], "planner"),
        timeout=5,
    )
    assert transport.closed
    assert exit_code == 3


def test_orphan_holder_script_really_outlives_its_parent(tmp_path: pathlib.Path) -> None:
    """量具自检：上面那个"孤儿"必须真的活过父进程，否则整条用例在测一个不存在的现场。

    失效方向：如果孙进程其实随父进程一起死了，端到端那条用例会**恒真**（它本来就不会卡）。

    注意这里**不能**用 `capture_output=True`：`subprocess.run` 收管道时也要等 stdout EOF，
    会被同一个孤儿卡住（第一版就这么挂了 60s）。所以把 stdout 重定向到文件，只等父进程退出。
    """
    if sys.platform != "win32":
        pytest.skip("Windows 形状")
    stop_flag = tmp_path / "stop.txt"
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    out_path = tmp_path / "out.txt"
    started = time.monotonic()
    with out_path.open("wb") as sink:
        completed = subprocess.run(
            [sys.executable, str(script), str(stop_flag)],
            input=b"",
            stdout=sink,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
    parent_elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert parent_elapsed < 30, "父进程本身应立刻退出"
    alive = _count_pipe_holders(stop_flag)
    stop_flag.write_text("stop", encoding="utf-8")
    assert alive >= 1, f"孙进程没能活过父进程（读到 {alive}）→ 端到端用例会恒真"

