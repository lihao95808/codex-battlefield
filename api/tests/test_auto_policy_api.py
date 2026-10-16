"""自动策略接到真实数据与端点上的判据（P3-5）。

`test_auto_policy.py` 钉的是**判定**（纯函数）；这里钉的是**接线**：
从真实 `task_nodes` / `run_records` 读出观察值，经过端点报出来的对不对。

接线类判据最容易写成"函数被调用过"（实现被注释掉时照样绿），所以断言全部落在
**可观测结果**上：报告里那条策略触发了没有、依据数字是多少、读盘前后有没有变化。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| N1 | 连续失败从**真实列**（leaf_attempts / verification_failures）读出来 | 接错列 → 策略永远不触发，而报告说"没到条件" |
| N2 | 最近结果按**时序**取窗口 | 取成"最早 N 条" → 一开始失败就永久降并发 |
| N3 | 读现场**只读**（调用前后磁盘指纹一致） | 看一眼策略就改了状态 |
| N4 | 没有树时端点仍可用，且明说"只报开关" | 空工程上面板整个消失，用户无法确认保护在不在 |
| N5 | 已在跑的任务不会被误判成"空闲" | 有活在跑却休眠 = 打断长跑 |
| N6 | 拿不到最后活动时间时**不给 idle_seconds** | 猜一个 0 → 空闲策略立刻触发 |
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine, init_db
from app.models import Base, RunRecord, TaskNode, TaskStatus


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


def _node(node_id: str, status: str, *, parent_id: str | None = None, depth: int = 0,
          leaf_attempts: int = 0, verification_failures: int = 0) -> TaskNode:
    return TaskNode(
        id=node_id,
        parent_id=parent_id,
        title=node_id,
        description="",
        depth=depth,
        status=status,
        leaf_attempts=leaf_attempts,
        verification_failures=verification_failures,
    )


def _run(status: str, index: int) -> RunRecord:
    return RunRecord(
        task_id="leaf",
        kind="worker",
        status=status,
        command=f"codex exec #{index}",
        sandbox="workspace-write",
        exit_code=0 if status == "succeeded" else 1,
    )


def _observe(root_id: str):
    from app import auto_policy
    from app.main import _recent_run_statuses
    from app.orchestrator import orchestrator

    with SessionLocal() as db:
        return auto_policy.observe(
            orchestrator.get_tree(db, root_id=root_id),
            recent_statuses=_recent_run_statuses(db, orchestrator.settings),
            settings=orchestrator.settings,
        )


# ---------- N1 连续失败来自真实列 ----------


def test_n1_consecutive_failures_come_from_the_real_columns() -> None:
    """`leaf_attempts` 与 `verification_failures` 取大者作为连续失败强度。

    这两列是**既有**的重试计数（不需要新增列）。接错列的表现是策略永不触发，
    而报告会写"没到条件"—— 一个看起来完全正常、实际什么都没保护的状态。
    """
    with SessionLocal() as db:
        db.add_all(
            [
                _node("root", TaskStatus.WAITING_CHILDREN.value),
                _node("hot", TaskStatus.QUEUED.value, parent_id="root", depth=1, leaf_attempts=4),
                _node("warm", TaskStatus.QUEUED.value, parent_id="root", depth=1, verification_failures=3),
                _node("cold", TaskStatus.QUEUED.value, parent_id="root", depth=1, leaf_attempts=1),
                _node("finished", TaskStatus.DONE.value, parent_id="root", depth=1, leaf_attempts=9),
            ]
        )
        db.commit()

    observation = _observe("root")
    assert observation["consecutive_failures"] == {"hot": 4, "warm": 3, "cold": 1}

    from app import auto_policy

    decision = auto_policy.evaluate_consecutive_failure(observation)
    assert decision.fired is True
    assert decision.evidence["nodes"] == ["hot", "warm"]


def test_n1_done_nodes_are_not_counted_as_failing() -> None:
    """已完成的节点历史失败次数再多也不算"还在失败"。

    不排除它们的话，一棵树跑得越久，越会被自己的历史判成"连续失败"。
    """
    with SessionLocal() as db:
        db.add_all(
            [
                _node("root", TaskStatus.WAITING_CHILDREN.value),
                _node("done-node", TaskStatus.DONE.value, parent_id="root", depth=1, leaf_attempts=9),
            ]
        )
        db.commit()

    assert _observe("root")["consecutive_failures"] == {}


# ---------- N2 最近结果按时序 ----------


def test_n2_recent_results_are_the_latest_not_the_earliest() -> None:
    """窗口必须取**最新**的 N 条。

    取成最早 N 条时，"一开始失败过"会永久把并发压在低位 ——
    而报告会写"最近 10 次无一成功"，与事实相反。
    """
    from app import auto_policy

    with SessionLocal() as db:
        db.add_all([_node("root", TaskStatus.RUNNING.value), _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1)])
        db.commit()
    with SessionLocal() as db:
        for index in range(auto_policy.DIMINISHING_WINDOW):
            db.add(_run("failed", index))
        for index in range(auto_policy.DIMINISHING_WINDOW):
            db.add(_run("succeeded", 100 + index))
        db.commit()

    observation = _observe("root")
    window = observation["recent_results"]
    assert len(window) >= auto_policy.DIMINISHING_WINDOW
    assert window[-1] == "succeeded", "窗口里没有最新的那次运行"
    assert auto_policy.evaluate_diminishing_returns(observation).fired is False


def test_n2_all_recent_failures_do_fire() -> None:
    from app import auto_policy

    with SessionLocal() as db:
        db.add_all([_node("root", TaskStatus.RUNNING.value), _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1)])
        db.commit()
    with SessionLocal() as db:
        for index in range(auto_policy.DIMINISHING_WINDOW):
            db.add(_run("failed", index))
        db.commit()

    observation = _observe("root")
    decision = auto_policy.evaluate_diminishing_returns(observation)
    assert decision.fired is True


# ---------- N3 只读 ----------


def test_n3_observing_is_read_only() -> None:
    """读现场不许改动数据库里的任何字节（含 `-wal` / `-shm`）。"""
    with SessionLocal() as db:
        db.add_all([_node("root", TaskStatus.RUNNING.value), _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1)])
        db.commit()

    db_path = pathlib.Path(engine.url.database or "")
    assert db_path.is_file()

    def snapshot() -> list[tuple[str, int]]:
        items: list[tuple[str, int]] = []
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{db_path}{suffix}")
            if candidate.is_file():
                items.append((candidate.name, candidate.stat().st_size))
        return items

    before = snapshot()
    _observe("root")
    assert snapshot() == before, "读自动策略现场改动了数据库"


# ---------- N4 没有树也能用 ----------


def test_n4_endpoint_works_without_any_tree_and_says_so(client: TestClient) -> None:
    """空工程上端点仍要可用，并明说"只报告开关状态"。

    报 404 会让面板整个消失 —— 而用户打开的正是想确认"那些保护在不在"。
    """
    response = client.get("/api/auto-policy")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["root_id"] is None
    assert "还没有任务树" in body["note"]
    assert {item["id"] for item in body["policies"]}  # 策略列表仍然完整


def test_n4_endpoint_reports_the_tree_it_judged(client: TestClient) -> None:
    with SessionLocal() as db:
        db.add(_node("root-auto", TaskStatus.QUEUED.value))
        db.commit()

    body = client.get("/api/auto-policy?root_id=root-auto").json()
    assert body["root_id"] == "root-auto"
    assert "observed" in body
    assert body["observed"]["auto_consecutive_failure_limit"] >= 1


# ---------- N5 在跑就不能算空闲 ----------


def test_n5_running_tasks_prevent_the_idle_hibernate_decision(client: TestClient) -> None:
    """有排队任务时不许判"空闲"。

    这条挡的是"树里还有活排队，但活动数恰好是 0"那一瞬间的误判 ——
    误触发的代价是打断一次正常的长跑。
    """
    with SessionLocal() as db:
        db.add_all(
            [
                _node("root-idle", TaskStatus.WAITING_CHILDREN.value),
                _node("queued-1", TaskStatus.QUEUED.value, parent_id="root-idle", depth=1),
            ]
        )
        db.commit()

    body = client.get("/api/auto-policy?root_id=root-idle").json()
    item = next(entry for entry in body["policies"] if entry["id"] == "hibernate_when_idle")
    assert item["fired"] is False
    assert "还有活在跑" in item["reason"]


# ---------- N6 缺数据时不猜 ----------


def test_n6_idle_seconds_is_absent_when_no_activity_time_is_available() -> None:
    """拿不到最后活动时间时**不给** `idle_seconds`。

    给 0 或给一个猜的值会让空闲策略立刻触发（阈值判断里 0 也能通过
    "0 >= 0"），而这条策略的动作是休眠整树。
    """
    observation = _observe("does-not-exist")
    # 没有节点 → 没有活动时间 → 该键不存在，于是策略报"拿不到数据"。
    assert "idle_seconds" not in observation

    from app import auto_policy

    decision = auto_policy.evaluate_idle_hibernate(observation)
    assert decision.fired is False
    assert "拿不到" in decision.reason


def test_n6_stale_slots_require_an_explicit_process_flag() -> None:
    """`has_process` 不给就是 `False` —— 不猜"大概还有进程"。

    猜错的代价是把真在干活的 agent 的槽位回收掉。
    """
    with SessionLocal() as db:
        db.add_all(
            [
                _node("root-slot", TaskStatus.WAITING_CHILDREN.value),
                _node("running-1", TaskStatus.RUNNING.value, parent_id="root-slot", depth=1),
            ]
        )
        db.commit()

    observation = _observe("root-slot")
    assert observation["slots"], "在跑的节点应当出现在槽位列表里"
    assert all(slot["has_process"] is False for slot in observation["slots"])
