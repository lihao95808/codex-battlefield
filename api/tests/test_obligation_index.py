"""债务派生索引（`obligation_index` 表）的回归测试。

**这张表的危险之处**：它看起来像"债务的又一权威来源"，实际只是 `node_meta` 的投影。
所以这里钉的正是"漂移必须被报出来"和"消失的债必须被删掉"——这两条坏掉，读表的人会拿到
**看起来更严格、实际是假证据**的结论。
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app import obligation_index, obligations
from app.database import SessionLocal, init_db
from app.models import ObligationIndex, TaskNode, TaskStatus

BLOCKING = obligations.SEVERITY_BLOCKING
NODE_IDS = (
    "root-idx",
    "node-idx",
    "root-a",
    "node-a",
    "root-b",
    "node-b",
    "root-stamp",
    "node-stamp",
    "root-drift",
    "node-drift",
)
# 删节点必须**子先父后**：`task_nodes.parent_id` 是外键，先删父会在子还引用它时报 FK 失败。
NODE_IDS_CHILD_FIRST = (
    "node-idx",
    "root-idx",
    "node-a",
    "root-a",
    "node-b",
    "root-b",
    "node-stamp",
    "root-stamp",
    "node-drift",
    "root-drift",
)


def _cleanup() -> None:
    with SessionLocal() as db:
        for node_id in NODE_IDS_CHILD_FIRST:
            db.query(ObligationIndex).filter(ObligationIndex.node_id == node_id).delete()
            db.query(TaskNode).filter(TaskNode.id == node_id).delete()
        db.commit()


@pytest.fixture(autouse=True)
def _database() -> None:
    """建表 + 只清掉**本模块自己的**行。

    为什么按 id 精确清而不是清空全表：`-n auto` 下同一个 worker 会跑多个测试模块、共用同一个库文件，
    清空 `task_nodes` 会把别的模块的种子数据一起删掉（那会变成"换个分片才失败"的假失败）。
    """
    init_db()
    _cleanup()
    yield
    _cleanup()


def _node(node_id: str, *, parent_id: str | None = None, depth: int = 0, meta: dict | None = None) -> TaskNode:
    return TaskNode(
        id=node_id,
        parent_id=parent_id,
        title=f"节点 {node_id}",
        description="x",
        depth=depth,
        status=TaskStatus.PLANNED.value,
        node_meta_json=json.dumps(meta or {}),
    )


def _obligation(*, obligation_id: str, kind: str = "constraint_coverage_gap", settled: bool = False) -> dict:
    return {
        "id": obligation_id,
        "type": kind,
        "source_node_id": "root-1",
        "severity": BLOCKING,
        "created_at": "2026-09-22T00:00:00+00:00",
        "settled_at": "2026-09-22T01:00:00+00:00" if settled else None,
        "recurrence_count": 0,
        "depth": 1,
        "payload": {"requirement_version": 3},
    }


def _seed(root_id: str = "root-idx", node_id: str = "node-idx") -> None:
    with SessionLocal() as db:
        root = _node(root_id, meta={"obligations": [_obligation(obligation_id=f"ob-{root_id}")]})
        child = _node(
            node_id,
            parent_id=root_id,
            depth=1,
            meta={"obligations": [_obligation(obligation_id=f"ob-{node_id}", kind="requirement_uncovered")]},
        )
        db.add_all([root, child])
        db.commit()


def test_rebuild_projects_node_meta_and_check_reports_no_drift() -> None:
    _seed()
    with SessionLocal() as db:
        summary = obligation_index.rebuild(db, "root-idx")
        db.commit()
        assert summary["created"] == 2
        assert summary["removed"] == 0
        report = obligation_index.check(db, "root-idx")
        assert report["in_sync"] is True
        assert report["indexed"] == report["projected"] == 2
        rows = {row.id: row for row in db.query(ObligationIndex).all()}
        assert rows["ob-node-idx"].root_id == "root-idx"
        assert rows["ob-node-idx"].depth == 1
        assert rows["ob-node-idx"].kind == "requirement_uncovered"
        assert rows["ob-node-idx"].status == "open"
        assert rows["ob-node-idx"].evidence_version == "3"


def test_check_reports_drift_without_fixing_it() -> None:
    """真相变了而没重建 → `check` 必须点名，且**不许**顺手改表（自动修会抹掉"为什么漂了"）。"""
    _seed()
    with SessionLocal() as db:
        obligation_index.rebuild(db, "root-idx")
        db.commit()
        node = db.get(TaskNode, "node-idx")
        node.node_meta_json = json.dumps(
            {"obligations": [_obligation(obligation_id="ob-node-idx", settled=True)]}
        )
        db.commit()

        report = obligation_index.check(db, "root-idx")
        assert report["in_sync"] is False
        assert "ob-node-idx:status" in report["changed"]
        # 只读：表里那行还是老的
        assert db.get(ObligationIndex, "ob-node-idx").status == "open"


def test_rebuild_removes_obligations_that_disappeared() -> None:
    """债从 node_meta 里没了 → 索引行必须删掉：留着的行会被读成"这条债还在"（假证据）。"""
    _seed()
    with SessionLocal() as db:
        obligation_index.rebuild(db, "root-idx")
        db.commit()
        node = db.get(TaskNode, "node-idx")
        node.node_meta_json = json.dumps({"obligations": []})
        db.commit()

        summary = obligation_index.rebuild(db, "root-idx")
        db.commit()
        assert summary["removed"] == 1
        assert db.get(ObligationIndex, "ob-node-idx") is None
        assert obligation_index.check(db, "root-idx")["in_sync"] is True


def test_cross_tree_by_type_aggregates_over_trees() -> None:
    """本表存在的唯一理由：按类型跨树聚合（JSON 里查不动）。

    断言写成**按树 / 按类型的相对关系**而不是全局总数：同一个 worker 里别的模块也会往库里放节点，
    写死 `total == 4` 会变成"换个分片就失败"的假失败。
    """
    _seed("root-a", "node-a")
    _seed("root-b", "node-b")
    with SessionLocal() as db:
        obligation_index.rebuild(db)
        db.commit()
        board = obligation_index.cross_tree_by_type(db, status="open")
        assert board["by_root"]["root-a"] == 2
        assert board["by_root"]["root-b"] == 2
        assert sum(board["by_type"].values()) == board["total"] == sum(board["by_root"].values())
        assert board["by_type"]["constraint_coverage_gap"] >= 2
        assert board["blocking_by_type"]["constraint_coverage_gap"] >= 2
        assert board["built_at"] is not None
        everything = obligation_index.cross_tree_by_type(db, status="")
        assert everything["total"] == board["total"], "这两条树里没有被清过的债"
        settled = obligation_index.cross_tree_by_type(db, status="settled")
        assert settled["by_root"].get("root-a") is None


def test_board_pairs_the_aggregate_with_the_drift_state() -> None:
    """读面必须**同时**给聚合与漂移状态：缓存过期时 `total` 不构成读数。

    这是本表最贵的误读方向——表里 0 条 vs 真相里 3 条，只看 `total` 的人会得出
    "没有债"的结论，而事实相反。所以 `built_at is None`（从没建过）与 `in_sync is False`
    都要出现，`total` 一律照实给表里的值，**不**顺手用真相回填（回填会让看板看不出该重建）。
    """
    _seed("root-board", "node-board")
    with SessionLocal() as db:
        never_built = obligation_index.board(db, status="open")
        assert never_built["total"] == 0
        assert never_built["built_at"] is None
        assert never_built["in_sync"] is False
        assert never_built["drift_counts"]["missing"] >= 2
        assert never_built["rebuild_command"].endswith("rebuild_obligation_index.py --rebuild")

        obligation_index.rebuild(db)
        db.commit()
        fresh = obligation_index.board(db, status="open")
        assert fresh["total"] == sum(fresh["by_root"].values())
        assert fresh["by_root"]["root-board"] == 2
        assert fresh["in_sync"] is True
        assert fresh["built_at"] is not None
        assert fresh["drift_counts"] == {"missing": 0, "extra": 0, "changed": 0}


def test_board_status_scope_is_explicit_and_rejects_unknown_values() -> None:
    """`open` / `settled` / `all` 是三个不同的问题；问错范围**不能被静默当成默认范围**。"""
    _seed("root-scope", "node-scope")
    with SessionLocal() as db:
        node = db.get(TaskNode, "node-scope")
        node.node_meta_json = json.dumps({"obligations": [_obligation(obligation_id="ob-node-scope", settled=True)]})
        db.commit()
        obligation_index.rebuild(db)
        db.commit()

        open_board = obligation_index.board(db, status="open")
        assert open_board["by_root"].get("root-scope") == 1, "根那条仍然开着"
        assert "ob-node-scope" not in {
            row.id for row in db.query(ObligationIndex).filter(ObligationIndex.status == "open")
        }
        settled_board = obligation_index.board(db, status="settled")
        assert settled_board["by_root"]["root-scope"] == 1
        all_board = obligation_index.board(db, status="all")
        assert all_board["total"] == open_board["total"] + settled_board["total"]
        with pytest.raises(ValueError):
            obligation_index.board(db, status="openish")


def test_board_built_at_is_a_property_of_the_table_not_of_the_asked_scope() -> None:
    """`built_at` 必须是**全表**的快照时刻，不能是"本范围那批行"的时刻（审计修正 2026-09-24）。

    旧实现取过滤后子集的 max，于是 `total == 0` **恒等于** `built_at is None`：索引明明刚
    重建过、只是这个范围里确实没有债，也会被读成"索引未建、读不出来"——"确实清干净了"
    这个状态因此无法表达（界面上那句"这个范围里没有债务"永远走不到）。

    构造上刻意让两个范围的行**带不同的时间戳**（先全建一次，再清掉子节点那条后重建：
    只有变动的行会更新 `built_at`），所以三个范围读到同一个值这件事是有区分度的，
    在旧实现下必然失败。
    """
    _seed("root-stamp", "node-stamp")
    with SessionLocal() as db:
        obligation_index.rebuild(db)
        db.commit()
        child = db.get(TaskNode, "node-stamp")
        child.node_meta_json = json.dumps(
            {"obligations": [_obligation(obligation_id="ob-node-stamp", kind="requirement_uncovered", settled=True)]}
        )
        db.commit()
        obligation_index.rebuild(db)
        db.commit()

        table_built_at = db.scalar(select(func.max(ObligationIndex.built_at)))
        open_board = obligation_index.board(db, status="open")
        settled_board = obligation_index.board(db, status="settled")
        all_board = obligation_index.board(db, status="all")
        assert open_board["built_at"] == settled_board["built_at"] == all_board["built_at"] == table_built_at
        # 这棵树里 open 只剩根那条、settled 只有子那条——两个范围的行确实是分开的。
        assert open_board["by_root"]["root-stamp"] == 1
        assert settled_board["by_root"]["root-stamp"] == 1
        # `projected`（真相里几条）是"0 到底可不可信"的依据：表空且 projected==0 才是真的没有债。
        assert open_board["projected"] >= 2
        assert open_board["indexed"] == open_board["projected"]
        assert open_board["in_sync"] is True


def test_touching_a_node_without_touching_its_obligations_is_not_drift() -> None:
    """节点 mtime 变了**不算**漂移；债务内容变了必须报（反面自检，防这条判据恒假）。

    `orchestrator` 里有 17 处 `task.updated_at = utcnow()`（每次状态流转都动）。把
    `source_updated_at` 算进漂移判据的后果是：活跃树上 `in_sync` 近乎恒假，于是"真漂移"
    （债务凭空多/少）与"只是节点被动过"都显示成"变 N"，读的人分不开 → 两种都被忽略。
    """
    _seed("root-drift", "node-drift")
    with SessionLocal() as db:
        obligation_index.rebuild(db)
        db.commit()
        assert obligation_index.check(db, "root-drift")["in_sync"] is True

        node = db.get(TaskNode, "node-drift")
        node.updated_at = node.updated_at + timedelta(hours=3)
        db.commit()
        after_touch = obligation_index.check(db, "root-drift")
        assert after_touch["in_sync"] is True, "只动 updated_at 不构成漂移"
        assert after_touch["changed"] == []

        # 反面自检：真的改了债务（严重度降级）必须照样报出来，否则上面那条断言就是恒真。
        node.node_meta_json = json.dumps(
            {
                "obligations": [
                    {
                        **_obligation(obligation_id="ob-node-drift", kind="requirement_uncovered"),
                        "severity": obligations.SEVERITY_ADVISORY,
                    }
                ]
            }
        )
        db.commit()
        after_edit = obligation_index.check(db, "root-drift")
        assert after_edit["in_sync"] is False
        assert "ob-node-drift:severity" in after_edit["changed"]
