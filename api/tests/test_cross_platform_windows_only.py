"""P6-2 深挖：Windows-only 调用点必须**要么有平台守卫、要么在允许清单里**（判据 W1–W6）。

## 为什么加这一组

`docs/cross-platform.md` 记着"没有在 Linux 上跑过任何东西"。为了把这句话变得**可反驳**
（而不是永远停留在"我们不知道"），这里做一件不需要 Linux 也能做的事：

> 静态扫出所有 **Windows-only 的系统调用**（`powershell.exe` / `taskkill` / `msvcrt` /
> `reg` / `cmd.exe`），逐个要求它**要么被平台守卫包住，要么进允许清单并写清理由**。

扫描第一次跑就抓到两处**无守卫**的 `powershell.exe`（`install_watchdog_hook` 与
`run_watchdog_once`）—— 在 Linux 上会抛 `FileNotFoundError: powershell.exe`，
用户看到的是一句"找不到文件"而不是"这个功能只有 Windows 有"。已修成显式的
`RuntimeError` + 可行动的提示。

## 它证明什么、不证明什么

* **证明**：没有"裸调用 Windows 命令"的路径。这在**任何**平台上都能验，而且能红。
* **不证明**：整条链路在 Linux 上能跑通（worker 提示词里仍假设 PowerShell、
  看门狗仍是 PowerShell 脚本）。这些是**已知的产品级边界**，写在
  `docs/cross-platform.md` §5，不在这一组里假装解决。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_DIRS = (REPO / "api" / "app", REPO / "scripts")

#: 值得扫的 Windows-only 调用/导入。**窄**是刻意的：清单越长，误报越多，越容易被忽略。
WINDOWS_ONLY = {
    "powershell.exe": re.compile(r"""["']powershell\.exe["']"""),
    "taskkill": re.compile(r"""["']taskkill["']"""),
    "msvcrt": re.compile(r"^\s*import msvcrt", re.MULTILINE),
    "reg": re.compile(r"""["']reg(\.exe)?["']"""),
    "cmd": re.compile(r"""["']cmd(\.exe)?["']"""),
}

#: 平台守卫的写法（往上看 30 行，命中任一即算有守卫）。
#:
#: ⚠️ 两个都踩过的坑：
#: 1. **不要把 `_powershell_exe` 放进来** —— 第一版放进去之后，W2 把"定义那个方法"的
#:    调用行当成了守卫，变异测试里换成裸 `"powershell.exe"` 也照样绿（判据空转）；
#: 2. **`!= "nt"` 也算守卫** —— `_powershell_exe()` 内部就是"非 Windows 就报错"，
#:    这是**反向**守卫，只写 `== "nt"` 会把它误判成没守卫。
GUARD = re.compile(r"os\.name\s*(?:==|!=)\s*[\"']nt[\"']|is_windows\(\)|sys\.platform")

#: 允许清单：**没有**守卫但确实只会在 Windows 上走到的地方 → 文件:行号附近的理由。
#: 每条都要能说清"为什么这样是安全的"。
ALLOWED_WITHOUT_GUARD: dict[str, str] = {}


def _call_sites_in(path: Path) -> list[tuple[int, str, str]]:
    """单个文件里的 Windows-only 调用点：`(行号, 名字, 该行)`。"""

    found: list[tuple[int, str, str]] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        for name, pattern in WINDOWS_ONLY.items():
            if pattern.search(line):
                found.append((index + 1, name, line))
    return found


def _call_sites() -> list[tuple[Path, int, str, str]]:
    """扫出所有 Windows-only 调用点：`(文件, 行号, 名字, 该行)`。"""

    found: list[tuple[Path, int, str, str]] = []
    for directory in SCAN_DIRS:
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue  # 临时脚本不参与
            for line_number, name, line in _call_sites_in(path):
                found.append((path, line_number, name, line))
    return found


def _enclosing_function(module_ast: "object", line_number: int) -> tuple[int, int] | None:
    """`line_number` 落在哪个函数里 → `(起行, 止行)`；不在任何函数里返回 `None`。"""

    import ast

    best: tuple[int, int] | None = None
    for node in ast.walk(module_ast):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = node.lineno
        # `ast.arguments` 之类没有 `end_lineno`（实测踩过），所以逐个取并跳过缺失的。
        ends = [getattr(child, "end_lineno", None) or getattr(child, "lineno", None) for child in ast.walk(node)]
        end = max((value for value in ends if value is not None), default=start)
        if start <= line_number <= end and (best is None or start > best[0]):
            best = (start, end)
    return best


def _is_guarded(path: Path, line_number: int) -> bool:
    """这一行的调用**在语法上**被平台守卫包住了吗？

    ⚠️ 判据第一版是"往上看 30 行里有没有守卫字样"，**变异测试证明它在空转**：
    把调用换成裸 `"powershell.exe"` 之后仍然绿 —— 因为 30 行窗口**跨过了函数边界**，
    把隔壁 `_powershell_exe()` 里的守卫当成了自己的。

    现在分两步，都在**同一个函数体内**看：
    1. 若存在"提前返回/抛错"形式的反向守卫（`if os.name != "nt": raise/return`），
       那么它**之后**的代码都算被守卫（`_powershell_exe` 就是这种形状）；
    2. 否则要求函数体里出现 `if os.name == "nt"` / `is_windows()` / `sys.platform`。
       **注意**：这条比"守卫真的包住这一行"更宽（`if nt: A` 之后的无条件 `B` 也算过），
       对当前这批调用点足够；要收紧就得引入 CFG，那是另一个量级的复杂度 ——
       这里把已知的宽松之处写出来，而不是假装精确。
    """

    import ast

    source = path.read_text(encoding="utf-8")
    lines = source.split("\n")
    tree = ast.parse(source)

    if line_number in _string_literal_lines(path):
        return True  # 只是文案，不算调用点（由 `_real_call_sites` 过滤，这里兜底）

    bounds = _enclosing_function(tree, line_number)
    if bounds is None:
        # 模块级代码：只看这一行之前（没有函数边界可依）。
        context = "\n".join(lines[: line_number - 1])
        return bool(GUARD.search(context))

    start, _end = bounds
    body = "\n".join(lines[start - 1 : line_number - 1])

    # 1) 反向守卫：`if os.name != "nt": <raise|return>` —— 之后的代码都受它保护。
    for match in re.finditer(r"if\s+os\.name\s*!=\s*[\"']nt[\"']\s*:", body):
        tail = body[match.end() :]
        if re.match(r"\s*\n\s*(raise|return)\b", tail):
            return True

    # 2) 正向守卫（宽松，见 docstring）。
    return bool(GUARD.search(body))


#: 判断一次命中是不是"真的在调它"：`"powershell.exe"` 出现在**字符串字面量**里
#: 不算（例如错误消息、docstring）—— 用 `tokenize` 找长字符串，把它们的位置记下来。
def _string_literal_lines(path: Path) -> set[int]:
    import tokenize

    literal_lines: set[int] = set()
    try:
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type == tokenize.STRING and token.start[0] != token.end[0]:
                    # 多行字符串（docstring / 长消息）：整段都算字面量。
                    literal_lines.update(range(token.start[0], token.end[0] + 1))
    except (OSError, tokenize.TokenError):  # pragma: no cover - 语法坏掉的脚本不该让判据崩
        return set()
    return literal_lines


#: 允许清单：**没有**守卫但确实只会在 Windows 上走到的地方 → 文件:行号附近的理由。
#: 每条都要能说清"为什么这样是安全的"。
ALLOWED_WITHOUT_GUARD: dict[str, str] = {}


def _real_call_sites() -> list[tuple[Path, int, str, str]]:
    """在 `_call_sites()` 基础上剔掉"只出现在多行字符串里"的命中。

    实测踩过：`_powershell_exe()` 的**错误消息**里写着 `"powershell.exe"`（解释现象用），
    结果 W2 把它当成一个未守卫的调用点，判据永远红。**判据要能区分"代码"与"文案"**。
    """

    sites: list[tuple[Path, int, str, str]] = []
    for path, line_number, name, text in _call_sites():
        if line_number in _string_literal_lines(path):
            continue
        sites.append((path, line_number, name, text))
    return sites


def test_w1_scan_actually_finds_something():
    """判据不能空转：仓库里**确实**有 Windows-only 调用（否则这一组在测空气）。"""

    sites = _call_sites()
    names = {name for _path, _line, name, _text in sites}
    assert "taskkill" in names, "至少应当扫到 taskkill（它确实存在）"
    assert "msvcrt" in names, "至少应当扫到 msvcrt（它确实存在）"
    assert len(sites) >= 5, f"扫到的调用点太少（{len(sites)}），判据可能在空转"


# --- W2 每个调用点都要有守卫或在允许清单里 -----------------------------------


def test_w2_every_windows_only_call_site_is_guarded_or_allowed():
    offenders: list[str] = []
    for path, line_number, name, text in _real_call_sites():
        key = f"{path.name}:{line_number}"
        if _is_guarded(path, line_number):
            continue
        if path.name in ALLOWED_WITHOUT_GUARD or key in ALLOWED_WITHOUT_GUARD:
            continue
        offenders.append(f"{key} [{name}] {text.strip()[:70]}")
    assert offenders == [], (
        "这些地方直接调了 Windows-only 的东西又没有平台守卫，"
        "在 Linux/macOS 上会以 `FileNotFoundError` 的形式炸掉（很难归因）：\n  "
        + "\n  ".join(offenders)
        + "\n要么加平台守卫（给一句能行动的错误），要么进 ALLOWED_WITHOUT_GUARD 并写清为什么安全"
    )


# --- W3 看门狗的两个入口：非 Windows 上给的是"人话"而不是 FileNotFoundError ----


def test_w3_watchdog_entry_points_use_the_guarded_helper():
    text = (REPO / "api" / "app" / "orchestrator.py").read_text(encoding="utf-8")
    assert "def _powershell_exe(" in text
    assert text.count("self._powershell_exe()") == 2, "两个看门狗入口都该走这个守卫"
    # 判据盯**当参数用的那一行**，而不是"出现过这个字符串"：方法自己的 docstring 里
    # 也会提到 `powershell.exe`，按字符串查会误报（实测踩过）。
    assert '\n            "powershell.exe",' not in text, "不该再留裸的 powershell.exe 参数"


def test_w3_powershell_helper_rejects_non_windows_with_an_actionable_message(tmp_path, monkeypatch):
    """在非 Windows 上（这里用 `os.name` 伪装）必须抛**说明原因**的错，而不是文件找不到。"""

    from app.config import Settings
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(Settings(workspace_dir=tmp_path / "workspace", codex_bin="codex"))
    monkeypatch.setattr("app.orchestrator.os.name", "posix", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        orchestrator._powershell_exe()
    message = str(excinfo.value)
    # 错误里要写清"为什么"和"怎么办"，否则用户只会以为文件丢了。
    assert "Windows" in message
    assert "cron" in message or "systemd" in message


def test_w3_powershell_helper_returns_the_binary_on_windows(tmp_path, monkeypatch):
    from app.config import Settings
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(Settings(workspace_dir=tmp_path / "workspace", codex_bin="codex"))
    monkeypatch.setattr("app.orchestrator.os.name", "nt", raising=False)
    assert orchestrator._powershell_exe() == "powershell.exe"


# --- W4 已知的产品级边界要写在文档里 -----------------------------------------


def test_w4_known_product_level_boundaries_are_documented():
    doc = (REPO / "docs" / "cross-platform.md").read_text(encoding="utf-8")
    # worker 提示词与看门狗都是 PowerShell 假定 —— 这是**产品级**边界，不是路径问题。
    for token in ("提示词", "看门狗", "PowerShell"):
        assert token in doc, f"跨平台文档里应当写明这条边界：{token}"
