"""宪法版本在任务树里的下沉与判读。

**为什么单独一个模块**：宪法版本是"上下文新鲜度"唯一可机器判定的依据，而它有三条
互相咬合的规则，分开写就读不懂：

1. **下沉必须单调继承**（`docs/spec-parent-child-obligations.md` §7.1.1）：
   `child.version = max(parent.version, 建节点时的头版本)`。否则子节点会拿旧版语义反向
   覆盖父节点已升级的判据。
2. **修订时按状态分治**（`docs/spec-constitution-amendment.md` §6）：全树的节点不是一个
   整数，是若干类——`planned` 换快照重规划代价为零，`running` 中途换版会产出半旧半新的
   产物，`done` 不自动重做但要按方向决定是否再验证。**不能一个循环刷下去。**
3. **方向敏感**（同 spec §7）：收紧时已完成产物可能不再合规，**必须**再验证；放宽时产物
   仍合规，**不得**拿新宪法追溯洗白已失败/阻塞的节点——那正是
   `docs/spec-modularization.md` §7.3 禁止的"放宽断言来变绿"。

本模块只做**纯判读**：不碰数据库、不读文件、不改任何节点状态，因此可脱离 orchestrator 单测。

**接线状态要说清楚**：产生版本变更的事务（`amend_constitution`：新版本落库、快照替换、
事件、原子性、续跑）尚未实现。因此 `migration_plan` / `version_debt` 目前只被单元测试
与看板消费，**不产生任何副作用**——这不是"已接线"，别按已完成读。
"""

from __future__ import annotations

from typing import Any

from . import obligations
from .models import TaskStatus

# 迁移动作：§6 裁决表的机器可读形式
MIGRATE_NOW = "migrate_now"
SELF_ONLY = "self_only"
WAIT_RUNNING = "wait_running"
REVALIDATE = "revalidate"
RECLASSIFY = "reclassify"
ON_RESUME = "on_resume"
STAMP_ONLY = "stamp_only"

ACTIONS = (MIGRATE_NOW, SELF_ONLY, WAIT_RUNNING, REVALIDATE, RECLASSIFY, ON_RESUME, STAMP_ONLY)

# 修订方向：由**人**显式声明，不由代码推断（产出方自己判断方向 = 自己放水）
DIRECTION_TIGHTEN = "tighten"
DIRECTION_LOOSEN = "loosen"
DIRECTION_MIXED = "mixed"
DIRECTION_NEUTRAL = "neutral"

DIRECTIONS = (DIRECTION_TIGHTEN, DIRECTION_LOOSEN, DIRECTION_MIXED, DIRECTION_NEUTRAL)

UNKNOWN_VERSION_KEY = "unknown"

# 未知状态**保守处理**：要求新宪法下的一次真实裁决，而不是静默放行。
# 取名 `REVALIDATE` 而不是"忽略"，因为"静默"正是本设计要消灭的东西。
FALLBACK_ACTION = REVALIDATE

_STATUS_ACTIONS: dict[str, str] = {
    # 尚无产物：换快照重规划的代价为零
    TaskStatus.PLANNED.value: MIGRATE_NOW,
    TaskStatus.QUEUED.value: MIGRATE_NOW,
    TaskStatus.REPAIR.value: MIGRATE_NOW,
    # 只换自身快照；子节点各自按本表裁决，避免重复遍历
    TaskStatus.WAITING_CHILDREN.value: SELF_ONLY,
    # 执行中**不动**：中途换版会让产物半旧半新，且 `constitution_verdict` 与实际不符。
    # `planning` / `decomposing` 同样在写（产出是子任务与验收清单），所以按执行中处理。
    TaskStatus.PLANNING.value: WAIT_RUNNING,
    TaskStatus.DECOMPOSING.value: WAIT_RUNNING,
    TaskStatus.RUNNING.value: WAIT_RUNNING,
    TaskStatus.WORKING.value: WAIT_RUNNING,
    TaskStatus.VERIFYING.value: WAIT_RUNNING,
    TaskStatus.ACCEPTING.value: WAIT_RUNNING,
    # 产物是既成事实：不自动重做，收紧时进再验证队列（放宽时只看 `action_for_status`）
    TaskStatus.DONE.value: REVALIDATE,
    # 旧版下的分类可能已失效 → 按新宪法重新分类并重试
    TaskStatus.BLOCKED.value: RECLASSIFY,
    TaskStatus.FAILED.value: RECLASSIFY,
    # 已停下的：恢复时自然用新版
    TaskStatus.PAUSED.value: ON_RESUME,
    TaskStatus.HIBERNATING.value: ON_RESUME,
    TaskStatus.CANCELED.value: STAMP_ONLY,
}


def normalize_version(value: Any) -> int | None:
    """把版本号收敛成非负整数；`None` / 非法值返回 `None`（表示"未登记"）。"""
    if value is None:
        return None
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return version if version >= 0 else None


def normalize_direction(value: Any) -> str:
    """未声明的方向按**收紧**处理：保守优先，避免"没写方向"变成静默放宽。"""
    text = str(value or "").strip().lower()
    return text if text in DIRECTIONS else DIRECTION_TIGHTEN


def inherit_version(parent_version: Any, head_version: Any) -> int | None:
    """下沉规则：`max(父版本, 建节点时的头版本)`。

    取 `max` 而不是直接取头版本，是为了在"父节点已被升级、头版本却又落后"的异常场景下
    也不会让新节点降版本——**单调性是这条规则的全部意义**。

    父节点版本未登记时返回 `None`（子节点也标未登记）：子节点继承的是**父节点的快照**，
    拿头版本给它盖章会造出"版本写新版、快照还是旧版"的半旧半新，正是 §6 `WAIT_RUNNING`
    要避免的那种错配。未知不冒充已知。

    `head_version` 必须是**父节点快照已经携带**的版本。当前可从 `ConstitutionRecord.version`
    取，因为已锁定的宪法不允许再 `update_constitution`，所以运行中的树 head 恒等于建树时的
    版本；等到修订事务（A3）让头版本真正前进，A3 必须在**同一事务**里替换快照，否则这里的
    `max` 会放大那个错配。
    """
    parent = normalize_version(parent_version)
    if parent is None:
        return None
    head = normalize_version(head_version)
    return parent if head is None else max(parent, head)


def forces_revalidation(direction: Any) -> bool:
    """只有收紧（含 `mixed` 的保守取法）才必须再验证。

    放宽与中性**不得**触发再验证：它们的产物仍合规，拉回来复查没有意义，
    更不允许借新宪法追溯洗白已 `blocked` / `failed` 的节点。
    """
    return normalize_direction(direction) not in {DIRECTION_LOOSEN, DIRECTION_NEUTRAL}


def action_for_status(status: Any, *, direction: Any = DIRECTION_TIGHTEN) -> str:
    """节点状态 → 迁移动作。方向只影响 `done`（既成事实的那一类）。"""
    text = str(getattr(status, "value", status) or "").strip()
    action = _STATUS_ACTIONS.get(text, FALLBACK_ACTION)
    if action == REVALIDATE and not forces_revalidation(direction):
        return STAMP_ONLY
    return action


def is_stale(node: Any, effective_version: Any) -> bool:
    """落后 → 真；**未登记版本 → 假**。

    存量行（本列引入前）的版本是"未知"而不是"落后"，把它算成落后会让升级瞬间给全树
    记满债。未知由 `version_distribution` 的 `unknown` 桶显式暴露，不靠沉默。
    """
    node_version = normalize_version(getattr(node, "constitution_version", None))
    effective = normalize_version(effective_version)
    if node_version is None or effective is None:
        return False
    return node_version < effective


def stale_nodes(nodes: list[Any], effective_version: Any) -> list[Any]:
    return [node for node in nodes if is_stale(node, effective_version)]


def version_distribution(nodes: list[Any]) -> dict[str, int]:
    """`{版本: 节点数}`，未登记的落到 `unknown` 桶。看板靠它一眼看出几个版本并存。"""
    distribution: dict[str, int] = {}
    for node in nodes:
        version = normalize_version(getattr(node, "constitution_version", None))
        key = str(version) if version is not None else UNKNOWN_VERSION_KEY
        distribution[key] = distribution.get(key, 0) + 1
    return distribution


def migration_plan(
    nodes: list[Any],
    *,
    effective_version: Any,
    direction: Any = DIRECTION_TIGHTEN,
) -> dict[str, list[str]]:
    """按 §6 裁决表把**落后的**节点分桶。

    只对落后节点分桶：未落后的节点没有迁移动作，把它们列进来只会让看板噪声化。
    返回值七个桶**全部存在**（含空桶），免得消费方到处写 `get(..., [])`。
    """
    direction = normalize_direction(direction)
    plan: dict[str, list[str]] = {action: [] for action in ACTIONS}
    for node in sorted(stale_nodes(nodes, effective_version), key=lambda item: str(getattr(item, "id", ""))):
        plan[action_for_status(getattr(node, "status", ""), direction=direction)].append(str(getattr(node, "id", "")))
    return plan


def version_board(
    nodes: list[Any],
    *,
    effective_version: Any,
    effective_hash: str | None = None,
    revalidate_queue_depth: int = 0,
    last_amendment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """宪法传播看板（`docs/spec-constitution-amendment.md` §9）。

    它回答的是一个**不能靠自述回答**的问题：传播完成了吗。所以字段都可核对：几个版本并存、
    多少节点落后、落后的分几类、其中多少是"必须等它跑完"的、最近一次修订动了什么。

    三条恒等式（K7 判据，`tests/test_constitution_versions.py` 钉住）：
    - `sum(version_distribution.values()) == node_total`——不能有节点没被数进去；
    - `sum(stale_by_action.values()) == stale_total`——分桶不得多也不得少；
    - `running_on_old_version == stale_by_action["wait_running"]`——"必须等"的那部分只能来自执行中。

    `stale_by_action` 七个桶**全部返回**（含空桶）：少一个键，消费方就会把"没有"读成"未知"。
    """
    effective = normalize_version(effective_version)
    stale = stale_nodes(nodes, effective)
    return {
        "effective_version": effective,
        "effective_hash": effective_hash,
        "node_total": len(nodes),
        "version_distribution": version_distribution(nodes),
        "stale_total": len(stale),
        "stale_by_action": migration_plan(nodes, effective_version=effective),
        "running_on_old_version": sum(
            1 for node in stale if action_for_status(getattr(node, "status", "")) == WAIT_RUNNING
        ),
        "revalidate_queue_depth": int(revalidate_queue_depth or 0),
        "last_amendment": last_amendment,
    }


def version_debt(
    nodes: list[Any],
    *,
    effective_version: Any,
    direction: Any = DIRECTION_TIGHTEN,
) -> dict[str, list[dict[str, Any]]]:
    """版本落后 → 债务，**记在父节点上**（§7.2：子节点产生的债属于父节点）。

    根节点自己落后时归它自己（它没有父节点）。

    幂等性来自 `id`：`constitution_stale:{落后节点}:v{旧}->v{新}` 与
    `revalidate_required:{落后节点}:v{旧}->v{新}` 都不含"谁负责"，所以同一件事在任何轮次
    都落到同一条债上——这是 §6.4 教训的直接应用：**id 里不得写产出方**，否则修复方一出现
    就会生出新 id，债与子任务一起无限增长。
    """
    direction = normalize_direction(direction)
    effective = normalize_version(effective_version)
    result: dict[str, list[dict[str, Any]]] = {}
    for node in sorted(stale_nodes(nodes, effective_version), key=lambda item: str(getattr(item, "id", ""))):
        node_id = str(getattr(node, "id", ""))
        node_version = normalize_version(getattr(node, "constitution_version", None))
        owner = str(getattr(node, "parent_id", "") or node_id)
        depth = getattr(node, "depth", None)
        debt = obligations.obligations_from_version(
            node_id=node_id,
            node_version=node_version,
            effective_version=effective,
            direction=direction,
            depth=depth,
        )
        if str(getattr(node, "status", "")) == TaskStatus.DONE.value and forces_revalidation(direction):
            debt.append(
                obligations.make_obligation(
                    obligations.REVALIDATE_REQUIRED,
                    source_node_id=node_id,
                    discriminator=f"v{node_version}->v{effective}",
                    payload={
                        "node_version": node_version,
                        "effective_version": effective,
                        "direction": direction,
                    },
                    depth=depth,
                )
            )
        result.setdefault(owner, []).extend(debt)
    return result
