"""工程挂载/切换的数据层判据（`docs/spec-state-project-decoupling.md` §9）。

`api/app/project_store.py` 是 `active.json` / `registry.json` 的唯一读写口，
本文件钉住它四条最重要的性质：

| 编号 | 性质 | 不满足会怎样 |
|---|---|---|
| M1 | 挂载是**可重入**的 | 重复 mount 产生第二份状态，或把已存在的树清掉 |
| M2 | `active.json` **原子写** | 半写文件让下次启动读成"未绑定"→ 悄悄退回老工作区 |
| M3 | 拒绝会静默搞坏状态的输入（相对路径 / 盘根 / 状态根自身） | 同一工程得到不同 key；或状态根被当成"被改的工程" |
| M4 | 摘除默认**不删**状态目录 | "顺手"造成不可逆的数据删除 |

另加：registry 被写坏时，磁盘上真实存在的工程不许凭空消失。
"""
from __future__ import annotations

import sys

import json
import pathlib

import pytest

from app import project_store
from app.state_layout import project_key, resolve_state_home


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """把状态根指到 tmp，保证用例不碰本机真实状态。"""
    target = tmp_path / "state-home"
    target.mkdir()
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(target))
    return target


@pytest.fixture()
def project_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    target = tmp_path / "projects-root" / "g10"
    target.mkdir(parents=True)
    return target


# ---------- M1 可重入 ----------


def test_m1_mount_is_idempotent(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    first = project_store.mount(project_dir)
    # 在工程状态目录里放一个哨兵文件，验证重复挂载不会清掉已有状态
    sentinel = first.project_dir / "keep-me.txt"
    sentinel.write_text("x", encoding="utf-8")

    second = project_store.mount(project_dir)

    assert first.key == second.key
    assert sentinel.is_file(), "重复挂载清掉了已有状态 —— 挂载必须可重入"
    # registry 里也只应有一条
    registry = json.loads((home / "registry.json").read_text(encoding="utf-8"))
    assert list(registry["projects"]) == [first.key]


def test_mount_creates_the_full_derived_dir_set(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    mounted = project_store.mount(project_dir)
    for name in ("runs", "worktrees", "state_snapshots", "checkpoint_ledger", "full_checkpoints", "resource_leases"):
        assert (mounted.project_dir / name).is_dir(), f"缺少目录 {name}"
    assert mounted.db_path.parent == mounted.project_dir


# ---------- M2 原子写 ----------


def test_m2_active_json_is_written_atomically(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    """原子写：不允许留下临时文件，且内容必须是完整 JSON。

    做法上不能直接观测"是否用 os.replace"，所以断言两件可观测的事：
      1. 写完目录里没有 `.tmp` 残留（有的话说明失败路径没清理）
      2. 内容永远是完整可解析的 JSON
    """
    project_store.mount(project_dir)

    leftovers = [p.name for p in home.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"原子写留下了临时文件：{leftovers}"

    payload = json.loads((home / "active.json").read_text(encoding="utf-8"))
    assert payload["project_key"] == project_key(project_dir)
    assert pathlib.Path(payload["project_root"]) == project_dir.resolve()


def test_m2_active_json_survives_a_simulated_interrupted_write(
    home: pathlib.Path, project_dir: pathlib.Path, monkeypatch
) -> None:
    """写入中途抛异常时，旧的 active.json 必须**原样还在**。

    这正是"半写会悄悄退回老工作区"那个风险的直接判据：
    宁可保留旧值，也不能留一个读不出来的文件。
    """
    project_store.mount(project_dir)
    good = (home / "active.json").read_text(encoding="utf-8")

    other = project_dir.parent / "g11"
    other.mkdir()

    real_replace = project_store.os.replace

    def boom(src, dst):  # noqa: ANN001
        raise OSError("模拟写入中断")

    monkeypatch.setattr(project_store.os, "replace", boom)
    with pytest.raises(OSError):
        project_store.mount(other, make_active=True)
    monkeypatch.setattr(project_store.os, "replace", real_replace)

    assert (home / "active.json").read_text(encoding="utf-8") == good, (
        "写入失败把 active.json 弄坏了 —— 下次启动会读成未绑定并悄悄回退老工作区"
    )


# ---------- M3 拒绝危险输入 ----------


def test_m3_rejects_relative_path(home: pathlib.Path) -> None:
    """相对路径必须拒绝：它随 cwd 变化，同一工程会得到不同 key。"""
    with pytest.raises(project_store.ProjectError) as excinfo:
        project_store.validate_project_root("some/relative/dir")
    assert "绝对路径" in str(excinfo.value)


def test_m3_rejects_drive_root(home: pathlib.Path) -> None:
    with pytest.raises(project_store.ProjectError):
        project_store.validate_project_root(pathlib.Path.home().anchor)


def test_m3_rejects_the_state_home_itself(home: pathlib.Path) -> None:
    """状态根（或它下面）不能当工程——否则状态被卷进"工程材料"。"""
    with pytest.raises(project_store.ProjectError) as excinfo:
        project_store.validate_project_root(home)
    assert "状态根" in str(excinfo.value)

    with pytest.raises(project_store.ProjectError):
        project_store.validate_project_root(home / "projects" / "x")


def test_m3_accepts_a_normal_dir(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    assert project_store.validate_project_root(project_dir) == project_dir.resolve()


# ---------- M4 摘除不删状态 ----------


def test_m4_unmount_keeps_state_by_default(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    mounted = project_store.mount(project_dir)
    sentinel = mounted.project_dir / "precious.json"
    sentinel.write_text("{}", encoding="utf-8")

    result = project_store.remove_project(mounted.key)

    assert result["purged"] is False
    assert sentinel.is_file(), "摘除竟然删了状态目录 —— 默认必须保留"
    assert project_store.current_project() is None, "摘除当前工程时应清掉 active.json"


def test_m4_purge_is_explicit(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    mounted = project_store.mount(project_dir)
    result = project_store.remove_project(mounted.key, purge=True)
    assert result["purged"] is True
    assert not mounted.project_dir.exists()


def test_unmount_unknown_key_is_a_clear_error(home: pathlib.Path) -> None:
    with pytest.raises(project_store.ProjectError):
        project_store.remove_project("nope--not-there")


# ---------- 索引坏了也不许让真实状态消失 ----------


def test_list_recovers_projects_from_disk_when_registry_is_broken(
    home: pathlib.Path, project_dir: pathlib.Path
) -> None:
    mounted = project_store.mount(project_dir)
    (home / "registry.json").write_text("{broken", encoding="utf-8")

    listed = {item.key for item in project_store.list_projects()}

    assert mounted.key in listed, (
        "registry 写坏后磁盘上真实存在的工程消失了 —— 用户会以为状态没了，其实只是索引丢了"
    )


def test_current_returns_none_when_unbound(home: pathlib.Path) -> None:
    assert project_store.current_project() is None


def test_clear_active_makes_it_unbound(home: pathlib.Path, project_dir: pathlib.Path) -> None:
    project_store.mount(project_dir)
    assert project_store.current_project() is not None
    clear = project_store.clear_active
    clear()
    assert project_store.current_project() is None


# ---------- 跨盘判定（spec §8） ----------


@pytest.mark.skipif(sys.platform != "win32", reason="`Path.drive` 在 POSIX 上恒为空，没有“跨盘”这回事")
def test_worktree_drive_comparison() -> None:
    assert project_store.worktree_drive_matches(pathlib.Path("D:/a/b"), pathlib.Path("D:/c")) is True
    assert project_store.worktree_drive_matches(pathlib.Path("C:/a"), pathlib.Path("D:/c")) is False


def test_resolve_state_home_honours_the_env_override(home: pathlib.Path) -> None:
    assert resolve_state_home() == home
