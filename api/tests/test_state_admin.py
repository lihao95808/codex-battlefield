"""状态根**概览 / 迁移预演 / 重置**端点的判据（P2-5）。

这三件事凑在一起是有原因的：它们是用户**最怕做错**、又**最不可逆**的操作。
换机器、搬状态、清空重来 —— 一旦点错，损失的是整棵任务树。
而界面上的按钮比命令行更容易手滑（命令行至少要求你把 key 抄一遍）。

所以本文件的重点不是"功能能用"，而是**闸门有没有关严**：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| S1 | 概览必须**只读**：调用前后磁盘逐文件一致 | "看一眼"就把状态改了，用户再也不敢点 |
| S2 | 概览必须给出**大小/是否同盘/是否需要重启** | 用户在不知道代价的情况下删掉 8 GB 状态 |
| S3 | 迁移预演**必须不写任何文件** | 预演即执行 —— 唯一会真正丢数据的错 |
| S4 | 迁移的预演与执行必须说清**目标 key** | 迁到别的工程上去，而两边都不报错 |
| S5 | 重置的 `confirm` 不匹配必须 409 且**不删任何东西** | 手滑一下 8 GB 状态没了 |
| S6 | 重置只允许操作**当前**工程 | "我本想删 A 结果删了 B"，界面上看不出来 |
| S7 | 重置必须同时清 registry 与 `active.json` | 留下悬空记录：`state show` 还认得，进程启动却报 NotBound |

S1/S3/S5 都是**负向**判据（"不许发生什么"）—— 正向断言在这里没有意义：
一个会顺手写文件的预演，功能测试照样全绿。
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def state_home(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    """本次用例专属的状态根。

    **必须**同时改环境变量：`project_store.validate_project_root` 用的是
    `resolve_state_home()`（读环境变量），而端点的 `settings.layout` 走的是
    注入的 settings 实例 —— 两边不一致时会出现"挂载成功但概览里没有"。
    """
    target = tmp_path / "state-home"
    target.mkdir()
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(target))
    return target


@pytest.fixture()
def project_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """一个在状态根**之外**的工程目录（状态根下面会被 `validate_project_root` 拒掉）。"""
    root = tmp_path / "outside" / "my-project"
    root.mkdir(parents=True)
    return root


def _mount(project_root: pathlib.Path, state_home: pathlib.Path):
    from app import project_store

    return project_store.mount(project_root, state_home=state_home)


def _use_settings(
    state_home: pathlib.Path,
    project_root: pathlib.Path | None = None,
    project_key: str = "",
):
    """把 orchestrator 的 settings 换成指向本次用例状态根的实例。

    `Settings` 是 frozen dataclass，但 `orchestrator.settings` 是普通属性 ——
    既有用例（`test_api.py`）就是这么注入的。

    ⚠️ `project_key` 与 `project_root` 必须**显式传**，不能靠环境变量：
    这两个字段的默认值在**类定义时**求值（`os.getenv`），
    而 conftest 的 monkeypatch 发生在 import 之后 —— 一个字节都读不到。
    靠默认值会回落到开发机真实的 `~/.codex-battlefield/active.json`，
    于是用例结果取决于这台机器上绑过什么工程（实测：概览读到别的 key）。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    old = orchestrator.settings
    if project_root is None:
        new = Settings(state_home=state_home, project_key=project_key)
    else:
        new = Settings(
            state_home=state_home, project_key=project_key, project_root=project_root
        )
    orchestrator.settings = new
    assert isinstance(orchestrator.settings, Settings)
    return old, new


def _snapshot(root: pathlib.Path) -> list[tuple[str, int]]:
    """目录的逐文件指纹（相对路径 + 字节数）。用来证明"什么都没发生"。"""
    if not root.exists():
        return []
    items: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            items.append((path.relative_to(root).as_posix(), path.stat().st_size))
    return items


# ---------- S1 概览只读 ----------


def test_s1_overview_is_read_only(
    client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path
) -> None:
    """看一眼概览**不许**改动状态根里的任何字节。

    这条防的是"只是查询"却顺手建目录/写 registry —— 一旦发生，
    用户点一次面板就多出一份状态，而没人会把面板和状态变化联系起来。
    """
    from app.orchestrator import orchestrator

    _mount(project_root, state_home)
    old, _ = _use_settings(state_home, project_root)
    try:
        before = _snapshot(state_home)
        response = client.get("/api/state/overview")
        assert response.status_code == 200, response.text
        after = _snapshot(state_home)
    finally:
        orchestrator.settings = old

    assert after == before, "GET /api/state/overview 动了磁盘 —— 概览必须是纯只读的"


def test_s2_overview_reports_sizes_and_restart_state(
    client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path
) -> None:
    """概览必须给出"删掉会损失多少"和"要不要重启"，而不是只报路径。"""
    from app.orchestrator import orchestrator

    mounted = _mount(project_root, state_home)
    # 造一份真实占用：状态目录比 db 大是常态（runs / worktrees / 快照都在这儿）。
    (mounted.project_dir / "runs").mkdir(parents=True, exist_ok=True)
    (mounted.project_dir / "runs" / "log.txt").write_text("x" * 4096, encoding="utf-8")

    old, _ = _use_settings(state_home, project_root)
    try:
        body = client.get("/api/state/overview").json()
    finally:
        orchestrator.settings = old

    assert body["active_key"] == mounted.key
    assert body["state_home"] == str(state_home)
    entry = next(item for item in body["projects"] if item["key"] == mounted.key)
    assert entry["active"] is True
    assert entry["root_exists"] is True
    # 大小是"敢不敢删"的唯一依据，必须有且自洽。
    assert entry["state_bytes"] >= entry["db_bytes"]
    assert entry["state_bytes"] >= 4096
    # 全部落在 tmp_path 下 —— 同盘，worktree 可用。
    assert entry["drive_matches"] is True
    assert body["snapshot_matches_active_file"] is True


def test_s2_overview_flags_a_stale_process_snapshot(
    client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path
) -> None:
    """切了工程但没重启时，概览必须说"落点与实际不一致"。

    不说的话，用户会以为已经切过去了，实际当前进程还在往旧库里写。

    造法：进程快照停在 A（挂载 A 时定的），之后磁盘上又挂载了 B
    —— 正是"用户切了工程但没重启"的那一瞬间。
    """
    from app.orchestrator import orchestrator

    first = _mount(project_root, state_home)
    old, _ = _use_settings(state_home, project_root, first.key)
    second_root = project_root.parent / "later-project"
    second_root.mkdir()
    second = _mount(second_root, state_home)
    try:
        body = client.get("/api/state/overview").json()
    finally:
        orchestrator.settings = old

    assert first.key != second.key
    assert body["active_key"] == second.key
    assert body["snapshot_matches_active_file"] is False


# ---------- S3 预演不写文件 ----------


def test_s3_migration_preview_writes_nothing(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """预演必须是**纯计算**：连目标目录都不该建。

    实测风险最高的就是这条 —— 预演一旦顺手建了 `projects/<key>/`，
    用户会以为"已经迁过了"，于是不再执行真正的迁移，而库里其实是空的。
    """
    import sqlite3

    from app.orchestrator import orchestrator

    repo = tmp_path / "legacy-repo"
    workspace = repo / "workspace"
    workspace.mkdir(parents=True)
    db = workspace / "battlefield.sqlite3"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE task_nodes (id TEXT PRIMARY KEY, worktree_path TEXT)")
        con.execute("INSERT INTO task_nodes (id, worktree_path) VALUES ('n1', '/tmp/wt/n1')")
        con.commit()
    finally:
        con.close()

    # 必须真的挂一个工程：没有目标工程时 preveiw 给不出 target_key，
    # 那条断言也就测不到东西了。
    target = tmp_path / "outside" / "target-project"
    target.mkdir(parents=True)
    mounted = _mount(target, state_home)

    old, _ = _use_settings(state_home, target, mounted.key)
    try:
        before = _snapshot(state_home)
        response = client.post(
            "/api/state/migrate", json={"project_root": str(repo), "dry_run": True}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        after = _snapshot(state_home)
    finally:
        orchestrator.settings = old

    assert body["available"] is True
    assert body["legacy_task_count"] == 1
    assert body["repo_root"] == str(repo)
    # S4：预演必须说清"会迁到哪个 key"，否则用户无法判断目标选对没有。
    assert body["current_project_root"] == str(target)
    assert body["target_key"] == mounted.key
    assert after == before, "预演写盘了 —— 这正是最危险的一种'预演'"


def test_s3_migration_preview_reports_absence_instead_of_failing(
    tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """没有旧库时是"没有可迁的东西"，**不是**错误。

    报错会让用户以为工具坏了；而真相是这个工程本来就不需要迁移。
    """
    from app import state_admin
    from app.config import Settings

    empty = tmp_path / "empty-repo"
    empty.mkdir()

    body = state_admin.migration_preview(Settings(state_home=state_home), repo_root=empty)

    assert body["available"] is False
    assert body["reason"]
    assert "没有需要迁移" in body["reason"]


# ---------- S5 重置闸门：confirm 必须逐字相等 ----------


def test_s5_missing_confirm_is_refused_and_deletes_nothing(
    client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path
) -> None:
    """没有 `confirm` 时必须 409，而且状态目录**一个字节都不能少**。"""
    from app.orchestrator import orchestrator

    mounted = _mount(project_root, state_home)
    sentinel = mounted.project_dir / "precious.json"
    sentinel.write_text('{"keep": true}', encoding="utf-8")

    old, _ = _use_settings(state_home, project_root)
    try:
        response = client.post("/api/state/reset", json={"confirm": "", "purge": True})
    finally:
        orchestrator.settings = old

    assert response.status_code == 409, response.text
    assert mounted.key in response.json()["detail"], "拒绝时必须把正确的 key 告诉用户"
    assert sentinel.is_file(), "被拒的重置竟然删了东西"


def test_s5_wrong_confirm_is_refused(client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path) -> None:
    """key 抄错了同样必须拒绝 —— 差一个字符就删 8 GB 是不可接受的。"""
    from app.orchestrator import orchestrator

    mounted = _mount(project_root, state_home)

    old, _ = _use_settings(state_home, project_root)
    try:
        response = client.post(
            "/api/state/reset", json={"confirm": mounted.key + "x", "purge": True}
        )
    finally:
        orchestrator.settings = old

    assert response.status_code == 409, response.text
    assert mounted.project_dir.is_dir()


def test_s6_reset_refuses_when_nothing_is_bound(client: TestClient, state_home: pathlib.Path) -> None:
    """没绑定工程时重置是 400，不是 500 —— "无可重置对象"必须说清楚。"""
    from app.orchestrator import orchestrator

    old, _ = _use_settings(state_home)
    try:
        response = client.post("/api/state/reset", json={"confirm": "whatever", "purge": True})
    finally:
        orchestrator.settings = old

    assert response.status_code == 400, response.text
    assert "没有绑定工程" in response.json()["detail"]


# ---------- S7 命中：真的删，而且删得干净 ----------


def test_s7_matching_confirm_purges_state_and_clears_active(
    client: TestClient, state_home: pathlib.Path, project_root: pathlib.Path
) -> None:
    """确认串正确时：状态目录删掉，registry 与 `active.json` **同时**清干净。

    只清一半是最难查的坏味道：`state show` 还认得这个工程，
    进程启动却报 NotBound —— 两个界面说的不是一回事。
    """
    from app import project_store
    from app.orchestrator import orchestrator

    mounted = _mount(project_root, state_home)
    assert mounted.project_dir.is_dir()
    assert project_store.current_project(state_home=state_home) is not None

    old, _ = _use_settings(state_home, project_root)
    try:
        response = client.post(
            "/api/state/reset", json={"confirm": mounted.key, "purge": True}
        )
    finally:
        orchestrator.settings = old

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["purged"] is True
    assert body["key"] == mounted.key
    assert not mounted.project_dir.exists(), "状态目录还在 —— 重置是假动作"
    assert project_store.current_project(state_home=state_home) is None
    assert mounted.key not in {item.key for item in project_store.list_projects(state_home=state_home)}
    # 工程目录本身**没有**被碰 —— 用户删的是"工具的状态"，不是"他的代码"。
    assert project_root.is_dir()


def test_s7_reset_does_not_touch_other_projects(
    client: TestClient, state_home: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """重置**当前**工程时，别的工程的状态必须原封不动。

    这是"我本想删 A 结果删了 B"的回归：跨工程删除的错误在界面上完全看不出来
    （两边都是同一个面板、同一个按钮），只能靠端点只认 `layout.project_key` 挡。

    这里的设置刻意做成**不一致**：磁盘上 `active.json` 指向 second，
    而进程快照停在 other —— 端点必须听进程快照，因为那才是它真正在写的库。
    """
    from app.orchestrator import orchestrator

    other = tmp_path / "outside" / "other-project"
    other.mkdir(parents=True)
    keep = _mount(other, state_home)

    second = tmp_path / "outside" / "second-project"
    second.mkdir(parents=True)
    other_mounted = _mount(second, state_home)
    assert keep.key != other_mounted.key

    old, _ = _use_settings(state_home, other, keep.key)
    try:
        response = client.post("/api/state/reset", json={"confirm": keep.key, "purge": True})
    finally:
        orchestrator.settings = old

    assert response.status_code == 200, response.text
    assert not keep.project_dir.exists()
    assert other_mounted.project_dir.is_dir(), "重置当前工程时删掉了另一个工程的状态"
    # 没被删的那个还在 registry 里 —— 不能出现"删了 B 顺带把 A 的索引也清了"。
    remaining = {item.key for item in _list(state_home)}
    assert other_mounted.key in remaining


def _list(state_home: pathlib.Path):
    from app import project_store

    return project_store.list_projects(state_home=state_home)
