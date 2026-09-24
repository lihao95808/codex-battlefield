"""进程外在的跑批监督器：**防"整个解释器冻住"**。

为什么不能只靠进程内的兜底（实测 2026-09-22，两次复现）：真实档跑 unsolvable 任务时，跑批进程的
4 个线程全部 parked、CPU 停止增长、`faulthandler` 的 dump 线程同时停摆、连普通线程的定时
`Event.wait` 都不返回 —— 也就是**整个解释器冻住**。这种情况下进程内任何兜底都不可靠
（60s 心跳 / 600s CLI 看门狗 / `--task-timeout` / `--hard-stop` 线程全都没执行），只能靠**外部**杀。
这个脚本就是那个"外部"。

用法（`--report` 与 `bench_run.py` 的 `--stamp/--provider` 必须指向同一份文件）：

```powershell
api\\.venv\\Scripts\\python.exe scripts\\bench_supervise.py ^
  --report bench\\reports\\20260922-real-fg5-real.json --stall-seconds 420 --max-seconds 3600 ^
  -- api\\.venv\\Scripts\\python.exe scripts\\bench_run.py --provider real --tasks fix-subtract ^
     --stamp 20260922-real-fg5 --heartbeat 60 --hard-stop 900
```

判据（失效方向写明）：
- **报告文件 mtime 就是进度证据**：跑批每 60s 心跳都会重写它（`bench_run.py` 的 heartbeat），
  所以"mtime 长时间不动"= 跑批没在推进。
- 报告还不存在时，用**子进程启动时刻**当参考，否则"刚开始、还没落盘"会被误判成卡住。
- 无进展超过 `--stall-seconds`，或总时长超过 `--max-seconds` → **杀掉子进程树 + 落监督说明
  + 退出码 4**（方向：宁可停下来让人看，也不要无限挂着占着 CLI 槽位）。
- 子进程正常结束 → **原样转发它的退出码**（监督器不改变正常路径的语义，也不解释它的结论）。
- 子进程的 stdout/stderr **不接管**（继承本进程），这样 `> log 2>&1` 照旧能拿到跑批自己的输出，
  包括 `faulthandler` 的栈 dump。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time

def progress_reference(report_path: pathlib.Path, *, child_started: float) -> float:
    """进度参考时刻：报告存在就用它的 mtime，否则退回子进程启动时刻。"""
    try:
        return max(report_path.stat().st_mtime, child_started)
    except OSError:
        return child_started


def supervise_decision(
    report_path: pathlib.Path,
    *,
    child_started: float,
    now: float,
    stall_seconds: float,
    max_seconds: float,
    alive: bool,
) -> str:
    """返回 `exited` / `max_seconds` / `stalled` / `running`。

    反面自检：这两个"该杀"的判据会不会恒真？——不会：报告文件在跑（心跳每 60s 重写）时
    `now - reference` 很小；子进程已退出时第一分支直接短路，正常路径绝不会被判成卡住。
    """
    if not alive:
        return "exited"
    if max_seconds > 0 and now - child_started > max_seconds:
        return "max_seconds"
    if stall_seconds > 0 and now - progress_reference(report_path, child_started=child_started) > stall_seconds:
        return "stalled"
    return "running"


def terminate_tree(pid: int) -> str:
    """杀**整棵**子进程树（codex CLI 是孙进程，只杀直接子进程会留下孤儿）。"""
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return f"taskkill /T /F {pid} → exit={result.returncode}"
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return f"killpg {pid} → SIGKILL"
    except OSError as exc:
        return f"killpg {pid} 失败：{type(exc).__name__}: {exc}"


def descendant_pids(process_table: list[tuple[int, int]], root_pid: int) -> list[int]:
    """纯函数：从 `(pid, ppid)` 表里取出 root 的所有后代（含多层）。

    为什么要这一层：跑批进程是 venv 的转发器，**真正在跑事件循环的是它的子进程**
    （实测：`api\\.venv\\Scripts\\python.exe` → 基础解释器），只 dump 直接子进程拿不到现场。
    """
    by_parent: dict[int, list[int]] = {}
    for pid, ppid in process_table:
        by_parent.setdefault(ppid, []).append(pid)
    result: list[int] = []
    stack = list(by_parent.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid == root_pid or pid in result:  # 成环时不许转不出来，也不许把根自己算成后代
            continue
        result.append(pid)
        stack.extend(by_parent.get(pid, []))
    return result


def _process_table() -> list[tuple[int, int]]:
    """当前进程表 `(pid, ppid)`；拿不到就返回空（取证降级，不影响判停）。"""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            completed = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[tuple[int, int]] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1])))
    return rows


def _thread_states(pid: int) -> list[str]:
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | ForEach-Object {{ $_.Threads }} | "
                "ForEach-Object { $_.Id.ToString() + ' ' + $_.ThreadState + ' ' + $_.WaitReason }",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def capture_hang_evidence(child_pid: int, out_path: pathlib.Path) -> list[str]:
    """在**杀掉之前**把现场抓下来：每个解释器进程的 `py-spy` 栈 + 原生线程等待态。

    这一条是"3 小时白等"换来的：冻住时进程内的一切定时器都不执行（连 `--hard-stop` 线程、
    `faulthandler` 的 dump 线程都停了），所以**能问的只有进程外**。以前靠人手 py-spy，
    现在由监督器自动落盘，下次冻结自带证据。
    """
    lines = [f"# 冻结现场取证（子进程 pid={child_pid}）", f"抓取时刻：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    pids = [child_pid, *descendant_pids(_process_table(), child_pid)]
    py_spy = shutil.which("py-spy")
    lines.append(f"py-spy：{py_spy or '未找到（取证降级）'}")
    for pid in pids:
        lines.append(f"\n== pid {pid} 的线程等待态（原生） ==")
        lines.extend(_thread_states(pid) or ["（拿不到线程状态）"])
        lines.append(f"\n== pid {pid} 的 Python 栈（py-spy） ==")
        if not py_spy:
            lines.append("（没有 py-spy，跳过；建议 `pip install py-spy`）")
            continue
        try:
            completed = subprocess.run(
                [py_spy, "dump", "--pid", str(pid), "--json"], capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            lines.append(f"（py-spy 失败：{type(exc).__name__}: {exc}）")
            continue
        lines.append(completed.stdout.strip() or f"（py-spy 返回空，stderr={completed.stderr.strip()[:200]}）")
    try:
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return lines
    return lines


def write_note(path: pathlib.Path, lines: list[str]) -> None:
    """监督说明落到 `<report>.supervisor.txt`：跑批自己的报告是"跑到哪"，这份是"为什么停的"。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", required=True, help="跑批会反复重写的报告路径（进度证据）")
    parser.add_argument("--stall-seconds", type=float, default=420, help="报告 mtime 无进展超过这个秒数就杀（0=关）")
    parser.add_argument("--max-seconds", type=float, default=0, help="总时长上限（秒；0=关）")
    parser.add_argument("--poll-seconds", type=float, default=15, help="检查间隔（秒）")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="`--` 之后是要监督的命令")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = [item for item in args.command if item != "--"]
    if not command:
        print("用法：bench_supervise.py --report <报告路径> -- <要监督的命令...>", file=sys.stderr)
        return 2
    report_path = pathlib.Path(args.report)
    note_path = report_path.with_suffix(report_path.suffix + ".supervisor.txt")
    child = subprocess.Popen(command)
    child_started = time.time()
    print(f"[监督] 子进程 pid={child.pid}；报告={report_path}；停滞阈值={args.stall_seconds}s", flush=True)
    try:
        while True:
            time.sleep(max(1.0, args.poll_seconds))
            decision = supervise_decision(
                report_path,
                child_started=child_started,
                now=time.time(),
                stall_seconds=args.stall_seconds,
                max_seconds=args.max_seconds,
                alive=child.poll() is None,
            )
            if decision == "exited":
                return int(child.returncode or 0)
            if decision == "running":
                continue
            reference = progress_reference(report_path, child_started=child_started)
            # **先取证再杀**：冻住时进程内什么都不执行，能问的只有进程外（见 capture_hang_evidence）。
            evidence_path = report_path.with_suffix(report_path.suffix + ".hang-evidence.txt")
            capture_hang_evidence(child.pid, evidence_path)
            termination = terminate_tree(child.pid)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # 杀不掉也要把结论写完再退：这一步的失败不能把"为什么停的"一起带走
                termination += "；子进程 30s 内未退出（可能已脱离进程组，请手工核对）"
            elapsed = time.time() - child_started
            reason = (
                f"总时长超过 {args.max_seconds}s（已跑 {elapsed:.0f}s）"
                if decision == "max_seconds"
                else f"报告 {report_path.name} 连续 {time.time() - reference:.0f}s 没有更新（阈值 {args.stall_seconds}s）"
            )
            write_note(
                note_path,
                [
                    "监督器判定：跑批卡住 → 已杀掉子进程树。",
                    f"原因：{reason}",
                    f"子进程 pid：{child.pid}（启动于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(child_started))}）",
                    f"清理动作：{termination}",
                    f"冻结现场证据：{evidence_path}",
                    f"判定时刻：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                    "注意：进程内兜底（心跳 / CLI 看门狗 / --task-timeout / --hard-stop 线程）没能生效，",
                    "说明这是**解释器级冻结**。现场取证：py-spy dump --pid <pid> --json",
                    "（若进程已被杀掉，用跑批自己的日志里的 --debug-stacks 输出）。",
                ],
            )
            print(f"[监督] {reason}；{termination}；说明已落盘：{note_path}", flush=True)
            return 4
    except KeyboardInterrupt:
        terminate_tree(child.pid)
        return 130


if __name__ == "__main__":
    sys.exit(main())
