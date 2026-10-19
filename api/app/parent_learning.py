"""Parent review evidence and OpenViking outbox; never a self-learning store.

Opt in with CODEX_BATTLEFIELD_MEMORY_URL and CODEX_BATTLEFIELD_MEMORY_USER_ID;
use a dedicated OpenViking user per project. MEMORY_PEER_ID is an optional tag.
CODEX_BATTLEFIELD_MEMORY_API_KEY is sent only to the provider. No user configs.
The parent owns the background flush loop. External extraction can incur separate
model/network costs: archive acceptance does NOT mean learned or budget-free.
Failure is advisory, never a task completion gate. Ambiguous sends need explicit
retry; sending rows are deliberately not reclaimed automatically after a crash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .diagnostics import redact
from .experience_provider import OpenVikingExperienceProvider, ProviderConfig
from .models import LearningRecord, TaskNode, utcnow

MAX_SNAPSHOT_CHARS = 40_000
MAX_REVIEW_CHARS = 30_000
MAX_PUBLICATION_CHARS = 8_000
_flush_lock = asyncio.Lock()
_SECRET_FIELD = re.compile(r"(?i)^(?:api[_-]?key|.*(?:password|passwd|token|secret|credential)|authorization)$")


def settings_from_env() -> ProviderConfig:
    """Explicit environment only; URL + dedicated user enable memory operations."""
    peer = os.getenv("CODEX_BATTLEFIELD_MEMORY_PEER_ID", "").strip()
    user = os.getenv("CODEX_BATTLEFIELD_MEMORY_USER_ID", "").strip()
    url = os.getenv("CODEX_BATTLEFIELD_MEMORY_URL", "").strip()
    return ProviderConfig(
        url=url if user else "",
        api_key=os.getenv("CODEX_BATTLEFIELD_MEMORY_API_KEY", ""),
        peer_id=peer, user_id=user,
        target_uri=f"viking://user/{user}/memories/experiences/" if user else "",
    )


def configuration_state() -> str:
    """Safe public state; never exposes URL, key, or raw validation errors."""
    url = os.getenv("CODEX_BATTLEFIELD_MEMORY_URL", "").strip()
    peer = os.getenv("CODEX_BATTLEFIELD_MEMORY_PEER_ID", "").strip()
    user = os.getenv("CODEX_BATTLEFIELD_MEMORY_USER_ID", "").strip()
    if not url and not peer and not user and not os.getenv("CODEX_BATTLEFIELD_MEMORY_API_KEY", ""):
        return "disabled"
    if not url or not user:
        return "misconfigured"
    try:
        settings_from_env()
    except ValueError:
        return "misconfigured"
    return "configured"


def _enabled(config: ProviderConfig) -> bool:
    return bool(config.url and config.user_id)


def _provider_fingerprint(config: ProviderConfig) -> str:
    # Conservatively normalize whitespace/trailing slash only: distinct path/host
    # spelling may block a send, but must never authorize a different destination.
    normalized = config.url.strip().rstrip("/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _identity_matches(payload: dict, config: ProviderConfig) -> bool:
    return (payload.get("peer_id") == config.peer_id and payload.get("user_id") == config.user_id
            and payload.get("provider_url_sha256") == _provider_fingerprint(config))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (ValueError, TypeError):
        return default


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "***REDACTED***" if _SECRET_FIELD.match(str(k)) else _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, str):
        key = os.getenv("CODEX_BATTLEFIELD_MEMORY_API_KEY", "")
        return redact(value.replace(key, "***REDACTED***") if key else value)
    return value


def _bounded(value: dict, limit: int = MAX_SNAPSHOT_CHARS) -> dict:
    """Deterministically retain full safe JSON when small, explicit previews otherwise."""
    safe = _safe(value)
    encoded = _json(safe)
    if len(encoded) <= limit:
        return safe

    def clip(item: Any, chars: int, count: int, depth: int = 0) -> Any:
        if depth > 12:
            return {"truncated": True, "reason": "snapshot_depth_limit"}
        if isinstance(item, str) and len(item) > chars:
            return item[:chars] + "…[TRUNCATED]"
        if isinstance(item, dict):
            pairs = sorted(item.items())
            keep = max(count, len(pairs)) if depth == 0 and len(pairs) <= 100 else count
            result = {k: clip(v, chars, count, depth + 1) for k, v in pairs[:keep]}
            if len(pairs) > keep:
                result["_truncated_entries"] = len(pairs) - keep
            return result
        if isinstance(item, list):
            result = [clip(v, chars, count, depth + 1) for v in item[:count]]
            if len(item) > count:
                result.append({"truncated_entries": len(item) - count})
            return result
        return item

    chars, count = 8000, 100
    while True:
        result = clip(safe, chars, count)
        result["truncation"] = {"truncated": True, "original_chars": len(encoded), "limit": limit}
        if len(_json(result)) <= limit:
            return result
        chars, count = max(32, chars // 2), max(1, count // 2)
        if chars == 32 and count == 1:
            # Pathological enormous field names: preserve an explicit bounded preview.
            return {"truncation": result["truncation"], "snapshot_preview": encoded[:limit // 2]}


def _set_payload(row: LearningRecord, payload: dict) -> None:
    if row.kind == "review":
        # Reserve independent publication space: receipt growth cannot rewrite
        # already-published objective/facts/context evidence on subsequent loops.
        evidence = {key: value for key, value in payload.items() if key != "publication"}
        stored = _bounded(evidence, MAX_REVIEW_CHARS)
        if "publication" in payload:
            stored["publication"] = _bounded(payload["publication"], MAX_PUBLICATION_CHARS)
    else:
        stored = _bounded(payload)
    row.payload_json = _json(stored)
    row.content_sha256 = hashlib.sha256(row.payload_json.encode("utf-8")).hexdigest()
    row.updated_at = utcnow()


def _as_dict(row: LearningRecord) -> dict:
    return {
        "id": row.id, "task_id": row.task_id, "kind": row.kind, "phase": row.phase,
        "attempt_key": row.attempt_key, "status": row.status,
        "payload": _load(row.payload_json, {}), "content_sha256": row.content_sha256,
        "provider_task_id": row.provider_task_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def query_records(db: Session, task_id: str = "", limit: int = 50) -> list[dict]:
    statement = select(LearningRecord)
    if task_id:
        statement = statement.where(LearningRecord.task_id == task_id)
    rows = db.scalars(statement.order_by(LearningRecord.created_at.desc(), LearningRecord.id).limit(max(0, min(limit, 500))))
    return [_as_dict(row) for row in rows]


async def recall_for_task(session_factory, task_id: str) -> None:
    config_error = "provider_misconfigured" if configuration_state() == "misconfigured" else ""
    try:
        config = settings_from_env()
    except ValueError:
        config = ProviderConfig(url="", api_key="", peer_id="", target_uri="")
        config_error = "provider_misconfigured"
    with session_factory() as db:
        parent = db.get(TaskNode, task_id)
        if parent is None:
            return
        meta = _load(parent.node_meta_json, {})
        query = _bounded({
            "title": parent.title, "description": parent.description,
            "scope": _load(parent.scope_json, []),
            "constitution": _load(parent.constitution_json, {}),
            "project_key": meta.get("project_key", os.getenv("CODEX_BATTLEFIELD_PROJECT_KEY", "")),
        })
        previous = meta.get("experience_context")
    # The read session is closed BEFORE adapter construction or any await.
    status = "error" if config_error else "disabled"
    result: dict = {"status": status}
    if config_error:
        result["error"] = config_error
    if _enabled(config):
        try:
            result = await OpenVikingExperienceProvider(config).recall(_json(query))
            status = "error" if result.get("status") == "error" else "ok"
        except Exception:
            status, result = "error", {"status": "error", "error": "provider_recall_failed"}
    snapshot = _bounded({"status": status, "configuration_state": configuration_state(), "query": query, "result": result,
                         "entries": result.get("entries", []) if status == "ok" else [],
                         "user_id": config.user_id, "peer_id": config.peer_id,
                         "provider_url_sha256": _provider_fingerprint(config)})
    if status == "error" and previous is not None:
        snapshot["prior_context"] = previous  # inherited retrieval isn't erased by an outage
        snapshot["prior_context_preserved"] = True
        snapshot = _bounded(snapshot)
    with session_factory() as db:
        parent = db.get(TaskNode, task_id)
        if parent is not None:
            meta = _load(parent.node_meta_json, {})
            meta["experience_context"] = snapshot
            parent.node_meta_json = _json(meta)
        # A fresh attempt id preserves the query of every planning invocation.
        row = LearningRecord(id=uuid.uuid4().hex, task_id=task_id, kind="recall", phase="planning",
                             attempt_key=uuid.uuid4().hex, status=status)
        _set_payload(row, snapshot)
        db.add(row)
        db.commit()


def _context_entries(snapshot: Any) -> list:
    if not isinstance(snapshot, dict):
        return []
    if snapshot.get("status") != "ok":
        return []  # historic prior_context is evidence, never silently reactivated
    entries = snapshot.get("entries", snapshot.get("result", {}).get("entries", []))
    return entries if isinstance(entries, list) else []


def normalize_reflection(value: Any) -> dict | str | None:
    """Validate the nullable retrospective shape; bounded hypotheses, not facts.

    Legacy strings remain explicitly labeled analysis at the adapter boundary.
    Invalid objects are dropped, never repaired into invented lessons.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _safe(value[:6000])
    if not isinstance(value, dict) or set(value) != {"cause_hypothesis", "alternative_causes", "lesson", "experience_feedback"}:
        return None
    if not isinstance(value["cause_hypothesis"], str):
        return None
    alternatives = value["alternative_causes"]
    feedback = value["experience_feedback"]
    if not isinstance(alternatives, list) or not all(isinstance(item, str) for item in alternatives):
        return None
    if not isinstance(feedback, list):
        return None
    for item in feedback:
        if (not isinstance(item, dict) or set(item) != {"uri", "implemented", "observation"}
                or not isinstance(item["uri"], str)
                or (item["implemented"] is not None and type(item["implemented"]) is not bool)
                or item["observation"] not in ("helpful", "unhelpful", "unknown")):
            return None
    lesson = value["lesson"]
    if lesson is not None:
        if (not isinstance(lesson, dict) or set(lesson) != {"title", "advice", "applicability"}
                or not isinstance(lesson["title"], str) or not isinstance(lesson["advice"], str)):
            return None
        applicability = lesson["applicability"]
        if (not isinstance(applicability, dict) or set(applicability) != {"engine", "task_type", "tags"}
                or not isinstance(applicability["engine"], str) or not isinstance(applicability["task_type"], str)
                or not isinstance(applicability["tags"], list)
                or not all(isinstance(tag, str) for tag in applicability["tags"])):
            return None
        lesson = {"title": lesson["title"][:240], "advice": lesson["advice"][:2000], "applicability": {
            "engine": applicability["engine"][:100], "task_type": applicability["task_type"][:100],
            "tags": [tag[:100] for tag in applicability["tags"][:10]],
        }}
    return _safe({"cause_hypothesis": value["cause_hypothesis"][:2000],
                  "alternative_causes": [item[:1000] for item in alternatives[:5]], "lesson": lesson,
                  "experience_feedback": [{"uri": item["uri"][:1000], "implemented": item["implemented"],
                                            "observation": item["observation"]} for item in feedback[:10]]})


def record_review(db: Session, parent: TaskNode, phase: str, attempt_key: str,
                  facts: Any, analysis: Any = None) -> dict:
    """No network and no synthesized lesson; caller owns the enclosing transaction."""
    row = db.scalar(select(LearningRecord).where(
        LearningRecord.task_id == parent.id, LearningRecord.kind == "review",
        LearningRecord.attempt_key == attempt_key))
    if row is not None:
        return _as_dict(row)
    try:
        config = settings_from_env()
    except ValueError:
        config = ProviderConfig(url="", api_key="", peer_id="", target_uri="")
    row = LearningRecord(id=uuid.uuid4().hex, task_id=parent.id, kind="review", phase=phase,
                         attempt_key=attempt_key, status="pending" if _enabled(config) else
                         "error" if configuration_state() == "misconfigured" else "disabled")
    _set_payload(row, {
        "objective": parent.description, "title": parent.title,
        "scope": _load(parent.scope_json, []),
        "context": _load(parent.node_meta_json, {}).get("experience_context", {"status": "not_recalled"}),
        "facts": facts, "analysis": normalize_reflection(analysis), "learned": False,
        "user_id": config.user_id, "peer_id": config.peer_id, "configuration_state": configuration_state(),
        "provider_url_sha256": _provider_fingerprint(config),
    })
    db.add(row)
    db.flush()
    return _as_dict(row)


def _extraction_state(task_status: Any) -> str:
    if isinstance(task_status, dict):
        state = task_status.get("task_status", task_status.get("status"))
        if state in ("completed", "succeeded", "success", "done"):
            return "extraction_completed"
        if state in ("failed", "canceled", "cancelled"):
            return "extraction_failed"
    return "pending_extraction"


def _outbound_review(payload: dict) -> dict:
    return {"objective": payload.get("objective", ""), "facts": payload.get("facts"),
            "analysis": payload.get("analysis") if isinstance(payload.get("analysis"), str) else _json(payload.get("analysis")),
            "contexts": _context_entries(payload.get("context", {}))}


async def flush_pending(session_factory, limit: int = 5) -> list[dict]:
    """Serialize sends in this process; no DB transactions span provider awaits."""
    async with _flush_lock:
        try:
            config = settings_from_env()
        except ValueError:
            return []  # invalid configuration is advisory, not a parent task failure
        if not _enabled(config):
            return []  # disabled reviews are never promoted or uploaded implicitly
        provider = OpenVikingExperienceProvider(config)
        with session_factory() as db:
            ids = list(db.scalars(select(LearningRecord.id).where(
                LearningRecord.kind == "review", LearningRecord.status == "pending"
            ).order_by(LearningRecord.created_at, LearningRecord.id).limit(max(0, min(limit, 50)))))
        results = []
        if callable(getattr(provider, "get_task", None)):
            with session_factory() as db:
                polling = [(row.id, row.provider_task_id) for row in db.scalars(select(LearningRecord).where(
                    LearningRecord.kind == "review", LearningRecord.status == "pending_extraction",
                    LearningRecord.provider_task_id != "",
                ).order_by(LearningRecord.created_at, LearningRecord.id).limit(max(0, min(limit, 50))))
                    if _identity_matches(_load(row.payload_json, {}), config)]
            for record_id, task_id in polling:
                try:
                    task_status = await provider.get_task(task_id)
                except Exception:
                    task_status = {"status": "error", "error": "provider_task_query_failed"}
                with session_factory() as db:
                    row = db.get(LearningRecord, record_id)
                    if row is None or row.status != "pending_extraction":
                        continue
                    payload = _load(row.payload_json, {})
                    payload.setdefault("publication", {})["task_status"] = task_status
                    payload["learned"] = False
                    row.status = _extraction_state(task_status)
                    _set_payload(row, payload)
                    db.commit()
                    results.append(_as_dict(row))
        for record_id in ids:
            with session_factory() as db:
                row = db.get(LearningRecord, record_id)
                if row is None or row.status != "pending":
                    continue
                payload = _load(row.payload_json, {})
                if not _identity_matches(payload, config):
                    row.status = "error"
                    payload["publication"] = {"status": "error", "error": "memory_identity_configuration_changed"}
                    _set_payload(row, payload)
                    db.commit()
                    results.append(_as_dict(row))
                    continue
                identity = str(db.get_bind().url)
                session_id = "battlefield-" + hashlib.sha256(
                    _json([identity, _provider_fingerprint(config), config.user_id, config.peer_id, record_id, row.attempt_key]).encode("utf-8")
                ).hexdigest()[:40]
                # Database CAS arbitrates independent publisher processes too.
                # End the read transaction first; no stale SQLite read snapshot
                # can be upgraded to a write after another process claims it.
                db.rollback()
                claimed = db.execute(update(LearningRecord).where(
                    LearningRecord.id == record_id, LearningRecord.status == "pending"
                ).values(status="sending", updated_at=utcnow()).execution_options(synchronize_session=False))
                if claimed.rowcount != 1:
                    db.rollback()
                    continue
                row = db.get(LearningRecord, record_id)
                payload["publication"] = {"session_id": session_id, "status": "sending"}
                _set_payload(row, payload)
                # Re-read bounded persisted evidence, then fingerprint EXACTLY
                # the arguments to the adapter, not a pre-truncation Python dict.
                payload = _load(row.payload_json, {})
                outbound = _outbound_review(payload)
                outbound_sha256 = hashlib.sha256(_json(outbound).encode("utf-8")).hexdigest()
                payload["publication"]["outbound_review_sha256"] = outbound_sha256
                _set_payload(row, payload)
                db.commit()
                payload = _load(row.payload_json, {})
                outbound = _outbound_review(payload)
            try:
                published = await provider.publish_review(session_id=session_id, **outbound)
            except Exception:
                published = {"status": "pending", "archived": False, "error": "send_outcome_unknown",
                             "retry_requires_explicit_request": True}
            # Archive acceptance != extracted experiences. Never mark learned here.
            accepted = bool(published.get("archived")) and published.get("status") != "error"
            if accepted and published.get("task_id") and callable(getattr(provider, "get_task", None)):
                try:
                    published["task_status"] = await provider.get_task(published["task_id"])
                except Exception:
                    published["task_status"] = {"status": "error", "error": "provider_task_query_failed"}
            with session_factory() as db:
                row = db.get(LearningRecord, record_id)
                if row is None:
                    continue
                row.status = (_extraction_state(published.get("task_status")) if accepted else
                              "send_unknown" if published.get("status") == "pending" or
                              published.get("error") == "send_outcome_unknown" else "error")
                row.provider_task_id = str(_safe(published.get("task_id") or ""))
                payload["publication"] = {**published, "caller_session_id": session_id,
                                          "outbound_review_sha256": outbound_sha256}
                payload["learned"] = False
                _set_payload(row, payload)
                db.commit()
                results.append(_as_dict(row))
        return results


async def retry_record(session_factory, id: str) -> dict:
    """Explicit retry may resend an ambiguous archive: caller must acknowledge risk."""
    async with _flush_lock:
        with session_factory() as db:
            row = db.get(LearningRecord, id)
            if row is None:
                return {"status": "not_found", "id": id}
            try:
                config = settings_from_env()
            except ValueError:
                return _as_dict(row)
            if row.kind == "review" and row.status in {"error", "send_unknown", "sending", "disabled"} and _enabled(config):
                if row.status == "disabled":
                    payload = _load(row.payload_json, {})
                    payload["peer_id"] = config.peer_id
                    payload["user_id"] = config.user_id
                    payload["provider_url_sha256"] = _provider_fingerprint(config)
                    _set_payload(row, payload)
                row.status = "pending"
                row.updated_at = utcnow()
                db.commit()
            return _as_dict(row)
