"""Godot 校验用的纯函数与词表。

搬移自 `orchestrator.Orchestrator`。这一簇只做一件事：判断集成目标 / 根成品目录是否
真的可用——入口、主场景、res:// 引用闭合、资产文件不是空壳、文本没有乱码。

**为什么门禁本体不在这里**：`_parent_godot_integration_gate(db, task)` 与
`_final_root_product_gate(db, task)` 内部要经 `_integration_target_path(db, task)` 访问数据库，
无法做成纯函数。因此搬来的是它们**全部的计算部分**，门禁本体留在 orchestrator 里作为薄壳，
负责解析出 `target` 后调用本模块。这是本次唯一的结构性动作，不含逻辑改写。

依赖方向：本模块只依赖 `godot_files`（路径/引用解析）与 `utils`（乱码判定），
**不得**导入 `orchestrator`，否则形成循环导入。

隐性契约（沿用原实现）：`requires_parent_godot_integration_gate` 接收**已小写化**的文本。
"""

from __future__ import annotations

from pathlib import Path

from .godot_files import godot_res_references, godot_resource_path
from .utils import has_irreversible_question_mark_corruption, has_mojibake_corruption

TEXT_DELIVERABLE_SUFFIXES = {
    ".gd",
    ".tscn",
    ".tres",
    ".godot",
    ".json",
    ".md",
    ".txt",
    ".cfg",
    ".ini",
    ".yaml",
    ".yml",
    ".csv",
}

IMAGE_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_ASSET_SUFFIXES = {".wav", ".ogg", ".mp3"}
ASSET_IGNORED_DIRS = {".git", ".godot", ".import", "node_modules", ".venv", "venv", "__pycache__", ".codex_pycache"}


def requires_parent_godot_integration_gate(gate_text: str) -> bool:
    godot_markers = ("godot", "project.godot", "res://")
    explicit_markers = (
        "集成验收",
        "最终集成",
        "父级集成",
        "集成项目",
        "resource reference closure",
        "resource reference check",
        "integration resource gate",
    )
    integration_context_markers = ("集成", "父级", "最终", "验收", "integration")
    has_godot = any(marker in gate_text for marker in godot_markers)
    has_explicit_gate = any(marker in gate_text for marker in explicit_markers)
    has_scoped_broken_link_check = "断链" in gate_text and any(
        marker in gate_text for marker in integration_context_markers
    )
    return has_godot and (has_explicit_gate or has_scoped_broken_link_check)


def missing_contains_text_integrity_issue(missing: list[str]) -> bool:
    return any("不可读文本乱码" in item or "text corruption" in item or "mojibake" in item for item in missing)


def missing_only_text_integrity_issues(missing: list[str]) -> bool:
    return bool(missing) and all(
        "不可读文本乱码" in item or "text corruption" in item or "mojibake" in item for item in missing
    )


def asset_header_is_valid(suffix: str, head: bytes) -> bool:
    if suffix == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return head.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if suffix == ".wav":
        return len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if suffix == ".ogg":
        return head.startswith(b"OggS")
    if suffix == ".mp3":
        return head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    return True


def asset_file_is_valid(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in IMAGE_ASSET_SUFFIXES and suffix not in AUDIO_ASSET_SUFFIXES:
        return True
    try:
        head = path.read_bytes()[:16]
        size = path.stat().st_size
    except OSError:
        return False
    return size > 0 and asset_header_is_valid(suffix, head)


def valid_asset_files(root: Path, suffixes: set[str]) -> list[Path]:
    assets: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part.lower() in ASSET_IGNORED_DIRS for part in rel.parts):
            continue
        if asset_file_is_valid(path):
            assets.append(path)
    return sorted(assets)


def valid_referenced_asset_files(root: Path, suffixes: set[str]) -> list[Path]:
    assets: dict[str, Path] = {}
    for ref in sorted(godot_res_references(root)):
        path = godot_resource_path(root, ref)
        if not path or not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if asset_file_is_valid(path):
            assets[str(path.resolve()).lower()] = path
    return [assets[key] for key in sorted(assets)]


def invalid_referenced_asset_files(root: Path) -> list[str]:
    invalid: list[str] = []
    for ref in sorted(godot_res_references(root)):
        path = godot_resource_path(root, ref)
        if not path or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".mp3"}:
            continue
        if asset_file_is_valid(path):
            continue
        invalid.append(ref)
    return invalid


def missing_godot_res_references(root: Path) -> list[str]:
    refs = godot_res_references(root)
    missing: list[str] = []
    for ref in sorted(refs):
        resource_path = godot_resource_path(root, ref)
        if resource_path is None:
            missing.append(ref)
            continue
        if resource_path.suffix and not resource_path.is_file():
            missing.append(ref)
    return missing


def readme_completion_issues(root: Path) -> list[str]:
    issues: list[str] = []
    stale_markers = (
        "项目骨架",
        "主场景骨架",
        "当前只包含",
        "后续子系统接入",
        "后续接入",
        "将由后续",
        "placeholder",
        "skeleton",
        "future subsystem",
        "not implemented",
        "todo",
    )
    for path in (root / "README.md", root / "README.txt"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        lowered = text.lower()
        matched = [marker for marker in stale_markers if marker in lowered or marker in text]
        if matched:
            issues.append(f"{rel}(stale incomplete-scope markers: {', '.join(matched[:4])})")
    return issues


def target_text_integrity_issues(root: Path) -> list[str]:
    issues: list[str] = []
    ignored_dirs = {".git", ".godot", "node_modules", ".venv", "venv", "__pycache__", ".codex_pycache"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_DELIVERABLE_SUFFIXES:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part.lower() in ignored_dirs for part in rel.parts):
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if has_irreversible_question_mark_corruption(text):
            issues.append(f"{rel.as_posix()}(irreversible question-mark text corruption)")
        elif has_mojibake_corruption(text):
            issues.append(f"{rel.as_posix()}(mojibake text corruption)")
    return issues


def target_text_index(root: Path) -> str:
    parts: list[str] = []
    suffixes = {".gd", ".tscn", ".tres", ".json", ".md", ".godot"}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            rel = path.relative_to(root).as_posix().lower()
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        parts.append(rel)
        parts.append(text[:20000])
    return "\n".join(parts)


def target_script_index(root: Path) -> str:
    parts: list[str] = []
    for path in root.rglob("*.gd"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix().lower()
            raw_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        code_lines: list[str] = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code_lines.append(line.split("#", 1)[0])
        text = "\n".join(code_lines).lower()
        parts.append(rel)
        parts.append(text[:20000])
    return "\n".join(parts)
