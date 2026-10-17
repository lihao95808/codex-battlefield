"""`workspace_dir` / `database_url` 的默认值必须**跟随绑定的工程**（P2 核心）。

这是"装载不同目录作为工程目录"真正落地的地方：绑了工程之后，每工程状态
（库、运行记录、工作树）落到该工程的状态目录，而不是仓库里的 `workspace/`。

## 落地过程中实测踩到的两个坑（都有对应用例）

**坑 1 —— 裸构造不回退 `active.json`。**
第一版把解析写成 `StateLayout(state_home=..., project_key=self.project_key, ...)`。
直接 `Settings()` 时 `project_key` 是空串 → `layout.bound` 为 False →
路径**静默退回老仓库**。而 `get_settings()` 会额外读 `active.json` 补 key，
于是两条路径不一致。实机现象：`/api/state` 报 `bound=true`，
而 `database_url` 指向老库（库真的建在老地方，端点却宣称在状态目录）。
修法：`__post_init__` 也用 `load_layout(...)`，与 `get_settings()` 同一份解析逻辑。
→ `test_bare_settings_follows_active_json`

**坑 2 —— 测试被"机器上的真实 active.json"污染。**
`load_layout()` 在没有显式 key 时会读**真实**状态根（`~/.codex-battlefield`），
这在生产是对的，在测试里却是隐藏的全局状态：本机一旦绑过工程，
所有"未绑定"用例都会拿到那个真实 key。
→ 本文件所有用例都显式传 `state_home` 并清掉 key/root（见 `_settings`），
  只有一条**故意**读真实状态根的用例用 `monkeypatch` 把它指到 tmp。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app import config as config_module
from app.config import WORKSPACE_DIR


def _settings(tmp_path: pathlib.Path, **overrides):
    """模板来自真实 Settings，但**状态根与绑定都替换掉**，保证用例不依赖本机状态。"""
    base = config_module.get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(
        {
            "state_home": tmp_path,
            "project_key": "",
            "project_root": None,
            "workspace_dir": config_module._UNSET,
            "database_url": config_module._UNSET,
        }
    )
    fields.update(overrides)
    return base.__class__(**fields)


def _bind(tmp_path: pathlib.Path, root: str) -> str:
    from app.state_layout import project_key

    key = project_key(root)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "active.json").write_text(
        json.dumps({"project_key": key, "project_root": root}), encoding="utf-8"
    )
    return key


def _isolate_env(monkeypatch) -> None:
    """去掉会干扰默认值解析的环境变量。"""
    monkeypatch.delenv("CODEX_BATTLEFIELD_PROJECT_KEY", raising=False)
    monkeypatch.delenv("CODEX_BATTLEFIELD_DATABASE_URL", raising=False)


def test_unbound_falls_back_to_the_legacy_workspace(tmp_path: pathlib.Path, monkeypatch) -> None:
    """没绑工程时回退老路径——保住"clone 下来什么都不配也能跑"。"""
    _isolate_env(monkeypatch)
    settings = _settings(tmp_path)

    assert settings.layout.bound is False
    assert settings.workspace_dir == WORKSPACE_DIR
    assert str(WORKSPACE_DIR).replace("/", "\\") in settings.database_url.replace("/", "\\")


def test_bound_project_moves_workspace_and_database(tmp_path: pathlib.Path, monkeypatch) -> None:
    """绑定后：每工程状态必须落到该工程的状态目录（P2 的核心目标）。"""
    _isolate_env(monkeypatch)
    _bind(tmp_path, r"C:\work\g10")
    settings = _settings(tmp_path)

    assert settings.layout.bound is True
    assert settings.workspace_dir == settings.layout.project_dir
    # 两边都先归一成 `/` 再比：`database_url` 走 `as_posix()`（永远 `/`），
    # 而 `project_dir` 在 Windows 上是反斜杠。原写法把 URL 换成反斜杠去比 POSIX 路径，
    # 在 Linux 上必然失败（2026-10-18 WSL 实测）。
    assert str(settings.layout.project_dir).replace("\\", "/") in settings.database_url.replace("\\", "/")
    # 关键对照：不能再是老仓库 workspace
    assert settings.workspace_dir != WORKSPACE_DIR


def test_bare_settings_follows_active_json(tmp_path: pathlib.Path, monkeypatch) -> None:
    """**坑 1 的回归**：裸 `Settings()`（不带 project_key）也必须跟随 `active.json`。

    这里刻意让 `Settings()` 走"自己解析状态根"那条路，而不是给实例传 `state_home`
    （传了就不测这条路径了）。**驱动它的必须是环境变量**：`state_home` 字段的
    default_factory 在**类定义时**就绑定了 `resolve_state_home`，
    事后 monkeypatch 模块属性对它无效 —— 第一版就是这么写的，
    于是 Settings 用了 conftest 的隔离状态根、而绑定写在 tmp_path，测的是两回事。
    """
    _isolate_env(monkeypatch)
    _bind(tmp_path, r"C:\work\g10")
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(tmp_path))

    bare = config_module.Settings()
    assert bare.layout.bound is True, "裸 Settings() 没回退读 active.json —— 就是坑 1"
    assert bare.workspace_dir == bare.layout.project_dir
    assert bare.workspace_dir != WORKSPACE_DIR


def test_get_settings_and_bare_settings_agree(tmp_path: pathlib.Path, monkeypatch) -> None:
    """两条路径（`get_settings()` 与裸构造）必须给**同一个**答案。

    不一致正是坑 1 的表现形态，而且很难发现：端点报 A、库在 B。
    """
    _isolate_env(monkeypatch)
    _bind(tmp_path, r"C:\work\g10")
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(tmp_path))

    from_settings = config_module.get_settings()
    from_bare = config_module.Settings()

    assert from_settings.workspace_dir == from_bare.workspace_dir
    assert from_settings.database_url == from_bare.database_url


def test_explicit_arguments_always_win(tmp_path: pathlib.Path, monkeypatch) -> None:
    """显式传入优先——约 150 处测试/脚本依赖这一点，不能被"跟随布局"覆盖。"""
    _isolate_env(monkeypatch)
    _bind(tmp_path, r"C:\work\g10")
    explicit_ws = tmp_path / "explicit-ws"
    explicit_db = "sqlite:///D:/explicit/x.sqlite3"

    settings = _settings(tmp_path, workspace_dir=explicit_ws, database_url=explicit_db)
    assert settings.workspace_dir == explicit_ws
    assert settings.database_url == explicit_db


def test_env_database_url_wins_over_the_layout(tmp_path: pathlib.Path, monkeypatch) -> None:
    """`CODEX_BATTLEFIELD_DATABASE_URL` 必须压过布局（决策 D2）。

    测试套件整体依赖这条：`conftest.py` 用它给每个 pytest 进程一个独立库。
    """
    monkeypatch.delenv("CODEX_BATTLEFIELD_PROJECT_KEY", raising=False)
    monkeypatch.setenv("CODEX_BATTLEFIELD_DATABASE_URL", "sqlite:///D:/env/wins.sqlite3")
    _bind(tmp_path, r"C:\work\g10")

    settings = _settings(tmp_path)
    assert settings.database_url == "sqlite:///D:/env/wins.sqlite3"
    # workspace_dir 仍跟随布局（库与工作区是两个独立问题）
    assert settings.workspace_dir == settings.layout.project_dir


def test_pytest_suite_cannot_be_polluted_by_a_real_active_json() -> None:
    """守卫：本机真的绑了工程时，**本文件的其它用例仍不许受影响**。

    这条不测产品行为，测的是"测试自己不会因为机器状态而给出假绿/假红"：
    如果 `_settings()` 忘了清 `project_key`，这里会直接暴露。
    """
    settings = _settings(pathlib.Path(r"C:\definitely\not\bound\here"))
    assert settings.project_key == "", "测试模板没清 project_key —— 会把本机绑定带进用例"
    assert settings.project_root is None


@pytest.mark.parametrize("root", [r"C:\work\g10", r"\\server\share\x"])
def test_bound_layout_is_used_for_cwd_and_runs(tmp_path: pathlib.Path, monkeypatch, root: str) -> None:
    """跟随的不只是库：`runs/`、`worktrees/` 也必须落在工程目录里。"""
    _isolate_env(monkeypatch)
    _bind(tmp_path, root)
    settings = _settings(tmp_path)
    layout = settings.layout

    for path in (layout.runs_dir, layout.worktrees_dir, layout.snapshots_dir):
        assert str(path).startswith(str(layout.project_dir)), f"{path} 不在工程目录里"
