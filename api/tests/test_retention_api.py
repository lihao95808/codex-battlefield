"""数据保留端点的接线判据（P5-4）。

`test_retention.py` 钉的是**纯逻辑**；这里钉**端点**：计划真的读的是当前工程目录、
默认预演、真删要显式要一次、冻结模式下挡住。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| S1 | `GET /api/retention` 纯只读（调用前后磁盘一致） | "看一下"就把东西删了 |
| S2 | 计划里有**受保护项**与**保留窗口** | 用户无法核对"没删不该删的" |
| S3 | `POST /api/retention/sweep` **默认预演** | 一次误点不可逆 |
| S4 | `dry_run: false` 才真删，且只删计划里的 | 手滑 |
| S5 | 冻结模式下 sweep 被挡（读计划不受影响） | 冻结时还能删数据 |
| S6 | 保留窗口非法时 400 | 配置错了没人拦 |
| S7 | 端点读的是**当前工程**的状态目录 | 清到别的工程去了 |
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app import retention
from app.database import engine, init_db
from app.models import Base


@pytest.fixture()
def client(tmp_path: pathlib.Path, monkeypatch) -> TestClient:
    """隔离的状态根 + 配置文件 —— **绝不能真的清开发机的状态目录**。

    这条不是洁癖：`sweep` 会递归删目录，跑在真实状态根上就是真删。
    """
    state_home = tmp_path / "state-home"
    project = state_home / "projects" / "demo"
    for name in retention.SWEEPABLE_DIRS:
        (project / name).mkdir(parents=True)
    (project / retention.ANCHOR_DIR / "root-1").mkdir(parents=True)
    (project / "worktrees").mkdir()
    (project / "battlefield.sqlite3").write_text("sqlite", encoding="utf-8")

    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(state_home))
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "cfg.toml"))

    Base.metadata.drop_all(bind=engine)
    init_db()

    from app.config import Settings
    from app.main import app
    from app.orchestrator import orchestrator

    old = orchestrator.settings
    # 显式传 `project_key`：字段默认值在类定义时求值，读不到 monkeypatch 的环境变量。
    orchestrator.settings = Settings(
        state_home=state_home, project_key="demo", project_root=tmp_path / "repo"
    )
    try:
        yield TestClient(app)
    finally:
        orchestrator.settings = old
        Base.metadata.drop_all(bind=engine)


def _project_dir(client: TestClient) -> pathlib.Path:
    from app.orchestrator import orchestrator

    return pathlib.Path(orchestrator.settings.layout.project_dir)


def _snapshot(root: pathlib.Path) -> list[tuple[str, int]]:
    if not root.exists():
        return []
    return sorted(
        (item.relative_to(root).as_posix(), item.stat().st_size)
        for item in root.rglob("*")
        if item.is_file()
    )


# ---------- S1 只读 ----------


def test_s1_getting_the_plan_deletes_nothing(client: TestClient) -> None:
    project = _project_dir(client)
    (project / "runs" / "old").mkdir()
    (project / "runs" / "old" / "a.txt").write_text("x", encoding="utf-8")

    before = _snapshot(project)
    response = client.get("/api/retention")
    assert response.status_code == 200, response.text
    assert _snapshot(project) == before, "读计划改了磁盘"


# ---------- S2 计划形状 ----------


def test_s2_the_plan_reports_protected_items_and_the_window(client: TestClient) -> None:
    body = client.get("/api/retention").json()
    assert body["retention_days"] >= 0
    assert body["default_days"] == retention.DEFAULT_RETENTION_DAYS
    assert body["protected_count"] >= 1, "计划里没有受保护清单"
    reasons = [item["reason"] for item in body["protected"]]
    assert all(reasons)
    assert any("sqlite" in item["path"] for item in body["protected"])
    assert "summary" in body and "永不清理" in body["note"]


def test_s2_the_database_shows_up_as_protected(client: TestClient) -> None:
    body = client.get("/api/retention").json()
    protected = {pathlib.Path(item["path"]).name for item in body["protected"]}
    assert "battlefield.sqlite3" in protected


# ---------- S3 默认预演 ----------


def test_s3_sweep_defaults_to_dry_run(client: TestClient) -> None:
    project = _project_dir(client)
    stale = project / "runs" / "old"
    stale.mkdir()
    (stale / "a.txt").write_text("x", encoding="utf-8")
    # 把保留窗口设成 0，让"刚才建的"也算过期 —— 免得为了造"旧文件"去改 mtime。
    from dataclasses import replace

    from app.orchestrator import orchestrator

    orchestrator.settings = replace(orchestrator.settings, data_retention_days=0)

    response = client.post("/api/retention/sweep", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert body["removed_count"] == 0
    assert stale.is_dir(), "默认预演却删了东西"


def test_s3_sweep_without_a_body_still_works(client: TestClient) -> None:
    """`payload` 是可选的 —— 不带 body 的 POST 也要能跑（默认预演）。"""
    response = client.post("/api/retention/sweep")
    assert response.status_code == 200
    assert response.json()["dry_run"] is True


# ---------- S4 真删 ----------


def test_s4_explicit_dry_run_false_removes_only_the_plan(client: TestClient) -> None:
    from dataclasses import replace

    from app.orchestrator import orchestrator

    project = _project_dir(client)
    stale = project / "runs" / "old"
    stale.mkdir()
    (stale / "a.txt").write_text("x" * 512, encoding="utf-8")
    anchor = project / retention.ANCHOR_DIR / "root-1" / "2026-01-01T00-00-00-snap.json"
    anchor.write_text("{}", encoding="utf-8")

    orchestrator.settings = replace(orchestrator.settings, data_retention_days=0)
    response = client.post("/api/retention/sweep", json={"dry_run": False})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is False
    assert not stale.exists()
    assert anchor.is_file(), "锚点被删了 —— 表被清空时就真的没得还原"


# ---------- S5 冻结 ----------


def test_s5_frozen_mode_blocks_the_sweep_but_not_the_plan(client: TestClient) -> None:
    from dataclasses import replace

    from app.orchestrator import orchestrator

    orchestrator.settings = replace(orchestrator.settings, frozen_mode=True)
    blocked = client.post("/api/retention/sweep", json={"dry_run": False})
    read = client.get("/api/retention")
    assert blocked.status_code == 423
    assert read.status_code == 200


# ---------- S6 窗口非法 ----------


def test_s6_an_out_of_range_window_is_refused(client: TestClient) -> None:
    from dataclasses import replace

    from app.orchestrator import orchestrator

    orchestrator.settings = replace(orchestrator.settings, data_retention_days=9999)
    response = client.get("/api/retention")
    assert response.status_code == 400
    assert "保留天数" in response.json()["detail"]


# ---------- S7 读的是当前工程 ----------


def test_s7_the_plan_reads_the_current_project_directory(client: TestClient) -> None:
    """清到别的工程上去是这一层最糟的错之一。"""
    project = _project_dir(client)
    mine = project / "runs" / "mine"
    mine.mkdir()
    (mine / "a.txt").write_text("x", encoding="utf-8")

    body = client.get("/api/retention").json()
    paths = [item["path"] for item in body["removable"]]
    # 窗口是默认 7 天，刚建的不会被列进去；但**受保护项**必须指向当前工程。
    protected_paths = [item["path"] for item in body["protected"]]
    assert any(str(project) in path for path in protected_paths), protected_paths
    assert all(str(project) in path for path in paths) or not paths


def test_s7_worktrees_never_appear_as_candidates(client: TestClient) -> None:
    from dataclasses import replace

    from app.orchestrator import orchestrator

    project = _project_dir(client)
    (project / "worktrees" / "node-x").mkdir(parents=True)
    orchestrator.settings = replace(orchestrator.settings, data_retention_days=0)

    body = client.get("/api/retention").json()
    assert not any("worktrees" in item["path"] for item in body["removable"])
    assert any(
        pathlib.Path(item["path"]).name == "worktrees" for item in body["protected"]
    )
