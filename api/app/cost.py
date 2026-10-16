"""**花费归因与成本预测**（P3-3 / P3-2）。

## 要回答的两个问题

* **P3-3**："钱花在哪一层？" —— 按节点、按角色（`run_records.kind`）、按模型拆开。
* **P3-2**："跑完还要多少？" —— 用已完成节点的实际花费外推剩余工作量。

## 四条设计取舍（这一层最容易编数据，所以每条都写明失效方向）

1. **读不到就说读不到，不填占位桶。**
   取不到模型名的运行单独计数（`runs_unknown_model`），**不**伪造一个
   `model="unknown"` 的桶 —— 那个桶会让「47% 的花费属于 unknown」看起来像
   统计结论，而真相是「这些运行没记录模型」。

2. **币种分开，绝不跨币种相加。** 与 `usage.summarize_usages` 同一条规矩。
   混合币种时给出多个桶，不做汇率假设 —— 那是本模块不该知道的事。

3. **预测必须带区间与依据。** 外推只用「已完成节点的实际花费」，不去猜模型
   单价、也不假设「后面的节点和前面一样难」。样本不足时**明确拒绝给数**
   （`available: false` + 原因），而不是给一个看起来很确定的单一数字 ——
   一个错的预测比没有预测更容易让人做出错误决定（比如放心去睡觉）。

4. **区分「花了多少」与「还剩多少」。** 用量只统计**已发生**的运行；
   剩余工作量按节点数算。区间下界只覆盖「剩余节点与已完成节点一样贵」，
   上界额外覆盖历史重试率与难度上升。

## 与预算硬闸（P3-1）的关系

本模块**只报告**，不做任何拦截。硬闸在 `budget.py` 与 orchestrator 的发车点，
那里按「启动次数」计数。两者口径不同且**有意保持不同**：
闸门管「还允许发几次车」，本模块管「这些车一共花了多少钱」。
把它们合成一个数会同时毁掉两边的可解释性。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .usage import normalize_usage

# 树的终态：这些状态不会再产生新的花费（`blocked` 是终态，见领域文档）。
TERMINAL_STATUSES = frozenset({"done", "failed", "canceled", "blocked"})
# 已经花过钱、已经算「消耗掉一个工作单位」的状态 —— 外推的单价样本就是它们。
SETTLED_STATUSES = frozenset({"done", "failed", "canceled"})


def _amount_of(entry: Mapping[str, Any]) -> tuple[float, str | None]:
    """从规范化后的用量里取 (金额, 币种)。没有金额就是 (0.0, None)。"""
    cost = entry.get("cost")
    if not isinstance(cost, Mapping):
        return 0.0, None
    amount = cost.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return 0.0, None
    currency = cost.get("currency")
    code = currency.strip().upper() if isinstance(currency, str) and currency.strip() else None
    return float(amount), code


def _token_total(entry: Mapping[str, Any]) -> int:
    value = entry.get("total_tokens")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    parts = (entry.get("input_tokens"), entry.get("output_tokens"))
    return sum(part for part in parts if isinstance(part, int) and not isinstance(part, bool))


class _Bucket:
    """一个分组（某模型 / 某角色 / 某节点）的累计。"""

    __slots__ = ("key", "runs", "tokens", "cost_by_currency", "title", "depth")

    def __init__(self, key: str) -> None:
        self.key = key
        self.runs = 0
        self.tokens = 0
        self.cost_by_currency: dict[str, float] = {}
        # 节点桶才有；模型/角色桶留空。归因表要能显示「哪个子任务最贵」，
        # 只给 id 的话用户还得回树里查。
        self.title = ""
        self.depth: int | None = None

    def add(self, entry: Mapping[str, Any]) -> None:
        self.runs += 1
        self.tokens += _token_total(entry)
        amount, code = _amount_of(entry)
        if amount:
            bucket = code or "UNKNOWN"
            self.cost_by_currency[bucket] = self.cost_by_currency.get(bucket, 0.0) + amount

    def dump(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "key": self.key,
            "runs": self.runs,
            "total_tokens": self.tokens,
            # 没有金额时是 None，不是 {} 或 0 —— 「没记账」与「记了 0 元」是两件事。
            "cost_by_currency": _freeze(self.cost_by_currency),
        }
        if self.title or self.depth is not None:
            item["title"] = self.title
            item["depth"] = self.depth
        return item


def _freeze(values: Mapping[str, float]) -> dict[str, float] | None:
    return (
        {code: round(value, 6) for code, value in sorted(values.items())} if values else None
    )


class Attribution:
    """把一串 `(节点, 角色, 模型, usage)` 记录归因成三个维度的桶。

    **纯计算**：不读库、不碰文件。这样「归因对不对」可以被单元测试直接钉住，
    而不用起数据库、造树、跑 mock runner。
    """

    def __init__(self) -> None:
        self._by_kind: dict[str, _Bucket] = {}
        self._by_model: dict[str, _Bucket] = {}
        self._by_node: dict[str, _Bucket] = {}
        self.runs = 0
        self.runs_with_usage = 0
        self.runs_unknown_model = 0
        # 有运行但一条用量都识别不出来 —— 这通常意味着「供应商换了字段名」，
        # 而不是「没花钱」。必须单独报，否则归因表看起来像「这棵树没花过钱」。
        self.runs_without_usage = 0
        self.total_tokens = 0
        self.cost_by_currency: dict[str, float] = {}

    def add(
        self,
        *,
        node_id: str,
        kind: str,
        usage: Any,
        node_title: str = "",
        node_depth: int | None = None,
    ) -> None:
        self.runs += 1
        entry = normalize_usage(usage)
        if entry is None:
            self.runs_without_usage += 1
            return
        self.runs_with_usage += 1

        model = entry.get("model")
        if isinstance(model, str) and model:
            self._by_model.setdefault(model, _Bucket(model)).add(entry)
        else:
            self.runs_unknown_model += 1

        kind_key = str(kind or "unknown")
        self._by_kind.setdefault(kind_key, _Bucket(kind_key)).add(entry)

        node_key = str(node_id)
        node_bucket = self._by_node.setdefault(node_key, _Bucket(node_key))
        node_bucket.add(entry)
        node_bucket.title = node_title or node_bucket.title
        if node_depth is not None:
            node_bucket.depth = node_depth

        self.total_tokens += _token_total(entry)
        amount, code = _amount_of(entry)
        if amount:
            key = code or "UNKNOWN"
            self.cost_by_currency[key] = self.cost_by_currency.get(key, 0.0) + amount

    def dump(self) -> dict[str, Any]:
        by_node = []
        for key, bucket in self._by_node.items():
            item = bucket.dump()
            item["node_id"] = key
            by_node.append(item)
        # 最贵的排前面：归因表的第一行就该回答「钱主要花在哪」。
        by_node.sort(key=lambda item: (-item["total_tokens"], item["node_id"]))

        return {
            "runs": self.runs,
            "runs_with_usage": self.runs_with_usage,
            "runs_without_usage": self.runs_without_usage,
            "runs_unknown_model": self.runs_unknown_model,
            "total_tokens": self.total_tokens,
            "cost_by_currency": _freeze(self.cost_by_currency),
            "by_kind": sorted(
                (bucket.dump() for bucket in self._by_kind.values()), key=lambda i: i["key"]
            ),
            "by_model": sorted(
                (bucket.dump() for bucket in self._by_model.values()), key=lambda i: i["key"]
            ),
            "by_node": by_node,
        }


def shares(cost_by_currency: Mapping[str, float] | None) -> dict[str, float]:
    """把各桶换成一语种内的占比（百分比）。

    **只在同一币种内算**：跨币种的占比没有意义（不同币种不是同一个分母）。
    """
    if not cost_by_currency:
        return {}
    total = sum(value for value in cost_by_currency.values() if isinstance(value, (int, float)))
    if total <= 0:
        return {}
    return {
        code: round(100.0 * float(value) / total, 2)
        for code, value in sorted(cost_by_currency.items())
        if isinstance(value, (int, float))
    }


def project_cost(
    *,
    settled_costs: Iterable[float],
    settled_nodes: int,
    remaining_nodes: int,
    retry_factor: float = 1.0,
    currency: str | None = None,
) -> dict[str, Any]:
    """用**已完成节点**的实际花费外推剩余花费（P3-2）。

    ## 为什么给区间而不是一个数

    单一数字会被读成「确定要花这么多」，而这个外推建立在两个不可验证的假设上：
    剩余节点与已完成节点**一样难**、**不做额外重试**。所以给三档：

    * `low`  = 平均单价 × 剩余节点数（最乐观：后面更省、不重试）
    * `mid`  = low × `retry_factor`（与历史重试率一致时的期望）
    * `high` = mid × 2（后面更难或重试更多）

    `retry_factor` 由调用方按「已观测重试 / 节点数」给出（见 `retry_factor_for`）。

    ## 两个"拒绝"必须分开说

    `settled_nodes` 与 `settled_costs` 是**两个不同的输入**，因为两种失败的原因
    完全不同、用户要做的事也完全不同：

    * `settled_nodes == 0` → "还没有样本"（去跑几个节点）
    * `settled_nodes > 0` 但没有任何金额 → "供应商没报金额"（去查计价/网关配置）

    合并成一条消息，用户就会去跑更多节点，而问题其实在计价链路上。

    ## 口径上的诚实点

    * 单位是「节点」而不是「小时」：本工具没有可靠的耗时模型，用时间外推会更假。
    * 金额只按**单一币种**外推。混币种时调用方应分别调用 —— 本函数不做转换。
    """
    costs = [float(value) for value in settled_costs if isinstance(value, (int, float))]
    if settled_nodes <= 0:
        return {
            "available": False,
            "reason": "还没有已结算的节点，无法外推（至少要有一个已完成节点当单价样本）。",
            "currency": currency,
        }
    if not costs:
        return {
            "available": False,
            "reason": "已结算的节点没有可用的计价数据（供应商没报金额）—— 只能报 token，不能报钱。",
            "currency": currency,
        }
    if remaining_nodes <= 0:
        return {
            "available": True,
            "reason": "",
            "currency": currency,
            "unit_cost": round(sum(costs) / len(costs), 6),
            "settled_cost": round(sum(costs), 6),
            "settled_nodes": settled_nodes,
            "remaining_nodes": 0,
            "low": 0.0,
            "mid": 0.0,
            "high": 0.0,
            "sample_nodes": len(costs),
            "retry_factor": 1.0,
            "note": "没有剩余节点，不需要再花。",
        }

    unit = sum(costs) / len(costs)
    factor = retry_factor if retry_factor and retry_factor > 0 else 1.0
    low = unit * remaining_nodes
    mid = low * factor
    high = mid * 2
    return {
        "available": True,
        "reason": "",
        "currency": currency,
        "unit_cost": round(unit, 6),
        "settled_cost": round(sum(costs), 6),
        "settled_nodes": settled_nodes,
        "remaining_nodes": remaining_nodes,
        "low": round(low, 6),
        "mid": round(mid, 6),
        "high": round(high, 6),
        # 样本量必须一起报：3 个节点算出的「平均单价」与 300 个的说服力完全不同。
        "sample_nodes": len(costs),
        "retry_factor": round(factor, 4),
        "note": (
            "按「已完成节点的平均花费 × 剩余节点数」外推：下界假设后面更省，"
            "上界假设后面更难或重试更多。**不含**模型涨价、需求变更与人工返工。"
        ),
    }


def retry_factor_for(*, retry_count: int, node_count: int) -> float:
    """历史重试率 → 预测乘数。

    为什么用重试率而不是「平均重试次数」：重试的代价最终表现为**重复付费**，
    率是可直接乘的。没有数据时返回 1.0（不再额外放大）而不是拒绝 ——
    重试率未知不该让整个预测不可用，但它会在 `retry_factor` 里如实显示成 1.0。
    """
    if node_count <= 0:
        return 1.0
    ratio = retry_count / node_count
    # 上限 3：重试率再高，用 3 倍去吓人也不比「看事实」更有用。
    return round(min(3.0, 1.0 + max(0.0, ratio)), 4)


# ---------- 读库聚合（把上面的纯计算接到真实数据上）----------


def _nodes_of_tree(db: Any, root_id: str) -> list[tuple[str, str, int, str]]:
    """返回 `[(id, title, depth, status)]`。**走边表而不是假设有 root_id 列。**

    `task_nodes` 只有 `parent_id`，没有 `root_id`；要拿到一棵子树的全部节点必须
    从根往下走。这一步刻意在这里做一次 BFS，而不是让调用方各写一遍 ——
    "哪些节点算这棵树"如果有两份实现，归因与门禁就会对不上。
    """
    from sqlalchemy import select

    from .models import TaskNode

    rows = db.execute(select(TaskNode.id, TaskNode.parent_id, TaskNode.title, TaskNode.depth, TaskNode.status)).all()
    children: dict[str | None, list[Any]] = {}
    by_id: dict[str, Any] = {}
    for row in rows:
        by_id[str(row.id)] = row
        children.setdefault(row.parent_id, []).append(row)

    out: list[tuple[str, str, int, str]] = []
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in seen or current not in by_id:
            continue
        seen.add(current)
        row = by_id[current]
        out.append(
            (
                str(row.id),
                str(row.title or ""),
                int(row.depth or 0),
                str(row.status or ""),
            )
        )
        for child in children.get(current, []):
            stack.append(str(child.id))
    return out


def cost_report(db: Any, root_id: str) -> dict[str, Any]:
    """一棵树的**花费归因 + 成本预测**（P3-3 / P3-2）。

    只读。运行时把 `run_records.usage_json` 逐条过 `usage.normalize_usage`，
    因此改进规范化逻辑会对历史数据一并生效 —— 换供应商时这一点很关键。

    实现上**只扫一遍 run_records**：先按节点累计，再从中取单价样本。
    逐节点再扫一遍（那是最容易写出来的版本）在千节点树上就是 O(节点 × 运行)，
    而 P3-6 明确要求这个规模下界面仍然可用。
    """
    import json

    from sqlalchemy import select

    from .models import RunRecord

    nodes = _nodes_of_tree(db, root_id)
    node_index = {node_id: (title, depth, status) for node_id, title, depth, status in nodes}
    if not node_index:
        return {
            "root_id": root_id,
            "attribution": Attribution().dump(),
            "projection": {
                "available": False,
                "reason": "这棵树还没有任何节点。",
                "currency": None,
            },
            "node_counts": {"total": 0, "settled": 0, "remaining": 0},
            "note": "归因与预测都只读，且只统计已发生的运行。",
        }

    attribution = Attribution()
    cost_by_node: dict[str, float] = {}
    currency_by_node: dict[str, str | None] = {}

    rows = db.execute(
        select(RunRecord.task_id, RunRecord.kind, RunRecord.usage_json).where(
            RunRecord.task_id.in_(list(node_index))
        )
    ).all()
    for row in rows:
        task_id = str(row.task_id)
        title, depth, _status = node_index.get(task_id, ("", None, ""))
        payload: Any = None
        if row.usage_json:
            try:
                payload = json.loads(row.usage_json)
            except (TypeError, ValueError):
                payload = None
        attribution.add(
            node_id=task_id,
            kind=str(row.kind or ""),
            usage=payload,
            node_title=title,
            node_depth=depth,
        )
        entry = normalize_usage(payload)
        if entry is None:
            continue
        amount, code = _amount_of(entry)
        if not amount:
            continue
        cost_by_node[task_id] = cost_by_node.get(task_id, 0.0) + amount
        if code:
            currency_by_node[task_id] = code

    # 单价样本只取**已结算**节点（done/failed/canceled）。
    # `blocked` 是终态但**不是**已结算：它花过钱却没有产出，算进去会把平均单价拉低，
    # 而偏乐观的预测是最坏的方向。
    settled_costs: list[float] = []
    settled_currency: str | None = None
    currency_conflict = False
    settled_state_count = 0
    for node_id, _title, _depth, status in nodes:
        if status not in SETTLED_STATUSES:
            continue
        # **状态上**已结算就计数，与"有没有金额"无关 —— 两种情况要分开报
        # （见 `project_cost` 的注释：一种是去多跑几个节点，一种是去查计价链路）。
        settled_state_count += 1
        amount = cost_by_node.get(node_id)
        if not amount:
            continue
        code = currency_by_node.get(node_id)
        if settled_currency is None:
            settled_currency = code
        elif code and settled_currency != code:
            # 混币种：**不合并**。合并出来的单价在任何币种下都是错的。
            currency_conflict = True
        settled_costs.append(amount)

    remaining = sum(1 for _id, _t, _d, status in nodes if status not in TERMINAL_STATUSES)
    retries = sum(1 for _id, _t, _d, status in nodes if status in {"failed", "canceled"})
    factor = retry_factor_for(retry_count=retries, node_count=max(1, len(nodes)))

    projection: dict[str, Any]
    if currency_conflict:
        projection = {
            "available": False,
            "reason": (
                "这棵树的状态目录里出现了多种币种的金额，无法给出单一币种的预测"
                "（不做汇率假设）。请分别看各币种的归因。"
            ),
            "currency": None,
        }
    else:
        projection = project_cost(
            settled_costs=settled_costs,
            settled_nodes=settled_state_count,
            remaining_nodes=remaining,
            retry_factor=factor,
            currency=settled_currency,
        )

    return {
        "root_id": root_id,
        "attribution": attribution.dump(),
        "projection": projection,
        "node_counts": {
            "total": len(nodes),
            # 已结算 = 状态上的已结算（含没报金额的）；单价样本数在 projection 里。
            "settled": settled_state_count,
            "priced_samples": len(settled_costs),
            "remaining": remaining,
        },
        "note": (
            "归因与预测都**只读**，只统计已发生的运行。硬闸（能不能再发车）在"
            " `/api/budget-guard`，两者口径不同且有意保持不同。"
        ),
    }
