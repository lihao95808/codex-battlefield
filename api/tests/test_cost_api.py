"""花费归因与成本预测接到真实数据上的判据（P3-3 / P3-2）。

`test_cost.py` 钉的是**纯计算**；这里钉的是**接线**：真实 `task_nodes` +
`run_records` 经过 `cost_report()` 之后，报出来的东西对不对。

接线类判据最容易写成"函数被调用过"（那种断言在实现被注释掉时照样绿）。
所以下面的断言全部落在**可观测结果**上：某个桶的数字、某个计数、某个拒绝理由。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| R1 | 单价样本只取已结算节点；**blocked 不算** | 被阻塞的节点花过钱却没产出，算进去会把单价拉低 → 预测偏乐观 |
| R2 | 混币种 → **拒绝**预测（不做汇率假设） | 悄悄引入一个汇率，没人知道用了哪个 |
| R3 | 剩余节点数 = 非终态节点数 | "还要花多少"的分母错了，区间就整个错 |
| R4 | 归因键真的接上了：模型/角色/节点三维 | 归因表全是空桶，看起来像"没花过钱" |
| R5 | 读归因**只读**：调用前后磁盘指纹一致 | 看一眼归因就改了状态 |
| R6 | 重试（failed/canceled）会把预测区间放宽 | 预测永远是最乐观的那个数 |
| R7 | 没有树时报 404 / 没有节点时明确拒绝归因 | 凭空给一个 0，读起来像"没花钱" |
"""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import cost
from app.database import SessionLocal, engine, init_db
from app.models import Base, RunRecord, TaskNode, TaskStatus


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _node(node_id: str, status: str, *, parent_id: str | None = None, depth: int = 0, title: str = "") -> TaskNode:
    return TaskNode(
        id=node_id,
        parent_id=parent_id,
        title=title or node_id,
        description="",
        depth=depth,
        status=status,
    )


def _run(task_id: str, kind: str, usage: dict | None) -> RunRecord:
    return RunRecord(
        task_id=task_id,
        kind=kind,
        status="succeeded",
        command="codex exec",
        sandbox="workspace-write",
        exit_code=0,
        usage_json=json.dumps(usage) if usage is not None else None,
    )


def _seed_tree(rows: list[TaskNode], runs: list[RunRecord]) -> None:
    with SessionLocal() as db:
        db.add_all(rows)
        db.add_all(runs)
        db.commit()


def _report(root_id: str) -> dict:
    with SessionLocal() as db:
        return cost.cost_report(db, root_id)


# ---------- R1 blocked 不算单价样本 ----------


def test_r1_blocked_nodes_are_not_used_as_price_samples() -> None:
    """被阻塞的节点花过钱，却没有产出 —— 它**不能**进单价样本。

    算进去会把平均单价拉低，于是预测偏乐观；而乐观的预测会让人放心去睡觉。
    这里刻意让 blocked 节点很便宜：如果它被算进去，单价会从 2.0 掉到 1.0。
    """
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("done-1", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("blocked-1", TaskStatus.BLOCKED.value, parent_id="root", depth=1),
            _node("queued-1", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [
            _run("done-1", "worker", {"cost": {"amount": 2.0, "currency": "USD"}}),
            _run("blocked-1", "worker", {"cost": {"amount": 0.001, "currency": "USD"}}),
        ],
    )

    report = _report("root")
    assert report["node_counts"]["settled"] == 1
    assert report["projection"]["unit_cost"] == 2.0, "blocked 节点的花费被算进单价样本了"
    # 但它的花费**仍然要出现在归因里** —— 那是真的花掉的钱。
    assert report["attribution"]["cost_by_currency"] == {"USD": 2.001}


def test_r1_failed_and_canceled_do_count_as_samples() -> None:
    """失败/取消**要**算：它们同样消耗了一个工作单位，是真实成本。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("failed-1", TaskStatus.FAILED.value, parent_id="root", depth=1),
            _node("canceled-1", TaskStatus.CANCELED.value, parent_id="root", depth=1),
        ],
        [
            _run("failed-1", "worker", {"cost": {"amount": 1.0, "currency": "USD"}}),
            _run("canceled-1", "worker", {"cost": {"amount": 3.0, "currency": "USD"}}),
        ],
    )
    report = _report("root")
    assert report["node_counts"]["settled"] == 2
    assert report["projection"]["unit_cost"] == 2.0


# ---------- R2 混币种拒绝 ----------


def test_r2_mixed_currencies_refuse_to_project_instead_of_guessing_a_rate() -> None:
    """两个币种时**拒绝**给预测，并说清原因。

    合并会悄悄引入一个汇率（多半是 1:1），而没有任何人知道用了哪个。
    拒绝给数比给一个错的数好：用户会去看归因表。
    """
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("a", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("b", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("c", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [
            _run("a", "worker", {"cost": {"amount": 1.0, "currency": "USD"}}),
            _run("b", "worker", {"cost": {"amount": 7.0, "currency": "CNY"}}),
        ],
    )
    report = _report("root")
    assert report["projection"]["available"] is False
    assert "币种" in report["projection"]["reason"]
    # 归因里两个桶都要在 —— 拒绝的是"合并"，不是"记账"。
    assert report["attribution"]["cost_by_currency"] == {"CNY": 7.0, "USD": 1.0}


# ---------- R3 剩余节点数 ----------


def test_r3_remaining_nodes_counts_every_non_terminal_status() -> None:
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("d", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("f", TaskStatus.FAILED.value, parent_id="root", depth=1),
            _node("q", TaskStatus.QUEUED.value, parent_id="root", depth=1),
            _node("r", TaskStatus.RUNNING.value, parent_id="root", depth=1),
            _node("b", TaskStatus.BLOCKED.value, parent_id="root", depth=1),
            _node("p", TaskStatus.PLANNED.value, parent_id="root", depth=1),
        ],
        [_run("d", "worker", {"cost": {"amount": 1.0, "currency": "USD"}})],
    )
    counts = _report("root")["node_counts"]
    assert counts["total"] == 7
    # 终态 = done/failed/canceled/blocked；其余都算"还要花"。
    assert counts["remaining"] == 7 - 3  # root(waiting) + queued + running + planned
    # `settled` 是**状态上**的已结算（done + failed），与"有没有金额"无关。
    assert counts["settled"] == 2
    # 单价样本只算真的报了金额的那些。
    assert counts["priced_samples"] == 1


# ---------- R4 三维归因真的接上了 ----------


def test_r4_attribution_splits_real_rows_by_model_kind_and_node() -> None:
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value, title="根目标"),
            _node("leaf", TaskStatus.DONE.value, parent_id="root", depth=1, title="唯一子任务"),
            _node("next", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [
            _run("leaf", "worker", {"model": "cheap-model", "input_tokens": 100, "cost": {"amount": 0.5, "currency": "USD"}}),
            _run("leaf", "verifier", {"model": "strong-model", "input_tokens": 40, "cost": {"amount": 0.2, "currency": "USD"}}),
            _run("leaf", "worker", {"input_tokens": 10}),
        ],
    )
    attribution = _report("root")["attribution"]

    assert attribution["runs"] == 3
    assert attribution["runs_with_usage"] == 3
    assert attribution["runs_unknown_model"] == 1
    assert attribution["total_tokens"] == 150

    models = {bucket["key"]: bucket for bucket in attribution["by_model"]}
    assert models["cheap-model"]["runs"] == 1
    assert models["strong-model"]["total_tokens"] == 40

    kinds = {bucket["key"]: bucket for bucket in attribution["by_kind"]}
    assert kinds["worker"]["runs"] == 2
    assert kinds["verifier"]["cost_by_currency"] == {"USD": 0.2}

    by_node = attribution["by_node"]
    assert len(by_node) == 1
    assert by_node[0]["node_id"] == "leaf"
    assert by_node[0]["title"] == "唯一子任务"
    assert by_node[0]["depth"] == 1


def test_r4_runs_without_any_usage_are_reported_not_hidden() -> None:
    """有运行但没用量 → 单独计数，归因表不能看起来像"没花过钱"。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1),
        ],
        [_run("leaf", "worker", None), _run("leaf", "worker", {"无关字段": 1})],
    )
    attribution = _report("root")["attribution"]
    assert attribution["runs"] == 2
    assert attribution["runs_without_usage"] == 2
    assert attribution["cost_by_currency"] is None


# ---------- R5 只读 ----------


def test_r5_cost_report_is_read_only(tmp_path: pathlib.Path) -> None:
    """看一眼归因**不许**改动数据库里的任何字节（含 `-wal` / `-shm`）。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("leaf", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("next", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [_run("leaf", "worker", {"cost": {"amount": 1.0, "currency": "USD"}})],
    )

    db_path = pathlib.Path(engine.url.database or "")
    assert db_path.is_file()

    def snapshot() -> list[tuple[str, int]]:
        items: list[tuple[str, int]] = []
        for suffix in ("", "-wal", "-shm"):
            candidate = pathlib.Path(f"{db_path}{suffix}")
            if candidate.is_file():
                items.append((candidate.name, candidate.stat().st_size))
        return items

    before = snapshot()
    _report("root")
    assert snapshot() == before, "读归因改动了数据库文件"


# ---------- R6 重试放宽区间 ----------


def test_r6_retries_widen_the_projection() -> None:
    """有 failed/canceled 时 `retry_factor` 大于 1，于是区间上界更高。

    这条挡的是"预测永远只报最乐观那个数"：重试是真实存在的成本，
    不体现在区间里，用户就会低估。
    """
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("ok", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("bad", TaskStatus.FAILED.value, parent_id="root", depth=1),
            _node("todo", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [_run("ok", "worker", {"cost": {"amount": 2.0, "currency": "USD"}})],
    )
    projection = _report("root")["projection"]
    # 4 个节点里 1 个失败 → 1.25
    assert projection["retry_factor"] == 1.25
    assert projection["high"] == pytest.approx(projection["mid"] * 2)
    assert projection["mid"] > projection["low"]


def test_r6_no_retries_keeps_the_factor_at_one() -> None:
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("ok", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("todo", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [_run("ok", "worker", {"cost": {"amount": 2.0, "currency": "USD"}})],
    )
    projection = _report("root")["projection"]
    assert projection["retry_factor"] == 1.0
    assert projection["mid"] == projection["low"]


# ---------- R7 空树 / 没有树 ----------


def test_r7_an_empty_tree_refuses_to_attribute_instead_of_reporting_zero() -> None:
    """只有根、没有运行 → 明确"。给 0 会被读成"没花过钱"。"""
    _seed_tree([_node("root", TaskStatus.RUNNING.value)], [])
    report = _report("root")
    assert report["attribution"]["runs"] == 0
    assert report["attribution"]["cost_by_currency"] is None
    assert report["projection"]["available"] is False
    assert report["projection"]["reason"]


def test_r7_unknown_root_reports_zero_nodes_without_crashing() -> None:
    report = _report("does-not-exist")
    assert report["node_counts"]["total"] == 0
    assert report["projection"]["available"] is False


def test_r7_endpoint_without_any_tree_is_404(client: TestClient) -> None:
    assert client.get("/api/cost").status_code == 404


def test_r7_endpoint_defaults_to_the_most_recently_updated_root(client: TestClient) -> None:
    """不传 `root_id` 时用最近更新的那棵树 —— 界面不该被迫先问一次树。"""
    _seed_tree(
        [
            _node("older", TaskStatus.DONE.value, title="旧树"),
            _node("newer", TaskStatus.DONE.value, title="新树"),
        ],
        [_run("newer", "worker", {"cost": {"amount": 4.0, "currency": "USD"}})],
    )
    with SessionLocal() as db:
        # 显式把两棵树的 updated_at 拉开，避免依赖插入顺序。
        older = db.get(TaskNode, "older")
        newer = db.get(TaskNode, "newer")
        assert older is not None and newer is not None
        older.updated_at = older.updated_at.replace(year=2020)
        newer.updated_at = newer.updated_at.replace(year=2030)
        db.commit()

    body = client.get("/api/cost").json()
    assert body["root_id"] == "newer"
    assert body["attribution"]["cost_by_currency"] == {"USD": 4.0}


def test_r7_endpoint_honours_an_explicit_root_id(client: TestClient) -> None:
    _seed_tree(
        [_node("root-a", TaskStatus.DONE.value)],
        [_run("root-a", "worker", {"cost": {"amount": 1.0, "currency": "USD"}})],
    )
    body = client.get("/api/cost?root_id=root-a").json()
    assert body["root_id"] == "root-a"


def test_r8_tree_walk_covers_grandchildren_not_just_direct_children() -> None:
    """归因必须覆盖**整棵子树**：只算直接子节点会漏掉最贵的那一层。

    这条挡的是"用 parent_id == root 取子节点"这种顺手写法 —— 它在两层树上是对的，
    在真实的三层树上会漏掉一半的花费，而且不报错。
    """
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("mid", TaskStatus.WAITING_CHILDREN.value, parent_id="root", depth=1),
            _node("deep", TaskStatus.DONE.value, parent_id="mid", depth=2),
            _node("sibling-of-root", TaskStatus.DONE.value, title="别的树，不该被算"),
        ],
        [
            _run("deep", "worker", {"cost": {"amount": 9.0, "currency": "USD"}}),
            _run("sibling-of-root", "worker", {"cost": {"amount": 100.0, "currency": "USD"}}),
        ],
    )
    report = _report("root")
    assert report["node_counts"]["total"] == 3, "子树遍历没覆盖到孙节点"
    assert report["attribution"]["cost_by_currency"] == {"USD": 9.0}, "把别的树的花费算进来了"


def test_r8_tree_walk_terminates_on_a_cycle() -> None:
    """环（数据被写坏）不能让归因挂死 —— 它是只读诊断，必须能给出结论。

    造法：直接给 `_nodes_of_tree` 一个**会返回环状邻接**的假 session。
    为什么不插进真库：`task_nodes.parent_id` 是外键，"a 的父是 b、b 的父是 a"
    在真库里根本插不进去。用假 session 才测得到"拿到脏数据之后会不会挂"。
    """

    class _Row:
        def __init__(self, id: str, parent_id: str | None) -> None:
            self.id = id
            self.parent_id = parent_id
            self.title = id
            self.depth = 0
            self.status = TaskStatus.RUNNING.value

    class _Result:
        def all(self) -> list[_Row]:
            return [_Row("a", "b"), _Row("b", "a")]

    class _FakeSession:
        def execute(self, _statement: object) -> _Result:
            return _Result()

    nodes = cost._nodes_of_tree(_FakeSession(), "a")
    assert sorted(node[0] for node in nodes) == ["a", "b"]


def test_r8_usage_json_that_is_not_json_is_counted_as_without_usage() -> None:
    """`usage_json` 写坏时算"没有用量"，不能整个归因失败。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1),
        ],
        [],
    )
    with SessionLocal() as db:
        db.add(
            RunRecord(
                task_id="leaf",
                kind="worker",
                status="succeeded",
                command="codex exec",
                sandbox="workspace-write",
                exit_code=0,
                usage_json="{not json",
            )
        )
        db.commit()

    attribution = _report("root")["attribution"]
    assert attribution["runs"] == 1
    assert attribution["runs_without_usage"] == 1


def test_r8_missing_usage_column_values_do_not_break_selection() -> None:
    """`usage_json` 为 `NULL` 是常态，不能被当成解析失败。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("leaf", TaskStatus.RUNNING.value, parent_id="root", depth=1),
        ],
        [_run("leaf", "planner", None)],
    )
    attribution = _report("root")["attribution"]
    assert attribution["runs"] == 1
    assert attribution["runs_without_usage"] == 1


def test_r8_settled_nodes_without_money_do_not_fake_a_unit_cost() -> None:
    """已结算节点只有 token、没有金额 → 预测**拒绝**（不能报钱）。"""
    _seed_tree(
        [
            _node("root", TaskStatus.WAITING_CHILDREN.value),
            _node("leaf", TaskStatus.DONE.value, parent_id="root", depth=1),
            _node("next", TaskStatus.QUEUED.value, parent_id="root", depth=1),
        ],
        [_run("leaf", "worker", {"input_tokens": 5000, "output_tokens": 100})],
    )
    report = _report("root")
    # 状态上有 1 个已结算节点，但它没报金额 —— 这**正是**要区分开的那种情况。
    assert report["node_counts"]["settled"] == 1
    assert report["node_counts"]["priced_samples"] == 0
    projection = report["projection"]
    assert projection["available"] is False
    assert "计价" in projection["reason"]
    # token 仍然要报出来 —— 没有钱不等于没有数据。
    assert report["attribution"]["total_tokens"] == 5100


def test_r8_selector_only_reads_the_columns_it_needs() -> None:
    """选择器不该把整棵树的 text 列读进来（千节点树上那是实打实的开销）。

    这条用**列名**钉住：如果以后有人改成 `select(TaskNode)`，它会红。
    """
    with SessionLocal() as db:
        rows = cost._nodes_of_tree(db, "nonexistent")
    assert rows == []

    source = (pathlib.Path(cost.__file__)).read_text(encoding="utf-8")
    assert "select(TaskNode.id, TaskNode.parent_id, TaskNode.title, TaskNode.depth, TaskNode.status)" in source
