from __future__ import annotations

from dataclasses import replace
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

from fastapi.testclient import TestClient

from app import obligations
from app.database import SessionLocal, engine, init_db
from app.main import app
from app.models import Base, RunKind, RunRecord, RunStatus, TaskNode, TaskStatus
from app.orchestrator import orchestrator
from app.utils import dumps, loads


def setup_function():
    Base.metadata.drop_all(bind=engine)
    init_db()


def teardown_function():
    Base.metadata.drop_all(bind=engine)


def locked_constitution(client: TestClient, goal: str) -> str:
    draft = client.post("/api/constitutions/draft", json={"goal": goal, "runner_mode": "mock"})
    assert draft.status_code == 200
    constitution_id = draft.json()["id"]
    locked = client.post(f"/api/constitutions/{constitution_id}/lock")
    assert locked.status_code == 200
    assert locked.json()["status"] == "locked"
    return constitution_id


def test_tree_endpoint_returns_started_root():
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    response = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    assert response.status_code == 200
    root_id = response.json()["root_id"]

    tree = client.get("/api/tree")
    assert tree.status_code == 200
    payload = tree.json()
    assert payload["stats"]["total"] == 1
    assert payload["tasks"][0]["id"] == root_id
    assert payload["constitution"]["id"] == constitution_id
    # 契约：全新树上也有债务看板结构（`None` 表示判不出时间戳，不是被省略）
    assert payload["stats"]["obligations"]["blocking_total"] == 0
    assert payload["stats"]["obligations"]["oldest_blocking_age_seconds"] is None


def test_tree_endpoint_aggregates_obligations_across_the_whole_tree():
    """债务记在**父节点**上，所以"整棵树被卡多久"只能靠逐节点汇总——这是看板的全部意义。"""
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    started = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    root_id = started.json()["root_id"]
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta["obligations"] = obligations.upsert(
            [],
            obligations.obligations_from_version(node_id=root_id, node_version=1, effective_version=2, depth=0),
        )[:1]
        root.node_meta_json = dumps(meta)
        db.commit()

    board = client.get("/api/tree").json()["stats"]["obligations"]
    assert board["blocking_total"] == 1
    assert board["by_type"] == {obligations.CONSTITUTION_STALE: 1}
    assert board["by_depth"] == {"0": 1}
    assert board["oldest_blocking_age_seconds"] is not None
    assert board["revalidate_queue_depth"] == 0


def test_tree_endpoint_compacts_done_subtrees_by_default():
    client = TestClient(app)
    with SessionLocal() as db:
        root = TaskNode(id="api-compact-root", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value)
        done_child = TaskNode(id="api-compact-done", parent_id=root.id, title="Done", description="", depth=1, status=TaskStatus.DONE.value)
        queued_child = TaskNode(id="api-compact-queued", parent_id=root.id, title="Queued", description="", depth=1, status=TaskStatus.QUEUED.value)
        db.add_all([root, done_child, queued_child])
        db.commit()

    compact = client.get("/api/tree?root_id=api-compact-root")
    full = client.get("/api/tree?root_id=api-compact-root&compact_done=false")

    assert compact.status_code == 200
    assert full.status_code == 200
    compact_ids = {task["id"] for task in compact.json()["tasks"]}
    full_ids = {task["id"] for task in full.json()["tasks"]}
    assert "api-compact-done" not in compact_ids
    assert {"api-compact-root", "api-compact-queued"} <= compact_ids
    assert {"api-compact-root", "api-compact-done", "api-compact-queued"} <= full_ids


def test_watchdog_state_endpoint_discovers_current_root():
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a watchdog status probe.")
    response = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a watchdog status probe.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    assert response.status_code == 200
    root_id = response.json()["root_id"]

    state = client.get("/api/watchdog/state")
    assert state.status_code == 200
    payload = state.json()
    assert payload["root_id"] == root_id
    assert payload["recommended_action"] == "top_off"
    assert payload["scheduled_task_name"] == "CodexBattlefieldWatchdog"
    assert payload["logon_task_name"] == "CodexBattlefieldWatchdog-Logon"
    assert payload["watchdog_interval_minutes"] == 1
    assert payload["watchdog_log_path"].endswith("watchdog.log")
    assert "battlefield_watchdog.ps1" in payload["watchdog_run_command"]
    assert "register_battlefield_watchdog_task.ps1" in payload["watchdog_install_command"]
    assert root_id in payload["watchdog_run_command"]
    assert root_id in payload["watchdog_install_command"]
    assert "watchdog" in payload["token_reserve_summary"].lower() or "Codex agent" in payload["token_reserve_summary"]


def test_watchdog_install_endpoint_returns_local_action(monkeypatch):
    client = TestClient(app)

    def fake_install(root_id: str | None = None):
        return {
            "status": "succeeded",
            "root_id": root_id,
            "command": "powershell.exe install",
            "exit_code": 0,
            "stdout": "installed",
            "stderr": "",
        }

    monkeypatch.setattr(orchestrator, "install_watchdog_hook", fake_install)

    response = client.post("/api/watchdog/install?root_id=root-local")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["root_id"] == "root-local"
    assert payload["exit_code"] == 0


def test_frozen_mode_rejects_mutating_endpoints():
    client = TestClient(app)
    old_settings = orchestrator.settings
    orchestrator.settings = replace(old_settings, frozen_mode=True)
    try:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["frozen_mode"] == "true"

        start = client.post(
            "/api/projects/start",
            json={"goal": "Build nothing while frozen.", "runner_mode": "mock", "auto_run": False},
        )
        assert start.status_code == 423

        watchdog = client.post("/api/watchdog/run-once?root_id=root-frozen")
        assert watchdog.status_code == 423

        pause = client.post("/api/tasks/root-frozen/pause")
        assert pause.status_code == 423
    finally:
        orchestrator.settings = old_settings


def test_preview_file_serves_allowed_workspace_image(tmp_path):
    client = TestClient(app)
    old_settings = orchestrator.settings
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = workspace / "shot.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    orchestrator.settings = replace(old_settings, workspace_dir=workspace)
    try:
        response = client.get("/api/files/preview", params={"path": str(image)})
    finally:
        orchestrator.settings = old_settings

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_preview_file_rejects_image_outside_allowed_roots(tmp_path):
    client = TestClient(app)
    old_settings = orchestrator.settings
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")
    orchestrator.settings = replace(old_settings, workspace_dir=workspace)
    try:
        response = client.get("/api/files/preview", params={"path": str(outside)})
    finally:
        orchestrator.settings = old_settings

    assert response.status_code == 403


def test_project_start_requires_locked_constitution():
    client = TestClient(app)
    response = client.post(
        "/api/projects/start",
        json={"goal": "Build a recursive Codex control tower.", "runner_mode": "mock", "auto_run": False},
    )
    assert response.status_code == 409

    draft = client.post("/api/constitutions/draft", json={"goal": "Build a recursive Codex control tower.", "runner_mode": "mock"})
    assert draft.status_code == 200
    unlocked_response = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": draft.json()["id"],
            "auto_run": False,
        },
    )
    assert unlocked_response.status_code == 409


def test_one_shot_project_accepts_only_goal_and_locks_constitution():
    client = TestClient(app)
    goal = "开发一个2D 街机 格斗 闯关 游戏 godot 品质和市面上街机一个水平"
    old_settings = orchestrator.settings
    root_id = None
    orchestrator.settings = replace(old_settings, runner_mode="mock", mock_delay_seconds=0)
    try:
        response = client.post("/api/projects/one-shot", json={"goal": goal})
        assert response.status_code == 200
        root_id = response.json()["root_id"]

        tree = client.get(f"/api/tree?root_id={root_id}")
        assert tree.status_code == 200
        payload = tree.json()
        root = next(task for task in payload["tasks"] if task["id"] == root_id)
        constitution = payload["constitution"]

        assert root["description"] == goal
        assert constitution["goal"] == goal
        assert constitution["status"] == "locked"
        assert constitution["root_id"] == root_id
        assert constitution["target_path"]
        assert Path(constitution["target_path"]).exists()
        assert Path(constitution["target_path"]).name.startswith("godot-")
    finally:
        if root_id:
            client.post(f"/api/projects/cancel-all?root_id={root_id}")
        orchestrator.settings = old_settings


def test_one_shot_project_rejects_project_inputs_other_than_goal():
    client = TestClient(app)
    goal = "开发一个2D 街机 格斗 闯关 游戏 godot 品质和市面上街机一个水平"
    forbidden_payloads = [
        {"goal": goal, "runner_mode": "mock"},
        {"goal": goal, "target_path": "C:/projects/should-not-be-used"},
        {"goal": goal, "constitution_id": "const-existing"},
        {"goal": goal, "auto_run": False},
    ]

    for payload in forbidden_payloads:
        response = client.post("/api/projects/one-shot", json=payload)
        assert response.status_code == 422, payload

    query_response = client.post("/api/projects/one-shot?auto_run=false", json={"goal": goal})
    assert query_response.status_code == 422


def test_constitution_can_be_revised_then_locked():
    client = TestClient(app)
    draft = client.post("/api/constitutions/draft", json={"goal": "Build a recursive Codex control tower.", "runner_mode": "mock"})
    assert draft.status_code == 200
    payload = draft.json()

    updated = client.put(
        f"/api/constitutions/{payload['id']}",
        json={
            "markdown": "# Revised constitution\n\nQuality floor is explicit.",
            "data": {
                **payload["data"],
                "quality_floor": ["Explicit floor"],
                "quality_ceiling": ["Explicit ceiling"],
            },
        },
    )
    assert updated.status_code == 200
    updated_payload = updated.json()
    assert updated_payload["version"] == payload["version"] + 1
    assert updated_payload["content_hash"] != payload["content_hash"]
    assert updated_payload["data"]["quality_floor"] == ["Explicit floor"]

    locked = client.post(f"/api/constitutions/{payload['id']}/lock")
    assert locked.status_code == 200
    assert locked.json()["status"] == "locked"

    locked_update = client.put(
        f"/api/constitutions/{payload['id']}",
        json={"markdown": "# Too late", "data": payload["data"]},
    )
    assert locked_update.status_code == 409


def test_human_can_settle_a_debt_through_the_api():
    """§8.2 逃生舱：没有这条通路时，被 blocked 的树只能靠改库放行。"""
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    started = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    root_id = started.json()["root_id"]
    obligation_id = f"constitution_stale:{root_id}:v1->v2"
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        meta = loads(root.node_meta_json, {})
        meta["obligations"] = obligations.upsert(
            [],
            obligations.obligations_from_version(node_id=root_id, node_version=1, effective_version=2),
        )
        root.node_meta_json = dumps(meta)
        db.commit()

    detail = client.get(f"/api/tasks/{root_id}")
    assert detail.status_code == 200
    assert [item["id"] for item in detail.json()["obligations"]] == [obligation_id]

    missing_reason = client.post(
        f"/api/tasks/{root_id}/obligations/settle",
        json={"obligation_id": obligation_id, "actor": "operator", "reason": ""},
    )
    assert missing_reason.status_code == 422  # 无声的放宽不许发生

    settled = client.post(
        f"/api/tasks/{root_id}/obligations/settle",
        json={"obligation_id": obligation_id, "actor": "operator", "reason": "人确认旧版产物仍然合规"},
    )
    assert settled.status_code == 200
    assert settled.json()["obligations"]["blocking_total"] == 0

    unknown = client.post(
        f"/api/tasks/{root_id}/obligations/settle",
        json={"obligation_id": "constitution_stale:nope:v1->v2", "actor": "operator", "reason": "r"},
    )
    assert unknown.status_code == 404

    after = client.get(f"/api/tasks/{root_id}").json()["obligations"]
    assert after[0]["settled_by"] == "human"
    assert after[0]["settled_actor"] == "operator"


def test_tree_stats_expose_the_constitution_propagation_board():
    """K7：`stats.constitution` 要能回答"传播完成了吗"，且数字自洽。"""
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    started = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "constitution_id": constitution_id,
            "runner_mode": "mock",
            "auto_run": False,
        },
    )
    root_id = started.json()["root_id"]
    board = client.get("/api/tree").json()["stats"]["constitution"]
    assert board["effective_version"] == 1
    assert board["node_total"] == 1
    assert sum(board["version_distribution"].values()) == board["node_total"]
    assert board["stale_total"] == 0
    assert board["last_amendment"] is None

    client.post(
        f"/api/projects/{root_id}/constitution/amend",
        json={"markdown": "# v2\n", "data": {"global_acceptance": ["收紧"]}, "direction": "tighten", "actor": "operator"},
    )
    board = client.get("/api/tree").json()["stats"]["constitution"]
    assert board["effective_version"] == 2
    assert board["last_amendment"]["from_version"] == 1
    assert board["last_amendment"]["to_version"] == 2
    assert board["last_amendment"]["direction"] == "tighten"
    assert board["last_amendment"]["actor"] == "operator"
    # 根节点在迁移里是 `waiting_children`/`planned` 类，直接换了快照，所以没有落后节点
    assert board["stale_total"] == 0
    assert sum(len(ids) for ids in board["stale_by_action"].values()) == board["stale_total"]


def test_latest_constitution_endpoint_refuses_to_guess_across_two_trees():
    """P2 / K5：/latest 不带 root_id 且有多棵树时必须 409，不许"取最近一条"。"""
    client = TestClient(app)
    for goal in ("Build a recursive Codex control tower.", "Build another recursive Codex control tower."):
        constitution_id = locked_constitution(client, goal)
        started = client.post(
            "/api/projects/start",
            json={"goal": goal, "runner_mode": "mock", "constitution_id": constitution_id, "auto_run": False},
        )
        assert started.status_code == 200

    ambiguous = client.get("/api/constitutions/latest")
    assert ambiguous.status_code == 409
    assert "root_id" in ambiguous.json()["detail"]

    root_id = client.get("/api/tree").json()["tasks"][0]["id"]
    scoped = client.get(f"/api/constitutions/latest?root_id={root_id}")
    assert scoped.status_code == 200
    assert scoped.json()["root_id"] == root_id


def test_amend_endpoint_bumps_the_locked_constitution_version():
    """锁定的宪法不能再 `PUT` 改，但可以走"修订"：新增一版 + 迁移存量节点。"""
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    started = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    assert started.status_code == 200
    root_id = started.json()["root_id"]

    amended = client.post(
        f"/api/projects/{root_id}/constitution/amend",
        json={
            "markdown": "# Revised while running\n\nRaise the floor.",
            "data": {"global_acceptance": ["Every leaf must cite a reproducible command"]},
            "direction": "tighten",
            "actor": "operator",
            "note": "raise the floor",
        },
    )
    assert amended.status_code == 200
    body = amended.json()
    assert (body["from_version"], body["to_version"]) == (1, 2)
    assert body["direction"] == "tighten"
    assert body["migrated_count"] == 1  # 根节点是 planned → 直接换快照

    tree = client.get("/api/tree")
    assert tree.status_code == 200
    root = next(task for task in tree.json()["tasks"] if task["id"] == root_id)
    assert root["constitution_version"] == 2
    assert root["constitution_hash"] == body["content_hash"]

    current = client.get(f"/api/constitutions/{body['constitution_id']}")
    assert current.status_code == 200
    assert current.json()["supersedes_id"] == constitution_id
    assert current.json()["amendment_direction"] == "tighten"


def test_amend_endpoint_rejects_an_undeclared_direction_or_missing_actor():
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    started = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    root_id = started.json()["root_id"]

    missing_direction = client.post(
        f"/api/projects/{root_id}/constitution/amend",
        json={"markdown": "# v2", "data": {}, "actor": "operator"},
    )
    assert missing_direction.status_code == 422  # 方向必须显式声明，不能由服务端推断

    missing_actor = client.post(
        f"/api/projects/{root_id}/constitution/amend",
        json={"markdown": "# v2", "data": {}, "direction": "tighten", "actor": ""},
    )
    assert missing_actor.status_code == 422

    unknown_root = client.post(
        "/api/projects/no-such-root/constitution/amend",
        json={"markdown": "# v2", "data": {}, "direction": "tighten", "actor": "operator"},
    )
    assert unknown_root.status_code == 404


def test_task_detail_endpoint_returns_events_and_runs():
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    response = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    root_id = response.json()["root_id"]

    detail = client.get(f"/api/tasks/{root_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["task"]["id"] == root_id
    assert payload["events"]
    assert payload["runs"] == []


def test_cancel_all_endpoint_marks_current_tree_canceled():
    client = TestClient(app)
    constitution_id = locked_constitution(client, "Build a recursive Codex control tower.")
    response = client.post(
        "/api/projects/start",
        json={
            "goal": "Build a recursive Codex control tower.",
            "runner_mode": "mock",
            "constitution_id": constitution_id,
            "auto_run": False,
        },
    )
    assert response.status_code == 200

    cancel = client.post("/api/projects/cancel-all")
    assert cancel.status_code == 200
    assert cancel.json()["count"] == 1

    tree = client.get("/api/tree")
    assert tree.status_code == 200
    assert tree.json()["tasks"][0]["status"] == TaskStatus.CANCELED.value


def test_run_artifact_endpoint_returns_prompt_preview():
    client = TestClient(app)
    prompt_path = orchestrator.settings.workspace_dir / "runs" / "api-test-prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("prompt preview", encoding="utf-8")

    with SessionLocal() as db:
        task = TaskNode(id="api-artifact-task", title="Artifact", description="", depth=0, status=TaskStatus.DONE.value)
        db.add(task)
        db.flush()
        run = RunRecord(
            task_id=task.id,
            kind=RunKind.PLANNER.value,
            status=RunStatus.SUCCEEDED.value,
            command="test",
            sandbox="read-only",
            exit_code=0,
            result_json=dumps({"ok": True}),
            prompt_path=str(prompt_path),
        )
        db.add(run)
        db.commit()
        run_id = run.id

    response = client.get(f"/api/runs/{run_id}/artifact?kind=prompt")

    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"] is True
    assert payload["content"] == "prompt preview"
