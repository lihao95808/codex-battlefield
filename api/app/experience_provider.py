"""Opt-in OpenViking v0.5.0 HTTP experience adapter; no local semantic engine.

The SDK is not a runtime dependency, so this is an independently written thin
HTTP integration, not copied OpenViking implementation code. Contract references:
https://github.com/volcengine/OpenViking/blob/v0.5.0/docs/en/api/05-sessions.md
https://github.com/volcengine/OpenViking/blob/v0.5.0/openviking/server/routers/search.py
https://github.com/volcengine/OpenViking/blob/v0.5.0/openviking/server/routers/content.py

Recall proves only that content was read, never that it was applied. Commit
acceptance proves archiving, not completed extraction or useful learning.
OpenViking's experiences policy also enables cases/trajectories internally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}\Z")
_MEMORY_POLICY = {"self": {"enabled": True}, "peer": {"enabled": False}, "memory_types": ["experiences"]}


@dataclass(frozen=True)
class ProviderConfig:
    url: str
    api_key: str = field(repr=False)
    peer_id: str
    target_uri: str
    timeout_seconds: float = 5
    max_chars: int = 6000
    limit: int = 5
    user_id: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            return
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
            valid = (
                parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
                and "?" not in self.url and "#" not in self.url
                and "\\" not in self.url
                and not any(ord(c) <= 32 or ord(c) == 127 for c in self.url)
                and (port is None or 0 < port <= 65535)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("invalid_provider_url")
        if not _safe_segment(self.user_id):
            raise ValueError("invalid_provider_user")
        if self.peer_id != "" and not _safe_segment(self.peer_id):
            raise ValueError("invalid_provider_peer")
        if self.target_uri != f"viking://user/{self.user_id}/memories/experiences/":
            raise ValueError("invalid_provider_scope")
        if not isinstance(self.api_key, str) or any(ord(c) < 32 or ord(c) == 127 for c in self.api_key):
            raise ValueError("invalid_provider_key")
        if self.api_key.strip() and parsed.scheme == "http" and not _loopback(parsed.hostname):
            raise ValueError("insecure_provider_key")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60
            or type(self.max_chars) is not int or not 1 <= self.max_chars <= 100_000
            or type(self.limit) is not int or not 1 <= self.limit <= 100
        ):
            raise ValueError("invalid_provider_bounds")


def _safe_segment(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_SEGMENT.fullmatch(value)) and value not in {".", ".."}


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class _Failure(Exception):
    """Only constant codes cross the public boundary; never exception messages."""


class OpenVikingExperienceProvider:
    """Explicit configuration only: never reads environment or private configs.

    Use a dedicated project USER key or dedicated per-project service. v0.5.0
    trains experiences at user scope: peer-only policies exclude training cases.
    Actor-peer headers/tags are metadata, never project isolation.
    A remote session is reserved for one review per (user, peer, caller session_id).
    Existing archived reviews are not appended again. Ambiguous partial batches
    fail closed. The per-instance lock prevents duplicate concurrent publishes;
    cross-process exactly-once requires caller serialization (HTTP has no CAS).
    """

    def __init__(self, config: ProviderConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._transport = transport
        self._publish_lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        # api_key mode derives identity from the key and ignores this header.
        # Dev/trusted modes accept it; it is not an authentication credential.
        headers = {"X-OpenViking-User": self.config.user_id}
        if self.config.peer_id:
            headers["X-OpenViking-Actor-Peer"] = self.config.peer_id
        if self.config.api_key.strip():
            headers["X-API-Key"] = self.config.api_key
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/") + "/", headers=headers,
            timeout=httpx.Timeout(self.config.timeout_seconds), follow_redirects=False,
            transport=self._transport, trust_env=False,
        )

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, *, missing_ok: bool = False, **kwargs: Any) -> Any:
        try:
            # Total deadline also bounds slow streaming, not just each socket read.
            async with asyncio.timeout(self.config.timeout_seconds):
                async with client.stream(method, "api/v1/" + path, **kwargs) as response:
                    if missing_ok and response.status_code == 404:
                        return None
                    if not 200 <= response.status_code < 300:
                        raise _Failure("provider_unauthorized" if response.status_code in {401, 403} else "provider_http_error")
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > MAX_RESPONSE_BYTES:
                        raise _Failure("provider_response_too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise _Failure("provider_response_too_large")
                        body.extend(chunk)
                    payload = json.loads(body)
                    if not isinstance(payload, dict) or payload.get("status") != "ok" or "result" not in payload:
                        raise _Failure("provider_invalid_response")
                    return payload["result"]
        except _Failure:
            raise
        except (httpx.TransportError, TimeoutError):
            raise _Failure("send_outcome_unknown" if method == "POST" and path.startswith("sessions") else "provider_timeout") from None
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, httpx.HTTPError):
            raise _Failure("provider_invalid_response") from None

    def _scoped_uri(self, uri: Any) -> bool:
        if (
            not isinstance(uri, str) or len(uri) > 4096
            or any(c in uri for c in ("%", "\\", "?", "#"))
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in uri)
        ):
            return False
        try:
            parsed = urlsplit(uri)
        except ValueError:
            return False
        segments = parsed.path.split("/")
        # Canonical exact project-user scope; peers are NOT an isolation boundary.
        if parsed.scheme != "viking" or parsed.netloc != "user" or len(segments) < 5:
            return False
        if segments[0] != "" or not _safe_segment(segments[1]):
            return False
        # Official extraction permits language-localized filenames. Identity
        # remains strict ASCII, but path components may contain literal Unicode.
        if any(not s or len(s) > 255 or s in {".", ".."} for s in segments[4:]):
            return False
        return (
            segments[1:4] == [self.config.user_id, "memories", "experiences"]
            and segments[-1] not in {".abstract.md", ".overview.md"}
        )

    async def recall(self, query: str) -> dict[str, Any]:
        if not self.config.url:
            return {"status": "disabled", "entries": []}
        if not isinstance(query, str) or not query.strip():
            return {"status": "error", "entries": [], "error": "invalid_query"}
        try:
            async with self._client() as client:
                result = await self._request(client, "POST", "search/find", json={
                    "query": query[:self.config.max_chars], "target_uri": self.config.target_uri,
                    "limit": self.config.limit, "context_type": "memory", "level": 2,
                })
                if not isinstance(result, dict) or not isinstance(result.get("memories"), list):
                    raise _Failure("provider_invalid_response")
                entries = []
                seen = set()
                remaining = self.config.max_chars
                for hit in result["memories"]:
                    if len(entries) >= self.config.limit or remaining <= 0:
                        break
                    if not isinstance(hit, dict):
                        raise _Failure("provider_invalid_response")
                    uri = hit.get("uri")
                    if not self._scoped_uri(uri) or uri in seen:
                        continue
                    seen.add(uri)
                    content = await self._request(client, "GET", "content/read", params={"uri": uri})
                    # No summary/inline fallback: only the explicit full L2 read counts.
                    if not isinstance(content, str):
                        raise _Failure("provider_invalid_response")
                    score = hit.get("score", 0)
                    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                        raise _Failure("provider_invalid_response")
                    entries.append({"uri": uri, "content": content[:remaining],
                                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "score": score})
                    remaining -= len(entries[-1]["content"])
                return {"status": "ok", "entries": entries}
        except _Failure as exc:
            return {"status": "error", "entries": [], "error": exc.args[0]}
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return {"status": "error", "entries": [], "error": "provider_invalid_response"}

    async def get_task(self, task_id: str) -> dict[str, Any]:
        """Observe extraction task state, never whether an experience was learned.

        v0.5.0: GET /api/v1/tasks/{task_id}. Remote result/error strings are
        deliberately not forwarded because they can contain private information.
        """
        if not self.config.url:
            return {"status": "error", "error": "provider_disabled", "learned": False}
        if not _safe_segment(task_id):
            return {"status": "error", "error": "invalid_task_id", "learned": False}
        try:
            async with self._client() as client:
                task = await self._request(client, "GET", "tasks/" + task_id)
            if (
                not isinstance(task, dict) or task.get("task_id") != task_id
                or task.get("status") not in {"pending", "queued", "running", "cancelling", "completed", "failed", "cancelled"}
            ):
                raise _Failure("provider_invalid_response")
            result = {"status": task["status"], "task_id": task_id, "learned": False}
            if task["status"] in {"failed", "cancelled"}:
                result["error"] = "provider_task_failed" if task["status"] == "failed" else "provider_task_cancelled"
            return result
        except _Failure as exc:
            return {"status": "error", "error": exc.args[0], "learned": False}
        except (ValueError, TypeError):
            return {"status": "error", "error": "provider_invalid_response", "learned": False}

    def _review_messages(self, objective: str, facts: Any, analysis: str, contexts: Any) -> list[dict[str, Any]]:
        if not isinstance(objective, str) or not isinstance(analysis, str) or not isinstance(contexts, (list, tuple)):
            raise _Failure("invalid_review")
        try:
            evidence = facts if isinstance(facts, str) else json.dumps(facts, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise _Failure("invalid_review") from None
        context_parts = []
        for item in contexts[:self.config.limit]:
            uri = item.get("uri") if isinstance(item, dict) else item
            if not self._scoped_uri(uri):
                raise _Failure("invalid_context_scope")
            context_parts.append({"type": "context", "uri": uri, "context_type": "memory",
                                  "abstract": "Read context only; application and causality are not established."})
        n = self.config.max_chars
        return [
            {"role": "user", "peer_id": self.config.peer_id,
             "parts": [{"type": "text", "text": "Objective:\n" + objective[:n]}]},
            {"role": "assistant", "peer_id": self.config.peer_id,
             "parts": [{"type": "text", "text": "Analysis / hypotheses (not established facts):\n" + analysis[:n]}, *context_parts]},
            {"role": "assistant", "peer_id": self.config.peer_id,
             "parts": [{"type": "tool", "tool_id": "review_evidence", "tool_name": "review_evidence",
                        "tool_status": "completed", "tool_output": "Reported tool evidence (not independently verified):\n" + evidence[:n]}]},
        ]

    def _require_expected_user(self, session: dict[str, Any]) -> None:
        user = session.get("user")
        if not isinstance(user, dict) or user.get("user_id") != self.config.user_id:
            raise _Failure("provider_identity_mismatch")

    async def publish_review(self, session_id: str, objective: str, facts: Any, analysis: str, contexts: Any) -> dict[str, Any]:
        if not self.config.url:
            return {"status": "error", "error": "provider_disabled"}
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "error", "error": "invalid_session_id"}
        remote_id = "battlefield-review-" + hashlib.sha256(
            json.dumps([self.config.user_id, self.config.peer_id, session_id], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        base = {"session_id": remote_id, "task_id": None, "archive_uri": None}
        try:
            messages = self._review_messages(objective, facts, analysis, contexts)
            async with self._publish_lock, self._client() as client:
                path = "sessions/" + remote_id
                existing = await self._request(client, "GET", path, params={"auto_create": "false"}, missing_ok=True)
                if existing is None:
                    created = await self._request(client, "POST", "sessions", json={
                        "session_id": remote_id, "memory_policy": _MEMORY_POLICY, "auto_commit_policy": None,
                    })
                    if not isinstance(created, dict) or created.get("session_id") != remote_id:
                        raise _Failure("provider_invalid_response")
                    self._require_expected_user(created)
                    live = 0
                else:
                    if not isinstance(existing, dict) or existing.get("session_id") != remote_id:
                        raise _Failure("provider_invalid_response")
                    self._require_expected_user(existing)
                    commits = existing.get("commit_count")
                    live = existing.get("message_count")
                    if type(commits) is not int or commits < 0 or type(live) is not int or live < 0:
                        raise _Failure("provider_invalid_response")
                    if commits > 0:
                        return {**base, "status": "archived", "archived": True, "extraction_status": "unknown"}
                    # Only resume the verified project user's self-only experience policy.
                    policy = existing.get("memory_policy")
                    if (
                        not isinstance(policy, dict)
                        or policy.get("self") != {"enabled": True}
                        or policy.get("peer") != {"enabled": False}
                        or policy.get("working_memory", {"enabled": False}) != {"enabled": False}
                        or not isinstance(policy.get("memory_types"), list)
                        or not all(isinstance(t, str) for t in policy["memory_types"])
                        or sorted(policy["memory_types"]) not in [["experiences"], ["cases", "experiences", "trajectories"]]
                        or existing.get("auto_commit_policy") is not None
                    ):
                        raise _Failure("existing_session_policy_mismatch")
                    # Counts do not prove payload identity. An existing live batch
                    # may belong to a different review even when its count matches.
                    # Without a verified digest, require manual reconciliation.
                    if live > 0:
                        return {**base, "status": "pending", "archived": False,
                                "extraction_status": "unknown", "error": "send_outcome_unknown"}
                if live == 0:
                    batch = await self._request(client, "POST", path + "/messages/batch", json={"messages": messages})
                    if not isinstance(batch, dict) or batch.get("added") != len(messages):
                        raise _Failure("send_outcome_unknown")
                committed = await self._request(client, "POST", path + "/commit", json={"keep_recent_count": 0})
                if (
                    not isinstance(committed, dict) or committed.get("status") != "accepted"
                    or committed.get("archived") is not True
                    or not isinstance(committed.get("task_id"), str) or not committed["task_id"]
                    or not isinstance(committed.get("archive_uri"), str) or not committed["archive_uri"]
                ):
                    raise _Failure("send_outcome_unknown")
                return {**base, "status": "pending", "archived": True, "extraction_status": "pending",
                        "task_id": committed["task_id"], "archive_uri": committed["archive_uri"]}
        except _Failure as exc:
            if exc.args[0] == "send_outcome_unknown":
                return {**base, "status": "pending", "archived": False, "extraction_status": "unknown", "error": "send_outcome_unknown"}
            return {**base, "status": "error", "error": exc.args[0]}
