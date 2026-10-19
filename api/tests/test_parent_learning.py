"""Own disposable SQLite engine: never touches app.database or shared fixtures."""
import asyncio
import hashlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app import parent_learning as learning
from app.models import Base, TaskNode


@pytest.fixture
def store(tmp_path, monkeypatch):
    for name in ("URL", "API_KEY", "PEER_ID", "USER_ID"):
        monkeypatch.delenv("CODEX_BATTLEFIELD_MEMORY_" + name, raising=False)
    engine = create_engine(f"sqlite:///{(tmp_path / 'evidence.sqlite3').as_posix()}")
    @event.listens_for(engine, "connect")
    def foreign_keys(conn, record):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    active = []
    class TrackedSession(Session):
        def __enter__(self):
            active.append(self)
            return super().__enter__()
        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                active.remove(self)
    factory = sessionmaker(bind=engine, class_=TrackedSession, expire_on_commit=False)
    with factory() as db:
        db.add(TaskNode(id="parent", title="Integrate", description="Ship verified integration",
                        scope_json='["src"]', constitution_json='{"goal":"ship"}',
                        node_meta_json='{"unrelated":{"preserve":true},"project_key":"test-project"}'))
        db.commit()
    yield factory, active
    engine.dispose()


def enable(monkeypatch):
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_PEER_ID", "test-peer")
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_USER_ID", "test-user")
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_API_KEY", "private-key-value")


def records(factory):
    with factory() as db:
        return learning.query_records(db)


async def test_disabled_is_no_network_and_metadata_is_preserved(store, monkeypatch):
    factory, _ = store
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", lambda *_: pytest.fail("disabled provider constructed"))
    await learning.recall_for_task(factory, "parent")
    with factory() as db:
        meta = json.loads(db.get(TaskNode, "parent").node_meta_json)
        assert meta["unrelated"] == {"preserve": True}
        assert meta["experience_context"]["status"] == "disabled"
        review = learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    assert review["status"] == "disabled"
    assert await learning.flush_pending(factory) == []
    assert review["payload"]["learned"] is False


async def test_recall_snapshot_query_content_sha_and_error_inheritance(store, monkeypatch):
    factory, active = store
    enable(monkeypatch)
    content = "Full L2 evidence, not a synthesized lesson"
    reply = {"status": "ok", "entries": [{"uri": "viking://user/test-user/memories/experiences/e1",
             "content": content, "sha256": hashlib.sha256(content.encode()).hexdigest()}]}
    calls = []
    class Provider:
        def __init__(self, config):
            assert not active
            assert config.target_uri == "viking://user/test-user/memories/experiences/"
            assert config.user_id == "test-user"
        async def recall(self, query):
            assert not active
            calls.append(json.loads(query))
            await asyncio.sleep(0)
            if len(calls) > 1:
                raise RuntimeError("private-key-value network error")
            return reply
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    await learning.recall_for_task(factory, "parent")
    first = records(factory)[0]
    assert first["status"] == "ok"
    assert first["payload"]["result"] == reply
    assert first["payload"]["entries"] == reply["entries"]
    assert calls[0]["constitution"] == {"goal": "ship"}
    assert calls[0]["project_key"] == "test-project"
    await learning.recall_for_task(factory, "parent")
    all_records = records(factory)
    assert len(all_records) == 2
    error = next(row for row in all_records if row["status"] == "error")
    assert error["payload"]["prior_context"]["result"] == reply
    assert error["payload"]["prior_context_preserved"] is True
    assert error["payload"]["entries"] == []
    assert learning._context_entries(error["payload"]) == []
    assert "private-key-value" not in json.dumps(all_records)
    with factory() as db:
        meta = json.loads(db.get(TaskNode, "parent").node_meta_json)
        assert meta["unrelated"] == {"preserve": True}
        assert meta["experience_context"]["status"] == "error"


def test_review_idempotent_objective_facts_not_lessons_survive_delete(store, monkeypatch):
    factory, _ = store
    enable(monkeypatch)
    facts = {"run_id": 7, "integration_report": {"exit_code": 1, "checks": ["failed"]}, "api_key": "another-private-value"}
    with factory() as db:
        parent = db.get(TaskNode, "parent")
        first = learning.record_review(db, parent, "integration", "report:7", facts, "possible conflict")
        second = learning.record_review(db, parent, "integration", "report:7", {"different": True})
        assert first["id"] == second["id"]
        assert first["payload"]["facts"]["integration_report"] == facts["integration_report"]
        assert first["payload"]["analysis"] == "possible conflict"
        assert first["payload"]["learned"] is False
        assert "lessons" not in first["payload"]
        db.delete(parent)
        db.commit()
    assert len(records(factory)) == 1
    assert "another-private-value" not in json.dumps(records(factory))


def test_snapshot_bound_explicit_and_deterministic(store):
    factory, _ = store
    with factory() as db:
        parent = db.get(TaskNode, "parent")
        one = learning.record_review(db, parent, "integration", "big:1", {"run_id": 1, "logs": "证据" * 100_000})
        two = learning.record_review(db, parent, "integration", "big:2", {"run_id": 1, "logs": "证据" * 100_000})
        db.commit()
    assert one["payload"] == two["payload"]
    assert one["payload"]["truncation"]["truncated"] is True
    assert len(learning._json(one["payload"])) <= learning.MAX_SNAPSHOT_CHARS
    assert one["content_sha256"] == hashlib.sha256(learning._json(one["payload"]).encode()).hexdigest()


async def test_flush_no_db_await_serialized_accepted_not_learned(store, monkeypatch):
    factory, active = store
    enable(monkeypatch)
    calls = []
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            assert not active
            calls.append(kwargs)
            await asyncio.sleep(0.01)
            return {"status": "pending", "archived": True, "extraction_status": "pending", "task_id": "extract-1"}
        async def get_task(self, task_id):
            assert not active
            assert task_id == "extract-1"
            return {"status": "completed"}
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    with factory() as db:
        learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1}, "hypothesis only")
        db.commit()
    outputs = await asyncio.gather(learning.flush_pending(factory), learning.flush_pending(factory))
    assert len(calls) == 1
    assert sum(len(result) for result in outputs) == 1
    row = records(factory)[0]
    assert row["status"] == "extraction_completed"
    assert row["provider_task_id"] == "extract-1"
    assert row["payload"]["learned"] is False
    assert row["payload"]["publication"]["task_status"] == {"status": "completed"}
    assert "private-key-value" not in json.dumps(row)


async def test_transport_error_requires_explicit_retry_with_same_session(store, monkeypatch):
    factory, active = store
    enable(monkeypatch)
    sessions = []
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            assert not active
            sessions.append(kwargs["session_id"])
            raise RuntimeError("private-key-value ambiguous send")
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    with factory() as db:
        row = learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    await learning.flush_pending(factory)
    assert records(factory)[0]["status"] == "send_unknown"
    assert await learning.flush_pending(factory) == []
    assert len(sessions) == 1
    assert (await learning.retry_record(factory, row["id"]))["status"] == "pending"
    await learning.flush_pending(factory)
    assert sessions[0] == sessions[1]
    assert "private-key-value" not in json.dumps(records(factory))


async def test_enabling_later_does_not_silently_upload_disabled_rows(store, monkeypatch):
    factory, _ = store
    with factory() as db:
        learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    enable(monkeypatch)
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            pytest.fail("disabled evidence uploaded without explicit retry")
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    assert await learning.flush_pending(factory) == []
    assert records(factory)[0]["status"] == "disabled"
    row_id = records(factory)[0]["id"]
    retried = await learning.retry_record(factory, row_id)
    assert retried["status"] == "pending"
    assert retried["payload"]["peer_id"] == "test-peer"


async def test_context_entries_forwarded_without_snapshot_shape_confusion(store, monkeypatch):
    factory, _ = store
    enable(monkeypatch)
    entry = {"uri": "viking://user/test-user/memories/experiences/e1", "content": "Evidence", "sha256": "digest"}
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            assert kwargs["contexts"] == []  # stale prior snapshot is not automatically reactivated
            assert kwargs["analysis"] == "unproven"
            return {"status": "pending", "archived": True, "task_id": "extract-2"}
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    with factory() as db:
        parent = db.get(TaskNode, "parent")
        parent.node_meta_json = json.dumps({"experience_context": {"status": "error", "prior_context_preserved": True,
            "prior_context": {"status": "ok", "result": {"entries": [entry]}}}})
        learning.record_review(db, parent, "integration", "report:1", {"integration_report": {"success": True}}, "unproven")
        db.commit()
    assert (await learning.flush_pending(factory))[0]["status"] == "pending_extraction"


def test_reflection_schema_allowlist_bounded_nullable_no_invented_lesson():
    reflection = {"cause_hypothesis": "maybe" * 1000, "alternative_causes": ["alternative"] * 20,
                  "lesson": None, "experience_feedback": [{"uri": "viking://example", "implemented": None, "observation": "unknown"}]}
    bounded = learning.normalize_reflection(reflection)
    assert len(bounded["cause_hypothesis"]) == 2000
    assert len(bounded["alternative_causes"]) == 5
    assert bounded["lesson"] is None
    assert bounded["experience_feedback"][0]["implemented"] is None
    assert learning.normalize_reflection({**reflection, "secret": "ignored"}) is None
    assert learning.normalize_reflection({**reflection, "lesson": {"title": "unsupported"}}) is None
    assert learning.normalize_reflection(None) is None
    assert learning.normalize_reflection({**reflection, "experience_feedback": [{"uri": "x", "implemented": 1, "observation": "helpful"}]}) is None


@pytest.mark.parametrize("terminal,expected", [("completed", "extraction_completed"), ("failed", "extraction_failed")])
async def test_future_flush_polls_extraction_without_republication(store, monkeypatch, terminal, expected):
    factory, active = store
    enable(monkeypatch)
    polls = []
    sends = []
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            assert not active
            sends.append(kwargs)
            return {"status": "pending", "archived": True, "task_id": "extract-later"}
        async def get_task(self, task_id):
            assert not active
            polls.append(task_id)
            return {"status": "running" if len(polls) == 1 else terminal}
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    with factory() as db:
        learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    assert (await learning.flush_pending(factory))[0]["status"] == "pending_extraction"
    assert (await learning.flush_pending(factory))[0]["status"] == expected
    assert len(sends) == 1
    assert len(polls) == 2
    assert records(factory)[0]["payload"]["learned"] is False
    assert await learning.flush_pending(factory) == []


@pytest.mark.parametrize("changed", ["USER_ID", "PEER_ID", "URL"])
async def test_pending_review_cannot_cross_changed_identity(store, monkeypatch, changed):
    factory, _ = store
    enable(monkeypatch)
    with factory() as db:
        learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_" + changed,
                       "http://127.0.0.1:9998" if changed == "URL" else "different-identity")
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            pytest.fail("review crossed project memory identity")
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    output = await learning.flush_pending(factory)
    assert output[0]["status"] == "error"
    assert output[0]["payload"]["publication"]["error"] == "memory_identity_configuration_changed"
    assert output[0]["payload"]["user_id"] == "test-user"
    assert output[0]["payload"]["provider_url_sha256"] == hashlib.sha256(b"http://127.0.0.1:9999").hexdigest()
    assert "http://127.0.0.1" not in json.dumps(output)


async def test_db_cas_lost_claim_never_publishes(store, monkeypatch):
    factory, _ = store
    enable(monkeypatch)
    with factory() as db:
        row = learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:1", {"run_id": 1})
        db.commit()
    original_execute = factory.class_.execute
    competed = []
    def racing_execute(self, statement, *args, **kwargs):
        if getattr(statement, "is_update", False) and not competed:
            competed.append(True)
            # Simulate another process claiming between our read and CAS.
            with factory() as other:
                other_row = other.get(learning.LearningRecord, row["id"])
                other_row.status = "sending"
                other.commit()
        return original_execute(self, statement, *args, **kwargs)
    monkeypatch.setattr(factory.class_, "execute", racing_execute)
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, **kwargs):
            pytest.fail("CAS loser published the same evidence")
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    assert await learning.flush_pending(factory) == []
    assert competed
    assert records(factory)[0]["status"] == "sending"
    assert await learning.flush_pending(factory) == []  # no stale sending reclaim


async def test_large_outbound_matches_persisted_evidence_receipt_cannot_rewrite_it(store, monkeypatch):
    factory, _ = store
    enable(monkeypatch)
    captured = []
    with factory() as db:
        row = learning.record_review(db, db.get(TaskNode, "parent"), "acceptance", "run:big",
                                     {"run_id": 1, "output": "e" * 39000}, "hypothesis")
        db.commit()
    before = row["payload"]
    assert before["truncation"]["truncated"] is True
    assert len(learning._json(before)) <= learning.MAX_REVIEW_CHARS
    class Provider:
        def __init__(self, config):
            pass
        async def publish_review(self, session_id, **kwargs):
            saved = records(factory)[0]["payload"]
            assert kwargs == learning._outbound_review(saved)
            digest = hashlib.sha256(learning._json(kwargs).encode()).hexdigest()
            assert saved["publication"]["outbound_review_sha256"] == digest
            captured.append(digest)
            return {"status": "pending", "archived": True, "task_id": "extract-large", "huge_receipt": "r" * 50000}
    monkeypatch.setattr(learning, "OpenVikingExperienceProvider", Provider)
    after = (await learning.flush_pending(factory))[0]["payload"]
    assert {key: value for key, value in after.items() if key != "publication"} == before
    assert after["publication"]["outbound_review_sha256"] == captured[0]
    assert after["publication"]["truncation"]["truncated"] is True
    assert len(learning._json(after)) <= learning.MAX_SNAPSHOT_CHARS


def test_destination_fingerprint_normalizes_trailing_slash_only(store, monkeypatch):
    enable(monkeypatch)
    first = learning._provider_fingerprint(learning.settings_from_env())
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_URL", "  http://127.0.0.1:9999/  ")
    assert learning._provider_fingerprint(learning.settings_from_env()) == first
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_URL", "http://127.0.0.1:9999/other")
    assert learning._provider_fingerprint(learning.settings_from_env()) != first


def test_peer_is_optional_not_security_boundary(store, monkeypatch):
    enable(monkeypatch)
    monkeypatch.delenv("CODEX_BATTLEFIELD_MEMORY_PEER_ID")
    config = learning.settings_from_env()
    assert config.peer_id == ""
    assert config.user_id == "test-user"
    assert learning.configuration_state() == "configured"
    assert learning._enabled(config)


async def test_invalid_config_is_advisory_recall_error_and_url_alone_disabled(store, monkeypatch):
    factory, _ = store
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_URL", "http://127.0.0.1:9999")
    assert learning.settings_from_env().url == ""
    assert learning.configuration_state() == "misconfigured"
    await learning.recall_for_task(factory, "parent")
    assert records(factory)[0]["payload"]["configuration_state"] == "misconfigured"
    enable(monkeypatch)
    monkeypatch.setenv("CODEX_BATTLEFIELD_MEMORY_URL", "not-a-url")
    await learning.recall_for_task(factory, "parent")
    assert records(factory)[0]["status"] == "error"
    assert await learning.flush_pending(factory) == []
