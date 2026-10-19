"""管家的判据（`docs/spec-steward.md`）。

管家是一个**只读的全局对话 agent**。它最容易犯的错不是"答不上来"，
而是下面三类 —— 本文件就是钉这三类：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| S1 | 只读模式下，改状态的能力**明确不可用** | agent 会以为自己能改，或假装改了 |
| S2 | 状态快照**不编**：取不到的项标 `available: false` | 假 0 会被用户读成"没花钱/没有债" |
| S3 | 契约不符时**仍能回话**，不把 500 抛给用户 | 一次 schema 不符就变成对话崩了 |
| S4 | 轮廓截断要**说出来** | agent 会以为看到的就是全部 |
| S5 | 对话**可审计**：谁说了什么、当时是不是只读，都落库 | 出问题查不到它当时看到什么 |
"""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import steward
from app.config import get_settings
from app.database import get_db
from app.models import Base, TaskNode, TaskStatus, utcnow
from app.steward_capabilities import CAPABILITIES, catalog, mutating_names
from app.steward_status import MAX_OUTLINE_NODES, build_status
from app.steward import parse_chat


def _settings(**overrides):
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


@pytest.fixture(scope="function")
def session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    """Use a fresh database, not bulk DELETEs against other tests' FK-linked state."""
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'steward.sqlite3').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    try:
        # Importing get_db registers the production Engine connect hook, including FK enforcement.
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        Base.metadata.create_all(bind=engine)
        yield factory
    finally:
        engine.dispose()


# ---------- S1 权限声明 ----------


def test_s1_read_only_disables_every_mutating_capability() -> None:
    """只读模式下，**每一个**改状态的能力都必须 available=false。

    逐个核对而不是抽查：漏掉一个就是一个没被声明的写入口。
    """
    caps = catalog(read_only=True)["capabilities"]
    mutating = mutating_names()
    assert mutating, "目录里应该有会改状态的能力（否则这个判据没意义）"

    for cap in caps:
        if cap["mutating"]:
            assert cap["available"] is False, f"{cap['name']} 在只读模式下仍然可用"
            assert cap["when_unavailable"], f"{cap['name']} 没解释为什么不可用"
        else:
            assert cap["available"] is True, f"{cap['name']} 是只读的，不该被禁用"


def test_s1_write_mode_enables_them_again() -> None:
    """对照：放开写权限后它们确实可用（证明上一条不是恒真）。"""
    caps = catalog(read_only=False)["capabilities"]
    enabled = {c["name"] for c in caps if c["available"]}
    assert mutating_names() <= enabled


def test_s1_every_capability_declares_its_effect() -> None:
    """每条能力都要声明 mutating，且改状态的要有 rationale。

    声明与权限在**同一份定义**里：分成两处写迟早漂移，
    而漂移的后果是"文档说只读，实际能改"。
    """
    for cap in CAPABILITIES:
        assert isinstance(cap.mutating, bool), f"{cap.name} 没声明 mutating"
        if cap.mutating:
            assert cap.rationale, f"{cap.name} 会改状态却没说明为什么需要它"


# ---------- S2 快照不编 ----------


def test_s2_snapshot_is_honest_about_missing_usage(session_factory: sessionmaker[Session]) -> None:
    """空库上用量必须报 `available: false`，**不能**填 0。

    填 0 会被读成"没花钱"，而真相是"没有数据"—— 用户会基于它做决定。
    """
    with session_factory() as db:
        snapshot = build_status(db, _settings())

    assert snapshot["usage"]["available"] is False, "空库不该声称有用量数据"
    assert snapshot["counts"]["total"] >= 0
    assert "state_home" in snapshot["tool"]


def test_s2_snapshot_shape_is_stable(session_factory: sessionmaker[Session]) -> None:
    """快照的顶层键是**给 agent 的契约**，不能悄悄变。

    变了而没人知道 -> agent 会去读一个不存在的字段，然后编一个值出来。
    """
    with session_factory() as db:
        snapshot = build_status(db, _settings())

    for key in ("tool", "counts", "blocked_nodes", "usage", "recent_events", "outline"):
        assert key in snapshot, f"快照缺字段 {key}"
    for key in ("total", "done", "blocked", "failed", "running"):
        assert key in snapshot["counts"], f"counts 缺字段 {key}"


# ---------- S3 契约不符仍能回话 ----------


def test_s3_broken_payload_still_produces_a_reply() -> None:
    """payload 不符契约时**必须还能回话**，而不是把 500 抛给用户。

    管家是给人用的对话。一次 schema 不符不该让对话崩掉 ——
    退化成"我解析不了，已记录"，用户至少知道发生了什么。
    """
    chat = parse_chat({"totally": "wrong"})
    assert chat.reply, "解析失败时 reply 不能为空"
    assert chat.confidence == "low"


def test_s3_payload_with_text_still_surfaces_it() -> None:
    """如果原始输出里至少有可读文本，要把它露出来，而不是丢掉。"""
    chat = parse_chat({"text": "这是模型的原话"})
    assert "这是模型的原话" in chat.reply


def test_s3_valid_payload_passes_through() -> None:
    """对照：合法 payload 正常解析（证明上面不是恒真）。"""
    chat = parse_chat(
        {
            "reply": "好的",
            "situation": "树空着",
            "proposed_actions": [{"action": "kick_root", "target": "r1", "why": "卡住了", "risk": "花 token"}],
            "needs_user_input": False,
            "confidence": "high",
        }
    )
    assert chat.reply == "好的"
    assert chat.confidence == "high"
    assert len(chat.proposed_actions) == 1


# ---------- S4 截断要说出来 ----------


def test_s4_outline_truncation_is_reported(session_factory: sessionmaker[Session]) -> None:
    """轮廓被截断时必须给出 `outline_truncated` 与省略数量。

    否则 agent 会以为"这就是整棵树"，进而做出"树已经完整/没有遗漏"的错误结论。
    这里直接构造一个超过上限的树来验证 —— 不能只测空库那种恒真情形。
    """
    with session_factory() as db:
        # 造 MAX_OUTLINE_NODES + 5 个平铺节点（都挂在 None 下）
        for index in range(MAX_OUTLINE_NODES + 5):
            db.add(
                TaskNode(
                    id=f"n{index:05d}",
                    parent_id=None,
                    title=f"节点 {index}",
                    depth=0,
                    status=TaskStatus.PLANNED.value,
                    created_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
        db.commit()

        snapshot = build_status(db, _settings())

        assert snapshot["counts"]["total"] == MAX_OUTLINE_NODES + 5
        assert len(snapshot["outline"]) <= MAX_OUTLINE_NODES
        assert snapshot["outline_truncated"] is True, "截断了却没说"
        assert snapshot["outline_omitted"] == snapshot["counts"]["total"] - len(snapshot["outline"])


def test_s4_no_truncation_flag_when_everything_fits(session_factory: sessionmaker[Session]) -> None:
    """对照：没截断时标志必须是 false。"""
    with session_factory() as db:
        snapshot = build_status(db, _settings())
        assert snapshot["outline_truncated"] is False
        assert snapshot["outline_omitted"] == 0


# ---------- 对话落库与历史 ----------


def test_history_renders_in_speaking_order(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as db:
        steward.append_message(db, role=steward.ROLE_USER, content="第一句")
        steward.append_message(db, role=steward.ROLE_STEWARD, content="第二句")

        rows = steward.recent_messages(db)
        text = steward.render_history(rows)

    assert text.index("用户：第一句") < text.index("管家：第二句"), "历史顺序反了"


def test_clear_history_only_clears_the_conversation(session_factory: sessionmaker[Session]) -> None:
    """清空对话**不能**顺手动树 —— 用户说"忘掉我们聊的"，不是"把活删了"。"""
    with session_factory() as db:
        db.add(
            TaskNode(
                id="keepme",
                parent_id=None,
                title="必须留着",
                depth=0,
                status=TaskStatus.PLANNED.value,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )
        db.commit()
        steward.append_message(db, role=steward.ROLE_USER, content="hello")

        removed = steward.clear_history(db)
        assert removed >= 1
        assert db.get(TaskNode, "keepme") is not None, "清对话把树也删了"


# ---------- 端点接线 ----------


@pytest.fixture()
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    from app.main import app

    def isolated_db() -> Generator[Session, None, None]:
        with session_factory() as db:
            yield db

    # Keep the real endpoint dependency wiring; restore any previous override on teardown.
    monkeypatch.setitem(app.dependency_overrides, get_db, isolated_db)
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()


def test_endpoints_chat_roundtrip_and_persist(client: TestClient) -> None:
    """接线判据：POST 一句话 -> 拿回回复 -> GET 能看到两条。

    用 mock runner（默认 `CODEX_BATTLEFIELD_RUNNER=mock`），
    所以这里测的是**接线与落库**，不是模型质量。
    """
    response = client.post("/api/steward/chat", json={"message": "状态怎么样？"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"], "回复不能为空"
    assert body["read_only_mode"] is True

    listing = client.get("/api/steward/messages").json()
    roles = [item["role"] for item in listing["messages"]]
    assert roles[-2:] == ["user", "steward"], f"对话没落库：{roles}"

    # metadata 要留下"当时是不是只读"——审计要用的就是这一条
    last = listing["messages"][-1]
    assert last["metadata"].get("read_only_mode") is True


def test_endpoint_rejects_empty_message(client: TestClient) -> None:
    assert client.post("/api/steward/chat", json={"message": "   "}).status_code == 422


def test_capabilities_endpoint_matches_read_only_mode(client: TestClient) -> None:
    body = client.get("/api/steward/capabilities").json()
    assert body["read_only_mode"] is True
    mutating = [c for c in body["capabilities"] if c["mutating"]]
    assert mutating, "目录里应有改状态的能力"
    assert all(not c["available"] for c in mutating), "只读模式下仍然放行了写能力"
