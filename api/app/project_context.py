"""给提示词用的**项目上下文摘要**：把任务相关目录扫成一段可读文本。

搬移自 `api/app/runner.py`（R-b 波，只搬移不改行为）。判据是
`scripts/audit_orchestrator_movable_mass.py` 的闭包实测：

    python scripts/audit_orchestrator_movable_mass.py api/app/runner.py \
      --closure _planner_prompt,_worker_prompt,_verifier_prompt,_constitution_prompt,_acceptance_prompt,_inherited_constraints_block,_default_constitution_data \
      --state

职责边界：本模块只做"读盘 → 摘要"——不构建提示词、不碰 settings、不拉起 Codex、不碰任务树。
提示词的拼装在上层的 `prompts.py`。

依赖方向：只依赖 `models` 与 `utils`，**不得**导入 `runner` / `prompts` / `orchestrator`。
"""

from __future__ import annotations

from pathlib import Path

from .models import TaskNode
from .utils import loads, repair_mojibake


def project_context_summary(task: TaskNode) -> str:
    roots = context_roots(task)
    if not roots:
        return "No existing project context path is available."
    sections = [summarize_context_root(label, path) for label, path in roots]
    return "\n\n".join(section for section in sections if section).strip() or "No readable project context."


def context_roots(task: TaskNode) -> list[tuple[str, Path]]:
    raw_roots: list[tuple[str, str]] = []
    worktree_root: Path | None = None
    target_root: Path | None = None
    if task.worktree_path:
        raw_roots.append(("WORKING_DIRECTORY", task.worktree_path))
        try:
            worktree_root = Path(task.worktree_path).resolve()
        except OSError:
            worktree_root = None
    constitution = loads(task.constitution_json, {})
    target_path = constitution.get("target_path") if isinstance(constitution, dict) else None
    if isinstance(target_path, str) and target_path:
        raw_roots.append(("CONSTITUTION_TARGET_PATH", target_path))
        try:
            target_root = Path(target_path).resolve()
        except OSError:
            target_root = None
    for item in loads(task.scope_json, []):
        if isinstance(item, str) and item:
            if looks_like_scope_path(item):
                path = Path(item)
                if not path.is_absolute() and worktree_root is not None:
                    raw_roots.append(("TASK_SCOPE_PATH", str(worktree_root / path)))
                elif not path.is_absolute() and target_root is not None:
                    raw_roots.append(("TASK_SCOPE_PATH", str(target_root / path)))
                else:
                    raw_roots.append(("TASK_SCOPE_PATH", item))

    roots: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, raw in raw_roots:
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        roots.append((label, path))
        if len(roots) >= 5:
            break
    return roots


def looks_like_scope_path(value: str) -> bool:
    stripped = value.strip()
    if not stripped or "\n" in stripped:
        return False
    if any(separator in stripped for separator in ("/", "\\")):
        return True
    return bool(Path(stripped).suffix)


def summarize_context_root(label: str, root: Path) -> str:
    lines = [f"{label}: {root}"]
    if not root.exists():
        lines.append("STATUS: missing")
        return "\n".join(lines)
    if not root.is_dir():
        lines.append("STATUS: file")
        try:
            lines.append(f"FILE_BYTES: {root.stat().st_size}")
        except OSError:
            pass
        lines.append(f"PARENT_DIR: {root.parent}")
        return "\n".join(lines)

    ignored_dirs = {
        ".git",
        ".godot",
        ".godot_user",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".codex_pycache",
        "dist",
        "build",
    }
    files: list[Path] = []
    dirs: set[str] = set()
    for path in root.rglob("*"):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in ignored_dirs for part in rel.parts):
            continue
        if path.is_dir():
            if len(rel.parts) == 1:
                dirs.add(rel.as_posix())
            continue
        files.append(rel)
        if len(files) >= 120:
            break
    lines.append("STATUS: directory")
    lines.append(f"TOP_LEVEL_DIRS: {', '.join(sorted(dirs)[:24]) or '(none)'}")
    lines.append("FILES_SAMPLE:")
    for rel in sorted(files)[:80]:
        lines.append(f"- {rel.as_posix()}")
    if len(files) > 80:
        lines.append(f"- ... ({len(files) - 80} more files not shown)")

    for rel in ("project.godot", "README.md", "README.txt"):
        path = root / rel
        if path.is_file():
            excerpt = safe_text_excerpt(path, 1200)
            if excerpt:
                lines.append(f"{rel.upper()}_EXCERPT:\n{excerpt}")
    return "\n".join(lines)


def safe_text_excerpt(path: Path, max_chars: int) -> str:
    try:
        if path.stat().st_size > 500_000:
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = str(repair_mojibake(text)).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n..."
    return text
