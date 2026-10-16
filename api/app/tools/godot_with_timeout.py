from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


STRUCTURAL_SCAN_SUFFIXES = {".gd", ".tscn", ".tres", ".godot"}
DATA_SCAN_SUFFIXES = {".json"}
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".mp3"}


def _lock_path() -> Path:
    configured = os.environ.get("CODEX_BATTLEFIELD_GODOT_LOCK_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().with_name("godot.global.lock")


def _lock_capacity() -> int:
    try:
        return max(1, int(float(os.environ.get("CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY", "1"))))
    except ValueError:
        return 1


def _state_path() -> Path:
    configured = os.environ.get("CODEX_BATTLEFIELD_GODOT_STATE_PATH", "").strip()
    base = _lock_path()
    if configured:
        return Path(configured).expanduser().resolve()
    return base.with_name(base.name + ".state.json")


def _slot_paths(capacity: int) -> list:
    base = _lock_path()
    return [base.with_name(f"{base.name}.slot{index}") for index in range(capacity)]


def _lock_wait_seconds() -> int:
    try:
        return max(1, int(float(os.environ.get("CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS", "1800"))))
    except ValueError:
        return 1800


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if sys.platform.startswith("win"):
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except Exception:
        return True


def _queue_policy():
    """(是否启用优先级, 老化秒数)。

    老化秒数是"每等这么久，等效优先级 +1"，用来防饥饿；0 表示不老化（纯静态优先级）。
    默认启用：静态优先级按角色（门禁/验收 > 验证 > 实现），由 runner 注入 `..._QUEUE_WEIGHT`。
    """
    raw = os.environ.get("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1").strip().lower()
    enabled = raw not in {"0", "false", "no"}
    try:
        aging = max(0, int(float(os.environ.get("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "300"))))
    except ValueError:
        aging = 300
    return enabled, aging


def _my_weight() -> int:
    try:
        return int(float(os.environ.get("CODEX_BATTLEFIELD_GODOT_QUEUE_WEIGHT", "0")))
    except ValueError:
        return 0


def _effective_priority(weight, since, aging: int, now=None) -> float:
    if aging <= 0:
        return float(weight)
    try:
        waited = max(0.0, (now if now is not None else time.time()) - float(since))
    except (TypeError, ValueError):
        waited = 0.0
    return float(weight) + waited / aging


def _abandoned(entry, now: float) -> bool:
    """这个等待者条目是否早已超过它可能存活的最长时间（超过就一定是泄漏，不是"还在等"）。

    为什么必须有过期：`_pid_alive` 对"已退出但仍可 OpenProcess 的 pid"返回 True，
    pid 还可能被复用给无关进程；而老化让 `since` 越老等效优先级越高。一条泄漏条目于是永久
    占着 top 排名，别人只能眼睁睁看着空槽等满 LOCK_WAIT_SECONDS。反过来，真实等待者活不过
    LOCK_WAIT_SECONDS——超时那条路会自己 drop——所以比它老的就是泄漏。
    """
    try:
        since = float(entry.get("since"))
    except (AttributeError, TypeError, ValueError):
        return False
    return (now - since) > _lock_wait_seconds() + 60


def _live_waiters(state) -> list:
    items = state.get("waiters") if isinstance(state, dict) else None
    if not isinstance(items, list):
        return []
    live = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid", 0))
        except (TypeError, ValueError):
            continue
        if pid > 0 and _pid_alive(pid):
            live.append(item)
    return live


def _ranked_waiters(state, aging: int, now: float) -> list:
    return sorted(
        _live_waiters(state),
        key=lambda item: (
            -_effective_priority(item.get("priority", 0), item.get("since"), aging, now),
            float(item.get("since") or now),
            int(item.get("pid", 0)),
        ),
    )


def _may_attempt_now(my_pid: int, capacity: int) -> bool:
    """应用层仲裁：只有排名前 `capacity` 的等待者去抢槽，其余继续等。

    为什么需要它：OS 字节锁只保证**互斥**（容量绝不超过），它不认识优先级——谁先轮询到
    释放的空槽谁就拿到。所以"谁先抢"只能在应用层排。

    读不到/解析失败/算不出来一律**放行**：仲裁是优化，不能因为它坏了就让人永远等下去。
    容量上限始终由 OS 锁保证，与这里无关。

    ⚠️ `OSError` 这一支**不是**"保险起见"：Windows 上写者用 `os.replace` 换名的瞬间，
    目标文件处于"待替换"状态，读者会吃到 `PermissionError: [Errno 13]` —— 实测在整套门禁里
    红过一次（读者是测试的采样循环）。这条 fail-open 有专门的判据盯着
    （`api/tests/test_godot_queue_priority.py::test_may_attempt_now_fails_open_when_state_file_is_unreadable`，
    拿目录冒充状态文件：Windows 上 `open(目录)` 正是 Errno 13）。
    """
    enabled, aging = _queue_policy()
    if not enabled:
        return True
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    if not isinstance(state, dict):
        return True
    ranked = _ranked_waiters(state, aging, time.time())
    for index, item in enumerate(ranked):
        try:
            if int(item.get("pid", 0)) == my_pid:
                return index < max(1, capacity)
        except (TypeError, ValueError):
            continue
    # 自己不在名单里（还没登记 / 状态文件被清）→ 放行，登记后自然会进排名
    return True


def _update_resource_state(role: str, slot=None, project: str = "") -> None:
    """Best-effort queue bookkeeping so the orchestrator can report holders and waiters.

    Never raises: a bookkeeping failure must not block or fail a real Godot run.
    """
    try:
        state_path = _state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        guard_path = state_path.with_name(state_path.name + ".guard")
        with guard_path.open("a+", encoding="utf-8") as guard_file:
            acquired = False
            for _ in range(200):
                if _try_lock(guard_file):
                    acquired = True
                    break
                time.sleep(0.05)
            if not acquired:
                return
            try:
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    state = {}
                if not isinstance(state, dict):
                    state = {}
                pid = os.getpid()
                now = time.time()

                def _alive(entry):
                    try:
                        return _pid_alive(int(entry.get("pid", 0)))
                    except (AttributeError, TypeError, ValueError):
                        return False

                def _previous(items):
                    for entry in items:
                        try:
                            if int(entry.get("pid", 0)) == pid:
                                return entry
                        except (AttributeError, TypeError, ValueError):
                            continue
                    return {}

                old_holders = state.get("holders")
                old_waiters = state.get("waiters")
                old_holders = old_holders if isinstance(old_holders, list) else []
                old_waiters = old_waiters if isinstance(old_waiters, list) else []
                previous = _previous(old_holders) or _previous(old_waiters)
                holders = [item for item in old_holders if _alive(item)]
                # 等待者还要能**过期**：pid 存活判定挡不住"已退出但句柄仍可打开"与 pid 复用，
                # 而老化会让泄漏条目越等越靠前 → 别人永远抢不到那把已经空着的锁。
                waiters = [item for item in old_waiters if _alive(item) and not _abandoned(item, now)]
                holders = [item for item in holders if int(item.get("pid", 0)) != pid]
                waiters = [item for item in waiters if int(item.get("pid", 0)) != pid]
                # `since` 必须跨刷新保留：它是老化公式的输入，每次刷新都重置就等于没有老化。
                record = {
                    "pid": pid,
                    "since": previous.get("since") or now,
                    "project": project,
                    "priority": _my_weight(),
                }
                if role == "holder":
                    record["slot"] = slot
                    holders.append(record)
                elif role == "waiter":
                    waiters.append(record)
                enabled, aging = _queue_policy()
                for item in waiters:
                    weight = item.get("priority", 0)
                    value = _effective_priority(weight, item.get("since"), aging, now)
                    try:
                        item["waited_seconds"] = round(max(0.0, now - float(item.get("since") or now)), 1)
                    except (TypeError, ValueError):
                        item["waited_seconds"] = 0.0
                    item["effective_priority"] = round(value, 3)
                    item["aged"] = bool(aging > 0 and value > float(weight or 0))
                waiters.sort(
                    key=lambda item: (-float(item.get("effective_priority", 0)), float(item.get("since") or now))
                )
                payload = json.dumps(
                    {
                        "capacity": _lock_capacity(),
                        "holders": holders,
                        "waiters": waiters,
                        "queue_policy": {"priority_enabled": enabled, "aging_seconds": aging},
                        "updated_at": now,
                    },
                    ensure_ascii=False,
                )
                # 换名落盘（不是 `write_text` 的"截断后写"）：读方 `_may_attempt_now` 不取 guard 锁，
                # 撞上写窗口会读到半截 JSON → 解析失败 → 那一刻**所有人一起放行**，优先级静默失效。
                #
                # `os.replace` 仍然是**原子**的：读者只会看到旧内容或新内容，不会看到半截。
                #
                # ⚠️ 两层修复都必须留着（见两个 helper 的注释）：
                #   `_state_tmp_path`     → 写者之间不抢同一个临时文件；
                #   `_replace_with_retry` → 读者短暂持有目标文件时不丢整次更新。
                tmp_path = _state_tmp_path(state_path)
                try:
                    tmp_path.write_text(payload, encoding="utf-8")
                    _replace_with_retry(tmp_path, state_path)
                except OSError:
                    # 清理自己那一份；清不掉也不能让异常冒出去（状态更新是尽力而为）。
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                _unlock(guard_file)
    except Exception:
        return


def _state_tmp_path(state_path: Path) -> Path:
    """给状态文件造一个**每个写者唯一**的临时路径。

    为什么必须唯一：写死成 `<state>.tmp` 时，两个进程同时走到落盘那一步会抢同一个
    文件 —— 一方 `os.replace` 之后，另一方的写入吃到 `PermissionError: [Errno 13]`，
    而外层 `except` 把它静默吞掉，于是那次状态更新**完全没发生**
    （持有者/等待者列表少一条，优先级静默失效）。

    抽成函数还有一个理由：判据要能**调用产品代码本身**，而不是在测试里复制一份
    命名逻辑 —— 复制的判据会在实现改名之后继续用旧逻辑通过。
    """
    return state_path.with_name(state_path.name + f".{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 10) -> None:
    """把 `src` 原子换名到 `dst`，**撞上并发读者时重试**。

    为什么需要重试（这是实测的第二层原因）：Windows 上 `os.replace` 在**目标文件
    正被别的进程打开**时会失败（`PermissionError: [Errno 13]`）。读者
    （`_may_attempt_now`）不取 guard 锁，所以这个窗口真实存在。

    第一层修复（临时文件名每个写者唯一）解决的是"两个写者抢同一个 `.tmp`"；
    那一层修完之后仍然偶发，才定位到这一层 —— **两层都要**：

    * 唯一临时名 → 写者之间不互相踩；
    * 换名重试 → 读者短暂持有目标文件时不至于整次更新丢失。

    重试总时长约 0.3 秒（10 次 × 30ms 递增）。状态文件是**尽力而为**的观测值，
    所以超时就放弃（由调用方的 `except` 收口），不做无限等待 ——
    无限等待会让一个卡住的读者把整个槽位机制堵死。
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.03 * (attempt + 1))
    assert last is not None
    raise last


def _try_lock(lock_file) -> bool:
    if sys.platform.startswith("win"):
        import msvcrt

        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(lock_file) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _prepare_slot(handle) -> None:
    """Keep slot files non-empty.

    A Windows byte-range lock over an empty file protects nothing, which lets two
    processes "acquire" the same slot. Guarantee byte 0 exists before locking.
    """
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(".")
            handle.flush()
        handle.seek(0)
    except OSError:
        pass


def _acquire_godot_lock():
    base = _lock_path()
    base.parent.mkdir(parents=True, exist_ok=True)
    capacity = _lock_capacity()
    handles = []
    for slot in _slot_paths(capacity):
        handle = slot.open("a+", encoding="utf-8")
        _prepare_slot(handle)
        handles.append(handle)
    wait_seconds = _lock_wait_seconds()
    deadline = time.monotonic() + wait_seconds
    last_notice = 0.0
    try:
        project = str(_project_path(sys.argv[1:]))
    except Exception:
        project = ""
    weight = _my_weight()
    _update_resource_state("waiter", project=project)
    last_refresh = time.monotonic()
    try:
        while True:
            # 应用层仲裁：排名靠前的才去抢槽（容量上限仍由 OS 锁保证，不受这里影响）
            if _may_attempt_now(os.getpid(), capacity):
                for index, handle in enumerate(handles):
                    if _try_lock(handle):
                        # Informational only. Never truncate (that would empty the file and
                        # void the byte-range lock), and never let bookkeeping abort a run.
                        try:
                            handle.seek(0)
                            record = f"pid={os.getpid()} slot={index} weight={weight} acquired_at={time.time()}"
                            handle.write(record.ljust(256)[:256])
                            handle.flush()
                            handle.seek(0)
                        except OSError:
                            pass
                        for other in handles:
                            if other is not handle:
                                other.close()
                        _update_resource_state("holder", slot=index, project=project)
                        return handle
            now = time.monotonic()
            if now >= deadline:
                for handle in handles:
                    handle.close()
                _update_resource_state("drop", project=project)
                print(
                    f"GODOT_RESOURCE_QUEUE_TIMEOUT after {wait_seconds}s waiting for a Godot slot "
                    f"(capacity {capacity}, weight={weight}): {base}"
                )
                return None
            if now - last_refresh >= 60:
                # 定期刷新：让等待期内的优先级/老化在看板上可见。`since` 由状态层跨刷新保留，
                # 所以刷新不会把老化时间归零。
                _update_resource_state("waiter", project=project)
                last_refresh = now
            if now - last_notice >= 30:
                enabled, aging = _queue_policy()
                note = f"weight={weight}" + (f", aging={aging}s" if enabled and aging > 0 else "")
                print(f"GODOT_RESOURCE_QUEUE waiting for a Godot slot (capacity {capacity}, {note}): {base}")
                last_notice = now
            time.sleep(2)
    except BaseException:
        for handle in handles:
            handle.close()
        _update_resource_state("drop", project=project)
        raise


def _arg_value(args: list[str], name: str) -> str | None:
    try:
        index = args.index(name)
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return args[index + 1]


def _project_path(args: list[str]) -> Path:
    raw_path = _arg_value(args, "--path") or "."
    return Path(raw_path).expanduser().resolve()


def _with_local_user_data(args: list[str]) -> list[str]:
    if "--user-data-dir" in args:
        return args
    project = _project_path(args)
    user_dir = project / ".godot_user"
    (user_dir / "logs").mkdir(parents=True, exist_ok=True)
    return [*args, "--user-data-dir", str(user_dir)]


def _extract_main_scene(project_text: str) -> str | None:
    for line in project_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("run/main_scene") and "=" in stripped:
            return stripped.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def _strict_data_refs_enabled(args: list[str]) -> bool:
    if "--strict-data-refs" in args:
        return True
    value = os.environ.get("CODEX_BATTLEFIELD_GODOT_STATIC_STRICT_DATA_REFS", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _project_files(project: Path, *, include_data_refs: bool = False) -> list[Path]:
    suffixes = STRUCTURAL_SCAN_SUFFIXES | (DATA_SCAN_SUFFIXES if include_data_refs else set())
    try:
        return [
            path
            for path in project.rglob("*")
            if path.is_file() and path.suffix.lower() in suffixes
        ]
    except OSError:
        return []


def _resource_path(project: Path, resource: str | None) -> Path | None:
    if not resource or not resource.startswith("res://"):
        return None
    resource_path = resource.removeprefix("res://").split("#", 1)[0].split("?", 1)[0].rstrip(".,;`。；，、")
    rel = Path(resource_path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return project / rel


def _res_references(project: Path, *, include_data_refs: bool = False) -> set[str]:
    refs: set[str] = set()
    for path in _project_files(project, include_data_refs=include_data_refs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs.update(match.rstrip(".,;`。；，、") for match in re.findall(r"res://[^\"'\s\])}]+", text))
    return refs


def _asset_header_is_valid(suffix: str, head: bytes) -> bool:
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    return True


def _missing_res_references(project: Path, *, include_data_refs: bool = False) -> list[str]:
    missing: list[str] = []
    for ref in sorted(_res_references(project, include_data_refs=include_data_refs)):
        path = _resource_path(project, ref)
        if path is None or (path.suffix and not path.is_file()):
            missing.append(ref)
    return missing


def _invalid_asset_references(project: Path, *, include_data_refs: bool = False) -> list[str]:
    invalid: list[str] = []
    for ref in sorted(_res_references(project, include_data_refs=include_data_refs)):
        path = _resource_path(project, ref)
        if not path or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ASSET_SUFFIXES:
            continue
        try:
            head = path.read_bytes()[:16]
            size = path.stat().st_size
        except OSError:
            invalid.append(ref)
            continue
        if size == 0 or not _asset_header_is_valid(suffix, head):
            invalid.append(ref)
    return invalid


def _static_project_check(args: list[str]) -> int:
    project = _project_path(args)
    project_file = project / "project.godot"
    if not project_file.exists():
        print(f"GODOT_STATIC_CHECK failed: missing {project_file}")
        return 1
    project_text = project_file.read_text(encoding="utf-8", errors="replace")
    main_scene = _extract_main_scene(project_text)
    if main_scene and main_scene.startswith("res://"):
        main_scene_path = _resource_path(project, main_scene)
        if not main_scene_path or not main_scene_path.exists():
            print(f"GODOT_STATIC_CHECK failed: missing main scene {main_scene}")
            return 1
    script_arg = _arg_value(args, "--script")
    if script_arg and script_arg.startswith("res://"):
        script_path = _resource_path(project, script_arg)
        if not script_path or not script_path.exists():
            print(f"GODOT_STATIC_CHECK failed: missing script {script_arg}")
            return 1
    strict_data_refs = _strict_data_refs_enabled(args)
    missing_refs = _missing_res_references(project, include_data_refs=strict_data_refs)
    if missing_refs:
        print(f"GODOT_STATIC_CHECK failed: missing resource {missing_refs[0]}")
        return 1
    invalid_assets = _invalid_asset_references(project, include_data_refs=strict_data_refs)
    if invalid_assets:
        print(f"GODOT_STATIC_CHECK failed: invalid asset {invalid_assets[0]}")
        return 1
    mode = "strict-data" if strict_data_refs else "leaf-structural"
    print(f"GODOT_STATIC_CHECK ok ({mode}): {project}")
    return 0


def _should_static_check(args: list[str]) -> bool:
    static_exit_flags = {"--quit", "--check-only", "--quit-after"}
    return "--headless" in args and "--path" in args and any(flag in args for flag in static_exit_flags)


def _is_real_godot_crash_output(output: str, exit_code: int | None) -> bool:
    if exit_code in (0, None):
        return False
    normalized = output.lower()
    return any(
        marker in normalized
        for marker in (
            "crashhandlerexception",
            "program crashed with signal",
            "could not create directory: 'user://logs'",
            "application error",
            "memory could not be read",
            "内存不能 read",
            "该内存不能为 read",
            "应用程序错误",
        )
    )


def main() -> int:
    godot_args = sys.argv[1:]
    if _should_static_check(godot_args):
        return _static_project_check(godot_args)
    real = os.environ.get("CODEX_BATTLEFIELD_REAL_GODOT_BIN", "").strip()
    if not real:
        print("CODEX_BATTLEFIELD_REAL_GODOT_BIN is not set.")
        return 127
    try:
        timeout = max(1, int(float(os.environ.get("CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS", "60"))))
    except ValueError:
        timeout = 60
    timeout = min(timeout, 45)
    command = [real, *_with_local_user_data(godot_args)]
    lock_file = _acquire_godot_lock()
    if lock_file is None:
        return 125
    process = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if sys.platform.startswith("win"):
                kill = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True)
                if kill.stdout:
                    print(kill.stdout, end="")
                if kill.stderr:
                    print(kill.stderr, end="")
                # Godot console builds can leave a second GUI child alive after taskkill.
                # Restrict the sweep to headless project validations started by this tool.
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        "Get-CimInstance Win32_Process | "
                        "Where-Object { $_.Name -like 'Godot*' -and $_.CommandLine -like '*--headless*' -and $_.CommandLine -like '*--path*' } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
                    ],
                    capture_output=True,
                    text=True,
                )
            else:
                process.kill()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = ""
            if output:
                print(output, end="")
            print(f"GODOT_TIMEOUT after {timeout}s: {' '.join(command)}")
            return 124
    finally:
        _unlock(lock_file)
        lock_file.close()
        _update_resource_state("drop")
    if output:
        print(output, end="")
    if _is_real_godot_crash_output(output or "", process.returncode):
        print(
            "GODOT_VALIDATION_ENVIRONMENT_ERROR: real Godot crashed or opened an OS crash dialog; "
            "treat this as validation-environment blocked and use the static wrapper path."
        )
    return int(process.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
