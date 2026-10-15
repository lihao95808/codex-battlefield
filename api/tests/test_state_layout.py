"""`StateLayout` 的地基判据（`docs/spec-state-project-decoupling.md` §5.1）。

三条不变量：

| 编号 | 不变量 | 为什么必须有 |
|---|---|---|
| L1 | `StateLayout` 的任何方法**不建目录、不读写文件** | 它是值对象。一旦它会写盘，"读一下路径"就变成有副作用的操作，测试与干跑都会污染真实状态 |
| L2 | 目录创建只发生在**进程启动**与**挂载工程** | 散落的 mkdir 会让"状态根里为什么有这个目录"无法回答 |
| L3 | 未绑定工程时取每工程路径必须**响亮失败** | 静默退回状态根会在状态目录里读写本该属于工程的产物，而且不会立刻暴露 |

外加 P1 的核心新能力：`/api/state` 能回答"我现在在哪个工程"（目标 G6）。
"""
from __future__ import annotations

import json
import pathlib
import tomllib

import pytest

from app.state_layout import (
    NotBoundError,
    StateLayout,
    config_file,
    load_layout,
    normalize_project_root,
    project_key,
    read_active_project,
    read_state_home_config,
    set_config_value,
    resolve_state_home,
    set_state_home,
)


# ---------- 用户级配置：状态根可以持久化（GUI 启动也生效） ----------


def test_state_home_round_trips_through_the_config_file(tmp_path: pathlib.Path, monkeypatch) -> None:
    """**关键判据：Windows 路径必须能原样读回来。**

    第一版把路径直接塞进双引号里，`D:\\work\\x` 的 `\\w` 是非法 TOML 转义，
    读端静默 `TOMLDecodeError -> None`，于是配置被忽略、悄悄退回默认值。
    实测现象：搬完状态根后 `state show` 仍报旧位置。
    """
    cfg = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STATE_HOME", raising=False)

    target = tmp_path / "state-home"
    set_state_home(target)

    # 文件得是**合法 TOML**（这一步就是当初挂掉的地方）
    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["state_home"] == str(target)

    assert read_state_home_config() == target
    assert resolve_state_home() == target


def test_env_var_beats_the_config_file(tmp_path: pathlib.Path, monkeypatch) -> None:
    """环境变量优先——测试与一次性脚本靠它完全绕开用户配置。"""
    cfg = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))
    set_state_home(tmp_path / "from-config")

    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(tmp_path / "from-env"))
    assert resolve_state_home() == tmp_path / "from-env"


def test_config_survives_a_path_with_spaces_and_quotes(tmp_path: pathlib.Path, monkeypatch) -> None:
    """带空格/引号的路径也要能往返（转义写对了才行）。"""
    cfg = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STATE_HOME", raising=False)

    weird = tmp_path / 'state "quoted" dir'
    set_state_home(weird)
    assert read_state_home_config() == weird


def test_broken_config_falls_back_instead_of_crashing(tmp_path: pathlib.Path, monkeypatch) -> None:
    """配置写坏时退回默认值，**不是**让工具报废。

    引导阶段抛异常会让 API 连启动都做不到，而"配置写坏"不该有那种后果。
    """
    cfg = tmp_path / "cfg.toml"
    cfg.write_text("state_home = \n  not toml at all", encoding="utf-8")
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))
    monkeypatch.delenv("CODEX_BATTLEFIELD_STATE_HOME", raising=False)

    assert read_state_home_config() is None
    assert resolve_state_home() == pathlib.Path.home() / ".codex-battlefield"


def test_set_state_home_preserves_other_keys(tmp_path: pathlib.Path, monkeypatch) -> None:
    """只改 `state_home`，别的键别弄丢（将来会往这个文件放更多设置）。"""
    cfg = tmp_path / "cfg.toml"
    cfg.write_text('future_option = "keep-me"\nstate_home = "/old"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))

    set_state_home(tmp_path / "new-home")

    parsed = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert parsed["future_option"] == "keep-me"
    assert parsed["state_home"] == str(tmp_path / "new-home")


def test_config_file_path_can_be_overridden_for_tests(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "x.toml"))
    assert config_file() == tmp_path / "x.toml"


def test_runner_mode_can_come_from_the_config_file(tmp_path: pathlib.Path, monkeypatch) -> None:
    """`runner_mode` 也能写进配置 —— 否则"我想用真实 runner"每次都得记着带环境变量。

    环境变量只对当前 shell 生效，而 API 常被 GUI/计划任务拉起。
    """
    import importlib

    from app import config as config_module

    cfg = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(cfg))
    monkeypatch.delenv("CODEX_BATTLEFIELD_RUNNER", raising=False)

    set_config_value("runner_mode", "real")
    importlib.reload(config_module)
    try:
        assert config_module.Settings().runner_mode == "real"

        # 环境变量必须能盖过配置（测试与一次性脚本靠这个）
        monkeypatch.setenv("CODEX_BATTLEFIELD_RUNNER", "mock")
        importlib.reload(config_module)
        assert config_module.Settings().runner_mode == "mock"
    finally:
        monkeypatch.delenv("CODEX_BATTLEFIELD_RUNNER", raising=False)
        importlib.reload(config_module)


def test_runner_mode_defaults_to_mock_without_config(tmp_path: pathlib.Path, monkeypatch) -> None:
    """对照：没有配置也没有环境变量时，默认必须是 mock（不能悄悄跑真实模型花钱）。"""
    import importlib

    from app import config as config_module

    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("CODEX_BATTLEFIELD_RUNNER", raising=False)
    importlib.reload(config_module)
    try:
        assert config_module.Settings().runner_mode == "mock"
    finally:
        importlib.reload(config_module)


# ---------- L1：纯值对象，不碰文件系统 ----------


def test_l1_path_getters_do_not_touch_the_filesystem(tmp_path: pathlib.Path) -> None:
    """取路径不许产生任何目录/文件——否则"读一下"就有副作用。"""
    home = tmp_path / "state-home"
    home.mkdir()
    layout = StateLayout(state_home=home, project_key="p--abc", project_root=tmp_path / "proj")

    # 把能取的全取一遍
    _ = (
        layout.tools_dir,
        layout.active_file,
        layout.registry_file,
        layout.projects_dir,
        layout.project_dir,
        layout.db_path,
        layout.runs_dir,
        layout.snapshots_dir,
        layout.checkpoint_ledger_dir,
        layout.full_checkpoints_dir,
        layout.worktrees_dir,
        layout.leases_dir,
        layout.derived_dirs,
        layout.describe(),
    )

    assert list(home.iterdir()) == [], f"取路径竟然创建了东西：{list(home.iterdir())}"


def test_l1_describe_is_pure(tmp_path: pathlib.Path) -> None:
    """`describe()` 服务于 `/api/state`，必须是纯的（不能顺手 mkdir）。"""
    home = tmp_path / "state-home"
    home.mkdir()
    StateLayout(state_home=home, project_key="p--abc").describe()
    assert list(home.iterdir()) == []


# ---------- L3：未绑定工程要响亮失败 ----------


def test_l3_unbound_project_paths_fail_loudly(tmp_path: pathlib.Path) -> None:
    """未绑定工程时，取每工程/工程材料路径必须抛错，**不能**静默退回状态根。"""
    layout = StateLayout(state_home=tmp_path / "state-home", project_key="")

    with pytest.raises(NotBoundError) as excinfo:
        _ = layout.project_dir

    message = str(excinfo.value)
    # 错误信息要能直接告诉人怎么修，否则"响亮"只等于"吵"
    assert "battlefield-project" in message, f"错误信息没给修法：{message}"


def test_l3_but_machine_level_paths_still_work_when_unbound(tmp_path: pathlib.Path) -> None:
    """对照：未绑定时机器级路径**必须**仍然可用（槽位/预算/健康文件都在那）。"""
    home = tmp_path / "state-home"
    layout = StateLayout(state_home=home, project_key="")

    assert layout.tools_dir == home / "tools"
    assert layout.active_file == home / "active.json"
    assert layout.bound is False
    info = layout.describe()
    assert info["bound"] is False
    # 未绑定时不暴露每工程字段，避免调用方拿到一个不存在的路径
    assert "project_dir" not in info


# ---------- project key 的确定性 / 折叠 / 防碰撞 ----------


def test_key_is_deterministic_and_safe() -> None:
    for raw in [r"D:\work\g10", r"\\server\share\x", r"C:\a b\中文"]:
        key = project_key(raw)
        assert key == project_key(raw)
        assert key.isascii(), f"key 不是 ASCII（会导致编码问题）：{key}"
        assert all(ch.isalnum() or ch in "._%-" for ch in key), f"key 含不安全字符：{key}"
        assert len(key) <= 100


def test_windows_case_folding_gives_one_project() -> None:
    """Windows 大小写不敏感：同一工程不许得到两个目录。"""
    assert project_key(r"D:\work\g10") == project_key(r"D:\WORK\G10")


def test_distinct_paths_get_distinct_keys() -> None:
    keys = {
        project_key(r"D:\work\g10"),
        project_key(r"D:\work\g10\art_res"),
        project_key(r"E:\work\g10"),
        project_key(r"D:\a b\c"),
        project_key(r"D:\a-b\c"),
    }
    # 5 个不同路径必须得到 5 个不同 key（末尾哈希防碰撞）
    assert len(keys) == 5, f"出现碰撞：{keys}"


def test_trailing_separator_is_ignored() -> None:
    assert project_key(r"D:\work\g10") == project_key(r"D:\work\g10\\")
    assert normalize_project_root(r"D:\work\g10/") == normalize_project_root(r"D:\work\g10")


# ---------- key 的长度预算（被组合路径反推，不是可读性优先） ----------


def test_key_is_short_enough_for_the_composed_path_budget() -> None:
    """key 必须短。这不是审美问题——是 Windows MAX_PATH 的硬约束。

    实测教训：第一版用"完整路径 slug"当 key，临时目录下的工程得到 150+ 字符的 key，
    于是 `<状态根>/projects/<key>/runs` 直接
    `[WinError 206] 文件名或扩展名太长`，连目录都建不出来。
    """
    deep = r"C:\Users\duzuguang\Documents\公司项目\2026年\国风UI重构\art_res\structure_backup2021\ta_tools\ps_helper\sub_agent\agent3"
    key = project_key(deep)

    assert len(key) <= 70, f"key 太长（{len(key)}），组合路径会撞 MAX_PATH：{key}"


def test_key_stays_readable_via_the_leaf_name() -> None:
    """可读性由**末段目录名**提供：一眼能看出是哪个工程。"""
    deep = r"C:\Users\duzuguang\Documents\公司项目\2026年\国风UI重构\art_res\structure_backup2021\ta_tools\ps_helper\sub_agent\agent3"
    assert "agent3" in project_key(deep)
    assert "g10" in project_key(r"D:\work\g10")
    assert "battlefield" in project_key(r"D:\work\codex-battlefield")


def test_leaf_only_keys_still_prevent_collisions() -> None:
    """**关键**：两个都叫 `agent3` 的不同目录必须得到不同 key（靠哈希，不靠路径）。"""
    first = project_key(r"D:\work\projA\agent3")
    second = project_key(r"D:\work\projB\agent3")

    assert first != second, f"同名末段撞了：{first}"
    # 但两个都还能一眼看出叫 agent3
    assert "agent3" in first and "agent3" in second


def test_key_length_is_bounded_for_absurd_paths() -> None:
    absurd = "D:\\" + "\\".join(f"segment_{index:02d}" for index in range(40))
    assert len(project_key(absurd)) <= 70


def test_pathological_leaf_falls_back_to_head_and_hash() -> None:
    """末段名本身超长时退到 `<盘符>--<哈希>`，仍然安全且够短。"""
    leaf = "x" * 200
    key = project_key(f"D:\\work\\{leaf}")
    assert len(key) <= 70
    assert key.startswith("d--")


def test_unc_and_drive_heads_are_still_distinct() -> None:
    """UNC 与盘符路径即使末段同名也不同 key（哈希覆盖完整路径）。"""
    assert project_key(r"\\server\share\g10") != project_key(r"D:\server\share\g10")


# ---------- active.json 的读取：坏文件不能把进程炸掉 ----------


def test_read_active_project_handles_missing_and_broken_files(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "state-home"
    home.mkdir()

    # 不存在
    assert read_active_project(home) == ("", None)

    # 坏 JSON
    (home / "active.json").write_text("{not json", encoding="utf-8")
    assert read_active_project(home) == ("", None)

    # 合法但不是对象
    (home / "active.json").write_text("[1,2]", encoding="utf-8")
    assert read_active_project(home) == ("", None)

    # 合法对象
    (home / "active.json").write_text(
        json.dumps({"project_key": "d--work-g10--abcdef12", "project_root": r"D:\work\g10"}),
        encoding="utf-8",
    )
    key, root = read_active_project(home)
    assert key == "d--work-g10--abcdef12"
    assert root == pathlib.Path(r"D:\work\g10")


def test_load_layout_prefers_the_explicit_override(tmp_path: pathlib.Path) -> None:
    """显式 override 优先于 active.json（环境变量要能压过磁盘状态）。"""
    home = tmp_path / "state-home"
    home.mkdir()
    (home / "active.json").write_text(
        json.dumps({"project_key": "from-file", "project_root": r"D:\file"}), encoding="utf-8"
    )

    layout = load_layout(state_home=home, project_key_override="from-override")
    assert layout.project_key == "from-override"

    from_file = load_layout(state_home=home)
    assert from_file.project_key == "from-file"


# ---------- G6：能回答"我在哪个工程" ----------


def test_settings_layout_snapshot_does_not_reread_active_json(tmp_path: pathlib.Path) -> None:
    """决策 D1：`Settings.layout` 是**启动快照**，运行期不许回读 active.json。

    如果一个进程开着、用户改了 `active.json`，进程必须继续用**自己启动时**那个工程——
    否则引擎/会话缓存仍指向旧库，而路径已经指向新库 = 静默写错库。
    """
    from app.config import get_settings

    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update({"state_home": tmp_path, "project_key": "snapshot-key"})
    settings = base.__class__(**fields)

    assert settings.layout.project_key == "snapshot-key"

    # 磁盘上把 active.json 改成别的工程
    (tmp_path / "active.json").write_text(
        json.dumps({"project_key": "other-key", "project_root": r"D:\other"}), encoding="utf-8"
    )

    # 快照不受影响——这是刻意的
    assert settings.layout.project_key == "snapshot-key", (
        "layout 回读了 active.json！这会让'进程开着但切了工程'变成静默写错库"
    )
