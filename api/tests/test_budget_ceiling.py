"""树级累计 token 硬闸（P3-1 的第二道闸，2026-10-18）。

## 为什么补这一条

P3-1 原来只有两样东西：**时间窗守卫**（账户额度耗尽 → 整树休眠到刷新时刻）和 **agent burst**
（每窗口允许起几个 agent）。两者都**不看这棵树已经花了多少**。实测代价是具体的：
2026-10-18 的一轮真实递归探针自己拆到 **6 层**、吃掉 **24.1M input tokens**，
系统全程没有停 —— 同一句目标有时 4M、有时 24M。

这里验的是"**钱的上限**这道闸"，它与"时间窗"是两件事：

* 达到上限 → 整树休眠 + 写事件（`budget.token_ceiling`，带 used/ceiling）；
* 放行只能靠人（清守卫或预授权），**不是**自动把上限改大；
* 上限为 0 时**一次都不拦**（默认关闭，不替用户做决定）。

判据 B1–B5 各自对应上面一条，另外 B4 钉住"钩子真的接在起 agent 的入口上"
（B1–B3 直接调方法，删掉钩子它们照样绿 —— 那样就测不到接线）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import EventRecord, RunRecord, RunStatus, TaskNode, TaskStatus
from app.orchestrator import BUDGET_TOKEN_GUARD_RESUME_SECONDS, Orchestrator


def _settings(tmp_path: Path, **overrides):
    return get_settings().__class__(
        workspace_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'budget-ceiling.sqlite3').as_posix()}",
        runner_mode="mock",
        mock_delay_seconds=0,
        max_concurrency=1,
        **overrides,
    )


@pytest.fixture(autouse=True)
def scheduled_resumes(monkeypatch):
    """把 _schedule_auto_resume 换成记账版本。

    休眠会安排一个 +24 小时的唤醒任务；在测试里它只会变成 syncio.run 关闭时的那句
    “Task was destroyed but it is pending”。这里既不测唤醒、也不该泄漏任务，
    所以替换掉它，并顺手断言“确实安排了一次恢复”。
    """

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        Orchestrator,
        "_schedule_auto_resume",
        lambda self, root_id, resume_at: calls.append((root_id, resume_at)),
    )
    return calls


def _seed_tree(tmp_path: Path, *, usage_tokens: int) -> tuple[str, str]:
    """种一棵根 + 子节点，并写一条带用量的运行记录（`usage_json` 走真实形状）。"""

    init_db()
    # 每个用例一套独立 id：测试库是共享的，写死 id 会撞 UNIQUE（本文件第一版就是这样红的）。
    suffix = tmp_path.name
    with SessionLocal() as db:
        root = TaskNode(
            id=f"ceiling-root-{suffix}",
            title="树级上限验证",
            description="根",
            depth=0,
            status=TaskStatus.WAITING_CHILDREN.value,
        )
        child = TaskNode(
            id=f"ceiling-child-{suffix}",
            parent_id=root.id,
            title="子节点",
            description="子",
            depth=1,
            status=TaskStatus.QUEUED.value,
        )
        db.add_all([root, child])
        db.flush()
        db.add(
            RunRecord(
                task_id=child.id,
                kind="worker",
                status=RunStatus.SUCCEEDED.value,
                exit_code=0,
                usage_json=json.dumps({"input_tokens": usage_tokens, "output_tokens": 0}),
            )
        )
        db.commit()
    return root.id, child.id


def test_b1_zero_ceiling_never_holds(tmp_path: Path) -> None:
    """默认关闭：上限 0 时一次都不拦（不替用户做决定）。"""

    root_id, child_id = _seed_tree(tmp_path, usage_tokens=10_000_000)
    orchestrator = Orchestrator(_settings(tmp_path))

    held = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "worker"))  # noqa: SLF001

    assert held is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"


def test_b2_over_ceiling_holds_and_writes_the_event(tmp_path: Path, scheduled_resumes) -> None:
    """超上限 → 拦住 + 休眠 + 事件里带 used/ceiling（人要知道差多少）。"""

    root_id, child_id = _seed_tree(tmp_path, usage_tokens=1_000_000)
    orchestrator = Orchestrator(_settings(tmp_path, budget_token_ceiling=400_000))

    held = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "worker"))  # noqa: SLF001

    assert held is True
    state = orchestrator.budget_guard_state(root_id)
    assert state["status"] == "budget_guard_active"
    assert "1,000,000" in state["reason"] and "400,000" in state["reason"], state["reason"]

    with SessionLocal() as db:
        # 测试库是共享的：只数**这棵树**的事件（否则会数到别的用例写的）。
        events = (
            db.query(EventRecord)
            .filter(EventRecord.type == "budget.token_ceiling", EventRecord.task_id == root_id)
            .all()
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json or "{}")
        assert payload["used_tokens"] == 1_000_000
        assert payload["ceiling_tokens"] == 400_000
        assert payload["phase"] == "worker"
    assert len(scheduled_resumes) == 1, "休眠之后应当安排一次恢复（到点再确认）"


def test_b3_under_ceiling_does_not_hold(tmp_path: Path) -> None:
    root_id, child_id = _seed_tree(tmp_path, usage_tokens=100_000)
    orchestrator = Orchestrator(_settings(tmp_path, budget_token_ceiling=400_000))

    held = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "worker"))  # noqa: SLF001

    assert held is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"


def test_b3b_active_guard_does_not_duplicate_events(tmp_path: Path, scheduled_resumes) -> None:
    """已经在守卫里：继续拦住，但不叠第二条事件、不重复休眠。"""

    root_id, child_id = _seed_tree(tmp_path, usage_tokens=1_000_000)
    orchestrator = Orchestrator(_settings(tmp_path, budget_token_ceiling=400_000))

    first = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "worker"))  # noqa: SLF001
    second = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "verifier"))  # noqa: SLF001

    assert (first, second) == (True, True)
    with SessionLocal() as db:
        events = (
            db.query(EventRecord)
            .filter(EventRecord.type == "budget.token_ceiling", EventRecord.task_id == root_id)
            .all()
        )
    assert len(events) == 1, f"守卫期间不该重复记事件（实际 {len(events)} 条）"


@pytest.mark.asyncio
async def test_b4_the_hook_is_wired_into_agent_reservation(tmp_path: Path) -> None:
    """**接线判据**：起 agent 的入口必须在超上限时返回 False。

    B1–B3 直接调方法，删掉钩子它们照样绿；这一条才钉住"真的接上了"。
    """

    _root_id, child_id = _seed_tree(tmp_path, usage_tokens=1_000_000)
    orchestrator = Orchestrator(_settings(tmp_path, budget_token_ceiling=400_000))

    allowed = await orchestrator._reserve_agent_start(child_id, "planner")  # noqa: SLF001

    assert allowed is False, "起 agent 的入口没有拦住超预算的节点 —— 钩子没接上"


def test_b5_finished_tasks_are_not_held(tmp_path: Path) -> None:
    """已经终止/停止的节点不该再触发守卫（否则会留下一堆没法解释的休眠）。"""

    root_id, child_id = _seed_tree(tmp_path, usage_tokens=1_000_000)
    with SessionLocal() as db:
        child = db.get(TaskNode, child_id)
        assert child is not None
        child.status = TaskStatus.DONE.value
        db.commit()
    orchestrator = Orchestrator(_settings(tmp_path, budget_token_ceiling=400_000))

    held = asyncio.run(orchestrator._hold_agent_start_for_token_budget(child_id, "worker"))  # noqa: SLF001

    assert held is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"


def test_b6_resume_window_is_a_day() -> None:
    """休眠窗口是有意的常量：24 小时后再确认一次，而不是“永久”或“几分钟就自己回来”。"""

    assert BUDGET_TOKEN_GUARD_RESUME_SECONDS == 86_400
