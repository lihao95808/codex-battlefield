"""**请求指标与关联 ID**（P5-1 / P5-5 共用的那一层）。

## 为什么两件事放一起做

* **P5-1** 要回答"最慢的端点是谁、有没有退化"；
* **P5-5** 要回答"一个请求从界面到子进程能不能串起来"。

两者需要的底层是同一件东西：**每次请求都要被计量，且带一个能一路带下去的 ID**。
分两套实现的结果会是两个各记一半的地方，而排查问题时哪一套都不完整。

## 四条设计规矩

1. **有界内存**。每个路由保留最近 `SAMPLE_LIMIT` 个样本。不设上限的指标记录
   本身就是内存泄漏 —— 而它长得像"一个很正常的监控功能"。
2. **分位数要说明是"最近窗口"的**。这里给的是**滑动窗口分位数**，不是全历史。
   把它当全历史读，会在树跑了几小时之后得出完全错误的结论。
3. **失败也算进分位数**。500 与成功请求花掉的时间一样是真实的负载；
   把它们排除出去，指标会在系统最不稳定的时候看起来最漂亮。
4. **ID 由服务端给，且回写到响应头**。客户端日志、界面报错、服务端日志三者
   靠它串起来 —— 用户截图里的那个 ID 必须能在日志里搜到。

## 与 `test_scale.py` 的关系

`test_scale.py` 钉的是**特定端点在大数据量下**的绝对耗时（真库、真 ORM）。
本模块钉的是**每个端点自己的历史基线**（`metrics.baseline`）。两者不重复：
前者抓"这个查询写成了 O(n²)"，后者抓"这个端点比上周慢了 5 倍"。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

# 每个路由保留的样本数。1000 个 float ≈ 8KB/路由；70 个路由约 0.6MB，可接受。
SAMPLE_LIMIT = 1000
# 分位数口径。写在这里而不是散在调用处 —— 两处不一致会让数字对不上。
QUANTILES = (0.5, 0.95, 0.99)

# 关联 ID 的响应头名。**小写**：Starlette 会按原样发送，而 curl/浏览器都不区分大小写。
REQUEST_ID_HEADER = "x-request-id"
# 客户端也可以带一个进来（网关/前端已有 ID 时能接上）。
REQUEST_ID_INBOUND = "x-request-id"


@dataclass
class RouteStats:
    """一个路由的滑动窗口统计。"""

    key: str
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=SAMPLE_LIMIT))
    count: int = 0
    errors: int = 0
    last_status: int = 0
    # 累计耗时只用于算总量（"这个端点一共花了多少"），不参与分位数。
    total_ms: float = 0.0

    def record(self, duration_ms: float, status: int) -> None:
        self.samples.append(duration_ms)
        self.count += 1
        self.total_ms += duration_ms
        self.last_status = status
        if status >= 400:
            self.errors += 1


class Metrics:
    """进程内的请求指标。

    **刻意是进程内的**：要跨进程汇总就得引入时序库或共享存储，那是另一个量级的
    依赖。本工具的每个 API 进程服务一台机器，`GET /api/metrics` 读自己那份就够，
    而"多进程汇总"这件事等到真有多进程需求时再说（届时也不需要改这里的接口）。
    """

    def __init__(self) -> None:
        self._routes: dict[str, RouteStats] = {}
        # 基线：`{路由键: p95 毫秒}`。由 `/api/metrics/baseline` 写入，
        # 用于"不再退化"的比较。**默认空** —— 没有基线时不做退化判断，
        # 而不是拿一个假想的阈值去卡。
        self.baseline: dict[str, float] = {}
        self.started_at = perf_counter()

    def record(self, key: str, duration_ms: float, status: int) -> None:
        stats = self._routes.get(key)
        if stats is None:
            stats = RouteStats(key=key)
            self._routes[key] = stats
        stats.record(duration_ms, status)

    def route(self, key: str) -> RouteStats | None:
        return self._routes.get(key)

    def keys(self) -> list[str]:
        return sorted(self._routes)

    def snapshot(self, *, limit: int = 0) -> list[dict[str, Any]]:
        """按 `p95` 从慢到快排（`limit=0` 表示全部）。

        为什么按 p95 而不是平均：平均会被大量快请求稀释，而用户感受到的是最慢那几次。
        """
        items = [percentiles(stats) for stats in self._routes.values()]
        items.sort(key=lambda item: (-(item["p95_ms"] or 0.0), item["key"]))
        return items[:limit] if limit > 0 else items

    def payload(self, *, limit: int = 0) -> dict[str, Any]:
        return {
            "routes": self.snapshot(limit=limit),
            "route_count": len(self._routes),
            "sample_limit": SAMPLE_LIMIT,
            "quantiles": list(QUANTILES),
            "baseline": dict(sorted(self.baseline.items())),
            "note": (
                "分位数是**最近窗口**的（每个路由最多 %d 个样本），不是全历史。"
                "把它当全历史读会在长跑之后得出错误的结论。" % SAMPLE_LIMIT
            ),
        }

    def reset(self) -> None:
        """清空样本。**不动基线** —— 清样本是"重新开始量"，不是"忘掉标准"。"""
        self._routes.clear()


def percentiles(stats: RouteStats) -> dict[str, Any]:
    """一个路由的统计。样本为空时所有分位数是 `None`，**不是 0**。

    0 会被读成"快得看不见"，而真相是"还没量到"。
    """
    values = sorted(stats.samples)
    out: dict[str, Any] = {
        "key": stats.key,
        "count": stats.count,
        "errors": stats.errors,
        "error_rate": round(stats.errors / stats.count, 4) if stats.count else None,
        "last_status": stats.last_status,
        "samples": len(values),
        "total_ms": round(stats.total_ms, 3),
        "mean_ms": round(stats.total_ms / stats.count, 3) if stats.count else None,
        "max_ms": round(values[-1], 3) if values else None,
    }
    for quantile in QUANTILES:
        out[f"p{int(quantile * 100)}_ms"] = _quantile(values, quantile)
    return out


def _quantile(values: list[float], quantile: float) -> float | None:
    """最近样本的线性插值分位数。样本为空给 `None`。"""
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    position = quantile * (len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    value = values[lower] * (1 - weight) + values[upper] * weight
    return round(value, 3)


def new_request_id() -> str:
    """请求 ID。用短随机串：它的作用是**在日志里能搜到**，不是防碰撞。"""
    return uuid4().hex[:16]


def route_key(method: str, path: str, route_path: str | None = None) -> str:
    """指标里用的路由键。

    **优先用路由模板**（`/api/tasks/{task_id}`）而不是真实路径：按真实路径分组会让
    每个 task_id 各占一行，于是指标表里几千行里全是 `/api/tasks/abc123`，
    而那正是最需要看清"哪个端点慢"的时候。

    拿不到模板时用真实路径（并保留，便于排查）。
    """
    template = (route_path or "").strip()
    key = template or path
    return f"{method.upper()} {key}"


def regression(
    snapshot: list[dict[str, Any]], baseline: dict[str, float], *, tolerance: float = 1.5
) -> list[dict[str, Any]]:
    """比基线慢超过 `tolerance` 倍的路由。

    `tolerance=1.5` 是**容忍噪声**：p95 在共享机器上本来就会跳。判据要抓的是
    "慢了 5 倍"，而不是"抖了 20%"。抓太紧的结果是所有人学会忽略它。
    """
    out: list[dict[str, Any]] = []
    for item in snapshot:
        target = baseline.get(item["key"])
        current = item.get("p95_ms")
        if target is None or current is None or target <= 0:
            continue
        ratio = current / target
        if ratio > tolerance:
            out.append(
                {
                    "key": item["key"],
                    "baseline_p95_ms": target,
                    "current_p95_ms": current,
                    "ratio": round(ratio, 2),
                    "samples": item["samples"],
                }
            )
    out.sort(key=lambda item: -item["ratio"])
    return out


class Recorder:
    """一次请求的计时器。**用 `with` 或 `finish()` 收口**，别漏。"""

    def __init__(self, key: str) -> None:
        self.key = key
        self.started = perf_counter()
        self.duration_ms: float | None = None

    def finish(self, metrics: Metrics, status: int) -> float:
        if self.duration_ms is None:
            self.duration_ms = (perf_counter() - self.started) * 1000
        metrics.record(self.key, self.duration_ms, status)
        return self.duration_ms


# 进程内单例。与编排层一致 —— 指标是"这个进程看到了什么"。
metrics = Metrics()


def load_baseline(path: Any) -> dict[str, float]:
    """从 JSON 文件加载 p95 基线，写进注册表。

    **基线放在仓库里而不是代码里**：它是一份**测量结果**，需要带来源（怎么量的、
    什么时候量的、适用于什么条件）。写在代码常量里的话，那几个数字会慢慢变成
    "谁也不知道哪来的阈值"，然后有人为了让测试变绿把它调大。

    文件不存在/坏了时**不抛错**：没有基线只是"不做退化判断"，不该让 API 起不来。
    但那也意味着"不再退化"这条判据在那个时候是**静默失效**的 —— 所以判据里专门
    有一条查"文件真的被加载了"。
    """
    import json
    from pathlib import Path

    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    budgets = doc.get("budgets_ms")
    if not isinstance(budgets, dict):
        return {}
    loaded: dict[str, float] = {}
    for key, value in budgets.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            loaded[str(key)] = round(number, 3)
    metrics.baseline.update(loaded)
    return loaded


def baseline_file() -> "object":
    """基线文件位置：`<repo>/bench/endpoint_baseline.json`。

    与 `bench/` 下其它能力基准放一起 —— 它们都是"量出来的、带来源的"数字。
    """
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "bench" / "endpoint_baseline.json"


# 便于测试与脚本重置。
#
# ⚠️ 两个函数**职责不同**，混用会让"基线已被加载"这条判据变成空转：
#
# * `_reset_for_tests()` 把一切清空（含基线）—— 适合"我要从零构造一个注册表"的用例；
# * `restore_baseline()` 把基线**恢复成启动时从文件加载的那一份** —— 适合
#   "清样本但别动文件基线"的用例。
#
# 第一版只有一个 `_reset_for_tests`，于是所有 fixture 都把文件基线清空了，
# 而"基线真的被加载了"那条判据因此永远看不到它 —— 判据还在、永远红或永远跳过。
# 实测被全量跑抓到（单跑时 import 顺序不同，看起来是绿的）。
_BASELINE_AT_STARTUP: dict[str, float] = {}


def _remember_startup_baseline() -> dict[str, float]:
    """在加载完基线之后调一次，记下"启动时的基线"供测试恢复。"""
    _BASELINE_AT_STARTUP.clear()
    _BASELINE_AT_STARTUP.update(metrics.baseline)
    return dict(_BASELINE_AT_STARTUP)


def restore_baseline() -> None:
    """把基线恢复成启动时那一份（**不清样本**）。"""
    metrics.baseline.clear()
    metrics.baseline.update(_BASELINE_AT_STARTUP)


def _reset_for_tests() -> None:
    """清样本 + 清基线 + 恢复启动基线。

    顺序有讲究：先清空、再恢复启动快照 —— 于是它的净效果是
    "样本清零，基线回到启动状态"，这正是每个用例需要的起点。
    """
    metrics.reset()
    restore_baseline()


__all__ = [
    "Metrics",
    "RouteStats",
    "Recorder",
    "SAMPLE_LIMIT",
    "QUANTILES",
    "REQUEST_ID_HEADER",
    "new_request_id",
    "route_key",
    "percentiles",
    "regression",
    "metrics",
    "load_baseline",
    "baseline_file",
    "restore_baseline",
]
