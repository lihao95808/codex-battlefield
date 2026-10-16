"""R1 的"静态优先级 + 老化"：多进程争抢时的可观测断言。

背景：OS 字节锁只保证**互斥**（容量绝不超过），它不认识优先级——原来的实现是纯 FCFS，
谁先轮询到释放的空槽谁拿。长等低优会被新到的反复插队（`spec-resource-governance.md` §3.6
的"无队列、无老化、无饥饿防护"）。所以仲裁放在包装器的应用层：

- 静态优先级按角色（runner 注入 `..._QUEUE_WEIGHT`：门禁/验收 > 验证 > 实现）；
- 老化：每等 `..._QUEUE_AGING_SECONDS` 秒，等效优先级 +1。注意老化对**同时在等**的人不改变差距
  （两人同速上涨），它拉开的是"先到者 vs 后来者"——所以它防饥饿，但不改变高优对同到者的领先；
- 读不到状态/解析失败一律放行（仲裁坏了不能让人永远等），容量上限始终由 OS 锁保证。

这些用例都真的起多进程抢同一个槽位，断言的是**获取顺序**：顺序错了就是饥饿或插队。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.runner import CodexExecRunner


DRIVER_SOURCE = '''
import importlib.util
import os
import time

spec = importlib.util.spec_from_file_location("godot_wrapper", os.environ["GW_PATH"])
gw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gw)

tag = os.environ["MY_TAG"]
weight = int(os.environ["MY_WEIGHT"])
log_path = os.environ["MY_LOG"]


def record(event: str) -> None:
    with open(log_path, "a", encoding="utf-8") as handle:
        # 记**发起顺序**而不只是权重：同权重的两个进程按权重看顺序永远是 [0, 0]，
        # 那种断言是空的——"同权重仍 FCFS"这条契约必须按发起顺序才能验证。
        # （不用 pid：`sys.executable` 在 Windows 上可能是转发器，`Popen.pid` 与子进程自己的
        # `getpid()` 未必相同，拿它当身份会得到一条看起来像"顺序错了"的假失败。）
        handle.write(f"{event} {weight} {os.environ.get('MY_ORDER', '0')} {time.time()}\\n")
        handle.flush()


lock_file = gw._acquire_godot_lock()
if lock_file is None:
    record(f"{tag}_timeout")
    raise SystemExit(125)
record(f"{tag}_acquire")
time.sleep(float(os.environ.get("HOLD_SECONDS", "2")))
gw._unlock(lock_file)
lock_file.close()
gw._update_resource_state("drop")
record(f"{tag}_release")
'''


def _wrapper_script(case_dir: Path, capacity: int = 1) -> Path:
    settings = get_settings().__class__(workspace_dir=case_dir / "workspace", godot_max_concurrency=capacity)
    wrapper = Path(CodexExecRunner(settings)._ensure_godot_timeout_wrapper())
    return wrapper.with_name("godot_with_timeout.py")


def _case_env(
    wrapper_script: Path,
    log_path: Path,
    state_path: Path,
    lock_path: Path,
    *,
    priority_enabled: bool,
    aging_seconds: int,
    capacity: int = 1,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GW_PATH": str(wrapper_script),
            "MY_LOG": str(log_path),
            "CODEX_BATTLEFIELD_GODOT_LOCK_PATH": str(lock_path),
            "CODEX_BATTLEFIELD_GODOT_STATE_PATH": str(state_path),
            "CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY": str(capacity),
            "CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS": "90",
            "CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY": "1" if priority_enabled else "0",
            "CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS": str(aging_seconds),
        }
    )
    return env


def _spawn(env: dict[str, str], driver: Path, tag: str, weight: int, hold_seconds: float, order: int = 0) -> subprocess.Popen:
    child_env = dict(env)
    child_env.update(
        {
            "MY_TAG": tag,
            "MY_WEIGHT": str(weight),
            "MY_ORDER": str(order),
            # 这才是包装器读的权重。漏了它所有进程都是 0 权重，仲裁会静默退化成"先到先得"——
            # 第一版测试就漏了，于是"优先级"用例拿到了 [0,2,1] 这种看起来像时序抖动的结果。
            "CODEX_BATTLEFIELD_GODOT_QUEUE_WEIGHT": str(weight),
            "HOLD_SECONDS": str(hold_seconds),
        }
    )
    return subprocess.Popen(
        [sys.executable, str(driver)],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _read_order(log_path: Path, tag: str) -> list[tuple[int, int]]:
    """按拿到槽的先后返回 `(权重, 发起顺序)`。"""
    order: list[tuple[int, int]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == f"{tag}_acquire":
            order.append((int(parts[1]), int(parts[2])))
    return order


def _run_case(
    tmp_path: Path,
    name: str,
    *,
    priority_enabled: bool,
    aging_seconds: int,
    waiters: list[tuple[int, float, float]],
    hold_seconds: float,
    blocker_hold_seconds: float,
) -> tuple[Path, list[subprocess.Popen]]:
    """`waiters` 是 (权重, 相对开局的延迟秒, 自己持槽秒)。返回日志路径与**尚未收割**的进程列表。

    ⚠️ 这些等待窗口是**时间预算**的一部分，不是断言的一部分（2026-10-17 收窄）：

    * 阻塞者的持有时长只需覆盖"最后一个等待者到场"（延迟最大 3.0s / 老化那条 5.0s），
      所以 8.0 → **4.0**、12.0 → **7.0**（仍留 1.0–2.0s 余量）；
    * 等待者拿到槽后自己持有 2.0 → **1.0** 秒：它只影响"下一名何时能拿"，
      不影响排名（排名看的是 `since` 与老化，而老化由**阻塞者**的持有窗口提供）。

    实测：本文件串行 **79.9s → 54.7s**（−31%），连跑 3 次全绿（54.7 / 56.7 / 54.7）。
    为什么值得改：这 5 条是本机最慢的一簇，也是 Linux CI"30 分钟跑不完"的主要嫌疑之一
    —— 门禁跑不完时，减等待比调大超时更根本。余量刻意留着：老化那条仍给出
    4 秒老化 vs 2 点静态权重（差值 2）。
    """
    case_dir = tmp_path / name
    case_dir.mkdir(parents=True, exist_ok=True)
    wrapper_script = _wrapper_script(case_dir)
    log_path = case_dir / "queue.log"
    env = _case_env(
        wrapper_script,
        log_path,
        case_dir / "state.json",
        case_dir / "godot.lock",
        priority_enabled=priority_enabled,
        aging_seconds=aging_seconds,
    )
    driver = case_dir / "driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")

    processes = [_spawn(env, driver, "blocker", 0, blocker_hold_seconds)]
    started = time.monotonic()
    for index, (weight, delay, hold) in enumerate(waiters, start=1):
        remaining = delay - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        processes.append(_spawn(env, driver, "waiter", weight, hold, order=index))
    return log_path, processes


def _settle(processes: list[subprocess.Popen], log_path: Path) -> list[tuple[int, int]]:
    failures = []
    for process in processes:
        output, _ = process.communicate(timeout=180)
        if process.returncode != 0:
            failures.append(f"exit={process.returncode} output={output}")
    assert not failures, f"queue driver failed: {failures}"
    return _read_order(log_path, "waiter")


def test_higher_role_weight_gets_the_freed_slot_first(tmp_path: Path) -> None:
    """门禁/验收 > 验证 > 实现：槽位释放后按权重从上往下拿，而不是谁先轮询到谁拿。"""
    log_path, processes = _run_case(
        tmp_path,
        "priority",
        priority_enabled=True,
        aging_seconds=3600,
        waiters=[(0, 1.0, 1.0), (2, 1.2, 1.0), (1, 1.4, 1.0)],
        hold_seconds=2.0,
        blocker_hold_seconds=4.0,
    )
    assert [weight for weight, _pid in _settle(processes, log_path)] == [2, 1, 0]


def test_aging_lets_a_long_waiting_low_weight_win(tmp_path: Path) -> None:
    """老化：先到的低权重等久了，等效优先级超过后来居上的高权重——这是防饥饿那条线。"""
    log_path, processes = _run_case(
        tmp_path,
        "aging",
        priority_enabled=True,
        aging_seconds=1,
        waiters=[(0, 1.0, 1.0), (2, 5.0, 1.0)],
        hold_seconds=2.0,
        blocker_hold_seconds=7.0,
    )
    assert [weight for weight, _pid in _settle(processes, log_path)] == [0, 2]


def test_priority_disabled_keeps_everyone_eligible(tmp_path: Path) -> None:
    """关掉策略后不再有"排名"这回事：两个等待者都能拿到槽。

    顺序在关掉策略后**本来就是时序相关的**（谁先轮询到释放的空槽谁拿），断言它就等于断言抖动；
    所以这里只断言"都能拿到 + 状态里写着策略已关"，把顺序留给下面那组确定性单元断言。
    """
    log_path, processes = _run_case(
        tmp_path,
        "disabled",
        priority_enabled=False,
        aging_seconds=1,
        waiters=[(0, 1.0, 1.0), (3, 3.0, 1.0)],
        hold_seconds=2.0,
        blocker_hold_seconds=4.0,
    )
    settled = _settle(processes, log_path)
    assert sorted(weight for weight, _pid in settled) == [0, 3], settled
    state = json.loads((tmp_path / "disabled" / "state.json").read_text(encoding="utf-8"))
    assert state["queue_policy"] == {"priority_enabled": False, "aging_seconds": 1}


def _load_wrapper(script: Path):
    spec = importlib.util.spec_from_file_location("godot_wrapper_unit", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_queue_state(path: Path, waiters: list[dict], capacity: int = 1) -> None:
    path.write_text(
        json.dumps({"capacity": capacity, "holders": [], "waiters": waiters}),
        encoding="utf-8",
    )


def _unit_case(tmp_path: Path, name: str) -> tuple[object, Path]:
    case_dir = tmp_path / name
    case_dir.mkdir()
    wrapper = _load_wrapper(_wrapper_script(case_dir))
    state_path = case_dir / "state.json"
    return wrapper, state_path


def test_may_attempt_now_gates_low_ranked_waiters(tmp_path: Path, monkeypatch) -> None:
    """仲裁规则本身（确定性，不依赖时序）：容量 1 时只有排名第一的等待者可以去抢槽。"""
    wrapper, state_path = _unit_case(tmp_path, "unit")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(state_path))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "3600")
    now = time.time()
    me, other = os.getpid(), os.getppid()  # 父进程（pytest 自己）一定存活
    _write_queue_state(
        state_path,
        [
            {"pid": me, "since": now, "priority": 0},
            {"pid": other, "since": now, "priority": 3},
        ],
    )
    assert wrapper._may_attempt_now(me, 1) is False
    assert wrapper._may_attempt_now(other, 1) is True
    assert wrapper._may_attempt_now(me, 2) is True  # 容量 2 时排名第二也可以抢


def test_may_attempt_now_lets_aging_overtake(tmp_path: Path, monkeypatch) -> None:
    """老化在仲裁层的效果，外加"关掉策略就无条件放行"这条机器判据。"""
    wrapper, state_path = _unit_case(tmp_path, "unit-aging")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(state_path))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "1")
    now = time.time()
    me, other = os.getpid(), os.getppid()
    _write_queue_state(
        state_path,
        [
            {"pid": me, "since": now - 5, "priority": 0},  # 等了 5 秒 → 等效 5
            {"pid": other, "since": now, "priority": 3},  # 刚到 → 等效 3
        ],
    )
    assert wrapper._may_attempt_now(me, 1) is True
    # 老化关掉（=0）则回到纯静态优先级：低权重必须让位
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "0")
    assert wrapper._may_attempt_now(me, 1) is False
    # 策略整个关掉则无条件放行
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "0")
    assert wrapper._may_attempt_now(me, 1) is True


def test_may_attempt_now_fails_open_without_state(tmp_path: Path, monkeypatch) -> None:
    """读不到状态就放行：仲裁是优化，坏了不能让人永远等（容量上限始终由 OS 锁保证）。"""
    wrapper, state_path = _unit_case(tmp_path, "unit-missing")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(state_path.with_name("absent.json")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1")
    assert wrapper._may_attempt_now(os.getpid(), 1) is True


def test_may_attempt_now_fails_open_when_state_file_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    """读者撞上 `PermissionError` 时必须放行 —— 那正是 Windows 换名窗口里的真实错误。

    事故记录见 `docs/incident-conclusions.md` §8.2：写者用 `os.replace` 换名的瞬间，
    读者打不开目标文件（Windows 给 `PermissionError: [Errno 13]`）。这里拿一个**目录**
    冒充状态文件：Windows 上 `open(目录)` 正是 Errno 13，POSIX 上是 `IsADirectoryError` ——
    两者都是 `OSError`，走的必须是同一条 fail-open 分支。
    """
    wrapper, state_path = _unit_case(tmp_path, "unit-unreadable")
    blocked = tmp_path / "state-as-directory"
    blocked.mkdir()
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(blocked))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1")

    # 先确认这个替身**真的**读不了：否则下面那条 `is True` 可能只是"文件不存在"的另一种走法，
    # 判据看着绿，其实没验到"读到错误"这一支。
    with pytest.raises(OSError) as excinfo:
        blocked.read_text(encoding="utf-8")
    if os.name == "nt":
        assert isinstance(excinfo.value, PermissionError), excinfo.value

    assert wrapper._may_attempt_now(os.getpid(), 1) is True


def test_codex_env_feeds_the_role_weight(tmp_path: Path) -> None:
    """优先级的**来源**必须真的喂进子进程环境。

    仲裁写得再对，没人给权重也是死的——这一带的常见坑就是"接线看着在、实际是死的"，
    所以这里断言的是 runner 注入的那三个环境变量本身。
    """
    godot_bin = tmp_path / "godot.exe"
    godot_bin.write_bytes(b"")
    settings = get_settings().__class__(workspace_dir=tmp_path / "workspace", godot_bin=str(godot_bin))
    executor = CodexExecRunner(settings)

    for kind, expected in (("acceptance", 3), ("verifier", 2), ("worker", 1), ("planner", 0), ("", 0)):
        env = executor._codex_env(kind)
        assert env["CODEX_BATTLEFIELD_GODOT_QUEUE_WEIGHT"] == str(expected), kind
        assert env["CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY"] == "1"
        assert env["CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS"] == str(
            max(0, settings.godot_queue_aging_seconds)
        )

    off = get_settings().__class__(
        workspace_dir=tmp_path / "workspace",
        godot_bin=str(godot_bin),
        godot_queue_priority_enabled=False,
        godot_queue_aging_seconds=0,
    )
    env = CodexExecRunner(off)._codex_env("acceptance")
    assert env["CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY"] == "0"
    assert env["CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS"] == "0"


def test_same_weight_stays_first_come_first_served(tmp_path: Path) -> None:
    """同权重内仍是 FCFS：否则"优先级"会变成新的饥饿源。

    断言**发起顺序**而不是权重顺序：两个权重都是 0 时，按权重看的顺序恒为 `[0, 0]`，
    那种"断言"永远通过、什么也没验证（第一版就是这么写的）。
    """
    log_path, processes = _run_case(
        tmp_path,
        "same-weight",
        priority_enabled=True,
        aging_seconds=3600,
        waiters=[(0, 1.0, 1.0), (0, 3.0, 1.0)],
        hold_seconds=2.0,
        blocker_hold_seconds=4.0,
    )
    settled = _settle(processes, log_path)
    assert [order for _weight, order in settled] == [1, 2]


def test_abandoned_waiters_are_pruned(tmp_path: Path, monkeypatch) -> None:
    """泄漏的等待者必须能过期，否则它永久占着排名第一（老化还让它越来越靠前）。

    `_pid_alive` 挡不住这种条目：已退出但句柄仍可 OpenProcess 的 pid 返回 True，pid 还可能被复用；
    结果是别人只能看着**已经空着**的锁等满 LOCK_WAIT_SECONDS 然后超时。
    真实的等待者活不过 LOCK_WAIT_SECONDS（超时那条路它会自己 drop），所以比它更老的就是泄漏。
    """
    wrapper, state_path = _unit_case(tmp_path, "unit-abandoned")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(state_path))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS", "60")
    zombie = os.getppid()  # 存活，所以只可能被"过期"这条规则清掉
    _write_queue_state(state_path, [{"pid": zombie, "since": time.time() - 3600, "priority": 0}])

    wrapper._update_resource_state("waiter")

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert [item["pid"] for item in after["waiters"]] == [os.getpid()]


def test_fresh_waiters_survive_the_refresh(tmp_path: Path, monkeypatch) -> None:
    """过期规则不能顺手把**正常等待者**清掉：它只清超过存活上限的条目。"""
    wrapper, state_path = _unit_case(tmp_path, "unit-fresh")
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_STATE_PATH", str(state_path))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", str(state_path.with_name("godot.lock")))
    monkeypatch.setenv("CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS", "600")
    other = os.getppid()
    _write_queue_state(state_path, [{"pid": other, "since": time.time() - 5, "priority": 2}])

    wrapper._update_resource_state("waiter")

    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert sorted(item["pid"] for item in after["waiters"]) == sorted([other, os.getpid()])


def test_queue_state_exposes_priority_wait_and_aging(tmp_path: Path) -> None:
    """排队可见性：等待者要带权重、等待时长与等效优先级，看板才能说清"谁在等、为什么"。"""
    case_dir = tmp_path / "visibility"
    case_dir.mkdir(parents=True, exist_ok=True)
    wrapper_script = _wrapper_script(case_dir)
    log_path = case_dir / "queue.log"
    state_path = case_dir / "state.json"
    env = _case_env(
        wrapper_script,
        log_path,
        state_path,
        case_dir / "godot.lock",
        priority_enabled=True,
        aging_seconds=1,
    )
    driver = case_dir / "driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")

    processes = [_spawn(env, driver, "blocker", 0, 14.0)]
    time.sleep(1.0)
    processes.append(_spawn(env, driver, "waiter", 0, 1.0))  # 先到的低权重
    time.sleep(5.0)
    processes.append(_spawn(env, driver, "waiter", 3, 1.0))  # 后来的高权重

    observed: dict | None = None
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            snapshot = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            snapshot = None
        waiters = (snapshot or {}).get("waiters") or []
        if len(waiters) == 2 and float(waiters[0].get("waited_seconds") or 0) >= 3.0:
            observed = snapshot
            break
        time.sleep(0.3)

    for process in processes:
        process.communicate(timeout=180)

    assert observed is not None, "等待者与老化信息没有出现在状态文件里"
    assert observed["queue_policy"] == {"priority_enabled": True, "aging_seconds": 1}
    waiters = observed["waiters"]
    # 排在前面的是等效优先级最高的：先到的低权重（0 + 等待秒数）已反超后到的高权重（3 + 较短等待）
    assert [item["priority"] for item in waiters] == [0, 3], waiters
    assert waiters[0]["effective_priority"] > waiters[1]["effective_priority"], waiters
    assert waiters[0]["waited_seconds"] > waiters[1]["waited_seconds"], waiters
    assert waiters[0]["aged"] is True, waiters
    # 不变式（与观察时刻无关）：等效优先级就是"静态权重 + 等待秒数/老化秒数"，aged 表示它被抬起来了
    for item in waiters:
        expected = item["priority"] + item["waited_seconds"] / 1
        assert abs(item["effective_priority"] - expected) < 0.2, item
        assert item["aged"] is (item["effective_priority"] > item["priority"]), item
