"""约束覆盖核对（`docs/spec-parent-child-obligations.md` §7.1.4）。

**为什么必须靠"逐字身份"而不是文本相似度**：这条判据要判断"子节点集合有没有覆盖父节点的
约束清单"。用相似度（编辑距离、embedding、关键词命中）必然误报——两句话"看起来相关"不等于
同一条约束，而一旦误报，门控就会拦下本来没问题的树，人就会去调阈值，最后这条判据变成噪声。

本模块用的判据是**归一化后的严格相等**：约束要么被逐字继承下去，要么就是丢了。这恰好也是
§7.1.2"约束必须结构继承、不得文本重述"的机器形式——重述就是侵蚀的第一步，
所以"重述算没覆盖"不是严苛，而是**把漂移判成漂移**。

零误报意味着它可能误**漏**（把同义改写算成缺口），这是刻意选的偏移方向：漏报会让人看到
"有缺口"而去补，误报会让人学会忽略它。
"""

from __future__ import annotations

import re
from typing import Any

from . import obligations

_WHITESPACE = re.compile(r"\s+")


def normalize_constraint(text: Any) -> str:
    """归一化：折叠空白、去首尾、统一大小写。

    只做这些——**不做**标点归一、同义词替换、词干化。每多一步"聪明"的处理，就多一分把
    两条不同约束判成同一条的机会，而那种误报的代价是门控失去可信度。
    """
    if not isinstance(text, str):
        return ""
    return _WHITESPACE.sub(" ", text).strip().casefold()


def delegable_constraints(constitution_data: Any) -> list[str]:
    """必须被继承下去的约束清单：宪法的 `global_acceptance`。

    **为什么只取这一项**：父节点自己的 `acceptance` 里往往混着"只有父节点才能判"的整合级条目
    （例如"组合结果必须可玩"）——那些本来就不该要求子节点逐字重复。要求它们也覆盖，
    得到的是噪声而不是覆盖率。`global_acceptance` 是项目级底线，它对每一层都成立，
    正是 §7.1.2 说的"必须结构继承"的那一类。
    """
    if not isinstance(constitution_data, dict):
        return []
    items = constitution_data.get("global_acceptance")
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if isinstance(item, str) and str(item).strip()]


def inherit_down(parent_acceptance: list[Any], parent_inherited: list[Any]) -> list[str]:
    """父节点的 must/never 与它已继承的链，一起下沉给子节点（§7.1.3）。

    **逐层累积**是刻意的：MasDrift 的核心发现是约束随委托链逐跳被侵蚀，只看直接父节点的话，
    第 3 跳以后没人能说出第 1 跳原本要求什么。所以子节点拿到的是"祖先链 + 直接父节点"的并集。

    去重按归一化后比较（与覆盖核对同一把尺子），顺序保持：祖先在前、直接父节点在后——
    越靠后越具体，读起来也符合"约束逐步收紧"的直觉。

    **不写进子节点的 `acceptance`**：那是验收判据，会直接进 worker/verifier 的判定。
    父节点的整合级条目（例如"组合结果必须可玩"）压到叶子身上会让叶子验收必然失败——
    下沉的是**约束**，不是把父节点的判分标准摊给每个子节点。约束通过 prompt 的独立区块
    与 `node_meta` 传递。
    """
    result: list[str] = []
    seen: set[str] = set()
    for item in [*parent_inherited, *parent_acceptance]:
        if not isinstance(item, str) or not item.strip():
            continue
        key = normalize_constraint(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def coverage_gap(parent_constraints: list[str], child_acceptance_lists: list[list[Any]]) -> list[str]:
    """返回**没有被任何子节点逐字继承**的约束（保持父清单顺序）。

    调用方负责保证 `child_acceptance_lists` 是"子节点**集合**"的 acceptance——§7.1.4 说的是
    集合覆盖，不是每个子节点各自覆盖全部。
    """
    covered = {
        normalize_constraint(item)
        for acceptance in child_acceptance_lists
        for item in (acceptance or [])
    }
    covered.discard("")
    return [constraint for constraint in parent_constraints if normalize_constraint(constraint) not in covered]


def coverage_gap_debt(
    parent_id: str,
    missing: list[str],
    *,
    depth: int | None = None,
    blocks: bool = False,
) -> dict[str, Any] | None:
    """缺口 → 债务，记在**父节点**上（它是那个"不能完成"的一方）。

    幂等键用**父节点 id**、不带缺口内容：缺口清单会随子节点变化而变，把它写进 id 会每轮生出
    新债。缺口明细放 `payload`，配 `recurrence_count` 足以看出"改了没有"。

    **严重度由规则显式声明**（§6.2），默认 `advisory`，要阻断需显式 `blocks=True`。
    这个是量出来的，不是猜的：把它默认设成 blocking 之后，全量回归当场 14 条既有契约变红——
    **LLM 规划器会改写措辞**，于是每一棵树的父节点都永远拿不到 `done`。默认阻断等于把
    "约束会被重述"这个现实判成每一棵树都失败；而记录 + 上浮能让人**看见**它，
    这正是这个设计一贯的方向（与集成冲突档位的取舍同源）。
    """
    if not missing:
        return None
    return obligations.make_obligation(
        obligations.CONSTRAINT_COVERAGE_GAP,
        source_node_id=parent_id,
        discriminator="global_acceptance",
        payload={"missing": list(missing), "missing_count": len(missing), "blocks": bool(blocks)},
        severity=obligations.SEVERITY_BLOCKING if blocks else obligations.SEVERITY_ADVISORY,
        depth=depth,
    )


def settle_coverage_cleared(
    existing: list[dict[str, Any]],
    *,
    still_missing: list[str],
) -> list[dict[str, Any]]:
    """按事实清债：缺口被补上（子节点集合已逐字继承）即清。"""
    if still_missing:
        return list(existing)
    result: list[dict[str, Any]] = []
    for item in existing:
        current = dict(item)
        if current.get("type") == obligations.CONSTRAINT_COVERAGE_GAP and not current.get("settled_at"):
            current["settled_at"] = obligations._now()
            current["settled_by"] = obligations.SETTLED_BY_COVERAGE_CLEARED
            current["settle_reason"] = "子节点集合已逐字继承该约束"
        result.append(current)
    return result
