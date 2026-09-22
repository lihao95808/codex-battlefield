from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# 静态检查**覆盖的路径**。单独列成常量，是为了让"门禁范围"本身可被测试盯住
# （`api/tests/test_repo_gates.py`）——门禁少了一个目录不会报错，只会安静地变弱，
# 而"安静的变弱"正是这一带踩过的坑：`scripts/` 曾长期不在任何 lint 范围内。
LINT_PATHS = ("api", "scripts")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)


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
                    missing = sorted(properties - required)
                    extra = sorted(required - properties)
                    raise SystemExit(f"{path.name} {location} required 字段不完整：missing={missing} extra={extra}")
            for key, child in value.items():
                walk(child, f"{location}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{location}[{index}]")

    walk(schema, "$")


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
    # **`scripts/` 也必须在门禁里**：这行以前只从 `api/` 里跑 `ruff check .`，于是仓库根的
    # `scripts/*.py` 谁都没检查过。代价是一次真事件：`bench_run.py` 里 `Path(...)` 少了个
    # import（F821），只在 **real 档**才走到那一行（mock 档没有 worktree，短路了），
    # 结果两轮真实基准都在"判据求值前"崩掉、连报告一起带走，而门禁全程是绿的。
    # 失效方向：**阻断**——脚本静态检查不过就别发。
    run_step("仓库静态检查（ruff：api + scripts）", lint_command(), ROOT)
    run_step("后端 pytest", [sys.executable, "-m", "pytest"], ROOT / "api")

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("找不到 pnpm，无法运行前端验证。")
    run_step("前端测试", [pnpm, "test"], ROOT / "web")
    run_step("前端 lint", [pnpm, "lint"], ROOT / "web")
    run_step("前端 build", [pnpm, "build"], ROOT / "web")

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
