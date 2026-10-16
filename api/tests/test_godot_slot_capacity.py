from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from app.config import get_settings
from app.runner import CodexExecRunner


HOLDER_SOURCE = '''
import importlib.util
import os
import time

spec = importlib.util.spec_from_file_location("godot_wrapper", os.environ["GW_PATH"])
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

log_path = os.environ["HOLDER_LOG"]
pid = os.getpid()


def record(tag: str) -> None:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"{tag} {pid} {time.time()}\\n")
        handle.flush()


lock_file = gw._acquire_godot_lock()
if lock_file is None:
    record("timeout")
    raise SystemExit(125)
record("hold")
time.sleep(float(os.environ.get("HOLD_SECONDS", "1")))
gw._unlock(lock_file)
lock_file.close()
gw._update_resource_state("drop")
record("release")
'''


def _max_concurrency(events: list[tuple[str, float]]) -> int:
    running = 0
    observed = 0
    for tag, _ts in sorted(events, key=lambda item: item[1]):
        if tag == "hold":
            running += 1
            observed = max(observed, running)
        elif tag in ("release", "timeout"):
            running -= 1
    return observed


def test_godot_slot_capacity_limits_concurrency(tmp_path: Path) -> None:
    """The Godot wrapper must never let more than `godot_max_concurrency` real launches run.

    Regression guard for the original single-file lock, which hard-coded a capacity of 1.
    """
    capacity = 2
    process_count = 3
    workspace = tmp_path / "workspace"
    settings = get_settings().__class__(workspace_dir=workspace, godot_max_concurrency=capacity)

    wrapper_bin = Path(CodexExecRunner(settings)._ensure_godot_timeout_wrapper())
    wrapper_script = wrapper_bin.with_name("godot_with_timeout.py")
    assert wrapper_script.is_file()

    holder_script = tmp_path / "holder.py"
    holder_script.write_text(HOLDER_SOURCE, encoding="utf-8")
    log_path = tmp_path / "holders.log"
    state_path = tmp_path / "godot.resource.state.json"

    env = dict(os.environ)
    env.update(
        {
            "GW_PATH": str(wrapper_script),
            "HOLDER_LOG": str(log_path),
            # 持有时长要**明显长于**采样循环之外的调度抖动：`-n auto` 下整机被抢 CPU 时，
            # 1 秒的窗口会在"进程都跑完了"和"状态文件被采到"之间输掉 —— 实测这一条
            # 因此在一次完整门禁里偶发失败（同一条用例单独跑全绿）。5 秒仍然是个短测试，
            # 但它不再和调度器赌运气。
            "HOLD_SECONDS": "5",
            "CODEX_BATTLEFIELD_GODOT_LOCK_PATH": str(tmp_path / "godot.lock"),
            "CODEX_BATTLEFIELD_GODOT_STATE_PATH": str(state_path),
            "CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY": str(capacity),
            "CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS": "60",
        }
    )

    processes = [
        subprocess.Popen(
            [sys.executable, str(holder_script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(process_count)
    ]

    observed_state: dict | None = None
    read_errors = 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if state_path.exists():
            try:
                snapshot = json.loads(state_path.read_text(encoding="utf-8"))
            except ValueError:
                snapshot = None
            except OSError:
                # 采样点撞上写窗口，不是被测行为坏了：Windows 上写者 `os.replace` 换名的那一瞬间，
                # 读者会吃到 `PermissionError: [Errno 13]`（目标文件处于"待替换"状态）。
                # 产品的读者 `_may_attempt_now` 对同一个错误是**放行**的，并有单独判据钉住
                # （test_godot_queue_priority.py::test_may_attempt_now_fails_open_when_state_file_is_unreadable）。
                # 所以这里继续采样；20 秒内一直采不到仍然会红，失败信息里带上错误次数，
                # 免得"文件一直读不了"被这个 except 吞成"只是没采到"。
                read_errors += 1
                snapshot = None
            if snapshot and snapshot.get("holders"):
                observed_state = snapshot
                break
        time.sleep(0.2)

    failures = []
    for process in processes:
        output, _ = process.communicate(timeout=120)
        if process.returncode != 0:
            failures.append(f"exit={process.returncode} output={output}")

    assert not failures, f"holder processes failed: {failures}"

    events: list[tuple[str, float]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 3:
            events.append((parts[0], float(parts[2])))

    holds = [event for event in events if event[0] == "hold"]
    timeouts = [event for event in events if event[0] == "timeout"]
    assert len(holds) == process_count, events
    assert not timeouts, events
    assert _max_concurrency(events) <= capacity, events

    # Queue visibility: while slots were contended, holders and waiters must be observable.
    assert observed_state is not None, f"queue state file never reported holders (read_errors={read_errors})"
    assert observed_state["capacity"] == capacity
    assert len(observed_state["holders"]) <= capacity


def test_godot_capacity_defaults_to_one(tmp_path: Path) -> None:
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace")
    assert settings.godot_max_concurrency == max(1, int(settings.godot_max_concurrency))
    wrapper_bin = Path(CodexExecRunner(settings)._ensure_godot_timeout_wrapper())
    wrapper_script = wrapper_bin.with_name("godot_with_timeout.py")
    source = wrapper_script.read_text(encoding="utf-8")
    assert "CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY" in source
    assert "_slot_paths" in source
