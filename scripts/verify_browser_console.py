"""A6 反缩水契约的量具：在**真浏览器**里打开看板，判"console error = 0"。

为什么要有它：`docs/spec-modularization.md` 的 A6 长期挂着"**未验证**——我没有浏览器可执行 JS"，
而那一栏写的是"改动全是纯渲染，所以新增 console error 面为零"——那是**推理，不是测量**。
本机装着 Chrome / Edge，Chrome DevTools Protocol 又只要一个 WebSocket（`websockets` 已在 api 的 venv 里），
所以这条可以真测，不必再靠推理。

**判据的失效方向（这是本脚本的重点，不是装饰）**：
"0 条 console error"有两种来源 —— 页面真的干净，或者**我根本没在听**（CDP 没连上、
page target 找错、导航失败、页面白屏）。后者下"0 错误"是**瞎的**，而它长得和"干净"一模一样。
所以这里把两件事拆成独立判据，任何一条不成立都**判失败**（绝不退化成"没收到消息 = 通过"）：

1. **渲染证据**：`Runtime.evaluate` 必须在页面里读到 `--expect-text`（默认看板标题）
   且 body 文本长度过阈值 —— 证明"我看的是真渲染出来的看板"，不是 about:blank。
2. **收集器自检（反面自检）**：故意注入一条 `console.error(<哨兵>)`，它**必须**出现在收集结果里。
   收不到 → 说明监听面是死的，此时脚本 exit 4，而不是报"0 错误"。
3. 只有 1 与 2 都成立时，"哨兵之外还有几条 error"才是可信读数；非零 → exit 1。

退出码：0 通过 / 1 页面真有 console error / 2 用法或环境问题（浏览器找不到、CDP 连不上）/
3 渲染证据不成立 / 4 收集器自检失败。

用法（需要先起好 api 与 web，两个服务不由本脚本负责）：

    python scripts/verify_browser_console.py --url http://127.0.0.1:5173/ --out workspace/_a6_console.json

刻意**不接进** `scripts/verify_all.py`：它要真浏览器 + 两个在跑的服务，接进门禁会让门禁在没起服务时
变成"红一片"，那种红会被人习惯性跳过 —— 比没有门禁更坏。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# 同目录的跨平台解析器（P6-2）：浏览器候选位置只有一份实现。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_support import browser_candidates  # noqa: E402

try:
    import websockets
except ModuleNotFoundError:  # pragma: no cover - 环境问题要说清，不要伪装成"页面没问题"
    from platform_support import python_executable as _py  # noqa: E402

    print(f"缺少 websockets（CDP 要用）。用仓库 venv 跑：{_py()}", file=sys.stderr)
    raise SystemExit(2) from None

SENTINEL = "a6-probe-self-check-sentinel"

# 候选位置按**平台**给（P6-2）：Windows 要写死安装路径，Unix 侧交给 `which`。
# 这份清单只有一处实现，见 `scripts/platform_support.py`。
BROWSER_CANDIDATES = browser_candidates()

RENDER_PROBE_JS = """
(() => {
  const h1 = document.querySelector('h1');
  const text = (document.body && document.body.innerText || '');
  return JSON.stringify({
    title: document.title,
    h1: h1 ? h1.textContent : null,
    bodyTextLength: text.length,
    bodyTextExcerpt: text.slice(0, 1500),
    elementCount: document.querySelectorAll('*').length,
    buttonCount: document.querySelectorAll('button').length,
    hasReactRoot: !!document.querySelector('#root') && document.querySelector('#root').childElementCount > 0,
  });
})()
"""


def find_browser(explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).exists():
            raise SystemExit(f"给定的浏览器不存在：{explicit}")
        return explicit
    for candidate in BROWSER_CANDIDATES:
        # 绝对路径直接看文件；可执行名交给 `which`（Unix 侧的 Chrome 装在 PATH 里）。
        if os.path.isabs(candidate):
            if Path(candidate).exists():
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    raise SystemExit(
        "本机找不到 Chrome / Chromium / Edge；用 --browser 指定可执行文件路径 "
        "（Windows 给 chrome.exe 的完整路径，Linux 给 /usr/bin/google-chrome 这类路径）"
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - 固定指向本机 CDP
        return json.loads(response.read().decode("utf-8"))


def wait_for_page_target(port: int, deadline: float) -> dict:
    """等 CDP 起来并拿到一个 page target。

    失效方向：等不到就**抛**（exit 2），不许继续往下跑 —— 继续跑的结果必然是"0 条消息"，
    那正是最贵的误读。
    """
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            for target in http_json(f"http://127.0.0.1:{port}/json/list"):
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.25)
    raise SystemExit(f"连不上 CDP（端口 {port}）：{last_error}")


class ConsoleCollector:
    """把四种"页面在喊疼"的信号收在一起：console API / 未捕获异常 / 浏览器日志 / 请求失败。

    只收 error 级别进 `errors`（判据），warning 与 network 失败单独留证（不进判据，但要能看见）——
    把 warning 也算进判据会让这条门禁在第一天就红着，然后被习惯性忽略。
    """

    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.network_failures: list[dict] = []
        self.raw_events: list[str] = []

    @staticmethod
    def _text_of(args: list[dict]) -> str:
        parts = []
        for arg in args:
            if "value" in arg:
                parts.append(str(arg["value"]))
            elif arg.get("description"):
                parts.append(str(arg["description"]))
            else:
                parts.append(arg.get("type", "?"))
        return " ".join(parts)

    def feed(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params", {})
        self.raw_events.append(method)
        if method == "Runtime.consoleAPICalled":
            text = self._text_of(params.get("args") or [])
            record = {"source": "console", "level": params.get("type"), "text": text}
            if params.get("type") in {"error", "assert"}:
                self.errors.append(record)
            elif params.get("type") == "warning":
                self.warnings.append(record)
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            text = details.get("exception", {}).get("description") or details.get("text") or "uncaught exception"
            self.errors.append({"source": "exception", "level": "error", "text": str(text)})
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            record = {
                "source": f"log/{entry.get('source')}",
                "level": entry.get("level"),
                "text": str(entry.get("text")),
                "url": entry.get("url"),
            }
            if entry.get("level") == "error":
                self.errors.append(record)
            elif entry.get("level") == "warning":
                self.warnings.append(record)
        elif method == "Network.loadingFailed":
            self.network_failures.append(
                {
                    "text": params.get("errorText"),
                    "type": params.get("type"),
                    "canceled": params.get("canceled"),
                }
            )

    def split_sentinel(self) -> tuple[list[dict], int]:
        """把自检哨兵从 error 列表里摘出来：剩下的才是页面自己的错误。"""
        real = [item for item in self.errors if SENTINEL not in (item.get("text") or "")]
        sentinel_hits = len(self.errors) - len(real)
        return real, sentinel_hits


async def drive(
    websocket_url: str,
    page_url: str,
    settle_seconds: float,
    expect_text: str,
    min_body_chars: int,
    screenshot_path: Path | None,
) -> dict:
    collector = ConsoleCollector()
    next_id = 0
    pending: dict[int, asyncio.Future] = {}

    async with websockets.connect(websocket_url, max_size=32 * 1024 * 1024) as socket_conn:

        async def send(method: str, params: dict | None = None) -> dict:
            nonlocal next_id
            next_id += 1
            request_id = next_id
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            pending[request_id] = future
            await socket_conn.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            return await asyncio.wait_for(future, timeout=30)

        async def pump() -> None:
            async for raw in socket_conn:
                message = json.loads(raw)
                if "id" in message:
                    future = pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    collector.feed(message)

        pump_task = asyncio.ensure_future(pump())
        try:
            await send("Runtime.enable")
            await send("Log.enable")
            await send("Network.enable")
            await send("Page.enable")
            await send("Page.navigate", {"url": page_url})
            # 不用 loadEventFired 就收手：看板是 SPA，首屏数据是 load 之后再 fetch 的，
            # 那一段恰恰是最容易出 console error 的地方。所以固定再观察 settle_seconds。
            await asyncio.sleep(settle_seconds)
            render_raw = await send("Runtime.evaluate", {"expression": RENDER_PROBE_JS, "returnByValue": True})
            render = json.loads(render_raw.get("result", {}).get("result", {}).get("value") or "{}")
            if screenshot_path is not None:
                shot = await send("Page.captureScreenshot", {"format": "png"})
                data = shot.get("result", {}).get("data")
                if data:
                    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    screenshot_path.write_bytes(base64.b64decode(data))
            # 反面自检：注入一条真 console.error，它必须被上面的收集器抓到
            await send("Runtime.evaluate", {"expression": f"console.error('{SENTINEL}')"})
            await asyncio.sleep(1.0)
        finally:
            pump_task.cancel()

    real_errors, sentinel_hits = collector.split_sentinel()
    body_chars = int(render.get("bodyTextLength") or 0)
    rendered = bool(render.get("h1")) and expect_text in (render.get("h1") or "") and body_chars >= min_body_chars
    return {
        "page_url": page_url,
        "render": render,
        "render_ok": rendered,
        "screenshot": str(screenshot_path) if screenshot_path is not None else None,
        "expect_text": expect_text,
        "min_body_chars": min_body_chars,
        "collector_self_check_ok": sentinel_hits >= 1,
        "sentinel_hits": sentinel_hits,
        "errors": real_errors,
        "warnings": collector.warnings,
        "network_failures": collector.network_failures,
        "event_kinds": sorted(set(collector.raw_events)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="在真浏览器里判看板的 console error 是否为 0（A6）")
    parser.add_argument("--url", default="http://127.0.0.1:5173/")
    parser.add_argument("--browser", default=None, help="chrome / msedge 可执行文件路径；默认自动找")
    parser.add_argument("--settle-seconds", type=float, default=8.0, help="导航后再观察多久（SPA 的首屏 fetch 在这段里）")
    parser.add_argument("--expect-text", default="L4 Navigator", help="页面 h1 必须包含的文本（渲染证据）")
    parser.add_argument("--min-body-chars", type=int, default=200, help="body 文本至少这么长才算真渲染")
    parser.add_argument("--out", default=None, help="把完整证据写成 json")
    parser.add_argument("--screenshot", default=None, help="落一张首屏截图（A6 契约要求可复现的 DOM/截图证据）")
    args = parser.parse_args()

    browser = find_browser(args.browser)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="a6-probe-")
    command = [
        browser,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--window-size=1600,1000",
        "about:blank",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        target = wait_for_page_target(port, deadline=time.monotonic() + 20)
        result = asyncio.run(
            drive(
                target["webSocketDebuggerUrl"],
                args.url,
                args.settle_seconds,
                args.expect_text,
                args.min_body_chars,
                Path(args.screenshot) if args.screenshot else None,
            )
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)

    result["browser"] = browser
    result["browser_version"] = None
    try:
        result["browser_version"] = http_json(f"http://127.0.0.1:{port}/json/version").get("Browser")
    except OSError:
        pass

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, out_path)

    print(f"浏览器：{result.get('browser_version') or browser}")
    print(f"页面：{result['page_url']}")
    print(f"渲染证据：h1={result['render'].get('h1')!r} body={result['render'].get('bodyTextLength')} 字符 "
          f"元素 {result['render'].get('elementCount')} 个 → {'OK' if result['render_ok'] else '不成立'}")
    print(f"收集器自检：注入的 console.error 抓到 {result['sentinel_hits']} 次 → "
          f"{'OK' if result['collector_self_check_ok'] else '失败（此时 0 错误是瞎的）'}")
    print(f"console error（排除自检那条）：{len(result['errors'])} 条")
    for item in result["errors"]:
        print(f"  - [{item['source']}/{item['level']}] {item['text'][:300]}")
    print(f"warning：{len(result['warnings'])} 条（不进判据，仅留证）")
    print(f"请求失败：{len(result['network_failures'])} 条（不进判据，仅留证）")

    if not result["render_ok"]:
        print("判定：**失败** —— 页面没有真渲染出来，这种情况下的 0 错误不是读数。", file=sys.stderr)
        return 3
    if not result["collector_self_check_ok"]:
        print("判定：**失败** —— 收集器自己没抓到注入的 console.error，监听面是死的。", file=sys.stderr)
        return 4
    if result["errors"]:
        print("判定：**失败** —— 页面真的报了 console error。", file=sys.stderr)
        return 1
    print("判定：**通过** —— 渲染证据成立、收集器自检通过、且没有 console error。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
