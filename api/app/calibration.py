"""自评校准：把"它自称有 300% 把握"与"后来真的通过了没有"对上（verification-independence §5）。

**只采集，不上阈值。** 自动动作（低于某个比率就强制拆分）需要"多低才算过于自信"这个**政策输入**，
而政策不该由实现方悄悄定下来——那正是这个仓库一直在防的事：把判断悄悄塞进代码，然后没人知道
门禁为什么时而紧时而松。

口径（都是**已有列**的聚合，不新增字段）：
- `claims`：真正执行过自己那份判断的节点数（`leaf_attempts > 0`）；
- `misses`：判断落空的节点数（转成了父节点，或验证失败过）；
- `overconfidence_ratio`：`misses / claims`。**没有样本时是 `None`，不是 0**——"没样本"与"零失误"
  是两件事，填 0 会让人读成"这个模型从不过度自信"。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from .models import TaskNode


def _missed(task: TaskNode) -> bool:
    return bool(task.converted_from_leaf) or int(task.verification_failures or 0) > 0


def summarize(tasks: Sequence[TaskNode]) -> dict[str, Any]:
    """整棵树的校准读数。分桶按**深度**：浅层的过度自信与深层的过度自信不是一回事。"""
    claims = 0
    misses = 0
    by_depth_claims: Counter[str] = Counter()
    by_depth_misses: Counter[str] = Counter()
    for task in tasks:
        if int(task.leaf_attempts or 0) <= 0:
            continue
        claims += 1
        by_depth_claims[str(task.depth)] += 1
        if _missed(task):
            misses += 1
            by_depth_misses[str(task.depth)] += 1
    return {
        "claims": claims,
        "misses": misses,
        # 没有样本 → None（不许填 0）
        "overconfidence_ratio": round(misses / claims, 4) if claims else None,
        "by_depth": {
            depth: {
                "claims": count,
                "misses": by_depth_misses.get(depth, 0),
            }
            for depth, count in sorted(by_depth_claims.items(), key=lambda item: int(item[0]))
        },
        # 照实写清这条读数**不**触发什么
        "policy": "只读：不据此拆分、不降级、不加债（阈值属政策，见 spec §6 VI-1）",
    }
