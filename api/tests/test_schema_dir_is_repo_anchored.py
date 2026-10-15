"""输出 schema 目录必须锚在**仓库**上，不能从 workspace/state 路径推导。

**事故形态**（`docs/spec-state-project-decoupling.md` §11-H2）：`runner.py` 曾用

    self.schema_dir = settings.workspace_dir.parent / "schemas"

它**只是碰巧**等于 `<repo>/schemas`——因为 `workspace_dir` 的默认值恰好是 `<repo>/workspace`。
一旦 workspace 不再是仓库的子目录（搬到 `~/.codex-battlefield` 或
`<state_home>/projects/<key>/`），`.parent` 就指向 `~` 或 `projects/`，
于是 `codex --output-schema <不存在的文件>`：

    Failed to read output schema file ...

后果不是报错停机，而是 **codex 秒退、0 token**，而状态机把它当成
"这一轮跑完了"继续往前走。实测过 5 秒、0 token、判据 1/3——
也就是**看起来像"跑完了但没做对"**，最难归因的那种失败。

本文件的判据是**行为性**的，不是文本匹配：把 `workspace_dir` 指到一个完全
无关的临时目录，schema 目录仍必须指向仓库里那个真实存在的 `schemas/`。
"""
from __future__ import annotations

import pathlib

from app.config import ROOT_DIR, get_settings
from app.runner import CodexExecRunner


def _settings(**overrides):
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


def test_schema_dir_survives_a_workspace_outside_the_repo(tmp_path: pathlib.Path) -> None:
    """核心判据：workspace 搬到仓库外，schema 目录不许跟着跑。"""
    outside = tmp_path / "somewhere-else" / "workspace"
    outside.mkdir(parents=True)
    runner = CodexExecRunner(_settings(workspace_dir=outside))

    assert runner.schema_dir == ROOT_DIR / "schemas", (
        f"schema_dir 没锚在仓库上：{runner.schema_dir}；"
        "这会让 codex 找不到 --output-schema 并秒退 0 token"
    )
    # 而且必须真的是个能读到 schema 的目录，否则断言等于没测
    assert runner.schema_dir.is_dir(), f"schema 目录不存在：{runner.schema_dir}"
    assert list(runner.schema_dir.glob("*.json")), f"schema 目录里没有 json：{runner.schema_dir}"


def test_schema_dir_is_identical_for_different_workspaces(tmp_path: pathlib.Path) -> None:
    """不同 workspace 必须得到**同一个** schema 目录（它不属于任何工程）。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    runner_a = CodexExecRunner(_settings(workspace_dir=a))
    runner_b = CodexExecRunner(_settings(workspace_dir=b))

    assert runner_a.schema_dir == runner_b.schema_dir == ROOT_DIR / "schemas"


def test_schema_dir_does_not_use_workspace_parent(tmp_path: pathlib.Path) -> None:
    """盯住那个具体错误形态：不许出现 workspace_dir.parent 这种推导。

    这条是"防回归到旧写法"的守卫——即使有人把 ROOT_DIR 又换回某种相对推导，
    只要结果不等于 `workspace_dir.parent / "schemas"` 就还能拦住最常见的写法。
    """
    workspace = tmp_path / "decoy" / "workspace"
    workspace.mkdir(parents=True)
    runner = CodexExecRunner(_settings(workspace_dir=workspace))

    accidental = workspace.parent / "schemas"
    assert runner.schema_dir != accidental, (
        f"schema_dir 退回了从 workspace 推导的写法：{accidental}"
    )
