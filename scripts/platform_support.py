"""跨平台的**可执行文件与解释器解析**（P6-2）。

## 为什么需要它

仓库里散落着这种写法：

```python
print("用 api 的 venv 跑：api\\.venv\\Scripts\\python.exe", file=sys.stderr)
r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
```

它们在 Windows 上正确，在 Linux/macOS 上**只是打印一句帮不上忙的话**，或者让脚本直接
找不到浏览器。这类问题不会在 Windows 上暴露，而 CI 是双平台的（P6-3），
所以在这里收成两个函数：**只有一份实现**，谁需要就调。

## 判据的边界（写在代码里）

* 解析器**不猜**：找不到就返回 `None`。调用方自己决定是报错、降级还是继续。
  返回一个不存在的路径会让失败推迟到真正用到它的时候 —— 那时更难归因。
* 这些函数只做"路径查找"，**不验证能不能跑**（那要在真机上跑才知道）。
  Windows 上验证过的只有"选中的是 venv 里那个"；Linux 分支在本机**无法实测**，
  只能靠 CI（见 `docs/cross-platform.md`）。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Iterable


def is_windows() -> bool:
    """当前平台是不是 Windows。

    ⚠️ 平台分派**不要靠改写这个函数的结果**来测另一个平台：第一版写过一个
    `pretend_platform` 上下文管理器去改 `os.name`/`sys.platform`，结果
    `pathlib` 跟着换成 `PosixPath`，在 Windows 上直接
    `NotImplementedError: cannot instantiate 'PosixPath'`（实测）。
    现在改成**参数**（`venv_python_candidates(windows=...)` /
    `browser_candidates(windows=...)`）：测试传参，全局一个字节都不动。
    """

    return sys.platform.startswith("win")


def repo_root() -> Path:
    """仓库根（`scripts/` 的上一级）。"""

    return Path(__file__).resolve().parents[1]


def venv_python_candidates(root: Path | None = None, *, windows: bool | None = None) -> tuple[Path, ...]:
    """`api/.venv` 里解释器的**可能位置**，按优先顺序。

    Windows 的 venv 是 `Scripts\\python.exe`，Unix 是 `bin/python` ——
    这是 venv 的约定，不是我们的选择，所以两边都要列。

    `windows` 可以显式传：测试要验"两个平台各自选哪一支"，而**不能**靠改写
    `sys.platform` —— 那会让 `pathlib` 换成 `PosixPath`，
    在 Windows 上直接 `NotImplementedError: cannot instantiate 'PosixPath'`
    （实测踩过）。
    """

    base = (root or repo_root()) / "api" / ".venv"
    use_windows = is_windows() if windows is None else windows
    windows_paths = (base / "Scripts" / "python.exe", base / "Scripts" / "python")
    posix_paths = (base / "bin" / "python", base / "bin" / "python3")
    return windows_paths + posix_paths if use_windows else posix_paths + windows_paths


def venv_python(root: Path | None = None) -> Path | None:
    """仓库 venv 里的解释器；不存在返回 `None`（**不猜**）。"""

    for candidate in venv_python_candidates(root):
        if candidate.is_file():
            return candidate
    return None


def python_executable(root: Path | None = None) -> str:
    """要用来跑"同仓库其他脚本"的解释器：优先 venv，否则当前解释器。

    返回**当前解释器**而不是字面量 `"python3"`：调用方多半已经在用某个解释器跑，
    凭空换成另一个可能连依赖都没有。`None` 风险为零、且行为可预期。
    """

    found = venv_python(root)
    if found is not None:
        return str(found)
    return sys.executable


def browser_candidates(*, windows: bool | None = None) -> tuple[str, ...]:
    """Chrome/Chromium/Edge 的候选位置（**按平台**）。

    Unix 一侧用"可执行名"交给 `shutil.which` 解析，Windows 一侧要写死安装路径
    （Windows 上没有稳定的可执行名）。

    `windows` 可显式传，理由同 `venv_python_candidates`（不要改写 `sys.platform`）。
    """

    use_windows = is_windows() if windows is None else windows
    if use_windows:
        return (
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            "chrome",
            "msedge",
        )
    return (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        # macOS 的 .app 包（which 找不到，要显式给路径）
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    )


def find_browser(candidates: Iterable[str] | None = None) -> str | None:
    """第一个**存在**的浏览器路径；都没有返回 `None`（不猜、不报假路径）。"""

    for candidate in candidates if candidates is not None else browser_candidates():
        if os.path.isabs(candidate):
            if Path(candidate).is_file():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def path_list_separator() -> str:
    """`PATH` 的分隔符。写死 `;` 或 `:` 都会在另一个平台上把 PATH 拆错。"""

    return os.pathsep


def child_env_with_path(entry: str | os.PathLike[str], env: dict[str, str] | None = None) -> dict[str, str]:
    """把 `entry` 加到 `PATH` 最前面（**用对分隔符**）。"""

    result = dict(os.environ if env is None else env)
    current = result.get("PATH", "")
    result["PATH"] = f"{entry}{path_list_separator()}{current}" if current else str(entry)
    return result
