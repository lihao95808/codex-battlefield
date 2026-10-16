"""动作的**风险分级**与**预授权**（P4-4）。

## 为什么必须有这一层

现在的模型是"每个动作都要人点一下"。这在两个方向上都不好：

* **太慢**：低风险动作（重试、续跑）在长跑里最需要自动化，却和最危险的动作
  走同一条审批路；
* **太松**：一旦有人图省事加个"全部自动批准"，`cancel_all` 也会跟着自动。

P4-4 要的是**把这两件事分开**：低风险可以预授权，高风险**永远**要人点。

## 一条不可协商的规矩

> **高风险动作永不可预授权。** 这不是配置项，是常量。

写在这里而不是写在文档里：文档会被读成建议，常量会当场拒绝执行。
`can_pre_authorize()` 里对 `high` 直接返回 `False`，**不看授权范围写了什么**。

## 失效方向

预授权是权限模型的实质性放宽，所以：

* **默认关**：没有授权记录就是"每次都要人点"（`DEFAULT_GRANT` 为空）；
* **范围与期限都是必填**：没有"无限期全部动作"这种授权形态 ——
  一个没有边界的授权等于把整个审批层删掉；
* **越界一律拒绝**（动作不在允许集里 / 目标不匹配 / 过期），
  并在拒绝理由里说清**哪一条**不满足，而不是笼统一句"未授权"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# ---- 风险等级 ----

LOW = "low"
MEDIUM = "medium"
HIGH = "high"

RISK_LEVELS: tuple[str, ...] = (LOW, MEDIUM, HIGH)

# 每个风险等级的中文说明。界面要显示它 —— "高风险"三个字本身不解释为什么。
RISK_LABEL: dict[str, str] = {
    LOW: "低（可预授权）",
    MEDIUM: "中（可预授权，但会留痕）",
    HIGH: "高（**永远**要人确认）",
}

RISK_RATIONALE: dict[str, str] = {
    LOW: "可逆或只是让流程往前走：做错了还能改回来。",
    MEDIUM: "改变状态但有明确目标：做错了要人工收拾，但不会扩及其他节点。",
    HIGH: "不可逆或影响全局面：取消、全停、放宽额度这类，做错了没有回头路。",
}

# 动作 → 风险等级。**封闭映射**：目录里有而这里没有的动作会被
# `unclassified()` 报出来，而不是默认成低风险（默认值必须偏保守）。
RISK_BY_ACTION: dict[str, str] = {
    # 低：可逆 / 只是往前推
    "kick_root": LOW,
    "retry_node": LOW,
    "resume_node": LOW,
    "start_task": LOW,
    # 中：改状态、目标明确
    "pause_node": MEDIUM,
    "hibernate_node": MEDIUM,
    # 高：不可逆或影响全局
    "cancel_node": HIGH,
    "cancel_all": HIGH,
    "set_budget_guard": HIGH,
}


class RiskError(ValueError):
    """风险分级或授权不合法。调用方当 4xx 报出去。"""


def risk_of(action: str) -> str:
    """某个动作的风险等级。

    **未登记的动作按 `high` 处理**，不是按 `low`：漏登记时失效方向必须偏保守 ——
    一个未分级的新动作如果被当成低风险，它会静默获得预授权资格。
    """
    return RISK_BY_ACTION.get(action, HIGH)


def unclassified(known: list[str]) -> list[str]:
    """在能力目录里、却没有风险分级的动作。

    这条是给门禁用的：新增动作时忘了分级，它会红 —— 而不是静默按高风险挡下来
    （那看起来像"这个动作坏了"）或按低风险放行（那更糟）。
    """
    return sorted(name for name in known if name not in RISK_BY_ACTION)


def catalog(known: list[str] | None = None) -> list[dict[str, Any]]:
    """动作风险目录，按风险从高到低排（危险的在前面，先被看见）。"""
    names = known if known is not None else sorted(RISK_BY_ACTION)
    order = {HIGH: 0, MEDIUM: 1, LOW: 2}
    items = [
        {
            "action": name,
            "risk": risk_of(name),
            "label": RISK_LABEL[risk_of(name)],
            "rationale": RISK_RATIONALE[risk_of(name)],
            "pre_authorizable": can_pre_authorize(name),
        }
        for name in names
    ]
    return sorted(items, key=lambda item: (order[item["risk"]], item["action"]))


def can_pre_authorize(action: str) -> bool:
    """这个动作**是否允许被预授权**。

    `high` 无条件 `False` —— 不看配置、不看授权范围写了什么。
    这条是 P4-4 的核心取舍，所以它在代码里是一个常量判断，不是一个开关。
    """
    return risk_of(action) != HIGH


# ---- 授权记录 ----


@dataclass(frozen=True)
class Grant:
    """一次预授权：**动作集 + 目标范围 + 期限**，三样都必填。

    没有"无限期全部动作"这种形态：没有边界的授权等于把审批层删掉，
    而它看起来只是一个便利设置。
    """

    actions: tuple[str, ...]
    # 允许的目标。空元组 = 不限目标（但仍受动作集与期限约束）。
    targets: tuple[str, ...] = ()
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    granted_by: str = ""
    reason: str = ""

    def remaining_seconds(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(timezone.utc)
        return int((self.expires_at - moment).total_seconds())


def default_grant() -> None:
    """出厂状态：**没有授权**。每个动作都要人点。

    刻意返回 `None` 而不是一个"空 Grant"：空对象会被 `if grant:` 判成真，
    于是"有没有授权"这件事在代码里就有了两种表示。
    """
    return None


class PreAuthorization:
    """当前生效的预授权。**至多一条**。

    为什么只有一条：多条授权叠加之后，"这个动作为什么被放行"要翻好几条记录
    才能回答；而审计要的正是那句话。一条 = 一个可读的答案。
    """

    def __init__(self, grant: Grant | None = None) -> None:
        self._grant = grant

    @property
    def grant(self) -> Grant | None:
        return self._grant

    def set(self, grant: Grant) -> None:
        self._grant = grant

    def clear(self) -> None:
        self._grant = None

    def authorize(
        self, action: str, target: str, *, now: datetime | None = None
    ) -> tuple[bool, str, str]:
        """能不能靠预授权执行。

        返回 `(allowed, basis, reason)`：

        * `basis` ∈ {`pre_authorized`, `human_confirmation_required`}。
          它要进审计记录 —— 事后要能回答"这次是谁批的"，而不只是"发生了取消"。
        * 不允许时 `reason` **必须说清哪一条不满足**（风险等级 / 动作集 / 目标 / 期限），
          不是笼统一句"未授权"。
        """
        moment = now or datetime.now(timezone.utc)

        if not can_pre_authorize(action):
            return (
                False,
                "human_confirmation_required",
                f"{action} 是高风险动作（{RISK_LABEL[HIGH]}），预授权永远不适用。",
            )

        grant = self._grant
        if grant is None:
            return False, "human_confirmation_required", "当前没有预授权，需要人确认。"

        if grant.expires_at <= moment:
            return (
                False,
                "human_confirmation_required",
                f"预授权已于 {grant.expires_at.isoformat()} 过期，需要人确认。",
            )

        if action not in grant.actions:
            allowed = "、".join(grant.actions) or "（空）"
            return (
                False,
                "human_confirmation_required",
                f"预授权的动作集是 {allowed}，不含 {action}。",
            )

        if grant.targets and target not in grant.targets:
            allowed = "、".join(grant.targets)
            return (
                False,
                "human_confirmation_required",
                f"预授权只覆盖目标 {allowed}，不含 {target!r}。",
            )

        remaining = grant.remaining_seconds(moment)
        return (
            True,
            "pre_authorized",
            f"命中预授权（{grant.granted_by or '未署名'} 授予，还剩 {max(0, remaining)}s）。",
        )


def make_grant(
    *,
    actions: list[str],
    targets: list[str] | None = None,
    minutes: int,
    granted_by: str,
    reason: str,
) -> Grant:
    """造一条预授权。**期限必填且必须为正**。

    为什么期限不能省：一个"永久预授权"在界面上和"期限 24 小时"长得几乎一样，
    但前者意味着从此不需要人 —— 那种差别不该靠用户读小字发现。
    """
    if not actions:
        raise RiskError("预授权至少要指定一个动作。")
    unknown = [name for name in actions if name not in RISK_BY_ACTION]
    if unknown:
        raise RiskError(f"未知动作：{'、'.join(unknown)}")
    blocked = [name for name in actions if not can_pre_authorize(name)]
    if blocked:
        raise RiskError(
            f"这些动作是高风险的，不允许被预授权：{'、'.join(blocked)}。"
            "请去掉它们 —— 高风险动作永远需要人确认。"
        )
    if minutes <= 0:
        raise RiskError("预授权必须有一个正的期限（分钟）。没有无限期的预授权。")
    if not granted_by.strip():
        raise RiskError("预授权必须署名（谁批的），否则事后无法归因。")
    now = datetime.now(timezone.utc)
    return Grant(
        actions=tuple(sorted(set(actions))),
        targets=tuple(sorted(set(targets or []))),
        expires_at=now + timedelta(minutes=minutes),
        granted_by=granted_by.strip(),
        reason=reason.strip(),
    )
