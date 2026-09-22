"""R6 失败复盘：失败/阻塞子树必须进复盘，且能按 `category` / `retryable` / `parent_action` 查。

**这个文件的存在理由**：父层 skill note 原先只要求"读已完成子树"，恰好把最该总结的失败路径
排除在外。这一波要钉住的正是那条边界：

| 编号 | 要钉住的事 |
|---|---|
| G6 | 故意失败的子节点必须出现在**复盘产物**里（`docs/skill_notes/*_merged.json` 的 `failure_retrospective`） |
| 来源 | 四条证据来源都要收：`block_report` / `blocked_child_reports` / `error_message` / `result_json` |
| 可查 | 按 `category` / `retryable` / `parent_action` 聚合，且树级 `stats.failures` 能跨节点查出 |
| 不编造 | 复盘只搬运原始证据：没有失败的节点不进复盘，没有失败时不写空壳 |
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import failure_retrospective, skill_notes
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec
from app.utils import dumps


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


def _node(
    node_id: str,
    *,
    status: str = TaskStatus.BLOCKED.value,
    depth: int = 2,
    meta: dict | None = None,
    error: str | None = None,
    result: dict | None = None,
    parent_id: str | None = None,
) -> TaskNode:
    return TaskNode(
        id=node_id,
        parent_id=parent_id,
        title=f"节点 {node_id}",
        description="",
        depth=depth,
        status=status,
        node_meta_json=dumps(meta or {}),
        error_message=error,
        result_json=dumps(result) if result is not None else None,
    )


# --- 纯模块：证据抽取 -------------------------------------------------------


def test_entry_reads_block_report() -> None:
    task = _node(
        "n-block",
        meta={
            "block_report": {
                "category": "rate_limit",
                "retryable": True,
                "parent_action": "wait_and_retry",
                "reason": "HTTP 429 too many requests",
            }
        },
    )
    entry = failure_retrospective.entry_from_node(task)
    assert entry is not None
    assert entry["source"] == "block_report"
    assert entry["category"] == "rate_limit"
    assert entry["retryable"] is True
    assert entry["parent_action"] == "wait_and_retry"
    assert "429" in entry["reason"]


def test_entry_reads_error_message_and_worker_failure() -> None:
    from_error = failure_retrospective.entry_from_node(_node("n-err", error="权限不足，需要人工授权"))
    assert from_error is not None and from_error["source"] == "error_message"
    assert from_error["category"] == "unknown"  # 没有 block_report 时不编造类别

    from_result = failure_retrospective.entry_from_node(
        _node(
            "n-worker",
            status=TaskStatus.RUNNING.value,
            result={"worker": {"status": "failed", "summary": "编译报错", "changed_files": []}},
        )
    )
    assert from_result is not None and from_result["source"] == "result_json"
    assert from_result["worker_status"] == "failed"
    assert "编译报错" in from_result["reason"]


def test_entry_reads_blocked_child_reports() -> None:
    """父节点名下挂着阻塞子节点——**最常见的形态**，必须有自己的来源标注。

    第一版的三选一里没有 `blocked_child_reports`，这种节点被标成 `result_json`：
    它根本没有 worker 结果，而按 `source` 过滤的人永远查不到这一类（与"四条线都收"对不上）。
    """
    task = _node(
        "n-parent-of-blocked",
        status=TaskStatus.REPAIR.value,
        meta={
            "blocked_child_reports": [
                {"task_id": "c1", "category": "timeout", "retryable": True, "parent_action": "wait_and_retry", "reason": "超时"},
                {"task_id": "c2", "category": "human_auth", "retryable": False, "parent_action": "human_auth_required", "reason": "需要授权"},
            ]
        },
    )
    entry = failure_retrospective.entry_from_node(task)
    assert entry is not None
    assert entry["source"] == "blocked_child_reports"
    assert entry["derived_from_children"] is True
    assert entry["child_failure_ids"] == ["c1", "c2"]
    # 汇总条目自己没有类别时从被引用的子节点取——写 "unknown" 会让按类别查询凭空多一个假类别
    assert entry["category"] == "timeout"
    assert entry["retryable"] is True
    assert entry["blocked_child_count"] == 2
    assert [item["task_id"] for item in entry["blocked_child_reports"]] == ["c1", "c2"]
    assert entry["reason"] == "超时"  # 自己没有理由时，用子层第一条作线索


def test_clean_node_is_not_an_entry() -> None:
    assert failure_retrospective.entry_from_node(_node("n-ok", status=TaskStatus.DONE.value)) is None


def test_collect_and_summarize_aggregate_by_the_three_lines() -> None:
    tasks = [
        _node("b", depth=3, meta={"block_report": {"category": "timeout", "retryable": True, "parent_action": "wait_and_retry", "reason": "t"}}),
        _node("a", depth=2, meta={"block_report": {"category": "timeout", "retryable": True, "parent_action": "wait_and_retry", "reason": "t"}}),
        _node("c", depth=2, meta={"block_report": {"category": "human_auth", "retryable": False, "parent_action": "human_auth_required", "reason": "h"}}),
        _node("ok", depth=2, status=TaskStatus.DONE.value),
    ]
    entries = failure_retrospective.collect_entries(tasks)
    assert [item["node_id"] for item in entries] == ["a", "c", "b"]  # 浅的先看，同层按 id
    summary = failure_retrospective.summarize(entries)
    assert summary["total"] == 3
    assert summary["by_category"] == {"timeout": 2, "human_auth": 1}
    assert summary["by_retryable"] == {"not_retryable": 1, "retryable": 2}
    assert summary["by_parent_action"] == {"human_auth_required": 1, "wait_and_retry": 2}


def test_build_retrospective_shape_and_entry_limit() -> None:
    retrospective = failure_retrospective.build_retrospective(
        parent_id="p1",
        children=[_node(f"n{i}", meta={"block_report": {"category": "timeout", "retryable": True, "parent_action": "wait_and_retry", "reason": "t"}}) for i in range(4)],
        generated_at="2026-09-20T00:00:00+00:00",
        entry_limit=2,
    )
    assert retrospective["schema"] == "codex-battlefield.failure_retrospective.v1"
    assert retrospective["parent_task_id"] == "p1"
    assert retrospective["generated_at"] == "2026-09-20T00:00:00+00:00"
    assert retrospective["summary"]["total"] == 4
    assert len(retrospective["entries"]) == 2  # 明细有界，计数不受限
    assert retrospective["child_ids"] == ["n0", "n1", "n2", "n3"]


def test_board_does_not_double_count_derived_rollups() -> None:
    """同一件事不能算两次。

    父节点会从 `blocked_child_reports` 派生一条自己的条目（"我名下有哪些失败"），而那个子节点
    本来就在同一棵树里、也有自己的条目。不去重的话 `total` 是真实值的两倍，`by_category` 还会多出
    一个派生的 `unknown` —— 看板要回答的"这棵树在哪些类别上失败过"当场失真。
    """
    parent = _node(
        "p",
        depth=1,
        status=TaskStatus.WAITING_CHILDREN.value,
        meta={
            "blocked_child_reports": [
                {"task_id": "c", "category": "validation", "retryable": False, "parent_action": "manual_review", "reason": "子节点验证失败"}
            ]
        },
    )
    child = _node(
        "c",
        parent_id="p",
        status=TaskStatus.BLOCKED.value,
        meta={"block_report": {"category": "validation", "retryable": False, "parent_action": "manual_review", "reason": "验证失败"}},
    )

    board = failure_retrospective.board([parent, child])
    assert board["total"] == 1
    assert board["by_category"] == {"validation": 1}
    assert [item["node_id"] for item in board["entries"]] == ["c"]

    # 被引用的子节点不在这批任务里（例如只看父这一层）→ 汇总条目不能被顺手丢掉
    alone = failure_retrospective.board([parent])
    assert alone["total"] == 1
    assert alone["entries"][0]["node_id"] == "p"


def test_retrospective_block_is_empty_without_failures() -> None:
    assert failure_retrospective.retrospective_block(None) == ""
    assert failure_retrospective.retrospective_block({"entries": []}) == ""
    block = failure_retrospective.retrospective_block(
        {"summary": {"total": 1, "by_category": {"timeout": 1}, "by_retryable": {"retryable": 1}},
         "entries": [{"category": "timeout", "node_id": "n1", "status": "blocked", "parent_action": "wait_and_retry", "retryable": True, "reason": "超时"}]}
    )
    assert "FAILURE_RETROSPECTIVE" in block
    assert "n1" in block and "timeout" in block
    assert "不得只读已完成子树" in block


# --- 接线：skill note 采集范围 ---------------------------------------------


def _parent_with(retrospective: dict | None) -> TaskNode:
    meta = {failure_retrospective.RETROSPECTIVE_KEY: retrospective} if retrospective else {}
    return TaskNode(
        id="sp",
        title="父",
        description="",
        depth=1,
        status=TaskStatus.WAITING_CHILDREN.value,
        scope_json=dumps(["."]),
        node_meta_json=dumps(meta),
    )


def test_skill_note_child_carries_failure_evidence() -> None:
    retrospective = failure_retrospective.build_retrospective(
        parent_id="sp",
        children=[_node("n1", meta={"block_report": {"category": "timeout", "retryable": True, "parent_action": "wait_and_retry", "reason": "超时"}})],
        generated_at="2026-09-20T00:00:00+00:00",
    )
    spec = skill_notes.parent_skill_note_child(_parent_with(retrospective), [ChildTaskSpec(title="子", description="")])
    assert "FAILURE_RETROSPECTIVE" in spec.description
    assert "n1" in spec.description
    assert any("失败/阻塞子树必须各有一条经验" in item for item in spec.acceptance)
    assert any("category、retryable 与 parent_action" in item for item in spec.acceptance)


def test_skill_note_child_still_requires_failures_without_a_retrospective() -> None:
    """没有复盘数据时，要求本身不能消失——否则"没采集"会被读成"没失败"。"""
    spec = skill_notes.parent_skill_note_child(_parent_with(None), [ChildTaskSpec(title="子", description="")])
    assert "FAILURE_RETROSPECTIVE" not in spec.description
    assert "不能只读已完成子树" in spec.description
    assert any("失败/阻塞子树必须各有一条经验" in item for item in spec.acceptance)


# --- 接线：父层写入 + 复盘产物 + 树级可查 -----------------------------------


def _orchestrator(tmp_path: Path) -> Orchestrator:
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        runner_mode="real",
        parent_skill_note_task_enabled=True,
        workspace_dir=tmp_path / "workspace",
    )
    return Orchestrator(settings)


def _seed_tree(tmp_path: Path) -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.add(
            TaskNode(id="fr-root", title="Root", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value, scope_json=dumps([str(tmp_path / "target")]))
        )
        db.add(
            TaskNode(
                id="fr-parent",
                parent_id="fr-root",
                title="Parent",
                description="",
                depth=1,
                status=TaskStatus.WAITING_CHILDREN.value,
                worktree_path=str(artifacts),
                node_meta_json=dumps({}),
            )
        )
        db.add(
            TaskNode(
                id="fr-child-failed",
                parent_id="fr-parent",
                title="失败子节点",
                description="",
                depth=2,
                status=TaskStatus.FAILED.value,
                node_meta_json=dumps(
                    {"block_report": {"category": "validation", "retryable": False, "parent_action": "manual_review", "reason": "验证失败：产物缺文件"}}
                ),
            )
        )
        db.add(
            TaskNode(
                id="fr-child-ok",
                parent_id="fr-parent",
                title="成功子节点",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                node_meta_json=dumps({}),
            )
        )
        db.commit()
    return artifacts


def test_write_retrospective_records_failed_subtree_and_board_aggregates(tmp_path: Path) -> None:
    _seed_tree(tmp_path)  # 只为了落库，本用例不读产物目录
    orchestrator = _orchestrator(tmp_path)
    with SessionLocal() as db:
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        retrospective = orchestrator._write_failure_retrospective(db, parent)
        db.commit()
        tasks = list(db.scalars(select(TaskNode)).all())

    assert retrospective["summary"]["total"] == 1
    assert retrospective["entries"][0]["node_id"] == "fr-child-failed"
    assert retrospective["entries"][0]["category"] == "validation"
    assert retrospective["entries"][0]["parent_action"] == "manual_review"

    with SessionLocal() as db:
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        stored = json.loads(parent.node_meta_json)[failure_retrospective.RETROSPECTIVE_KEY]
        assert stored["summary"]["by_category"] == {"validation": 1}
        assert [event.type for event in db.scalars(select(EventRecord)).all()] == ["parent.failure_retrospective"]

    # 树级可查：按类别跨节点聚合
    board = orchestrator._tree_failure_board(tasks)
    assert board["total"] == 1
    assert board["by_category"] == {"validation": 1}
    assert board["by_retryable"] == {"not_retryable": 1}
    assert [item["node_id"] for item in board["entries"]] == ["fr-child-failed"]


def test_write_retrospective_removes_stale_shell_when_subtree_is_clean(tmp_path: Path) -> None:
    """没有失败条目时不写空壳，并把旧的一并摘掉：否则分不清"确实没失败"和"根本没采集"。"""
    _seed_tree(tmp_path)
    orchestrator = _orchestrator(tmp_path)
    with SessionLocal() as db:
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        meta = json.loads(parent.node_meta_json)
        meta[failure_retrospective.RETROSPECTIVE_KEY] = {"schema": "old", "entries": [{"node_id": "ghost"}]}
        parent.node_meta_json = dumps(meta)
        failed = db.get(TaskNode, "fr-child-failed")
        assert failed is not None
        failed.status = TaskStatus.DONE.value
        failed.node_meta_json = dumps({})
        db.commit()
        orchestrator._write_failure_retrospective(db, parent)
        db.commit()
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        assert failure_retrospective.RETROSPECTIVE_KEY not in json.loads(parent.node_meta_json)


def test_retrospective_is_injected_into_a_pending_skill_note_child(tmp_path: Path) -> None:
    """复盘是在**规划期之后**才产生的，note 子任务那时的描述里没有它。

    不补这一刀，"失败路径必须进经验库"在主链路上其实从未生效：note 子任务创建时拿到的是空块，
    之后也不会重建（父下已有 note 子节点就直接早退）。已经开跑的不能动——改不动一个正在执行的进程。
    """
    _seed_tree(tmp_path)
    orchestrator = _orchestrator(tmp_path)
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="fr-note",
                parent_id="fr-parent",
                title="父：归拢递归经验 Skill",
                description="读取本父节点所有已完成子树……",
                depth=2,
                status=TaskStatus.PLANNED.value,
                node_meta_json=dumps({"coverage_ids": [skill_notes.SKILL_NOTE_COVERAGE_ID]}),
            )
        )
        db.commit()
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        orchestrator._write_failure_retrospective(db, parent)
        db.commit()

        note = db.get(TaskNode, "fr-note")
        assert note is not None
        assert "FAILURE_RETROSPECTIVE" in note.description
        assert "fr-child-failed" in note.description
        assert [event.type for event in db.scalars(select(EventRecord)).all()] == [
            "parent.failure_retrospective",
            "parent.failure_retrospective_injected",
        ]

        # 已经开跑的不动它
        note.status = TaskStatus.RUNNING.value
        note.description = "已开跑"
        db.commit()
        orchestrator._write_failure_retrospective(db, parent)
        db.commit()
        assert db.get(TaskNode, "fr-note").description == "已开跑"


def test_consolidated_skill_note_product_contains_failure_entries(tmp_path: Path) -> None:
    """G6 的正身：**复盘产物**（`docs/skill_notes/<parent>_merged.json`）里必须出现该失败条目。"""
    artifacts = _seed_tree(tmp_path)
    orchestrator = _orchestrator(tmp_path)
    with SessionLocal() as db:
        parent = db.get(TaskNode, "fr-parent")
        assert parent is not None
        orchestrator._write_failure_retrospective(db, parent)
        orchestrator._consolidate_parent_skill_notes(db, parent)
        db.commit()

    product = artifacts / "docs" / "skill_notes" / "fr-parent_merged.json"
    assert product.is_file()
    payload = json.loads(product.read_text(encoding="utf-8"))
    assert payload["failure_retrospective"]["summary"]["by_category"] == {"validation": 1}
    assert payload["failure_retrospective"]["entries"][0]["node_id"] == "fr-child-failed"
    assert "验证失败" in payload["failure_retrospective"]["entries"][0]["reason"]
