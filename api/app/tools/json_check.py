from __future__ import annotations

import argparse
import json
from pathlib import Path


def _summarize(value):
    if isinstance(value, dict):
        keys = list(value.keys())
        return {"type": "object", "key_count": len(keys), "keys": keys[:40]}
    if isinstance(value, list):
        return {"type": "array", "length": len(value)}
    return {"type": type(value).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate JSON files without dumping their contents.")
    parser.add_argument("files", nargs="+", help="JSON files to validate.")
    args = parser.parse_args()

    results = []
    ok = True
    for raw in args.files:
        path = Path(raw)
        item = {"file": str(path)}
        if not path.exists():
            item.update({"ok": False, "error": "missing"})
            ok = False
            results.append(item)
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except UnicodeDecodeError as exc:
            item.update({"ok": False, "error": "utf8_decode_error", "offset": exc.start})
            ok = False
        except json.JSONDecodeError as exc:
            item.update({"ok": False, "error": "json_decode_error", "line": exc.lineno, "column": exc.colno, "message": exc.msg})
            ok = False
        except OSError as exc:
            item.update({"ok": False, "error": "os_error", "message": str(exc)})
            ok = False
        else:
            item.update({"ok": True, "bytes": path.stat().st_size, "summary": _summarize(data)})
        results.append(item)

    print(json.dumps({"ok": ok, "files": results}, ensure_ascii=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
