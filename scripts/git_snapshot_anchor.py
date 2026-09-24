from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = ROOT_DIR / "workspace"
DEFAULT_USER_NAME = "Codex Battlefield Snapshotter"
DEFAULT_USER_EMAIL = "codex-battlefield-snapshots@example.local"
GODOT_GITIGNORE = """\
.godot/
.import/
.mono/
export.cfg
export_presets.cfg
*.translation
*.tmp
*.log
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(command: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed in {cwd}: {' '.join(command)}\n{detail}")
    return result


def git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args], ROOT_DIR, check=check)


def is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    result = git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def ensure_git_repo(path: Path, init: bool) -> bool:
    if is_git_repo(path):
        ensure_default_gitignore(path)
        ensure_git_identity(path)
        return False
    if not init:
        raise RuntimeError(f"{path} is not a git repository; pass --init-git to initialize it")
    path.mkdir(parents=True, exist_ok=True)
    ensure_default_gitignore(path)
    run(["git", "init"], path)
    ensure_git_identity(path)
    return True


def ensure_default_gitignore(path: Path) -> None:
    if (path / ".gitignore").exists():
        return
    if (path / "project.godot").is_file():
        (path / ".gitignore").write_text(GODOT_GITIGNORE, encoding="utf-8")


def ensure_git_identity(path: Path) -> None:
    name = git(path, ["config", "--get", "user.name"], check=False).stdout.strip()
    email = git(path, ["config", "--get", "user.email"], check=False).stdout.strip()
    if not name:
        git(path, ["config", "user.name", DEFAULT_USER_NAME])
    if not email:
        git(path, ["config", "user.email", DEFAULT_USER_EMAIL])


def current_commit(path: Path) -> str | None:
    result = git(path, ["rev-parse", "--verify", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def status_porcelain(path: Path) -> str:
    return git(path, ["status", "--porcelain"], check=True).stdout


def commit_all(path: Path, message: str) -> str | None:
    if not status_porcelain(path).strip():
        return current_commit(path)
    git(path, ["add", "-A"])
    if not status_porcelain(path).strip():
        return current_commit(path)
    commit = git(path, ["commit", "-m", message], check=False)
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "").strip().lower()
        if "nothing to commit" in detail or "working tree clean" in detail:
            return current_commit(path)
        raise RuntimeError(f"command failed in {path}: git commit -m {message}\n{commit.stderr or commit.stdout}")
    return current_commit(path)


def tag_commit(path: Path, tag_name: str, message: str) -> None:
    existing = git(path, ["rev-parse", "--verify", f"refs/tags/{tag_name}"], check=False)
    if existing.returncode == 0:
        return
    git(path, ["tag", "-a", tag_name, "-m", message])


def tag_exists(path: Path, tag_name: str | None) -> bool:
    if not tag_name:
        return False
    existing = git(path, ["rev-parse", "--verify", f"refs/tags/{tag_name}"], check=False)
    return existing.returncode == 0


def push(path: Path, remote: str, tag_name: str | None) -> None:
    git(path, ["push", remote, "HEAD"])
    if tag_name:
        git(path, ["push", remote, tag_name])


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def latest_manifest_for_root(root_id: str) -> Path:
    root = WORKSPACE_DIR / "full_checkpoints" / root_id
    if not root.is_dir():
        raise RuntimeError(f"no full checkpoint directory found for root {root_id}")
    manifests = sorted(root.glob("*/manifest.json"), key=lambda item: item.parent.name, reverse=True)
    if not manifests:
        raise RuntimeError(f"no full checkpoint manifest found for root {root_id}")
    return manifests[0]


def resolve_manifest(args: argparse.Namespace) -> Path:
    if args.manifest:
        manifest = Path(args.manifest).resolve()
        if manifest.name == "latest.json":
            payload = read_json(manifest)
            root_id = str(payload.get("root_id") or args.root_id or "")
            if not root_id:
                raise RuntimeError("latest.json does not contain a root_id")
            return latest_manifest_for_root(root_id)
        return manifest
    if not args.root_id:
        raise RuntimeError("pass --root-id or --manifest")
    return latest_manifest_for_root(args.root_id)


def safe_tag_part(value: str) -> str:
    cleaned = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "checkpoint"


def write_ledger(root_id: str, checkpoint_id: str, payload: dict[str, Any]) -> Path:
    ledger_dir = WORKSPACE_DIR / "checkpoint_ledger" / root_id
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{checkpoint_id}.json"
    ledger_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    latest_path = ledger_dir / "latest.json"
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return ledger_path


def build_restore_payload(
    *,
    root_id: str,
    checkpoint_id: str,
    database_copy: Path | None,
    target_path: Path | None,
    target_copy: Path | None,
    target_ref: str | None,
    battlefield_ref: str | None,
) -> dict[str, Any]:
    return {
        "restore_version": 1,
        "summary": (
            "Stop Battlefield/API schedulers first. Restore the database copy to workspace/battlefield.sqlite3 "
            "and restore the target project from either its git ref or the full target copy."
        ),
        "root_id": root_id,
        "checkpoint_id": checkpoint_id,
        "battlefield_restore_ref": battlefield_ref,
        "target_restore_ref": target_ref,
        "database_copy": str(database_copy) if database_copy else None,
        "target_path": str(target_path) if target_path else None,
        "target_copy": str(target_copy) if target_copy else None,
        "commands": {
            "battlefield_checkout": f"git -C {ROOT_DIR} checkout {battlefield_ref}" if battlefield_ref else "",
            "target_checkout": f"git -C {target_path} checkout {target_ref}" if target_path and target_ref else "",
            "database_restore": (
                f"copy {database_copy} {WORKSPACE_DIR / 'battlefield.sqlite3'}"
                if database_copy
                else ""
            ),
            "target_copy_restore": f"robocopy {target_copy} {target_path} /MIR" if target_copy and target_path else "",
        },
    }


def build_ledger_payload(
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    root_id: str,
    checkpoint_id: str,
    db_copy: Path | None,
    target_path: Path | None,
    target_before: str | None,
    target_after: str | None,
    target_tag: str | None,
    target_git_ready: bool,
    battlefield_before: str | None,
    battlefield_tag: str | None,
    push_requested: bool,
    remote: str | None,
) -> dict[str, Any]:
    target_copy_raw = str(manifest.get("target_copy") or "").strip()
    target_copy = Path(target_copy_raw).resolve() if target_copy_raw else None
    target_ref = target_tag or target_after or target_before
    battlefield_ref = battlefield_tag or battlefield_before
    return {
        "ledger_version": 1,
        "anchor_schema_version": 2,
        "created_at": utc_now(),
        "root_id": root_id,
        "checkpoint_id": checkpoint_id,
        "version_id": f"{root_id}-{checkpoint_id}",
        "checkpoint_reason": manifest.get("reason"),
        "checkpoint_created_at": manifest.get("created_at"),
        "checkpoint_manifest": str(manifest_path),
        "checkpoint_manifest_sha256": sha256_file(manifest_path),
        "database_copy": str(db_copy) if db_copy else None,
        "database_copy_sha256": sha256_file(db_copy) if db_copy else None,
        "state_snapshot": manifest.get("state_snapshot"),
        "target_source": str(target_path) if target_path else None,
        "target_path": str(target_path) if target_path else None,
        "target_copy": str(target_copy) if target_copy else None,
        "target_copied": bool(manifest.get("target_copied")),
        "target_git_ready": target_git_ready,
        "target_git_commit_before": target_before,
        "target_git_commit_after": target_after,
        "target_git_tag": target_tag,
        "target_restore_ref": target_ref,
        "battlefield_git_commit_before": battlefield_before,
        "battlefield_git_tag": battlefield_tag,
        "battlefield_restore_ref": battlefield_ref,
        "push_requested": push_requested,
        "remote": remote if push_requested else None,
        "restore": build_restore_payload(
            root_id=root_id,
            checkpoint_id=checkpoint_id,
            database_copy=db_copy,
            target_path=target_path,
            target_copy=target_copy,
            target_ref=target_ref,
            battlefield_ref=battlefield_ref,
        ),
        "publish_hint": "Commit and publish checkpoint_ledger with GitHub Desktop; full_checkpoints remain local recovery artifacts.",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record a full checkpoint as git commits/tags and a lightweight ledger.")
    parser.add_argument("--root-id", help="Battlefield root task id, for example root-a50ce4a514.")
    parser.add_argument("--manifest", help="Full checkpoint manifest path. If omitted, uses latest manifest for --root-id.")
    parser.add_argument("--target-path", help="Override target Godot project path from the manifest.")
    parser.add_argument("--init-git", action="store_true", help="Initialize git repositories when missing.")
    parser.add_argument("--commit", action="store_true", help="Commit Battlefield ledger and target project changes.")
    parser.add_argument("--tag", action="store_true", help="Create annotated tags for the commits.")
    parser.add_argument("--push", action="store_true", help="Push HEAD and tags to the configured remote.")
    parser.add_argument("--remote", default="origin", help="Git remote used by --push. Defaults to origin.")
    return parser.parse_args()


def anchor_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = resolve_manifest(args)
    manifest = read_json(manifest_path)
    root_id = str(manifest.get("root_id") or args.root_id or "")
    if not root_id:
        raise RuntimeError("checkpoint manifest is missing root_id")
    checkpoint_id = manifest_path.parent.name
    tag_base = f"battlefield-{safe_tag_part(root_id)}-{safe_tag_part(checkpoint_id)}"
    db_copy_raw = str(manifest.get("database_copy") or "").strip()
    db_copy = Path(db_copy_raw).resolve() if db_copy_raw else None
    target_raw = str(args.target_path or manifest.get("target_source") or "").strip()
    target_path = Path(target_raw).resolve() if target_raw else None
    battlefield_tag = f"{tag_base}-battlefield" if args.tag else None

    ensure_git_repo(ROOT_DIR, init=args.init_git)
    battlefield_before = current_commit(ROOT_DIR)
    existing_ledger_path = WORKSPACE_DIR / "checkpoint_ledger" / root_id / f"{checkpoint_id}.json"
    if tag_exists(ROOT_DIR, battlefield_tag) and existing_ledger_path.is_file():
        existing_payload = read_json(existing_ledger_path)
        return {
            **existing_payload,
            "already_anchored": True,
            "ledger_path": str(existing_ledger_path),
            "battlefield_git_commit_after": current_commit(ROOT_DIR),
            "battlefield_git_tag": str(existing_payload.get("battlefield_git_tag") or battlefield_tag),
        }

    target_before = None
    target_after = None
    target_tag = None
    target_git_ready = bool(target_path and target_path.exists())
    if target_git_ready:
        ensure_git_repo(target_path, init=args.init_git)
        target_before = current_commit(target_path)
        if args.commit:
            target_after = commit_all(target_path, f"Checkpoint {root_id} {checkpoint_id}")
        else:
            target_after = target_before
        if args.tag and target_after:
            target_tag = f"{tag_base}-target"
            tag_commit(target_path, target_tag, f"Codex Battlefield target checkpoint {root_id} {checkpoint_id}")
        if args.push:
            push(target_path, args.remote, target_tag)

    ledger_payload = build_ledger_payload(
        manifest=manifest,
        manifest_path=manifest_path,
        root_id=root_id,
        checkpoint_id=checkpoint_id,
        db_copy=db_copy,
        target_path=target_path,
        target_before=target_before,
        target_after=target_after,
        target_tag=target_tag,
        target_git_ready=target_git_ready,
        battlefield_before=battlefield_before,
        battlefield_tag=battlefield_tag,
        push_requested=bool(args.push),
        remote=args.remote,
    )
    ledger_path = write_ledger(root_id, checkpoint_id, ledger_payload)

    battlefield_after = battlefield_before
    if args.commit:
        battlefield_after = commit_all(ROOT_DIR, f"Record checkpoint {root_id} {checkpoint_id}")
    if args.tag and battlefield_after:
        tag_commit(ROOT_DIR, battlefield_tag, f"Codex Battlefield checkpoint ledger {root_id} {checkpoint_id}")
    if args.push:
        push(ROOT_DIR, args.remote, battlefield_tag)

    return {
        **ledger_payload,
        "ledger_path": str(ledger_path),
        "battlefield_git_commit_after": battlefield_after,
        "battlefield_git_tag": battlefield_tag,
    }


def main() -> int:
    args = parse_args()
    result = anchor_checkpoint(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        # `from None`：上面已经把原因打到 stderr 了，再挂一条 exception chain 只会让输出变成两份
        raise SystemExit(1) from None
