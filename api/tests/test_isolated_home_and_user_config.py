"""`--ignore-user-config` 必须与隔离 `CODEX_HOME` 互斥（2026-10-14 实测事故）。

**事故**：接 本地代理 时，隔离 `CODEX_HOME/config.toml` 里写了
`model_provider = "mygw"`（base_url 指向本地 Hub 代理）。但只读角色
（constitution / planner）仍然拿到了 `--ignore-user-config`，而该 flag 会让
**整个用户配置层被忽略**，包括隔离 home 里的 `model_provider`。于是请求落到默认的
`api.openai.com`，那里没有可用凭据：

    ERROR codex_api::endpoint::responses_websocket:
    failed to connect ... 401 Unauthorized, url: wss://api.openai.com/v1/responses

四种组合的实测（真实 codex 0.162.1，子进程 + 隔离 home）：

| 额外 flag | 结果 |
|---|---|
| 无 | ✅ 走 Hub |
| 仅 `--ignore-rules` | ✅ 走 Hub |
| **仅 `--ignore-user-config`** | ❌ provider 被忽略 → api.openai.com |
| 两者都有 | ❌ 同上 |

本文件钉住：
| 编号 | 要钉住的事 |
|---|---|
| I1 | 配了 `codex_home` → **绝不**加 `--ignore-user-config`（这是本次修复） |
| I2 | 没配 `codex_home` + read-only + 开关开 → 仍然加（保留原行为） |
| I3 | 没配 home 但 read-only 关了开关 → 不加 |
| I4 | 配了 `codex_profile` → 不加（原逻辑，profile 本身就是叠加层） |
| I5 | **接线**：`_codex_exec_args` 的命令行里真的没有该 flag |
"""

from __future__ import annotations

import pathlib

from app.config import Settings, get_settings


def _settings(**overrides) -> Settings:
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


def test_i1_isolated_home_never_ignores_user_config(tmp_path: pathlib.Path) -> None:
    """本次修复：Hub 场景必须走隔离 home 的 provider。"""
    s = _settings(codex_home=str(tmp_path / "codex-home"), codex_ignore_user_config=True)
    from app.runner import CodexExecRunner

    r = CodexExecRunner(s)
    assert r._should_ignore_user_config_for_sandbox("read-only") is False


def test_i2_without_home_behaviour_is_unchanged(tmp_path: pathlib.Path) -> None:
    """没配隔离 home 时保留原行为，避免这次修复顺手改掉既有的只读路径。"""
    s = _settings(codex_home="", codex_ignore_user_config=True, codex_profile="")
    from app.runner import CodexExecRunner

    r = CodexExecRunner(s)
    assert r._should_ignore_user_config_for_sandbox("read-only") is True


def test_i3_switch_off_still_wins(tmp_path: pathlib.Path) -> None:
    s = _settings(codex_home="", codex_ignore_user_config=False)
    from app.runner import CodexExecRunner

    r = CodexExecRunner(s)
    assert r._should_ignore_user_config_for_sandbox("read-only") is False


def test_i4_profile_alone_suppresses_it(tmp_path: pathlib.Path) -> None:
    s = _settings(codex_home="", codex_ignore_user_config=True, codex_profile="battlefield")
    from app.runner import CodexExecRunner

    r = CodexExecRunner(s)
    assert r._should_ignore_user_config_for_sandbox("read-only") is False


def test_i5_command_line_has_no_flag_when_home_is_set(tmp_path: pathlib.Path) -> None:
    """接线：断言命令行内容，而不是"函数返回了 False"。"""
    home = tmp_path / "codex-home"
    home.mkdir()
    s = _settings(codex_home=str(home), codex_ignore_user_config=True,
                  workspace_dir=tmp_path, codex_ignore_rules=False, codex_ephemeral=False)
    from app.runner import CodexExecRunner

    r = CodexExecRunner(s)
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    args = r._codex_exec_args(tmp_path, schema, tmp_path / "out.json", "read-only", "planner")

    assert "--ignore-user-config" not in args, f"隔离 home 下不该出现该 flag：{args}"
    # 对照：没配 home 时它必须出现，证明这条断言不是恒真
    s2 = _settings(codex_home="", codex_ignore_user_config=True, codex_profile="",
                   workspace_dir=tmp_path, codex_ignore_rules=False, codex_ephemeral=False)
    r2 = CodexExecRunner(s2)
    args2 = r2._codex_exec_args(tmp_path, schema, tmp_path / "out2.json", "read-only", "planner")
    assert "--ignore-user-config" in args2, f"没配 home 时应保留该 flag：{args2}"
