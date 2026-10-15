"""Godot 项目文件的读取与解析 —— 纯函数，无 Orchestrator 状态依赖。

搬移自 `orchestrator.Orchestrator` 上的同名方法。这些方法原先写成实例方法，但实际
不读取 `self` 的任何状态，只做路径解析与文本解析，因此适合独立成模块。

`Orchestrator` 保留同名的一行委托。这样调用点零改动，本次搬移的 diff 只包含
「移动 + import + 委托」，符合 W4 的"只搬移、不得夹带逻辑改写"要求。

注意：本模块**不得**导入 `orchestrator`，否则会形成循环导入。需要共享的底层工具应放在
`utils.py` 一类更低的层。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# res:// 引用的扫描模式。原实现在 `_godot_res_references` 内联，搬移时提为常量以便复用与测试。
RES_REFERENCE_RE = re.compile(r"res://[A-Za-z0-9_./@+\-%~:#?=&]+")


def resource_value(value: dict[str, Any], keys: tuple[str, ...]) -> str:
    cursor: Any = value
    for key in keys:
        if not isinstance(cursor, dict):
            return ""
        cursor = cursor.get(key)
    return str(cursor or "")


def godot_resource_path(root: Path, resource: str | None) -> Path | None:
    if not resource or not resource.startswith("res://"):
        return None
    resource_path = resource.removeprefix("res://").split("#", 1)[0].split("?", 1)[0].rstrip(".,;")
    rel = Path(resource_path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return root / rel


def resource_exists(root: Path, resource: str | None) -> bool:
    path = godot_resource_path(root, resource)
    return bool(path and path.is_file())


def resource_text(root: Path, resource: str | None) -> str:
    path = godot_resource_path(root, resource)
    if not path or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""


def godot_scene_node_count(scene_text: str) -> int:
    return sum(1 for line in scene_text.splitlines() if line.strip().startswith("[node "))


def godot_scene_has_script_reference(scene_text: str) -> bool:
    return 'type="script"' in scene_text or "script =" in scene_text or "script=" in scene_text


def godot_res_references(root: Path) -> set[str]:
    suffixes = {".gd", ".tscn", ".tres", ".json", ".godot"}
    refs: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if rel_parts and rel_parts[0].lower() == "docs":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs.update(match.rstrip(".,;`") for match in RES_REFERENCE_RE.findall(text))
    return refs


def extract_godot_main_scene(project_text: str) -> str | None:
    for line in project_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("run/main_scene") or "=" not in stripped:
            continue
        return stripped.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def godot_line_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith((";", "#")) or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def parse_godot_project_sections(content: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"": []}
    current = ""
    for raw_line in content.splitlines():
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def count_files(root: Path, pattern: str) -> int:
    try:
        return sum(1 for path in root.rglob(pattern) if path.is_file())
    except OSError:
        return 0
