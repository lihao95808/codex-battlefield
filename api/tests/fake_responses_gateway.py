"""假网关：记录收到的请求，并按 OpenAI **Responses** 的形状回一个最小应答（P6-5）。

## 为什么要有它

`docs/provider-matrix.md` 记着 P6-5 的缺口是"三类端点的**真实可达性与鉴权**未验"
（本机没有凭据）。但有一件事**不需要凭据也不需要真模型**就能验，而且价值不小：

> **战场这一侧到底按什么协议发请求？真实端点必须满足什么才能被接上？**

做法：起一个本机假网关，让真实的 Codex CLI 通过它跑一次，把收到的**原始请求**记下来。
于是"端点的契约"从'照文档猜'变成**可断言的形状**（路径、方法、鉴权头、body 的关键字段）。

## 它证明什么、不证明什么

* **证明**：这一侧的客户端行为（打到哪个路径、带什么头、body 里有哪些字段）；
  以及"一个只实现 Responses 子集的端点能不能撑住一次真实调用"。
* **不证明**：真实端点的可达性、鉴权是否被接受、模型输出质量 —— 那些要有真凭据。

## 依赖

只用标准库（`http.server` + `threading`），不引入测试框架之外的东西。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: 假网关回的正文。**故意用一段合法 JSON**：真实 planner 会对它做 schema 校验，
#: 回别的会让"契约通不通"和"输出不合 schema"混在一起。
OUT = "{\"ok\": true}"


class _Handler(BaseHTTPRequestHandler):
    """把请求记到 `records`，然后回一个最小的 Responses 应答。"""

    protocol_version = "HTTP/1.1"
    server_version = "FakeResponsesGateway/1.0"

    def log_message(self, *_args: object) -> None:  # noqa: D102 - 静音默认日志
        return

    def _record(self, body: bytes) -> None:
        records: list[dict] = self.server.records  # type: ignore[attr-defined]
        records.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body.decode("utf-8", errors="replace"),
            }
        )

    def _respond(self, payload: dict, status: int = 200) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _auth_rejection(self) -> bool:
        """配了 `require_token` 时校验 `Authorization`；不通过就回 401 并返回 True。

        为什么要这个旋钮：`docs/provider-matrix.md` 一直把"鉴权是否被接受"记为**未验**。
        真实厂商端点要凭据才能验，但"**错的 key 会被拒、而且我们会看见**"这件事可以用本机
        假网关验 —— 它证明的是**这一侧的失败方向**（不静默、不假装成功），
        这正是 vendor 端点接不上时最想先排除的那一半。
        """

        required = getattr(self.server, "required_token", None)
        if not required:
            return False
        got = (self.headers.get("authorization") or "").strip()
        if got == f"Bearer {required}":
            return False
        self._respond({"error": {"message": "Invalid API key provided (fake gateway)", "type": "invalid_request_error"}}, status=401)
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        self._record(b"")
        if self._auth_rejection():
            return
        # 一些客户端会先探 `/v1/models` 做能力发现。
        self._respond({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if self._auth_rejection():
            return
        try:
            payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            payload = {}

        # ⚠️ **实测发现的契约细节**：codex 默认发的是**流式**请求
        # （`"stream": true`），并且要求以 SSE 事件结尾：
        #
        #   stream disconnected before completion: stream closed before response.completed
        #
        # 只回一个普通 JSON body 是不够的 —— 这一点 `docs/provider-matrix.md` 原来没写，
        # 现在写进去了。真实网关也必须支持流式。
        if payload.get("stream"):
            self._respond_stream()
            return

        # 最小但形状完整的 Responses 应答：`output_text` 是客户端读的那一项。
        self._respond(
            {
                "id": "resp-fake-1",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "fake-model",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg-fake-1",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": OUT, "annotations": []}],
                    }
                ],
                "output_text": OUT,
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                "error": None,
            }
        )

    def _respond_stream(self) -> None:
        """按 SSE 回一串事件，以 `response.completed` 收尾。"""

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        def event(name: str, data: dict) -> None:
            chunk = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        base = {"type": "response", "id": "resp-fake-stream", "model": "fake-model", "status": "in_progress"}
        message_item = {
            "type": "message",
            "role": "assistant",
            "id": "msg-fake-1",
            "status": "completed",
            "content": [{"type": "output_text", "text": OUT, "annotations": []}],
        }
        event("response.created", {"type": "response.created", "response": base})
        event(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "id": "msg-fake-1", "role": "assistant", "status": "in_progress", "content": []},
            },
        )
        event(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "item_id": "msg-fake-1", "output_index": 0, "content_index": 0, "delta": OUT},
        )
        event(
            "response.output_text.done",
            {"type": "response.output_text.done", "item_id": "msg-fake-1", "output_index": 0, "content_index": 0, "text": OUT},
        )
        # `output_item.done` 不能省：只发 delta/done 时实测 codex 会报
        # `no last agent message; wrote empty content` —— 它要的是**完整的 item**。
        event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": message_item})
        event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    **base,
                    "status": "completed",
                    "output_text": OUT,
                    "output": [message_item],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    "error": None,
                },
            },
        )


class FakeResponsesGateway:
    """上下文管理器：`with FakeResponsesGateway() as gateway: ...`。

    `require_token` 非空时，网关**强制**校验 `Authorization: Bearer <token>`：
    不匹配回 401。用来验"错的 key 会被拒、而且这一侧会把它报出来"（判据 F5/F6）。
    """

    def __init__(self, require_token: str | None = None) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.require_token = require_token
        self.port = 0
        self.records: list[dict] = []

    def __enter__(self) -> "FakeResponsesGateway":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.records = self.records  # type: ignore[attr-defined]
        self._server.required_token = self.require_token  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def responses_requests(self) -> list[dict]:
        return [item for item in self.records if "/responses" in item["path"]]

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
