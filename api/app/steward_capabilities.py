"""管家的**能力目录** —— 它能调用什么，以及每个能力是否改状态。

## 为什么要有这份目录（而不是让 agent 自己猜）

管家是 LLM。它必须**知道**工具的存在、参数形状、以及**哪个会改状态**才能可靠工作。
不告诉它就等于让它猜，而猜测的代价是乱调接口。

## 为什么声明与权限在同一个地方

每条能力在这里声明 `mutating`。渲染给 agent 的说明、以及"只读模式"的过滤，
都由**同一份声明**推导出来。分成两处写迟早会漂移 —— 而漂移的后果是
"文档说只读，实际能改"，这正是权限设计里最不该出的错。

## 第一版只读

目录里可以把改状态的能力列出来（让 agent 知道**存在**这些能力、可以建议用户去做），
但 `read_only=True` 时它们会被标成 `available: false`，服务端也会拒绝执行。
**说清"有能力但被禁用"比假装不存在更有用**：agent 才能告诉用户"要做这个得先放开 X"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    method: str
    path: str
    summary: str
    mutating: bool
    params: dict[str, str] = field(default_factory=dict)
    # 改状态的能力为什么需要它 —— 给 agent 向用户解释用
    rationale: str = ""
    # 这个动作的 `target` 该填什么；**非空表示必填**。
    #
    # 为什么必须显式声明，不能用 `params` 里有没有 `task_id` 来推断
    # （2026-10-15 实测踩到）：`start_task` 的目标描述走的是 `target` 槽位，
    # 它的 params 里写的是 `goal` 而不是 `task_id`，于是"推断"认为它不需要目标，
    # 放行了一条 `target=""` 的提议 —— 而那个动作**必然**执行失败
    # （`_exec_start_task` 会拒绝空 goal）。
    # 结果就是界面上摆了一个"点了必失败"的按钮，正是提议校验要拦的东西。
    # 声明出来，规则就只剩一条：**声明了 target 就必须给**。
    target_label: str = ""

    def as_dict(self, *, read_only: bool) -> dict[str, Any]:
        available = (not self.mutating) or (not read_only)
        return {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "mutating": self.mutating,
            "available": available,
            "params": dict(self.params),
            "target_label": self.target_label,
            "when_unavailable": (
                ""
                if available
                else "当前是只读模式，这个能力被禁用；要执行需要用户先放开写权限。"
            ),
        }


# 顺序就是建议 agent 的使用顺序：先看，再判断，最后才动。
CAPABILITIES: tuple[Capability, ...] = (
    # ---------- 观察 ----------
    Capability(
        name="status",
        method="GET",
        path="/api/steward/status",
        summary="当前工具状态快照：工程/状态根/库、树的计数、被阻塞节点及原因、用量、最近事件、树的紧凑轮廓。",
        mutating=False,
    ),
    Capability(
        name="tree",
        method="GET",
        path="/api/tree",
        summary="完整任务树（比 status 里的轮廓重得多，节点多时慎用）。",
        mutating=False,
        params={"root_id": "只看某棵子树（可选）", "compact_done": "是否折叠已完成节点"},
    ),
    Capability(
        name="node",
        method="GET",
        path="/api/tasks/{task_id}",
        summary="单个节点的详情：状态、进度、验收判据、结果、错误、所属 worktree。",
        mutating=False,
        params={"task_id": "节点 id（必填）"},
    ),
    Capability(
        name="run_artifact",
        method="GET",
        path="/api/runs/{run_id}/artifact",
        summary="看某次运行的原始产物：提示词、事件流、最后消息。排查'为什么做错'时最有用。",
        mutating=False,
        params={"run_id": "运行记录 id", "kind": "prompt | logs | last_message"},
    ),
    Capability(
        name="obligation_index",
        method="GET",
        path="/api/obligation-index",
        summary="跨树债务索引：哪些验收判据还没结清。注意它是**派生缓存**，可能过期。",
        mutating=False,
        params={"status": "open | settled | all"},
    ),
    Capability(
        name="watchdog_state",
        method="GET",
        path="/api/watchdog/state",
        summary="看护状态：是否该续跑、资源压力、额度、看门狗与计划任务是否就位。",
        mutating=False,
    ),
    Capability(
        name="budget_guard",
        method="GET",
        path="/api/budget-guard",
        summary="预算闸门状态：是否因额度/资源在休眠，以及何时允许恢复。",
        mutating=False,
    ),
    # ---------- 控制（会改状态）----------
    Capability(
        name="kick_root",
        method="POST",
        path="/api/tasks/{task_id}/kick",
        summary="推一下某个根节点，让它继续跑（从停滞中恢复）。",
        mutating=True,
        params={"task_id": "要推的节点 id"},
        rationale="会让树真的开始跑，消耗模型的 token。",
        target_label="要推的根节点 id",
    ),
    Capability(
        name="pause_node",
        method="POST",
        path="/api/tasks/{task_id}/pause",
        summary="暂停一个节点。",
        mutating=True,
        params={"task_id": "节点 id"},
        rationale="会改变树的运行状态。",
        target_label="要暂停的节点 id",
    ),
    Capability(
        name="resume_node",
        method="POST",
        path="/api/tasks/{task_id}/resume",
        summary="恢复一个被暂停的节点。",
        mutating=True,
        params={"task_id": "节点 id"},
        rationale="会让它继续消耗 token。",
        target_label="要恢复的节点 id",
    ),
    Capability(
        name="retry_node",
        method="POST",
        path="/api/tasks/{task_id}/retry",
        summary="重试一个失败/被阻塞的节点。",
        mutating=True,
        params={"task_id": "节点 id"},
        rationale="会重新花 token 跑一遍。",
        target_label="要重试的节点 id",
    ),
    Capability(
        name="cancel_node",
        method="POST",
        path="/api/tasks/{task_id}/cancel",
        summary="取消一个节点（不可逆）。",
        mutating=True,
        params={"task_id": "节点 id"},
        rationale="取消是不可逆的。",
        target_label="要取消的节点 id",
    ),
    Capability(
        name="hibernate_node",
        method="POST",
        path="/api/tasks/{task_id}/hibernate",
        summary="让节点休眠，到时间或条件满足再恢复。",
        mutating=True,
        params={"task_id": "节点 id", "resume_after_seconds": "多久后允许恢复"},
        rationale="会改变调度状态。",
        target_label="要休眠的节点 id",
    ),
    Capability(
        name="cancel_all",
        method="POST",
        path="/api/projects/cancel-all",
        summary="取消当前所有项目（不可逆，影响面最大）。",
        mutating=True,
        rationale="一次影响整棵树，属于高危操作。",
        # 整机操作，没有"哪个节点"可言 —— 但**仍要填一句理由**：
        # 界面上这是个不可逆的高危按钮，用户需要看到"为什么建议现在按"。
        target_label="一句话说明为什么要全部取消",
    ),
    Capability(
        name="set_budget_guard",
        method="POST",
        path="/api/budget-guard",
        summary="设置预算闸门：让整棵树休眠到某个时间再恢复（额度不足时用）。",
        mutating=True,
        rationale="会冻结整棵树的运行。",
        target_label="一句话说明为什么要开闸门",
    ),
    Capability(
        name="start_task",
        method="POST",
        path="/api/projects/one-shot",
        summary="下发给新的目标，起一棵新树。",
        mutating=True,
        params={"goal": "目标描述（必填）"},
        rationale="会起一棵新树，消耗大量 token。",
        target_label="目标描述（必填，越小越好）",
    ),
)


def catalog(*, read_only: bool) -> dict[str, Any]:
    """渲染成给 agent 看的目录。"""
    return {
        "read_only_mode": read_only,
        "note": (
            "你是**只读**的：可以调用 available=true 且 mutating=false 的能力。"
            "mutating=true 的能力当前被禁用 —— 不要尝试，也不要谎称做了；"
            "如果用户需要，就说明需要什么条件才能执行。"
            if read_only
            else "你可以调用下面所有 available=true 的能力。改状态前先向用户确认。"
        ),
        "capabilities": [cap.as_dict(read_only=read_only) for cap in CAPABILITIES],
    }


def mutating_names() -> frozenset[str]:
    return frozenset(cap.name for cap in CAPABILITIES if cap.mutating)
