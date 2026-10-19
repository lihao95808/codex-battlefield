"""Read-only, non-recursive directory listing within the configured project root."""
from __future__ import annotations

import heapq
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat


MAX_ENTRIES = 300
SKIPPED_DIRECTORIES = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".godot",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt",
    "dist", "build", "coverage", "htmlcov", ".cache",
})


def _is_link(metadata: os.stat_result) -> bool:
    # Junctions and other Windows reparse points are not necessarily symlinks.
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or "\x00" in relative:
        raise ValueError("Invalid project-relative path")
    normalized = relative.replace("\\", "/")
    windows = PureWindowsPath(relative)
    if windows.drive or windows.root or PurePosixPath(normalized).is_absolute():
        raise ValueError("Absolute paths and drive paths are not allowed")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if any(part == ".." or ":" in part for part in parts):
        raise ValueError("Parent traversal and drive paths are not allowed")
    # Windows strips trailing spaces/dots; reject ambiguous aliases on every OS.
    if any(part.endswith((" ", ".")) for part in parts):
        raise ValueError("Ambiguous path components are not allowed")
    return parts


def list_directory(root: Path | None, relative: str = "") -> dict:
    """List at most 300 direct children; root must come from project configuration.

    Invalid/unsafe paths raise ValueError. Missing paths and non-directories raise
    FileNotFoundError. No file contents or descendant directories are read.
    """
    parts = _relative_parts(relative)
    path = "/".join(parts)
    if root is None:
        return {"root": None, "path": path, "entries": [], "truncated": False, "status": "unbound"}

    root = Path(root)
    if _is_link(root.lstat()):
        raise ValueError("Linked project roots are not allowed")
    base = root.resolve(strict=True)
    target = base
    for part in parts:
        if part.casefold() in SKIPPED_DIRECTORIES:
            raise ValueError("Generated and excluded directories are not browsable")
        target = target / part
        try:
            metadata = target.lstat()
        except NotADirectoryError as exc:
            raise FileNotFoundError("Project directory not found") from exc
        if _is_link(metadata):
            raise ValueError("Linked paths are not allowed")
        if not stat.S_ISDIR(metadata.st_mode):
            raise FileNotFoundError("Project directory not found")
    target = target.resolve(strict=True)
    if not target.is_relative_to(base):
        raise ValueError("Path escapes the project root")
    if not target.is_dir():
        raise FileNotFoundError("Project directory not found")

    count = 0

    def children():
        nonlocal count
        with os.scandir(target) as iterator:
            for child in iterator:
                try:
                    metadata = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A child may disappear while the directory is listed.
                if _is_link(metadata):
                    continue
                is_directory = stat.S_ISDIR(metadata.st_mode)
                if is_directory and child.name.casefold() in SKIPPED_DIRECTORIES:
                    continue
                if not is_directory and not stat.S_ISREG(metadata.st_mode):
                    continue
                # Defense in depth against links introduced during enumeration.
                if not Path(child.path).resolve().is_relative_to(base):
                    continue
                count += 1
                yield {
                    "name": child.name,
                    "path": f"{path}/{child.name}" if path else child.name,
                    "kind": "directory" if is_directory else "file",
                }

    entries = heapq.nsmallest(
        MAX_ENTRIES, children(),
        key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold(), entry["name"]),
    )
    return {"root": str(base), "path": path, "entries": entries, "truncated": count > MAX_ENTRIES, "status": "ok"}
