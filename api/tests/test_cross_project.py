"""跨工程总览的判据（P3-4）。

这个端点要回答的是"我所有工程现在怎么样"。它最容易犯的错**不是崩溃**，而是
**报出一个看起来很正常的数字**：

* 库不存在 → 报 `0 个任务`（读起来像"这个工程是空的"，真相是"它没跑过"）；
* 单个工程读不到 → 整个端点 500（用户失去的正是"哪个坏了"这条信息）；
* 两个工程用不同币种 → 加成一个总额（那个数字在任何币种下都是错的）；
* 为看一眼总览 → 给每个没跑过的工程**造一个空库**（副作用比不显示更糟）。

所以本文件的判据大多是"**不许报什么**"。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| X1 | 打不开/不存在的库 → `available: false` + `reason`，字段是 `None` 不是 `0` | "0 个任务"与"读不到"混为一谈，用户以为工程是空的 |
| X2 | 一个工程坏了**不拖垮**整张表 | "有一个工程坏了"变成"总览坏了" |
| X3 | 读总览**绝不建库**（调用前后磁盘指纹一致） | 点一下面板就给所有工程造出空库 |
| X4 | 合计**不含**读不到的工程，且显式标 `totals_incomplete` | 合计被读成"全部"，少算的部分没人知道 |
| X5 | 金额**按币种分桶**，无币种标签的单列 | 把两种钱加成一个数，或把无标签的钱塞进某个币种 |
| X6 | "需要人动手"的判断顺序正确（读不到 > 有阻断债 > 在跑 > 空闲） | 看板不能一眼回答"哪个要我看" |
| X7 | 花费与当前工程看板**用同一份规范化逻辑** | 总览的钱和看板的钱对不上，没人解释得清 |
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture()
def state_home(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    target = tmp_path / "state-home"
    target.mkdir()
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(target))
    return target


def _mount(root: pathlib.Path, state_home: pathlib.Path):
    from app import project_store

    return project_store.mount(root, state_home=state_home)


def _make_project(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """在状态根之外造一个工程目录（状态根里面会被 `validate_project_root` 拒掉）。"""
    root = tmp_path / "outside" / name
    root.mkdir(parents=True)
    return root


def _seed_db(
    db_path: pathlib.Path,
    *,
    tasks: dict[str, int] | None = None,
    root_title: str = "目标",
    root_status: str = "running",
    blocking: int = 0,
    advisory: int = 0,
    usage: list[dict] | None = None,
    last_event: str = "",
) -> None:
    """按**真实 schema** 造一个最小库（列名与 `models.py` 对齐）。

    刻意手写建表而不是 import `models` 建库：本模块读的是**别的工程**的库，
    它的正确性不能依赖"对方是最新版 schema" —— 手写反而更贴近要测的现实。

    `root_status` 可覆盖：根节点状态直接决定 `verdict`，写死成 running 会让
    "全终态 → idle"这类用例永远测不到。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE task_nodes (id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, "
            "status TEXT, updated_at TEXT)"
        )
        con.execute(
            "CREATE TABLE run_records (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
            "kind TEXT, status TEXT, usage_json TEXT)"
        )
        con.execute(
            "CREATE TABLE obligation_index (id TEXT PRIMARY KEY, root_id TEXT, node_id TEXT, "
            "kind TEXT, severity TEXT, status TEXT)"
        )
        con.execute(
            "CREATE TABLE event_records (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, "
            "type TEXT, message TEXT, created_at TEXT)"
        )

        con.execute(
            "INSERT INTO task_nodes (id, parent_id, title, status, updated_at) VALUES (?, NULL, ?, ?, ?)",
            ("root-1", root_title, root_status, "2026-10-16T10:00:00"),
        )
        for status, count in (tasks or {}).items():
            for index in range(count):
                con.execute(
                    "INSERT INTO task_nodes (id, parent_id, title, status, updated_at) "
                    "VALUES (?, 'root-1', ?, ?, ?)",
                    (f"{status}-{index}", f"子 {index}", status, "2026-10-16T10:00:00"),
                )
        for index in range(blocking):
            con.execute(
                "INSERT INTO obligation_index (id, root_id, node_id, kind, severity, status) "
                "VALUES (?, 'root-1', 'root-1', 'child_failed', 'blocking', 'open')",
                (f"ob-blocking-{index}",),
            )
        for index in range(advisory):
            con.execute(
                "INSERT INTO obligation_index (id, root_id, node_id, kind, severity, status) "
                "VALUES (?, 'root-1', 'root-1', 'integration_skipped', 'advisory', 'open')",
                (f"ob-advisory-{index}",),
            )
        for payload in usage or []:
            con.execute(
                "INSERT INTO run_records (task_id, kind, status, usage_json) VALUES ('root-1', 'worker', 'succeeded', ?)",
                (json.dumps(payload),),
            )
        if last_event:
            con.execute(
                "INSERT INTO event_records (task_id, type, message, created_at) "
                "VALUES ('root-1', 'tree.started', ?, '2026-10-16T10:00:00')",
                (last_event,),
            )
        con.commit()
    finally:
        con.close()


def _snapshot(root: pathlib.Path) -> list[tuple[str, int]]:
    if not root.exists():
        return []
    return sorted(
        (path.relative_to(root).as_posix(), path.stat().st_size)
        for path in root.rglob("*")
        if path.is_file()
    )


# ---------- X1 读不到 ≠ 0 ----------


def test_x1_missing_database_is_unavailable_not_empty(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """库不存在 → `available: false` + 原因，且计数字段是 `None`。

    报 `0` 会被读成"这个工程是空的"。真相是"它还没跑过" —— 两件事对用户的
    含义完全不同（前者要排查，后者什么都不用做）。
    """
    from app.orchestrator import orchestrator

    _mount(_make_project(tmp_path, "never-ran"), state_home)
    old = orchestrator.settings
    from app.config import Settings

    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["available"] is False
    assert entry["reason"], "读不到就必须说原因"
    assert entry["task_total"] is None, "0 会被读成'工程是空的'"
    assert entry["verdict"] == "unknown"
    assert body["totals"]["readable_count"] == 0
    assert body["totals"]["totals_incomplete"] is True


def test_x2_a_broken_database_does_not_break_the_whole_table(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """一个库损坏时，整张表仍然出得来，坏的那个带原因。

    把"有一个工程坏了"变成"总览坏了"，用户失去的正是他最需要的那条信息。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    good = _mount(_make_project(tmp_path, "good"), state_home)
    broken = _mount(_make_project(tmp_path, "broken"), state_home)
    _seed_db(good.db_path, tasks={"done": 2})
    broken.db_path.parent.mkdir(parents=True, exist_ok=True)
    broken.db_path.write_text("this is not a sqlite file", encoding="utf-8")

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        response = client.get("/api/overview/cross-project")
    finally:
        orchestrator.settings = old

    assert response.status_code == 200, response.text
    body = response.json()
    by_key = {item["key"]: item for item in body["projects"]}
    assert by_key[good.key]["available"] is True
    assert by_key[good.key]["task_total"] == 3  # root-1 + 2 done
    assert by_key[broken.key]["available"] is False
    assert by_key[broken.key]["reason"]
    assert body["totals"]["totals_incomplete"] is True


def test_x1_database_without_task_nodes_is_unavailable_with_reason(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """能打开但不是我们的库（没有 task_nodes）→ 读不到，并说清是哪一步不对。"""
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "foreign"), state_home)
    mounted.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(mounted.db_path))
    try:
        con.execute("CREATE TABLE something_else (id TEXT)")
        con.commit()
    finally:
        con.close()

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["available"] is False
    assert "task_nodes" in entry["reason"]


# ---------- X3 只读：绝不建库 ----------


def test_x3_reading_the_overview_creates_no_databases(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """看总览**不许**给任何工程造出数据库。

    这条是负向判据里最要紧的一条：`sqlite3.connect()` 默认会**创建**文件，
    所以"顺手建库"是这里最自然的写法 —— 而后果是每个从没跑过的工程
    都会多出一个空库，用户之后看到的就是一堆"空工程"。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    for name in ("a", "b", "c"):
        _mount(_make_project(tmp_path, name), state_home)

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        before = _snapshot(state_home)
        response = client.get("/api/overview/cross-project")
        assert response.status_code == 200, response.text
        after = _snapshot(state_home)
    finally:
        orchestrator.settings = old

    assert after == before, "读总览动了磁盘 —— 它必须只读，且不建库"
    assert not list(state_home.rglob("battlefield.sqlite3"))


def test_x3_absent_database_is_not_created_by_the_read(
    tmp_path: pathlib.Path,
) -> None:
    """`_read_project` 对不存在的库**连一个字节都不该产生**。

    这条补的是上面那条的一个真实漏洞：只比"状态根里有没有新文件"抓不到
    `sqlite3.connect()` 缺 `uri=True` 的情形 —— 那时 URI 被当成**字面文件名**，
    SQLite 会在**当前工作目录**创建 `file:...?mode=ro`（Windows 上文件名里的
    `?` 变成 `_`），而状态根一个字节都没变、只多出一条"打不开"的原因。

    后果是：库明明存在也读不到，用户看到的是"读不到"，而不是"代码坏了"。
    所以必须在**另一条路径**上核对接得上。
    """
    from app import cross_project

    missing = tmp_path / "state" / "projects" / "k" / "battlefield.sqlite3"
    missing.parent.mkdir(parents=True)
    stray = list(missing.parent.iterdir())

    fact = cross_project._read_project(missing)

    assert fact["available"] is False
    assert "还没跑过" in fact["reason"], "该报'没跑过'；报别的说明连接方式有问题"
    assert list(missing.parent.iterdir()) == stray, "读一个不存在的库竟然产生了文件"


def test_x3_a_real_database_is_read_through_a_uri(
    tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """真库必须读得出来，且读完之后目录里**没有多出** `file:...` 这种垃圾文件。

    这是上一个用例的正向对照：只有"负向"会被"连接参数静默失效"骗过 ——
    失效时读不到任何东西，而"读不到"在负向判据里恰好是期望结果。
    """
    from app import cross_project

    mounted_root = tmp_path / "outside" / "uri"
    mounted_root.mkdir(parents=True)
    db_path = tmp_path / "state" / "projects" / "uri" / "battlefield.sqlite3"
    _seed_db(db_path, tasks={"done": 2}, root_status="done")

    before = {path.name for path in db_path.parent.iterdir()}
    fact = cross_project._read_project(db_path)
    after = {path.name for path in db_path.parent.iterdir()}

    assert fact["available"] is True, f"真库读不出来：{fact['reason']}"
    assert fact["task_total"] == 3
    assert after == before, f"读盘产生了额外文件：{sorted(after - before)}"


# ---------- X4 合计不含读不到的 ----------


def test_x4_totals_exclude_unreadable_and_say_so(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """合计只累计读得到的，并显式标注不完整。

    不标的话，用户会把合计读成"我的全部花费"，而它其实少算了。
    少算的钱比不报更危险：它给人"还早"的错觉。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    good = _mount(_make_project(tmp_path, "good"), state_home)
    _mount(_make_project(tmp_path, "never-ran"), state_home)
    _seed_db(
        good.db_path,
        tasks={"done": 4},
        usage=[{"usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}],
    )

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    totals = body["totals"]
    assert totals["project_count"] == 2
    assert totals["readable_count"] == 1
    assert totals["totals_incomplete"] is True
    assert totals["task_total"] == 5  # 只算 good 的
    assert totals["total_tokens"] == 120
    assert totals["unreadable_projects"] == [item["key"] for item in body["projects"] if not item["available"]]
    assert any("读不到" in note for note in body["notes"])


# ---------- X5 金额按币种分桶 ----------


def test_x5_costs_are_bucketed_by_currency_never_summed_across(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """两个工程用不同币种时，给出两个桶，不给一个假总额。"""
    from app.config import Settings
    from app.orchestrator import orchestrator

    cny = _mount(_make_project(tmp_path, "cny"), state_home)
    usd = _mount(_make_project(tmp_path, "usd"), state_home)
    _seed_db(cny.db_path, usage=[{"cost": {"amount": 1.5, "currency": "CNY"}}])
    _seed_db(usd.db_path, usage=[{"cost": {"amount": 2.0, "currency": "USD"}}])

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    buckets = body["totals"]["cost_by_currency"]
    assert buckets == {"CNY": 1.5, "USD": 2.0}


def test_x5_amount_without_a_currency_is_never_folded_into_a_bucket(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """没有币种标签的金额单列在 `cost_unlabeled`，不塞进任何币种桶。

    塞进去就是编数据：那个数字在它被塞入的币种下没有任何依据。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "unlabeled"), state_home)
    _seed_db(mounted.db_path, usage=[{"cost": {"amount": 3.25}}])

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    assert body["totals"]["cost_by_currency"] is None
    assert body["totals"]["cost_unlabeled"] == 3.25


def test_x5_no_usage_at_all_is_none_not_zero(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """完全没有用量记录 → 金额是 `None`，不是 `0`。

    `0` 会被读成"跑了但没花钱"，`None` 才是"没有可用的计价数据"。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "no-usage"), state_home)
    _seed_db(mounted.db_path, last_event="树已启动")

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    assert body["totals"]["cost_by_currency"] is None
    assert body["totals"]["cost_unlabeled"] is None
    assert body["totals"]["total_tokens"] == 0


# ---------- X6 结论的优先级 ----------


def test_x6_blocking_obligations_outrank_running(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """有未清阻断债 → `blocked`，即使还有节点在跑。

    "在跑"是表象，"有债在等人"才是要人动手的那件事。反过来报会让看板
    在真正需要人时显示"运行中"。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "blocked-but-running"), state_home)
    _seed_db(mounted.db_path, tasks={"running": 3}, blocking=1)

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["open_blocking_obligations"] == 1
    assert entry["verdict"] == "blocked"
    assert body["totals"]["blocked_projects"] == [mounted.key]
    assert any("阻断债" in note for note in body["notes"])


def test_x6_advisory_obligations_do_not_mark_a_project_blocked(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """只有 advisory 债 → 不算 blocked。

    把 advisory 也当阻断会让"需要人动手"这个信号天天亮着，然后被无视
    —— 那正是"假警"的代价。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "advisory-only"), state_home)
    _seed_db(mounted.db_path, tasks={"done": 1}, root_status="done", advisory=3)

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["open_blocking_obligations"] == 0
    assert entry["verdict"] == "idle"


def test_x6_all_terminal_is_idle(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """全终态且无债 → `idle`。"""
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "finished"), state_home)
    _seed_db(mounted.db_path, tasks={"done": 5, "canceled": 1}, root_status="done")

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    assert body["projects"][0]["verdict"] == "idle"


# ---------- X7 花费口径与看板一致 ----------


def test_x7_spend_uses_the_same_normalizer_as_the_board(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """总览的花费必须等于 `usage.summarize_usages` 对同样负载的结果。

    两边各写一份规范化 = 总览的钱和看板的钱对不上，而那种不一致
    没有任何人能解释清楚（也没有人有时间逐条比）。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator
    from app.usage import summarize_usages

    mounted = _mount(_make_project(tmp_path, "spend"), state_home)
    payloads = [
        {"usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}},
        {"usage": {"input_tokens": 50, "output_tokens": 5}, "cost": {"amount": 0.25, "currency": "USD"}},
    ]
    _seed_db(mounted.db_path, usage=payloads)

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    expected = summarize_usages(payloads)
    entry = body["projects"][0]
    assert entry["spend"] == expected
    assert body["totals"]["total_tokens"] == expected["total_tokens"]
    assert body["totals"]["cost_by_currency"] == {"USD": 0.25}


# ---------- 界面需要的那几样 ----------


def test_lead_goal_and_latest_event_are_reported(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """"这个工程在干什么"必须能一眼看到（最新一棵树的目标 + 最近一条事件）。

    只报"3 个根节点 / 12 个任务"回答不了用户真正的问题。
    """
    from app.config import Settings
    from app.orchestrator import orchestrator

    mounted = _mount(_make_project(tmp_path, "with-goal"), state_home)
    _seed_db(mounted.db_path, root_title="把玩家移动改成状态机", last_event="树已启动")

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["lead_goal"] == "把玩家移动改成状态机"
    assert entry["latest_event"]["message"] == "树已启动"
    assert entry["roots"][0]["id"] == "root-1"


def test_offline_project_directory_is_called_out(
    client: TestClient, tmp_path: pathlib.Path, state_home: pathlib.Path
) -> None:
    """工程目录被删掉时要在提醒里点名 —— 状态还在但跑不起来，这是要人处理的。"""
    import shutil

    from app.config import Settings
    from app.orchestrator import orchestrator

    root = _make_project(tmp_path, "gone")
    mounted = _mount(root, state_home)
    _seed_db(mounted.db_path, tasks={"done": 1})
    shutil.rmtree(root)

    old = orchestrator.settings
    orchestrator.settings = Settings(state_home=state_home)
    try:
        body = client.get("/api/overview/cross-project").json()
    finally:
        orchestrator.settings = old

    entry = body["projects"][0]
    assert entry["root_exists"] is False
    assert any("目录已经不存在" in note for note in body["notes"])
