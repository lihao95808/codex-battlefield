"""Isolated contract/security tests: no app/config/model/DB imports or live calls.

Run: api/.venv/Scripts/python.exe -m pytest --noconftest -o addopts= \
     api/tests/test_experience_provider.py
"""

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "_isolated_experience_provider", Path(__file__).parents[1] / "app" / "experience_provider.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
ProviderConfig = _MODULE.ProviderConfig
OpenVikingExperienceProvider = _MODULE.OpenVikingExperienceProvider
ROOT = "viking://user/alice/memories/experiences/"
URI = ROOT + "review.md"
POLICY = {"self": {"enabled": True}, "peer": {"enabled": False}, "memory_types": ["experiences"]}


def config(**kwargs):
    return ProviderConfig(**{"url": "http://127.0.0.1:1933", "api_key": "secret-key",
                             "peer_id": "battlefield", "target_uri": ROOT, "user_id": "alice", **kwargs})


def ok(result):
    return httpx.Response(200, json={"status": "ok", "result": result})


def provider(handler, **kwargs):
    return OpenVikingExperienceProvider(config(**kwargs), transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


def test_disabled_is_distinct_from_empty_results_and_does_not_read_configs(monkeypatch):
    monkeypatch.setenv("OPENVIKING_URL", "https://private.invalid")
    p = OpenVikingExperienceProvider(ProviderConfig("", "", "", ""), transport=httpx.MockTransport(
        lambda request: pytest.fail("disabled provider must not send")
    ))
    assert run(p.recall("query")) == {"status": "disabled", "entries": []}
    assert run(p.publish_review("local", "objective", [], "analysis", [])) == {
        "status": "error", "error": "provider_disabled"
    }
    assert run(provider(lambda request: ok({"memories": []})).recall("query")) == {
        "status": "ok", "entries": []
    }


@pytest.mark.parametrize("kwargs", [
    {"url": "file:///private"}, {"url": "https://user:password@example.org"},
    {"url": "https://example.org?key=secret"}, {"url": "https://example.org?"},
    {"url": "https://example.org#anchor"}, {"url": "https://example.org:bad"},
    {"url": "https://example.org\\private"}, {"url": "https://example.org\n"},
    {"url": "http://example.org"}, {"url": "http://127.0.0.1.example.org"},
    {"user_id": ""}, {"user_id": "../other"}, {"user_id": "bob"},
    {"peer_id": "other/peer"}, {"peer_id": ".."},
    {"target_uri": "viking://~/memories/experiences/"},
    {"target_uri": "viking://~/peers/other/memories/experiences/"},
    {"timeout_seconds": 0}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 61},
    {"max_chars": 0}, {"limit": 0}, {"api_key": "secret\r\nInjected: yes"},
])
def test_invalid_enabled_config_fails_without_echoing_secret(kwargs):
    with pytest.raises(ValueError) as exc:
        config(**kwargs)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("url,key", [
    ("https://example.org", "secret"), ("http://localhost:1933", "secret"),
    ("http://127.0.0.2", "secret"), ("http://[::1]:1933", "secret"),
    ("http://example.org", ""), ("http://example.org", "   "),
])
def test_valid_config_and_key_not_in_repr(url, key):
    assert "secret" not in repr(config(url=url, api_key=key))


def test_recall_exact_requests_scoped_full_content_hash_and_total_character_budget():
    calls = []
    uri2 = URI.replace("review.md", "second.md")
    content = "complete first read αβγ"

    def handler(request):
        calls.append(request)
        assert request.headers["X-API-Key"] == "secret-key"
        assert request.headers["X-OpenViking-Actor-Peer"] == "battlefield"
        assert request.headers["X-OpenViking-User"] == "alice"
        assert "authorization" not in request.headers
        if request.url.path == "/api/v1/search/find":
            assert request.method == "POST"
            assert json.loads(request.content) == {"query": "1234567890", "target_uri": ROOT,
                                                  "limit": 5, "context_type": "memory", "level": 2}
            return ok({"memories": [{"uri": URI, "score": .8, "abstract": "NOT FULL CONTENT"},
                                    {"uri": uri2, "score": .7}]})
        assert request.method == "GET"
        assert request.url.path == "/api/v1/content/read"
        assert dict(request.url.params) == {"uri": URI}
        return ok(content)

    result = run(provider(handler, max_chars=10).recall("12345678901234567890"))
    assert result == {"status": "ok", "entries": [{"uri": URI, "content": content[:10],
                         "sha256": hashlib.sha256(content.encode()).hexdigest(), "score": .8}]}
    assert len(calls) == 2
    assert "applied" not in result


@pytest.mark.parametrize("bad_uri", [
    "viking://user/alice/peers/other/memories/experiences/review.md",
    "viking://user/alice/peers/battlefield2/memories/experiences/review.md",
    "viking://user/bob/memories/experiences/review.md",
    "viking://user/alice2/memories/experiences/review.md",
    "viking://user/alice/peers/battlefield/memories/experiences/review.md",
    "viking://~/peers/battlefield/memories/experiences/review.md",
    "viking://~/memories/experiences/review.md",
    "viking://user/alice/memories/experiences/../review.md",
    "viking://user/alice/memories/experiences/%2e%2e/review.md",
    "viking://user/alice/memories/experiences/ok%252f..%252fother",
    "viking://user/alice/memories/experiences//review.md",
    "viking://user/alice/memories/experiences/review.md?key=x",
    "viking://user/alice/memories/experiences/review.md#x",
    "viking://user/alice/memories/experiences/\\other",
    "viking://user/alice/memories/experiences/.abstract.md",
    "viking://user/alice/memories/experiences/.overview.md",
    "viking://[invalid/", None,
])
def test_cross_peer_alias_traversal_and_summary_uris_never_read(bad_uri):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/v1/search/find"
        return ok({"memories": [{"uri": bad_uri, "abstract": "summary"}]})

    assert run(provider(handler).recall("query")) == {"status": "ok", "entries": []}
    assert len(calls) == 1


def test_official_unicode_experience_filename_read_and_context_reference():
    uri = ROOT + "csv导入bom修复.md"
    content = "## Situation\n- CSV 导入含 BOM\n## Approach\n- 使用 utf-8-sig"

    def handler(request):
        if request.method == "POST":
            return ok({"memories": [{"uri": uri, "score": 0.9}]})
        assert request.url.params["uri"] == uri
        return ok(content)

    p = provider(handler)
    result = run(p.recall("CSV BOM"))
    assert result["entries"] == [{"uri": uri, "content": content,
                                  "sha256": hashlib.sha256(content.encode()).hexdigest(), "score": 0.9}]
    assert p._review_messages("objective", [], "analysis", [{"uri": uri}])[1]["parts"][1]["uri"] == uri


@pytest.mark.parametrize("suffix", ["../csv导入.md", "%2e%2e/csv导入.md", "csv\n导入.md",
                                    "csv\t导入.md", "csv\\导入.md", "csv\x7f导入.md", "x" * 256,
                                    "\ud800.md", "nested//csv导入.md"])
def test_unicode_paths_do_not_relax_traversal_controls(suffix):
    p = provider(lambda request: pytest.fail("no network expected"))
    assert not p._scoped_uri(ROOT + suffix)
    assert not p._scoped_uri((ROOT + "csv导入.md").replace("alice", "bob"))


def test_total_chars_across_entries_and_deduplicate_reads():
    reads = []
    uri2 = URI.replace("review.md", "second.md")

    def handler(request):
        if request.method == "POST":
            return ok({"memories": [{"uri": URI}, {"uri": URI}, {"uri": uri2}]})
        reads.append(request.url.params["uri"])
        return ok("abcd")

    result = run(provider(handler, max_chars=6).recall("q"))
    assert [entry["content"] for entry in result["entries"]] == ["abcd", "ab"]
    assert reads == [URI, uri2]
    assert result["entries"][1]["sha256"] == hashlib.sha256(b"abcd").hexdigest()


@pytest.mark.parametrize("response", [
    httpx.Response(401, text="secret-key"), httpx.Response(403, text="secret-key"),
    httpx.Response(302, headers={"location": "https://attacker.invalid"}),
    httpx.Response(200, text="secret-key"), ok({"memories": "bad"}),
    httpx.Response(200, json={"status": "error", "error": "secret-key"}),
    httpx.Response(200, json={"status": "ok"}), ok(None),
])
def test_http_and_malformed_errors_are_safe_and_no_redirects(response):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    result = run(provider(handler).recall("query"))
    assert result["status"] == "error"
    assert result["entries"] == []
    assert "secret-key" not in repr(result)
    assert len(calls) == 1


def test_read_failure_never_silently_uses_abstract_or_partial_entries():
    def handler(request):
        if request.method == "POST":
            return ok({"memories": [{"uri": URI, "abstract": "summary", "content": "inline"}]})
        return ok({"summary": "not the full content"})

    assert run(provider(handler).recall("query")) == {
        "status": "error", "entries": [], "error": "provider_invalid_response"
    }


def test_read_timeout_never_echoes_request_exception_or_key():
    def handler(request):
        raise httpx.ReadTimeout("secret-key leaked by exception", request=request)

    assert run(provider(handler).recall("query")) == {
        "status": "error", "entries": [], "error": "provider_timeout"
    }


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self):
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(30):
            self.yielded += 1
            yield b"x" * 65536

    async def aclose(self):
        self.closed = True


def test_streaming_body_limit_stops_and_closes_oversized_content():
    stream = ChunkStream()

    def handler(request):
        if request.method == "POST":
            return ok({"memories": [{"uri": URI}]})
        return httpx.Response(200, stream=stream)

    result = run(provider(handler).recall("q"))
    assert result == {"status": "error", "entries": [], "error": "provider_response_too_large"}
    assert stream.closed and stream.yielded < 30


def test_slow_stream_has_total_deadline_and_is_closed():
    class SlowStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield b'{}'

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    result = run(provider(lambda request: httpx.Response(200, stream=stream),
                          timeout_seconds=0.001).recall("q"))
    assert result["error"] == "provider_timeout"
    assert stream.closed


def test_content_length_limit_rejects_before_read():
    result = run(provider(lambda request: httpx.Response(
        200, headers={"content-length": str(_MODULE.MAX_RESPONSE_BYTES + 1)}, content=b"secret-key"
    )).recall("q"))
    assert result["error"] == "provider_response_too_large"


def test_blank_key_omitted_and_timeout_config_passed():
    def handler(request):
        assert "X-API-Key" not in request.headers
        assert request.extensions["timeout"] == {"connect": 2, "read": 2, "write": 2, "pool": 2}
        return ok({"memories": []})

    assert run(provider(handler, api_key="   ", timeout_seconds=2).recall("q"))["status"] == "ok"


def publish(p):
    return p.publish_review("local-session", "solve", [{"tool": "pytest", "result": "passed"}], "might help", [{"uri": URI}])


class SessionServer:
    def __init__(self, *, archived=False, live=0):
        self.exists = archived or live > 0
        self.archived = archived
        self.live = live
        self.calls = []
        self.remote = None

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if request.method == "GET":
            assert dict(request.url.params) == {"auto_create": "false"}
            self.remote = path.rsplit("/", 1)[1]
            if not self.exists:
                return httpx.Response(404)
            return ok({"session_id": self.remote, "commit_count": int(self.archived),
                       "message_count": self.live, "memory_policy": POLICY, "auto_commit_policy": None,
                       "user": {"user_id": "alice", "account_id": "default"}})
        data = json.loads(request.content)
        if path == "/api/v1/sessions":
            assert data == {"session_id": self.remote, "memory_policy": POLICY, "auto_commit_policy": None}
            self.exists = True
            return ok({"session_id": self.remote, "user": {"user_id": "alice", "account_id": "default"}})
        if path.endswith("/messages/batch"):
            messages = data["messages"]
            assert len(messages) == 3
            assert all(message["peer_id"] == "battlefield" for message in messages)
            assert messages[0]["role"] == "user"
            assert messages[0]["parts"][0]["text"] == "Objective:\nsolve"
            assert "hypotheses" in messages[1]["parts"][0]["text"]
            assert messages[1]["parts"][1] == {
                "type": "context", "uri": URI, "context_type": "memory",
                "abstract": "Read context only; application and causality are not established."
            }
            evidence = messages[2]["parts"][0]
            assert evidence["type"] == "tool" and evidence["tool_status"] == "completed"
            assert "pytest" in evidence["tool_output"]
            self.live = len(messages)
            return ok({"added": self.live, "message_count": self.live})
        assert path.endswith("/commit") and data == {"keep_recent_count": 0}
        self.archived = True
        self.live = 0
        return ok({"status": "accepted", "archived": True, "task_id": "task-123",
                   "archive_uri": f"viking://user/alice/sessions/{self.remote}/history/archive_001"})


def test_commit_accepted_is_pending_not_learned_and_repeated_publish_not_reappended():
    server = SessionServer()
    p = provider(server)
    result = run(publish(p))
    assert result["status"] == "pending" and result["archived"] is True
    assert result["extraction_status"] == "pending" and result["task_id"] == "task-123"
    assert result["archive_uri"].endswith("/history/archive_001")
    assert "learned" not in result
    assert [(r.method, r.url.path) for r in server.calls] == [
        ("GET", f"/api/v1/sessions/{server.remote}"), ("POST", "/api/v1/sessions"),
        ("POST", f"/api/v1/sessions/{server.remote}/messages/batch"),
        ("POST", f"/api/v1/sessions/{server.remote}/commit"),
    ]
    again = run(publish(p))
    assert again["status"] == "archived" and again["extraction_status"] == "unknown"
    assert again["session_id"] == result["session_id"]
    assert len(server.calls) == 5


def test_new_provider_checks_existing_archived_session_before_mutation():
    server = SessionServer(archived=True)
    result = run(publish(provider(server)))
    assert result["status"] == "archived"
    assert len(server.calls) == 1 and server.calls[0].method == "GET"


def test_existing_full_batch_is_unknown_without_payload_digest():
    server = SessionServer(live=3)
    result = run(publish(provider(server)))
    assert result["status"] == "pending" and result["archived"] is False
    assert result["error"] == "send_outcome_unknown"
    assert len(server.calls) == 1 and server.calls[0].method == "GET"


def test_changed_review_same_session_cannot_commit_old_matching_count_batch():
    server = SessionServer(live=3)
    result = run(provider(server).publish_review("local-session", "DIFFERENT objective",
                                               [{"result": "DIFFERENT evidence"}], "DIFFERENT analysis", []))
    assert result["extraction_status"] == "unknown" and result["archived"] is False
    assert result["error"] == "send_outcome_unknown"
    assert len(server.calls) == 1 and server.calls[0].method == "GET"


def test_partial_batch_is_unknown_and_not_reappended():
    server = SessionServer(live=1)
    result = run(publish(provider(server)))
    assert result["status"] == "pending" and result["extraction_status"] == "unknown"
    assert result["error"] == "send_outcome_unknown"
    assert len(server.calls) == 1


@pytest.mark.parametrize("stage", ["create", "batch", "commit"])
def test_ambiguous_send_timeout_reports_unknown_and_retry_checks_server(stage):
    server = SessionServer()

    def handler(request):
        response = server(request)
        if request.method == "POST" and (
            (stage == "create" and request.url.path == "/api/v1/sessions")
            or (stage == "batch" and request.url.path.endswith("/messages/batch"))
            or (stage == "commit" and request.url.path.endswith("/commit"))
        ):
            raise httpx.ReadTimeout("request secret-key may have been accepted", request=request)
        return response

    result = run(publish(provider(handler)))
    assert result["status"] == "pending" and result["extraction_status"] == "unknown"
    assert result["error"] == "send_outcome_unknown" and "secret-key" not in repr(result)
    before = len(server.calls)
    retry = run(publish(provider(server)))
    assert retry["status"] in {"archived", "pending"}
    assert server.calls[before].method == "GET"
    assert sum(r.url.path.endswith("/messages/batch") for r in server.calls) == 1
    if stage == "batch":
        assert retry["error"] == "send_outcome_unknown"
        assert retry["archived"] is False
        assert len(server.calls) == before + 1
        assert not any(r.url.path.endswith("/commit") for r in server.calls)


def test_concurrent_publish_single_batch():
    server = SessionServer()
    p = provider(server)

    async def both():
        return await asyncio.gather(publish(p), publish(p))

    result = run(both())
    assert [r["status"] for r in result] == ["pending", "archived"]
    assert sum(r.url.path.endswith("/messages/batch") for r in server.calls) == 1


def test_wrong_user_context_rejected_before_any_write():
    p = provider(lambda request: pytest.fail("invalid context must not send"))
    result = run(p.publish_review("s", "objective", [], "hypothesis", [URI.replace("alice", "other")]))
    assert result["status"] == "error" and result["error"] == "invalid_context_scope"


def test_existing_unsafe_policy_fails_closed():
    def handler(request):
        assert request.method == "GET"
        return ok({"session_id": request.url.path.rsplit("/", 1)[1], "commit_count": 0,
                   "message_count": 0, "memory_policy": {"self": {"enabled": True}}, "user": {"user_id": "alice"}})

    assert run(publish(provider(handler)))["error"] == "existing_session_policy_mismatch"


def test_normalized_v050_policy_can_resume_empty_session():
    server = SessionServer()
    server.exists = True

    def handler(request):
        response = server(request)
        if request.method == "GET":
            result = json.loads(response.content)["result"]
            result["memory_policy"] = {**POLICY, "working_memory": {"enabled": False},
                                       "memory_types": ["cases", "experiences", "trajectories"]}
            return ok(result)
        return response

    assert run(publish(provider(handler)))["status"] == "pending"


@pytest.mark.parametrize("status", ["pending", "queued", "running", "cancelling", "completed", "failed", "cancelled"])
def test_get_task_states_are_observed_not_claimed_learned(status):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/api/v1/tasks/task-123"
        assert not request.url.query
        return ok({"task_id": "task-123", "status": status, "error": "secret-key",
                   "result": {"memories_extracted": {"experiences": 1}, "private": "secret-key"}})

    result = run(provider(handler).get_task("task-123"))
    assert result["status"] == status and result["learned"] is False
    assert result["task_id"] == "task-123" and "secret-key" not in repr(result)
    assert "result" not in result
    if status in {"failed", "cancelled"}:
        assert result["error"] in {"provider_task_failed", "provider_task_cancelled"}


@pytest.mark.parametrize("payload", [None, {}, {"task_id": "other", "status": "completed"},
                                      {"task_id": "task-123", "status": "invented"},
                                      {"task_id": "task-123", "status": []}])
def test_get_task_malformed_response_is_safe(payload):
    result = run(provider(lambda request: ok(payload)).get_task("task-123"))
    assert result == {"status": "error", "error": "provider_invalid_response", "learned": False}


@pytest.mark.parametrize("task_id", ["../private", "task?key=secret", "", None])
def test_get_task_rejects_invalid_ids_without_requests(task_id):
    result = run(provider(lambda request: pytest.fail("must not send")).get_task(task_id))
    assert result["error"] == "invalid_task_id"


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("user", [None, {}, {"user_id": "bob"}, {"user_id": "alice2"}])
def test_publish_identity_mismatch_stops_before_append_or_commit(existing, user):
    calls = []

    def handler(request):
        calls.append(request)
        remote = request.url.path.rsplit("/", 1)[1]
        if request.method == "GET":
            if not existing:
                return httpx.Response(404)
            return ok({"session_id": remote, "user": user, "commit_count": 1, "message_count": 0})
        assert request.url.path == "/api/v1/sessions"
        return ok({"session_id": json.loads(request.content)["session_id"], "user": user})

    result = run(publish(provider(handler)))
    assert result["status"] == "error" and result["error"] == "provider_identity_mismatch"
    assert len(calls) == (1 if existing else 2)
    assert not any(r.url.path.endswith(("/messages/batch", "/commit")) for r in calls)


def test_optional_peer_header_is_metadata_not_scope():
    def handler(request):
        assert "X-OpenViking-Actor-Peer" not in request.headers
        assert request.headers["X-OpenViking-User"] == "alice"
        assert json.loads(request.content)["target_uri"] == ROOT
        return ok({"memories": []})

    assert run(provider(handler, peer_id="").recall("q"))["status"] == "ok"


def test_get_task_disabled_and_auth_errors_are_safe():
    disabled = OpenVikingExperienceProvider(ProviderConfig("", "", "", ""))
    assert run(disabled.get_task("task-123"))["error"] == "provider_disabled"
    result = run(provider(lambda request: httpx.Response(401, text="secret-key")).get_task("task-123"))
    assert result == {"status": "error", "error": "provider_unauthorized", "learned": False}
