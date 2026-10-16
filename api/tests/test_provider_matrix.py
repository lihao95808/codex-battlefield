"""P6-5：provider 矩阵 —— 三类端点的配置必须**能被 codex 读进去**。

这一组判据防的是"配置写错但没人知道"：

* `use_provider.ps1` 的默认 `wire_api = "chat"` 在新版 codex 上会让**整份配置加载失败**，
  而 codex 给出的失败信息只有 `error invalid data` —— 指不到是哪一行，
  症状是所有节点一起起不来（上游同形态 issue: openai/codex#41816）。
* provider 段里的键名写错一个字，同样整份配置加载失败。所以这里拿
  codex 二进制里抽出来的字段表当白名单，靠编造出来的键过不了。

真调模型的那一步**不在**这里（要凭据、要花钱），见 `scripts/verify_provider_planner.py`。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify_provider_matrix.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("verify_provider_matrix", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_module()

POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
CODEX = shutil.which("codex.cmd") or shutil.which("codex")

requires_powershell = pytest.mark.skipif(POWERSHELL is None, reason="本机没有 pwsh/powershell")


def _case(key: str):
    for case in matrix.CASES:
        if case.key == key:
            return case
    raise AssertionError(f"CASES 里没有 {key}")


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> dict:
    """一次生成三份配置，供只读断言复用（生成要起三次 PowerShell，别每个用例都起）。"""

    if POWERSHELL is None:
        pytest.skip("本机没有 pwsh/powershell")
    root = tmp_path_factory.mktemp("provider-matrix")
    out: dict[str, tuple[object, Path]] = {}
    for case in matrix.CASES:
        home = root / case.key
        home.mkdir(parents=True, exist_ok=True)
        proc = matrix.generate_config(case, home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        out[case.key] = (case, home)
    return out


# --- M1：三类端点都必须生成出来，且类别不重样 ---------------------------------


@requires_powershell
def test_m1_matrix_covers_three_distinct_endpoint_classes(generated):
    assert set(generated) == {"official", "gateway", "local"}
    urls = {case.base_url for case, _ in generated.values()}
    ids = {case.provider_id for case, _ in generated.values()}
    keys = {case.env_key for case, _ in generated.values()}
    assert len(urls) == 3, urls
    assert len(ids) == 3, ids
    # 本地推理一般不需要真凭据；如果三类共用同一个 env_key，那份配置其实没区分端点。
    assert len(keys) == 3, keys


# --- M2：显式落点必须写到指定目录，不碰工程状态根 -----------------------------


@requires_powershell
def test_m2_explicit_codex_home_wins_and_is_the_only_thing_written(tmp_path):
    case = _case("official")
    home = tmp_path / "isolated-home"
    proc = matrix.generate_config(case, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (home / "config.toml").exists()
    # 没绑定工程时脚本会回退 workspace/codex-home —— 显式落点不该再往那儿写。
    assert not (ROOT / "workspace" / "codex-home" / "config.toml").exists()


# --- M3：-NoEnv 一个环境变量都不动 --------------------------------------------


@requires_powershell
def test_m3_no_env_mode_leaves_environment_untouched(tmp_path):
    tracked = (
        "CODEX_BATTLEFIELD_RUNNER",
        "CODEX_BATTLEFIELD_CODEX_HOME",
        "CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG",
        "CODEX_BATTLEFIELD_CODEX_MODEL",
        "CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT",
        "OPENAI_API_KEY",
    )
    before = {name: os.environ.get(name) for name in tracked}
    home = tmp_path / "home"
    matrix.generate_config(_case("local"), home)
    after = {name: os.environ.get(name) for name in tracked}
    assert before == after


# --- M4：配置内容逐项对不对（含 wire_api 只有 responses） ---------------------


@requires_powershell
def test_m4_generated_config_matches_the_case(generated):
    for key, (case, home) in generated.items():
        result = matrix.inspect(case, home, with_binary=False)
        assert result["failures"] == [], (key, result["failures"])
        assert result["checks"]["wire_api"] == "responses"


@requires_powershell
def test_m4_gateway_case_only_writes_windows_sandbox_when_asked(generated):
    _, official_home = generated["official"]
    _, gateway_home = generated["gateway"]
    assert "sandbox" not in (official_home / "config.toml").read_text(encoding="utf-8")
    assert 'sandbox = "unelevated"' in (gateway_home / "config.toml").read_text(encoding="utf-8")


# --- M5：坏配置必须被抓住（判据能红的证明） -----------------------------------


def _write(home: Path, text: str) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_m5a_chat_wire_api_is_rejected(tmp_path):
    case = _case("official")
    _write(
        tmp_path,
        'model = "gpt-5-codex"\nmodel_provider = "official"\n\n[model_providers.official]\n'
        'name = "x"\nbase_url = "https://api.openai.com/v1"\nwire_api = "chat"\nenv_key = "OPENAI_API_KEY"\n',
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert any("wire_api" in f for f in result["failures"]), result["failures"]


def test_m5b_invented_provider_field_is_rejected(tmp_path):
    case = _case("local")
    _write(
        tmp_path,
        'model = "qwen2.5-coder:7b"\nmodel_provider = "ollama-local"\n\n[model_providers.ollama-local]\n'
        'name = "x"\nbase_url = "http://127.0.0.1:11434/v1"\nwire_api = "responses"\n'
        'env_key = "OLLAMA_API_KEY"\nmodel_max_output_tokens = 4096\n',
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert any("model_max_output_tokens" in f for f in result["failures"]), result["failures"]


def test_m5c_unknown_endpoint_key_is_rejected(tmp_path):
    """`endpoint`/`auth` 是允许出现的"计算键"，但其它编造的键必须被拦。"""

    case = _case("gateway")
    _write(
        tmp_path,
        'model = "team-default"\nmodel_provider = "gateway"\n\n[model_providers.gateway]\n'
        'name = "x"\nbase_url = "http://127.0.0.1:8080/v1"\nwire_api = "responses"\n'
        'env_key = "MY_GATEWAY_KEY"\napi_base = "http://127.0.0.1:8080/v1"\n',
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert any("api_base" in f for f in result["failures"]), result["failures"]


def test_m5d_bom_is_rejected(tmp_path):
    case = _case("official")
    path = tmp_path / "config.toml"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\xef\xbb\xbf"
        b'model = "gpt-5-codex"\nmodel_provider = "official"\n\n[model_providers.official]\n'
        b'name = "x"\nbase_url = "https://api.openai.com/v1"\nwire_api = "responses"\n'
        b'env_key = "OPENAI_API_KEY"\n'
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert any("BOM" in f for f in result["failures"]), result["failures"]


def test_m5e_wrong_provider_id_is_rejected(tmp_path):
    case = _case("gateway")
    _write(
        tmp_path,
        'model = "team-default"\nmodel_provider = "someone-else"\n\n[model_providers.gateway]\n'
        'name = "x"\nbase_url = "http://127.0.0.1:8080/v1"\nwire_api = "responses"\n'
        'env_key = "MY_GATEWAY_KEY"\n',
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert result["failures"], "model_provider 与段名不一致必须报错"


def test_m5f_credentials_in_config_are_rejected(tmp_path):
    case = _case("official")
    _write(
        tmp_path,
        'model = "gpt-5-codex"\nmodel_provider = "official"\n\n[model_providers.official]\n'
        'name = "x"\nbase_url = "https://api.openai.com/v1"\nwire_api = "responses"\n'
        'env_key = "OPENAI_API_KEY"\nexperimental_bearer_token = "sk-abc123"\n',
    )
    result = matrix.inspect(case, tmp_path, with_binary=False)
    assert any("凭据" in f for f in result["failures"]), result["failures"]


# --- M6：脚本级判据（不需要 PowerShell 也能跑） -------------------------------


def test_m6_use_provider_defaults_to_responses_and_never_defaults_to_chat():
    text = (ROOT / "scripts" / "use_provider.ps1").read_text(encoding="utf-8")
    assert '[ValidateSet("responses", "chat")][string]$WireApi = "responses"' in text
    assert '[string]$WireApi = "chat"' not in text
    assert "Write-Warning" in text, "选了 chat 必须告警，不能静默"


def test_m6_use_local_proxy_defaults_to_responses_too():
    text = (ROOT / "scripts" / "use_local_proxy.ps1").read_text(encoding="utf-8")
    assert '[ValidateSet("responses", "chat")][string]$WireApi = "responses"' in text
    assert '[string]$WireApi = "chat"' not in text


def test_m6_verify_all_runs_the_matrix_script():
    text = (ROOT / "scripts" / "verify_all.py").read_text(encoding="utf-8")
    assert "verify_provider_matrix.py" in text


def test_m6_docs_exist_and_name_the_three_classes():
    doc = (ROOT / "docs" / "provider-matrix.md").read_text(encoding="utf-8")
    for token in ("官方", "自建网关", "本地推理", "responses", "wire_api"):
        assert token in doc, token


def test_m6_shipped_example_config_also_uses_responses():
    """发出去的模板文件必须和脚本默认值一致 —— 用户会把这份抄进自己的配置。"""

    text = (ROOT / "scripts" / "provider.codex.toml.example").read_text(encoding="utf-8")
    assert 'wire_api = "responses"' in text
    assert 'wire_api = "chat"' not in text


def test_m6_setup_doc_records_the_chat_removal():
    doc = (ROOT / "docs" / "provider-setup.md").read_text(encoding="utf-8")
    assert "wire_api" in doc and "chat" in doc
    # 这条陷阱必须是"实测过"的形态，不能只写"不支持"。
    assert "invalid data" in doc


# --- M7：codex 真读得进去（本机没有 codex 时明确记为"没查"） ------------------


@pytest.mark.skipif(CODEX is None, reason="本机没有 codex，无法验证配置能否被读进去")
def test_m7_codex_can_load_every_generated_config(generated):
    for key, (_case, home) in generated.items():
        loaded = matrix.check_config_load(home / "config.toml")
        assert loaded is True, f"{key} 的配置 codex 读不进去"


@pytest.mark.skipif(CODEX is not None, reason="本机有 codex，应走真实验证")
def test_m7_reports_null_when_codex_is_absent():
    assert matrix.check_config_load(Path("does-not-matter")) is None


# --- M8：报告里"验过"和"没验"分开列 -------------------------------------------


def test_m8_report_separates_verified_from_unverified(tmp_path, capsys):
    """`--skip-binary-check` 时 `could_load_config` 必须是 null，而不是 true。

    注意"本机没有 codex"和"我选择不查"是同一件事的两种入口：都只能记 **没查**，
    不能记成通过 —— 这条断言就是防"把没查写成通过"。
    """

    code = matrix.main(["--work-dir", str(tmp_path), "--skip-binary-check"])
    payload = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert payload["binary_check"] is False
    assert "endpoint_reachable" in payload["not_verified_here"]
    for case in payload["cases"]:
        assert case["checks"]["could_load_config"] is None


def test_m8_with_skip_flag_the_matrix_still_passes(monkeypatch, tmp_path, capsys):
    code = matrix.main(["--work-dir", str(tmp_path), "--skip-binary-check"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["ok"] is True


def test_m8_quiet_mode_still_reports_which_part_was_not_checked(capsys, tmp_path):
    """门禁日志用 `--quiet`；"没查 binary"必须出现在输出里，否则读日志的人会当成通过。"""

    code = matrix.main(["--work-dir", str(tmp_path), "--skip-binary-check", "--quiet"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "未做" in out or "codex" in out
    assert "OK" in out


def test_m8_json_out_writes_full_report(tmp_path):
    target = tmp_path / "report.json"
    code = matrix.main(
        ["--work-dir", str(tmp_path / "work"), "--skip-binary-check", "--quiet", "--json-out", str(target)]
    )
    assert code == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert len(payload["cases"]) == 3


# --- M9：脚本自己在 CLI 上跑得通 ----------------------------------------------


@requires_powershell
def test_m9_script_runs_standalone_and_exits_zero(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--work-dir", str(tmp_path / "out")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True


# --- M10：矩阵里那三个 env_key 真的能被送到子进程 -----------------------------


def _runner(tmp_path: Path, **overrides):
    from app.config import Settings
    from app.runner import CodexExecRunner

    base = {"workspace_dir": tmp_path / "workspace", "codex_bin": "codex"}
    base.update(overrides)
    return CodexExecRunner(Settings(**base))


def test_m10_gateway_credential_survives_only_with_an_allowlisted_prefix(tmp_path, monkeypatch):
    """`docs/provider-setup.md` 陷阱 3.4 的**矩阵版本**。

    矩阵里自建网关用 `MY_GATEWAY_KEY`、本地推理用 `OLLAMA_API_KEY`，这两个前缀都**不在**
    默认放行名单里。子进程环境默认收窄，于是真实凭据被丢掉、runner 再补一个占位值 ——
    症状是"端点 401，而配置看起来完全正确"。

    这里用真实的 `CodexExecRunner._codex_env` 验两个方向：不放行会被丢（fail-closed 是对的），
    配了前缀就留得住（也证明放行这条通道有效）。
    """

    monkeypatch.setenv("MY_GATEWAY_KEY", "real-gateway-secret")
    # 对照组：`OPENAI_` 本来就在默认放行名单里。
    monkeypatch.setenv("OPENAI_API_KEY", "real-openai-secret")

    strict = _runner(tmp_path)._codex_env("worker")
    assert strict.get("MY_GATEWAY_KEY") is None, "默认不该把名单外的网关凭据交给子进程"
    assert strict.get("OPENAI_API_KEY") == "real-openai-secret"

    relaxed = _runner(tmp_path, codex_env_allow_prefixes="CODEX_,OPENAI_,MY_GATEWAY_")._codex_env("worker")
    assert relaxed.get("MY_GATEWAY_KEY") == "real-gateway-secret"


def test_m10_matrix_env_keys_are_named_in_the_allowlist_docs():
    """文档必须点名这三个 env_key —— 它们的默认前缀不在放行名单里，用户不知道就会踩 401。"""

    doc = (ROOT / "docs" / "provider-matrix.md").read_text(encoding="utf-8")
    setup = (ROOT / "docs" / "provider-setup.md").read_text(encoding="utf-8")
    for key in ("OPENAI_API_KEY", "MY_GATEWAY_KEY", "OLLAMA_API_KEY"):
        assert key in doc, key
    assert "CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES" in setup
    assert "CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES" in doc
