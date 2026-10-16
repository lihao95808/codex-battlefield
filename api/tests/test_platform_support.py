"""P6-2：跨平台解析（判据 L1–L9）。

## 这一组能证明什么、不能证明什么

**能证明**（本机就能）：

* 平台分派正确 —— Windows 选 `Scripts/python.exe`、Unix 选 `bin/python`
  （平台是**参数**，测试传参即可，不改全局）；
* 找不到就返回 `None`，**不返回一个不存在的路径**（那会把失败推迟到更难归因的地方）；
* 浏览器候选位置按平台给，Unix 侧用可执行名交给 `which`；
* `PATH` 用 `os.pathsep` 拼（写死 `;` 或 `:` 都会在另一个平台上拆错）；
* 仓库里**没有第二份**手写的 Windows 路径清单（防止修一处、别处又长出来）。

**不能证明**（如实写在 `docs/cross-platform.md`）：

* Linux/macOS 上真的跑得起来（本机没有 Linux/WSL，只能靠 P6-3 的双平台 CI）；
* 真实的路径分隔符、权限、浏览器安装位置在另一个平台上的行为。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def _load_platform_support():
    path = SCRIPTS / "platform_support.py"
    spec = importlib.util.spec_from_file_location("platform_support_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ps = _load_platform_support()


# --- L1/L2 平台分派 ----------------------------------------------------------


def test_l1_windows_prefers_scripts_python():
    candidates = ps.venv_python_candidates(windows=True)
    assert str(candidates[0]).replace("\\", "/").endswith("api/.venv/Scripts/python.exe")


def test_l2_posix_prefers_bin_python():
    candidates = ps.venv_python_candidates(windows=False)
    first = str(candidates[0]).replace("\\", "/")
    assert first.endswith("api/.venv/bin/python"), first
    # 另一边的候选仍然在列表里（venv 的布局是约定，不是"猜一个"）。
    assert any("Scripts/python.exe" in str(item).replace("\\", "/") for item in candidates)


def test_l2_both_orders_cover_both_layouts():
    windows = {str(item).replace("\\", "/") for item in ps.venv_python_candidates(windows=True)}
    posix = {str(item).replace("\\", "/") for item in ps.venv_python_candidates(windows=False)}
    assert windows == posix, "两边的候选集合必须一致，只有顺序不同"


def test_l2_platform_switch_does_not_rewrite_sys_platform():
    """判据本身要防一件事：**不要靠改写 `sys.platform` 来测另一个平台**。

    实测踩过：把 `sys.platform` 改成 `"linux"` 之后 `pathlib` 换成 `PosixPath`，
    在 Windows 上直接 `NotImplementedError: cannot instantiate 'PosixPath'`。
    所以平台分派是**参数**，测试只传参、不动全局。
    """

    before = sys.platform
    ps.venv_python_candidates(windows=False)
    ps.browser_candidates(windows=False)
    assert sys.platform == before


# --- L3 不猜 -----------------------------------------------------------------


def test_l3_venv_python_returns_none_when_absent(tmp_path):
    assert ps.venv_python(tmp_path) is None


def test_l3_venv_python_finds_the_real_interpreter():
    found = ps.venv_python(REPO)
    assert found is not None, "仓库里应当有 api/.venv（没有的话这条判据要跳过而不是假装通过）"
    assert found.is_file()


def test_l3_python_executable_falls_back_to_the_current_interpreter(tmp_path):
    # 没有 venv 时不许编一个路径：退回"当前解释器"（行为可预期、依赖一定在）。
    assert ps.python_executable(tmp_path) == sys.executable


def test_l3_python_executable_prefers_the_venv():
    found = ps.ps_python = ps.python_executable(REPO)  # type: ignore[attr-defined]
    assert Path(found).is_file()
    assert "venv" in found.lower()


# --- L4/L5 浏览器候选 --------------------------------------------------------


def test_l4_windows_candidates_are_absolute_install_paths():
    candidates = ps.browser_candidates(windows=True)
    absolute = [item for item in candidates if os.path.isabs(item)]
    assert absolute, "Windows 上没有稳定的可执行名，必须给安装路径"
    assert all(item.lower().endswith((".exe",)) for item in absolute)
    # 也留了可执行名（用户把 chrome 加了 PATH 的情况）。
    assert any(not os.path.isabs(item) for item in candidates)


def test_l5_posix_candidates_use_executable_names():
    candidates = ps.browser_candidates(windows=False)
    names = [item for item in candidates if not os.path.isabs(item)]
    assert "google-chrome" in names
    assert "chromium" in names
    # macOS 的 .app 包 which 找不到，必须显式给路径。
    assert any(item.endswith(".app/Contents/MacOS/Google Chrome") for item in candidates)
    # Windows 的安装路径**不该**出现在 Unix 候选里。
    assert not any("Program Files" in item for item in candidates)


def test_l5_find_browser_returns_none_when_nothing_matches(tmp_path):
    assert ps.find_browser(["definitely-not-a-browser-xyz"]) is None
    assert ps.find_browser([str(tmp_path / "nope.exe")]) is None


def test_l5_find_browser_finds_a_real_absolute_path(tmp_path):
    fake = tmp_path / "fake-browser"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    assert ps.find_browser([str(fake)]) == str(fake)


# --- L6 PATH 拼接 ------------------------------------------------------------


def test_l6_path_separator_comes_from_os():
    assert ps.path_list_separator() == os.pathsep


def test_l6_child_env_puts_the_entry_first():
    env = ps.child_env_with_path("/some/bin", {"PATH": "/usr/bin"})
    assert env["PATH"] == f"/some/bin{os.pathsep}/usr/bin"


def test_l6_child_env_handles_an_empty_path():
    env = ps.child_env_with_path("/some/bin", {})
    assert env["PATH"] == "/some/bin", "空 PATH 时不该留下一个悬空的分隔符"


# --- L7 只有一份实现 --------------------------------------------------------


#: 允许出现 Windows 路径字面量的脚本 → 理由。**刻意写得短**：多一条就要在这里解释。
WINDOWS_PATHS_ALLOWED = {
    "platform_support.py": "跨平台解析器的唯一实现，Windows 候选位置就写在这里",
    "bench_supervise.py": "py-spy / 符号服务器是 Windows 专用的诊断路径，按平台分支",
    "scan_history_privacy.py": "隐私扫描的正则**必须**认识 Windows 用户目录（那是要抓的目标）",
}

#: 只在**说明文字**里出现 Windows 路径不算违规（那是帮用户照抄的例子）。
#: 判据盯的是**可执行的安装路径模式**，见下面两条 assert。
INSTALL_PATH_PATTERNS = ("Program Files\\\\Google", "Program Files\\\\Microsoft", "Program Files (x86)\\\\Google")


def test_l7_windows_path_literals_live_in_one_place():
    offenders: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name in WINDOWS_PATHS_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if any(pattern in text for pattern in INSTALL_PATH_PATTERNS):
            offenders.append(path.name)
    assert offenders == [], (
        "这些脚本里又长出了 Windows 安装路径字面量："
        f"{offenders}。请改用 scripts/platform_support.py —— 否则修一处、别处还会长出来"
    )


def test_l7_browser_script_uses_the_shared_candidates():
    text = (SCRIPTS / "verify_browser_console.py").read_text(encoding="utf-8")
    assert "browser_candidates()" in text
    # 判据放在**安装路径模式**上，而不是"出现过 Program Files 这个词"：
    # 后者连"提示用户时举的例子"都会误报（实测踩过）。
    assert "Program Files\\\\Google" not in text, "候选清单不该再抄一份"
    assert "Program Files\\\\Microsoft" not in text, "候选清单不该再抄一份"
