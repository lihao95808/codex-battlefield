"""每节点隔离 `CODEX_HOME` 必须继承基座 `config.toml` 的 provider 配置（2026-10-14 实测事故）。

**事故链条**（两处，缺一都让 provider 通道静默失效）：

1. `docs/spec-task-isolation.md` §2.1 把节点的 `CODEX_HOME` 指到
   `<base>/nodes/<node-id>`，收益是会话/缓存/插件状态互不可见。
   但 **codex 不从父目录继承配置**——它只读自己 `CODEX_HOME/config.toml`。
   节点 home 里那个文件当时只有 `[projects...] trust_level`（codex 自己写的），
   于是 `model_provider = "mygw"` 和 `[model_providers.mygw]` 整段丢失，
   请求回落到默认 `api.openai.com`：

       ERROR codex_api::endpoint::responses_websocket:
       failed to connect ... 401 Unauthorized, url: wss://api.openai.com/v1/responses

   实测时间线（本机）：基座 `config.toml` 生于 18:25:18，而 18:20–18:26 之间的
   节点 home 全部只有 `trust_level` 一行。

2. 子进程环境默认收窄（§2.4）的默认放行前缀是
   `CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_`，**不含 `OPENAI_`**。
   而隔离 `config.toml` 用 `env_key = "OPENAI_API_KEY"`，codex 要求该变量存在——
   于是凭据被自己的收窄过滤掉了。

两处的失效形态都是"**树照跑、只是全打错端点**"，而不是干脆起不来；
这正是它值得被测试钉住的原因。

本文件钉住：
| 编号 | 要钉住的事 |
|---|---|
| S1 | 节点 home 的 `config.toml` 内容 == 基座内容（provider 段没丢） |
| S2 | 基座改动后，**同一节点再次取 env 会刷新**（不留过期副本） |
| S3 | 基座没有 `config.toml` 时静默跳过，不凭空造一份 |
| S4 | 未配 `codex_home` 时完全不碰磁盘（不凭空造 home） |
| S5 | 基座是唯一真相：节点 home 里已有的陈旧 provider 会被覆盖 |
| E1 | `OPENAI_API_KEY` 默认**能**穿过后缀收窄（端到端接线，非只断言常量） |
"""
from __future__ import annotations

import pathlib

from app.config import get_settings
from app.runner import CodexExecRunner


def _settings(**overrides):
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


BASE_CONFIG = """model = "deepseek-flash"
model_provider = "mygw"

[model_providers.mygw]
name = "Local proxy"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"

[windows]
sandbox = "unelevated"
"""


def _base_home(tmp_path: pathlib.Path, text: str = BASE_CONFIG) -> pathlib.Path:
    home = tmp_path / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")
    return home


def test_s1_node_home_inherits_provider_block(tmp_path: pathlib.Path) -> None:
    """S1：这是本次修复的核心——节点 home 必须拿得到 provider 段。"""
    home = _base_home(tmp_path)
    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))

    env = r._codex_env("planner", "node-abc")
    node_cfg = pathlib.Path(env["CODEX_HOME"]) / "config.toml"

    assert node_cfg.is_file(), f"节点 home 里没有 config.toml：{node_cfg}"
    text = node_cfg.read_text(encoding="utf-8")
    assert 'model_provider = "mygw"' in text, f"provider 丢了：{text!r}"
    assert "[model_providers.mygw]" in text, f"provider 定义丢了：{text!r}"
    assert "127.0.0.1:15721" in text, f"base_url 丢了：{text!r}"
    assert text == BASE_CONFIG


def test_s2_base_change_is_reflected_on_next_call(tmp_path: pathlib.Path) -> None:
    """S2：覆盖语义。若"只在缺失时写一次"，改基座就只对新节点生效。"""
    home = _base_home(tmp_path)
    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))

    first = pathlib.Path(r._codex_env("planner", "node-abc")["CODEX_HOME"]) / "config.toml"
    assert 'model = "deepseek-flash"' in first.read_text(encoding="utf-8")

    # 基座换模型 —— 同一个节点再次取 env 必须跟上
    (home / "config.toml").write_text(BASE_CONFIG.replace("deepseek-flash", "gpt-5.6-luna"),
                                      encoding="utf-8")
    r._codex_env("planner", "node-abc")
    assert 'model = "gpt-5.6-luna"' in first.read_text(encoding="utf-8"), \
        "节点 home 留下了过期配置：改基座后老节点仍会打错端点"


def test_s3_missing_base_config_does_not_fabricate(tmp_path: pathlib.Path) -> None:
    """S3：基座没有 config.toml 时行为退回改动前，不凭猜测造一份。"""
    home = tmp_path / "codex-home"
    home.mkdir()
    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))

    env = r._codex_env("planner", "node-abc")
    assert not (pathlib.Path(env["CODEX_HOME"]) / "config.toml").exists()
    assert pathlib.Path(env["CODEX_HOME"]).is_dir(), "home 本身仍要建好，否则 CLI 自造一个又变共享"


def test_s4_no_home_configured_touches_nothing(tmp_path: pathlib.Path) -> None:
    """S4：未配 `codex_home` 时行为与今天完全一致（§4-I5）。"""
    r = CodexExecRunner(_settings(codex_home="", workspace_dir=tmp_path))
    env = r._codex_env("worker", "node-a")
    assert "CODEX_HOME" not in env


def test_s5_stale_node_copy_is_overwritten(tmp_path: pathlib.Path) -> None:
    """S5：节点 home 里已存在的陈旧 provider 必须被基座覆盖。"""
    home = _base_home(tmp_path)
    stale = home / "nodes" / "node-abc"
    stale.mkdir(parents=True)
    (stale / "config.toml").write_text('model_provider = "openai"\n', encoding="utf-8")

    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))
    r._codex_env("planner", "node-abc")

    text = (stale / "config.toml").read_text(encoding="utf-8")
    assert 'model_provider = "mygw"' in text and '"openai"' not in text, \
        f"陈旧配置没被覆盖，节点仍会打 api.openai.com：{text!r}"


def test_e1_mygw_key_survives_env_narrowing(tmp_path: pathlib.Path, monkeypatch) -> None:
    """E1：接线断言——默认配置下 `OPENAI_API_KEY` 真的到得了子进程。"""
    monkeypatch.setenv("OPENAI_API_KEY", "hub-local")
    monkeypatch.setenv("VENDOR_A_API_KEY", "should-be-dropped")
    home = _base_home(tmp_path)

    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))
    env = r._codex_env("planner", "node-abc")

    assert env.get("OPENAI_API_KEY") == "hub-local", \
        "Hub 凭据被自己的收窄过滤掉了：隔离 config.toml 的 env_key 将拿不到值"
    # 对照：无关凭据仍必须被丢掉，证明断言不是恒真
    assert "VENDOR_A_API_KEY" not in env, "收窄失效了：无关凭据被交给了节点"


def test_e2_missing_env_key_is_backfilled_from_the_config(tmp_path: pathlib.Path, monkeypatch) -> None:
    """E2（回归）：**环境里没有** `OPENAI_API_KEY` 时，也必须补出来。

    实测（2026-10-15）：`OPENAI_API_KEY` 只存在于开发机的 shell 环境里，而 API 常被
    GUI/计划任务拉起 —— 那时 codex 直接失败：

        {"type":"turn.failed","error":{"message":"Missing environment variable: `OPENAI_API_KEY`."}}

    也就是说"认证在 Hub 里做"并不够：codex 仍要求 `env_key` 指名的变量**存在**
    （本地代理不校验它的值）。所以按 config 里声明的 `env_key` 补占位值。
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = _base_home(tmp_path)

    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))
    env = r._codex_env("steward", "node-abc")

    assert env.get("OPENAI_API_KEY"), (
        "环境里没有 OPENAI_API_KEY 时没补占位值 —— GUI 启动的 API 会让整条 Hub 通道断掉"
    )


def test_e2_does_not_inject_keys_the_config_never_names(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """E2 的对照：只补 config 真正声明的 env_key，别无脑塞。

    多塞一个变量就是多一处泄露面。
    """
    # 夹具的 config.toml 声明的是 OPENAI_API_KEY（见 `_base_home`），
    # 所以这里要断言的是"**别的** key 没被塞进来"，而不是它自己。
    #
    # 为什么必须先把宿主环境里的这两个变量清掉：`_codex_env` 在收窄时会放行
    # 白名单前缀（默认含 `OPENAI_`），所以宿主上真实存在的 OPENAI_API_KEY 会被
    # 合法地继承下来 —— 那是**继承**，不是本用例要盯的"注入占位值"。
    # 第一版就是这么写的（把被声明的那个 key 也断言成"不该出现"），
    # 于是本机恰好设了它时用例就红 —— 典型的对宿主环境敏感。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    home = _base_home(tmp_path)
    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))
    env = r._codex_env("steward", "node-abc")

    # 被声明的那个**补上了**（这是 E2 的正向行为）……
    assert env.get("OPENAI_API_KEY"), "config 声明了 env_key 却没补占位值"
    # ……但没声明的那些**不该**被凭空造出来。
    assert "ANTHROPIC_API_KEY" not in env, "塞了 config 从未声明的 key"
    assert "AZURE_OPENAI_API_KEY" not in env


def test_e2_no_config_file_means_no_injection(tmp_path: pathlib.Path, monkeypatch) -> None:
    """没有 config.toml 时不该凭空造变量 —— 没有声明就没有 env_key。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    home = tmp_path / "empty-home"
    home.mkdir()

    r = CodexExecRunner(_settings(codex_home=str(home), workspace_dir=tmp_path))
    env = r._codex_env("steward", "node-abc")

    assert "OPENAI_API_KEY" not in env
