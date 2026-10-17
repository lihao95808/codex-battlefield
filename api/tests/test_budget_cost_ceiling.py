"""树级**金额**闸（P3-1 的第三道闸，2026-10-18）。

## 为什么它和 token 闸不能是同一个函数

token 数每一侧都能自己数；**金额只有端点自报**，而且币种可能不同。所以这道闸有两个失败方向，
而它们**必须不同对待**：

* **读不到金额**（端点没报、币种没配、记过的是别的币种）→ **不能暂停**（拿猜测的币种停树 = 误报），
  但也**不能静默**：记一条事件说明"为什么它没生效"；
* **读到了且确实超了** → 暂停（复用 `set_budget_guard`），事件里写清 used / ceiling / currency，
  多币种的其他部分单列（跨币种相加没有意义）。

C1–C6 各钉一条：

| 用例 | 盯什么 |
|---|---|
| C1 | 上限 0 = 不拦（默认关） |
| C2 | 币种匹配 + 已超 → 拦、休眠、事件带 used/ceiling/currency |
| C3 | 币种匹配 + 未超 → 不拦 |
| C4 | **完全没有金额** → 不拦，但记 `budget.cost_unreadable`（且只记一次） |
| C5 | 记过的是别的币种 → 不拦 + `budget.cost_currency_absent` |
| C6 | 多币种 → 只比配置的币种，其余进 `other_currencies` |
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import EventRecord, RunRecord, RunStatus, TaskNode, TaskStatus
from app.orchestrator import Orchestrator


def _settings(tmp_path: Path, **overrides):
    return get_settings().__class__(
        workspace_dir=tmp_path,
        database_url=f"sqlite:///{(tmp_path / 'budget-cost.sqlite3').as_posix()}",
        runner_mode="mock",
        mock_delay_seconds=0,
        max_concurrency=1,
        **overrides,
    )


def _seed(tmp_path: Path, entries: list[dict]) -> tuple[str, str]:
    """种根 + 子节点，并按 `entries` 各写一条运行记录。"""

    init_db()
    suffix = tmp_path.name
    root_id = f"costroot-{suffix}"
    child_id = f"costchild-{suffix}"
    with SessionLocal() as db:
        if db.get(TaskNode, root_id) is None:
            db.add_all(
                [
                    TaskNode(
                        id=root_id,
                        title="金额闸验证",
                        description="根",
                        depth=0,
                        status=TaskStatus.WAITING_CHILDREN.value,
                    ),
                    TaskNode(
                        id=child_id,
                        parent_id=root_id,
                        title="子节点",
                        description="子",
                        depth=1,
                        status=TaskStatus.QUEUED.value,
                    ),
                ]
            )
            db.flush()
        for entry in entries:
            db.add(
                RunRecord(
                    task_id=child_id,
                    kind="worker",
                    status=RunStatus.SUCCEEDED.value,
                    exit_code=0,
                    usage_json=json.dumps(entry),
                )
            )
        db.commit()
    return root_id, child_id


@pytest.fixture(autouse=True)
def scheduled_resumes(monkeypatch):
    """休眠会安排一个 +24h 的唤醒任务；测试里替换成记账，避免泄漏任务。"""

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        Orchestrator,
        "_schedule_auto_resume",
        lambda self, root_id, resume_at: calls.append((root_id, resume_at)),
    )
    return calls


def _events(root_id: str, kind: str) -> list[EventRecord]:
    with SessionLocal() as db:
        return (
            db.query(EventRecord)
            .filter(EventRecord.type == kind, EventRecord.task_id == root_id)
            .all()
        )


def _hold(orchestrator: Orchestrator, child_id: str) -> bool:
    return asyncio.run(orchestrator._hold_agent_start_for_cost_budget(child_id, "worker"))  # noqa: SLF001


def test_c1_zero_ceiling_never_holds(tmp_path: Path) -> None:
    root_id, child_id = _seed(
        tmp_path, [{"input_tokens": 1, "cost": {"amount": 99.0, "currency": "CNY"}}]
    )
    orchestrator = Orchestrator(_settings(tmp_path))

    assert _hold(orchestrator, child_id) is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"


def test_c2_over_ceiling_holds_with_numbers(tmp_path: Path) -> None:
    root_id, child_id = _seed(
        tmp_path,
        [
            {"input_tokens": 1, "cost": {"amount": 3.5, "currency": "CNY"}},
            {"input_tokens": 1, "cost": {"amount": 2.0, "currency": "CNY"}},
        ],
    )
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=5.0, budget_cost_currency="CNY"))

    assert _hold(orchestrator, child_id) is True

    state = orchestrator.budget_guard_state(root_id)
    assert state["status"] == "budget_guard_active"
    assert "5.50" in state["reason"] and "CNY" in state["reason"], state["reason"]
    events = _events(root_id, "budget.cost_ceiling")
    assert len(events) == 1
    payload = json.loads(events[0].payload_json or "{}")
    assert payload["used"] == 5.5
    assert payload["ceiling"] == 5.0
    assert payload["currency"] == "CNY"


def test_c3_under_ceiling_does_not_hold(tmp_path: Path) -> None:
    root_id, child_id = _seed(
        tmp_path, [{"input_tokens": 1, "cost": {"amount": 1.0, "currency": "CNY"}}]
    )
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=5.0, budget_cost_currency="CNY"))

    assert _hold(orchestrator, child_id) is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"


def test_c4_no_amounts_at_all_does_not_hold_but_says_why(tmp_path: Path) -> None:
    """端点没报金额：**不暂停**，但要留下"为什么闸没生效"的痕（且只留一次）。"""

    root_id, child_id = _seed(tmp_path, [{"input_tokens": 10, "output_tokens": 5}])
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=0.01, budget_cost_currency="CNY"))

    assert _hold(orchestrator, child_id) is False
    assert _hold(orchestrator, child_id) is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"
    events = _events(root_id, "budget.cost_unreadable")
    assert len(events) == 1, f"同一种原因只该记一次（实际 {len(events)} 条）"


def test_c5_other_currency_does_not_hold_but_says_why(tmp_path: Path) -> None:
    root_id, child_id = _seed(
        tmp_path, [{"input_tokens": 1, "cost": {"amount": 99.0, "currency": "USD"}}]
    )
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=5.0, budget_cost_currency="CNY"))

    assert _hold(orchestrator, child_id) is False
    assert orchestrator.budget_guard_state(root_id)["status"] != "budget_guard_active"
    events = _events(root_id, "budget.cost_currency_absent")
    assert len(events) == 1
    assert "USD" in events[0].message


def test_c6_multiple_currencies_only_compare_the_configured_one(tmp_path: Path) -> None:
    root_id, child_id = _seed(
        tmp_path,
        [
            {"input_tokens": 1, "cost": {"amount": 6.0, "currency": "CNY"}},
            {"input_tokens": 1, "cost": {"amount": 100.0, "currency": "USD"}},
        ],
    )
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=5.0, budget_cost_currency="CNY"))

    assert _hold(orchestrator, child_id) is True
    payload = json.loads(_events(root_id, "budget.cost_ceiling")[0].payload_json or "{}")
    assert payload["used"] == 6.0
    assert payload["other_currencies"] == {"USD": 100.0}, payload

@pytest.mark.asyncio
async def test_c7_the_hook_is_wired_into_agent_reservation(tmp_path: Path) -> None:
    """接线判据：起 agent 的入口必须拦住超金额上限的节点（删掉钩子这里就红）。"""

    _root_id, child_id = _seed(
        tmp_path, [{"input_tokens": 1, "cost": {"amount": 9.0, "currency": "CNY"}}]
    )
    orchestrator = Orchestrator(_settings(tmp_path, budget_cost_ceiling=1.0, budget_cost_currency="CNY"))

    allowed = await orchestrator._reserve_agent_start(child_id, "worker")  # noqa: SLF001

    assert allowed is False, "起 agent 的入口没有拦住超金额预算的节点 —— 钩子没接上"
