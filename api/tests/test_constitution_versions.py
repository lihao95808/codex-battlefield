"""O-A4 回归测试：宪法版本下沉与判读。

**要证明的四件事**（缺一即未达成 spec 的验收）：
1. **版本单调**（O5）——子节点版本永不早于父节点，且不因"头版本落后"而降级；
2. **未登记不冒充已知**——存量行的版本是"未知"而不是"落后"，升级不会瞬间给全树记满债；
3. **迁移判读覆盖全部状态**（§6 裁决表）——新增 `TaskStatus` 成员不能静默落到"不处理"，
   未知状态保守处理（要求新宪法下的一次真实裁决）；
4. **方向敏感**（§7）——收紧必须有再验证债，放宽只上浮且**不**产生再验证债。

第 4 条是"放宽断言来变绿"的入口处防线，所以它既测 `constitution_stale` 的 severity，
也测 `revalidate_required` **根本不存在**。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest
from sqlalchemy import select

from app import constitution_versions as cv
from app import obligations
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, RunKind, RunRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec


@dataclass
class Node:
    id: str
    status: str
    constitution_version: int | None = None
    parent_id: str | None = None
    depth: int = 0


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


# --- 1. 版本单调（O5） -------------------------------------------------------


def test_child_version_is_never_below_parent_version():
    """O5 的主体：父节点已被升级、头版本却落后时，新节点**不得**降版本。"""
    assert cv.inherit_version(3, 1) == 3
    assert cv.inherit_version(1, 3) == 3
    assert cv.inherit_version(2, 2) == 2


def test_inherited_version_is_monotonic_over_a_dense_range():
    """把单调性做成性质而不是抽样：所有组合下都不得低于父版本。"""
    for parent in range(0, 6):
        for head in range(0, 6):
            inherited = cv.inherit_version(parent, head)
            assert inherited is not None
            assert inherited >= parent, (parent, head, inherited)


def test_unknown_parent_version_is_not_papered_over_with_known_head():
    """未知不得冒充已知：父节点版本未登记时，拿头版本给子节点盖章会造出半旧半新。"""
    assert cv.inherit_version(None, 4) is None
    assert cv.inherit_version(None, None) is None
    assert cv.inherit_version(2, None) == 2
    assert cv.inherit_version("", "not-a-number") is None


# --- 2. 落后 vs 未知 --------------------------------------------------------


def test_unregistered_version_is_unknown_not_stale():
    """存量行（本列引入前）不记债，但必须可见——靠 `unknown` 桶，不靠沉默。"""
    nodes = [Node("legacy", TaskStatus.DONE.value), Node("fresh", TaskStatus.DONE.value, 1)]
    assert cv.stale_nodes(nodes, 2) == [nodes[1]]
    assert cv.version_distribution(nodes) == {"unknown": 1, "1": 1}


def test_stale_requires_both_versions_known():
    assert cv.is_stale(Node("a", TaskStatus.DONE.value, 1), 2) is True
    assert cv.is_stale(Node("b", TaskStatus.DONE.value, 2), 2) is False
    assert cv.is_stale(Node("c", TaskStatus.DONE.value, 3), 2) is False
    assert cv.is_stale(Node("d", TaskStatus.DONE.value, None), 2) is False
    assert cv.is_stale(Node("e", TaskStatus.DONE.value, 1), None) is False


# --- 3. 迁移判读覆盖全部状态 -------------------------------------------------


def test_every_task_status_maps_to_a_known_action():
    """新增 `TaskStatus` 成员不能静默落到"不处理"——这是裁决表不腐烂的机器判据。"""
    for status in TaskStatus:
        action = cv.action_for_status(status.value)
        assert action in cv.ACTIONS, status


def test_unknown_status_is_treated_conservatively():
    assert cv.action_for_status("some-future-status") == cv.REVALIDATE
    assert cv.FALLBACK_ACTION == cv.REVALIDATE


def test_status_actions_match_the_designed_table():
    assert cv.action_for_status(TaskStatus.PLANNED.value) == cv.MIGRATE_NOW
    assert cv.action_for_status(TaskStatus.QUEUED.value) == cv.MIGRATE_NOW
    assert cv.action_for_status(TaskStatus.REPAIR.value) == cv.MIGRATE_NOW
    assert cv.action_for_status(TaskStatus.WAITING_CHILDREN.value) == cv.SELF_ONLY
    for working in (TaskStatus.PLANNING, TaskStatus.DECOMPOSING, TaskStatus.RUNNING, TaskStatus.WORKING):
        assert cv.action_for_status(working.value) == cv.WAIT_RUNNING
    for in_flight in (TaskStatus.VERIFYING, TaskStatus.ACCEPTING):
        assert cv.action_for_status(in_flight.value) == cv.WAIT_RUNNING
    for stopped in (TaskStatus.PAUSED, TaskStatus.HIBERNATING):
        assert cv.action_for_status(stopped.value) == cv.ON_RESUME
    for failed in (TaskStatus.BLOCKED, TaskStatus.FAILED):
        assert cv.action_for_status(failed.value) == cv.RECLASSIFY
    assert cv.action_for_status(TaskStatus.CANCELED.value) == cv.STAMP_ONLY


def test_migration_plan_only_buckets_stale_nodes_and_keeps_all_buckets():
    nodes = [
        Node("root", TaskStatus.WAITING_CHILDREN.value, 1),
        Node("planned-child", TaskStatus.PLANNED.value, 1, parent_id="root", depth=1),
        Node("running-child", TaskStatus.RUNNING.value, 1, parent_id="root", depth=1),
        Node("done-child", TaskStatus.DONE.value, 1, parent_id="root", depth=1),
        Node("failed-child", TaskStatus.FAILED.value, 1, parent_id="root", depth=1),
        Node("current", TaskStatus.DONE.value, 2, parent_id="root", depth=1),
    ]
    plan = cv.migration_plan(nodes, effective_version=2)
    assert set(plan) == set(cv.ACTIONS)
    assert plan[cv.MIGRATE_NOW] == ["planned-child"]
    assert plan[cv.WAIT_RUNNING] == ["running-child"]
    assert plan[cv.REVALIDATE] == ["done-child"]
    assert plan[cv.RECLASSIFY] == ["failed-child"]
    assert plan[cv.SELF_ONLY] == ["root"]
    assert plan[cv.STAMP_ONLY] == []
    assert all("current" not in bucket for bucket in plan.values())


# --- 4. 方向敏感 ------------------------------------------------------------


def test_only_tightening_forces_revalidation():
    assert cv.forces_revalidation(cv.DIRECTION_TIGHTEN) is True
    assert cv.forces_revalidation(cv.DIRECTION_MIXED) is True
    assert cv.forces_revalidation(cv.DIRECTION_LOOSEN) is False
    assert cv.forces_revalidation(cv.DIRECTION_NEUTRAL) is False


def test_undeclared_direction_is_treated_as_tightening():
    """没写方向 ≠ 放宽。默认取保守，否则"漏填"就成了静默放宽的入口。"""
    assert cv.normalize_direction("") == cv.DIRECTION_TIGHTEN
    assert cv.normalize_direction(None) == cv.DIRECTION_TIGHTEN
    assert cv.normalize_direction("loosened") == cv.DIRECTION_TIGHTEN
    assert cv.normalize_direction("LOOSEN") == cv.DIRECTION_LOOSEN


def test_done_node_under_loosen_is_stamped_not_revalidated():
    assert cv.action_for_status(TaskStatus.DONE.value, direction=cv.DIRECTION_LOOSEN) == cv.STAMP_ONLY
    assert cv.action_for_status(TaskStatus.DONE.value, direction=cv.DIRECTION_NEUTRAL) == cv.STAMP_ONLY
    assert cv.action_for_status(TaskStatus.DONE.value, direction=cv.DIRECTION_TIGHTEN) == cv.REVALIDATE


def test_tightening_produces_blocking_stale_and_revalidate_debt():
    nodes = [
        Node("root", TaskStatus.WAITING_CHILDREN.value, 2),
        Node("child", TaskStatus.DONE.value, 1, parent_id="root", depth=1),
    ]
    debt = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_TIGHTEN)
    assert set(debt) == {"root"}
    types = sorted(item["type"] for item in debt["root"])
    assert types == [obligations.CONSTITUTION_STALE, obligations.REVALIDATE_REQUIRED]
    assert obligations.has_blocking(debt["root"]) is True
    assert debt["root"][0]["depth"] == 1


def test_loosen_produces_advisory_debt_and_never_a_revalidate_obligation():
    """放宽只对未来生效：产物仍合规，不复查，更不得借新宪法洗白。"""
    nodes = [Node("child", TaskStatus.DONE.value, 1, parent_id="root", depth=1)]
    debt = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_LOOSEN)
    assert [item["type"] for item in debt["root"]] == [obligations.CONSTITUTION_STALE]
    assert obligations.has_blocking(debt["root"]) is False
    assert obligations.blocking(debt["root"]) == []


def test_blocked_node_is_never_whitelisted_by_a_loosened_amendment():
    """被放宽"洗白"最危险的对象是已 failed/blocked 的节点：放宽不得产生任何债清理动作。"""
    nodes = [Node("failed", TaskStatus.FAILED.value, 1, parent_id="root", depth=1)]
    debt = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_LOOSEN)
    assert all(item["type"] != obligations.REVALIDATE_REQUIRED for item in debt["root"])
    assert debt["root"][0]["severity"] == obligations.SEVERITY_ADVISORY


def test_stale_root_owns_its_own_debt():
    nodes = [Node("lonely-root", TaskStatus.DONE.value, 1)]
    debt = cv.version_debt(nodes, effective_version=2)
    assert set(debt) == {"lonely-root"}


# --- 传播看板（§9 / K7） ----------------------------------------------------


def _board_nodes() -> list[Node]:
    return [
        Node("root", TaskStatus.WAITING_CHILDREN.value, 2),
        Node("planned-child", TaskStatus.PLANNED.value, 2, parent_id="root", depth=1),
        Node("running-child", TaskStatus.WORKING.value, 1, parent_id="root", depth=1),
        Node("done-child", TaskStatus.DONE.value, 1, parent_id="root", depth=1),
        Node("legacy-child", TaskStatus.QUEUED.value, None, parent_id="root", depth=1),
    ]


def test_board_identities_hold():
    """K7：看板的数字必须自洽——不自洽的看板比没有看板更坏，因为它会被当成事实。"""
    nodes = _board_nodes()
    board = cv.version_board(nodes, effective_version=2, effective_hash="hash-v2")
    assert sum(board["version_distribution"].values()) == board["node_total"] == len(nodes)
    assert sum(len(ids) for ids in board["stale_by_action"].values()) == board["stale_total"]
    assert board["running_on_old_version"] == len(board["stale_by_action"][cv.WAIT_RUNNING])


def test_board_reports_how_many_versions_coexist():
    board = cv.version_board(_board_nodes(), effective_version=2, effective_hash="hash-v2")
    assert board["version_distribution"] == {"2": 2, "1": 2, "unknown": 1}
    assert board["effective_version"] == 2
    assert board["effective_hash"] == "hash-v2"


def test_board_buckets_by_action_and_marks_what_must_wait():
    board = cv.version_board(_board_nodes(), effective_version=2)
    assert board["stale_total"] == 2
    assert board["stale_by_action"][cv.WAIT_RUNNING] == ["running-child"]
    assert board["stale_by_action"][cv.REVALIDATE] == ["done-child"]
    assert board["running_on_old_version"] == 1


def test_board_returns_every_action_bucket_even_when_empty():
    """少一个键，消费方就会把"没有"读成"未知"。"""
    board = cv.version_board([Node("only", TaskStatus.PLANNED.value, 2)], effective_version=2)
    assert set(board["stale_by_action"]) == set(cv.ACTIONS)
    assert board["stale_total"] == 0


def test_board_carries_the_last_amendment_and_the_revalidate_queue():
    board = cv.version_board(
        _board_nodes(),
        effective_version=2,
        effective_hash="hash-v2",
        revalidate_queue_depth=3,
        last_amendment={"from_version": 1, "to_version": 2, "direction": "tighten", "actor": "operator"},
    )
    assert board["effective_hash"] == "hash-v2"
    assert board["revalidate_queue_depth"] == 3
    assert board["last_amendment"]["actor"] == "operator"


def test_board_without_a_constitution_says_so_instead_of_zero():
    """没绑定宪法时 `effective_version` 是 `None`——"没有"与"v0"是两件事。"""
    board = cv.version_board(_board_nodes(), effective_version=None)
    assert board["effective_version"] is None
    assert board["stale_total"] == 0


def test_version_debt_ids_do_not_contain_the_owner():
    """§6.4 教训的机器判据：id 里出现产出方，修复方一出现就会生出新 id 并无限增长。"""
    nodes = [
        Node("root", TaskStatus.WAITING_CHILDREN.value, 2),
        Node("child", TaskStatus.DONE.value, 1, parent_id="root", depth=1),
    ]
    first = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_TIGHTEN)
    second = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_TIGHTEN)
    assert [item["id"] for item in first["root"]] == [item["id"] for item in second["root"]]
    for item in first["root"]:
        assert item["id"] == f"{item['type']}:child:v1->v2"
        assert item["source_node_id"] == "child"


def test_repeated_version_debt_generation_is_idempotent():
    """同一件事重复判读不得重复记债（O3 的版本维度）。"""
    nodes = [
        Node("root", TaskStatus.WAITING_CHILDREN.value, 2),
        Node("child", TaskStatus.DONE.value, 1, parent_id="root", depth=1),
    ]
    stored: list[dict] = []
    for _ in range(3):
        incoming = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_TIGHTEN)
        stored = obligations.upsert(stored, incoming["root"])
    assert len(stored) == 2
    assert sum(int(item["recurrence_count"]) for item in stored) == 0
    assert obligations.summarize(stored)["by_type"] == {
        obligations.CONSTITUTION_STALE: 1,
        obligations.REVALIDATE_REQUIRED: 1,
    }


def test_settled_version_debt_does_not_come_back_as_a_blocker():
    """债清掉之后不得因为"版本还没追平"再次阻断——清债有界，门才会开。"""
    nodes = [Node("child", TaskStatus.DONE.value, 1, parent_id="root", depth=1)]
    debt = cv.version_debt(nodes, effective_version=2, direction=cv.DIRECTION_TIGHTEN)["root"]
    settled = obligations.settle(
        debt,
        debt[0]["id"],
        settled_by=obligations.SETTLED_BY_HUMAN,
        reason="人确认旧版产物仍合规",
        actor="operator",
    )
    assert obligations.summarize(settled)["settled_by_human_count"] == 1


# --- 5. 落库：版本真的写进节点与运行记录 -------------------------------------


async def _tree_with_one_child(orchestrator: Orchestrator) -> tuple[str, str]:
    constitution_id = await orchestrator.draft_constitution("为版本落库造一棵小树。", runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    root_id = await orchestrator.start_project(
        goal="为版本落库造一棵小树。",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        orchestrator._create_children(
            db,
            root,
            [
                ChildTaskSpec(
                    title="唯一子任务",
                    description="占位。",
                    acceptance=["必须给出证据。"],
                    coverage_ids=["evidence"],
                    estimated_leaf=True,
                )
            ],
        )
        db.commit()
        child_id = str(db.scalars(select(TaskNode.id).where(TaskNode.parent_id == root_id)).first() or "")
    return root_id, child_id


@pytest.mark.asyncio
async def test_root_and_child_and_run_records_carry_the_constitution_version():
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    root_id, child_id = await _tree_with_one_child(orchestrator)
    assert child_id
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        child = db.get(TaskNode, child_id)
        assert root is not None and child is not None
        assert root.constitution_version == 1
        assert child.constitution_version == 1
        orchestrator._record_synthetic_run(db, child, RunKind.ACCEPTANCE, "占位验收")
        db.commit()
        record = db.query(RunRecord).filter(RunRecord.task_id == child_id).one()
        assert record.constitution_version == 1
        assert record.constitution_hash == child.constitution_hash


@pytest.mark.asyncio
async def test_child_never_regresses_when_parent_is_ahead_of_the_head_version():
    """父节点版本高于头版本（异常场景）时，子节点必须跟着父节点，而不是降到头版本。"""
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    root_id, _ = await _tree_with_one_child(orchestrator)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        root.constitution_version = 5
        orchestrator._create_children(
            db,
            root,
            [
                ChildTaskSpec(
                    title="第二个子任务",
                    description="占位。",
                    acceptance=["必须给出证据。"],
                    coverage_ids=["evidence"],
                    estimated_leaf=True,
                )
            ],
        )
        db.commit()
        created = {
            str(child.title): child.constitution_version
            for child in db.query(TaskNode).filter(TaskNode.parent_id == root_id)
        }
    assert created == {"唯一子任务": 1, "第二个子任务": 5}


# --- 6. 增量迁移：旧库上可安全落地 -------------------------------------------


def test_incremental_migration_adds_version_columns_to_a_legacy_database(tmp_path, monkeypatch):
    """A2 的迁移判据：本列引入前的旧库必须能原地补列，且重复执行不报错。

    生产库就是"旧库"——`init_db()` 在启动时走同一段 ALTER，所以 DDL 写错只会在启动时炸。
    """
    from sqlalchemy import create_engine

    from app import database

    url = f"sqlite:///{(tmp_path / 'legacy.sqlite3').as_posix()}"
    legacy_engine = create_engine(url)
    # 旧库的样子：三张表都在（`create_all` 保证表存在），只是缺新列。
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE task_nodes (id VARCHAR(40) PRIMARY KEY, constitution_hash VARCHAR(80))")
        connection.exec_driver_sql("CREATE TABLE run_records (id INTEGER PRIMARY KEY, constitution_hash VARCHAR(80))")
        connection.exec_driver_sql("CREATE TABLE constitutions (id VARCHAR(40) PRIMARY KEY, version INTEGER)")
    monkeypatch.setattr(database, "engine", legacy_engine)
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_url=url))

    database._migrate_sqlite_columns()
    database._migrate_sqlite_columns()

    with legacy_engine.begin() as connection:
        task_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(task_nodes)")}
        run_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(run_records)")}
        constitution_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(constitutions)")}
    assert "constitution_version" in task_columns
    assert "constitution_version" in run_columns
    assert {"supersedes_id", "amendment_direction", "effective_at"} <= constitution_columns


def test_incremental_migration_tolerates_tables_that_do_not_exist_yet(tmp_path, monkeypatch):
    """**回归**：老库里还没有某张表时，补列不能把启动打挂。

    实测踩到（2026-10-15）：给 `steward_messages` 加了补列之后，
    旧的（只有三张表的）库上 `_migrate_sqlite_columns()` 直接
    `no such table: steward_messages` —— 而这段代码在 **API 启动路径**上，
    所以后果是"老库一律起不来"，不是某个功能不可用。

    修法是先查 `sqlite_master` 确认表存在，再补列。
    """
    from sqlalchemy import create_engine

    from app import database

    url = f"sqlite:///{(tmp_path / 'very_old.sqlite3').as_posix()}"
    legacy_engine = create_engine(url)
    # 一张新表都没有的"最老"的库
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE task_nodes (id VARCHAR(40) PRIMARY KEY)")
    monkeypatch.setattr(database, "engine", legacy_engine)
    monkeypatch.setattr(database, "settings", SimpleNamespace(database_url=url))

    # 不该抛
    database._migrate_sqlite_columns()

    with legacy_engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
    # 缺的表**不该**被补列顺手建出来——建表是 create_all 的活，不是这里的
    assert "steward_messages" not in tables

