"""结论文档的判据（P4-5）。

这个模块要防的错是**文档读起来像结论，其实只是一堆没核对过的事实**。
具体三种表现，各有一组判据：

* **把"没发生"与"读不到"混为一谈**（空段被读成"确定没有"）；
* **事实与人的判断混在一段里**（读者分不清哪句是数据、哪句是猜测）；
* **只有事实也叫"结论"**（那它只是另一份事件表，而且更少被人重新检查）。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| K1 | 空的时间线/花费要进 `incomplete` | "没有决策点"被读成"确定没人做过决定" |
| K2 | 没有未清债**不进** `incomplete` | "没有欠账"是一个确定事实，不是缺失 |
| K3 | 事实与判断在 Markdown 里**分段** | 读者分不清哪句是数据 |
| K4 | `write()` 拒绝空 `authored` | 只有事实的文档被当成结论归档 |
| K5 | 只抽决策点，不抽全部事件 | 一份 500 行的时间线没人读 |
| K6 | "以后什么条件下重新打开"必须给 | 结案即遗忘：同样的债再来时没人想起翻它 |
| K7 | 代价读不到时写"读不到"，不写 0 | 0 被读成"没花钱" |
| K8 | 结论必须挂在某棵树上；状态只能 draft/final | 归档记录找不回来 / 状态漂移 |
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app import conclusions
from app.database import SessionLocal, engine, init_db
from app.models import Base, IncidentConclusion


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _tree(tasks: list[dict]) -> dict:
    return {"root_id": "root-1", "tasks": tasks}


def _task(node_id: str, status: str, **extra) -> dict:
    return {
        "id": node_id,
        "title": f"任务 {node_id}",
        "status": status,
        "error_message": extra.get("error_message", ""),
        "leaf_attempts": extra.get("leaf_attempts", 0),
    }


def _event(event_type: str, at: str, message: str = "", task_id: str = "") -> dict:
    return {"type": event_type, "created_at": at, "message": message, "task_id": task_id}


# ---------- K5 只抽决策点 ----------


def test_k5_only_decision_events_are_in_the_timeline() -> None:
    """普通事件（创建、工作树、快照）不进时间线，决策事件进。

    一份把 300 条事件按时间倒出来的文档没人读；复盘真正要的是"在哪些岔路口
    做过选择"。
    """
    events = [
        _event("task.created", "2026-10-16T10:00:00", "建节点"),
        _event("worktree.created", "2026-10-16T10:01:00", "建工作树"),
        _event("constitution.locked", "2026-10-16T10:02:00", "宪法已锁定"),
        _event("tree.snapshot_restored", "2026-10-16T10:03:00", "恢复了快照"),
        _event("obligation.settled_human", "2026-10-16T10:04:00", "人工清债"),
    ]
    timeline = conclusions.decision_timeline(events)
    assert [item["type"] for item in timeline] == [
        "constitution.locked",
        "obligation.settled_human",
    ]


def test_k5_timeline_is_sorted_by_time_not_by_input_order() -> None:
    """输入顺序不可信（事件来自不同查询），必须按时序排。"""
    events = [
        _event("task.canceled", "2026-10-16T12:00:00", "晚"),
        _event("constitution.locked", "2026-10-16T09:00:00", "早"),
    ]
    timeline = conclusions.decision_timeline(events)
    assert [item["message"] for item in timeline] == ["早", "晚"]


def test_k5_unknown_event_types_are_ignored_not_guessed() -> None:
    """不认识的类型不进时间线（也不报错）—— 猜"这个大概算决策"会污染它。"""
    assert conclusions.decision_timeline([_event("brand.new.event", "2026-10-16T10:00:00")]) == []


# ---------- K1 / K2 incomplete 的边界 ----------


def test_k1_empty_timeline_lands_in_incomplete_with_both_readings() -> None:
    """空时间线必须进 `incomplete`，并说清两种可能。

    这是本模块最要紧的一条判据：「没有决策点」与「读不到决策点」在文档里
    长得一模一样，而它们的含义完全不同。
    """
    facts, incomplete = conclusions.build_facts(
        tree=_tree([_task("a", "done")]), events=[], cost={"total_tokens": 10}
    )
    assert facts["timeline"] == []
    message = next(text for text in incomplete if "决策" in text)
    assert "要么" in message and "要么" in message, "没有区分'确实没有'与'没记上'"


def test_k1_missing_cost_lands_in_incomplete_and_facts_has_no_cost_key() -> None:
    facts, incomplete = conclusions.build_facts(
        tree=_tree([_task("a", "done")]),
        events=[_event("constitution.locked", "2026-10-16T10:00:00")],
        cost=None,
    )
    assert "cost" not in facts
    assert any("花费" in text for text in incomplete)


def test_k1_empty_tree_lands_in_incomplete() -> None:
    _facts, incomplete = conclusions.build_facts(tree=_tree([]), events=[], cost={"total_tokens": 0})
    assert any("没有节点" in text for text in incomplete)


def test_k1_missing_node_falls_back_to_the_whole_tree_and_says_so() -> None:
    """指定了一个不在树里的节点时退回整树，**并在 incomplete 里说明**。

    静默退回是最糟的：文档标题写着那个节点，内容却是整棵树的。
    """
    facts, incomplete = conclusions.build_facts(
        tree=_tree([_task("a", "done"), _task("b", "blocked")]),
        events=[],
        cost={"total_tokens": 0},
        node_id="not-in-tree",
    )
    assert facts["node_count"] == 2
    assert any("不在这棵树里" in text for text in incomplete)


def test_k2_open_obligations_do_not_land_in_incomplete() -> None:
    """"没有未清债"是**一个确定的事实**，不是缺失 —— 不该进 incomplete。

    把它当缺失会让每份干净的结论都带着一条"没能确定"，于是没人再认真读那一段。
    """
    _facts, incomplete = conclusions.build_facts(
        tree=_tree([_task("a", "done")]),
        events=[_event("constitution.locked", "2026-10-16T10:00:00")],
        open_obligations=[],
        cost={"total_tokens": 1},
    )
    assert incomplete == [], f"干净的输入不该有未确定项：{incomplete}"


def test_k2_facts_carry_impact_counts_and_stuck_nodes() -> None:
    facts, _ = conclusions.build_facts(
        tree=_tree(
            [
                _task("a", "done"),
                _task("b", "blocked", error_message="缺依赖", leaf_attempts=3),
                _task("c", "failed", leaf_attempts=1),
                _task("d", "running"),
            ]
        ),
        events=[],
        cost={"total_tokens": 5},
    )
    assert facts["node_count"] == 4
    assert facts["settled_count"] == 3  # done + blocked + failed
    assert facts["unsettled_count"] == 1
    assert facts["status_counts"] == {"blocked": 1, "done": 1, "failed": 1, "running": 1}
    # 卡住的按尝试次数排：尝试多的更值得看。
    assert [node["id"] for node in facts["stuck_nodes"]] == ["b", "c"]
    assert facts["stuck_nodes"][0]["error"] == "缺依赖"


# ---------- K6 重新打开条件 ----------


def test_k6_reopen_conditions_are_derived_from_open_obligations() -> None:
    conditions = conclusions.reopen_conditions(
        [
            {"node_id": "n1", "type": "child_failed", "severity": "blocking"},
            {"node_id": "n2", "type": "integration_skipped", "severity": "advisory"},
        ]
    )
    assert any("n1" in text and "阻断" in text for text in conditions)
    assert any("n2" in text and "升级" in text for text in conditions)


def test_k6_no_obligations_still_gives_a_condition() -> None:
    """没有未清债时也要有一段，写明"再次出现同一症状才回来看"。

    留空会让人以为"没什么可看的"，而一段明确的"什么条件下回来"才是防结案即遗忘的东西。
    """
    conditions = conclusions.reopen_conditions([])
    assert len(conditions) == 1
    assert "再次出现" in conditions[0]


# ---------- K3 渲染分段 ----------


def _render(**over) -> str:
    facts = over.pop(
        "facts",
        {
            "root_id": "root-1",
            "node_count": 2,
            "settled_count": 1,
            "unsettled_count": 1,
            "status_counts": {"done": 1, "blocked": 1},
            "timeline": [
                {"at": "2026-10-16T10:00:00", "type": "constitution.locked", "message": "锁了", "node_id": ""}
            ],
            "timeline_total": 1,
            "stuck_nodes": [
                {"id": "b", "title": "任务 b", "status": "blocked", "error": "缺依赖", "attempts": 3}
            ],
            "open_obligations": [{"node_id": "b", "type": "child_blocked", "severity": "blocking"}],
            "cost": {"total_tokens": 1234, "cost_by_currency": {"USD": 1.5}},
        },
    )
    return conclusions.render_markdown(
        title=over.pop("title", "一次卡住"),
        facts=facts,
        incomplete=over.pop("incomplete", ["没有决策点。"]),
        authored=over.pop("authored", "我当时以为依赖已经装好了。"),
        author=over.pop("author", "duzuguang"),
    )


def test_k3_markdown_separates_facts_from_judgement() -> None:
    """事实段与"人写"段必须分开，且未确定项有自己的一段。"""
    markdown = _render()
    assert "## 影响面（代码抽取）" in markdown
    assert "## 做了什么决定（代码抽取的决策点）" in markdown
    assert "## 代价（代码抽取）" in markdown
    assert "## 当时为什么这么决定（**人写**）" in markdown
    assert "## 这份草稿里没能确定的地方" in markdown
    # 人的署名要跟着人的那一段。
    assert "— duzuguang" in markdown


def test_k3_markdown_keeps_the_node_ids_and_messages() -> None:
    markdown = _render()
    assert "constitution.locked" in markdown
    assert "缺依赖" in markdown
    assert "USD 1.5" in markdown
    assert "1234" in markdown


def test_k3_empty_authored_renders_an_explicit_placeholder() -> None:
    """没写"当时为什么"时不留空白，而是明说这一份还不值得归档。"""
    markdown = _render(authored="")
    assert "还没写" in markdown
    assert "另一份事件表" in markdown


def test_k3_missing_cost_says_unavailable_not_zero() -> None:
    """代价读不到时写"读不到"，**不写 0**。"""
    facts = {
        "root_id": "r",
        "node_count": 1,
        "settled_count": 1,
        "unsettled_count": 0,
        "status_counts": {"done": 1},
        "timeline": [],
        "timeline_total": 0,
        "stuck_nodes": [],
        "open_obligations": [],
    }
    markdown = _render(facts=facts)
    assert "没有花费数据" in markdown
    assert "不代表没花钱" in markdown


def test_k3_truncated_timeline_says_how_many_are_missing() -> None:
    """时间线被截断时必须写还剩多少条 —— 否则读者以为那就是全部。"""
    facts = {
        "root_id": "r",
        "node_count": 1,
        "settled_count": 1,
        "unsettled_count": 0,
        "status_counts": {"done": 1},
        "timeline": [
            {"at": "2026-10-16T10:00:00", "type": "task.retry", "message": "重试", "node_id": ""}
        ],
        "timeline_total": 55,
        "stuck_nodes": [],
        "open_obligations": [],
        "cost": {"total_tokens": 1, "cost_by_currency": None},
    }
    markdown = _render(facts=facts)
    assert "另有 54 条" in markdown


# ---------- K4 / K8 落库 ----------


def _write(**over):
    params = {
        "root_id": "root-1",
        "authored": "我当时以为依赖已经装好了，应该先跑一次 doctor。",
        "title": "一次卡住",
        "author": "duzuguang",
        "facts": {"node_count": 1, "status_counts": {"blocked": 1}, "open_obligations": []},
        "incomplete": ["没有决策点。"],
    }
    params.update(over)
    with SessionLocal() as db:
        row = conclusions.write(db, **params)
        _ = row.id
        return conclusions.payload(row)


def test_k4_write_requires_authored_text() -> None:
    """**只有事实的文档不算结论。**"""
    with SessionLocal() as db:
        with pytest.raises(ValueError) as excinfo:
            conclusions.write(db, root_id="root-1", authored="   ")
    assert "人写" in str(excinfo.value) or "事件表" in str(excinfo.value)


def test_k4_write_persists_and_is_readable_back() -> None:
    payload = _write()
    assert payload["status"] == "draft"
    assert payload["facts"]["node_count"] == 1
    assert payload["incomplete"] == ["没有决策点。"]
    assert "## 当时为什么这么决定" in payload["markdown"]

    with SessionLocal() as db:
        listed = conclusions.list_conclusions(db, root_id="root-1")
    assert [row.id for row in listed] == [payload["id"]]


def test_k8_write_rejects_unknown_status_and_missing_root() -> None:
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            conclusions.write(db, root_id="root-1", authored="x", status="published")
        with pytest.raises(ValueError):
            conclusions.write(db, root_id="   ", authored="x")


def test_k8_updating_keeps_the_id_and_rewrites_the_markdown() -> None:
    """改结论是**同一条记录**：改了"当时为什么"要重新渲染文档，不能留旧的。"""
    payload = _write()
    with SessionLocal() as db:
        row = conclusions.write(
            db,
            root_id="root-1",
            authored="更正：其实是我把路径写错了。",
            title="一次卡住",
            author="duzuguang",
            conclusion_id=payload["id"],
            status="final",
        )
        updated = conclusions.payload(row)

    assert updated["id"] == payload["id"]
    assert updated["status"] == "final"
    assert "把路径写错了" in updated["markdown"]
    assert "依赖已经装好了" not in updated["markdown"]


def test_k8_updating_a_missing_conclusion_is_an_error() -> None:
    with SessionLocal() as db:
        with pytest.raises(ValueError):
            conclusions.write(db, root_id="root-1", authored="x", conclusion_id=999999)


def test_k8_list_filters_by_root_and_is_newest_first() -> None:
    first = _write(root_id="root-a")
    second = _write(root_id="root-a")
    _write(root_id="root-b")

    with SessionLocal() as db:
        rows = conclusions.list_conclusions(db, root_id="root-a")
        assert [row.id for row in rows] == [second["id"], first["id"]]
        everything = conclusions.list_conclusions(db)
        assert len(everything) == 3


def test_k8_payload_survives_corrupt_json_columns() -> None:
    """`facts_json` 被写坏时 `payload()` 不能炸 —— 归档记录要读得出来。"""
    with SessionLocal() as db:
        row = IncidentConclusion(
            root_id="root-1",
            title="坏记录",
            facts_json="{not json",
            incomplete_json="also not json",
            authored="x",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        result = conclusions.payload(row)
    assert result["facts"] == {}
    assert result["incomplete"] == []


def test_k8_migration_creates_the_table() -> None:
    """表必须随 `init_db()` 建出来 —— 否则归档接口在干净库里直接 500。"""
    from sqlalchemy import inspect

    assert "incident_conclusions" in inspect(engine).get_table_names()


def test_k8_facts_are_stored_as_json_not_a_repr() -> None:
    """存进库的必须是**合法 JSON**（以后要做统计，`repr` 读不回来）。"""
    payload = _write()
    with SessionLocal() as db:
        row = db.get(IncidentConclusion, payload["id"])
        assert row is not None
        assert json.loads(row.facts_json)["node_count"] == 1


# ---------- 渲染不吞内容 ----------


def test_markdown_ends_with_a_newline_and_has_no_empty_sections() -> None:
    """每个段都要有内容（哪怕是"没有…"），不留空段。

    空段会让读者以为渲染坏了，而不是"确实没有"。
    """
    markdown = _render(facts={"root_id": "r", "node_count": 0, "status_counts": {}, "timeline": [], "timeline_total": 0, "stuck_nodes": [], "open_obligations": []})
    for heading in ("## 影响面", "## 做了什么决定", "## 代价", "## 当时为什么", "## 以后什么条件", "## 这份草稿"):
        index = markdown.index(heading)
        following = markdown[index:].split("\n\n", 1)
        assert len(following) == 2 and following[1].strip(), f"{heading} 下面是空的"
    assert markdown.endswith("\n")


def test_module_does_not_depend_on_orchestrator() -> None:
    """分层：结论生成是下层模块，不许反向依赖编排层（C2 判据的本地补充）。"""
    source = pathlib.Path(conclusions.__file__).read_text(encoding="utf-8")
    assert "from .orchestrator" not in source
    assert "import orchestrator" not in source
