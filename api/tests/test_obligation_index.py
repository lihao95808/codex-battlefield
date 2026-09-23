"""债务派生索引（`obligation_index` 表）的回归测试。

**这张表的危险之处**：它看起来像"债务的又一权威来源"，实际只是 `node_meta` 的投影。
所以这里钉的正是"漂移必须被报出来"和"消失的债必须被删掉"——这两条坏掉，读表的人会拿到
**看起来更严格、实际是假证据**的结论。
"""

from __future__ import annotations

import json

import pytest

from app import obligation_index, obligations
from app.database import SessionLocal, init_db
from app.models import ObligationIndex, TaskNode, TaskStatus

BLOCKING = obligations.SEVERITY_BLOCKING
NODE_IDS = ("root-idx", "node-idx", "root-a", "node-a", "root-b", "node-b")
# 删节点必须**子先父后**：`task_nodes.parent_id` 是外键，先删父会在子还引用它时报 FK 失败。
NODE_IDS_CHILD_FIRST = ("node-idx", "root-idx", "node-a", "root-a", "node-b", "root-b")


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
