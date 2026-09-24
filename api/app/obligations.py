"""债务表（obligations）：父子之间"未清事项"的结构化载体。

**为什么需要它**：父节点"能否完成"原先由**子节点状态**判定（`_accept_parent` 只看
`child.status ∈ {blocked, failed}`）。这漏掉了一整类情况——**子节点已经是 `done`，
但它留下了一件没解决的事**。最尖锐的例子是 R4 已能识别的集成路径冲突：子节点全 done、
`path_collision` 已记进 `node_meta["parent_integration"]`，但父节点照样进入验收，
**没有任何人拦**。

统一抽象（见 `docs/spec-parent-child-obligations.md`）：

> 父节点能否完成，不应由「子节点状态」决定，而应由「**入站债务集合是否为空**」决定。
> 子节点状态只是债务的一种来源。

于是既有的 `blocked_child_reports` 成为本模块的一个特例，而
`_superseded_by_done_repair` 成为「债被 `repair_child` 清掉」的一条通路——
是既有机制的一般化，不是另起一套。

**本模块只做纯逻辑**：生成、幂等合并、清债校验、汇总。不碰数据库、不读文件，
因此可以脱离 orchestrator 单测。

不设自动过期：债务自动过期等于静默放宽，是 `docs/spec-modularization.md` §7.3 明令禁止的。
逃生舱只有人显式清债，且**必须留 reason**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# 债务类型
CHILD_BLOCKED = "child_blocked"
CHILD_FAILED = "child_failed"
INTEGRATION_CONFLICT = "integration_conflict"
INTEGRATION_SKIPPED = "integration_skipped"
CONSTITUTION_STALE = "constitution_stale"
REVALIDATE_REQUIRED = "revalidate_required"
CONSTRAINT_COVERAGE_GAP = "constraint_coverage_gap"
# R5 的跨版本回归：上一版验收过的产物在新版本下缺失/改变。
REGRESSION_BREAK = "regression_break"
# 验收面被改动（`docs/spec-verification-independence.md` §3）：产出方改了自己正要通过的考卷。
# 这是唯一一条**与档位无关、无条件阻断**的债——理由见下面 `surface_severity`。
ACCEPTANCE_SURFACE_TAMPERED = "acceptance_surface_tampered"
# 需求覆盖缺口（`docs/spec-requirement-traceability.md` §3.2）：
# - `requirement_uncovered`：声明了要求但一条可核对判据都没有 —— 这就是"漏需求"；
# - `requirement_unmet`：有判据但没过 —— 这是"没做到"。
REQUIREMENT_UNCOVERED = "requirement_uncovered"
REQUIREMENT_UNMET = "requirement_unmet"
# 跨节点泄露（`docs/spec-task-isolation.md` §2.3）：节点产物里出现了兄弟节点的工作树路径。
# **advisory**：文本里出现路径不等于真的读了它——这条是证据，不是防线。
CROSS_NODE_LEAK = "cross_node_leak"

# 严重度：blocking 会阻断父节点 done；advisory 只上浮不阻断。
SEVERITY_BLOCKING = "blocking"
SEVERITY_ADVISORY = "advisory"

# 清债通路
SETTLED_BY_REPAIR_CHILD = "repair_child"
SETTLED_BY_VERSION_CATCHUP = "version_catchup"
SETTLED_BY_ROUND_CLEAR = "round_clear"
SETTLED_BY_UPWARD_CLEAR = "upward_clear"
SETTLED_BY_COVERAGE_CLEARED = "coverage_cleared"
SETTLED_BY_HUMAN = "human"
VALID_SETTLED_BY = (
    SETTLED_BY_REPAIR_CHILD,
    SETTLED_BY_VERSION_CATCHUP,
    SETTLED_BY_ROUND_CLEAR,
    SETTLED_BY_UPWARD_CLEAR,
    SETTLED_BY_COVERAGE_CLEARED,
    SETTLED_BY_HUMAN,
)

# `integration_conflict` 的严重度由集成策略决定（见 `conflict_severity`）。
# 这里只写策略字面量，避免让纯模块依赖 `integration` 模块的实现细节；
# 两处不一致由 `tests/test_integration_conflicts.py` 的断言钉住。
INTEGRATION_STRATEGY_REPAIR = "repair"

# 下面这些类型天然阻断；其余按生成时判定。
# `integration_conflict` **不在**此列——它是策略敏感的（见 `conflict_severity`）。
ALWAYS_BLOCKING = (CHILD_BLOCKED, CHILD_FAILED, REVALIDATE_REQUIRED)

# **轮次域**的债务：由"某一轮集成看见了什么"定义，所以可以被"这一轮不再看见"清掉。
# 版本类债务（`constitution_stale` / `revalidate_required`）**不在**此列：它们不由某一轮
# 集成定义，若允许"这轮集成没提到它"就把它们清掉，那就是静默放宽。
ROUND_SCOPED_TYPES = (INTEGRATION_CONFLICT, INTEGRATION_SKIPPED)

# 与轮次域相对的另一半：**版本域**的债务，由"这个节点记着的版本落不落后"定义，
# 所以判据是节点当前版本，而不是"再验证跑过了没有"。
VERSION_SCOPED_TYPES = (CONSTITUTION_STALE, REVALIDATE_REQUIRED)

# `integration_skipped` 里哪些原因算 advisory：
# `absolute path` 是子节点 changed_files 写法问题，不必然丢产物；
# `outside integration boundary` 是越界（真丢且是安全问题）；
# `source missing` 是声明了却没产出（真丢）。
ADVISORY_SKIP_REASONS = ("absolute path",)

# 与档位无关、一律阻断的跳过原因：越界是**边界契约被破坏**，不是产物完整性策略问题。
ALWAYS_BLOCKING_SKIP_REASONS = ("outside integration boundary",)

DEFAULT_STALE_DIRECTION = "tighten"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def obligation_id(obligation_type: str, source_node_id: str, discriminator: str = "") -> str:
    """幂等键。同一冲突在多次集成中被重复识别时必须落到同一条债上。"""
    return f"{obligation_type}:{source_node_id}:{discriminator}"


def make_obligation(
    obligation_type: str,
    *,
    source_node_id: str,
    discriminator: str = "",
    payload: dict[str, Any] | None = None,
    severity: str = SEVERITY_BLOCKING,
    depth: int | None = None,
) -> dict[str, Any]:
    """构造一条未清债务。

    `depth` 只在**会沿深度累积**的类型上给（约束漂移、版本落后）——MasDrift 的核心发现是
    约束随委托链逐跳侵蚀，所以必须记下它发生在第几层，否则事后无法定位是第几跳引入的。
    """
    return {
        "id": obligation_id(obligation_type, source_node_id, discriminator),
        "type": obligation_type,
        "source_node_id": source_node_id,
        "severity": severity,
        "created_at": _now(),
        "settled_at": None,
        "settled_by": None,
        "settle_reason": None,
        "recurrence_count": 0,
        "depth": depth,
        "payload": payload or {},
    }


def conflict_severity(strategy: Any) -> str:
    """`integration_conflict` 的严重度**由集成策略决定**，而不是由门控的开关决定。

    为什么必须放在这里（`docs/spec-parent-child-obligations.md` §6.1/§6.2）：判据 2 是
    "入站债务中不存在 blocking 且未清的项"，**一条无条件判据**。如果严重度还是写死的 blocking，
    而由门控按配置去过滤，就会出现"看板报阻断、树照跑"——看板就不再回答"树有没有被卡"，
    §9 存在的理由当场失效。

    - `repair` 档：冲突**阻断**验收（这是该档的语义）。
    - `overwrite` / `keep_both` 档：冲突**记录并上浮但不阻断**。`overwrite` 是默认档，
      它的语义本来就是"后写覆盖但绝不无声"；在那里拦 `done` 等于把默认行为悄悄改成阻断。
      `keep_both` 更明显：两份都留着，没有丢东西，没有理由阻断。

    升级为 blocking 是策略档位的选择，**不是产出方能自己投票的**：这满足 §6.2 的"降级为
    advisory 必须由规则显式声明"。
    """
    text = str(strategy or "").strip().lower()
    return SEVERITY_BLOCKING if text == INTEGRATION_STRATEGY_REPAIR else SEVERITY_ADVISORY


def skipped_severity(reason: Any, strategy: Any = "") -> str:
    """`integration_skipped` 的严重度。三类原因，规则不同：

    - `absolute path`：子节点 `changed_files` 写法问题，不必然丢产物 → **advisory**。
    - `outside integration boundary`：越界。这不是策略问题而是**边界契约被破坏**，
      与档位无关一律 **blocking**。
    - `source missing`：声明了却没产出。属"产物完整性"，与冲突同类 → **由档位决定**
      （`repair` 阻断；`overwrite`/`keep_both` 只上浮）。

    `source missing` 这一条是**修正过的写法**：原文把它一律列为 blocking，实测结果是默认档下
    4 条既有契约（worker 阻塞 → 叶子转父节点的几条用例，其 mock 子节点声明了并不落盘的
    `changed_files`）当场变成 `repair`。那不是"新发现了 bug"，而是把默认档悄悄改成阻断档——
    与用户在集成冲突上选定的"记录并上浮，后写仍覆盖"自相矛盾。规则改成让档位决定，
    严格档（`repair`）仍然阻断。
    """
    text = str(reason or "").strip().lower()
    if text in ADVISORY_SKIP_REASONS:
        return SEVERITY_ADVISORY
    if text in ALWAYS_BLOCKING_SKIP_REASONS:
        return SEVERITY_BLOCKING
    return conflict_severity(strategy)


def stale_severity(direction: Any) -> str:
    """版本债务的方向敏感：**收紧必须阻断，放宽只上浮**。

    放宽时把已完成产物拉回来复查没有意义（它们仍然合规），更不允许拿新宪法去追溯"洗白"
    已失败/阻塞的节点——那正是 §7.3 禁的"放宽断言来变绿"。
    """
    return SEVERITY_ADVISORY if str(direction or "").strip().lower() == "loosen" else SEVERITY_BLOCKING


def regression_severity(kind: Any, strategy: Any = "") -> str:
    """`regression_break` 的严重度（R5）。

    - `missing`（上一版验收过的产物**没了**）：一律 **blocking**。
      正常返工不会悄悄删掉已验收产物；缺了就是缺了，必须有人看一眼。
    - `changed`（内容变了）：**由策略决定**——`repair` 档阻断，`record` 档只记录并上浮。
      理由与 `conflict_severity` 同源：字节变化本身**可能正是修复动作**（新版本下的返工本来就要改文件），
      默认档拦 `done` 等于把默认行为悄悄改成阻断档，然后"看板报阻断、树照跑"就来了。
    """
    if str(kind or "").strip().lower() == "missing":
        return SEVERITY_BLOCKING
    return conflict_severity(strategy)


# `regression_break` 刻意**不**进 `ROUND_SCOPED_TYPES`：它不由"某一轮集成看见了什么"定义，
# 若允许"这轮没提到它就清掉"，就等于把回退静默放行。
# 也刻意**不**进 `VERSION_SCOPED_TYPES`：版本追平不代表"上一版验证过的产物又对了"，
# 拿版本追平去清它同样是静默放宽。它只由"产物回到基线"或人工清（见 `settle_regression_restored`）。


# 允许"清掉之后**因版本前进而再次回退**"重新阻断的类型：`regression_break` 是**版本域**，
# 同一个 `(kind, path)` 在 v2 与 v3 是两件独立的事——基线版本又前进之后它再坏一次，必须重新挡。
# 其余类型不复活：轮次域刻意不复活（否则"清了又报"会把人拖进反复清债）。
REVIVABLE_TYPES = (REGRESSION_BREAK, ACCEPTANCE_SURFACE_TAMPERED, REQUIREMENT_UNCOVERED, REQUIREMENT_UNMET)


def _evidence_version(item: dict[str, Any]) -> Any:
    """"这条债当初是按哪一版的事实记的"——回归债用基线版本，需求债用当前宪法版本。

    复活判据只看"这一版的证据和上一次是不是同一版"，所以两类债可以共用同一个读取口。
    """
    payload = item.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload.get("baseline_version", payload.get("requirement_version"))


def _revives_settled(current: dict[str, Any], item: dict[str, Any]) -> bool:
    if not current.get("settled_at"):
        return False
    if str(current.get("type") or "") not in REVIVABLE_TYPES:
        return False
    old, new = _evidence_version(current), _evidence_version(item)
    return old is not None and new is not None and old != new


def _escalate_severity(current: dict[str, Any], item: dict[str, Any]) -> None:
    """严重度只升不降：档位从 `record` 切到 `repair` 时，已登记的债必须跟着变成阻断。

    降级**不**跟着走是刻意的——严重度一旦按旧档位记成 blocking，让它自己掉回 advisory 就是静默放宽。
    """
    if str(item.get("severity") or "") == SEVERITY_BLOCKING:
        current["severity"] = SEVERITY_BLOCKING


def upsert(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `id` 幂等合并。

    三条规则：
    - 已存在且**未清** → 更新 payload 与时间戳，不重复追加；严重度**只升不降**。
    - 已存在且**已清** → 保持已清，只累加 `recurrence_count`。
      不复活是刻意的：否则"清掉→下一轮集成又报同一条"会形成死循环，人会被迫反复清债。
    - **例外**：`REVIVABLE_TYPES`（版本域的回归债）在"基线版本又前进"之后再次回退时**复活**。
      没有这条，一条路径第二次坏掉就再也拦不住了——第一次修好之后它已清，而清过的东西不再阻断。
    """
    by_id = {str(item.get("id") or ""): dict(item) for item in existing}
    for item in incoming:
        key = str(item.get("id") or "")
        if not key:
            continue
        current = by_id.get(key)
        if current is None:
            by_id[key] = dict(item)
            continue
        if _revives_settled(current, item):
            for field in ("settled_at", "settled_by", "settle_reason"):
                current.pop(field, None)
            current["recurrence_count"] = int(current.get("recurrence_count") or 0) + 1
            current["payload"] = dict(item.get("payload") or {})
            current["severity"] = str(item.get("severity") or current.get("severity") or "")
            current["updated_at"] = _now()
            continue
        current["payload"] = dict(item.get("payload") or current.get("payload") or {})
        _escalate_severity(current, item)
        if current.get("settled_at"):
            current["recurrence_count"] = int(current.get("recurrence_count") or 0) + 1
            continue
        current["updated_at"] = _now()
    return list(by_id.values())


def blocking(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in obligations
        if item.get("severity") == SEVERITY_BLOCKING and not item.get("settled_at")
    ]


def has_blocking(obligations: list[dict[str, Any]]) -> bool:
    return bool(blocking(obligations))


def settle(
    obligations: list[dict[str, Any]],
    target_id: str,
    *,
    settled_by: str,
    reason: str = "",
    actor: str = "",
) -> list[dict[str, Any]]:
    """清债。**人清债必须给理由与身份**——不是禁止放宽，而是放宽必须可见（§8.2）。

    机器清债（`repair_child` / `version_catchup` / `round_clear`）不强制 reason，因为它们各自
    有可核对的依据（修复子任务已完成 / 版本已追平 / 本轮不再复现）。
    """
    if settled_by not in VALID_SETTLED_BY:
        raise ValueError(f"未知的清债通路：{settled_by}")
    actor = str(actor or "").strip()
    if settled_by == SETTLED_BY_HUMAN and not str(reason or "").strip():
        raise ValueError("人工清债必须给出 reason，否则放宽就是无声的。")
    if settled_by == SETTLED_BY_HUMAN and not actor:
        raise ValueError("人工清债必须标明是谁清的：没有身份的放宽无法问责。")
    found = False
    result: list[dict[str, Any]] = []
    for item in obligations:
        current = dict(item)
        if str(current.get("id") or "") == target_id and not current.get("settled_at"):
            current["settled_at"] = _now()
            current["settled_by"] = settled_by
            current["settle_reason"] = str(reason or "").strip()
            current["settled_actor"] = actor or current.get("settled_actor")
            found = True
        result.append(current)
    if not found:
        raise KeyError(f"没有找到未清债务：{target_id}")
    return result


def obligations_from_integration(
    parent_id: str,
    integration_detail: dict[str, Any] | None,
    *,
    strategy: Any = "",
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """把 R4 已识别的冲突/跳过明细转成债务。

    `strategy` 决定冲突的严重度（见 `conflict_severity`）——**必须由调用方传真实的档位**，
    默认值刻意是"非 repair"，也就是"不给策略就别声称阻断"，避免漏传时把默认档悄悄变成阻断档。

    `depth` 是**这一层**的深度（父节点自己的深度）：看板的 `by_depth` 要回答"债务集中在第几层"，
    所以每种债都记层号，而不只记会沿委托链累积的那两种。

    R4 负责**识别**（`api/app/integration.py`），本函数负责**接入门控**——
    这是同一个缺口的两半，缺任一半都不成立。

    **幂等键绑在"这一件事"上，而不是绑在"谁产出的"上**：债务的 owner 是**父节点**
    （它是那个"不能完成"的一方），产出方只作为 payload 信息。这一点不是风格问题——
    若把产出方写进 id，那么为这条债创建的修复子任务自身也会成为新的产出方，
    下一轮集成会生成一个**全新的 id**，于是又新建修复子任务，债务与子任务一起无限增长
    （实测表现为 `RecursionError`）。同一路径的冲突在任意轮次都必须是同一条债。
    """
    if not isinstance(integration_detail, dict):
        return []
    severity = conflict_severity(strategy)
    result: list[dict[str, Any]] = []
    for conflict in integration_detail.get("conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        path = str(conflict.get("path") or "")
        result.append(
            make_obligation(
                INTEGRATION_CONFLICT,
                source_node_id=parent_id,
                discriminator=path,
                payload={
                    "path": path,
                    "conflict_type": conflict.get("type"),
                    "writers": conflict.get("writers"),
                    "overwritten": conflict.get("overwritten"),
                    "destination": conflict.get("destination"),
                    "strategy": str(strategy or ""),
                },
                severity=severity,
                depth=depth,
            )
        )
    for skipped in integration_detail.get("skipped") or []:
        if not isinstance(skipped, dict):
            continue
        path = str(skipped.get("path") or "")
        reason_text = str(skipped.get("reason") or "")
        result.append(
            make_obligation(
                INTEGRATION_SKIPPED,
                source_node_id=parent_id,
                discriminator=f"{path}|{reason_text}",
                payload={
                    "path": path,
                    "reason": reason_text,
                    "child_id": skipped.get("child_id"),
                    "output_node_id": skipped.get("output_node_id"),
                    "strategy": str(strategy or ""),
                },
                severity=skipped_severity(reason_text, strategy),
                depth=depth,
            )
        )
    return result


def obligations_from_version(
    *,
    node_id: str,
    node_version: int | None,
    effective_version: int | None,
    direction: str = DEFAULT_STALE_DIRECTION,
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """版本落后 → 一条 `constitution_stale` 债。

    收紧方向上它是 blocking（已完成的旧版产物可能不再合规，必须复查）；
    放宽方向上它是 advisory（产物仍合规，复查无意义，且不得追溯洗白已失败节点）。
    """
    if node_version is None or effective_version is None or node_version >= effective_version:
        return []
    return [
        make_obligation(
            CONSTITUTION_STALE,
            source_node_id=node_id,
            discriminator=f"v{node_version}->v{effective_version}",
            payload={"node_version": node_version, "effective_version": effective_version, "direction": direction},
            severity=stale_severity(direction),
            depth=depth,
        )
    ]


def obligations_from_regression(
    *,
    node_id: str,
    diff: dict[str, Any],
    current_version: int | None,
    strategy: Any = "",
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """把 `regression.diff_baseline` 的结果转成债务条目（R5）。

    `strategy` 必须由调用方传**真实档位**，默认值刻意是"非 repair"：不给策略就别声称阻断。

    幂等键绑在**产物路径**上（`regression_break:<node>:<kind>|<path>`），不绑在"谁改坏的"上：
    理由与 `obligations_from_integration` 完全相同——否则为这条债创建的修复子任务自身会变成
    新的产出方，下一轮生成新 id，债务与子任务一起无限增长（实测表现为 `RecursionError`）。
    同一路径的回退在任意轮次都必须是同一条债。

    `missing` 与 `changed` 分开进 discriminator：同一个文件先丢后改（或反之）是两件事，
    合成一条会让"到底恢复了没有"说不清。
    """
    result: list[dict[str, Any]] = []
    base_version = diff.get("base_version")
    for kind in ("missing", "changed"):
        for item in diff.get(kind) or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            result.append(
                make_obligation(
                    REGRESSION_BREAK,
                    source_node_id=node_id,
                    discriminator=f"{kind}|{path}",
                    payload={
                        "path": path,
                        "kind": kind,
                        "was": item.get("was"),
                        "now": item.get("now"),
                        "baseline_version": base_version,
                        "current_version": current_version,
                        "strategy": str(strategy or ""),
                    },
                    severity=regression_severity(kind, strategy),
                    depth=depth,
                )
            )
    return result


def surface_severity() -> str:
    """`acceptance_surface_tampered` 的严重度：**无条件 blocking**，不接受任何档位。

    与 `conflict_severity` / `regression_severity` 刻意不同：那两条是"产物层面的鲁棒性"，
    可以让人按档位选择记录还是阻断；这一条是**裁判被动过**——降成 advisory 等于承认
    "产出方可以改考卷，我们只记一笔"。所以这里没有策略参数，也不许有人加一个。
    """
    return SEVERITY_BLOCKING


def obligations_from_acceptance_surface(
    *,
    node_id: str,
    diff: dict[str, Any],
    current_version: int | None = None,
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """把验收面比对结果转成债务条目。

    幂等键绑在**路径**上（`acceptance_surface_tampered:<node>:<kind>|<path>`），不绑在"谁改的"上：
    与回归门禁同源——否则为这条债创建的修复子任务会变成新的产出方，下一轮生成新 id，债务与子任务
    一起无限增长。
    """
    result: list[dict[str, Any]] = []
    for kind in ("changed", "missing"):
        for item in diff.get(kind) or []:
            path = str(item.get("path") or "")
            if not path:
                continue
            result.append(
                make_obligation(
                    ACCEPTANCE_SURFACE_TAMPERED,
                    source_node_id=node_id,
                    discriminator=f"{kind}|{path}",
                    payload={
                        "path": path,
                        "kind": kind,
                        "was": item.get("was"),
                        "now": item.get("now"),
                        "frozen_version": diff.get("frozen_version"),
                        "frozen_by": diff.get("frozen_by"),
                        "current_version": current_version,
                    },
                    severity=surface_severity(),
                    depth=depth,
                )
            )
    return result


def obligations_from_requirements(
    *,
    node_id: str,
    uncovered: list[dict[str, Any]],
    unmet: list[dict[str, Any]],
    current_version: int | None = None,
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """需求覆盖缺口 → 债务（`docs/spec-requirement-traceability.md` §3.2）。

    两类都**无条件 blocking**：没有判据的要求等于没有要求（漏需求），有判据而没过就是没做到。
    与验收面篡改一样，这里不给档位参数——"要不要拦漏需求"不是可配置项。

    幂等键绑**要求 id /（要求 id，判据 id）**，不绑"谁改的"：与 R5/B 同源，避免修复子任务制造新 id。
    """
    result: list[dict[str, Any]] = []
    for item in uncovered:
        requirement_id = str(item.get("id") or "")
        if not requirement_id:
            continue
        result.append(
            make_obligation(
                REQUIREMENT_UNCOVERED,
                source_node_id=node_id,
                discriminator=requirement_id,
                payload={
                    "requirement_id": requirement_id,
                    "text": str(item.get("text") or ""),
                    # 复活判据要的"这一版"：同一条要求在更晚的版本里再次没被覆盖是另一件事
                    "requirement_version": current_version,
                },
                severity=SEVERITY_BLOCKING,
                depth=depth,
            )
        )
    for item in unmet:
        requirement_id = str(item.get("requirement_id") or "")
        criterion_id = str(item.get("criterion_id") or "")
        if not requirement_id or not criterion_id:
            continue
        result.append(
            make_obligation(
                REQUIREMENT_UNMET,
                source_node_id=node_id,
                discriminator=f"{requirement_id}|{criterion_id}",
                payload={
                    "requirement_id": requirement_id,
                    "criterion_id": criterion_id,
                    "type": item.get("type"),
                    "path": item.get("path"),
                    "detail": item.get("detail"),
                    "status": item.get("status"),
                    "requirement_version": current_version,
                },
                severity=SEVERITY_BLOCKING,
                depth=depth,
            )
        )
    return result


def obligations_from_leaks(
    *,
    node_id: str,
    found: list[dict[str, Any]],
    depth: int | None = None,
) -> list[dict[str, Any]]:
    """跨节点泄露 → **advisory** 债（`docs/spec-task-isolation.md` §2.3）。

    幂等键绑 `(节点, 命中的兄弟路径)`：同一条泄露重复出现不会堆债，换个路径则是一条新债。
    """
    result: list[dict[str, Any]] = []
    for item in found:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        result.append(
            make_obligation(
                CROSS_NODE_LEAK,
                source_node_id=node_id,
                discriminator=path,
                payload={"path": path, "excerpt": str(item.get("excerpt") or "")},
                severity=SEVERITY_ADVISORY,
                depth=depth,
            )
        )
    return result


def settle_round_cleared(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    round_scoped_types: tuple[str, ...] = ROUND_SCOPED_TYPES,
) -> list[dict[str, Any]]:
    """清掉**本轮集成不再复现**的轮次域债务。

    这是 `docs/spec-parent-child-obligations.md` §6.4 硬约束 2 的落地。第一版接线拿
    "修复子任务完成"当清债条件，结果是自增强循环（单个用例跑 5993 秒后 RecursionError）。
    这里换成**天然有界**的条件：某一件事只要在**本轮**集成里不再出现，就结清——不依赖谁
    完成了什么，也不依赖轮次计数。

    只作用于 `round_scoped_types`。版本类债务不由集成轮次定义，绝不能因为"这一轮没提到它"
    被顺手清掉：那等于静默放宽。

    刻意**不**把已清项重新拆开：清掉之后下一轮若又复现同一件事，`upsert` 会把它作为
    已清项累加 `recurrence_count` 而不是复活（见 `upsert` 的说明）——否则"清了又报、
    报了又清"会把人拖进反复清债的循环。
    """
    incoming_ids = {str(item.get("id") or "") for item in incoming}
    scoped = set(round_scoped_types)
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        if (
            str(current.get("type") or "") in scoped
            and not current.get("settled_at")
            and str(current.get("id") or "") not in incoming_ids
        ):
            current["settled_at"] = _now()
            current["settled_by"] = SETTLED_BY_ROUND_CLEAR
            current["settle_reason"] = "本轮集成不再复现该冲突/跳过"
        result.append(current)
    return result


def settle_version_caught_up(
    existing: list[dict[str, Any]],
    *,
    versions: dict[str, int | None],
    effective_version: int | None,
) -> list[dict[str, Any]]:
    """清掉**已经追平**的版本类债务（`constitution_stale` / `revalidate_required`）。

    与 `settle_round_cleared` 是同一个模式的另一半：清债条件是**可判的事实**——"这个节点当前
    记着的版本已经不落后了"，不依赖谁完成了什么，也不依赖轮次计数。版本未登记的节点（`None`）
    **不清**：未知不等于追平。

    刻意不拿"再验证跑过了"当条件：那会把清债绑死在一次具体运行上——运行重试、中断、换个人跑
    就说不清了。版本是落库的事实，判它就够了。
    """
    effective = effective_version if isinstance(effective_version, int) else None
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        node_version = versions.get(str(current.get("source_node_id") or ""))
        caught_up = (
            effective is not None
            and not current.get("settled_at")
            and str(current.get("type") or "") in VERSION_SCOPED_TYPES
            and isinstance(node_version, int)
            and node_version >= effective
        )
        if caught_up:
            current["settled_at"] = _now()
            current["settled_by"] = SETTLED_BY_VERSION_CATCHUP
            current["settle_reason"] = f"节点已追平到 v{node_version}（当前生效 v{effective}）"
        result.append(current)
    return result


def settle_regression_restored(
    existing: list[dict[str, Any]],
    *,
    restored_ids: set[str],
) -> list[dict[str, Any]]:
    """清掉**已经回到基线**的回归债（R5）。

    与 `settle_round_cleared` / `settle_version_caught_up` 是同一种模式：清债条件是**可核对的事实**——
    "这个产物现在与基线一致，或者基线已经在新版本上重记"，而不是"谁完成了什么"
    （那会把清债绑死在一次具体运行上：重试、中断、换个人跑就说不清了）。

    刻意**不**把"策略从 `repair` 改成 `record`"当成清债理由：那只是改变了债的分类，
    问题本身没有被解决。
    """
    ids = {str(item) for item in restored_ids}
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        if not current.get("settled_at") and str(current.get("id") or "") in ids:
            current["settled_at"] = _now()
            current["settled_by"] = SETTLED_BY_REPAIR_CHILD
            current["settle_reason"] = "该产物已回到基线内容（或基线已在新版本重记）"
        result.append(current)
    return result


def settle_surface_restored(existing: list[dict[str, Any]], *, restored_ids: set[str]) -> list[dict[str, Any]]:
    """验收面又和冻结值对上 → 清债。

    与 `settle_regression_restored` 是**同一条规则**（依据都是可核对的事实："它又和基线一致了"），
    另立一个名字只是为了让调用点读起来是它自己那件事，而不是"顺手借了回归门禁的函数"。
    """
    return settle_regression_restored(existing, restored_ids=restored_ids)


def _oldest_age_seconds(items: list[dict[str, Any]], now: datetime) -> float | None:
    """最早一条的年龄（秒）。`None` 表示没有可判定的时间戳——**不要**拿 0 冒充"刚记上"。"""
    oldest: datetime | None = None
    for item in items:
        raw = item.get("created_at")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            created = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if oldest is None or created < oldest:
            oldest = created
    if oldest is None:
        return None
    return max(0.0, (now - oldest).total_seconds())


def child_status_debt(
    parent_id: str,
    child_id: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
    depth: int | None = None,
) -> dict[str, Any] | None:
    """子节点处于 `blocked` / `failed` → 父节点的债（§7.3）。

    这是**唯一真相**：`node_meta["blocked_child_reports"]` 由未清的这类债重建，而不是各写一份。

    `payload["report"]` 保留原来的阻塞报告（`retryable` / `category` / `human_required` 等），
    所以派生的列表与原来逐字同形——既有消费方（等待报告、重试判定、看板）不用改。
    """
    text = str(status or "").strip()
    if text == "failed":
        obligation_type = CHILD_FAILED
    elif text == "blocked":
        obligation_type = CHILD_BLOCKED
    else:
        return None
    return make_obligation(
        obligation_type,
        source_node_id=parent_id,
        discriminator=child_id,
        payload={"child_id": child_id, "status": text, "report": dict(report or {})},
        severity=SEVERITY_BLOCKING,
        depth=depth,
    )


def child_status_reports(obligations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从未清的 `child_blocked` / `child_failed` 债派生 `blocked_child_reports` 的内容。"""
    reports: list[dict[str, Any]] = []
    for item in obligations:
        if item.get("settled_at") or item.get("type") not in (CHILD_BLOCKED, CHILD_FAILED):
            continue
        report = (item.get("payload") or {}).get("report")
        if isinstance(report, dict) and report:
            reports.append(dict(report))
    return reports


def settle_child_status_cleared(
    existing: list[dict[str, Any]],
    *,
    still_blocking_child_ids: set[str],
) -> list[dict[str, Any]]:
    """子节点被修复/替代后清债，通路记 `repair_child`（§6.3 的第一条通路）。"""
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        child_id = str((current.get("payload") or {}).get("child_id") or "")
        if (
            current.get("type") in (CHILD_BLOCKED, CHILD_FAILED)
            and not current.get("settled_at")
            and child_id
            and child_id not in still_blocking_child_ids
        ):
            current["settled_at"] = _now()
            current["settled_by"] = SETTLED_BY_REPAIR_CHILD
            current["settle_reason"] = f"{child_id} 已不再是阻塞/失败状态"
        result.append(current)
    return result


def upward_blocking_debt(
    parent_id: str,
    child_id: str,
    *,
    blocking_total: int,
    by_type: dict[str, int] | None = None,
    depth: int | None = None,
) -> dict[str, Any] | None:
    """子节点还有未清阻断事项 → 在父节点上记**一条事实**（§7.2.3）。

    **不是**把子树那一串债复制上来：复制的后果是全树翻倍，看板反而更难看。这里只记
    "我这一支还有未清事项"这一条，明细留在原处（`payload` 只带计数与分类）。

    幂等键是 `(父, 子)`：同一支上无论有多少条债，父节点上永远只有一条。这样
    "整棵树卡在第几层"可以直接按层读，而不是按债务条数读。
    """
    if int(blocking_total or 0) <= 0:
        return None
    return make_obligation(
        CHILD_BLOCKED,
        source_node_id=parent_id,
        discriminator=child_id,
        payload={
            "blocked_node_id": child_id,
            "blocking_total": int(blocking_total),
            "by_type": dict(by_type or {}),
        },
        severity=SEVERITY_BLOCKING,
        depth=depth,
    )


def settle_upward_cleared(
    existing: list[dict[str, Any]],
    *,
    still_blocked_child_ids: set[str],
) -> list[dict[str, Any]]:
    """清掉不满意的上浮事实：那个子节点已经**没有**未清阻断事项了（§7.2.3）。

    又是"按事实清债"：判据是子节点当前的债务状态，不是谁完成了什么、也不是轮次计数。
    """
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        blocked_node_id = str((current.get("payload") or {}).get("blocked_node_id") or "")
        if (
            current.get("type") == CHILD_BLOCKED
            and not current.get("settled_at")
            and blocked_node_id
            and blocked_node_id not in still_blocked_child_ids
        ):
            current["settled_at"] = _now()
            current["settled_by"] = SETTLED_BY_UPWARD_CLEAR
            current["settle_reason"] = f"{blocked_node_id} 已无未清阻断事项"
        result.append(current)
    return result


def summarize(obligations: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """看板用汇总（字段清单见 `docs/spec-parent-child-obligations.md` §9）。

    - `settled_by_human_count` 是"放宽"的**可见计量**——没有它，放宽就是无声的；
    - `oldest_blocking_age_seconds` 是"在干活"与"卡死"的唯一区分：没有它，一条挂了三天
      的阻断债和一条刚记上的看起来一模一样；
    - `revalidate_queue_depth` 是宪法再验证队列长度（A5 的队列读法）。
    """
    unsettled = [item for item in obligations if not item.get("settled_at")]
    by_type: dict[str, int] = {}
    by_depth: dict[str, int] = {}
    for item in unsettled:
        key = str(item.get("type") or "")
        by_type[key] = by_type.get(key, 0) + 1
        depth = item.get("depth")
        if depth is not None:
            depth_key = str(depth)
            by_depth[depth_key] = by_depth.get(depth_key, 0) + 1
    blocking_items = blocking(obligations)
    return {
        "blocking_total": len(blocking_items),
        "advisory_total": len(unsettled) - len(blocking_items),
        "unsettled_total": len(unsettled),
        "by_type": by_type,
        "by_depth": by_depth,
        "oldest_blocking_age_seconds": _oldest_age_seconds(blocking_items, now or datetime.now(timezone.utc)),
        "revalidate_queue_depth": by_type.get(REVALIDATE_REQUIRED, 0),
        "settled_by_human_count": sum(1 for item in obligations if item.get("settled_by") == SETTLED_BY_HUMAN),
        "settled_total": sum(1 for item in obligations if item.get("settled_at")),
        "recurrence_total": sum(int(item.get("recurrence_count") or 0) for item in obligations),
    }


def merge_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """把多个节点的汇总并成树级一份（看板要的是"整棵树卡在哪"，不是"每个节点各卡多少"）。

    只做可加的合并；`oldest_blocking_age_seconds` 取**最大**——树卡了多久由最老的那条决定，
    取平均或取最近的一条都会把"卡死"读成"在干活"。
    """
    merged: dict[str, Any] = {
        "blocking_total": 0,
        "advisory_total": 0,
        "unsettled_total": 0,
        "by_type": {},
        "by_depth": {},
        "oldest_blocking_age_seconds": None,
        "revalidate_queue_depth": 0,
        "settled_by_human_count": 0,
        "settled_total": 0,
        "recurrence_total": 0,
    }
    for summary in summaries:
        for key in ("blocking_total", "advisory_total", "unsettled_total", "revalidate_queue_depth", "settled_by_human_count", "settled_total", "recurrence_total"):
            merged[key] += int(summary.get(key) or 0)
        for bucket in ("by_type", "by_depth"):
            for name, count in (summary.get(bucket) or {}).items():
                merged[bucket][str(name)] = merged[bucket].get(str(name), 0) + int(count)
        oldest = summary.get("oldest_blocking_age_seconds")
        if isinstance(oldest, (int, float)) and (merged["oldest_blocking_age_seconds"] is None or oldest > merged["oldest_blocking_age_seconds"]):
            merged["oldest_blocking_age_seconds"] = oldest
    return merged

