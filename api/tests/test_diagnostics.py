"""The diagnostics bundle must actually redact. Test it against a planted secret.

"0 REDACTED occurrences" is only reassuring if a secret *would* have been caught.
So plant one in a file the collector reads, then assert it does not survive.
"""

import json
import pathlib
import shutil
import tempfile

import pytest

from app import diagnostics


def test_redact_masks_values_but_keeps_names() -> None:
    """名字留着（要看"有没有配"），值抹掉。

    ⚠️ 判据里的名字必须用**中性**样张：这里曾经写的是一个内部网关的真实前缀 +
    真实 hex 后缀。它看着像"随便举的例子"，实际上把内部标识发到了公开仓
    （`scripts/check_public_tree.py` 的词表会命中，见 `docs/public-tree-hygiene.md` §1）。
    判据要的是"名字里带 key/token 就脱敏"这个**形状**，不是那个具体名字。
    """
    out = diagnostics.redact('MY_GATEWAY_9CA735782AC72426_API_KEY = "hub-local-value"')
    assert "hub-local-value" not in out
    assert "MY_GATEWAY_9CA735782AC72426_API_KEY" in out
    assert "***REDACTED***" in out


def test_redact_leaves_non_secrets_alone() -> None:
    """对照：不是凭据的赋值必须原样保留，否则诊断包会变得没用。"""
    for line in (
        'model = "deepseek-flash"',
        'state_home = "D:\\\\work\\\\.codex-battlefield"',
        'runner_mode = "real"',
    ):
        assert diagnostics.redact(line) == line, f"误伤：{line}"


def test_redact_catches_bare_secret_shapes() -> None:
    """没有 `NAME=` 前缀的密钥形态也要抓（sk-/ghp_/AKIA）。"""
    out = diagnostics.redact("authorization used sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "***REDACTED***" in out


def test_planted_secret_does_not_survive_the_bundle(monkeypatch, tmp_path) -> None:
    """**接线判据**：往看门狗日志里种一个密钥，它不能出现在收集结果里。

    这条比"脱敏函数正确"更强：它同时证明**收集路径真的调用了脱敏**。
    只测 `redact()` 的话，忘了在 `_tail()` 里调它也会全绿。
    """
    secret = "sk-PLANTEDSECRET0123456789abcdef"
    log_path = tmp_path / "watchdog.log"
    log_path.write_text(f"2026-01-01T00:00:00 启动，OPENAI_API_KEY={secret}\n", encoding="utf-8")

    settings = _stub_settings(tmp_path)
    payload = diagnostics.collect(settings)
    rendered = diagnostics.render_markdown(payload) + diagnostics.render_json(payload)

    assert secret not in rendered, "种下的密钥活到了诊断包里 —— 脱敏没接到收集路径上"


def test_planted_secret_in_user_config_does_not_survive(monkeypatch, tmp_path) -> None:
    """用户配置里种一个密钥，同样不能活下来（这是最可能真有凭据的文件）。"""
    secret = "ghp_PLANTEDPLANTEDPLANTEDPLANTED1234"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".codex-battlefield.toml").write_text(
        f'repo_root = "D:\\\\work\\\\x"\ngithub_token = "{secret}"\n', encoding="utf-8"
    )
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))

    settings = _stub_settings(tmp_path)
    rendered = diagnostics.render_markdown(diagnostics.collect(settings))

    assert secret not in rendered, "用户配置里的密钥活到了诊断包里"


def test_env_collection_is_allowlisted_only(monkeypatch, tmp_path) -> None:
    """**只**收集白名单键名 —— 绝不 dump 整个 os.environ。

    宿主环境里可能有与本工具无关的公司凭据；dump 出去就是事故。
    """
    monkeypatch.setenv("SOME_UNRELATED_COMPANY_TOKEN", "must-not-appear")
    settings = _stub_settings(tmp_path)
    payload = diagnostics.collect(settings)
    blob = json.dumps(payload, ensure_ascii=False)
    assert "must-not-appear" not in blob
    assert "SOME_UNRELATED_COMPANY_TOKEN" not in blob


def test_bundle_shape_is_stable(tmp_path) -> None:
    """包里有排障真正需要的那几块，缺了就等于白收集。"""
    payload = diagnostics.collect(_stub_settings(tmp_path))
    for key in (
        "generated_at",
        "tool",
        "settings",
        "layout",
        "env",
        "mounted_projects",
        "files",
        "user_config",
    ):
        assert key in payload, f"诊断包缺少 {key}"


def _stub_settings(state_home: pathlib.Path):
    """最小可用 settings：`collect()` 只用到 `layout` 与几个标量。

    不要试图直接覆盖 `settings.layout` —— 它是**只读 property**（由 `state_home`
    解析出来），`object.__setattr__` 会抛 `AttributeError: property ... has no setter`
    （第一版就这么写的）。设真正的字段 `state_home` 才是对的做法，
    而且它走的正是生产路径。
    """
    from app.config import get_settings

    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields["state_home"] = state_home
    fields["project_key"] = ""
    return base.__class__(**fields)


@pytest.fixture(autouse=True)
def _cleanup_temp():
    yield
    for name in ("watchdog.log",):
        path = pathlib.Path(tempfile.gettempdir()) / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
