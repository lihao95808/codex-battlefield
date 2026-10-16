"""预授权端点的接线判据（P4-4）。

`test_action_risk.py` 钉的是**判定**；这里钉的是**接线**：从 HTTP 面授予一条授权之后，
提议的动作到底有没有被自动批准、有没有写审计依据、高风险有没有真的被挡住。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| AP1 | 默认（无授权）提议落成 `pending` | 出厂即放宽权限 |
| AP2 | 命中授权的提议落成 `approved` 并写下依据 | 事后无法回答"这次是谁批的" |
| AP3 | **高风险动作即使被授权也仍是 `pending`** | 一次授权就能全停 |
| AP4 | 授予非法授权（高危/无期限/无署名）→ 400 且**不生效** | 界面显示"已授权"，实际没生效或生效过头 |
| AP5 | 撤销后立刻回到"要人确认" | 撤销是假动作 |
| AP6 | **只读模式不被预授权越过** | 管家只读形同虚设 |
| AP7 | 目录端点把三档与"永远不能预授权"报出来 | 用户看不到"哪些动作危险" |
| AP8 | 目标范围真的生效（越界目标不被自动批准） | 授权针对 A 却能让 B 被操作 |
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import steward_actions
from app.database import SessionLocal, engine, init_db
from app.models import Base, StewardAction


@pytest.fixture()
def client() -> TestClient:
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    """清库 + 清预授权 + **显式关掉管家只读**。

    为什么要动 `steward_read_only`：授权/撤销这两个端点走 `_ensure_steward_mutable`，
    而它**默认是只读**（`steward_read_only` 默认 True，见领域文档）。也就是说
    **默认配置下根本授不出预授权** —— 这是有意的一道闸（放宽权限这件事本身
    也要先有权限）。测试要么显式开，要么就只能测 423。

    用完还原：这个开关是进程级 settings，泄漏给别的用例会让它们突然能改权限。
    """
    from dataclasses import replace

    from app.orchestrator import orchestrator

    Base.metadata.drop_all(bind=engine)
    init_db()
    steward_actions.pre_authorization.clear()

    old = orchestrator.settings
    orchestrator.settings = replace(old, steward_read_only=False)
    try:
        yield
    finally:
        orchestrator.settings = old
        Base.metadata.drop_all(bind=engine)
        steward_actions.pre_authorization.clear()


def test_default_configuration_refuses_to_grant_at_all(client: TestClient) -> None:
    """**默认只读时授不出预授权** —— 这条本身是判据，不是测试脚手架。

    放宽权限这件事自己也要先有权限：如果默认只读还能授出预授权，
    那"默认只读"就只是一个界面文案。
    """
    from dataclasses import replace

    from app.orchestrator import orchestrator

    # 本模块的 fixture 把只读关掉了，这里临时还原成出厂值。
    orchestrator.settings = replace(orchestrator.settings, steward_read_only=True)
    try:
        response = client.post(
            "/api/steward/pre-authorization",
            json={
                "actions": ["kick_root"],
                "minutes": 10,
                "granted_by": "me",
                "reason": "r",
            },
        )
    finally:
        orchestrator.settings = replace(orchestrator.settings, steward_read_only=False)

    assert response.status_code == 423, response.text
    assert "只读" in response.json()["detail"]


def _propose(items: list[dict]) -> list[StewardAction]:
    with SessionLocal() as db:
        rows, _ = steward_actions.propose(db, items, read_only=False)
        for row in rows:
            db.refresh(row)
        # 出 session 前把值取出来（row 与 session 绑定）。
        for row in rows:
            _ = (row.id, row.action, row.status, row.result, row.target)
        return list(rows)


def _grant(client: TestClient, **payload) -> None:
    body = {
        "actions": ["kick_root", "retry_node"],
        "targets": ["node-a"],
        "minutes": 30,
        "granted_by": "duzuguang",
        "reason": "长跑期间自动续跑",
    }
    body.update(payload)
    response = client.post("/api/steward/pre-authorization", json=body)
    assert response.status_code == 200, response.text


# ---------- AP1 默认无授权 ----------


def test_ap1_without_a_grant_every_proposal_waits_for_a_human(client: TestClient) -> None:
    body = client.get("/api/steward/pre-authorization").json()
    assert body["grant"] is None
    assert body["high_risk_never_pre_authorized"] is True

    rows = _propose([{"action": "kick_root", "target": "node-a", "why": "推进"}])
    assert [row.status for row in rows] == [steward_actions.PENDING]
    # 既有约定：被拒/待批的动作 `result` 为空（它记的是**执行结果**）。
    assert rows[0].result == ""


# ---------- AP2 命中授权 ----------


def test_ap2_a_matching_proposal_is_auto_approved_with_its_basis(client: TestClient) -> None:
    _grant(client)
    rows = _propose([{"action": "kick_root", "target": "node-a", "why": "推进"}])

    assert rows[0].status == steward_actions.APPROVED
    assert "[预授权]" in rows[0].result
    assert "duzuguang" in rows[0].result, "审计依据里没有署名"
    assert steward_actions.auto_approved(rows) == [rows[0]]


# ---------- AP3 高风险永不被自动批准 ----------


def test_ap3_high_risk_stays_pending_even_when_explicitly_granted(client: TestClient) -> None:
    """**这条是 P4-4 的核心**：请求里写了 `cancel_all`，端点必须 400，且不生效。

    然后在**没有**那条授权的情况下再提议 `cancel_all` —— 它必须是 `pending`。
    """
    bad = client.post(
        "/api/steward/pre-authorization",
        json={
            "actions": ["cancel_all"],
            "targets": [],
            "minutes": 30,
            "granted_by": "duzuguang",
            "reason": "图省事",
        },
    )
    assert bad.status_code == 400, bad.text
    assert "高风险" in bad.json()["detail"]
    assert client.get("/api/steward/pre-authorization").json()["grant"] is None, (
        "被拒的授权竟然生效了"
    )

    # 用一个**合法**授权，再提议高风险动作：仍然必须等人点。
    _grant(client)
    rows = _propose([{"action": "cancel_all", "target": "node-a", "why": "收工"}])
    assert rows[0].status == steward_actions.PENDING
    assert rows[0].result == ""


# ---------- AP4 非法授权 ----------


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"minutes": 0}, "期限"),
        ({"granted_by": "  "}, "署名"),
        ({"actions": ["not_an_action"]}, "未知"),
        ({"actions": []}, "至少要"),
        ({"actions": ["cancel_node"]}, "高风险"),
    ],
)
def test_ap4_invalid_grants_are_refused_and_do_not_take_effect(
    client: TestClient, payload: dict, fragment: str
) -> None:
    body = {
        "actions": ["kick_root"],
        "targets": [],
        "minutes": 10,
        "granted_by": "me",
        "reason": "r",
    }
    body.update(payload)
    response = client.post("/api/steward/pre-authorization", json=body)
    assert response.status_code == 400, response.text
    assert fragment in response.json()["detail"]
    assert client.get("/api/steward/pre-authorization").json()["grant"] is None


# ---------- AP5 撤销 ----------


def test_ap5_clearing_returns_to_per_action_confirmation(client: TestClient) -> None:
    _grant(client)
    assert _propose([{"action": "kick_root", "target": "node-a", "why": "x"}])[0].status == (
        steward_actions.APPROVED
    )

    cleared = client.delete("/api/steward/pre-authorization")
    assert cleared.status_code == 200
    assert "撤销" in cleared.json()["message"]
    assert client.get("/api/steward/pre-authorization").json()["grant"] is None

    assert _propose([{"action": "kick_root", "target": "node-a", "why": "x"}])[0].status == (
        steward_actions.PENDING
    )


# ---------- AP6 只读模式不被越过 ----------


def test_ap6_read_only_mode_is_not_bypassed_by_a_grant() -> None:
    """`read_only=True` 时**不自动批准**。

    只读是"管家能不能代你动树"的开关，预授权是"要不要逐次确认"的开关 ——
    两件事。混在一起的话，开一次预授权就把只读模式废掉了。
    """
    grant = steward_actions.action_risk.make_grant(
        actions=["kick_root"], minutes=10, granted_by="me", reason="r"
    )
    steward_actions.pre_authorization.set(grant)
    try:
        with SessionLocal() as db:
            rows, _ = steward_actions.propose(
                db, [{"action": "kick_root", "target": "node-a", "why": "x"}], read_only=True
            )
            assert rows[0].status == steward_actions.PENDING
            assert rows[0].result == ""
    finally:
        steward_actions.pre_authorization.clear()


# ---------- AP7 目录 ----------


def test_ap7_catalog_reports_levels_and_the_absolute_rule(client: TestClient) -> None:
    body = client.get("/api/steward/pre-authorization").json()

    assert {item["level"] for item in body["levels"]} == {"low", "medium", "high"}
    for item in body["levels"]:
        assert item["label"] and item["rationale"]

    catalog = {item["action"]: item for item in body["catalog"]}
    assert catalog["cancel_all"]["risk"] == "high"
    assert catalog["cancel_all"]["pre_authorizable"] is False
    assert catalog["kick_root"]["risk"] == "low"
    assert catalog["kick_root"]["pre_authorizable"] is True

    # 危险的在前面：用户从上往下看就该先看到不能预授权的那些。
    assert body["catalog"][0]["risk"] == "high"
    assert "确认次数" in body["note"]


def test_ap7_catalog_covers_every_proposable_action(client: TestClient) -> None:
    from app.steward_actions import known_actions

    catalog = {item["action"] for item in client.get("/api/steward/pre-authorization").json()["catalog"]}
    assert catalog == set(known_actions())


# ---------- AP8 目标范围 ----------


def test_ap8_target_scope_is_enforced(client: TestClient) -> None:
    _grant(client, targets=["node-a"])
    rows = _propose(
        [
            {"action": "kick_root", "target": "node-a", "why": "在范围内"},
            {"action": "kick_root", "target": "node-b", "why": "越界"},
        ]
    )
    by_target = {row.target: row.status for row in rows}
    assert by_target["node-a"] == steward_actions.APPROVED
    assert by_target["node-b"] == steward_actions.PENDING


def test_ap8_empty_targets_means_any_target(client: TestClient) -> None:
    _grant(client, targets=[])
    rows = _propose([{"action": "retry_node", "target": "whatever", "why": "不限目标"}])
    assert rows[0].status == steward_actions.APPROVED
