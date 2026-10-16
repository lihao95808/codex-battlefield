"""P5-5：请求关联 ID 从 HTTP 请求一路串到 **Codex 子进程**（判据 T1–T9）。

## 这一组要证明什么

ROADMAP 那一句是"一个请求从界面到子进程可串起来"。HTTP 侧早就有了
（`x-request-id` + 结构化日志）；缺的是**子进程侧**：真正干活的是另一个进程，
它的产物在 `runs/<id>/*.jsonl` 里。这一组钉住四件事：

1. 中间件把 id 放进**进程内上下文**，请求结束**恢复**（不恢复的话，同一个 worker 上
   后续的后台任务会继承上一个请求的 id，日志里出现串线的关联）；
2. `run_records.request_id` 记的是**那个** id；
3. 子进程环境里带同一个 id；
4. **没有 id 时不许编**：`NULL` / 变量不存在，而不是空串或假 id。

第 4 条是这一组最重要的：把"没有"写成"有"会让排查时**追一条不存在的链路**。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import trace
from app.config import Settings
from app.models import RunRecord
from app.runner import CodexExecRunner


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {"workspace_dir": tmp_path / "workspace", "codex_bin": "codex"}
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def clean_trace():
    """每个用例前后都清空上下文与环境变量 —— 否则用例之间会互相串。"""

    token = trace.set_request_id("")
    os.environ.pop(trace.ENV_NAME, None)
    yield
    trace.reset_request_id(token)
    os.environ.pop(trace.ENV_NAME, None)


# --- T1/T2 上下文的设置与读取 -------------------------------------------------


def test_t1_context_roundtrip():
    assert trace.current_request_id() == ""
    assert trace.request_id_or_none() is None

    trace.set_request_id("req-abc")
    assert trace.current_request_id() == "req-abc"
    assert trace.request_id_or_none() == "req-abc"


def test_t2_env_fallback_for_child_processes(monkeypatch):
    """子进程里没有 contextvar，只能靠环境变量 —— 这是那条链路的后半段。"""

    monkeypatch.setenv(trace.ENV_NAME, "req-from-env")
    assert trace.current_request_id() == "req-from-env"
    # contextvar 有值时优先（同一进程里以请求上下文为准）。
    trace.set_request_id("req-context")
    assert trace.current_request_id() == "req-context"


def test_t2_blank_values_are_treated_as_absent(monkeypatch):
    monkeypatch.setenv(trace.ENV_NAME, "   ")
    assert trace.current_request_id() == ""
    assert trace.request_id_or_none() is None


# --- T3 子进程环境：有 id 才写 ------------------------------------------------


def test_t3_child_env_carries_the_request_id(tmp_path):
    trace.set_request_id("req-run-1")
    env = CodexExecRunner(_settings(tmp_path))._codex_env("worker", "node-a")
    assert env[trace.ENV_NAME] == "req-run-1"


def test_t3_child_env_omits_the_variable_without_a_request_id(tmp_path):
    env = CodexExecRunner(_settings(tmp_path))._codex_env("worker", "node-a")
    # **不存在**，而不是空串：空串会被下游读成"有关联但 id 是空的"。
    assert trace.ENV_NAME not in env


# --- T4 运行记录：记的是那个 id -----------------------------------------------


def test_t4_run_record_needs_a_database(tmp_path):
    """`run_records.request_id` 这一列真的建出来了（模型 + 迁移两处都要对）。"""

    from app.database import engine, init_db

    # 必须先建表：这个用例读的是 schema，不是业务数据。
    init_db()
    with engine.begin() as connection:
        rows = connection.exec_driver_sql("PRAGMA table_info(run_records)").fetchall()
    assert rows, "run_records 表应当已经建出来"
    columns = {row[1] for row in rows}
    assert "request_id" in columns
    # 可空是**语义**：树跑起来之后的调度不由某个请求触发。
    column = next(row for row in rows if row[1] == "request_id")
    assert column[3] == 0, "request_id 必须可空（NULL = 不是请求触发的）"


def test_t4_synthetic_run_records_the_request_id(tmp_path):
    """走真实的 `_record_synthetic_run`：它必须把当前请求 id 写进行里。"""

    from app.database import SessionLocal, init_db
    from app.models import TaskNode, TaskStatus
    from app.orchestrator import Orchestrator
    from app.models import RunKind

    init_db()
    orchestrator = Orchestrator(_settings(tmp_path, mock_delay_seconds=0))
    with SessionLocal() as db:
        db.add(TaskNode(id="t-1", title="t", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.commit()
        task = db.get(TaskNode, "t-1")

        trace.set_request_id("req-synthetic")
        orchestrator._record_synthetic_run(db, task, RunKind.ACCEPTANCE, "内部验收")
        db.commit()
        row = db.query(RunRecord).order_by(RunRecord.id.desc()).first()
        assert row.request_id == "req-synthetic"

        # 没有请求上下文时必须是 NULL —— 不许编。
        trace.set_request_id("")
        orchestrator._record_synthetic_run(db, task, RunKind.ACCEPTANCE, "内部验收")
        db.commit()
        row2 = db.query(RunRecord).order_by(RunRecord.id.desc()).first()
        assert row2.request_id is None


# --- T5 中间件：设置 + 恢复 ---------------------------------------------------


def test_t5_middleware_sets_and_resets_the_context():
    """直接驱动中间件函数：它必须在请求期间设置 id，并在结束时恢复。

    ⚠️ 第一版用 `asyncio.run(...)` 驱动，结果**这条判据是空转的**：
    `asyncio.run` 会把当前 context **复制**进新任务，所以协程里对 contextvar 的修改
    不会回传到调用方 —— 去掉中间件里的 reset，断言照样绿（变异测试抓出来的）。
    所以这里自己驱动协程帧：
      * step 1 进到中间件内部（此时它已经 set 过 id），断言 id 在位；
      * step 2 走完剩余部分（此时它应当已经 reset），断言上下文干净。
    """

    from starlette.requests import Request

    from app import main as main_mod

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/health",
        "headers": [(b"x-request-id", b"req-mw")],
        "query_string": b"",
    }
    request = Request(scope)
    seen: list[str] = []

    async def call_next(_request):
        seen.append(trace.current_request_id())

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}

        return _Response()

    coroutine = main_mod._measure_requests(request, call_next)
    try:
        coroutine.send(None)  # 中间件在一次 send 里跑完（`call_next` 是同步实现的）
    except StopIteration as stop:
        response = stop.value
    else:  # pragma: no cover - 真中间件只有一个 await，正常不会分两次
        raise AssertionError("中间件没有在一次 send 内结束")
    finally:
        coroutine.close()

    assert seen == ["req-mw"], "请求处理期间上下文里必须有这个 id"
    assert response.headers["x-request-id"] == "req-mw"
    # 恢复：出中间件之后上下文必须干净，否则下一条请求/后台任务会串线。
    assert trace.current_request_id() == ""


def test_t5_middleware_generates_an_id_when_the_client_sends_none():
    from starlette.requests import Request

    from app import main as main_mod

    scope = {"type": "http", "method": "GET", "path": "/api/health", "headers": [], "query_string": b""}
    request = Request(scope)
    seen: list[str] = []

    async def call_next(_request):
        seen.append(trace.current_request_id())

        class _Response:
            status_code = 200
            headers: dict[str, str] = {}

        return _Response()

    coroutine = main_mod._measure_requests(request, call_next)
    try:
        coroutine.send(None)
    except StopIteration as stop:
        response = stop.value
    else:  # pragma: no cover
        raise AssertionError("中间件没有在一次 send 内结束")
    finally:
        coroutine.close()

    assert seen and seen[0], "没有入站 id 时中间件必须生成一个"
    assert response.headers["x-request-id"] == seen[0]
    assert trace.current_request_id() == ""


# --- T6 覆盖边界写进代码 ------------------------------------------------------


def test_t6_trace_module_documents_its_boundary():
    """边界必须写在**代码里**：读代码的人不该去猜"哪些情况没有 id"。"""

    source = (Path(__file__).resolve().parents[1] / "app" / "trace.py").read_text(encoding="utf-8")
    for token in ("contextvars", "后台任务", "NULL", "不伪造"):
        assert token in source, token
