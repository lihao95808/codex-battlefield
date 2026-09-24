"""O-A2 / O-A3 回归测试：`integration_conflict_strategy=repair` 档下的债务门控。

**这个文件的存在理由**：把债务表接进 `_accept_parent` 的第一版实现引入了**自增强循环**——
门控自己创建修复子任务 → 修复子任务成为新的产出方 → 下一轮生成新债 → 再建子任务，
债务与子任务一起无限增长（单个用例 5993 秒后 `RecursionError`，另一次全量跑 13 小时）。
所以这里测的不是"门控能拦住"，而是**它在被踩爆的那两个方向上被钉住了**：

| 编号 | 要钉住的事 |
|---|---|
| O1 | 子节点全 `done` 但存在未清集成冲突时，父节点**不得** done（只 `repair` 档） |
| §6.4-1 | 门控**不创建子任务**：拦下前后子节点数不变 |
| §6.4-2 | 清债**有界**：某件事本轮集成不再复现即结清，不依赖谁完成了什么 |
| 有界 | 连续拦下超过上限即升级为人工阻塞，不是无限次 `repair` |
| O3 | 重复集成不产生重复债务（同一件事在任何轮次都是同一条债） |
| 反静默放宽 | 版本类债务**不**会被"这一轮集成没提到它"顺手清掉 |
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest
from sqlalchemy import select

from app import integration, obligations
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import BaseCodexRunner, RunnerEnvelope
from app.schemas import AcceptanceResult
from app.utils import dumps


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


class AcceptancePassOnlyRunner(BaseCodexRunner):
    """验收一律 pass：门控要拦住的正是"agent 说 pass，但冲突还在"这种情况。"""

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def verify(self, task: TaskNode, worker_result) -> RunnerEnvelope:
        raise NotImplementedError

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return RunnerEnvelope(
            command="test acceptance",
            exit_code=0,
            logs_path="logs/acceptance.jsonl",
            prompt_path="prompts/acceptance.txt",
            last_message_path="last/acceptance.json",
            workdir="workdirs/acceptance",
            payload=AcceptanceResult(verdict="pass", reason="Accepted deliverables.").model_dump(),
        )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(tmp_path: Path, *, strategy: str) -> tuple[Orchestrator, str, Path]:
    """造"根 + 父节点 + 两个都写 `src/shared.gd` 的子节点"，冲突在集成时出现。"""
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        integration_conflict_strategy=strategy,
    )
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    worktree_a = tmp_path / "wt-a"
    worktree_b = tmp_path / "wt-b"
    _write(worktree_a / "src" / "shared.gd", "FROM_A")
    _write(worktree_b / "src" / "shared.gd", "FROM_B")

    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="gate-root",
                title="Root",
                description="Owns target.",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
                scope_json=dumps([str(target)]),
            )
        )
        db.add(
            TaskNode(
                id="gate-parent",
                parent_id="gate-root",
                title="Parent",
                description="Integrate children.",
                depth=1,
                status=TaskStatus.ACCEPTING.value,
            )
        )
        for child_id, worktree in (("gate-child-a", worktree_a), ("gate-child-b", worktree_b)):
            db.add(
                TaskNode(
                    id=child_id,
                    parent_id="gate-parent",
                    title=child_id,
                    description="",
                    depth=2,
                    status=TaskStatus.DONE.value,
                    worktree_path=str(worktree),
                    result_json=dumps(
                        {"worker": {"status": "succeeded", "summary": "s", "changed_files": ["src/shared.gd"]}}
                    ),
                )
            )
        db.commit()
    return orchestrator, "gate-parent", target


def _parent() -> TaskNode:
    with SessionLocal() as db:
        task = db.get(TaskNode, "gate-parent")
        assert task is not None
        db.expunge(task)
        return task


def _obligations_of_parent() -> list[dict]:
    return json.loads(_parent().node_meta_json).get("obligations") or []


def _obligations_of(node_id: str) -> list[dict]:
    with SessionLocal() as db:
        node = db.get(TaskNode, node_id)
        assert node is not None
        return json.loads(node.node_meta_json).get("obligations") or []


def _child_count() -> int:
    with SessionLocal() as db:
        return db.query(TaskNode).filter(TaskNode.parent_id == "gate-parent").count()


def _event_types() -> list[str]:
    with SessionLocal() as db:
        return [event.type for event in db.query(EventRecord).all()]


# --- O1 / §6.4-1 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_refuses_done_when_a_conflict_obligation_is_unsettled(tmp_path: Path) -> None:
    orchestrator, parent_id, target = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    children_before = _child_count()

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    parent = _parent()
    assert parent.status == TaskStatus.REPAIR.value
    assert parent.status != TaskStatus.DONE.value
    # §6.4 硬约束 1：门控**不创建子任务**——第一版就是在这里自己建修复子任务的
    assert _child_count() == children_before
    assert "parent.obligation_gate_blocked" in _event_types()
    debt = _obligations_of_parent()
    assert [item["type"] for item in debt] == [obligations.INTEGRATION_CONFLICT]
    assert obligations.has_blocking(debt) is True
    assert json.loads(parent.node_meta_json)["obligation_gate"]["consecutive_refusals"] == 1
    # 默认档行为不受影响：产物还是后写者，冲突只是被记下来并且现在会拦下 done
    assert (target / "src" / "shared.gd").read_text(encoding="utf-8") == "FROM_B"


@pytest.mark.asyncio
async def test_default_overwrite_strategy_is_not_gated(tmp_path: Path) -> None:
    """默认 `overwrite` 档语义是"记录并上浮，后写仍覆盖"，不是阻断——别把它悄悄改成阻断。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_OVERWRITE)

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    assert _parent().status == TaskStatus.DONE.value
    # 债务仍然被记录（记录与阻断是两件事）
    assert [item["type"] for item in _obligations_of_parent()] == [obligations.INTEGRATION_CONFLICT]
    assert "parent.integration_obligation_gate_blocked" not in _event_types()


# --- O3 / §6.4-2 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_integration_rounds_do_not_grow_obligations(tmp_path: Path) -> None:
    """同一件事在任意轮次都落到同一条债上——债务不能随轮次增长。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    for _ in range(3):
        with SessionLocal() as db:
            parent = db.get(TaskNode, parent_id)
            assert parent is not None
            children = list(db.query(TaskNode).filter(TaskNode.parent_id == parent_id))
            orchestrator._integrate_child_outputs(db, parent, children)
            db.commit()
    assert len(_obligations_of_parent()) == 1


@pytest.mark.asyncio
async def test_conflict_that_no_longer_reproduces_settles_and_unblocks_done(tmp_path: Path) -> None:
    """§6.4 硬约束 2：清债条件是"本轮集成不再复现"，天然有界，不依赖谁完成了什么。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    semaphore = asyncio.Semaphore(1)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)
    assert _parent().status == TaskStatus.REPAIR.value

    # 让产出方不再重叠：第二个子节点改去写另一条路径
    with SessionLocal() as db:
        child_b = db.get(TaskNode, "gate-child-b")
        assert child_b is not None
        child_b.result_json = dumps(
            {"worker": {"status": "succeeded", "summary": "s", "changed_files": ["src/other.gd"]}}
        )
        worktree_b = Path(str(child_b.worktree_path or ""))
        db.commit()
    _write(worktree_b / "src" / "other.gd", "FROM_B_OTHER")
    with SessionLocal() as db:
        child_b = db.get(TaskNode, "gate-child-b")
        assert child_b is not None
        Path(str(child_b.worktree_path or ""), "src", "shared.gd").unlink()

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)

    parent = _parent()
    assert parent.status == TaskStatus.DONE.value
    debt = _obligations_of_parent()
    assert len(debt) == 1
    assert debt[0]["settled_by"] == obligations.SETTLED_BY_ROUND_CLEAR
    assert debt[0]["settle_reason"]
    assert obligations.summarize(debt)["blocking_total"] == 0


@pytest.mark.asyncio
async def test_gate_escalates_to_a_human_after_the_bounded_number_of_refusals(tmp_path: Path) -> None:
    """有界：连续拦下达到上限后升级为人工阻塞，而不是无限次 `repair`。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    limit = orchestrator.settings.obligation_gate_max_refusals
    assert limit >= 1
    semaphore = asyncio.Semaphore(1)
    children_before = _child_count()

    for _ in range(limit):
        await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)

    parent = _parent()
    assert parent.status == TaskStatus.BLOCKED.value
    assert _child_count() == children_before
    assert "parent.obligation_gate_escalated" in _event_types()
    block_report = json.loads(parent.node_meta_json).get("block_report")
    assert block_report is not None
    assert block_report.get("source") == "obligation.gate"


# --- §8.2 逃生舱：人清债之后门必须真的开 ----------------------------------


@pytest.mark.asyncio
async def test_human_settlement_reopens_the_gate(tmp_path: Path) -> None:
    """没有这条通路时，"升级为人工"就是死胡同：树被 blocked 之后只能改库。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    semaphore = asyncio.Semaphore(1)
    for _ in range(orchestrator.settings.obligation_gate_max_refusals):
        await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)
    assert _parent().status == TaskStatus.BLOCKED.value
    obligation_id = _obligations_of_parent()[0]["id"]

    result = orchestrator.settle_obligation(
        parent_id, obligation_id, actor="operator", reason="人确认这是有意的共享文件覆盖"
    )

    assert result["obligations"]["blocking_total"] == 0
    debt = _obligations_of_parent()
    assert debt[0]["settled_by"] == obligations.SETTLED_BY_HUMAN
    assert debt[0]["settled_actor"] == "operator"
    assert "obligation.settled_human" in _event_types()
    # 人清债之后"连续被拦几次"归零：否则这次放行只换来一次机会
    assert "obligation_gate" not in json.loads(_parent().node_meta_json)

    # 门必须真的开：清债后同一节点再走一次判据不得再被拦
    with SessionLocal() as db:
        task = db.get(TaskNode, parent_id)
        assert task is not None
        assert orchestrator._obligation_gate(db, task) is False
    # 注意：`blocked` 是终态，重新跑起来是既有的 `POST /api/tasks/{id}/retry` 的事——
    # "这条债无害"与"继续干"是两个判断，本方法不越权替用户决定后者。
    assert _parent().status == TaskStatus.BLOCKED.value


@pytest.mark.asyncio
async def test_human_settlement_requires_reason_and_actor(tmp_path: Path) -> None:
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))
    obligation_id = _obligations_of_parent()[0]["id"]

    for actor, reason in (("", "有理由"), ("operator", ""), ("  ", "  ")):
        with pytest.raises(ValueError):
            orchestrator.settle_obligation(parent_id, obligation_id, actor=actor, reason=reason)
    # 拒了之后债还是债：不许被半清
    assert obligations.has_blocking(_obligations_of_parent()) is True


@pytest.mark.asyncio
async def test_settling_an_unknown_obligation_is_rejected(tmp_path: Path) -> None:
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))
    with pytest.raises(KeyError):
        orchestrator.settle_obligation(parent_id, "integration_conflict:parent-1:nope.gd", actor="operator", reason="r")


# --- §7.2.3 上浮事实：整棵树卡在第几层要看得见 ------------------------------


@pytest.mark.asyncio
async def test_a_stuck_branch_rises_one_level_as_a_single_fact(tmp_path: Path) -> None:
    """不是把子树那一串债复制上来，只记"我这一支还有未清事项"这一条。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))
    assert obligations.has_blocking(_obligations_of_parent()) is True

    with SessionLocal() as db:
        root = db.get(TaskNode, "gate-root")
        assert root is not None
        orchestrator._refresh_upward_blocking_facts(db, root)
        db.commit()

    facts = _obligations_of("gate-root")
    assert [item["id"] for item in facts] == [f"child_blocked:gate-root:{parent_id}"]
    assert facts[0]["payload"]["blocking_total"] == 1
    assert facts[0]["payload"]["by_type"] == {obligations.INTEGRATION_CONFLICT: 1}
    # 一层一条：债有多少条，上浮事实都只有一条
    assert facts[0]["payload"]["blocked_node_id"] == parent_id


@pytest.mark.asyncio
async def test_the_upward_fact_clears_when_the_branch_unblocks(tmp_path: Path) -> None:
    """清债按事实：那一支已经没有未清事项了，上浮事实就不再成立。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))
    with SessionLocal() as db:
        root = db.get(TaskNode, "gate-root")
        assert root is not None
        orchestrator._refresh_upward_blocking_facts(db, root)
        db.commit()
    assert obligations.has_blocking(_obligations_of("gate-root")) is True

    orchestrator.settle_obligation(
        parent_id, _obligations_of_parent()[0]["id"], actor="operator", reason="人确认这支的冲突无害"
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, "gate-root")
        assert root is not None
        orchestrator._refresh_upward_blocking_facts(db, root)
        db.commit()

    facts = _obligations_of("gate-root")
    assert facts[0]["settled_by"] == obligations.SETTLED_BY_UPWARD_CLEAR
    assert obligations.blocking(facts) == []


@pytest.mark.asyncio
async def test_refresh_happens_on_the_natural_path_even_when_children_are_stuck(tmp_path: Path) -> None:
    """不能只在"子节点全 done"之后刷新。

    这个节点停止推进的大多数情形（子节点阻塞/修复中/等待）都会各自 `return`，
    放在那之后等于大多数时候不跑，"整棵树卡在几层"就永远看不到。这里造一个**子节点未完成**
    的自然场景，只跑真实流程、不手工调用刷新。
    """
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    # 让父节点的子节点留在"未完成"状态：根节点这次验收必然走等待分支
    with SessionLocal() as db:
        child = db.get(TaskNode, "gate-child-a")
        assert child is not None
        child.status = TaskStatus.REPAIR.value
        root = db.get(TaskNode, "gate-root")
        assert root is not None
        # 先人为给父节点留一条未清阻断债，制造"这一支还有事"的事实
        meta = json.loads(root.node_meta_json)
        meta["obligations"] = []
        root.node_meta_json = dumps(meta)
        parent = db.get(TaskNode, parent_id)
        assert parent is not None
        parent_meta = json.loads(parent.node_meta_json)
        parent_meta["obligations"] = obligations.upsert(
            [], obligations.obligations_from_integration(parent_id, {"conflicts": [{"path": "src/shared.gd"}]}, strategy="repair")
        )
        parent.node_meta_json = dumps(parent_meta)
        db.commit()

    await orchestrator._accept_parent("gate-root", AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    facts = _obligations_of("gate-root")
    assert [item["id"] for item in facts] == [f"child_blocked:gate-root:{parent_id}"]
    assert obligations.has_blocking(facts) is True


@pytest.mark.asyncio
async def test_the_upward_fact_shows_up_on_the_tree_board(tmp_path: Path) -> None:
    """看板的用途：不打开任何节点就能看出"卡在第几层"。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))
    with SessionLocal() as db:
        root = db.get(TaskNode, "gate-root")
        assert root is not None
        orchestrator._refresh_upward_blocking_facts(db, root)
        db.commit()
        tasks = list(db.scalars(select(TaskNode)).all())
        board = orchestrator._tree_obligation_board(tasks)

    # 父节点欠 1 条（集成冲突），根节点欠 1 条（"那一支还有未清事项"的上浮事实）
    assert board["by_type"] == {obligations.INTEGRATION_CONFLICT: 1, obligations.CHILD_BLOCKED: 1}
    # 按层读：上浮事实在 depth 0 的根上，冲突在 depth 1 的父节点上——这就是"卡在第几层"
    assert board["by_depth"] == {"0": 1, "1": 1}
    assert board["oldest_blocking_age_seconds"] is not None


# --- 反静默放宽 ------------------------------------------------------------

@pytest.mark.asyncio
async def test_version_debt_is_never_cleared_by_an_integration_round(tmp_path: Path) -> None:
    """版本类债务不由某一轮集成定义，绝不能因为"这一轮没提到它"被顺手清掉。"""
    orchestrator, parent_id, _ = _seed(tmp_path, strategy=integration.STRATEGY_REPAIR)
    with SessionLocal() as db:
        parent = db.get(TaskNode, parent_id)
        assert parent is not None
        meta = json.loads(parent.node_meta_json)
        meta["obligations"] = [
            obligations.make_obligation(
                obligations.CONSTITUTION_STALE, source_node_id="gate-child-a", discriminator="v1->v2"
            )
        ]
        parent.node_meta_json = dumps(meta)
        children = list(db.query(TaskNode).filter(TaskNode.parent_id == parent_id))
        orchestrator._integrate_child_outputs(db, parent, children)
        db.commit()
        stored = json.loads(parent.node_meta_json)["obligations"]

    by_type = {item["type"]: item for item in stored}
    assert by_type[obligations.CONSTITUTION_STALE]["settled_at"] is None
    assert by_type[obligations.CONSTITUTION_STALE]["settled_by"] is None
    assert by_type[obligations.INTEGRATION_CONFLICT]["settled_at"] is None
