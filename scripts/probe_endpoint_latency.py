"""一次性探针：找出**真实负载下最慢的 5 个端点**，并给出基线数字。

跑完就删（不是判据文件）。它回答的是 P5-1 的前半句"最慢 5 个端点是谁" ——
而这件事必须**量**，不能猜：猜出来的"慢端点"会让人去优化一个其实很快的地方。

用法：先起 API（`api/.venv/Scripts/python.exe -m uvicorn app.main:app --port 8765`），
再跑本脚本。它给自己的每个请求记耗时，按 p95 排序打印。
"""
from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
REPEATS = 15
WARMUP = 2

# 要量的端点。参数用**明显不存在**的 id：404 也要花 SQL 的时间，
# 而且真实 id 依赖库里的数据，量出来不可复现。
ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("health", "/api/health"),
    ("state", "/api/state"),
    ("tree", "/api/tree"),
    ("tree-full", "/api/tree?compact_done=false"),
    ("projects", "/api/projects"),
    ("state-overview", "/api/state/overview"),
    ("cross-project", "/api/overview/cross-project"),
    ("cost", "/api/cost"),
    ("metrics", "/api/metrics"),
    ("metrics-top5", "/api/metrics?limit=5"),
    ("attention", "/api/attention"),
    ("tuning", "/api/watchdog/tuning"),
    ("watchdog-state", "/api/watchdog/state"),
    ("obligation-index", "/api/obligation-index"),
    ("machine-safety", "/api/machine-safety"),
    ("deliveries", "/api/deliveries/latest"),
    ("conclusions", "/api/conclusions"),
    ("diagnostics-json", "/api/diagnostics?format=json"),
    ("steward-status", "/api/steward/status"),
    ("steward-capabilities", "/api/steward/capabilities"),
    ("steward-actions", "/api/steward/actions"),
    ("steward-preauth", "/api/steward/pre-authorization"),
    ("auto-policy", "/api/auto-policy"),
    ("budget-guard", "/api/budget-guard"),
    ("task-404", "/api/tasks/definitely-missing"),
    ("conclusion-404", "/api/conclusions/99999999"),
)

# **流式端点不能这么量**（实测踩到）：`/api/events` 是 SSE，响应体不结束，
# `urlopen` 会一直等到超时 —— 于是它的耗时是 `inf`，然后排在"最慢"第一名。
# 把它当慢端点去优化是纯粹的浪费。要量流式端点得量"首字节时间"，那是另一套方法。
STREAMING_PATHS: frozenset[str] = frozenset({"/api/events"})


def _one(url: str) -> tuple[float, int, int]:
    """返回 `(毫秒, 状态码, 荷载字节)`。"""
    start = time.perf_counter()
    status = 0
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - 本机探针
            payload = response.read()
            status = response.status
            size = len(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
        size = len(payload)
    except Exception as exc:  # noqa: BLE001
        return (float("inf"), -1, len(str(exc)))
    return ((time.perf_counter() - start) * 1000, status, size)


def main() -> int:
    rows: list[dict] = []
    skipped: list[str] = []
    for name, path in ENDPOINTS:
        if path.split("?")[0] in STREAMING_PATHS:
            skipped.append(path)
            continue
        url = f"{BASE}{path}"
        for _ in range(WARMUP):
            _one(url)
        samples: list[float] = []
        status = 0
        size = 0
        for _ in range(REPEATS):
            elapsed, code, payload_size = _one(url)
            samples.append(elapsed)
            status = code
            size = max(size, payload_size)
        samples.sort()
        rows.append(
            {
                "name": name,
                "path": path,
                "status": status,
                "p50": round(statistics.median(samples), 1),
                "p95": round(samples[max(0, int(len(samples) * 0.95) - 1)], 1),
                "max": round(samples[-1], 1),
                "kb": round(size / 1024, 1),
            }
        )

    rows.sort(key=lambda item: -item["p95"])
    print(f"\n{'端点':22} {'状态':>4} {'p50':>8} {'p95':>8} {'max':>8} {'荷载':>9}")
    for row in rows:
        print(
            f"{row['name']:22} {row['status']:>4} {row['p50']:>8.1f} {row['p95']:>8.1f} "
            f"{row['max']:>8.1f} {row['kb']:>8.1f}KB"
        )

    print("\n最慢 5 个（按 p95，**同一路径取最慢的那个变体**）：")
    # 同一路径的不同查询串（`/api/tree` 与 `/api/tree?compact_done=false`）要合并：
    # 基线是**按路径**记的（`GET /api/tree`），不合并会给出两行同一个 key。
    worst: dict[str, dict] = {}
    for row in rows:
        base_path = row["path"].split("?")[0]
        current = worst.get(base_path)
        if current is None or row["p95"] > current["p95"]:
            worst[base_path] = row
    top = sorted(worst.values(), key=lambda item: -item["p95"])[:5]
    for row in top:
        print(f"  {row['path']}  p95={row['p95']}ms")

    if skipped:
        print(f"\n跳过的流式端点（`urlopen` 会等到超时，量出来是 inf）：{'、'.join(skipped)}")

    print("\n建议基线（可直接喂给 POST /api/metrics/baseline）：")
    print(
        json.dumps(
            [{"key": f"GET {row['path'].split('?')[0]}", "p95_ms": row["p95"]} for row in top],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
