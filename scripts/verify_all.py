from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 静态检查**覆盖的路径**。单独列成常量，是为了让"门禁范围"本身可被测试盯住
# （`api/tests/test_repo_gates.py`）——门禁少了一个目录不会报错，只会安静地变弱，
# 而"安静的变弱"正是这一带踩过的坑：`scripts/` 曾长期不在任何 lint 范围内。
LINT_PATHS = ("api", "scripts")

# 「显式可选字段」例外表：`{schema 文件名: {字段名}}`。**2026-10-14 起为空**。
#
# 门禁的默认规则是"声明即必填"。曾经有一处例外：`planner-decision` 的 `block_report`
# 只在 `decision=blocked` 时才该出现，所以被排除在 `required` 之外。
#
# 那个做法被实测推翻：OpenAI 的结构化输出是**严格模式**——
# `additionalProperties: false` 时，`required` 必须列出 `properties` 里的每一个 key，否则：
#
#     Invalid schema for response_format 'codex_output_schema':
#     'schema.required' is required to include every key in properties. Missing 'block_report'.
#
# 结果是真实 planner 每次 HTTP 400 必然失败。现在的做法是把"可空"表达进 schema：
# `block_report: { anyOf: [{ $ref }, { type: "null" }] }` —— key 必填、值可空，两个约束同时成立。
# 于是例外表清空，"声明即必填"重新变成没有例外的规则。
#
# 表仍然留着（而不是删掉）：它是一条**逃生通道的记录**，`api/tests/test_repo_gates.py` 盯着它必须为空。
OPTIONAL_SCHEMA_FIELDS: dict[str, set[str]] = {}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)


def run_check(name: str, check: Callable[[], None]) -> None:
    """跑一个**进程内的**检查步骤（不需要起子进程的那种）。

    与 `run_step` 的差别只是没有 `command` 可打印；失败同样**阻断**。
    """
    print(f"\n==> {name}")
    check()


def run_step(name: str, command: list[str], cwd: Path) -> None:
    print(f"\n==> {name}")
    print(f"目录：{cwd}")
    print(f"命令：{' '.join(command)}")
    result = subprocess.run(command, cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def lint_command() -> list[str]:
    """静态检查命令。单独成函数是为了让测试能断言**它到底覆盖了哪些目录**
    （`api/tests/test_repo_gates.py`）——`LINT_PATHS` 被改小时，测试会红。"""
    return [
        sys.executable,
        "-m",
        "ruff",
        "check",
        # 显式指到 `api/pyproject.toml`：否则根目录下"没有配置"会被 ruff 当成用默认规则放行，
        # 那会变成一条恒真的门禁（选什么规则集由这个文件决定，看 `[tool.ruff.lint]` 的注释）。
        "--config",
        str(ROOT / "api" / "pyproject.toml"),
        *[str(ROOT / path) for path in LINT_PATHS],
    ]


def validate_schemas() -> None:
    print("\n==> JSON Schema 解析")
    schema_dir = ROOT / "schemas"
    for path in sorted(schema_dir.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert_strict_schema(path, schema)
        print(f"通过：{path.name}")


def assert_strict_schema(path: Path, schema: object) -> None:
    optional_fields = OPTIONAL_SCHEMA_FIELDS.get(path.name, set())

    def walk(value: object, location: str) -> None:
        if isinstance(value, dict):
            if "default" in value:
                raise SystemExit(f"{path.name} {location} 不允许包含 default。")
            if value.get("type") == "object":
                properties = set((value.get("properties") or {}).keys())
                required = set(value.get("required") or [])
                if value.get("additionalProperties") is not False:
                    raise SystemExit(f"{path.name} {location} 必须 additionalProperties=false。")
                if properties and required != properties:
                    missing = sorted((properties - required) - optional_fields)
                    extra = sorted(required - properties)
                    if missing or extra:
                        raise SystemExit(f"{path.name} {location} required 字段不完整：missing={missing} extra={extra}")
            for key, child in value.items():
                walk(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]")

    walk(schema, "$")


def check_ps1_encoding() -> None:
    """非 ASCII 的 `.ps1` **必须带 UTF-8 BOM**（`docs/spec-watchdog-steward.md` §5）。

    为什么这是一条门禁而不是一条笔记：

    **PowerShell 5.1 在没有 BOM 时按系统 ANSI 代码页解码脚本。** 本机是 GBK，
    于是含中文注释的 UTF-8 `.ps1` 被解成乱码，`}` 之类结构字符落在字符中间，
    表现是一串看不懂的语法错误（`Unexpected token '}'`）—— 而**文件本身完全正确**。

    代价是一次真事件：`battlefield_watchdog.ps1` 有 3165 个非 ASCII 字节且没有 BOM，
    于是 `powershell.exe -File` **直接语法错、根本跑不起来**（0 errors → 4 errors）；
    而它只有 `ParseInput`（先按 UTF-8 读进来再解析）看起来正常 —— 那正是当初误判
    "这不是真 bug，只是 PS 5.1 的解码怪癖"的原因。

    失效方向：**阻断**。这个坑的隐蔽之处在于"文件是对的"，只靠读代码看不出来。
    """
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*.ps1")):
        if any(part in {".git", "node_modules", ".venv"} for part in path.parts):
            continue
        raw = path.read_bytes()
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        non_ascii = sum(1 for byte in raw if byte > 127)
        if non_ascii and not has_bom:
            problems.append(
                f"{path.relative_to(ROOT)}：有 {non_ascii} 个非 ASCII 字节却没有 UTF-8 BOM。"
                "PowerShell 5.1 会按系统 ANSI 代码页解码它，脚本将无法运行。"
            )
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        raise SystemExit("PowerShell 脚本编码检查未通过。")
    print("  所有含非 ASCII 的 .ps1 都带 BOM。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Codex Battlefield 全套验证。")
    parser.add_argument("--real-smoke", action="store_true", help="额外运行真实 Codex CLI 文件修改烟测。")
    parser.add_argument("--real-recursive-probe", action="store_true", help="额外运行真实 Codex CLI 递归链路探针。")
    parser.add_argument("--real-constitution", action="store_true", help="真实 smoke 的宪法生成也使用真实 Codex。")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI 可执行文件。")
    parser.add_argument("--model", default="", help="真实 Codex CLI 使用的模型；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--reasoning-effort", default="", help="真实 Codex CLI 推理强度；默认留空，使用 Codex CLI 普通默认档。")
    parser.add_argument("--timeout", type=int, default=300, help="真实 Codex 单次调用超时秒数。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    validate_schemas()

    run_step("文档排版检查", [sys.executable, str(ROOT / "scripts" / "check_markdown_layout.py"), "."], ROOT)

    run_step("后端语法检查", [sys.executable, "-m", "compileall", "app"], ROOT / "api")
    # 放在 pytest 之前：编码错了的话 PowerShell 脚本根本跑不起来，
    # 而 pytest 用的是 Python 解码（`encoding="utf-8"`），**发现不了**这个问题。
    run_check("PowerShell 脚本编码检查", check_ps1_encoding)
    # **`scripts/` 也必须在门禁里**：这行以前只从 `api/` 里跑 `ruff check .`，于是仓库根的
    # `scripts/*.py` 谁都没检查过。代价是一次真事件：`bench_run.py` 里 `Path(...)` 少了个
    # import（F821），只在 **real 档**才走到那一行（mock 档没有 worktree，短路了），
    # 结果两轮真实基准都在"判据求值前"崩掉、连报告一起带走，而门禁全程是绿的。
    # 失效方向：**阻断**——脚本静态检查不过就别发。
    run_step("仓库静态检查（ruff：api + scripts）", lint_command(), ROOT)
    run_step("后端 pytest", [sys.executable, "-m", "pytest"], ROOT / "api")

    # provider 矩阵：三类端点各生成一份隔离配置，逐项验"codex 读不读得进去"。
    # **不发任何模型请求**，所以能进 CI（要花凭据的那条是 `verify_provider_planner.py`，
    # 只在 `--real-*` 档里跑）。失效方向：**阻断** —— 配置写错的症状是所有节点一起起不来。
    run_step(
        "provider 矩阵配置检查",
        [sys.executable, str(ROOT / "scripts" / "verify_provider_matrix.py"), "--quiet"],
        ROOT,
    )

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("找不到 pnpm，无法运行前端验证。")
    run_step("前端测试", [pnpm, "test"], ROOT / "web")
    run_step("前端 lint", [pnpm, "lint"], ROOT / "web")
    run_step("前端 build", [pnpm, "build"], ROOT / "web")
    # E2E（P5-3）：真浏览器 + **真后端**（它自己起一个隔离端口/隔离库的 uvicorn）。
    # 没装 Playwright 浏览器时它会报 `Executable doesn't exist` —— 按
    # `docs/e2e-browser.md` §5 跑一次 `playwright install chromium` 即可。
    # 失效方向：**阻断** —— 接缝断了（前端拿不到后端的字段）必须在发版前红。
    run_step("前端 E2E（真浏览器 + 真后端）", [pnpm, "exec", "playwright", "test", "--reporter=list"], ROOT / "web")

    if args.real_smoke:
        smoke_command = [
            sys.executable,
            str(ROOT / "scripts" / "real_codex_smoke.py"),
            "--codex-bin",
            args.codex_bin,
            "--timeout",
            str(args.timeout),
        ]
        if args.model:
            smoke_command.extend(["--model", args.model])
        if args.reasoning_effort:
            smoke_command.extend(["--reasoning-effort", args.reasoning_effort])
        if args.real_constitution:
            smoke_command.extend(["--constitution-runner", "real"])
        run_step("真实 Codex CLI 文件修改烟测", smoke_command, ROOT)

    if args.real_recursive_probe:
        probe_command = [
            sys.executable,
            str(ROOT / "scripts" / "real_recursive_probe.py"),
            "--codex-bin",
            args.codex_bin,
            "--timeout",
            str(args.timeout),
        ]
        if args.model:
            probe_command.extend(["--model", args.model])
        if args.reasoning_effort:
            probe_command.extend(["--reasoning-effort", args.reasoning_effort])
        if args.real_constitution:
            probe_command.extend(["--constitution-runner", "real"])
        run_step("真实 Codex CLI 递归链路探针", probe_command, ROOT)

    print("\n全部请求的验证已通过。")


if __name__ == "__main__":
    main()
