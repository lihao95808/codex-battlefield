"""一键导出**诊断包**（P2-4）。

## 它解决什么

出问题时最常见的成本不是修，而是**来回问**："你那边 API 起了吗""看门狗注册了吗"
"配置里写了什么""日志最后几行是什么"。每一次都要用户去敲命令、复制粘贴，
而这个工具的状态散在六七个地方（状态根、配置文件、计划任务、Hub、日志）。

所以把"别人排障需要的东西"一次性收集成一个文件，**并且脱敏**。

## 脱敏是硬要求，不是礼貌

这个包里天然含有几类不该直接发出去的东西：

| 类别 | 例子 | 处理 |
|---|---|---|
| 凭据值 | `MY_GATEWAY_API_KEY = "..."`、`sk-...`、`ghp_...` | 值整体替换为 `***REDACTED***`，**只保留变量名** |
| 绝对路径 | `C:\\Users\\<真名>\\...` | 用户名与工程名保留（排障需要），但**不**动内容之外的东西 |
| 环境变量 | 宿主上各类 token | 只收集**白名单键名**，绝不 dump 整个环境 |

"只收集白名单键名"这一条是刻意的：`os.environ` 里可能有与该工具无关的
公司凭据，dump 出去就是事故。
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 只收集这些**键名**对应的环境变量值；其余一律不碰。
ENV_ALLOWLIST = (
    "CODEX_BATTLEFIELD_RUNNER",
    "CODEX_BATTLEFIELD_CODEX_HOME",
    "CODEX_BATTLEFIELD_CODEX_MODEL",
    "CODEX_BATTLEFIELD_CODEX_PROFILE",
    "CODEX_BATTLEFIELD_STATE_HOME",
    "CODEX_BATTLEFIELD_WORKSPACE",
    "CODEX_BATTLEFIELD_DATABASE_URL",
    "CODEX_BATTLEFIELD_STEWARD_READ_ONLY",
    "CODEX_BATTLEFIELD_STEWARD_PROACTIVE_ENABLED",
    "CODEX_BATTLEFIELD_SERVE_WEB",
)

# 形如 `NAME=value` / `NAME: value` / `"name": "value"` 的赋值，名字里带这些词就脱敏。
_SECRET_NAME = re.compile(
    r"(?i)(key|token|secret|password|passwd|credential|authorization|bearer|api[_-]?key)"
)
_ASSIGN = re.compile(
    r"""(?P<prefix>(?P<name>[A-Za-z_][A-Za-z0-9_\-]*)\s*[:=]\s*)"""
    r"""(?P<quote>["']?)(?P<value>[^\s"',}]{4,})(?P=quote)"""
)
# 常见密钥形态（即使没有 NAME= 前缀也要脱敏）
_SECRET_SHAPE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"
)


def redact(text: str) -> str:
    """把疑似凭据的值替换掉，**保留名字**（排障要看的是"有没有配"，不是值）。"""

    def _sub(match: re.Match[str]) -> str:
        name = match.group("name") or ""
        if _SECRET_NAME.search(name):
            return f"{match.group('prefix')}{match.group('quote')}***REDACTED***{match.group('quote')}"
        return match.group(0)

    text = _ASSIGN.sub(_sub, text)
    return _SECRET_SHAPE.sub("***REDACTED***", text)


def _tail(path: Path, *, lines: int = 200, max_bytes: int = 256 * 1024) -> str:
    """取文件尾部若干行；文件不存在返回空串（不是错误 —— 排障时"没有日志"本身是信息）。"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                handle.readline()  # 丢掉可能被截断的半行
            raw = handle.read()
    except OSError as exc:
        return f"（读取失败：{exc}）"
    text = raw.decode("utf-8", "replace")
    kept = text.splitlines()[-lines:]
    return redact("\n".join(kept))


def _codex_version(codex_bin: str) -> str:
    """codex CLI 版本。取不到就如实说取不到。"""
    try:
        result = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"（取不到：{exc.__class__.__name__}）"
    return (result.stdout or result.stderr or "").strip()[:200] or "（空输出）"


def collect(settings: Any, *, status_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """收集诊断信息。返回值可直接 `json.dumps`。

    **不做任何网络调用**（除了取 codex 版本）：诊断包本身不该因为
    "要诊断的那个东西不通"而失败。
    """
    from .config import machine_path_of

    layout = settings.layout
    state_home = Path(layout.state_home)

    env: dict[str, str] = {}
    for name in ENV_ALLOWLIST:
        value = os.environ.get(name, "")
        if value:
            env[name] = redact(value) if _SECRET_NAME.search(name) else value

    files: dict[str, str] = {}
    for name in ("watchdog.log", "codex_health.json", "machine_safety.json", "watchdog_status.json"):
        path = machine_path_of(settings, name)
        text = _tail(path, lines=120)
        if text:
            files[name] = text

    config_file = Path(os.path.expanduser("~")) / ".codex-battlefield.toml"
    config_text = ""
    if config_file.exists():
        try:
            config_text = redact(config_file.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            config_text = f"（读取失败：{exc}）"

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": {
            "name": "codex-battlefield",
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "codex_bin": getattr(settings, "codex_bin", ""),
            "codex_version": _codex_version(getattr(settings, "codex_bin", "codex") or "codex"),
        },
        "settings": {
            "runner_mode": settings.runner_mode,
            "frozen_mode": bool(settings.frozen_mode),
            "steward_read_only": bool(getattr(settings, "steward_read_only", False)),
            "max_concurrency": getattr(settings, "max_concurrency", None),
            "codex_model": getattr(settings, "codex_model", ""),
            "serve_web_dist": bool(getattr(settings, "serve_web_dist", False)),
        },
        "layout": layout.describe(),
        "env": env,
        "mounted_projects": _mounted_projects(state_home),
        "files": files,
        "user_config": config_text,
        "status_snapshot": status_snapshot or {},
    }


def _mounted_projects(state_home: Path) -> list[dict[str, Any]]:
    """已挂载工程（只取元信息，不读库内容）。"""
    try:
        from .project_store import current_project, list_projects

        active = current_project(state_home=state_home)
        active_key = active.key if active else ""
        return [
            {
                "key": p.key,
                "root": str(p.root),
                "active": p.key == active_key,
                "root_exists": Path(p.root).is_dir(),
                "db_bytes": p.db_size_bytes(),
            }
            for p in list_projects(state_home=state_home)
        ]
    except Exception as exc:  # noqa: BLE001 - 诊断收集失败不该让整包失败
        return [{"_error": f"{exc.__class__.__name__}: {exc}"}]


def render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def render_markdown(payload: dict[str, Any]) -> str:
    """人读版：出问题时先看这个，再决定要不要翻 JSON。"""
    lines: list[str] = []
    add = lines.append
    add("# 诊断包（codex-battlefield）")
    add("")
    add(f"生成时间：{payload['generated_at']}")
    tool = payload["tool"]
    add(f"Python {tool['python']} · {tool['platform']}")
    add(f"codex CLI：{tool['codex_bin']} → {tool['codex_version']}")
    add("")

    add("## 设置（进程实际使用的）")
    add("")
    add("| 项 | 值 |")
    add("|---|---|")
    for key, value in payload["settings"].items():
        add(f"| {key} | `{value}` |")
    add("")

    add("## 布局")
    add("")
    for key, value in payload["layout"].items():
        add(f"- {key}: `{value}`")
    add("")

    add("## 环境变量（仅白名单键名）")
    add("")
    if payload["env"]:
        for key, value in payload["env"].items():
            add(f"- {key} = `{value}`")
    else:
        add("（一个都没设）")
    add("")

    add("## 已挂载工程")
    add("")
    for item in payload["mounted_projects"]:
        if "_error" in item:
            add(f"- 收集失败：{item['_error']}")
            continue
        mark = " ← 当前" if item.get("active") else ""
        exists = "" if item.get("root_exists") else "  ⚠️ 目录不存在"
        add(f"- `{item['key']}` {item['root']}{mark}{exists}")
    add("")

    add("## 用户配置（`~/.codex-battlefield.toml`，已脱敏）")
    add("")
    add("```toml")
    add(payload["user_config"] or "（没有这个文件）")
    add("```")
    add("")

    for name, text in payload["files"].items():
        add(f"## {name}（尾部）")
        add("")
        add("```")
        add(text)
        add("```")
        add("")
    return "\n".join(lines)
