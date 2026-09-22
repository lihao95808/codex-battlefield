import atexit
import os
import pathlib
import tempfile

# The machine safety guard compares host free memory against a fixed threshold. On a host with
# less free RAM than the production default the guard fires during mock tests and hibernates the
# tree, which makes the suite depend on the machine it runs on. Disable it by default here; tests
# that actually exercise the guard opt in by passing machine_safety_min_available_memory_mb
# explicitly.
os.environ.setdefault("CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB", "0")

# 测试库隔离：每个 pytest 进程用**自己**的 SQLite 文件。
#
# 原先各测试模块在导入时把 CODEX_BATTLEFIELD_DATABASE_URL 写死成同一个
# `%TEMP%/codex_battlefield_test.sqlite3`，于是——
#   1. 上一次运行被 Ctrl-C / 超时 / 强杀后留下的 `-wal`、`-shm` 或残留锁，会让**下一次运行直接挂住**：
#      不报错，卡在第一个用例上不动（干净克隆里复现：整批 682 个用例 300 秒零进展；删掉那个文件后
#      同一批 33 秒跑完）；
#   2. 两个终端同时跑测试会互相踩，表现为大批互不相关的用例集体失败。
# 按进程号分文件后两类问题都不存在。
#
# 必须写在 conftest 里：pytest 保证 conftest 先于任何测试模块导入，而 `app.database` 在导入时
# 就读这个环境变量，晚一步设就没用了。
_DB_PATH = pathlib.Path(tempfile.gettempdir()) / f"codex_battlefield_test_{os.getpid()}.sqlite3"
os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"


@atexit.register
def _remove_session_database() -> None:
    """退出时清掉本次会话的库（含 WAL/SHM），不给下一次留残留。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(f"{_DB_PATH}{suffix}").unlink()
        except OSError:
            pass
