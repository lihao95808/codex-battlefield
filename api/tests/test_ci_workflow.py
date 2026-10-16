"""CI 工作流的判据（P6-3）。

## 这一层判据的难点：我在本机跑不了 GitHub Actions

所以不能断言"CI 会绿" —— 那是**我无法验证的承诺**。能验证的是那些**必然导致 CI 无用**
的具体缺陷，每一条都对应一种真实发生过的失败：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| W1 | 工作流是**合法 YAML** 且结构完整 | 语法错 → GitHub 直接不认这个工作流，而本机看不出来 |
| W2 | 在 **Windows 与 Linux 两个平台**上跑 | "只有本机能过"正是 P6-3 要挡的东西 |
| W3 | 跑的是**同一套门禁**（`scripts/verify_all.py`） | CI 跑另一套命令 → 两边判据不同，CI 绿不代表本机绿 |
| W4 | **不带 `--real-smoke` / `--real-recursive-probe`** | CI 里没有 Codex CLI 与凭据 → 必然红，而那不是代码坏了 |
| W5 | 显式设 `RUNNER=mock` | 少一次"CI 上偷偷调了真模型"的可能 |
| W6 | 跑前装好依赖（venv + pnpm install） | 缺步 → CI 红在一个与代码无关的地方 |
| W7 | `fail-fast: false` | 一个平台失败会取消另一个 → 只看到一边，误以为另一边是好的 |
| W8 | 失败时收集诊断 | PR 上只看到 `exit 1`，还得本地复现一遍 |
| W9 | **工作流与 `verify_all` 的实际步骤对得上** | `verify_all` 加了新步骤而 CI 没跟上（或反之），两边悄悄分叉 |
| W10 | 子进程的 **stdout 编码**被钉成 UTF-8 | Windows runner 是 cp1252 → 第一个打印中文的步骤就 `UnicodeEncodeError` 退出（**真实红过**：run 38895747296 / 38943620205 都死在「文档排版检查」） |
| W11 | 单个脚本在 cp1252 的 stdout 下也**不崩** | 有人直接 `python scripts/check_markdown_layout.py .`（不走 verify_all）时炸在打印摘要上 |

W9 是最有价值的一条：它防的是**两份"门禁"慢慢变成不同的东西**。
做法是从 `verify_all.py` 源码里抽出它的步骤名，再核 CI 里的注释是否提到它们 ——
比硬编码一份清单可靠（硬编码会在改 `verify_all` 时过期）。

W10/W11 是**真 CI 反馈**换来的：这两条不是想出来的，是 GitHub Actions 在 Windows 上
红给我看的（本机中文 Windows 是 cp936，永远看不到）。它们的形态值得记住：
**同一个脚本、同一份代码，只因为"输出编码由环境决定"就整条门禁死在第一步。**
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import textwrap

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
VERIFY_ALL = ROOT / "scripts" / "verify_all.py"
MARKDOWN_CHECKER = ROOT / "scripts" / "check_markdown_layout.py"


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def verify_steps() -> list[str]:
    """`verify_all.py` 里**无条件**跑的步骤名（单一真源）。

    只取无条件的那些：`--real-smoke` / `--real-recursive-probe` 底下的两步在 CI 里
    **故意不跑**（没有 Codex CLI 与凭据），把它们算进来会让这条判据要求 CI 去声明
    "我不跑的东西" —— 那是噪声，不是同步。

    实现：逐行扫，遇到 `if args.real_...` 就记下缩进，跳过更深缩进的步骤登记。
    """
    source = VERIFY_ALL.read_text(encoding="utf-8")
    names: list[str] = []
    in_optional = False
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith("if args.real"):
            in_optional = True
            continue
        # 回到 `if` 那一层的缩进就说明可选块结束了。
        if in_optional and stripped and not line.startswith("    " * 2):
            in_optional = False
        if in_optional:
            continue
        match = re.match(r'\s*run_(?:step|check)\(\s*"([^"]+)"', line)
        if match:
            names.append(match.group(1))
    return names


def _all_runs(workflow: dict) -> list[str]:
    return [
        str(step.get("run", ""))
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if step.get("run")
    ]


def _all_uses(workflow: dict) -> list[str]:
    return [
        str(step.get("uses", ""))
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if step.get("uses")
    ]


def _matrix_os(workflow: dict) -> list[str]:
    values: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        values.extend(str(item) for item in (matrix.get("os") or []))
    return values


# ---------- W1 合法且完整 ----------


def test_w1_the_workflow_is_valid_yaml_with_a_job(workflow: dict) -> None:
    assert isinstance(workflow, dict), "工作流不是 YAML 映射"
    assert workflow.get("jobs"), "工作流里没有 job —— GitHub 不会跑它"
    for name, job in workflow["jobs"].items():
        assert job.get("runs-on"), f"{name} 没有 runs-on"
        assert job.get("steps"), f"{name} 没有 steps"


def test_w1_it_triggers_on_pull_requests_and_main(workflow: dict) -> None:
    # YAML 会把裸 `on:` 解析成布尔 True —— 两种键都要认。
    triggers = workflow.get("on", workflow.get(True))
    assert triggers, "没有 on: —— 工作流不会自动跑"
    text = str(triggers)
    assert "pull_request" in text, "PR 上不跑就挡不住'只有本机能过'的提交"
    assert "push" in text


# ---------- W2 双平台 ----------


def test_w2_it_runs_on_both_windows_and_linux(workflow: dict) -> None:
    """**P6-3 的核心**：Windows 优先写出来的仓库，必须有 Linux 证据。"""
    systems = _matrix_os(workflow)
    assert systems, "没有 os 矩阵 —— 单平台不算跨平台验证"
    assert any("windows" in item for item in systems), systems
    assert any("ubuntu" in item for item in systems), systems
    assert len(systems) == 2, f"平台数是 {len(systems)}：{systems}（每个平台都是一份等待时间）"


# ---------- W3 / W4 / W5 跑同一套门禁 ----------


def test_w3_ci_runs_the_same_gate_as_the_local_one(workflow: dict) -> None:
    """CI 跑另一套命令的话，两边判据不同 —— CI 绿不代表本机绿。"""
    runs = _all_runs(workflow)
    assert any("verify_all.py" in text for text in runs), (
        "CI 没有跑 scripts/verify_all.py —— 那是本机用的同一套门禁"
    )


@pytest.mark.parametrize("flag", ["--real-smoke", "--real-recursive-probe"])
def test_w4_real_mode_flags_are_absent(workflow: dict, flag: str) -> None:
    """CI 里没有 Codex CLI 与凭据 → 真实档必然红，而那不是"代码坏了"。"""
    for text in _all_runs(workflow):
        assert flag not in text, f"CI 传了 {flag} —— 在没有凭据的机器上必然失败"


def test_w5_the_runner_is_pinned_to_mock(workflow: dict) -> None:
    """显式钉住 mock：少一次"CI 上偷偷调了真模型"的可能。"""
    env_seen = False
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            env = step.get("env") or {}
            if str(env.get("CODEX_BATTLEFIELD_RUNNER", "")).lower() == "mock":
                env_seen = True
            if str((job.get("env") or {}).get("CODEX_BATTLEFIELD_RUNNER", "")).lower() == "mock":
                env_seen = True
    assert env_seen, "没有把 RUNNER 钉成 mock"


# ---------- W6 装依赖 ----------


def test_w6_dependencies_are_installed_before_the_gate(workflow: dict) -> None:
    runs = " ".join(_all_runs(workflow))
    assert "pip install" in runs, "没有装后端依赖"
    assert "pnpm install" in runs or "pnpm i" in runs, "没有装前端依赖"
    assert "venv" in runs, "没有建 venv（仓库约定：api/.venv）"


def test_w6_it_checks_out_and_sets_up_python_and_node(workflow: dict) -> None:
    uses = " ".join(_all_uses(workflow))
    assert "actions/checkout" in uses
    assert "setup-python" in uses
    assert "setup-node" in uses
    assert "pnpm/action-setup" in uses


def test_w6_the_python_version_matches_the_project_requirement(workflow: dict) -> None:
    """`requires-python = ">=3.12,<3.14"` —— CI 用别的版本会跑出一个假结果。"""
    requirement = re.search(
        r'requires-python\s*=\s*"([^"]+)"', (ROOT / "api" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert requirement, "读不到 requires-python"
    declared = requirement.group(1)
    assert "3.12" in declared

    versions = [
        str((step.get("with") or {}).get("python-version", ""))
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]
    assert any("3.12" in item for item in versions), f"CI 的 Python 版本与项目要求不符：{versions}"


# ---------- W7 一个平台失败不取消另一个 ----------


def test_w7_fail_fast_is_disabled(workflow: dict) -> None:
    for name, job in (workflow.get("jobs") or {}).items():
        strategy = job.get("strategy") or {}
        if strategy.get("matrix"):
            assert strategy.get("fail-fast") is False, (
                f"{name} 的 fail-fast 不是 false —— 一个平台失败会取消另一个，"
                "而两边的失败原因常常不同"
            )


# ---------- W8 收集诊断 ----------


def test_w8_there_is_a_diagnostics_step(workflow: dict) -> None:
    steps = [
        step
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]
    assert any(str(step.get("if", "")) == "failure()" for step in steps), (
        "没有 if: failure() 的步骤 —— PR 上只能看到 exit 1"
    )


# ---------- W9 与 verify_all 同步 ----------


def test_w9_the_workflow_mentions_every_verify_all_step(verify_steps: list[str]) -> None:
    """防的是**两份门禁慢慢变成不同的东西**。

    做法：从 `verify_all.py` 里抽出步骤名，再核 CI 注释里提到它们。
    比硬编码一份清单可靠 —— 硬编码会在改 `verify_all` 时过期。
    """
    assert verify_steps, "抽不到 verify_all 的步骤名 —— 这条判据会空转"
    text = WORKFLOW.read_text(encoding="utf-8")
    missing = [name for name in verify_steps if name not in text]
    assert missing == [], (
        f"CI 注释里没提到这些门禁步骤：{missing} —— "
        "要么是 verify_all 新增了步骤而 CI 没跟上，要么是注释过期了"
    )


def test_w9_verify_all_still_exists_and_is_the_gate(verify_steps: list[str]) -> None:
    assert VERIFY_ALL.is_file()
    # 步骤清单本身也要有内容 —— 否则上一条会因为"没有步骤"而空过。
    assert len(verify_steps) >= 6, verify_steps
    assert "后端 pytest" in verify_steps
    assert "前端 build" in verify_steps


# ---------- W10/W11 子进程编码（CI 上真实红过的一条） ----------


def _cp1252_env() -> dict[str, str]:
    """模拟 GitHub **Windows runner** 的 Python stdout 默认编码。"""

    return {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}


def test_w10_verify_all_pins_utf8_for_subprocesses(tmp_path: pathlib.Path) -> None:
    """`verify_all` 的每个子进程都必须拿到 UTF-8 —— 用**真子进程**验，不查源码字符串。

    探针是"用 `verify_all.run_step` 起一个打印中文的子进程"：把 stdout 环境设成 cp1252
    （= CI 的 Windows runner），如果 `step_env()` 没把编码钉回来，子进程会
    `UnicodeEncodeError` 退出，`run_step` 抛 `SystemExit` → 探针退出码非 0。
    """

    probe = tmp_path / "encoding_probe.py"
    probe.write_text(
        textwrap.dedent(
            f"""
            import importlib.util
            import pathlib
            import sys

            spec = importlib.util.spec_from_file_location("verify_all_under_test", r"{VERIFY_ALL}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.run_step(
                "编码探针",
                [sys.executable, "-c", "print('中文摘要不会崩')"],
                pathlib.Path(r"{ROOT}"),
            )
            print("PROBE-OK")
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(probe)],
        cwd=ROOT,
        env=_cp1252_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=180,
    )
    assert proc.returncode == 0, f"cp1252 下子进程打印中文就崩（= CI 上 Windows 那一侧）：{proc.stdout[-800:]}"
    assert "PROBE-OK" in proc.stdout, proc.stdout
    assert "中文摘要不会崩" in proc.stdout, proc.stdout


def test_w10_workflow_sets_the_same_encoding(workflow: dict) -> None:
    """工作流自己也要设 —— 它还有"直接调 python"的步骤（例如收集诊断）。"""

    envs = [
        (step.get("env") or {})
        for job in (workflow.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    ]
    assert any(
        str(values.get("PYTHONIOENCODING", "")).lower().replace("-", "") in ("utf8", "utf8mb4", "cp65001")
        for values in envs
    ), "工作流里没有任何一步设 PYTHONIOENCODING=utf-8 —— Windows runner 上打印中文就会红"


def test_w11_markdown_checker_survives_cp1252_stdout() -> None:
    """不走 verify_all 时，单个脚本也必须能打印摘要（编码改不了就退化，不许崩）。"""

    proc = subprocess.run(
        [sys.executable, str(MARKDOWN_CHECKER), "."],
        cwd=ROOT,
        env=_cp1252_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, f"cp1252 下文档排版检查崩了：{proc.stdout[-500:]} {proc.stderr[-800:]}"
    assert "个 .md" in proc.stdout, f"摘要没打出来：{proc.stdout[-300:]}"


# ---------- 我**不能**验证的事（如实写下来） ----------


def test_the_workflow_file_is_the_only_ci_config() -> None:
    """多加一份 CI 配置会让"门禁在哪跑"有两个答案 —— 而它们会分叉。"""
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert [item.name for item in workflows] == ["verify.yml"], workflows
