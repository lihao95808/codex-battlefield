"""宪法解析的回归测试（前置缺陷 P1）。

**为什么要有这个文件**：`_resolve_locked_constitution` 原先取「全库最近更新的 locked 记录」，
于是同一台机器上两个项目各自锁定宪法后，后启动的那个会**静默绑定到另一个项目的宪法**。
宪法变更传播要靠"当前生效版本"作判据，这种静默错绑会让版本判定从根上错掉。
这里把"显式或不歧义"的规则钉死。
"""

from __future__ import annotations


# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app.database import engine, init_db, SessionLocal
from app.models import Base, ConstitutionRecord
from app.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


async def _locked_constitution(orchestrator: Orchestrator, goal: str) -> str:
    constitution_id = await orchestrator.draft_constitution(goal, runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    return constitution_id


def _bind_root(constitution_id: str, root_id: str) -> None:
    with SessionLocal() as db:
        record = db.get(ConstitutionRecord, constitution_id)
        assert record is not None
        record.root_id = root_id
        db.commit()


@pytest.mark.asyncio
async def test_single_locked_constitution_is_used_without_explicit_id() -> None:
    """单项目流程必须与原先完全一致：不传 id 也能解析到唯一那条。"""
    orchestrator = Orchestrator()
    constitution_id = await _locked_constitution(orchestrator, "目标甲")

    with SessionLocal() as db:
        resolved = orchestrator._resolve_locked_constitution(db, None)
    assert resolved.id == constitution_id


@pytest.mark.asyncio
async def test_no_locked_constitution_reports_missing() -> None:
    """只草拟未锁定 → 明确提示，而不是解析到草稿。"""
    orchestrator = Orchestrator()
    await orchestrator.draft_constitution("目标甲", runner_mode="mock")

    with SessionLocal() as db:
        with pytest.raises(ValueError) as excinfo:
            orchestrator._resolve_locked_constitution(db, None)
    assert "必须先生成并锁定" in str(excinfo.value)


@pytest.mark.asyncio
async def test_explicit_id_must_be_locked() -> None:
    """显式传 id 时必须已锁定，草稿不能启动任务树。"""
    orchestrator = Orchestrator()
    draft_id = await orchestrator.draft_constitution("目标甲", runner_mode="mock")

    with SessionLocal() as db:
        with pytest.raises(ValueError) as excinfo:
            orchestrator._resolve_locked_constitution(db, draft_id)
    assert "尚未锁定" in str(excinfo.value)


@pytest.mark.asyncio
async def test_ambiguous_locked_constitutions_require_explicit_choice() -> None:
    """核心回归：两条都已绑定任务树时，**不得**猜最近一条，必须报错。

    这正是 P1 的原始缺陷形态——原先会把项目乙的启动静默绑到项目甲的宪法上。
    """
    orchestrator = Orchestrator()
    first = await _locked_constitution(orchestrator, "目标甲")
    second = await _locked_constitution(orchestrator, "目标乙")
    _bind_root(first, "root-甲")
    _bind_root(second, "root-乙")

    with SessionLocal() as db:
        with pytest.raises(ValueError) as excinfo:
            orchestrator._resolve_locked_constitution(db, None)
    message = str(excinfo.value)
    assert "constitution_id" in message
    # 报错必须带候选，否则调用方无法判断该传哪个
    assert "root-甲" in message or "root-乙" in message


@pytest.mark.asyncio
async def test_single_unbound_candidate_is_used() -> None:
    """支持「先草拟并锁定多个、再启动其中一个」：唯一的未绑定候选可直接用。"""
    orchestrator = Orchestrator()
    bound = await _locked_constitution(orchestrator, "旧项目")
    fresh = await _locked_constitution(orchestrator, "新项目")
    _bind_root(bound, "root-旧")

    with SessionLocal() as db:
        resolved = orchestrator._resolve_locked_constitution(db, None)
    assert resolved.id == fresh


@pytest.mark.asyncio
async def test_explicit_id_wins_even_when_bound() -> None:
    """显式指定时不受「已绑定」影响：用户明确要复用哪一版就是哪一版。"""
    orchestrator = Orchestrator()
    first = await _locked_constitution(orchestrator, "目标甲")
    await _locked_constitution(orchestrator, "目标乙")
    _bind_root(first, "root-甲")

    with SessionLocal() as db:
        resolved = orchestrator._resolve_locked_constitution(db, first)
    assert resolved.id == first
