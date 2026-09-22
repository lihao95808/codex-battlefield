from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write one Codex Battlefield full checkpoint for a root.")
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--reason", default="watchdog.manual")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from app.config import get_settings
    from app.database import SessionLocal
    from app.orchestrator import Orchestrator

    orchestrator = Orchestrator(get_settings())
    with SessionLocal() as db:
        manifest = orchestrator._write_full_checkpoint(db, args.root_id, args.reason)
    payload = {
        "status": "written" if manifest else "skipped",
        "root_id": args.root_id,
        "reason": args.reason,
        "manifest": str(manifest) if manifest else None,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
