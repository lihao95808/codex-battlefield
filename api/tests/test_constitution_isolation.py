"""K5 回归测试：跨 root 的宪法隔离。

`docs/spec-constitution-amendment.md` §4 的 P2 是"传播无处落地"的根因之一：一个 root 的节点
拿到**另一个 root** 的宪法，版本判据整体判错。这里测的不是"能不能读出来"，而是**读错的时候
会不会报错**——多棵树并存时宁可 409，也不许猜一条最近的给人。
"""

from __future__ import annotations


# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, ConstitutionRecord, TaskNode
from app.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


async def _two_trees() -> tuple[Orchestrator, str, str, str, str]:
    """两棵树，各自的宪法，各自再修订一版。返回 (orchestrator, root_a, root_b, const_a, const_b)。"""
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    roots: list[str] = []
    constitutions: list[str] = []
    for goal in ("第一棵树的宪法。", "第二棵树的宪法。"):
        constitution_id = await orchestrator.draft_constitution(goal, runner_mode="mock")
        orchestrator.lock_constitution(constitution_id)
        root_id = await orchestrator.start_project(
            goal=goal,
            target_path=None,
            runner_mode="mock",
            constitution_id=constitution_id,
            auto_run=False,
        )
        roots.append(root_id)
        constitutions.append(constitution_id)
    orchestrator.amend_constitution(
        roots[0],
        markdown="# 第一棵树 v2\n",
        data={"global_acceptance": ["只属于第一棵树"]},
        direction="tighten",
        actor="operator",
    )
    return orchestrator, roots[0], roots[1], constitutions[0], constitutions[1]


@pytest.mark.asyncio
async def test_each_root_reads_its_own_effective_constitution():
    """K5：两棵树并存，各读各的——第二棵树不得被第一棵树的修订影响。"""
    orchestrator, root_a, root_b, const_a, const_b = await _two_trees()
    with SessionLocal() as db:
        head_a = orchestrator.get_constitution(db, root_id=root_a)
        head_b = orchestrator.get_constitution(db, root_id=root_b)

    assert head_a is not None and head_b is not None
    assert head_a["version"] == 2 and head_a["supersedes_id"] == const_a
    assert head_b["version"] == 1 and head_b["id"] == const_b
    assert head_a["data"] == {"global_acceptance": ["只属于第一棵树"]}


@pytest.mark.asyncio
async def test_latest_refuses_to_guess_when_two_trees_have_constitutions():
    """多棵树并存时**必须报错**，不许"取最近更新的一条"——那正是 P2 的错绑。"""
    orchestrator, _root_a, _root_b, _const_a, _const_b = await _two_trees()
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            orchestrator.get_constitution(db)


@pytest.mark.asyncio
async def test_latest_still_works_for_a_single_tree():
    """单树是常见路径，不能因为防歧义把它一起弄坏。"""
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    constitution_id = await orchestrator.draft_constitution("单树。", runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    with SessionLocal() as db:
        latest = orchestrator.get_constitution(db)
        empty_root = orchestrator.get_constitution(db, root_id="不存在的树")
    assert latest is not None and latest["id"] == constitution_id
    assert empty_root is None


@pytest.mark.asyncio
async def test_latest_prefers_the_effective_version_of_a_single_root_chain():
    """同一个 root 有多版时返回**当前生效**的那一版，而不是"最近更新的那条"。"""
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    constitution_id = await orchestrator.draft_constitution("单树多版。", runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    root_id = await orchestrator.start_project(
        goal="单树多版。", target_path=None, runner_mode="mock", constitution_id=constitution_id, auto_run=False
    )
    orchestrator.amend_constitution(
        root_id, markdown="# v2\n", data={"global_acceptance": ["新版"]}, direction="tighten", actor="operator"
    )
    with SessionLocal() as db:
        latest = orchestrator.get_constitution(db)
    assert latest is not None
    assert latest["version"] == 2
    assert latest["root_id"] == root_id


@pytest.mark.asyncio
async def test_node_snapshots_stay_bound_to_their_own_tree_after_an_amendment():
    """修订只碰自己那棵树：第二棵树的节点快照与版本戳必须像素级不变。"""
    orchestrator, root_a, root_b, const_a, const_b = await _two_trees()
    with SessionLocal() as db:
        node_a = db.get(TaskNode, root_a)
        node_b = db.get(TaskNode, root_b)
        assert node_a is not None and node_b is not None
        assert node_a.constitution_version == 2
        assert node_b.constitution_version == 1
        assert node_b.constitution_id == const_b
        assert node_b.constitution_hash != node_a.constitution_hash
        assert db.query(ConstitutionRecord).filter(ConstitutionRecord.root_id == root_b).count() == 1
