"""慢端点基线的判据（P5-1）。

`test_metrics.py` / `test_metrics_api.py` 钉的是**机制**（怎么量、怎么报）。
这里钉的是**那份基线本身**：它有没有被接上、格式对不对、能不能真的抓住退化。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| B1 | 基线文件**真的被加载**进 `metrics.baseline` | 文件躺在那里没人读，退化永远不报 |
| B2 | 覆盖"最慢 5 个端点"，且 key 形态与中间件一致 | key 拼错 → 比对的永远是空集，静默失效 |
| B3 | 容忍度与文件里写的一致 | 两处数字漂移，判据按哪个走说不清 |
| B4 | 基线**抓得住**一个 5 倍退化 | 判据恒真 = 没有判据 |
| B5 | 基线**不误报** 20% 的抖动 | 抓太紧 → 所有人学会忽略这个信号 |
| B6 | 文件里的每个 key 都对应一个**真实存在的路由** | 端点改名后基线变成没人发现的死条目 |
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from app import metrics as metrics_mod
from app.metrics import Metrics, regression

BASELINE_FILE = pathlib.Path(__file__).resolve().parents[2] / "bench" / "endpoint_baseline.json"


@pytest.fixture(scope="module")
def baseline_doc() -> dict:
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def clean_metrics():
    """清样本，并把基线**恢复成启动时那一份**（而不是清空）。

    ⚠️ 第一版这里是 `_reset_for_tests()` + 清空基线，于是 B1 那条"基线真的被加载了"
    永远看不到它 —— **判据还在，永远红**。单跑时因为 import 顺序不同看起来是绿的，
    全量跑才稳定暴露。修法是把"清空"和"恢复启动快照"分成两件事。
    """
    metrics_mod._reset_for_tests()
    yield
    metrics_mod._reset_for_tests()


# ---------- B1 真的接了 ----------


def test_b1_the_baseline_file_exists_and_parses() -> None:
    assert BASELINE_FILE.is_file(), "基线文件不存在 —— 那 P5-1 的'不再退化'就没有依据"
    doc = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    assert isinstance(doc.get("budgets_ms"), dict)
    assert doc["budgets_ms"], "基线是空的 —— 空基线等于没有基线"


def test_b1_the_baseline_is_loaded_into_the_metrics_registry() -> None:
    """**这条是这一层的关键**：文件躺在那儿没人读，退化就永远不会被报出来。

    `app.main` 在导入时加载基线；这里直接查注册表里有没有那几条。
    """
    import app.main  # noqa: F401 - 导入即加载基线

    doc = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    for key, value in doc["budgets_ms"].items():
        assert metrics_mod.metrics.baseline.get(key) == pytest.approx(value, rel=1e-6), (
            f"{key} 的基线没有被加载进指标注册表"
        )


# ---------- B2 覆盖最慢 5 个 ----------


def test_b2_the_baseline_covers_five_endpoints(baseline_doc: dict) -> None:
    assert len(baseline_doc["budgets_ms"]) == 5, (
        "P5-1 的判据是'最慢 5 个端点有基线' —— 数量不对就说明没量全"
    )


def test_b2_keys_look_like_what_the_middleware_produces(baseline_doc: dict) -> None:
    """key 形态必须是 `METHOD /path`，且路径用**路由模板**。

    拼错的 key 不会报错：`regression()` 找不到基线就跳过，于是**静默失效** ——
    判据还在、永远绿、什么都没测。
    """
    for key in baseline_doc["budgets_ms"]:
        assert re.fullmatch(r"(GET|POST|DELETE|PUT) /api/\S+", key), key
        # 路由模板而不是真实 id。
        assert not re.search(r"/\d+(/|$)", key), f"{key} 看起来是真实路径，不是路由模板"


def test_b2_every_baseline_key_matches_a_real_route(baseline_doc: dict) -> None:
    """端点改名后基线会变成死条目 —— 这条让它红，而不是静默失效。"""
    from app.main import app

    routes = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or not path.startswith("/api"):
            continue
        for method in methods:
            routes.add(f"{method} {path}")
    missing = [key for key in baseline_doc["budgets_ms"] if key not in routes]
    assert missing == [], f"这些基线 key 找不到对应路由（端点改名了？）：{missing}"


# ---------- B3 容忍度一致 ----------


def test_b3_tolerance_is_stated_in_the_file(baseline_doc: dict) -> None:
    assert isinstance(baseline_doc.get("tolerance"), (int, float))
    assert 1.0 < float(baseline_doc["tolerance"]) <= 3.0, (
        "容忍度太大抓不住退化，太小会天天假警报"
    )


# ---------- B4 / B5 抓得住、不误报 ----------


def test_b4_a_five_times_slowdown_is_caught(baseline_doc: dict) -> None:
    """判据必须**能红**。"""
    tolerance = float(baseline_doc["tolerance"])
    key = next(iter(baseline_doc["budgets_ms"]))
    base = float(baseline_doc["budgets_ms"][key])
    snapshot = [{"key": key, "p95_ms": base * 5, "samples": 50}]
    found = regression(snapshot, {key: base}, tolerance=tolerance)
    assert len(found) == 1
    assert found[0]["ratio"] == pytest.approx(5.0, rel=0.05)


def test_b5_a_twenty_percent_wobble_is_not_reported(baseline_doc: dict) -> None:
    """p95 在共享机器上本来就会跳；抓 20% 的结果是所有人学会忽略它。"""
    tolerance = float(baseline_doc["tolerance"])
    key = next(iter(baseline_doc["budgets_ms"]))
    base = float(baseline_doc["budgets_ms"][key])
    snapshot = [{"key": key, "p95_ms": base * 1.2, "samples": 50}]
    assert regression(snapshot, {key: base}, tolerance=tolerance) == []


def test_b5_a_faster_endpoint_is_never_reported(baseline_doc: dict) -> None:
    tolerance = float(baseline_doc["tolerance"])
    key = next(iter(baseline_doc["budgets_ms"]))
    base = float(baseline_doc["budgets_ms"][key])
    snapshot = [{"key": key, "p95_ms": base * 0.1, "samples": 50}]
    assert regression(snapshot, {key: base}, tolerance=tolerance) == []


# ---------- B6 文件自述 ----------


def test_b6_the_file_says_how_it_was_measured(baseline_doc: dict) -> None:
    """数字必须带来源。没有来源的阈值，下一个人只会把它调大直到变绿。"""
    assert baseline_doc.get("measured_at")
    assert "probe_endpoint_latency" in str(baseline_doc.get("how"))
    assert baseline_doc.get("notes"), "没有写清这批数字的适用范围"


def test_b6_baseline_only_contains_file_entries() -> None:
    """注册表里**不该出现**文件之外的基线 —— 手工塞进去的条目没人维护。"""
    import app.main  # noqa: F401 - 导入即加载基线

    doc = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    extra = set(metrics_mod.metrics.baseline) - set(doc["budgets_ms"])
    assert extra == set(), f"有不在基线文件里的条目：{extra}"


def test_b6_an_empty_registry_reports_no_regressions() -> None:
    """没有基线时**不做判断** —— 拿假想的阈值卡只会制造假警报。"""
    registry = Metrics()
    registry.record("GET /api/x", 999.0, 200)
    assert regression(registry.snapshot(), registry.baseline) == []
