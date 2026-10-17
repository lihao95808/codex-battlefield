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

ROOT = pathlib.Path(__file__).resolve().parents[1]

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
        group = os.getpgid(pid)
    except OSError as exc:
        return f"getpgid {pid} 失败：{type(exc).__name__}: {exc}"
    if group == os.getpgid(0):
        # 子进程和我们同组：`killpg` 会把**我们自己**一起杀掉（实测症状是调用方立刻 SIGKILL、
        # 没有 traceback）。此时只能按 pid 逐个杀它这棵子树，并把这件事说出来。
        killed: list[int] = []
        own_pid = os.getpid()
        # **绝不能**杀到自己：这条分支存在的理由就是别把调用方一起带走。
        for item in [item for item in [pid, *descendant_pids(_process_table(), pid)] if item != own_pid]:
            try:
                os.kill(item, signal.SIGKILL)
                killed.append(item)
            except OSError:
                continue
        return f"refused killpg {pid}（与监督器同组 pgid={group}）；改为逐个 SIGKILL：{killed}"
    try:
        os.killpg(group, signal.SIGKILL)
        return f"killpg {pid} (pgid={group}) → SIGKILL"
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


def find_py_spy() -> str:
    """`py-spy` 的位置：PATH → **跑本脚本的这个解释器旁边**（venv 的 `Scripts/`）。找不到返回空串。

    为什么要第二条：`py-spy` 装在 `api/.venv` 里，而用 venv 的 python 跑脚本**不等于**激活了它——
    `PATH` 里通常没有 `Scripts/`，`shutil.which` 就会空手而归。实测 2026-09-24：只靠 `which` 时，
    取证报告里每个 pid 都写着"没有 py-spy，跳过"，而机器上其实装着（0.4.2，支持 `--native`）。
    失效方向：找不到 → **只降级、不报错**（取证不许挡杀），但必须在报告里写明这次没有栈。
    """
    found = shutil.which("py-spy")
    if found:
        return found
    sibling = pathlib.Path(sys.executable).parent / ("py-spy.exe" if os.name == "nt" else "py-spy")
    return str(sibling) if sibling.is_file() else ""


def find_procdump() -> str:
    """ProcDump 的位置：环境变量 → PATH → 仓库内 `workspace/tools/procdump/`。找不到返回空串。

    为什么要找仓库内那份：它不进发布快照（`workspace/` 已 gitignore），但本机就装在那儿；
    而本机的 cmd **不搜当前目录**找可执行文件，所以必须给绝对路径（写相对路径会报
    "不是内部或外部命令"，看起来像没装）。
    """
    explicit = os.environ.get("CODEX_BATTLEFIELD_PROCDUMP", "").strip()
    if explicit and pathlib.Path(explicit).is_file():
        return explicit
    for name in ("procdump64", "procdump"):
        found = shutil.which(name)
        if found:
            return found
    local = ROOT / "workspace" / "tools" / "procdump" / "procdump64.exe"
    return str(local) if local.is_file() else ""


def _run_capture(command: list[str], *, timeout: float) -> tuple[bool, str, str]:
    """跑一条取证命令。**任何失败都只变成一段文字**，绝不抛出——取证不许挡住"杀"。

    注意"成功"的判据按**工具**来：`procdump` 抓成功也返回 **exit=1**（"Dump count reached"），
    所以 dump 那条路不看退出码、只看**文件落没落**（见 `_procdump_capture`）。
    """
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    tail = ((completed.stdout or "") + (completed.stderr or "")).strip().replace("\n", " ")[-400:]
    if completed.returncode != 0:
        return False, completed.stdout or "", f"exit={completed.returncode} 输出尾部={tail}"
    if not (completed.stdout or "").strip():
        return False, "", f"输出为空（exit=0，输出尾部={tail}）"
    return True, completed.stdout, ""


def _dump_files(dump_dir: pathlib.Path) -> dict[str, int]:
    """dump 目录里**实际**有哪些非空 `.dmp`（名字由工具决定，不由我们假设）。"""
    if not dump_dir.is_dir():
        return {}
    found: dict[str, int] = {}
    for item in dump_dir.glob("*.dmp"):
        try:
            size = item.stat().st_size
        except OSError:
            continue
        if size > 0:
            found[item.name] = size
    return found


def _procdump_capture(procdump: str, pid: int, dump_dir: pathlib.Path, *, timeout: float) -> tuple[str, str]:
    """全内存 dump（`-ma`）。返回 `(状态, 明细)`；失败也只返回文字。

    `-ma` 是**全内存**：C 层栈要靠它（`py-spy --native` 拿不到时的备路）。dump 很大（十几到几十 MB），
    所以只在判停这一刻抓一次，不做周期性抓取。

    **判据的两个坑（都实测踩过，2026-09-24）**：
    1. 成功也 `exit=1` → 看退出码会把成功读成失败；
    2. ProcDump 把文件名里的 `PID` / `PROCESSNAME` 当**宏**展开（大小写不敏感），
       给它 `pid-123.dmp` 会落成 `123-123.dmp` → **只查自己以为的那个路径**会判成"没抓到"，
       而 dump 其实就在旁边。所以文件名避开宏，且判据按"目录里**新增**了哪些非空 `.dmp`"来算。
    """
    dump_dir.mkdir(parents=True, exist_ok=True)
    before = _dump_files(dump_dir)
    # 文件名里不许出现 `pid` / `processname` 字样（ProcDump 会展开它们）
    target = (dump_dir / f"hang-{pid}.dmp").resolve()
    ok, _stdout, error = _run_capture(
        [procdump, "-accepteula", "-ma", str(pid), str(target)], timeout=timeout
    )
    added = {name: size for name, size in _dump_files(dump_dir).items() if name not in before}
    if added:
        detail = "；".join(f"{dump_dir / name}（{size} 字节）" for name, size in sorted(added.items()))
        return "ok", detail
    if ok:
        return "failed", f"procdump 自称成功但目录里没有新 dump：{dump_dir}"
    return "failed", error or f"没有产生 dump：{dump_dir}"


def capture_hang_evidence(
    child_pid: int,
    out_path: pathlib.Path,
    *,
    dump_dir: pathlib.Path | None = None,
    budget_seconds: float = 240.0,
) -> list[str]:
    """在**杀掉之前**把现场抓下来：`py-spy` 栈（优先带原生帧）+ 原生线程等待态 + 全内存 dump。

    这一条是"3 小时白等"换来的：冻住时进程内的一切定时器都不执行（连 `--hard-stop` 线程、
    `faulthandler` 的 dump 线程都停了），所以**能问的只有进程外**。

    **判据的失效方向（这里最容易写反）**：
    - 取证**永远不许挡住杀**。任何一步失败（工具没装 / 超时 / 附加不上）都只写一行说明就往下走；
      预算（`budget_seconds`）用尽就**停止取证**并显式写明"还有哪些 pid 没抓"。
      反过来写（取证失败就不杀）会让"留证据"变成"让卡死的进程继续占着 CLI 槽位"。
    - **"没抓到"必须写进文件**。少一段栈却什么都不说，读的人会把它当成"现场很干净"——
      那正是最贵的误读方向（与债务索引那条"0 不代表没有债"同型）。
    - `--native` 拿不到原生帧时**降级**成普通 `py-spy dump`，并记下降级原因；
      不降级就会出现"这次冻结一条栈都没有"。
    """
    deadline = time.time() + max(0.0, budget_seconds)
    lines = [f"# 冻结现场取证（子进程 pid={child_pid}）", f"抓取时刻：{time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    pids = [child_pid, *descendant_pids(_process_table(), child_pid)]
    py_spy = find_py_spy()
    procdump = find_procdump()
    lines.append(f"py-spy：{py_spy or '未找到（取证降级：拿不到 Python 栈）'}")
    lines.append(f"procdump：{procdump or '未找到（取证降级：没有全内存 dump）'}")
    lines.append(f"取证预算：{budget_seconds:.0f}s（用尽即停止取证，但**照样杀**）")
    summary: list[str] = []
    for pid in pids:
        remaining = deadline - time.time()
        if remaining <= 0:
            skipped = ", ".join(str(item) for item in pids[pids.index(pid) :])
            lines.append(f"\n（取证预算用尽，未取证的 pid：{skipped}）")
            summary.append(f"pid={skipped} 取证=跳过（预算用尽）")
            break
        lines.append(f"\n== pid {pid} 的线程等待态（原生） ==")
        lines.extend(_thread_states(pid) or ["（拿不到线程状态）"])

        lines.append(f"\n== pid {pid} 的 Python 栈（py-spy） ==")
        stack_state = "无 py-spy"
        if py_spy:
            ok, stdout, error = _run_capture(
                [py_spy, "dump", "--pid", str(pid), "--native", "--locals", "--json"],
                timeout=min(90.0, max(5.0, deadline - time.time())),
            )
            if ok:
                stack_state = "native"
                lines.append("（带原生帧：--native --locals）")
                lines.append(stdout.strip())
            else:
                lines.append(f"（--native 失败，降级成普通 dump：{error}）")
                ok, stdout, error = _run_capture(
                    [py_spy, "dump", "--pid", str(pid), "--json"],
                    timeout=min(60.0, max(5.0, deadline - time.time())),
                )
                if ok:
                    stack_state = "plain（无原生帧）"
                    lines.append(stdout.strip())
                else:
                    stack_state = f"失败（{error}）"
                    lines.append(f"（py-spy 失败：{error}）")
        else:
            lines.append("（没有 py-spy，跳过；`pip install py-spy`）")

        dump_state = "无 procdump"
        if procdump and dump_dir is not None:
            dump_state, detail = _procdump_capture(
                procdump, pid, dump_dir, timeout=min(180.0, max(5.0, deadline - time.time()))
            )
            lines.append(f"\n== pid {pid} 的全内存 dump（procdump -ma） ==")
            lines.append(detail)
        elif procdump:
            dump_state = "未指定 dump 目录"
        summary.append(f"pid={pid} 栈={stack_state} dump={dump_state}")

    lines.insert(3, "## 取证结果一览（缺就是缺，不许读成'现场很干净'）")
    for offset, item in enumerate(summary):
        lines.insert(4 + offset, f"- {item}")
    lines.insert(4 + len(summary), "")
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
    parser.add_argument(
        "--capture-budget-seconds",
        type=float,
        default=240,
        help="判停时取证的总预算（秒）；用尽即停止取证，但**照样杀**（取证不许挡杀）",
    )
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
    # ⚠️ POSIX 上必须让子进程自成一个会话/进程组：`terminate_tree()` 用的是 `killpg`，
    # 而 `getpgid(child)` 在**没有** `start_new_session` 时返回的是**我们自己的组** ——
    # 于是"杀掉卡住的子进程"会把自己（甚至调用方的整个进程组）一起 SIGKILL。
    # 这是 2026-10-18 在 WSL Ubuntu 上跑后端测试时才暴露的真缺陷：
    # `test_supervisor_kills_a_stalled_child_and_writes_a_note` 一跑到就把 pytest 杀掉了
    # （日志停在那条用例名上、没有 traceback —— 内核 SIGKILL 不会给解释器留遗言）。
    child = subprocess.Popen(command, start_new_session=(os.name != "nt"))
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
            dump_dir = report_path.with_suffix(report_path.suffix + ".hang-dumps")
            capture_started = time.time()
            capture_lines = capture_hang_evidence(
                child.pid,
                evidence_path,
                dump_dir=dump_dir,
                budget_seconds=args.capture_budget_seconds,
            )
            capture_seconds = time.time() - capture_started
            capture_summary = [line for line in capture_lines if line.startswith("- pid=")]
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
                    f"全内存 dump 目录：{dump_dir}（没有文件就是**没抓到**，原因见证据文件）",
                    f"取证耗时：{capture_seconds:.0f}s（预算 {args.capture_budget_seconds:.0f}s；取证不挡杀）",
                    *capture_summary,
                    f"判定时刻：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    "",
                    "注意：进程内兜底（心跳 / CLI 看门狗 / --task-timeout / --hard-stop 线程）没能生效，",
                    "说明这是**解释器级冻结**。证据文件里带 `--native` 的那段是 C 层栈，但**CPython 内部帧名不可信**",
                    "（py-spy 在 Windows 上按导出表近似，实测把 `_PyEval_EvalFrameDefault` 写成 `PyThread_GetInfo`）；",
                    "OS 帧（ntdll / KERNELBASE）与 Python 帧（含 --locals）才是可信的那两头。",
                    "要准确的原生帧就读 dump（2026-09-24 实测验通）：",
                    "  set _NT_SYMBOL_PATH=srv*C:\\Symbols*https://msdl.microsoft.com/download/symbols;<CPython pdb 目录>",
                    "  cdb -z <dump> -c \"~*k 30; q\"",
                    "CPython 的 pdb 从 python.org 取：`<版本>/amd64/core_pdb.msi`（python312.pdb）",
                    "+ `lib_pdb.msi`（`DLLs/*.pdb`，`_overlapped` 这类扩展模块要它才出真名）；",
                    "`cdb.exe` 来自 Windows SDK 的 Debugging Tools（WinDbg 那个 Store 包里**没有** cdb）。",
                    "读的时候先看主线程 `_poll` 的 `ms`：`4294967295`(INFINITE) 说明当时事件循环**一条定时器都没有**，",
                    "有限值却不返回才是 IOCP 侧卡住 —— 这两种的修法完全不同。",
                ],
            )
            print(f"[监督] {reason}；{termination}；说明已落盘：{note_path}", flush=True)
            return 4
    except KeyboardInterrupt:
        terminate_tree(child.pid)
        return 130


if __name__ == "__main__":
    sys.exit(main())
