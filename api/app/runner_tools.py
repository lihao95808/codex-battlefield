"""Codex runner 落盘的辅助校验工具脚本，以及引用这些脚本的指导语。

搬移自 `api/app/runner.py`（R-a 波，只搬移不改行为）。判据是
`scripts/audit_orchestrator_movable_mass.py` 的闭包实测，不是方法组的表面行数：

    python scripts/audit_orchestrator_movable_mass.py api/app/runner.py \
      --closure _ensure_godot_timeout_wrapper,_ensure_assert_no_match_tool,_ensure_placeholder_asset_tool,_ensure_json_check_tool,_asset_placeholder_guidance,_negative_match_guidance,_json_check_guidance \
      --state

实测 **7 个方法 / 830 行**，不触达任何不可搬方法；除 `settings`（量具把它当作可以显式参数化的
配置通道）之外**零实例状态**，所以整体改成自由函数、把 `workspace_dir` 显式传进来即可，
`runner.py` 留同名薄委托，**零调用点改动**。

职责边界：本模块只做两件事——把工具脚本写进 `<workspace_dir>/tools/`，以及给提示词提供引用
这些脚本的指导语。它**不**拉起 Codex、不解析输出、不碰任务树。

依赖方向：本模块只依赖标准库，**不得**导入 `runner` 或 `orchestrator`。
"""

from __future__ import annotations

from pathlib import Path
import sys
import os
import shutil
from .config import Settings


TOOL_TEMPLATES_DIR = Path(__file__).resolve().parent / "tools"


def _write_tool_from_template(workspace_dir: Path, name: str) -> Path:
    """把仓库里的工具脚本复制到 `<workspace>/tools/<name>`（内容不同才写）。

    这些脚本以前是**字符串字面量**（最大的那个 642 行）。代价是实打实的：lint、类型检查、
    IDE 跳转全都看不到它们，重构也扫不到，改错只有"生成之后再编译"才发现（内层字符串还要再
    过一遍转义，`\\x89` 这类写法在字面量里和落盘后不是同一个文本）。现在它们是仓库里的真文件，
    "生成物 == 仓库文件"由 `tests/test_runner_tools.py` 逐字节断言，防两份源漂移。
    """
    template = TOOL_TEMPLATES_DIR / name
    if not template.is_file():
        raise FileNotFoundError(f"工具脚本模板缺失：{template}")
    target = workspace_dir / "tools" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    text = template.read_text(encoding="utf-8")
    if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != text:
        target.write_text(text, encoding="utf-8")
    return target


def ensure_godot_timeout_wrapper(workspace_dir: Path) -> Path:
    script_path = _write_tool_from_template(workspace_dir, "godot_with_timeout.py")
    if sys.platform.startswith("win"):
        wrapper_path = script_path.with_name("godot-with-timeout.cmd")
        command = f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n'
        if not wrapper_path.exists() or wrapper_path.read_text(encoding="utf-8", errors="replace") != command:
            wrapper_path.write_text(command, encoding="utf-8")
        return wrapper_path
    wrapper_path = script_path.with_name("godot-with-timeout")
    command = f'#!/usr/bin/env sh\nexec "{sys.executable}" "{script_path}" "$@"\n'
    if not wrapper_path.exists() or wrapper_path.read_text(encoding="utf-8", errors="replace") != command:
        wrapper_path.write_text(command, encoding="utf-8")
        try:
            wrapper_path.chmod(0o755)
        except OSError:
            pass
    return wrapper_path


def ensure_placeholder_asset_tool(workspace_dir: Path) -> Path:
    return _write_tool_from_template(workspace_dir, "generate_placeholder_asset.py")


def asset_placeholder_guidance(workspace_dir: Path) -> str:
    tool_path = ensure_placeholder_asset_tool(workspace_dir)
    return (
        f"Valid placeholder asset helper: python \"{tool_path}\" --output <relative-output.png-or.wav>. "
        "Use it when a task needs a small valid procedural placeholder image or sound. "
        "For unsupported referenced extensions such as .jpg, .webp, .ogg, or .mp3, prefer changing the project reference to a generated .png/.wav placeholder when that is within task scope; otherwise provide a real valid file for that extension.\n"
    )


def ensure_assert_no_match_tool(workspace_dir: Path) -> Path:
    return _write_tool_from_template(workspace_dir, "assert_no_match.py")


def negative_match_guidance(workspace_dir: Path) -> str:
    tool_path = ensure_assert_no_match_tool(workspace_dir)
    return (
        f"Readable text integrity helper: python \"{tool_path}\" --pattern \"???\" --pattern \"????\" --fail-on-mojibake "
        "--include \"*.md\" --include \"*.json\" --include \"*.gd\" --include \"*.tscn\" --include \"project.godot\" . "
        "Use this helper for absence checks such as question-mark corruption and common mojibake like `鐖跺眰`, `鑺傜偣`, `楠岃瘉`, or UTF-8-as-Latin-1 fragments; it exits 0 when no matches are found, avoiding false failed-command noise from `rg` exit code 1. "
        "Use direct `rg -n -F` only when a match is expected and should be printed as evidence.\n"
    )


def ensure_json_check_tool(workspace_dir: Path) -> Path:
    return _write_tool_from_template(workspace_dir, "json_check.py")


def json_check_guidance(workspace_dir: Path) -> str:
    tool_path = ensure_json_check_tool(workspace_dir)
    return (
        f"Compact JSON validation helper: python \"{tool_path}\" <file.json> [more.json]. "
        "Use this helper for ordinary JSON validity checks because it prints only a compact ASCII summary and avoids dumping large JSON bodies into the run log. "
        "Use `python -m json.tool file.json` only when you intentionally need to inspect the formatted JSON content, and keep that to small files.\n"
    )


def resolve_godot_executable(settings: Settings) -> str:
    configured = settings.godot_bin.strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        found = shutil.which(configured)
        if found:
            return found

    for candidate in ("godot", "godot4", "godot_console", "godot4_console"):
        found = shutil.which(candidate)
        if found:
            return found

    if sys.platform.startswith("win"):
        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        package_root = local_app_data / "Microsoft" / "WinGet" / "Packages"
        if package_root.exists():
            matches = sorted(package_root.glob("GodotEngine.GodotEngine_*/*_win64_console.exe"), reverse=True)
            if matches:
                return str(matches[0])
    return ""
