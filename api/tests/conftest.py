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

# 状态根隔离：**不能让测试读到开发机真实的状态根**。
#
# 为什么必须隔离（2026-10-14 实测）：真状态根 `~/.codex-battlefield/` 里可能有
# `active.json`，声明"本机当前绑定某个工程"。`Settings.__post_init__` 会读它，
# 于是测试里 `get_settings().__class__(...)` 造出来的 settings **继承了那个绑定**，
# `machine_root` 就指向真状态根而不是 tmp_path —— 具体表现是
# 21 个用例在并行全量时失败、单独跑却通过（典型的环境泄漏）。
#
# 指到一个每次会话独立的临时目录后，测试既不读、也不写开发机的真实状态。
_STATE_HOME = pathlib.Path(tempfile.gettempdir()) / f"codex_battlefield_state_test_{os.getpid()}"
os.environ["CODEX_BATTLEFIELD_STATE_HOME"] = str(_STATE_HOME)
# 同理：显式绑定的环境变量也要清掉，否则同样会把开发机的工程带进来。
os.environ.pop("CODEX_BATTLEFIELD_PROJECT_KEY", None)

# 工作区隔离：未绑定时的兜底目录也别落在 checkout 里。
#
# 为什么必须隔离（2026-10-15 实测）：测试会建**真实的 git worktree**
# （`git worktree add` 会在仓库里注册），落在 `<repo>/workspace/worktrees/`
# 就会往 checkout 里堆垃圾；目录删掉后还留下**悬空注册 + 孤儿分支** ——
# 实测跑一次 `verify_all` 就多出 2 个 worktree 注册和 2 个分支。
_STATE_WORKSPACE = _STATE_HOME / "workspace"
os.environ["CODEX_BATTLEFIELD_WORKSPACE"] = str(_STATE_WORKSPACE)

# runner 模式：测试**必须**是 mock。
#
# 为什么不能只靠内置默认值（2026-10-15 实测，53 个用例集体失败）：
# `runner_mode` 现在也能从用户配置文件读（那是为了 GUI 启动时不用带环境变量）。
# 开发机配置里一旦写了 `runner_mode = "real"`，测试就会**真的去调模型**，
# 于是大批与模式相关的用例失败、还白花钱。
# 显式钉住 mock，让测试既不依赖开发机配置、也不会误花钱。
os.environ["CODEX_BATTLEFIELD_RUNNER"] = "mock"


# 用户配置文件隔离：**测试不能读开发机的 `~/.codex-battlefield.toml`**。
#
# 为什么必须隔离（2026-10-15 实测，5 个用例失败）：`codex_sandbox_*` 等档位现在
# 也能从用户配置读（那是为了"看门狗重启 API 时不丢配置"）。于是开发机配置里一句
# `codex_sandbox_worker = "danger-full-access"`，就让
# `test_sandbox_modes.py::test_s1_default_is_returned_untouched` 报
# `assert 'danger-full-access' == 'workspace-write'` —— 它测的是**内置默认**，
# 却读到了开发机的配置。这与 `CODEX_BATTLEFIELD_STATE_HOME` 当初那类泄漏同源：
# 测试结果取决于它在哪台机器上跑。
#
# 指到一个"保证不存在"的路径即可：读不到配置 -> 全部走内置默认，
# 这正是这些用例想测的东西。
_CONFIG_PATH = pathlib.Path(tempfile.gettempdir()) / f"codex_battlefield_test_config_{os.getpid()}.toml"
os.environ["CODEX_BATTLEFIELD_CONFIG"] = str(_CONFIG_PATH)


@atexit.register
def _remove_session_database() -> None:
    """退出时清掉本次会话的库（含 WAL/SHM），不给下一次留残留。"""
    for suffix in ("", "-wal", "-shm"):
        try:
            pathlib.Path(f"{_DB_PATH}{suffix}").unlink()
        except OSError:
            pass
    try:
        _CONFIG_PATH.unlink()
    except OSError:
        pass
