"""R-a 反漂移：`runner.py` 的工具脚本委托必须与 `runner_tools` 给出同一答案。

参照 `test_blockers.py` 的 `test_orchestrator_delegates_agree_with_module`：断言的不是
"函数被调用过"，而是**可观测结果一致**——返回路径、落盘字节、返回文案。这一带最常见的坑是
"接线看着在、实际是死的"，只有比对结果才拦得住。

另一条是依赖方向：`runner_tools` 只允许依赖标准库与 `config`（配置标量），尤其不得 import
`runner`，否则搬出去的东西又被核心绑住，模块化等于白做——这条与 `test_module_layering.py` 的 C2 同源。
"""

from __future__ import annotations

import ast
import pathlib

from app import runner, runner_tools
from app.config import Settings

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# (runner 上的委托方法名, runner_tools 里的对应函数)
DELEGATES = [
    ("_ensure_godot_timeout_wrapper", runner_tools.ensure_godot_timeout_wrapper),
    ("_ensure_placeholder_asset_tool", runner_tools.ensure_placeholder_asset_tool),
    ("_ensure_assert_no_match_tool", runner_tools.ensure_assert_no_match_tool),
    ("_ensure_json_check_tool", runner_tools.ensure_json_check_tool),
    ("_asset_placeholder_guidance", runner_tools.asset_placeholder_guidance),
    ("_negative_match_guidance", runner_tools.negative_match_guidance),
    ("_json_check_guidance", runner_tools.json_check_guidance),
]


def test_runner_delegates_agree_with_module_functions(tmp_path: pathlib.Path) -> None:
    settings = Settings(workspace_dir=tmp_path / "workspace")
    executor = runner.CodexExecRunner(settings)
    for method_name, function in DELEGATES:
        assert getattr(executor, method_name)() == function(settings.workspace_dir), method_name


def test_delegate_writes_the_same_bytes_as_the_module_function(tmp_path: pathlib.Path) -> None:
    """返回值对不等于落盘对：先让委托写一遍取字节，删掉再让模块函数写一遍比对。"""
    settings = Settings(workspace_dir=tmp_path / "workspace")
    executor = runner.CodexExecRunner(settings)
    checked = 0
    for method_name, function in DELEGATES:
        delegated_result = getattr(executor, method_name)()
        if not isinstance(delegated_result, pathlib.Path):
            continue
        delegated_bytes = delegated_result.read_bytes()
        assert delegated_bytes, method_name
        delegated_result.unlink()
        direct_result = function(settings.workspace_dir)
        assert direct_result == delegated_result, method_name
        assert direct_result.read_bytes() == delegated_bytes, method_name
        assert direct_result.exists(), method_name
        checked += 1
    # 自检：四个 ensure_* 都得走到，否则这条断言会空过
    assert checked == 4


def test_generated_tools_match_the_repo_templates_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """工具脚本是**仓库里的真文件**，生成物必须与它逐字节相同。

    这几个脚本原先住在字符串字面量里（最大的那个 642 行）：lint、类型检查、IDE 跳转都看不到，
    重构扫不到，改错只有"生成之后跑一次"才发现。搬成真文件之后就只剩一个风险——**两份源漂移**
    （改了仓库文件但生成的是旧内容，或者反过来）。所以这条断言是这次搬移的**唯一必要不变量**。
    """
    settings = Settings(workspace_dir=tmp_path / "workspace")
    templates = pathlib.Path(runner_tools.__file__).parent / "tools"
    # `__init__.py` 让 `app.tools` 成为随包发布的包（否则非 editable 安装里模板不会被带上），
    # 它本身不是要复制到 workspace 的脚本。
    assert {path.name for path in templates.glob("*.py") if path.name != "__init__.py"} == {
        "godot_with_timeout.py",
        "generate_placeholder_asset.py",
        "assert_no_match.py",
        "json_check.py",
    }
    for function, name in (
        (runner_tools.ensure_godot_timeout_wrapper, "godot_with_timeout.py"),
        (runner_tools.ensure_placeholder_asset_tool, "generate_placeholder_asset.py"),
        (runner_tools.ensure_assert_no_match_tool, "assert_no_match.py"),
        (runner_tools.ensure_json_check_tool, "json_check.py"),
    ):
        function(settings.workspace_dir)
        generated = settings.workspace_dir / "tools" / name
        assert generated.exists(), name
        # 按**行**比较，而不是逐字节：仓库里的模板在 Windows 检出里是 CRLF，
        # 生成器统一写 LF —— 逐字节比较会在 Linux/CI 上必然红（2026-10-18 实测），
        # 而真正要守的不变量是“两份源不许漂移内容”。换行风格由 git 与检出决定，
        # 不是这两个文件之间的事。
        assert generated.read_text(encoding="utf-8").splitlines() == (
            (templates / name).read_text(encoding="utf-8").splitlines()
        ), name


def test_runner_tools_only_depends_on_the_standard_library_and_config() -> None:
    """`runner_tools` 是 R-a/R-b 抽出来的下层模块：只能靠标准库与 `config` 的配置标量。

    首批只允许标准库；R-b 把 `resolve_godot_executable`（探测外部可执行文件）也搬进来后，
    多了 `config` 这一条——它本身是叶子模块，不构成反向依赖，故写进白名单而不是放宽成"随便导"。
    """
    tree = ast.parse((APP_DIR / "runner_tools.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    relative: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.append(node.module or "")
                continue
            if node.module:
                imported.add(node.module.split(".")[0])
            else:
                imported.update(alias.name.split(".")[0] for alias in node.names)
    assert "runner" not in imported, "runner_tools 反向依赖了 runner，搬移就白做了"
    assert sorted(relative) == ["config"], f"runner_tools 只允许相对导入 config，实为：{sorted(relative)}"
    assert imported <= {"__future__", "os", "pathlib", "shutil", "sys"}, f"出现了非标准库依赖：{sorted(imported)}"
