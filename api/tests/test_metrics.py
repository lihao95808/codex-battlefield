"""请求指标与关联 ID 的判据（P5-1 / P5-5）。

这一层要防的错是**指标本身在骗人**：

* 分位数被当成全历史读（滑动窗口 vs 全量）；
* 空样本被渲染成 `0 ms`（读起来像"快得看不见"）；
* 失败请求不计时（系统最不稳定的时候指标最漂亮）；
* 按真实路径分组（几千行噪声里看不出哪个端点慢）。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| M1 | 样本有**上限**（不设上限的指标记录就是内存泄漏） | 长跑几十小时后进程被指标吃掉 |
| M2 | 空样本的分位数是 `None`，**不是 0** | 0ms 被读成"很快"，而真相是"还没量到" |
| M3 | **失败请求也进分位数** | 最不稳定的时候指标最好看 |
| M4 | 按**路由模板**分组，不按真实路径 | 每个 task_id 一行，指标表变成噪声 |
| M5 | 分位数口径是**最近窗口**，且响应里写明 | 当全历史读会得出错误结论 |
| M6 | 退化比较有**容忍度**（1.5×），且没基线时不判断 | 抓太紧 → 所有人学会忽略它；没基线乱判断 → 假警报 |
| M7 | 关联 ID：客户端带来的要透传，没有的才生成 | 网关/前端的 ID 被丢掉，链路断开 |
| M8 | 清样本**不动基线** | 一次误点抹掉两周的标准 |
"""
from __future__ import annotations

import pytest

from app import metrics as metrics_mod
from app.metrics import Metrics, RouteStats, new_request_id, percentiles, regression, route_key


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics_mod._reset_for_tests()
    yield
    metrics_mod._reset_for_tests()


# ---------- M1 有界 ----------


def test_m1_samples_are_bounded() -> None:
    """**不设上限的指标记录本身就是内存泄漏** —— 而它长得像一个正常的监控功能。"""
    stats = RouteStats(key="X")
    for index in range(metrics_mod.SAMPLE_LIMIT + 500):
        stats.record(float(index), 200)
    assert len(stats.samples) == metrics_mod.SAMPLE_LIMIT
    # 但**总计数**不能丢：那是"这个端点被调了多少次"，与窗口无关。
    assert stats.count == metrics_mod.SAMPLE_LIMIT + 500


def test_m1_the_window_keeps_the_newest_samples() -> None:
    stats = RouteStats(key="X")
    for index in range(metrics_mod.SAMPLE_LIMIT + 10):
        stats.record(float(index), 200)
    assert max(stats.samples) == float(metrics_mod.SAMPLE_LIMIT + 9)


# ---------- M2 空样本 ----------


def test_m2_empty_samples_give_none_not_zero() -> None:
    """0ms 会被读成"快得看不见"，而真相是"还没量到"。"""
    payload = percentiles(RouteStats(key="X"))
    assert payload["p50_ms"] is None
    assert payload["p95_ms"] is None
    assert payload["p99_ms"] is None
    assert payload["mean_ms"] is None
    assert payload["max_ms"] is None
    assert payload["error_rate"] is None
    assert payload["count"] == 0


def test_m2_percentiles_are_ordered() -> None:
    stats = RouteStats(key="X")
    for value in range(1, 101):
        stats.record(float(value), 200)
    payload = percentiles(stats)
    assert payload["p50_ms"] <= payload["p95_ms"] <= payload["p99_ms"]
    assert payload["p50_ms"] == pytest.approx(50.5, abs=1.0)
    assert payload["max_ms"] == 100.0


def test_m2_single_sample_works() -> None:
    stats = RouteStats(key="X")
    stats.record(12.5, 200)
    payload = percentiles(stats)
    assert payload["p50_ms"] == payload["p95_ms"] == payload["p99_ms"] == 12.5


# ---------- M3 失败也录 ----------


def test_m3_failures_count_in_percentiles_and_error_rate() -> None:
    stats = RouteStats(key="X")
    stats.record(10.0, 200)
    stats.record(500.0, 500)
    payload = percentiles(stats)
    assert payload["count"] == 2
    assert payload["errors"] == 1
    assert payload["error_rate"] == 0.5
    assert payload["last_status"] == 500
    # 那条 500ms 的失败**必须**影响分位数。
    assert payload["max_ms"] == 500.0


def test_m3_4xx_counts_as_an_error() -> None:
    stats = RouteStats(key="X")
    stats.record(1.0, 400)
    stats.record(1.0, 200)
    assert percentiles(stats)["errors"] == 1


# ---------- M4 路由模板 ----------


def test_m4_route_key_prefers_the_template() -> None:
    """按真实路径分组会让每个 task_id 各占一行 —— 而那时最需要看清"哪个端点慢"。"""
    assert route_key("get", "/api/tasks/abc123", "/api/tasks/{task_id}") == "GET /api/tasks/{task_id}"
    assert route_key("GET", "/api/tasks/abc") == "GET /api/tasks/abc"
    assert route_key("post", "/api/tree", None) == "POST /api/tree"


def test_m4_a_new_route_is_created_on_first_record() -> None:
    registry = Metrics()
    registry.record("GET /api/x", 5.0, 200)
    assert registry.keys() == ["GET /api/x"]
    assert registry.route("GET /api/y") is None


# ---------- M5 窗口口径 ----------


def test_m5_payload_states_the_window_semantics() -> None:
    registry = Metrics()
    registry.record("GET /api/x", 5.0, 200)
    payload = registry.payload()
    assert payload["sample_limit"] == metrics_mod.SAMPLE_LIMIT
    assert "最近窗口" in payload["note"]
    assert payload["route_count"] == 1


def test_m5_snapshot_is_sorted_slowest_first() -> None:
    """按 **p95** 排，不按平均：平均会被大量快请求稀释，而用户感受到的是最慢那几次。"""
    registry = Metrics()
    for value in (1.0, 2.0, 3.0):
        registry.record("GET /api/fast", value, 200)
    for value in (100.0, 200.0, 300.0):
        registry.record("GET /api/slow", value, 200)
    assert [item["key"] for item in registry.snapshot()] == ["GET /api/slow", "GET /api/fast"]
    assert [item["key"] for item in registry.snapshot(limit=1)] == ["GET /api/slow"]


# ---------- M6 退化比较 ----------


def test_m6_regression_needs_a_baseline() -> None:
    """没有基线时**不做判断** —— 拿假想的阈值去卡只会制造假警报。"""
    registry = Metrics()
    registry.record("GET /api/x", 100.0, 200)
    assert regression(registry.snapshot(), {}) == []


def test_m6_regression_tolerates_noise_but_catches_real_slowdowns() -> None:
    """`tolerance=1.5` 抓的是"慢了 5 倍"，不是"抖了 20%"。

    抓太紧的结果是所有人学会忽略这个信号 —— 那时它就不再是判据了。
    """
    snapshot = [{"key": "GET /api/x", "p95_ms": 120.0, "samples": 10}]
    assert regression(snapshot, {"GET /api/x": 100.0}) == []  # 1.2× → 容忍

    caught = regression(snapshot, {"GET /api/x": 40.0})
    assert len(caught) == 1
    assert caught[0]["ratio"] == 3.0


def test_m6_regression_skips_routes_without_samples() -> None:
    snapshot = [{"key": "GET /api/x", "p95_ms": None, "samples": 0}]
    assert regression(snapshot, {"GET /api/x": 10.0}) == []


def test_m6_regression_ignores_non_positive_baselines() -> None:
    snapshot = [{"key": "GET /api/x", "p95_ms": 50.0, "samples": 5}]
    assert regression(snapshot, {"GET /api/x": 0.0}) == []
    assert regression(snapshot, {"GET /api/x": -3.0}) == []


# ---------- M7 关联 ID ----------


def test_m7_request_ids_are_distinct_and_short() -> None:
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(value) == 16 for value in ids)


# ---------- M8 清样本不动基线 ----------


def test_m8_reset_clears_samples_but_keeps_the_baseline() -> None:
    registry = Metrics()
    registry.record("GET /api/x", 10.0, 200)
    registry.baseline["GET /api/x"] = 10.0
    registry.reset()
    assert registry.keys() == []
    assert registry.baseline == {"GET /api/x": 10.0}


def test_m8_recorder_measures_once() -> None:
    registry = Metrics()
    recorder = metrics_mod.Recorder("GET /api/x")
    first = recorder.finish(registry, 200)
    second = recorder.finish(registry, 200)
    assert first == second, "重复 finish 不该把同一段时间算两次"
    assert percentiles(registry.route("GET /api/x"))["count"] == 2


# ---------- 分位数实现本身 ----------


def test_quantile_interpolates_between_samples() -> None:
    stats = RouteStats(key="X")
    for value in (0.0, 100.0):
        stats.record(value, 200)
    payload = percentiles(stats)
    # 两个样本之间线性插值：p50 = 50，p99 = 99（不是"取最近的样本"）。
    assert payload["p50_ms"] == 50.0
    assert payload["p99_ms"] == pytest.approx(99.0)
