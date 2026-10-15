"""管家的**动作执行层**：从"只能建议"到"你批准后能执行"。

## 为什么要有审批这一层

管家是 LLM，判断会有错的时候。让它直接动树的风险不是"它可能做错"，
而是**做错了没人拦、而且不可逆**（cancel 就是不可逆的）。

所以设计成三段：

    管家提议  ->  用户批准  ->  服务端执行

管家**永远**不能自己执行。就算哪天把它的权限放开，那也是"它能自己批准"，
而不是"绕过了审批"—— 这两件事在代码里是分开的。

## 两道校验，缺一不可

| 时机 | 查什么 | 为什么不能只查一次 |
|---|---|---|
| **提议时** | 动作名在白名单里？参数齐？ | 拦掉模型幻觉出来的动作名，别在界面上摆一个点了会失败的建议 |
| **执行时** | 再查一次 | 提议与批准之间白名单/权限可能变了；执行是最危险的时刻，它必须自己判 |

## 参数与风险的来源

`ActionSpec.params` 与 `rationale` **复用 `steward_capabilities` 的同一份声明**，
不另写一份：分成两处迟早漂移，而漂移的后果是"文档说不可用、实际能执行"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .steward_capabilities import CAPABILITIES

# 动作名 -> 能力声明。只认 mutating 的那些：只读能力不需要审批，
# 管家直接调就行（它本来就是只读的）。
_BY_NAME = {cap.name: cap for cap in CAPABILITIES if cap.mutating}


class ActionError(ValueError):
    """动作不合法或无法执行。调用方把它当 4xx 报给用户。"""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    summary: str
    rationale: str
    params: dict[str, str]
    target_label: str = ""


def known_actions() -> dict[str, ActionSpec]:
    """可以**提议**的动作全集（= 能力目录里 mutating 的那些）。"""
    return {
        name: ActionSpec(
            name=cap.name,
            summary=cap.summary,
            rationale=cap.rationale,
            params=dict(cap.params),
            target_label=cap.target_label,
        )
        for name, cap in sorted(_BY_NAME.items())
    }


def validate_proposal(action: str, target: str, *, read_only: bool) -> None:
    """提议时的校验。不合法就抛 `ActionError`，并说清为什么。

    **只读模式也照常允许提议** —— 只读禁的是"执行"，不是"建议"。
    建议是它最有价值的产出之一，禁掉它等于把管家废了。

    目标必填这一条**读的是能力目录里的声明**（`target_label`），不是猜：
    早先的实现用 `"{task_id}" in summary` 来推断，于是 `start_task`
    （它的目标走 `target`、params 里却写的是 `goal`）被判定为"不需要目标"，
    放行了一条必定执行失败的提议 —— 界面上因此出现一个点了必失败的按钮。
    """
    spec = _BY_NAME.get(action)
    if spec is None:
        allowed = ", ".join(sorted(_BY_NAME))
        raise ActionError(f"未知动作 {action!r}。可选：{allowed}")
    # 声明了 target 就必须给 —— 否则批准之后一定执行失败，
    # 那还不如现在就告诉用户（并且告诉他该填什么）。
    if spec.target_label and not target.strip():
        raise ActionError(
            f"动作 {action!r} 需要你补上「{spec.target_label}」，但 target 是空的。"
        )
    del read_only  # 只读不影响"能不能提议"


def validate_execution(action: str) -> ActionSpec:
    """执行时的**第二道校验**。白名单此刻必须仍然认这个动作。"""
    spec = _BY_NAME.get(action)
    if spec is None:
        raise ActionError(f"动作 {action!r} 已不在白名单里，拒绝执行。")
    return spec


# ---- 执行器注册 ----
#
# 每个执行器收到 (orchestrator, target)，返回一句人话结果。
# 刻意做成显式映射而不是 getattr 拼名字：拼名字会让"改个方法名"
# 变成"某个动作静默失效"，而显式映射漏了就是漏了，一眼看得出来。

Executor = Callable[[Any, str], Awaitable[str]]


async def _exec_kick(orchestrator: Any, target: str) -> str:
    await orchestrator.kick_tree(target)
    return f"已推起 {target}，它会继续往下跑。"


async def _exec_retry(orchestrator: Any, target: str) -> str:
    await orchestrator.retry_task(target)
    return f"已重新排队 {target}。"


async def _exec_pause(orchestrator: Any, target: str) -> str:
    from .models import TaskStatus

    orchestrator.set_task_status(target, TaskStatus.PAUSED, "用户通过管家批准暂停。")
    return f"已暂停 {target}。"


async def _exec_resume(orchestrator: Any, target: str) -> str:
    from .models import TaskStatus

    orchestrator.set_task_status(target, TaskStatus.RUNNING, "用户通过管家批准恢复。")
    return f"已恢复 {target}。"


async def _exec_cancel(orchestrator: Any, target: str) -> str:
    from .models import TaskStatus

    orchestrator.set_task_status(target, TaskStatus.CANCELED, "用户通过管家批准取消。")
    return f"已取消 {target}（不可逆）。这台子树的产物保留在它的工作树里。"


async def _exec_hibernate(orchestrator: Any, target: str) -> str:
    # 方法名是 `hibernate_tree` 而不是 `hibernate_task`（实测对过签名）——
    # 这里刻意不猜方法名：猜错的表现是"某个动作永远失败"，而那种错很难归因。
    # 它同样是 **async**，必须 await。
    await orchestrator.hibernate_tree(target, 18000, "用户通过管家批准休眠。")
    return f"已让 {target} 休眠（5 小时后允许恢复）。"


async def _exec_cancel_all(orchestrator: Any, target: str) -> str:
    del target
    # `cancel_all` 是 async（实测签名）：漏掉 await 不会报错，只会静默不执行，
    # 然后 `f"...{count}"` 打印出一个协程对象 —— "看起来成功了，其实什么都没做"。
    count = await orchestrator.cancel_all()
    return f"已取消全部项目，共 {count} 个节点（不可逆）。"


async def _exec_set_budget_guard(orchestrator: Any, target: str) -> str:
    del target
    # 同上：`set_budget_guard` 是 async。这类遗漏是执行层最隐蔽的一种错，
    # 所以本文件里对 orchestrator 的每个调用都显式 await，并由测试钉住。
    await orchestrator.set_budget_guard(
        None, 3600, "用户通过管家批准开启预算闸门。", hibernate_tree=False
    )
    return "已开启预算闸门（1 小时后允许恢复）。"


async def _exec_start_task(orchestrator: Any, target: str) -> str:
    # `target` 承担 goal：提议里没有别的地方放目标描述，
    # 而复用 target 让"批准"在界面上仍然只有一对（动作 + 一个参数）。
    goal = target.strip()
    if not goal:
        raise ActionError("起新树需要给出目标描述（放在 target 里）。")
    root_id = await orchestrator.start_one_shot_project(goal)
    return f"已起新树，根节点 {root_id}。"


EXECUTORS: dict[str, Executor] = {
    "kick_root": _exec_kick,
    "retry_node": _exec_retry,
    "pause_node": _exec_pause,
    "resume_node": _exec_resume,
    "cancel_node": _exec_cancel,
    "hibernate_node": _exec_hibernate,
    "cancel_all": _exec_cancel_all,
    "set_budget_guard": _exec_set_budget_guard,
    "start_task": _exec_start_task,
}


async def execute(orchestrator: Any, action: str, target: str) -> str:
    """执行一个**已批准**的动作。

    调用方必须先确认它被批准过 —— 这里不管审批状态，只管"动作本身能不能做"。
    这样职责清楚：审批是策略，执行是能力；混在一起会让两条逻辑都难测。
    """
    validate_execution(action)
    executor = EXECUTORS.get(action)
    if executor is None:
        # 白名单里有、但没有执行器 —— 这是**编码疏漏**，不是用户错误。
        # 响亮报出来，别静默当成功。
        raise ActionError(f"动作 {action!r} 在白名单里却没有实现，拒绝执行（这是编码疏漏）。")
    return await executor(orchestrator, target)


# ---------- 持久化与审批 ----------

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
FAILED = "failed"


def _now() -> Any:
    from .models import utcnow

    return utcnow()


def propose(db: Any, proposed: list[dict[str, Any]], *, read_only: bool) -> tuple[list[Any], list[str]]:
    """把管家的建议落成 `pending` 动作。返回 `(入队的动作, 被拒的原因)`。

    **不合法的建议不写库，而是把原因回给调用方**：
    写进去会让界面上出现"点了会失败"的按钮，而用户看不出来它为什么坏。
    拒掉并说明原因，用户才能判断是模型幻觉还是自己该补参数。

    `read_only` 只影响**执行**，不影响**提议** —— 建议是管家最有价值的产出之一。
    """
    from .models import StewardAction

    queued: list[Any] = []
    rejected: list[str] = []
    for item in proposed or []:
        action = str((item or {}).get("action") or "").strip()
        target = str((item or {}).get("target") or "").strip()
        why = str((item or {}).get("why") or "").strip()
        risk = str((item or {}).get("risk") or "").strip()
        if not action:
            continue
        try:
            validate_proposal(action, target, read_only=read_only)
        except ActionError as exc:
            rejected.append(f"{action}: {exc}")
            continue
        row = StewardAction(action=action, target=target, why=why, risk=risk, status=PENDING)
        db.add(row)
        queued.append(row)
    if queued:
        db.commit()
        for row in queued:
            db.refresh(row)
    return queued, rejected


def list_actions(db: Any, *, status: str = "", limit: int = 50) -> list[Any]:
    from sqlalchemy import select

    from .models import StewardAction

    query = select(StewardAction).order_by(StewardAction.id.desc()).limit(limit)
    if status:
        query = (
            select(StewardAction)
            .where(StewardAction.status == status)
            .order_by(StewardAction.id.desc())
            .limit(limit)
        )
    return list(db.execute(query).scalars().all())


def decide(db: Any, action_id: int, *, approve: bool) -> Any:
    """批准或拒绝一个待办动作。**不执行** —— 执行由 `run_approved` 单独做。

    为什么把"决定"和"执行"分开：批准是用户的意图，执行是动作本身。
    混在一起会让"批准了但执行失败"变成一个没有记录的状态，
    而那种情况恰恰最需要留痕（用户以为做了，其实没做）。
    """
    from .models import StewardAction

    row = db.get(StewardAction, action_id)
    if row is None:
        raise ActionError(f"找不到动作 #{action_id}")
    if row.status != PENDING:
        raise ActionError(f"动作 #{action_id} 已经是 {row.status}，不能重复决定。")
    row.status = APPROVED if approve else REJECTED
    row.decided_at = _now()
    db.commit()
    db.refresh(row)
    return row


async def run_approved(db: Any, orchestrator: Any, action_id: int) -> tuple[bool, str]:
    """执行一个已批准的动作，并把结果/错误落库。返回 `(成功?, 说明)`。

    执行前**再查一次白名单**：提议与批准之间可能过了很久，
    而执行是最危险的时刻 —— 它必须自己判，不能只信"当时验过"。
    """
    from .models import StewardAction

    row = db.get(StewardAction, action_id)
    if row is None:
        raise ActionError(f"找不到动作 #{action_id}")
    if row.status != APPROVED:
        raise ActionError(f"动作 #{action_id} 尚未批准（当前 {row.status}），拒绝执行。")

    try:
        validate_execution(row.action)  # 第二道校验
        message = await execute(orchestrator, row.action, row.target)
    except Exception as exc:  # noqa: BLE001 - 任何失败都要落库，不能只抛给 HTTP
        row.status = FAILED
        row.error = f"{exc.__class__.__name__}: {exc}"
        row.executed_at = _now()
        db.commit()
        return False, row.error

    row.status = EXECUTED
    row.result = message
    row.executed_at = _now()
    db.commit()
    db.refresh(row)
    return True, message
