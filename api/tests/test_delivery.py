"""目标投递与进度的判据（P4-1）。

要解决的不是"能不能起树"，而是"**起完就断线**"。所以判据分两类：

* **进度是不是可核对的**：`第几步` 必须是 `12/30` 这种能对上的数，而不是"大概一半"；
* **取不到时会不会编**：读不到就说明原因，**不报 0/0**。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| G1 | 完成率的分母是节点数；**分母为 0 时给 `None` 不是 0** | 0% 被读成"一点没做" |
| G2 | **取消掉的分支不算完成** | "进度 90%" 读起来像"快好了"，而真相反 |
| G3 | 前线 = **最深**的未完成节点 | 报最浅的会让人以为还早 |
| G4 | 卡点优先于进度说话 | 用户以为一切正常，直到很久后才发现有人在等 |
| G5 | **没查债 ≠ 没有债** | 把"没查"读成"没有"，于是没人去看 |
| G6 | 投递记录必须带 `root_id` | "我投过这个目标"看起来像发生过，其实起树失败了 |
| G7 | 多人共用机器时按 `author` 取最近一次 | 拿到别人的树 |
| G8 | 空树给 `available: false` + 原因 | 报 0/0 像"还没开始"，而可能读错了树 |
"""
from __future__ import annotations

import pytest

from app import delivery
from app.database import SessionLocal, engine, init_db
from app.models import Base, GoalDelivery


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _task(node_id: str, status: str, *, depth: int = 0, error: str = "") -> dict:
    return {"id": node_id, "title": f"任务 {node_id}", "status": status, "depth": depth, "error_message": error}


def _tree(*tasks: dict, root_id: str = "root-1") -> dict:
    return {"root_id": root_id, "tasks": list(tasks)}


def _progress(tree: dict, **over):
    params: dict = {"open_obligations": []}
    params.update(over)
    return delivery.progress(tree, **params)


# ---------- G1 完成率 ----------


def test_g1_ratio_is_over_node_count() -> None:
    snapshot = _progress(_tree(_task("a", "done"), _task("b", "done"), _task("c", "running"), _task("d", "queued")))
    assert snapshot["done"] == 2
    assert snapshot["total"] == 4
    assert snapshot["ratio"] == 0.5


def test_g1_ratio_is_none_when_there_is_nothing_to_divide_by() -> None:
    """分母为 0 时给 `None`，**不给 0** —— 0% 会被读成"一点没做"。"""
    snapshot = _progress(_tree())
    assert snapshot["ratio"] is None
    assert snapshot["available"] is False


def test_g1_all_done_is_ratio_one() -> None:
    snapshot = _progress(_tree(_task("a", "done")))
    assert snapshot["ratio"] == 1.0
    assert "都已结算" in snapshot["next_step"]


# ---------- G2 取消不算完成 ----------


def test_g2_canceled_branches_are_not_counted_as_done() -> None:
    """取消掉的分支**没有被做出来**。

    把它算进完成率会让"进度 90%"读起来像"快好了"，而真相是"有一部分你决定不做了"
    —— 两者对用户的下一步动作完全不同。
    """
    snapshot = _progress(
        _tree(_task("a", "done"), _task("b", "done"), _task("c", "canceled"), _task("d", "failed"))
    )
    assert snapshot["done"] == 2
    assert snapshot["total"] == 4
    assert snapshot["ratio"] == 0.5


def test_g2_blocked_and_failed_are_not_done() -> None:
    snapshot = _progress(_tree(_task("a", "blocked"), _task("b", "failed")))
    assert snapshot["done"] == 0
    assert snapshot["ratio"] == 0.0


# ---------- G3 前线 ----------


def test_g3_frontier_is_the_deepest_unfinished_nodes() -> None:
    """"第几步"最接近的答案是**最深的那一层** —— 报最浅的会让人以为还早。"""
    snapshot = _progress(
        _tree(
            _task("shallow", "running", depth=1),
            _task("mid", "waiting_children", depth=3),
            _task("deep", "running", depth=7),
            _task("done-very-deep", "done", depth=9),
        )
    )
    assert [item["id"] for item in snapshot["frontier"]] == ["deep", "mid", "shallow"]


def test_g3_frontier_excludes_terminal_nodes() -> None:
    """已结算的节点不是"前线"，哪怕它最深。"""
    snapshot = _progress(_tree(_task("finished", "done", depth=99), _task("live", "running", depth=1)))
    assert [item["id"] for item in snapshot["frontier"]] == ["live"]


def test_g3_frontier_is_bounded() -> None:
    """前线列多了没人读 —— 而"卡在哪"通常就是那一两个。"""
    tasks = [_task(f"n{index}", "running", depth=index) for index in range(20)]
    snapshot = _progress(_tree(*tasks))
    assert len(snapshot["frontier"]) == delivery.FRONTIER_LIMIT
    # 深的在前。
    assert snapshot["frontier"][0]["depth"] == 19


# ---------- G4 卡点优先 ----------


def test_g4_blocking_obligations_outrank_progress() -> None:
    """有阻断债时先说那个 —— 否则用户以为一切正常，直到很久后才发现有人在等。"""
    snapshot = _progress(
        _tree(_task("a", "done"), _task("b", "running")),
        open_obligations=[{"id": "ob1", "type": "child_blocked", "severity": "blocking", "node_id": "b"}],
    )
    assert "阻断债" in snapshot["next_step"]
    assert "1" in snapshot["next_step"]


def test_g4_stuck_nodes_outrank_progress_too() -> None:
    snapshot = _progress(_tree(_task("a", "done"), _task("b", "blocked", error="缺依赖")))
    assert "卡住" in snapshot["next_step"]
    assert "b" in snapshot["next_step"]


def test_g4_advisory_obligations_do_not_count_as_stuck() -> None:
    """只有 advisory 债不算卡点 —— 把它当卡点会让"需要动手"天天亮着，然后被无视。"""
    snapshot = _progress(
        _tree(_task("a", "done"), _task("b", "running")),
        open_obligations=[{"id": "ob1", "type": "integration_skipped", "severity": "advisory"}],
    )
    assert "阻断债" not in snapshot["next_step"]
    assert snapshot["blocking_obligations"] == []


def test_g4_progress_only_when_nothing_needs_a_human() -> None:
    snapshot = _progress(_tree(_task("a", "done"), _task("b", "running", depth=2)))
    assert "最深" in snapshot["next_step"]
    assert "b" in snapshot["next_step"]


# ---------- G5 没查债 ≠ 没有债 ----------


def test_g5_not_looking_for_obligations_is_not_the_same_as_having_none() -> None:
    """`open_obligations=None` 表示**调用方没去查**。

    把它读成"没有债"是这一层最容易犯的错：用户会得到一个干净的进度，
    而实际上可能有阻断债在等人。
    """
    snapshot = delivery.progress(_tree(_task("a", "done")), open_obligations=None)
    assert any("没有一起读进来" in text for text in snapshot["incomplete"])
    assert any("不等于" in text for text in snapshot["incomplete"])


def test_g5_explicitly_empty_obligations_are_clean() -> None:
    """显式传空列表 = "查过了，没有债" —— 这时不该有任何 incomplete。"""
    snapshot = _progress(_tree(_task("a", "done"), _task("b", "running")))
    assert snapshot["incomplete"] == []


def test_g5_missing_root_id_is_reported() -> None:
    snapshot = _progress({"tasks": [_task("a", "done")]})
    assert any("root_id" in text for text in snapshot["incomplete"])


# ---------- G8 空树 ----------


def test_g8_empty_tree_says_unavailable_with_a_reason() -> None:
    """报 0/0 像"还没开始"，而可能读错了树 —— 必须说清。"""
    snapshot = _progress(_tree())
    assert snapshot["available"] is False
    assert snapshot["reason"]
    assert "读不到" in delivery.summary_line(snapshot)


def test_g8_summary_line_never_shows_a_fake_zero_ratio() -> None:
    line = delivery.summary_line(_progress(_tree()))
    assert "0%" not in line
    assert "0/0" not in line


def test_g8_summary_line_reads_like_a_sentence() -> None:
    line = delivery.summary_line(_progress(_tree(_task("a", "done"), _task("b", "running"))))
    assert "1/2" in line
    assert "50%" in line


# ---------- G6 投递记录 ----------


def test_g6_recording_requires_a_root_id() -> None:
    """一次"投递"的定义就是"确实起出了一棵树"。

    起树失败时留下一条投递记录，会让"我投过这个目标"看起来像发生过。
    """
    with SessionLocal() as db:
        with pytest.raises(ValueError) as excinfo:
            delivery.record(db, root_id="  ", goal="做个状态机")
    assert "root_id" in str(excinfo.value)


def test_g6_recording_persists_and_reads_back() -> None:
    with SessionLocal() as db:
        delivery.record(db, root_id="root-1", goal="做个状态机", author="duzuguang")
        row = delivery.latest(db)
        assert row is not None
        payload = delivery.payload(row)
    assert payload["root_id"] == "root-1"
    assert payload["goal"] == "做个状态机"
    assert payload["author"] == "duzuguang"
    assert payload["source"] == "steward_action"
    assert payload["created_at"]


def test_g6_latest_is_the_newest() -> None:
    with SessionLocal() as db:
        delivery.record(db, root_id="first", goal="一")
        delivery.record(db, root_id="second", goal="二")
        assert delivery.latest(db).root_id == "second"


def test_g6_latest_is_none_when_nothing_was_delivered() -> None:
    with SessionLocal() as db:
        assert delivery.latest(db) is None


# ---------- G7 按作者过滤 ----------


def test_g7_latest_can_be_scoped_to_one_author() -> None:
    """多人共用一台机器时，"我投的那个"不能拿到别人的树。"""
    with SessionLocal() as db:
        delivery.record(db, root_id="mine", goal="我的", author="duzuguang")
        delivery.record(db, root_id="theirs", goal="他的", author="someone-else")
        assert delivery.latest(db).root_id == "theirs"
        assert delivery.latest(db, author="duzuguang").root_id == "mine"


def test_g7_author_may_be_empty() -> None:
    """没有登录体系时"谁"只能是自报的 —— 允许为空，不假知道。"""
    with SessionLocal() as db:
        row = delivery.record(db, root_id="r", goal="g")
        assert row.author == ""
        assert delivery.payload(row)["author"] == ""


def test_g7_table_is_created_by_init_db() -> None:
    from sqlalchemy import inspect

    assert "goal_deliveries" in inspect(engine).get_table_names()


def test_g7_goal_and_author_are_trimmed() -> None:
    with SessionLocal() as db:
        row = delivery.record(db, root_id="  r  ", goal="  g  ", author="  me  ")
        assert row.root_id == "r"
        assert row.goal == "g"
        assert row.author == "me"


def test_g7_records_are_ordered_by_id_not_by_clock() -> None:
    """同一秒内多次投递时，靠时钟排序会给出错误的结果。"""
    with SessionLocal() as db:
        ids = [delivery.record(db, root_id=f"r{index}", goal=str(index)).id for index in range(5)]
        assert ids == sorted(ids)
        assert delivery.latest(db).root_id == "r4"

    assert GoalDelivery.__tablename__ == "goal_deliveries"
