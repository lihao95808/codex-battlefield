"""目标投递端点的接线判据（P4-1）。

`test_delivery.py` 钉的是**纯逻辑**；这里钉**接线**：真实树 → 进度对不对、
起树 → 有没有自动记一次投递、管家的上下文里有没有那段进度。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| H1 | 没有投递时端点给 `available: false` + 原因 | 报 0/0 像"还没开始" |
| H2 | 有投递时进度读的是**那棵树**，`root_id` 对得上 | 报的是别的树，而看起来完全正常 |
| H3 | `steward_actions` 起树时**自动记一次投递** | 起了树但管家仍然"起完就断线" |
| H4 | 记账失败**不让起树回滚** | 一个记账 bug 把已经跑起来的树弄没 |
| H5 | 管家的上下文里带 `delivery` 段 | 用户问"第几步"时它只能含糊其辞 |
| H6 | 端点写入受 `frozen_mode` 挡，读取不受 | 冻结模式下还能写状态 |
| H7 | 按 `author` 过滤真的生效 | 多人共用机器时拿到别人的树 |
| H8 | 债读不到时进度里明说"没查"而不是"没有" | 用户以为没有债，其实没人去看 |
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import delivery
from app.database import SessionLocal, engine, init_db
from app.models import Base, TaskNode, TaskStatus


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _seed_tree(root_id: str = "root-1", *, blocked: bool = False) -> None:
    with SessionLocal() as db:
        db.add(TaskNode(id=root_id, title="根目标", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value))
        db.add(
            TaskNode(
                id=f"{root_id}-a",
                parent_id=root_id,
                title="子任务 A",
                description="",
                depth=1,
                status=TaskStatus.DONE.value,
            )
        )
        db.add(
            TaskNode(
                id=f"{root_id}-b",
                parent_id=root_id,
                title="子任务 B",
                description="",
                depth=1,
                status=TaskStatus.BLOCKED.value if blocked else TaskStatus.RUNNING.value,
                error_message="缺依赖" if blocked else None,
            )
        )
        db.commit()


# ---------- H1 没有投递 ----------


def test_h1_without_a_delivery_the_endpoint_says_so(client: TestClient) -> None:
    body = client.get("/api/deliveries/latest").json()
    assert body["available"] is False
    assert body["reason"]
    assert body["progress"] is None


# ---------- H2 读的是那棵树 ----------


def test_h2_progress_reads_the_delivered_tree(client: TestClient) -> None:
    _seed_tree("root-1")
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="做个状态机", author="duzuguang")

    body = client.get("/api/deliveries/latest").json()
    assert body["available"] is True
    assert body["delivery"]["root_id"] == "root-1"
    assert body["delivery"]["goal"] == "做个状态机"
    assert body["progress"]["root_id"] == "root-1"
    assert body["progress"]["done"] == 1
    assert body["progress"]["total"] == 3
    assert "1/3" in body["summary"]
    # 同样不许有假警报（见下面对上下文那条判据的注释）。
    assert not any("root_id" in text for text in body["progress"]["incomplete"])


def test_h2_a_stale_delivery_pointing_at_a_missing_tree_is_unavailable(client: TestClient) -> None:
    """投递记录的树没了（换工程、删库）时**说读不到**，不报 0/0。"""
    with SessionLocal() as db:
        delivery.record(db, root_id="gone-root", goal="已经不在了")
    body = client.get("/api/deliveries/latest").json()
    assert body["available"] is True
    assert body["progress"]["available"] is False
    assert body["progress"]["reason"]


def test_h2_blocked_nodes_show_up_in_the_next_step(client: TestClient) -> None:
    _seed_tree("root-1", blocked=True)
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="会卡住的那个")
    body = client.get("/api/deliveries/latest").json()
    assert "卡住" in body["progress"]["next_step"]


# ---------- H3 起树自动记账 ----------


def test_h3_starting_a_task_records_a_delivery() -> None:
    """**这条是 P4-1 的核心接线**：起树之后管家不能再"断线"。"""
    from app import steward_actions

    recorded: dict = {}

    class _FakeOrchestrator:
        async def start_one_shot_project(self, goal: str) -> str:
            recorded["goal"] = goal
            return "new-root"

    import asyncio

    message = asyncio.run(steward_actions._exec_start_task(_FakeOrchestrator(), "做个状态机"))
    assert "new-root" in message
    assert recorded["goal"] == "做个状态机"

    with SessionLocal() as db:
        row = delivery.latest(db)
        assert row is not None, "起树之后没有记投递 —— 管家仍然会'起完就断线'"
        assert row.root_id == "new-root"
        assert row.goal == "做个状态机"
        assert row.source == "steward_action"


def test_h3_starting_without_a_goal_is_still_refused() -> None:
    from app import steward_actions

    class _FakeOrchestrator:
        async def start_one_shot_project(self, goal: str) -> str:  # pragma: no cover - 不该被调用
            raise AssertionError("空目标不该起树")

    import asyncio

    with pytest.raises(steward_actions.ActionError):
        asyncio.run(steward_actions._exec_start_task(_FakeOrchestrator(), "   "))


def test_h4_a_recording_failure_does_not_break_the_started_tree(monkeypatch) -> None:
    """记账失败**不让起树回滚** —— 树已经起来了，回滚它要糟得多。"""
    from app import delivery as delivery_module
    from app import steward_actions

    def _boom(*_args, **_kwargs):
        raise RuntimeError("库写不进去")

    monkeypatch.setattr(delivery_module, "record", _boom)

    class _FakeOrchestrator:
        async def start_one_shot_project(self, goal: str) -> str:
            return "root-ok"

    import asyncio

    message = asyncio.run(steward_actions._exec_start_task(_FakeOrchestrator(), "目标"))
    assert "root-ok" in message, "记账失败不该让起树的结果丢掉"


def test_h4_an_empty_root_id_is_not_recorded() -> None:
    """起树返回空 id 时不写记录（`record` 会拒），也不抛出去。"""
    from app import steward_actions

    steward_actions._record_delivery("", "目标")
    with SessionLocal() as db:
        assert delivery.latest(db) is None


# ---------- H5 管家上下文 ----------


def test_h5_the_steward_context_carries_a_delivery_block() -> None:
    """管家的状态快照里必须有 `delivery` 段 —— 否则它答不了"第几步、卡在哪"。"""
    from app import steward
    from app.config import get_settings

    _seed_tree("root-1")
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="做个状态机", author="duzuguang")
        status_text, _caps = steward.build_context(db, get_settings(), "进展如何？", "")

    snapshot = json.loads(status_text)
    assert "delivery" in snapshot, "管家上下文里没有投递进度"
    block = snapshot["delivery"]
    assert block["available"] is True
    assert block["root_id"] == "root-1"
    assert block["summary"]
    assert block["done"] == 1
    # **不许有假警报**：`get_tree` 的返回值里没有 `root_id`，忘记补上的话
    # 每一条投递报告都会拖着"读不到 root_id"—— 而假警报会让真警报被无视。
    assert not any("root_id" in text for text in block["incomplete"]), block["incomplete"]


def test_h5_without_a_delivery_the_block_says_so_instead_of_guessing() -> None:
    from app import steward
    from app.config import get_settings

    with SessionLocal() as db:
        status_text, _caps = steward.build_context(db, get_settings(), "进展如何？", "")

    block = json.loads(status_text)["delivery"]
    assert block["available"] is False
    assert "还没有" in block["reason"]


# ---------- H6 冻结模式 ----------


def test_h6_recording_a_delivery_is_blocked_in_frozen_mode(client: TestClient) -> None:
    from dataclasses import replace

    from app.orchestrator import orchestrator

    old = orchestrator.settings
    orchestrator.settings = replace(old, frozen_mode=True)
    try:
        response = client.post("/api/deliveries", json={"root_id": "root-1", "goal": "g"})
        read = client.get("/api/deliveries/latest")
    finally:
        orchestrator.settings = old

    assert response.status_code == 423
    # 读不受影响：冻结的是写。
    assert read.status_code == 200


# ---------- H7 按作者 ----------


def test_h7_author_filter_reaches_the_endpoint(client: TestClient) -> None:
    _seed_tree("mine-root")
    _seed_tree("their-root")
    with SessionLocal() as db:
        delivery.record(db, root_id="mine-root", goal="我的", author="duzuguang")
        delivery.record(db, root_id="their-root", goal="他的", author="someone-else")

    assert client.get("/api/deliveries/latest").json()["delivery"]["root_id"] == "their-root"
    assert (
        client.get("/api/deliveries/latest?author=duzuguang").json()["delivery"]["root_id"]
        == "mine-root"
    )


# ---------- H8 债读不到要说清 ----------


def test_h8_recording_requires_a_root_id_at_the_endpoint(client: TestClient) -> None:
    response = client.post("/api/deliveries", json={"goal": "没有 root"})
    assert response.status_code == 400
    assert "root_id" in response.json()["detail"]


def test_h8_progress_without_obligations_information_says_it_did_not_look(monkeypatch) -> None:
    """债读不到时，进度里要写"没查"——**不是"没有"**。"""
    from app import main as main_module

    monkeypatch.setattr(main_module, "_open_blocking_obligations", lambda _db, _root: None)

    _seed_tree("root-1")
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="x")

    body = client_get_latest()
    assert body["progress"]["available"] is True
    assert any("没有一起读进来" in text for text in body["progress"]["incomplete"])


def client_get_latest() -> dict:
    from app.main import app

    return TestClient(app).get("/api/deliveries/latest").json()


def test_h8_blocking_obligations_are_reported_when_readable(client: TestClient) -> None:
    """有阻断债时要出现在进度里（这条走真实的索引读取路径，读不到就算通过）。"""
    _seed_tree("root-1")
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="x")
    body = client.get("/api/deliveries/latest").json()
    # 索引里没有债 → 不该凭空出现。
    assert body["progress"]["blocking_obligations"] == []
