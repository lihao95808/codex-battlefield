"""本轮启动预算（budget guard / agent burst）的**状态推导**部分。

搬移自 `orchestrator.Orchestrator`，判据是 `scripts/audit_orchestrator_movable_mass.py`
的可搬质量普查（保守判据：一个方法可搬 ⟺ 它传递依赖到的所有同类方法都可搬），
而不是按方法组的表面行数估算。

- **可搬**：预算守卫文件的读写与状态推导、agent burst 上限推导、自动并发上下界、
  预算告警文案。它们只依赖 `workspace_dir` 与 `settings` 上的标量配置——改成**显式参数**后
  依赖比原先的 `self.settings` 更清楚，也可以脱离 `Orchestrator` 直接单测。
- **不可搬**：`set_budget_guard`(48 行)。它要调 `hibernate_tree`（整树休眠）、开 `SessionLocal`
  写事件、校验根节点存在 —— 那是 orchestrator 的消费者，不是预算推导。故留在原处。
  因此本模块只包含 `clear_budget_guard` 这一半，这是**测量结果**而非疏漏。
- `_coerce_utc` 有 16 处调用点且绝大多数在预算簇以外，因此它进 `utils.coerce_utc`，
  由本模块与 orchestrator 共同使用，而不是塞进这里。

依赖方向：本模块只依赖 `models.utcnow` 与 `utils`，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import utcnow
from .utils import coerce_utc, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


def budget_guard_path(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / "budget_guard.json"


def read_budget_guard(workspace_dir: str | Path) -> dict[str, Any]:
    path = budget_guard_path(workspace_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_budget_guard(workspace_dir: str | Path, payload: dict[str, Any]) -> None:
    path = budget_guard_path(workspace_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def active_budget_guard(workspace_dir: str | Path, root_id: str | None = None) -> dict[str, Any] | None:
    payload = read_budget_guard(workspace_dir)
    if payload.get("status") != "active":
        return None
    guard_root_id = str(payload.get("root_id") or "")
    if root_id and guard_root_id and guard_root_id != root_id:
        return None
    try:
        resume_at = datetime.fromisoformat(str(payload.get("resume_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    resume_at = coerce_utc(resume_at) or utcnow()
    if resume_at <= utcnow():
        return None
    return {
        **payload,
        "root_id": guard_root_id or None,
        "resume_at": resume_at,
        "reason": repair_mojibake_text(str(payload.get("reason") or "预算守卫生效，等待账户额度刷新。")),
    }


def budget_guard_state(workspace_dir: str | Path, root_id: str | None = None) -> dict[str, Any]:
    guard = active_budget_guard(workspace_dir, root_id)
    payload = read_budget_guard(workspace_dir)
    if guard:
        return {
            "status": "budget_guard_active",
            "root_id": guard.get("root_id") or root_id,
            "active": True,
            "resume_at": guard["resume_at"],
            "reason": str(guard.get("reason") or ""),
            "hibernated_count": 0,
        }
    return {
        "status": str(payload.get("status") or "inactive"),
        "root_id": payload.get("root_id") or root_id,
        "active": False,
        "resume_at": None,
        "reason": repair_mojibake_text(str(payload.get("reason") or "")),
        "hibernated_count": 0,
    }


def clear_budget_guard(workspace_dir: str | Path, root_id: str | None = None, *, frozen_mode: bool = False) -> dict[str, Any]:
    if frozen_mode:
        raise RuntimeError("Codex Battlefield is running in frozen read-only mode")
    payload = read_budget_guard(workspace_dir)
    guard_root_id = str(payload.get("root_id") or "")
    if root_id and guard_root_id and guard_root_id != root_id:
        active = bool(active_budget_guard(workspace_dir, root_id))
        return {
            "status": "unchanged",
            "root_id": guard_root_id or None,
            "active": active,
            "resume_at": active_budget_guard(workspace_dir, root_id).get("resume_at") if active else None,
            "reason": repair_mojibake_text(str(payload.get("reason") or "")),
            "hibernated_count": 0,
        }
    cleared = {
        **payload,
        "status": "cleared",
        "cleared_at": utcnow().isoformat(),
        "cleared_for_root_id": root_id,
    }
    write_budget_guard(workspace_dir, cleared)
    return {
        "status": "budget_guard_cleared",
        "root_id": guard_root_id or root_id,
        "active": False,
        "resume_at": None,
        "reason": repair_mojibake_text(str(payload.get("reason") or "")),
        "hibernated_count": 0,
    }


def agent_burst_limit(settings: Settings) -> int:
    if settings.goal_mode:
        return 0
    return max(0, settings.agent_burst_limit)


def agent_burst_effective_reserve(settings: Settings) -> int:
    limit = agent_burst_limit(settings)
    if limit <= 0:
        return 0
    reserve = max(0, settings.agent_burst_reserve)
    return min(reserve, max(limit - 1, 0))


def agent_burst_work_limit(settings: Settings) -> int:
    limit = agent_burst_limit(settings)
    if limit <= 0:
        return 0
    return max(1, limit - agent_burst_effective_reserve(settings))


def auto_concurrency_bounds(settings: Settings) -> tuple[int, int]:
    fixed = max(1, int(settings.max_concurrency))
    if not settings.auto_concurrency_enabled:
        return fixed, fixed
    upper = max(fixed, int(settings.auto_concurrency_max), 1)
    lower = max(1, min(int(settings.auto_concurrency_min), upper))
    return lower, upper


def budget_warning_state(settings: Settings, used: int, limit: int, work_limit: int, hibernating: int) -> tuple[str, str]:
    if settings.goal_mode:
        return (
            "goal",
            "Goal 长跑模式已开启：不按本轮启动预算暂停；仍会在 Codex 资源/流量阻塞时保存状态并休眠。",
        )
    if limit <= 0 or work_limit <= 0:
        return ("none", "未启用本轮启动预算上限；系统只依赖资源阻塞、空闲看门狗和人工操作暂停。")
    remaining = max(0, work_limit - used)
    if hibernating > 0 or remaining <= 0:
        return (
            "paused",
            f"本轮 Codex agent 工作启动额度已用完 {used}/{work_limit}；任务树会保存状态并暂停，等待刷新时间、充值后手动恢复或 watchdog 自动续跑。",
        )
    if remaining <= 1:
        return (
            "warning",
            f"本轮 Codex agent 工作启动额度仅剩 {remaining} 次；下一批子任务可能会进入自动休眠，建议保留给恢复钩子或切换 Goal 长跑模式。",
        )
    return ("none", f"本轮 Codex agent 工作启动额度剩余 {remaining} 次。")
