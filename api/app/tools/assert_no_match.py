from __future__ import annotations

import argparse
import fnmatch
import json
import re
from pathlib import Path


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".godot",
    ".godot_appdata",
    ".godot_user",
    ".codex_pycache",
    "__pycache__",
    "node_modules",
}

MOJIBAKE_MARKERS = (
    "鐨",
    "鐖",
    "鑺",
    "勪",
    "涓",
    "鍙",
    "浠",
    "绋",
    "妯",
    "鏍",
    "闂",
    "韬",
    "楠",
    "閫",
    "瘉",
    "跺",
    "眰",
    "骞",
    "涔",
    "鍔",
    "锛",
    "銆",
    "乻",
    "乵",
    "乸",
    "乁",
    "Ã",
    "Â",
    "ä",
    "æ",
    "�",
)
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")


def _iter_files(root: Path, include: list[str], exclude_dirs: set[str]):
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if include and not any(fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern) for pattern in include):
            continue
        yield path


def _looks_like_mojibake(line: str) -> bool:
    if not line:
        return False
    if PRIVATE_USE_RE.search(line):
        return True
    score = sum(line.count(marker) for marker in MOJIBAKE_MARKERS)
    if score >= 3:
        return True
    # Catch common UTF-8-as-Latin-1/CP1252 fragments without rejecting normal
    # Chinese, English, or code identifiers.
    latin_fragments = sum(line.count(marker) for marker in ("ä", "æ", "å", "ç", "è", "é", "Ã", "Â"))
    return latin_fragments >= 4


def main() -> int:
    parser = argparse.ArgumentParser(description="Assert that UTF-8 text files do not contain literal patterns.")
    parser.add_argument("root", nargs="?", default=".", help="File or directory to scan.")
    parser.add_argument("--pattern", action="append", default=[], help="Literal pattern that must be absent.")
    parser.add_argument("--fail-on-mojibake", action="store_true", help="Fail on common readable-text mojibake markers.")
    parser.add_argument("--include", action="append", default=[], help="Glob to include, repeatable, e.g. *.md.")
    parser.add_argument("--exclude-dir", action="append", default=[], help="Directory name to skip, repeatable.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    exclude_dirs = DEFAULT_EXCLUDED_DIRS | set(args.exclude_dir)
    matches: list[dict[str, object]] = []
    mojibake_matches: list[dict[str, object]] = []
    files_scanned = 0
    unreadable: list[str] = []

    if not root.exists():
        print(json.dumps({"ok": False, "error": "root_missing", "root": str(root)}, ensure_ascii=True))
        return 2

    for path in _iter_files(root, args.include, exclude_dirs):
        files_scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            unreadable.append(f"{path}: utf8_decode_error:{exc.start}")
            continue
        except OSError as exc:
            unreadable.append(f"{path}: os_error:{exc}")
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern in args.pattern:
                if pattern in line:
                    matches.append({"path": str(path), "line": line_number, "pattern": pattern})
                    break
            if args.fail_on_mojibake and _looks_like_mojibake(line):
                mojibake_matches.append({"path": str(path), "line": line_number, "preview": line[:160]})

    ok = not matches and not mojibake_matches and not unreadable
    print(json.dumps({
        "ok": ok,
        "root": str(root),
        "files_scanned": files_scanned,
        "patterns": args.pattern,
        "matches": matches[:50],
        "mojibake_matches": mojibake_matches[:50],
        "unreadable": unreadable[:50],
    }, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
