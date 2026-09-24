from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import budget
from app.orchestrator import Orchestrator
from app.utils import coerce_utc


def _guard(workspace: Path, **overrides: object) -> dict[str, object]:
    payload = {
        "status": "active",
        "root_id": "root-1",
        "resume_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "reason": "余额不足",
    }
    payload.update(overrides)
    budget.write_budget_guard(workspace, payload)
    return payload


def test_budget_guard_path(tmp_path: Path) -> None:
    assert budget.budget_guard_path(tmp_path) == tmp_path / "budget_guard.json"


def test_read_budget_guard_tolerates_missing_and_broken(tmp_path: Path) -> None:
    assert budget.read_budget_guard(tmp_path) == {}

    (tmp_path / "budget_guard.json").write_text("{not json", encoding="utf-8")
    assert budget.read_budget_guard(tmp_path) == {}

    (tmp_path / "budget_guard.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    assert budget.read_budget_guard(tmp_path) == {}


def test_write_budget_guard_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    budget.write_budget_guard(nested, {"status": "cleared"})
    assert (nested / "budget_guard.json").is_file()


def test_active_budget_guard_requires_active_future_and_matching_root(tmp_path: Path) -> None:
    _guard(tmp_path)
    assert budget.active_budget_guard(tmp_path) is not None
    assert budget.active_budget_guard(tmp_path, "root-1") is not None
    assert budget.active_budget_guard(tmp_path, "root-2") is None

    _guard(tmp_path, status="cleared")
    assert budget.active_budget_guard(tmp_path) is None

    _guard(tmp_path, resume_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    assert budget.active_budget_guard(tmp_path) is None

    _guard(tmp_path, resume_at="not-a-time")
    assert budget.active_budget_guard(tmp_path) is None


def test_active_budget_guard_normalizes_naive_resume_at(tmp_path: Path) -> None:
    # 无时区的时间串按 UTC 解释，否则在 UTC+8 上会把"未来"读成"过去"。
    naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    _guard(tmp_path, resume_at=naive)
    guarded = budget.active_budget_guard(tmp_path)
    assert guarded is not None
    assert guarded["resume_at"].tzinfo is timezone.utc


def test_active_budget_guard_defaults_reason(tmp_path: Path) -> None:
    _guard(tmp_path, reason="")
    guarded = budget.active_budget_guard(tmp_path)
    assert guarded is not None
    assert guarded["reason"] == "预算守卫生效，等待账户额度刷新。"


def test_budget_guard_state_reports_active_and_inactive(tmp_path: Path) -> None:
    state = budget.budget_guard_state(tmp_path)
    assert state["status"] == "inactive"
    assert state["active"] is False
    assert state["resume_at"] is None

    _guard(tmp_path)
    state = budget.budget_guard_state(tmp_path)
    assert state["status"] == "budget_guard_active"
    assert state["active"] is True
    assert state["root_id"] == "root-1"
    assert state["hibernated_count"] == 0


def test_clear_budget_guard_marks_cleared(tmp_path: Path) -> None:
    _guard(tmp_path)
    result = budget.clear_budget_guard(tmp_path, "root-1")
    assert result["status"] == "budget_guard_cleared"
    assert result["active"] is False
    assert result["reason"] == "余额不足"
    assert budget.read_budget_guard(tmp_path)["status"] == "cleared"


def test_clear_budget_guard_other_root_is_unchanged(tmp_path: Path) -> None:
    _guard(tmp_path)
    result = budget.clear_budget_guard(tmp_path, "root-2")
    assert result["status"] == "unchanged"
    assert result["root_id"] == "root-1"
    # 既有行为：active 是按"请求方 root_id"过滤后的答案，因此 root-2 视角下为 False，
    # 但它不影响守卫本身仍然 active。这里锁定现状而非改写语义。
    assert result["active"] is False
    assert result["resume_at"] is None
    assert budget.read_budget_guard(tmp_path)["status"] == "active"


def test_clear_budget_guard_respects_frozen_mode(tmp_path: Path) -> None:
    _guard(tmp_path)
    try:
        budget.clear_budget_guard(tmp_path, "root-1", frozen_mode=True)
    except RuntimeError as exc:
        assert "frozen read-only mode" in str(exc)
    else:  # pragma: no cover - 只在行为回归时触发
        raise AssertionError("frozen_mode 下清除预算守卫必须拒绝")
    assert budget.read_budget_guard(tmp_path)["status"] == "active"


def test_coerce_utc_marks_naive_as_utc() -> None:
    assert coerce_utc(None) is None
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert coerce_utc(naive) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert coerce_utc(aware) == datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone.utc)


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.goal_mode = False
        self.agent_burst_limit = 0
        self.agent_burst_reserve = 0
        self.max_concurrency = 4
        self.auto_concurrency_enabled = False
        self.auto_concurrency_min = 2
        self.auto_concurrency_max = 8
        for key, value in overrides.items():
            setattr(self, key, value)


def test_agent_burst_limit_and_derivatives() -> None:
    assert budget.agent_burst_limit(_Settings(agent_burst_limit=12)) == 12
    assert budget.agent_burst_limit(_Settings(agent_burst_limit=-3)) == 0
    # Goal 长跑模式不按启动预算暂停
    assert budget.agent_burst_limit(_Settings(agent_burst_limit=12, goal_mode=True)) == 0

    assert budget.agent_burst_effective_reserve(_Settings(agent_burst_limit=0)) == 0
    # 预留不得超过 limit - 1，否则工作额度会归零
    assert budget.agent_burst_effective_reserve(_Settings(agent_burst_limit=3, agent_burst_reserve=10)) == 2
    assert budget.agent_burst_effective_reserve(_Settings(agent_burst_limit=5, agent_burst_reserve=2)) == 2

    assert budget.agent_burst_work_limit(_Settings(agent_burst_limit=0)) == 0
    assert budget.agent_burst_work_limit(_Settings(agent_burst_limit=5, agent_burst_reserve=2)) == 3
    assert budget.agent_burst_work_limit(_Settings(agent_burst_limit=1, agent_burst_reserve=9)) == 1


def test_auto_concurrency_bounds() -> None:
    assert budget.auto_concurrency_bounds(_Settings()) == (4, 4)
    assert budget.auto_concurrency_bounds(_Settings(auto_concurrency_enabled=True)) == (2, 8)
    # 下界不得超过上界，上界不得低于固定并发
    assert budget.auto_concurrency_bounds(
        _Settings(auto_concurrency_enabled=True, max_concurrency=9, auto_concurrency_max=3, auto_concurrency_min=7)
    ) == (7, 9)


def test_budget_warning_state_levels() -> None:
    assert budget.budget_warning_state(_Settings(goal_mode=True), 0, 0, 0, 0)[0] == "goal"
    assert budget.budget_warning_state(_Settings(), 0, 0, 0, 0)[0] == "none"
    assert budget.budget_warning_state(_Settings(), 5, 6, 6, 0)[0] == "warning"
    assert budget.budget_warning_state(_Settings(), 6, 6, 6, 0)[0] == "paused"
    # hibernating > 0 时即便还有额度也算暂停
    assert budget.budget_warning_state(_Settings(), 1, 6, 6, 2)[0] == "paused"

    level, message = budget.budget_warning_state(_Settings(), 2, 6, 6, 0)
    assert level == "none"
    assert "剩余 4 次" in message


def test_orchestrator_delegates_agree_with_module(tmp_path: Path) -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings(
        agent_burst_limit=6,
        agent_burst_reserve=1,
        auto_concurrency_enabled=True,
        workspace_dir=tmp_path,
        frozen_mode=False,
    )

    assert orchestrator._budget_guard_path() == budget.budget_guard_path(tmp_path)
    assert orchestrator._agent_burst_limit() == budget.agent_burst_limit(orchestrator.settings)
    assert orchestrator._agent_burst_effective_reserve() == budget.agent_burst_effective_reserve(orchestrator.settings)
    assert orchestrator._agent_burst_work_limit() == budget.agent_burst_work_limit(orchestrator.settings)
    assert orchestrator._auto_concurrency_bounds() == budget.auto_concurrency_bounds(orchestrator.settings)
    assert orchestrator._budget_warning_state(1, 6, 5, 0) == budget.budget_warning_state(
        orchestrator.settings, 1, 6, 5, 0
    )

    _guard(tmp_path)
    assert orchestrator._read_budget_guard() == budget.read_budget_guard(tmp_path)
    assert orchestrator._active_budget_guard("root-1") == budget.active_budget_guard(tmp_path, "root-1")
    assert orchestrator.budget_guard_state("root-1") == budget.budget_guard_state(tmp_path, "root-1")
    assert orchestrator.clear_budget_guard("root-1") == budget.clear_budget_guard(tmp_path, "root-1")


def test_orchestrator_coerce_utc_delegates() -> None:
    orchestrator = Orchestrator.__new__(Orchestrator)
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert orchestrator._coerce_utc(naive) == coerce_utc(naive)
    assert orchestrator._coerce_utc(None) is None
