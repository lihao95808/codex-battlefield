"""P6-5 的可离线那一半：**战场这一侧到底按什么协议发请求**（判据 F1–F8）。

## 为什么这一组值得存在

`docs/provider-matrix.md` 的缺口是"三类端点的真实可达性与鉴权未验"（本机没凭据）。
但有一个问题**不需要凭据**就能回答，而且回答它能让真实端点少踩坑：

> 真实端点必须实现什么，才能被这个战场接上？

做法：起一个本机假网关（`fake_responses_gateway.py`，只用标准库），让**真实的 Codex CLI**
通过它跑一次 planner，然后把收到的原始请求逐项断言（路径、方法、鉴权头、body 字段）。
于是"端点的契约"从'照文档猜'变成**可断言的形状**。

## 边界（写在最前面）

* 本机**没有** codex 时整组 `skip`（并明确说明 skip 的原因），不假装通过；
* 它**不**证明真实端点可达、鉴权被接受、模型输出可用 —— 那是真凭据才能验的；
* 它验的是**我们这一侧**的行为。这一点也是为什么它有意义：
  真实端点接不上时，先要排除"我们发错了"。

## 为什么不用 `verify_provider_planner.py`

那个脚本要真凭据。这一组是它的**离线替身**：同一个调用路径（`CodexExecRunner.plan`），
但把端点换成本机假网关。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.database import SessionLocal, init_db
from app.models import TaskNode
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner
from tests.fake_responses_gateway import FakeResponsesGateway

# 平台各自的"真实 CLI"：POSIX 上如果挑到 Windows 的 `codex.cmd`（在 /mnt/c 的 PATH 里
# 很常见），`create_subprocess_exec` 会得到 `Exec format error`，五条判据会一起红 ——
# 那不是"协议不对"，是**挑错了可执行文件**（2026-10-18 在 WSL 上实测）。
CODEX = (
    (shutil.which("codex.cmd") or shutil.which("codex"))
    if os.name == "nt"
    else shutil.which("codex")
)

pytestmark = pytest.mark.skipif(
    CODEX is None
    or (os.name != "nt" and CODEX.startswith("/mnt/")),
    reason=(
        "本机没有可执行的 codex CLI：这一组要通过**真实的 Codex CLI** 验『我们这一侧发什么』；"
        "在 POSIX 上如果 only 找到 /mnt/… 里的 Windows 转发器，执行会得到 Exec format error —— "
        "那是挑错二进制，不是协议不对"
    ),
)

GOAL = "离线冒烟：确认这一侧按 Responses 协议发请求"


def _settings(tmp_path: Path, codex_home: Path) -> Settings:
    return Settings(
        workspace_dir=tmp_path / "workspace",
        codex_bin=CODEX or "codex",
        codex_home=str(codex_home),
        codex_model="",
        codex_reasoning_effort="",
        codex_timeout_seconds=120,
        codex_ignore_rules=True,
        codex_ignore_user_config=False,
        codex_ephemeral=True,
        max_concurrency=1,
        mock_delay_seconds=0,
    )


def _write_isolated_home(home: Path, base_url: str, *, env_key: str, requires_openai_auth: bool) -> None:
    """写一份隔离 `config.toml`：**与 `use_provider.ps1` 生成的形状一致**。"""

    home.mkdir(parents=True, exist_ok=True)
    auth_line = f"requires_openai_auth = {'true' if requires_openai_auth else 'false'}\n"
    (home / "config.toml").write_text(
        "model = \"fake-model\"\n"
        "model_provider = \"fakegw\"\n"
        "model_reasoning_effort = \"low\"\n"
        "model_context_window = 32000\n"
        "\n"
        "[model_providers.fakegw]\n"
        "name = \"Fake local gateway\"\n"
        f"base_url = \"{base_url}\"\n"
        "wire_api = \"responses\"\n"
        f"env_key = \"{env_key}\"\n"
        f"{auth_line}",
        encoding="utf-8",
    )


_PLANNER_CACHE: dict[str, dict] = {}


def _run_one_planner(tmp_path: Path, home: Path) -> dict:
    """跑一次**真实的** planner 调用，返回汇总（含 exit_code 与命令）。

    一次调用要几秒（真子进程）。同一个用例里会被问两次时（例如"先看日志、再看退出码"），
    这里按 `home` 做进程内缓存 —— 少起一次进程，判据的语义不变。
    """

    key = str(home)
    if key in _PLANNER_CACHE:
        return _PLANNER_CACHE[key]

    init_db()
    settings = _settings(tmp_path, home)
    orchestrator = Orchestrator(settings)
    constitution_id = _blocking(
        orchestrator.draft_constitution(GOAL, target_path=str(tmp_path / "target"), runner_mode="mock")
    )
    orchestrator.lock_constitution(constitution_id)
    root_id = _blocking(
        orchestrator.start_project(
            GOAL,
            target_path=str(tmp_path / "target"),
            runner_mode="real",
            constitution_id=constitution_id,
            auto_run=False,
        )
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        envelope = _blocking(CodexExecRunner(settings).plan(root))
    result = {
        "exit_code": envelope.exit_code,
        "command": envelope.command,
        "payload": envelope.payload,
        "logs_path": envelope.logs_path,
        "last_message_path": envelope.last_message_path,
    }
    _PLANNER_CACHE[key] = result
    return result


def _logs_tail(result: dict, limit: int = 2000) -> str:
    """把这次调用的 JSONL 日志尾巴读出来 —— 调用失败时**必须**能看到原因。

    没有这个，失败信息只剩一句 `exit=1`，排查要重新跑一遍。
    """

    text = ""
    path = str(result.get("logs_path") or "")
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if not text:
        return "(没有日志)"
    return text[-limit:]


def _blocking(awaitable):
    """在同步测试里驱动一个协程（这些方法本来就是 async）。"""

    import asyncio

    return asyncio.run(awaitable)


@pytest.fixture(scope="module")
def live_call(tmp_path_factory):
    """**一次**真实调用 + 它的假网关，供 F1/F2 共用。

    为什么合并：每次真实调用要好几秒（真子进程 + 重试）。分开跑三遍会让这一组变成
    门禁里最慢的一环，而"跑得慢的门禁会被跳过"—— 那比少一条判据更坏。
    这里把 F1/F2 合并到一次调用上；F3（负向）与 F1 的凭据用例各自单独跑。
    """

    tmp_path = tmp_path_factory.mktemp("wire-format")
    (tmp_path / "target").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "codex-home"
    gateway = FakeResponsesGateway().__enter__()
    try:
        _write_isolated_home(home, gateway.base_url, env_key="FAKE_GATEWAY_KEY", requires_openai_auth=False)
        result = _run_one_planner(tmp_path, home)
        yield gateway, result
    finally:
        gateway.__exit__(None, None, None)


# --- F1 请求形状：打到哪、用什么方法、body 里有什么 ---------------------------


def test_f1_the_client_speaks_the_responses_protocol(live_call):
    """一次真实 planner 调用 → 假网关收到的请求必须长得像 Responses API。"""

    gateway, result = live_call
    requests = gateway.responses_requests()
    assert requests, (
        "假网关没收到任何 `/responses` 请求 —— 这一侧没有按 Responses 协议发。"
        f"收到的全部请求：{[item['path'] for item in gateway.records]}；CLI exit={result['exit_code']}"
    )
    first = requests[0]
    assert first["method"] == "POST"
    assert first["path"].rstrip("/").endswith("/v1/responses"), first["path"]

    body = json.loads(first["body"])
    # 模型名来自隔离 `config.toml`（而不是环境变量）—— 这正是 P6-5 想钉住的一件事。
    assert body.get("model") == "fake-model", body
    # `input` 是消息数组（Responses 的形状），不是 `messages`（Chat Completions 的形状）。
    assert "input" in body, f"body 里没有 `input`：{sorted(body)}"
    assert "messages" not in body, "发的是 Chat Completions 的形状 —— 与 `wire_api = responses` 不符"


def test_f1_the_client_asks_for_streaming(live_call):
    """**实测发现的契约细节**：codex 默认要求**流式**应答。

    第一版假网关对 `stream: true` 回了普通 JSON body，codex 报：
    `stream disconnected before completion: stream closed before response.completed`。
    也就是说真实网关必须支持 SSE，而且事件序列里不能省 `response.output_item.done`
    （省了它 codex 报 `no last agent message; wrote empty content`）。
    这两条现在写在 `docs/provider-matrix.md` 里。
    """

    gateway, _result = live_call
    body = json.loads(gateway.responses_requests()[0]["body"])
    assert body.get("stream") is True, f"这一侧没有要求流式：stream={body.get('stream')!r}"


def test_f1_the_credential_reaches_the_child_process(tmp_path):
    """`env_key` 指名的变量必须**带着真值**传到子进程。

    ⚠️ 这里**必须**用默认的 `OPENAI_API_KEY` 键名：子进程环境默认收窄，放行的前缀是
    `CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_`（`docs/provider-matrix.md` §2.1）。
    第一版用自定义键名 + 一次真实调用，结果 runner 按 `env_key` 补了**占位值**
    `local-placeholder` —— 真实凭据被收窄丢掉，而判据看到的是一句
    `Bearer local-placeholder`。这正是文档里那条"网关 401 但配置看起来完全正确"的形态。

    这一条**不跑真实 CLI**：起一次真实调用要十几秒，而它验的是"环境里有没有那个值"，
    直接问 runner 的子进程环境更快也更确定（真实调用的覆盖由 `live_call` 那一组负责）。
    """

    home = tmp_path / "codex-home"
    sentinel = "sentinel-token-for-assertion"
    with FakeResponsesGateway() as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="OPENAI_API_KEY", requires_openai_auth=False)
        settings = _settings(tmp_path, home)
        os.environ["OPENAI_API_KEY"] = sentinel
        try:
            env = CodexExecRunner(settings)._codex_env("planner", "probe")  # noqa: SLF001
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    assert env.get("OPENAI_API_KEY") == sentinel, (
        f"子进程环境里的凭据是 {env.get('OPENAI_API_KEY')!r}，不是那个真值 —— "
        "说明它在环境收窄那一步被丢掉了（症状是端点 401，而配置看起来完全正确）"
    )


def test_f1_an_unlisted_env_key_gets_only_a_placeholder(tmp_path):
    """反向判据：**不在放行名单**里的键名只会拿到占位值 —— 这就是那条已知陷阱。"""

    home = tmp_path / "codex-home"
    with FakeResponsesGateway() as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="MY_GATEWAY_KEY", requires_openai_auth=False)
        settings = _settings(tmp_path, home)
        os.environ["MY_GATEWAY_KEY"] = "real-secret"
        try:
            env = CodexExecRunner(settings)._codex_env("planner", "probe")  # noqa: SLF001
        finally:
            os.environ.pop("MY_GATEWAY_KEY", None)

    assert env.get("MY_GATEWAY_KEY") != "real-secret", "名单外的凭据不该被交给子进程"
    assert env.get("MY_GATEWAY_KEY") == "local-placeholder", (
        "runner 应当补一个**占位值**（只为满足 codex 的存在性要求）—— "
        "用户必须自己把前缀加进 `CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES`"
    )


# --- F2 端点契约：假网关只需要实现一个很窄的子集 ------------------------------


def test_f2_a_minimal_responses_response_is_enough_to_complete_a_call(live_call):
    """假网关回的是**最小**应答；如果这一侧因此失败，就说明契约比文档写的更宽。"""

    _gateway, result = live_call
    assert result["exit_code"] == 0, (
        "最小 Responses 应答没能让这次调用成功 —— 说明真实端点要实现的东西比"
        f"`docs/provider-matrix.md` 写的更多。exit={result['exit_code']}，"
        f"命令={result['command']}\n--- 日志尾巴 ---\n{_logs_tail(result)}"
    )


def test_f2_the_gateway_sees_exactly_one_scenario_when_it_only_needs_one(tmp_path):
    """假网关只实现 `/v1/models` 与 `/v1/responses` —— 记录里不该出现别的必需端点。"""

    (tmp_path / "target").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "codex-home"
    with FakeResponsesGateway() as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="FAKE_GATEWAY_KEY", requires_openai_auth=False)
        _run_one_planner(tmp_path, home)
        paths = {item["path"].split("?")[0] for item in gateway.records}
        unexpected = {path for path in paths if not path.startswith("/v1/")}
        assert unexpected == set(), (
            f"这一侧还打了 `/v1/` 之外的路径：{unexpected} —— 真实端点也要实现它们才能接上"
        )


# --- F3 隔离：走假网关不能碰到用户的 ~/.codex -------------------------------


def test_f3_the_call_uses_the_isolated_home_not_the_user_profile(tmp_path):
    """隔离 `CODEX_HOME` 是 P6-5 的前提：一次调用不该读用户的 `~/.codex/config.toml`。"""

    (tmp_path / "target").mkdir(parents=True, exist_ok=True)
    home = tmp_path / "codex-home"
    with FakeResponsesGateway() as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="OPENAI_API_KEY", requires_openai_auth=False)
        settings = _settings(tmp_path, home)
        # runner 会把节点 home 里的 `config.toml` 从基座覆盖过去；这里直接核这一点。
        # 目录要先存在（真实路径上由 `_codex_env` 建），`_seed_node_home_config` 只负责写文件。
        node_home = home / "nodes" / "probe"
        node_home.mkdir(parents=True, exist_ok=True)
        runner = CodexExecRunner(settings)
        runner._seed_node_home_config(home, node_home)  # noqa: SLF001 - 就是要验这个私有方法的行为
        seeded = (node_home / "config.toml").read_text(encoding="utf-8")
        assert "base_url" in seeded and gateway.base_url in seeded, (
            "节点 home 没有拿到基座的 provider 配置 —— 那会让节点回落到默认端点（实测事故的形态）"
        )


# --- F4 判据自身可信 ---------------------------------------------------------


def test_f4_the_fake_gateway_records_what_it_receives(tmp_path):
    """假网关自己要可信：它得真的记录请求，否则上面几条判据都在看空气。"""

    import urllib.request

    with FakeResponsesGateway() as gateway:
        request = urllib.request.Request(  # noqa: S310 - 本机回环
            f"{gateway.base_url}/responses",
            data=json.dumps({"model": "x", "input": "hi"}).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": "Bearer probe"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["output_text"] == '{"ok": true}'
        assert len(gateway.responses_requests()) == 1
        assert gateway.responses_requests()[0]["headers"]["authorization"] == "Bearer probe"


def test_f4_dropping_the_provider_config_leaves_nothing_to_seed(tmp_path):
    """反向判据：基座没有 `config.toml` 时，节点 home **不会**被凭空写入一份。

    这一条防的是"判据只证明了'有请求'，而不是'请求到了我们配的端点'"。
    没有配置时不该有可继承的东西 —— 也不该由我们猜一份出来
    （`_seed_node_home_config` 的语义是"基座缺就静默跳过"）。

    这里**不跑真实 CLI**：起一次没有 provider 配置的调用会走 codex 自己的默认端点，
    重试到超时（实测十几秒），而它要验的只是"有没有被 seed 出配置"。
    """

    home = tmp_path / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    node_home = home / "nodes" / "probe"
    node_home.mkdir(parents=True, exist_ok=True)

    with FakeResponsesGateway() as gateway:
        settings = _settings(tmp_path, home)
        CodexExecRunner(settings)._seed_node_home_config(home, node_home)  # noqa: SLF001
        assert not (node_home / "config.toml").exists(), (
            "基座没有 `config.toml` 时不该凭空造一份 —— 那会让'配置从哪来'变得不可追"
        )
        assert not gateway.records, "没有配置就不该有任何出网请求"

# --- F5 鉴权：**错的 key 会被拒、而且这一侧会把它报出来** -------------------------
#
# `docs/provider-matrix.md` 长期把"鉴权是否被接受"记成**未验**（要真凭据）。
# 本机没有厂商凭据，但"错 key 会被拒 + 这一侧的失败方向不静默"这两件事可以用
# **带鉴权的假网关**验 —— 它排除的是"我们发错了/我们静默吞了 401"这一类原因，
# 剩下的才是"某家厂商端点特有的鉴权细节"，那一条仍然要有真凭据。


def test_f5_a_wrong_key_gets_401_and_the_failure_is_visible(tmp_path):
    """网关强制令牌；给错值 → 真实调用必须**失败**，并且日志里能看到 401。

    为什么这条重要：如果 401 被这一侧静默吞掉（比如重试到超时、或者当成空输出），
    用户看到的就是"模型什么都没说"，而真正的原因是凭据错了。
    """

    home = tmp_path / "codex-home"
    with FakeResponsesGateway(require_token="good-token") as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="OPENAI_API_KEY", requires_openai_auth=False)
        os.environ["OPENAI_API_KEY"] = "wrong-token"
        try:
            result = _run_one_planner(tmp_path, home)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

        assert result["exit_code"] != 0, (
            "错 key 竟然成功了 —— 说明假网关的鉴权没生效，或者这一侧根本没带凭据"
        )
        logs = _logs_tail(result, 6000).lower()
        assert "401" in logs or "invalid api key" in logs or "unauthorized" in logs, (
            "失败的日志里没有 401/unauthorized/invalid api key —— 401 被吞掉了，"
            f"用户只会看到'没有输出'。日志尾巴：\n{_logs_tail(result)}"
        )
        seen = [item["headers"].get("authorization") for item in gateway.responses_requests()]
        assert "Bearer wrong-token" in seen, (
            f"网关收到的 Authorization 是 {seen!r} —— 凭据没发出去，这条判据就没证明'鉴权被拒'"
        )


def test_f6_the_same_call_succeeds_with_the_right_key(tmp_path):
    """同一份配置、同一个端点，只把 key 换成对的 → 调用成功。

    与 F5 成对：单看 F5 会有一个盲区 —— "是不是只要网关要求鉴权，这一侧就一律失败"。
    """

    home = tmp_path / "codex-home"
    with FakeResponsesGateway(require_token="good-token") as gateway:
        _write_isolated_home(home, gateway.base_url, env_key="OPENAI_API_KEY", requires_openai_auth=False)
        os.environ["OPENAI_API_KEY"] = "good-token"
        try:
            result = _run_one_planner(tmp_path, home)
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    assert result["exit_code"] == 0, (
        "带正确 key 的调用没有成功 —— 这一侧在鉴权通过后仍然失败了。"
        f"exit={result['exit_code']}\n--- 日志尾巴 ---\n{_logs_tail(result)}"
    )
