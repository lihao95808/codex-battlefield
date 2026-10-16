"""离线核对 provider 矩阵：三类端点各生成一份隔离配置，再逐项验它**能不能被 codex 读进去**。

为什么要单独有这么一个脚本（`docs/provider-matrix.md`）：

* `verify_provider_planner.py` 是**真调一次模型**的（要凭据、要花钱、端点要活着），
  所以"配置写错了"和"端点不通"这两件事在那里面是混在一起的；
* 这个脚本**不发任何模型请求**，只做"配置→codex 读取"这一段，于是它能在
  没有凭据、没有网络的机器上跑，也能进 CI；
* 但它**不能**证明"这个端点真能跑 planner"——那条只能靠 `verify_provider_planner.py`。
  报告里 `probe` 字段把"验过什么"和"没验什么"分开写，不合成一个 ok。

三类端点（P6-5 要求的矩阵）：

| 类别 | 端点 | 谁在跑 |
|---|---|---|
| 官方 | `https://api.openai.com/v1` | OpenAI 自己的 Responses API |
| 自建网关 | `http://127.0.0.1:8080/v1` | 公司网关 / 本机转发，凭证在网关侧 |
| 本地推理 | `http://127.0.0.1:11434/v1` | Ollama 一类本机进程，通常不校验 key |

配置一律由 `scripts/use_provider.ps1 -CodexHome <临时目录> -NoEnv` 生成 —— 走的是
**用户真正会跑的那条路径**，而不是脚本自己拼一份"看起来一样"的 TOML。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# `codex doctor` 在"配置能加载"时也会以非 0 退出（它把安全建议当失败报），
# 所以**不能**用退出码判断，只能看输出里有没有这句。
CONFIG_LOAD_FAILURE = "config could not be loaded"

# 从 codex 0.162.1 的二进制里抽出来的 `struct ModelProviderInfo with 21 elements`
# 字段表（抽取方法见 `docs/provider-matrix.md`）。用途是**挡住编造的配置键**：
# 写错一个键名 codex 会整份配置加载失败，而失败信息只有 `error invalid data`，
# 根本指不到是哪一行 —— 这条清单让错误停在测试里，而不是停在用户的节点上。
MODEL_PROVIDER_FIELDS = frozenset(
    {
        "name",
        "base_url",
        "model_catalog_url",
        "env_key",
        "env_key_instructions",
        "experimental_bearer_token",
        "gateway_oauth",
        "aws",
        "wire_api",
        "capabilities",
        "query_params",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "websocket_connect_timeout_ms",
        "requires_openai_auth",
        "supports_websockets",
        "supports_standalone_web_search",
        # 计算出来的键：允许用户写，但**不是**结构体字段（`ModelProviderInfo` 收的是
        # 其余 21 个）。留在这里是为了让测试能明确区分"我认识的"和"编号外的"。
        "endpoint",
        "auth",
    }
)

# 顶层必须出现的键（缺了 codex 会回落到默认 provider，症状是"改了配置没生效"）。
REQUIRED_TOP_LEVEL = ("model", "model_provider")

# wire_api 的取值。**只有 responses**：`chat` 在新版 codex 上不存在这个变体，
# 写了就是整份配置加载失败（实测 + 上游 issue 41816）。
WIRE_API_SUPPORTED = frozenset({"responses"})
WIRE_API_REJECTED = frozenset({"chat", "chat_completions", "responses_api"})


@dataclass(frozen=True)
class ProviderCase:
    key: str
    title: str
    base_url: str
    env_key: str
    model: str
    provider_id: str
    provider_name: str
    reasoning_effort: str
    context_window: int
    windows_sandbox: str = ""


CASES: tuple[ProviderCase, ...] = (
    ProviderCase(
        key="official",
        title="官方 API",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        model="gpt-5-codex",
        provider_id="official",
        provider_name="OpenAI official",
        reasoning_effort="medium",
        context_window=128000,
    ),
    ProviderCase(
        key="gateway",
        title="自建网关 / 本机转发",
        base_url="http://127.0.0.1:8080/v1",
        env_key="MY_GATEWAY_KEY",
        model="team-default",
        provider_id="gateway",
        provider_name="Self-hosted gateway",
        reasoning_effort="medium",
        context_window=200000,
        # 网关是别人的进程，Windows 沙箱档由本机决定。
        windows_sandbox="unelevated",
    ),
    ProviderCase(
        key="local",
        title="本地推理（Ollama 类）",
        base_url="http://127.0.0.1:11434/v1",
        env_key="OLLAMA_API_KEY",
        model="qwen2.5-coder:7b",
        provider_id="ollama-local",
        provider_name="Ollama local (OpenAI compatible)",
        # 本地小模型的推理档位只有 low 有意义，给 medium 只是浪费 token。
        reasoning_effort="low",
        context_window=32768,
    ),
)


class MatrixError(RuntimeError):
    """配置生成失败（不是判据失败）——脚本自身环境有问题，别和"配置不合格"混一起。"""


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    raise MatrixError("找不到 pwsh / powershell，无法生成 provider 配置。")


def generate_config(case: ProviderCase, home: Path, *, force: bool = True) -> subprocess.CompletedProcess[str]:
    """走用户真正会跑的那条路生成配置：`use_provider.ps1 -CodexHome <home> -NoEnv`。"""

    script = ROOT / "scripts" / "use_provider.ps1"
    args = [
        _powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        "-BaseUrl",
        case.base_url,
        "-EnvKey",
        case.env_key,
        "-Model",
        case.model,
        "-ProviderId",
        case.provider_id,
        "-ProviderName",
        case.provider_name,
        "-ReasoningEffort",
        case.reasoning_effort,
        "-ContextWindow",
        str(case.context_window),
        "-CodexHome",
        str(home),
        "-NoEnv",
    ]
    if case.windows_sandbox:
        args += ["-WindowsSandbox", case.windows_sandbox]
    if force:
        args.append("-Force")
    env = dict(os.environ)
    # -NoEnv 已经保证脚本不改环境，这里再隔离一层：即使将来有人去掉 -NoEnv，
    # 也污染不到本进程。
    env.pop("CODEX_BATTLEFIELD_RUNNER", None)
    env.pop("CODEX_BATTLEFIELD_CODEX_HOME", None)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, check=False)


def check_config_load(config_path: Path) -> bool | None:
    """跑 `codex doctor` 看这份配置能不能被读进去。

    返回 `True`（读进去了）/ `False`（codex 明确报加载失败）/ `None`（本机没有 codex，
    **没查**——不是"没问题"）。
    """

    codex = shutil.which("codex.cmd") or shutil.which("codex")
    if not codex:
        return None
    env = dict(os.environ)
    env["CODEX_HOME"] = str(config_path.parent)
    proc = subprocess.run(
        [codex, "doctor"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=180,
    )
    blob = f"{proc.stdout}\n{proc.stderr}"
    return CONFIG_LOAD_FAILURE not in blob


def _provider_table(document: dict, provider_id: str) -> dict:
    providers = document.get("model_providers")
    if not isinstance(providers, dict) or provider_id not in providers:
        raise AssertionError(f"配置里没有 [model_providers.{provider_id}] 段")
    table = providers[provider_id]
    if not isinstance(table, dict):
        raise AssertionError(f"[model_providers.{provider_id}] 不是一张表")
    return table


def inspect(case: ProviderCase, home: Path, *, with_binary: bool) -> dict:
    """对生成的配置做逐项判据。任何一条不成立都记进 `failures`，不短路。"""

    config_path = home / "config.toml"
    failures: list[str] = []
    checks: dict[str, object] = {}

    if not config_path.exists():
        return {
            "key": case.key,
            "title": case.title,
            "config_path": str(config_path),
            "failures": [f"没生成 config.toml：{config_path}"],
            "checks": {},
        }

    raw = config_path.read_bytes()

    # codex 的 config.toml 是 UTF-8；带 BOM 会让第一行的键名带上隐形前缀。
    # **必须在解析之前查**：`tomllib.loads` 遇到 BOM 会直接抛 TOMLDecodeError，
    # 那样"为什么解析不了"就永远看不到 BOM 这条真实原因。
    checks["no_bom"] = not raw.startswith(b"\xef\xbb\xbf")
    if not checks["no_bom"]:
        failures.append("config.toml 带 UTF-8 BOM（codex 读第一行的键会带上隐形前缀）")

    try:
        document = tomllib.loads(raw.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        checks["toml_parses"] = False
        failures.append(f"TOML 解析失败：{exc}")
        return {
            "key": case.key,
            "title": case.title,
            "config_path": str(config_path),
            "checks": checks,
            "failures": failures,
        }

    # BOM 已经查过并记进 failures 了，这里用 utf-8-sig 解码，让后续判据还能继续跑完
    # （否则"有 BOM"会连带出一堆假的"键不存在"）。
    checks["toml_parses"] = True

    for key in REQUIRED_TOP_LEVEL:
        if key not in document:
            failures.append(f"顶层缺 `{key}`（codex 会回落到默认 provider，症状是配置不生效）")
    checks["required_top_level"] = [k for k in REQUIRED_TOP_LEVEL if k in document]

    if document.get("model_provider") != case.provider_id:
        failures.append(
            f"model_provider = {document.get('model_provider')!r}，期望 {case.provider_id!r}"
        )
    if document.get("model") != case.model:
        failures.append(f"model = {document.get('model')!r}，期望 {case.model!r}")

    table = _provider_table(document, case.provider_id)

    for field, expected in (
        ("base_url", case.base_url),
        ("env_key", case.env_key),
        ("wire_api", "responses"),
    ):
        if table.get(field) != expected:
            failures.append(f"[model_providers.{case.provider_id}].{field} = {table.get(field)!r}，期望 {expected!r}")

    # 编造的键：codex 只会回一句 `error invalid data`，指不到是哪一行。
    unknown = sorted(set(table) - MODEL_PROVIDER_FIELDS)
    checks["unknown_provider_fields"] = unknown
    if unknown:
        failures.append(f"provider 段里有 codex 不认识的键：{unknown}")

    wire = table.get("wire_api")
    checks["wire_api"] = wire
    if wire in WIRE_API_REJECTED:
        failures.append(f"wire_api = {wire!r} 在新版 codex 上已被移除，会让整份配置加载失败")
    elif wire not in WIRE_API_SUPPORTED:
        failures.append(f"wire_api = {wire!r} 不在已核实可用的取值 {sorted(WIRE_API_SUPPORTED)} 里")

    # Windows 沙箱档：只在显式要求时出现，且不能写成 codex 不认的值。
    windows = document.get("windows")
    checks["windows_sandbox"] = windows.get("sandbox") if isinstance(windows, dict) else None
    if case.windows_sandbox:
        if checks["windows_sandbox"] != case.windows_sandbox:
            failures.append(f"[windows].sandbox = {checks['windows_sandbox']!r}，期望 {case.windows_sandbox!r}")
    elif isinstance(windows, dict) and windows.get("sandbox"):
        failures.append("没要求写 [windows] 段，但配置里有 —— 会让节点沙箱档位与预期不符")

    # 生成的配置里不能出现真实凭据（脚本只该写变量名）。
    text = raw.decode("utf-8")
    for secret in ("sk-", "Bearer "):
        if secret in text:
            failures.append(f"配置里出现了疑似凭据的字样 {secret!r}：隔离配置只该写变量名")

    could_load: bool | None = None
    if with_binary:
        could_load = check_config_load(config_path)
        if could_load is False:
            failures.append("codex 读不进这份配置（doctor 报 config could not be loaded）")
    checks["could_load_config"] = could_load

    return {
        "key": case.key,
        "title": case.title,
        "config_path": str(config_path),
        "checks": checks,
        "failures": failures,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线核对三类 provider 端点的隔离配置。")
    parser.add_argument(
        "--work-dir",
        default="",
        help="生成配置的落点；留空则用临时目录（结束后删除）。",
    )
    parser.add_argument(
        "--skip-binary-check",
        action="store_true",
        help="不跑 codex doctor。跳过时报告里 could_load_config 记 null（**没查**，不是通过）。",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只打印一行摘要 + 每个端点的结果（门禁日志用；完整报告仍可加 --json-out）。",
    )
    parser.add_argument("--json-out", default="", help="把完整报告写到这个文件（utf-8）。")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with_binary = not args.skip_binary_check and bool(shutil.which("codex.cmd") or shutil.which("codex"))

    if args.work_dir:
        work_root = Path(args.work_dir).resolve()
        work_root.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work_root = Path(tempfile.mkdtemp(prefix="cbh-provider-matrix-"))
        cleanup = True

    report: dict[str, object] = {
        "work_dir": str(work_root),
        # 机器可读的两张清单：报告里"验过"和"没验"分开列，不合成一个 ok。
        "verified_here": [
            "config_generated_by_use_provider_ps1",
            "toml_parses_without_bom",
            "required_top_level_keys_present",
            "provider_fields_known_to_codex",
            "wire_api_in_verified_set",
            "codex_can_load_config",
        ],
        "not_verified_here": [
            "endpoint_reachable",
            "planner_worker_actually_run",
            "credentials_accepted",
        ],
        "binary_check": with_binary,
        "cases": [],
    }

    for case in CASES:
        home = work_root / case.key
        home.mkdir(parents=True, exist_ok=True)
        proc = generate_config(case, home)
        if proc.returncode != 0:
            raise MatrixError(f"{case.key} 生成配置失败（exit {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}")
        report["cases"].append(inspect(case, home, with_binary=with_binary))  # type: ignore[union-attr]

    cases = report["cases"]
    assert isinstance(cases, list)
    report["ok"] = all(not case["failures"] for case in cases)
    blob = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json_out:
        Path(args.json_out).write_text(blob, encoding="utf-8")

    if args.quiet:
        # 门禁日志里不该塞几十行 JSON；但"没查 binary"这件事必须出现在输出里，
        # 否则读日志的人会把"没查"当成"通过"。
        checked = [case["key"] for case in cases if case["checks"].get("could_load_config") is not None]
        print(
            f"provider 矩阵：{len(cases)} 类端点，全部结构判据通过={report['ok']}；"
            f"codex 实际加载验证={checked or '未做（本机没有 codex）'}"
        )
        for case in cases:
            mark = "OK  " if not case["failures"] else "FAIL"
            print(f"  {mark} {case['key']:9} {case['title']} -> {case['config_path']}")
    else:
        print(blob)

    if cleanup:
        shutil.rmtree(work_root, ignore_errors=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
