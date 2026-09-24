"""模块分层守卫（W4 的完成判据 C2）。

**为什么需要这个测试**：W4 的 A3（`orchestrator.py` 行数 < 7000）是**代理指标**，
而目标（`docs/spec-modularization.md` §1）是"拆成职责清晰的模块，降低维护与调试成本"。
行数可以靠把几千行塞进一个大杂烩模块来达标，数字过、目标死；而**反向依赖**是更隐蔽的倒退：
下层模块一旦 import `orchestrator`，搬移出来的东西就又被核心绑住，模块化等于白做。

因此这里把"依赖方向单向"写成**可机器判定、可反驳**的断言：

1. 除 `orchestrator` 自身与应用入口 `main` 外，`api/app/` 下任何模块都不得 import `orchestrator`；
2. `api/app/` 内部的模块依赖图**无环**（比逐条禁更严，能抓到 A→B→C→A 这种绕圈）。

判据 C4 因此成立：**行数只作结果记录，不再当开关**。
"""

from __future__ import annotations

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# 唯一允许依赖 orchestrator 的两个模块：它自己，以及应用入口。
# 其它任何下层模块反向依赖它，都说明那一层的边界是假的。
ALLOWED_TO_IMPORT_ORCHESTRATOR = {"orchestrator", "main"}


def _module_files() -> list[pathlib.Path]:
    return sorted(path for path in APP_DIR.glob("*.py"))


def _module_names() -> set[str]:
    return {path.stem for path in _module_files()}


def _imported_app_modules(path: pathlib.Path) -> set[str]:
    """解析模块里出现的 `api/app` 内部依赖名（相对导入与 `app.x` 绝对导入都算）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app" or alias.name.startswith("app."):
                    parts = alias.name.split(".")
                    if len(parts) > 1:
                        names.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                if node.module:
                    names.add(node.module.split(".")[0])
                else:
                    # `from . import a, b`
                    for alias in node.names:
                        names.add(alias.name.split(".")[0])
            elif node.module and (node.module == "app" or node.module.startswith("app.")):
                parts = node.module.split(".")
                if len(parts) > 1:
                    names.add(parts[1])
                else:
                    for alias in node.names:
                        names.add(alias.name.split(".")[0])
    return names


def test_app_dir_is_scannable() -> None:
    """自检：扫描面必须真的覆盖到模块，否则下面的断言会空转。"""
    names = _module_names()
    assert "orchestrator" in names
    # W4 已抽出的下层模块必须在扫描面内
    for expected in (
        "blockers",
        "budget",
        "checkpoint",
        "concurrency",
        "convergence",
        "failure_retrospective",
        "godot_files",
        "godot_gates",
        "project_context",
        "prompts",
        "recovery",
        "regression",
        "retry",
        "runner_tools",
        "runtime_logs",
        "runtime_markers",
        "skill_notes",
        "usage",
        "utils",
        "watchdog",
        "worktrees",
    ):
        assert expected in names, expected


def test_lower_layers_do_not_import_orchestrator() -> None:
    """C2 核心断言：下层模块不得反向依赖 orchestrator。"""
    violations: dict[str, list[str]] = {}
    for path in _module_files():
        if path.stem in ALLOWED_TO_IMPORT_ORCHESTRATOR:
            continue
        if "orchestrator" in _imported_app_modules(path):
            violations[path.stem] = sorted(_imported_app_modules(path))
    assert violations == {}, f"以下模块反向依赖了 orchestrator：{violations}"


def test_only_orchestrator_and_main_are_allowed_to_import_orchestrator() -> None:
    """反面自检：确保上面那条断言不是因为"没有任何模块 import orchestrator"而空过。"""
    importers = {
        path.stem
        for path in _module_files()
        if "orchestrator" in _imported_app_modules(path)
    }
    assert "main" in importers, "main 是应用入口，应当 import orchestrator"


def test_app_module_import_graph_is_acyclic() -> None:
    """比逐条禁更严：整个 `api/app` 内部依赖图无环。

    A→B→C→A 这种绕圈对单条"不得 import orchestrator"规则是隐形的，
    但它同样会让某一层无法独立测试。
    """
    names = _module_names()
    graph = {
        path.stem: sorted(_imported_app_modules(path) & names)
        for path in _module_files()
    }

    # 迭代式 DFS 三色标记找环，同时给出可读的环路径
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, index = stack.pop()
            if index == 0:
                color[node] = GREY
                path.append(node)
            if index < len(graph[node]):
                stack.append((node, index + 1))
                nxt = graph[node][index]
                if color[nxt] == GREY:
                    cycle = path[path.index(nxt) :] + [nxt]
                    raise AssertionError(f"api/app 内部出现循环依赖：{' -> '.join(cycle)}")
                if color[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                color[node] = BLACK
                path.pop()
    assert True
