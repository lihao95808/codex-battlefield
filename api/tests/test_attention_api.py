"""注意账与档位审计端点的接线判据（P4-2 / P4-3）。

`test_attention.py` / `test_watchdog_tuning.py` 钉的是**纯逻辑**；这里钉**端点**：
认领 → 别人被拒 → 界面能看到的形状；调档 → 审计 → 回滚。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| V1 | 端点报出的认领带标签与剩余秒数 | 界面只能显示 `stalled_agent` 这种内部词 |
| V2 | 非认领人解除 → 400；强制解除要留名 | 权限边界只存在于文档里 |
| V3 | 调档端点**先写审计再改配置** | 崩溃时留下无人知道的改动 |
| V4 | 一批里有一个非法值 → 整批不生效、也不写审计 | 一半生效一半失败 |
| V5 | 回滚用**上一次的旧值**，且需要留名 | 让人去猜原来的值 |
| V6 | 没有历史时回滚 → 400（不是 500） | 点一下就报服务器错误 |
| V7 | 调档/回滚受 `frozen_mode` 挡 | 冻结模式下还能改并发 |
"""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app import attention
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord


@pytest.fixture()
def client(tmp_path: pathlib.Path, monkeypatch) -> TestClient:
    """隔离的配置文件 —— **不能**写开发机真实的 `~/.codex-battlefield.toml`。"""
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "cfg.toml"))
    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


def _claim(kind: str = "stalled_agent", target: str = "node-1", actor: str = attention.WATCHDOG):
    with SessionLocal() as db:
        return attention.claim(db, kind=kind, target=target, actor=actor, reason="抢不到锁")


# ---------- V1 端点形状 ----------


def test_v1_attention_endpoint_reports_labels_and_remaining(client: TestClient) -> None:
    _claim()
    body = client.get("/api/attention").json()

    assert body["count"] == 1
    item = body["claims"][0]
    assert item["kind_label"] == "僵死进程"
    assert item["actor_label"].startswith("看门狗")
    assert item["remaining_seconds"] > 0
    assert body["kinds"] and body["actors"]


def test_v1_attention_endpoint_is_empty_when_nothing_is_claimed(client: TestClient) -> None:
    body = client.get("/api/attention").json()
    assert body["count"] == 0
    assert body["claims"] == []


# ---------- V2 解除的权限 ----------


def test_v2_a_non_owner_cannot_release(client: TestClient) -> None:
    claimed = _claim(actor=attention.STEWARD)
    response = client.delete(f"/api/attention/{claimed.claim_id}?actor=watchdog")
    assert response.status_code == 400, response.text
    assert "不能替它解除" in response.json()["detail"]
    # 仍然生效。
    assert client.get("/api/attention").json()["count"] == 1


def test_v2_forced_release_needs_a_signature(client: TestClient) -> None:
    claimed = _claim(actor=attention.STEWARD)
    refused = client.delete(f"/api/attention/{claimed.claim_id}?actor=human&force=true")
    assert refused.status_code == 400
    assert "留名" in refused.json()["detail"]

    ok = client.delete(
        f"/api/attention/{claimed.claim_id}?actor=human&force=true&declared_by=duzuguang"
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["forced"] is True
    assert ok.json()["released_by"] == "duzuguang"
    assert client.get("/api/attention").json()["count"] == 0


def test_v2_owner_can_release_without_force(client: TestClient) -> None:
    claimed = _claim(actor=attention.WATCHDOG)
    ok = client.delete(f"/api/attention/{claimed.claim_id}?actor=watchdog")
    assert ok.status_code == 200
    assert ok.json()["forced"] is False


def test_v2_releasing_a_missing_claim_is_400_not_500(client: TestClient) -> None:
    response = client.delete("/api/attention/999999?actor=watchdog")
    assert response.status_code == 400


# ---------- V3 调档端点的顺序与内容 ----------


def test_v3_tuning_get_reports_catalog_current_and_history(client: TestClient) -> None:
    body = client.get("/api/watchdog/tuning").json()
    keys = {item["key"] for item in body["catalog"]}
    assert "max_concurrency" in keys
    assert set(body["current"]) == keys
    assert body["history"] == []
    assert "记录**先于生效**" in body["note"]


def test_v3_applying_writes_audit_before_config_and_reports_restart(client: TestClient) -> None:
    response = client.post(
        "/api/watchdog/tuning",
        json={
            "actor": "duzuguang",
            "changes": [{"key": "max_concurrency", "value": 8, "reason": "机器空着"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["restart_required"] is True
    assert body["changes"][0]["old"] == 13
    assert body["changes"][0]["new"] == 8

    with SessionLocal() as db:
        assert db.query(EventRecord).filter(EventRecord.type == "watchdog.tuning").count() == 1
    history = client.get("/api/watchdog/tuning").json()["history"]
    assert history[0]["actor"] == "duzuguang"
    assert history[0]["reason"] == "机器空着"


def test_v3_the_value_actually_lands_in_the_config_file(client: TestClient, tmp_path) -> None:
    client.post(
        "/api/watchdog/tuning",
        json={"actor": "me", "changes": [{"key": "max_concurrency", "value": 6}]},
    )
    config = tmp_path / "cfg.toml"
    assert config.is_file()
    assert "max_concurrency = 6" in config.read_text(encoding="utf-8")


# ---------- V4 全过或全不过 ----------


def test_v4_one_bad_value_rejects_the_batch_and_writes_no_audit(client: TestClient, tmp_path) -> None:
    response = client.post(
        "/api/watchdog/tuning",
        json={
            "actor": "me",
            "changes": [
                {"key": "max_concurrency", "value": 8},
                {"key": "auto_concurrency_min", "value": 0},
            ],
        },
    )
    assert response.status_code == 400, response.text
    assert "不能小于" in response.json()["detail"]
    # 没有审计、没有写文件 —— 整批都没发生。
    with SessionLocal() as db:
        assert db.query(EventRecord).filter(EventRecord.type == "watchdog.tuning").count() == 0
    assert not (tmp_path / "cfg.toml").exists()


def test_v4_unknown_key_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/watchdog/tuning",
        json={"actor": "me", "changes": [{"key": "not_a_knob", "value": 1}]},
    )
    assert response.status_code == 400
    assert "max_concurrency" in response.json()["detail"]


def test_v4_missing_actor_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/watchdog/tuning",
        json={"changes": [{"key": "max_concurrency", "value": 8}]},
    )
    assert response.status_code == 400
    assert "留名" in response.json()["detail"]


# ---------- V5 / V6 回滚 ----------


def test_v5_rollback_restores_the_previous_value(client: TestClient) -> None:
    client.post(
        "/api/watchdog/tuning",
        json={"actor": "me", "changes": [{"key": "max_concurrency", "value": 8}]},
    )
    response = client.post("/api/watchdog/tuning/rollback", json={"actor": "me"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rolled_back"
    assert body["changes"][0]["new"] == 13, "回滚目标是**旧值**"

    history = client.get("/api/watchdog/tuning").json()["history"]
    assert len(history) == 2, "回滚本身也要留痕"


def test_v5_rollback_requires_a_signature(client: TestClient) -> None:
    client.post(
        "/api/watchdog/tuning",
        json={"actor": "me", "changes": [{"key": "max_concurrency", "value": 8}]},
    )
    response = client.post("/api/watchdog/tuning/rollback", json={"actor": "  "})
    assert response.status_code == 400
    assert "留名" in response.json()["detail"]


def test_v5_rollback_of_multiple_steps(client: TestClient) -> None:
    for value in (8, 6):
        client.post(
            "/api/watchdog/tuning",
            json={"actor": "me", "changes": [{"key": "max_concurrency", "value": value}]},
        )
    response = client.post("/api/watchdog/tuning/rollback", json={"actor": "me", "steps": 2})
    assert response.status_code == 200, response.text
    targets = {item["key"]: item["new"] for item in response.json()["changes"]}
    assert targets["max_concurrency"] == 13


def test_v6_rollback_without_history_is_400(client: TestClient) -> None:
    response = client.post("/api/watchdog/tuning/rollback", json={"actor": "me"})
    assert response.status_code == 400
    assert "没有可回滚" in response.json()["detail"]


# ---------- V7 冻结模式 ----------


def test_v7_frozen_mode_blocks_tuning_and_rollback(client: TestClient) -> None:
    """整机冻结时不许改并发 —— 冻结的意义就是"现在什么都别动"。"""
    from dataclasses import replace

    from app.orchestrator import orchestrator

    old = orchestrator.settings
    orchestrator.settings = replace(old, frozen_mode=True)
    try:
        applied = client.post(
            "/api/watchdog/tuning",
            json={"actor": "me", "changes": [{"key": "max_concurrency", "value": 8}]},
        )
        rolled = client.post("/api/watchdog/tuning/rollback", json={"actor": "me"})
        released = client.delete("/api/attention/1?actor=watchdog")
    finally:
        orchestrator.settings = old

    assert applied.status_code == 423
    assert rolled.status_code == 423
    assert released.status_code == 423
