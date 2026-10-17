"""P5-6 的后半句：`_stop_external_codex_process_tree` 对**真实** Codex CLI 进程树的那一刀。

为什么这条要单独写、而且默认不跑：

* ROADMAP 里 P5-6 的判据只证明了"每个写入阶段硬杀一次进程，锚点仍可读"，那是**自己杀自己**；
  真正没验的是 —— 看门狗/清理入口拿一个**外部** Codex CLI 的 pid 调 `taskkill /T /F`，
  能不能把它和它的子进程一起打掉。真实 Codex 的进程形状是 `cmd.exe → node → codex.exe`
  （npm 的 `.cmd` 转发器），**只杀父进程会留下孤儿 codex.exe**，而孤儿会继续占槽位 ——
  这正是"清不掉的外部 agent"那个故障形态。
* 它会真的调一次真实模型（几秒、几千 token 就杀掉），所以**默认 skip**：
  设 `CODEX_BATTLEFIELD_REAL_PROCESS_KILL=1` 才跑。skip 时**写明原因**，不假装通过。
* 本机没有 codex 时整组 skip（与 `test_provider_wire_format.py` 同一约定）。

判据（三条互不替代）：

1. `_stop_external_codex_process_tree(pid)` 返回 `stopped=True`；
2. 被杀的进程树（含孙进程）在 3 秒内**全部消失**；
3. 正在等它的那个调用**带着失败回来**（`exit_code != 0`），不是挂死、也不是静默成功。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner

REAL_KILL_ENABLED = os.environ.get("CODEX_BATTLEFIELD_REAL_PROCESS_KILL") == "1"
CODEX = shutil.which("codex.cmd") or shutil.which("codex")

pytestmark = [
    pytest.mark.skipif(
        not REAL_KILL_ENABLED,
        reason="会真的调用一次 Codex CLI（花额度）；设 CODEX_BATTLEFIELD_REAL_PROCESS_KILL=1 才跑",
    ),
    pytest.mark.skipif(CODEX is None, reason="本机没有 codex CLI，无法验证“杀真实进程树”"),
]


def _descendants(pid: int) -> list[int]:
    """用 WMI 取 `pid` 的整个子孙集合（含自己）。失败时返回 `[pid]`。"""

    if os.name != "nt":
        return [pid]
    command = (
        "Get-CimInstance -Query \"SELECT ProcessId,ParentProcessId FROM Win32_Process\" | "
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress -Depth 3"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return [pid]
    if isinstance(raw, dict):
        raw = [raw]
    children: dict[int, list[int]] = {}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            parent = int(item.get("ParentProcessId"))
            child = int(item.get("ProcessId"))
        except (TypeError, ValueError):
            continue
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = [pid]
    while queue:
        current = queue.pop()
        found.append(current)
        queue.extend(children.get(current, []))
    return found


def _alive(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in completed.stdout.replace(",", "")
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.mark.asyncio
async def test_p56_killing_a_real_codex_tree_leaves_no_orphans(tmp_path: Path, monkeypatch) -> None:
    settings = get_settings().__class__(
        workspace_dir=tmp_path,
        codex_bin=str(CODEX),
        codex_model=os.environ.get("CODEX_BATTLEFIELD_REAL_MODEL", ""),
        codex_timeout_seconds=120,
        # 关掉重试：被杀会被 runner 当成"可重试的失败"，于是它再拉起一次并**成功**回来
        # （实测第一次跑这里的断言就是这么红的：exit_code=0、2709 output tokens ——
        #  那不是杀失败，是第 2 次尝试跑完了）。要验"杀"这一刀，就得让它没有第二次机会。
        codex_retry_attempts=0,
        codex_ignore_rules=True,
        codex_ephemeral=True,
    )
    runner = CodexExecRunner(settings)
    orchestrator = Orchestrator(settings)

    captured: dict[str, object] = {}
    real_create = asyncio.create_subprocess_exec

    async def spy_create(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        captured["process"] = process
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_create)

    run = asyncio.create_task(
        runner._run_codex_io(
            run_id="kill-probe",
            cwd=tmp_path,
            kind="planner",
            # 提示词刻意要一个**长回答**：如果模型 3 秒就答完，进程会自己退出，
            # 那就分不清"是 taskkill 杀掉的"还是"它自己跑完的"（第一次实测就是
            # 这样被愚弄的：去掉 /T 的变异仍然绿，因为进程先自然结束了）。
            prompt=(
                "只输出一个 JSON 对象，不要别的内容。`reason` 字段必须写至少 800 字，"
                "逐条列出你对这条目标的所有判断依据与反例。"
            ),
            schema_name="planner-decision.schema.json",
            sandbox="read-only",
        )
    )

    deadline = asyncio.get_running_loop().time() + 60
    while "process" not in captured and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.2)
    assert "process" in captured, "60 秒内没有拉起 Codex 进程，无法验证杀进程树"
    process = captured["process"]
    parent_pid = process.pid
    # 等它真的把子进程生出来（npm 的 .cmd 转发器 → node → codex.exe 要一两秒）
    await asyncio.sleep(3.0)

    tree = _descendants(parent_pid)
    assert len(tree) > 1, f"只看到 {tree}，没有子进程就证明不了“杀树”；换个真实模型再跑"
    # 先证明"这些进程此刻确实活着"，否则下面"杀完都没了"可能只是它们自己退出了。
    assert all(_alive(pid) for pid in tree), f"杀之前就有进程已经退出（{tree}），这一轮量不准"

    result = orchestrator._stop_external_codex_process_tree(parent_pid)
    assert result["stopped"] is True, result

    gone_deadline = asyncio.get_running_loop().time() + 5
    survivors = [pid for pid in tree if _alive(pid)]
    while survivors and asyncio.get_running_loop().time() < gone_deadline:
        await asyncio.sleep(0.3)
        survivors = [pid for pid in tree if _alive(pid)]
    assert survivors == [], f"杀完之后还有孤儿进程活着：{survivors}（tree={tree}）"

    envelope = await asyncio.wait_for(run, timeout=60)
    assert envelope.exit_code != 0, "被杀的调用必须带着失败回来，不能静默成功"
    assert envelope.exit_code is not None
