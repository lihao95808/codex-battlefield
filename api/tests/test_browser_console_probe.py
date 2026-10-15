"""A6 浏览器 console 探针（`scripts/verify_browser_console.py`）的判据测试。

**它存在的理由**：这条判据最容易坏在"**我根本没在听**"那一侧 ——
CDP 没连上 / 找错 target / 页面白屏时，收集器一条消息都收不到，
而"0 条 console error"与"页面真的干净"长得**一模一样**，方向恰好是最贵的那个。
所以下面既测"错误要被认出来"，也测"分级不许把 warning 算成 error"，
还测"哨兵摘除不许把页面自己的错误一起摘掉"（否则真错误会被当成自检噪声吞掉）。

真浏览器那一段不在这里跑（要 Chrome + 两个在跑的服务），读数记在
`VERIFICATION_STATUS.md` 的「2026-09-24（A6）」一节。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROBE = ROOT / "scripts" / "verify_browser_console.py"


def _load() -> Any:
    if importlib.util.find_spec("websockets") is None:  # pragma: no cover - venv 里有
        pytest.skip("需要 websockets（CDP 客户端）")
    spec = importlib.util.spec_from_file_location("verify_browser_console_under_test", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_console_error_and_exception_are_both_counted() -> None:
    module = _load()
    collector = module.ConsoleCollector()
    collector.feed(
        {
            "method": "Runtime.consoleAPICalled",
            "params": {"type": "error", "args": [{"value": "boom"}]},
        }
    )
    collector.feed(
        {
            "method": "Runtime.exceptionThrown",
            "params": {"exceptionDetails": {"exception": {"description": "TypeError: x is not a function"}}},
        }
    )
    collector.feed(
        {
            "method": "Log.entryAdded",
            "params": {"entry": {"level": "error", "source": "network", "text": "404", "url": "http://x/y.ico"}},
        }
    )
    texts = [item["text"] for item in collector.errors]
    assert len(collector.errors) == 3, texts
    assert "boom" in texts[0]
    assert "TypeError" in texts[1]


def test_warnings_and_canceled_requests_stay_out_of_the_verdict() -> None:
    """warning 与被取消的请求**不进判据**：进了这条门禁第一天就红，然后被习惯性忽略。

    反面自检方向：它们仍必须被**留证**（能看见），只是不参与"通过/失败"。
    """
    module = _load()
    collector = module.ConsoleCollector()
    collector.feed({"method": "Runtime.consoleAPICalled", "params": {"type": "warning", "args": [{"value": "careful"}]}})
    collector.feed({"method": "Log.entryAdded", "params": {"entry": {"level": "warning", "source": "other", "text": "slow"}}})
    collector.feed({"method": "Network.loadingFailed", "params": {"errorText": "net::ERR_ABORTED", "type": "EventSource", "canceled": True}})
    assert collector.errors == []
    assert len(collector.warnings) == 2
    assert len(collector.network_failures) == 1


def test_sentinel_split_keeps_real_errors_and_proves_the_collector_is_alive() -> None:
    """哨兵只摘自己那条：页面真报的错不许被当成自检噪声吞掉。

    失效方向：这里若把整张 error 列表清空，"注入的 error 抓到了"会与"页面没有错误"同时成立，
    于是探针永远报通过 —— 判据恒真。
    """
    module = _load()
    collector = module.ConsoleCollector()
    collector.feed({"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": module.SENTINEL}]}})
    collector.feed({"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "real page error"}]}})
    real, hits = collector.split_sentinel()
    assert hits == 1
    assert [item["text"] for item in real] == ["real page error"]


def test_a_blind_collector_reports_zero_hits_so_the_caller_must_fail() -> None:
    """什么都没收到时，自检命中数必须是 0 —— 调用方据此判失败（而不是判"0 错误"）。

    这条是整脚本的**方向锚**：真浏览器那次实测里，对着 `about:blank` 跑就是这个形态
    （渲染证据不成立 → exit 3），脚本没有把它读成通过。
    """
    module = _load()
    collector = module.ConsoleCollector()
    real, hits = collector.split_sentinel()
    assert real == []
    assert hits == 0
