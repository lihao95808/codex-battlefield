"""结论文档端点的接线判据（P4-5）。

`test_conclusions.py` 钉的是**纯逻辑**；这里钉的是**接线**：真实事件表 → 草稿里
决策点对不对、落库 → 读回来/导出对不对。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| M1 | 草稿**不落库** | 每点一下草稿就在归档区留一条，很快没法看 |
| M2 | 决策点真的从事件表抽出来（含全局事件） | 时间线永远空，而 `incomplete` 会把它说成"可能没记上" |
| M3 | 只能看到**这棵树**的事件 | 归档文档里混进别的树的事故 |
| M4 | `authored` 为空 → 400 | 只有事实的文档被当成结论 |
| M5 | 导出是 `.md` 且带 `Content-Disposition` | "可归档"只是口号 |
| M6 | 写结论**不要求关管家只读** | 要归档先关只读 → 没人记录复盘 |
| M7 | 没有树时草稿给 404（不是 500） | 空工程点一下就报错 |
| M8 | 详情/导出找不到时 404 | 归档链接点开 500 |
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus


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


def _seed(root_id: str = "root-1", *, extra_tree: str = "") -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(TaskNode(id=root_id, title="根", description="", depth=0, status=TaskStatus.WAITING_CHILDREN.value))
        db.add(
            TaskNode(
                id=f"{root_id}-child",
                parent_id=root_id,
                title="子",
                description="",
                depth=1,
                status=TaskStatus.BLOCKED.value,
                error_message="缺依赖",
                leaf_attempts=2,
            )
        )
        if extra_tree:
            db.add(TaskNode(id=extra_tree, title="别的树", description="", depth=0, status=TaskStatus.DONE.value))
        db.add_all(
            [
                EventRecord(task_id=root_id, type="task.created", message="建节点", created_at=now),
                EventRecord(task_id=root_id, type="constitution.locked", message="宪法已锁定", created_at=now),
                EventRecord(
                    task_id=None, type="obligation.settled_human", message="人工清债（actor=me）", created_at=now
                ),
                EventRecord(task_id=f"{root_id}-child", type="worker.blocked", message="worker 阻塞", created_at=now),
            ]
        )
        if extra_tree:
            db.add(
                EventRecord(task_id=extra_tree, type="task.canceled", message="别的树被取消", created_at=now)
            )
        db.commit()


# ---------- M1 草稿不落库 ----------


def test_m1_draft_does_not_write_anything(client: TestClient) -> None:
    """生成草稿**不许**在库里留记录。

    这条是负向判据里最要紧的：草稿是给人改的中间产物，每次点一下就落一条的话，
    归档区很快会被草稿塞满，而"归档"也就失去意义了。
    """
    _seed()
    before = client.get("/api/conclusions").json()["conclusions"]
    response = client.get("/api/conclusions/draft?root_id=root-1")
    assert response.status_code == 200, response.text
    after = client.get("/api/conclusions").json()["conclusions"]

    assert before == [] and after == [], "草稿被写进库了"
    body = response.json()
    assert body["facts"]["node_count"] == 2
    assert "没有落库" in body["note"]


def test_m1_draft_says_the_authored_section_is_empty(client: TestClient) -> None:
    """草稿的"当时为什么"必须是空的占位 —— 那是要人来写的。"""
    _seed()
    markdown = client.get("/api/conclusions/draft?root_id=root-1").json()["markdown"]
    assert "## 当时为什么这么决定（**人写**）" in markdown
    assert "还没写" in markdown


# ---------- M2 决策点 ----------


def test_m2_decision_points_come_from_the_event_table(client: TestClient) -> None:
    _seed()
    body = client.get("/api/conclusions/draft?root_id=root-1").json()
    types = [item["type"] for item in body["facts"]["timeline"]]
    assert "constitution.locked" in types
    assert "obligation.settled_human" in types
    # 非决策事件不进时间线。
    assert "task.created" not in types
    assert "worker.blocked" not in types


def test_m2_global_decision_events_are_included(client: TestClient) -> None:
    """`task_id` 为空的决策事件（宪法锁定、人工清债）**必须**收进来。

    它们常常不属于某个节点，漏掉正好漏掉最该进时间线的东西。
    """
    _seed()
    body = client.get("/api/conclusions/draft?root_id=root-1").json()
    global_ones = [item for item in body["facts"]["timeline"] if not item["node_id"]]
    assert global_ones, "全局决策事件没有进时间线"


def test_m2_impact_counts_and_stuck_nodes_are_reported(client: TestClient) -> None:
    _seed()
    facts = client.get("/api/conclusions/draft?root_id=root-1").json()["facts"]
    assert facts["node_count"] == 2
    assert facts["stuck_nodes"][0]["id"] == "root-1-child"
    assert facts["stuck_nodes"][0]["error"] == "缺依赖"


# ---------- M3 只收这棵树 ----------


def test_m3_events_from_another_tree_are_excluded(client: TestClient) -> None:
    """别的树的事件不许出现在这份归档里。"""
    _seed(extra_tree="other-root")
    body = client.get("/api/conclusions/draft?root_id=root-1").json()
    messages = [item["message"] for item in body["facts"]["timeline"]]
    assert "别的树被取消" not in messages


# ---------- M4 必须有人写的一段 ----------


def test_m4_writing_without_authored_text_is_refused(client: TestClient) -> None:
    _seed()
    response = client.post(
        "/api/conclusions",
        json={"root_id": "root-1", "title": "一次卡住", "authored": "   "},
    )
    assert response.status_code == 400, response.text
    assert "事件表" in response.json()["detail"] or "人写" in response.json()["detail"]
    assert client.get("/api/conclusions").json()["conclusions"] == []


def test_m4_writing_persists_and_returns_the_markdown(client: TestClient) -> None:
    _seed()
    draft = client.get("/api/conclusions/draft?root_id=root-1").json()
    created = client.post(
        "/api/conclusions",
        json={
            "root_id": "root-1",
            "title": "一次卡住",
            "author": "duzuguang",
            "authored": "我当时以为依赖已经装好了。",
            "facts": draft["facts"],
            "incomplete": draft["incomplete"],
            "status": "final",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "final"
    assert "把依赖装好" not in body["markdown"]
    assert "我当时以为依赖已经装好了。" in body["markdown"]

    listed = client.get("/api/conclusions?root_id=root-1").json()["conclusions"]
    assert [item["id"] for item in listed] == [body["id"]]


# ---------- M5 导出 ----------


def test_m5_export_is_markdown_with_a_filename(client: TestClient) -> None:
    _seed()
    created = client.post(
        "/api/conclusions",
        json={"root_id": "root-1", "authored": "复盘内容", "title": "导出测试"},
    ).json()

    exported = client.get(f"/api/conclusions/{created['id']}/export")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "conclusion-" in exported.headers["content-disposition"]
    assert "# 导出测试" in exported.text
    assert "generated by codex-battlefield" in exported.text


# ---------- M6 写结论不受管家只读限制 ----------


def test_m6_writing_a_conclusion_does_not_require_disabling_steward_read_only() -> None:
    """`steward_read_only` 保持默认（开）时，写结论仍然可以。

    只读禁的是"替用户动树"，归档一份文档不动任何东西。要求先关只读才能记录复盘，
    结果只会是没人记录复盘。
    """
    _seed()
    from dataclasses import replace

    from app.main import app
    from app.orchestrator import orchestrator

    old = orchestrator.settings
    orchestrator.settings = replace(old, steward_read_only=True)
    try:
        response = TestClient(app).post(
            "/api/conclusions",
            json={"root_id": "root-1", "authored": "只读模式下也要能记录", "title": "只读"},
        )
    finally:
        orchestrator.settings = old

    assert response.status_code == 200, response.text


# ---------- M7 / M8 找不到就是 404 ----------


def test_m7_draft_without_any_tree_is_404(client: TestClient) -> None:
    response = client.get("/api/conclusions/draft")
    assert response.status_code == 404
    assert "没有任务树" in response.json()["detail"]


def test_m8_missing_conclusion_is_404_not_500(client: TestClient) -> None:
    assert client.get("/api/conclusions/999999").status_code == 404
    assert client.get("/api/conclusions/999999/export").status_code == 404


# ---------- 其他 ----------


def test_draft_for_a_specific_missing_node_falls_back_and_says_so(client: TestClient) -> None:
    _seed()
    body = client.get("/api/conclusions/draft?root_id=root-1&node_id=nope").json()
    assert body["facts"]["node_count"] == 2
    assert any("不在这棵树里" in text for text in body["incomplete"])


def test_draft_takes_the_latest_tree_when_root_is_omitted(client: TestClient) -> None:
    _seed()
    body = client.get("/api/conclusions/draft").json()
    assert body["root_id"] == "root-1"


def test_conclusion_title_can_be_changed_on_update(client: TestClient) -> None:
    _seed()
    created = client.post(
        "/api/conclusions",
        json={"root_id": "root-1", "authored": "第一版", "title": "旧标题"},
    ).json()

    updated = client.post(
        "/api/conclusions",
        json={
            "root_id": "root-1",
            "authored": "第二版",
            "title": "新标题",
            "conclusion_id": created["id"],
        },
    ).json()

    assert updated["id"] == created["id"]
    assert updated["title"] == "新标题"
    assert "# 新标题" in updated["markdown"]
    assert "第二版" in updated["markdown"]
