"""沙箱档可配（2026-10-14 实测事故的回归钉子）。

**事故**：真实 runner 的 worker 既不能执行命令也不能写文件。codex 报

    Failed to create unified exec process:
    CreateProcessAsUserW failed: 5 (拒绝访问。)

`read-only` 与 `workspace-write` 都失败，只有 `danger-full-access` 能跑。根因不是配置
写错，而是**当前进程链上 codex 建不出 Windows 受限令牌**。原先沙箱档在 `runner.py` 里
硬编码，没有任何降级手段，于是这条路在这台机器上完全走不通。

四种组合的实测结论（`danger-full-access` 是唯一能跑的）：

| sandbox              | profile 覆盖 unelevated | 结果 |
|---|---|---|
| `read-only`          | 是 | ❌ CreateProcessAsUserW 拒绝访问 |
| `workspace-write`    | 是 | ❌ 同样 |
| `danger-full-access` | 是 | ✅ 命令真的跑起来 |
| `workspace-write`    | 否（全局 elevated） | ❌ helper_unknown_error |

本文件钉住：
| 编号 | 要钉住的事 |
|---|---|
| S1 | 没配覆盖时**原样返回默认值**——可配不能改变既有行为 |
| S2 | 配了合法值就生效 |
| S3 | 非法值**响亮失败**，不静默退回默认 |
| S4 | 未知 `kind` 不猜，走默认 |
| S5 | 五个角色各自独立，互不影响 |
| S6 | **接线**：runner 真的把 `sandbox_for` 的结果传给了 codex 命令行 |
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import CODEX_SANDBOX_MODES, Settings, get_settings


def _settings(**overrides) -> Settings:
    """构造一个只改指定字段的 Settings（dataclass 是 frozen，用 replace 语义）。"""
    base = get_settings()
    return base.__class__(**{**{f: getattr(base, f) for f in base.__dataclass_fields__}, **overrides})


def test_s1_default_is_returned_untouched() -> None:
    s = _settings()
    # 每个角色的默认都必须原样返回——这是"可配"不改变行为的前提
    assert s.sandbox_for("constitution", "read-only") == "read-only"
    assert s.sandbox_for("planner", "read-only") == "read-only"
    assert s.sandbox_for("worker", "workspace-write") == "workspace-write"
    assert s.sandbox_for("verifier", "workspace-write") == "workspace-write"
    assert s.sandbox_for("acceptance", "workspace-write") == "workspace-write"


def test_s2_legal_override_takes_effect() -> None:
    s = _settings(codex_sandbox_worker="danger-full-access")
    assert s.sandbox_for("worker", "workspace-write") == "danger-full-access"
    # 只影响被配的角色
    assert s.sandbox_for("verifier", "workspace-write") == "workspace-write"


def test_s3_illegal_value_fails_loudly() -> None:
    """静默退回默认会让人以为配置生效了——必须抛。"""
    s = _settings(codex_sandbox_worker="DANGER-FULL-ACCESS")  # 大小写不对也是非法
    with pytest.raises(ValueError) as exc:
        s.sandbox_for("worker", "workspace-write")
    assert "不是合法沙箱档" in str(exc.value)
    # 报错信息必须列出合法值，便于直接照抄修复
    for mode in CODEX_SANDBOX_MODES:
        assert mode in str(exc.value)


def test_s4_unknown_kind_falls_back_to_default() -> None:
    s = _settings(codex_sandbox_worker="danger-full-access")
    assert s.sandbox_for("", "read-only") == "read-only"
    assert s.sandbox_for("nonexistent", "read-only") == "read-only"


def test_s5_roles_are_independent() -> None:
    s = _settings(
        codex_sandbox_constitution="read-only",
        codex_sandbox_planner="read-only",
        codex_sandbox_worker="danger-full-access",
        codex_sandbox_verifier="danger-full-access",
        codex_sandbox_acceptance="danger-full-access",
    )
    assert s.sandbox_for("planner", "workspace-write") == "read-only"
    assert s.sandbox_for("worker", "workspace-write") == "danger-full-access"
    assert s.sandbox_for("acceptance", "workspace-write") == "danger-full-access"


def test_s6_runner_passes_the_resolved_sandbox_to_codex(monkeypatch, tmp_path: pathlib.Path) -> None:
    """接线：`_codex_exec_args` 的命令行里必须是 `sandbox_for` 的结果。

    反对"接线看着在、实际是死的"：这条断言命令行内容，不断言函数被调用过。
    """
    from app import runner as runner_mod

    s = _settings(
        codex_sandbox_worker="danger-full-access",
        workspace_dir=tmp_path,
    )
    r = runner_mod.CodexExecRunner(s)
    schema = tmp_path / "s.json"
    schema.write_text("{}", encoding="utf-8")
    args = r._codex_exec_args(tmp_path, schema, tmp_path / "out.json", s.sandbox_for("worker", "workspace-write"), "worker")

    assert "--sandbox" in args, f"命令行里没有 --sandbox：{args}"
    value = args[args.index("--sandbox") + 1]
    assert value == "danger-full-access", f"--sandbox 传的是 {value!r}"

    # 对照：没配覆盖时必须是默认档
    s2 = _settings(workspace_dir=tmp_path)
    r2 = runner_mod.CodexExecRunner(s2)
    args2 = r2._codex_exec_args(tmp_path, schema, tmp_path / "out2.json", s2.sandbox_for("worker", "workspace-write"), "worker")
    assert args2[args2.index("--sandbox") + 1] == "workspace-write"
