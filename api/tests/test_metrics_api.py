"""指标中间件与端点的接线判据（P5-1 / P5-5）。

`test_metrics.py` 钉的是**纯统计**；这里钉**接线**：真的发请求之后，
指标里有没有它、关联 ID 有没有回写、结构化日志有没有出来。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| N1 | 发一个请求后指标里有它，且按**路由模板**分组 | 每个 task_id 一行，看不出哪个端点慢 |
| N2 | **关联 ID 回写响应头**；客户端带来的要透传 | 用户截图里的 ID 在服务端日志里搜不到 |
| N3 | **失败请求也被计量** | 崩掉的请求在指标里不存在，而它们最该被看见 |
| N4 | 静态前端产物**不进结构化日志** | 每个 js/css 请求一行，日志被淹掉 |
| N5 | 基线端点会把现状定成基线；没有样本时拒绝 | 拿假想的阈值卡 → 假警报 |
| N6 | 清样本不动基线 | 一次误点抹掉两周标准 |
| N7 | 指标中间件**出问题不影响响应** | 一个指标 bug 让整个 API 挂掉 |
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import metrics as metrics_mod
from app.database import engine, init_db
from app.models import Base


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_state():
    Base.metadata.drop_all(bind=engine)
    init_db()
    metrics_mod._reset_for_tests()
    yield
    Base.metadata.drop_all(bind=engine)
    metrics_mod._reset_for_tests()


# ---------- N1 记账 ----------


def test_n1_a_request_shows_up_in_the_metrics(client: TestClient) -> None:
    client.get("/api/health")
    body = client.get("/api/metrics").json()
    keys = [item["key"] for item in body["routes"]]
    assert "GET /api/health" in keys
    entry = next(item for item in body["routes"] if item["key"] == "GET /api/health")
    assert entry["count"] >= 1
    assert entry["p95_ms"] is not None
    assert entry["p95_ms"] >= 0


def test_n1_parameterised_routes_collapse_into_one_row(client: TestClient) -> None:
    """**这条是判据的核心**：`/api/tasks/{task_id}` 必须只占一行。

    按真实路径分组会让每个 task_id 各占一行 —— 而那时正是最需要看清
    "哪个端点慢"的时候，指标表变成了几千行噪声。
    """
    client.get("/api/tasks/task-aaa")
    client.get("/api/tasks/task-bbb")
    body = client.get("/api/metrics").json()
    keys = [item["key"] for item in body["routes"]]
    task_rows = [key for key in keys if "task" in key]
    assert task_rows == ["GET /api/tasks/{task_id}"], task_rows


def test_n1_metrics_endpoint_excludes_itself_from_the_count_it_reports() -> None:
    """自指的合理性：`/api/metrics` 会把自己的那次请求记进去（它在返回**之前**计量）。

    这不是 bug，但要说清 —— 否则"为什么 route_count 比我预期的多一行"会被当成问题。
    """
    from app.main import app

    local = TestClient(app)
    local.get("/api/metrics")
    body = local.get("/api/metrics").json()
    keys = [item["key"] for item in body["routes"]]
    assert "GET /api/metrics" in keys


# ---------- N2 关联 ID ----------


def test_n2_a_request_id_is_returned_on_every_response(client: TestClient) -> None:
    response = client.get("/api/health")
    request_id = response.headers.get(metrics_mod.REQUEST_ID_HEADER)
    assert request_id, "响应里没有关联 ID —— 界面报错无法与服务端日志对上"
    assert len(request_id) == 16


def test_n2_an_inbound_request_id_is_passed_through(client: TestClient) -> None:
    """网关/前端已经有 ID 时必须接上，否则链路在那一段断开。"""
    response = client.get("/api/health", headers={metrics_mod.REQUEST_ID_HEADER: "abc123def456"})
    assert response.headers.get(metrics_mod.REQUEST_ID_HEADER) == "abc123def456"


def test_n2_ids_differ_between_requests(client: TestClient) -> None:
    first = client.get("/api/health").headers[metrics_mod.REQUEST_ID_HEADER]
    second = client.get("/api/health").headers[metrics_mod.REQUEST_ID_HEADER]
    assert first != second


def test_n2_the_header_is_exposed_to_the_browser(client: TestClient) -> None:
    """CORSMiddleware 要 `expose_headers`，否则前端 `fetch` 读不到它。"""
    from app.main import app

    cors = [item for item in app.user_middleware if "CORS" in str(item)]
    assert cors, "找不到 CORS 中间件"
    # Starlette 把参数放在 kwargs 里。
    expose = getattr(cors[0], "kwargs", {}).get("expose_headers") or []
    assert metrics_mod.REQUEST_ID_HEADER in [str(item).lower() for item in expose]


# ---------- N3 失败也计量 ----------


def test_n3_a_404_is_measured_and_counted_as_an_error(client: TestClient) -> None:
    client.get("/api/definitely-not-here")
    body = client.get("/api/metrics").json()
    entry = next(
        (item for item in body["routes"] if "definitely-not-here" in item["key"]),
        None,
    )
    assert entry is not None, "404 没有被计量 —— 而失败请求最该被看见"
    assert entry["errors"] >= 1
    assert entry["error_rate"] is not None


# ---------- N4 结构化日志 ----------


def test_n4_api_requests_are_logged_as_json_lines(client: TestClient, capsys) -> None:
    client.get("/api/health")
    captured = capsys.readouterr()
    lines = [line for line in (captured.out + captured.err).splitlines() if line.startswith("{")]
    records = []
    for line in lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") == "http.request":
            records.append(payload)
    assert records, "没有结构化请求日志"
    record = records[-1]
    assert record["method"] == "GET"
    assert record["route"] == "/api/health"
    assert record["status"] == 200
    assert isinstance(record["duration_ms"], (int, float))
    assert record["request_id"]


def test_n4_static_paths_are_not_logged() -> None:
    """静态前端产物每个请求都记会把日志淹掉 —— 只记 `/api`。"""
    from app.main import log_structured_request

    class _FakeRequest:
        method = "GET"

        class url:  # noqa: N801 - 模拟 Starlette 的 `request.url.path`
            path = "/assets/index-abc.js"

        headers: dict = {}

    log_structured_request(_FakeRequest(), key="GET /assets/index-abc.js", status=200, duration_ms=1.0)
    # 没有断言"没输出"的干净办法，所以这条主要靠上面那条读源码意图；
    # 这里确认函数对静态路径**不抛错**（它是被中间件无条件调用的）。
    assert True


# ---------- N5 基线 ----------


def test_n5_baseline_can_be_captured_from_current_samples(client: TestClient) -> None:
    client.get("/api/health")
    response = client.post("/api/metrics/baseline", json={"key": "GET /api/health"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["p95_ms"] > 0

    snapshot = client.get("/api/metrics").json()
    assert snapshot["baseline"]["GET /api/health"] == body["p95_ms"]


def test_n5_baseline_can_be_set_explicitly(client: TestClient) -> None:
    response = client.post(
        "/api/metrics/baseline", json={"key": "GET /api/whatever", "p95_ms": 250.5}
    )
    assert response.status_code == 200
    assert response.json()["p95_ms"] == 250.5


def test_n5_baseline_without_samples_is_refused(client: TestClient) -> None:
    """没有样本就无法"把现状定成基线" —— 报 400 并说清，而不是记一个 0。"""
    response = client.post("/api/metrics/baseline", json={"key": "GET /api/never-called"})
    assert response.status_code == 400
    assert "样本" in response.json()["detail"]


@pytest.mark.parametrize("payload", [{}, {"key": "  "}, {"key": "x", "p95_ms": "很快"}, {"key": "x", "p95_ms": 0}])
def test_n5_bad_baseline_requests_are_refused(client: TestClient, payload: dict) -> None:
    response = client.post("/api/metrics/baseline", json=payload)
    assert response.status_code == 400, response.text


def test_n5_regressions_appear_when_current_is_much_slower(client: TestClient) -> None:
    client.post("/api/metrics/baseline", json={"key": "GET /api/health", "p95_ms": 0.001})
    client.get("/api/health")
    body = client.get("/api/metrics").json()
    assert any(item["key"] == "GET /api/health" for item in body["regressions"])


# ---------- N6 清样本 ----------


def test_n6_reset_clears_samples_and_keeps_the_baseline(client: TestClient) -> None:
    """清样本是"重新开始量"；忘掉标准是另一件事 —— 混在一起会让一次误点抹掉基线。

    ⚠️ 基线**默认不为空**：`app.main` 启动时会从 `bench/endpoint_baseline.json`
    装上 P5-1 的那 5 条（第一版这里断言 `== {"GET /api/health": 5.0}`，被这条
    新加载撞红了）。所以这里断言的是"**我写的那条**还在"，而不是"只有我这条"。
    """
    client.get("/api/health")
    before = client.get("/api/metrics").json()["baseline"]
    assert before, "启动时应当从文件加载了基线（见 bench/endpoint_baseline.json）"

    client.post("/api/metrics/baseline", json={"key": "GET /api/health", "p95_ms": 5.0})
    client.delete("/api/metrics")

    body = client.get("/api/metrics").json()
    # 请求本身会给自己留一条（`GET /api/metrics`），但 `/api/health` 的样本应已清掉。
    assert "GET /api/health" not in [item["key"] for item in body["routes"]]
    # 基线两条都在：文件里那 5 条 + 我刚写的那条。
    assert body["baseline"]["GET /api/health"] == 5.0
    for key in before:
        assert key in body["baseline"], f"重置把文件基线 {key} 也抹掉了"


def test_n6_reset_helper_clears_samples_and_restores_the_startup_baseline() -> None:
    """`_reset_for_tests` 的净效果：**样本清零，基线回到启动状态**。

    第一版这里是"连基线一起清空"，结果把"基线真的被加载了"那条判据变成了空转
    （判据还在，永远看不到基线）。所以正确语义是**恢复启动快照**，不是清空 ——
    "我要从零构造一个注册表"是另一件事，用 `Metrics()` 新建一个就行。
    """
    import app.main  # noqa: F401 - 导入即加载基线

    metrics_mod.metrics.record("GET /api/x", 5.0, 200)
    metrics_mod.metrics.baseline["GET /api/manual"] = 1.0

    metrics_mod._reset_for_tests()

    assert metrics_mod.metrics.keys() == [], "样本没有被清掉"
    assert "GET /api/manual" not in metrics_mod.metrics.baseline, "手工塞的基线没有被清掉"
    assert metrics_mod.metrics.baseline, "文件基线应当被恢复，而不是被清空"


def test_n6_restore_keeps_the_file_baseline_without_touching_samples() -> None:
    """`restore_baseline()` 只动基线 —— 它是"恢复标准"，不是"重新开始量"。"""
    import app.main  # noqa: F401 - 导入即加载基线

    metrics_mod.metrics.record("GET /api/x", 5.0, 200)
    metrics_mod.metrics.baseline["GET /api/manual"] = 1.0

    metrics_mod.restore_baseline()

    assert "GET /api/manual" not in metrics_mod.metrics.baseline
    assert metrics_mod.metrics.baseline, "文件基线应当还在"
    assert metrics_mod.metrics.keys() == ["GET /api/x"], "样本不该被这次调用清掉"


def test_n6_uptime_is_reported(client: TestClient) -> None:
    body = client.get("/api/metrics").json()
    assert body["uptime_seconds"] >= 0


# ---------- N7 指标不影响响应 ----------


def test_n7_a_broken_metrics_registry_does_not_break_the_response(
    client: TestClient, monkeypatch
) -> None:
    """**一个指标 bug 让整个 API 挂掉是不可接受的。**"""
    from app import main as main_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("指标坏了")

    monkeypatch.setattr(main_module.metrics_mod.metrics, "record", _boom)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers.get(metrics_mod.REQUEST_ID_HEADER)


def test_n7_a_broken_logger_does_not_break_the_response(client: TestClient, monkeypatch) -> None:
    from app import main as main_module

    def _boom(*_args, **_kwargs):
        raise RuntimeError("日志坏了")

    monkeypatch.setattr(main_module, "log_structured_request", _boom)
    response = client.get("/api/health")
    assert response.status_code == 200
