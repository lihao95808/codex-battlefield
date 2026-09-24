from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


EXPLICIT_RETRYABLE_MARKERS = (
    "资源/流量",
    "资源类阻塞",
    "流量类阻塞",
    "可等待后重试",
    "可重试的 codex 资源",
    "retryable resource",
    "wait_and_retry",
    "璧勬簮/娴侀噺",
    "鍙瓑寰呭悗閲嶈瘯",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair misclassified retryable Codex Battlefield block reports.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--retry-after-seconds", type=int, default=300)
    parser.add_argument("--limit", type=int, default=13)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_db() -> str:
    return utc_now().replace(tzinfo=None).isoformat(sep=" ")


def parse_db_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def explicit_retryable(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in EXPLICIT_RETRYABLE_MARKERS)


def load_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def subtree(rows: list[sqlite3.Row], root_id: str) -> list[sqlite3.Row]:
    by_id = {str(row["id"]): row for row in rows}
    children: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row["parent_id"]:
            children[str(row["parent_id"])].append(str(row["id"]))
    selected: list[sqlite3.Row] = []
    stack = [root_id]
    while stack:
        task_id = stack.pop()
        row = by_id.get(task_id)
        if row is None:
            continue
        selected.append(row)
        stack.extend(children.get(task_id, []))
    return selected


def retry_after_for(report: dict[str, Any], fallback: int) -> int:
    for key in ("retry_after_seconds", "quota_window_seconds"):
        try:
            value = int(float(report.get(key)))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return max(60, min(value, 604_800))
    return max(60, fallback)


def codex_health_retry_after(db_path: Path) -> tuple[int | None, str | None, bool]:
    health_path = db_path.parent / "codex_health.json"
    health = load_json(health_path.read_text(encoding="utf-8") if health_path.is_file() else None, {})
    if not isinstance(health, dict) or str(health.get("status") or "") != "resource_block":
        return None, None, False
    reset_at = parse_db_datetime(health.get("quota_reset_at"))
    if reset_at is not None:
        remaining = int((reset_at - utc_now()).total_seconds())
        if remaining > 0:
            return max(60, min(remaining, 604_800)), reset_at.isoformat(), False
        return 60, reset_at.isoformat(), True
    try:
        retry_after = int(float(health.get("quota_retry_after_seconds")))
    except (TypeError, ValueError):
        return None, None, False
    if retry_after > 0:
        return max(60, min(retry_after, 604_800)), None, False
    return None, None, False


def insert_event(connection: sqlite3.Connection, task_id: str | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
    connection.execute(
        "insert into event_records(task_id, type, message, payload_json, created_at) values (?, ?, ?, ?, ?)",
        (task_id, event_type, message, json.dumps(payload, ensure_ascii=False), utc_now_db()),
    )


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "select id, parent_id, status, title, error_message, node_meta_json, updated_at "
                "from task_nodes order by depth, updated_at"
            )
        )
        tasks = subtree(rows, args.root_id)
        repaired: list[str] = []
        requeued: list[str] = []
        adjusted_wait: list[str] = []
        now = utc_now()
        health_retry_after, health_reset_at, health_reset_elapsed = codex_health_retry_after(db_path)
        for task in tasks:
            if str(task["status"]) != "blocked":
                continue
            meta = load_json(task["node_meta_json"], {})
            if not isinstance(meta, dict):
                meta = {}
            report = meta.get("block_report") if isinstance(meta.get("block_report"), dict) else {}
            reason = str(report.get("reason") or task["error_message"] or "")
            if not explicit_retryable(reason):
                continue
            if not bool(report.get("retryable")) or report.get("category") != "rate_limit":
                report = {
                    **report,
                    "task_id": str(task["id"]),
                    "title": str(task["title"] or report.get("title") or ""),
                    "status": str(task["status"]),
                    "category": "rate_limit",
                    "retryable": True,
                    "parent_action": "wait_and_retry",
                    "reason": reason,
                    "reclassified_at": now.isoformat(),
                    "reclassified_by": "repair_retryable_block_reports.py",
                }
                meta["block_report"] = report
                repaired.append(str(task["id"]))
            if health_retry_after is not None and str(report.get("retry_after_source") or "") == "fallback_resource_window":
                adjusted_reported_at = now.isoformat()
                if health_reset_elapsed:
                    adjusted_reported_at = (now - timedelta(seconds=health_retry_after + 1)).isoformat()
                report = {
                    **report,
                    "retry_after_seconds": health_retry_after,
                    "quota_window_seconds": health_retry_after,
                    "retry_after_source": "codex_health_runtime_observation",
                    "codex_health_reset_at": health_reset_at,
                    "codex_health_reset_elapsed": health_reset_elapsed,
                    "reported_at": adjusted_reported_at,
                    "wait_adjusted_at": now.isoformat(),
                    "wait_adjusted_by": "repair_retryable_block_reports.py",
                }
                meta["block_report"] = report
                adjusted_wait.append(str(task["id"]))
            reported_at = parse_db_datetime(report.get("reported_at")) or parse_db_datetime(task["updated_at"]) or now
            retry_after = retry_after_for(report, args.retry_after_seconds)
            if (now - reported_at).total_seconds() < retry_after:
                if (str(task["id"]) in repaired or str(task["id"]) in adjusted_wait) and not args.dry_run:
                    connection.execute("update task_nodes set node_meta_json = ? where id = ?", (json.dumps(meta, ensure_ascii=False), task["id"]))
                continue
            if len(requeued) >= max(1, args.limit):
                if (str(task["id"]) in repaired or str(task["id"]) in adjusted_wait) and not args.dry_run:
                    connection.execute("update task_nodes set node_meta_json = ? where id = ?", (json.dumps(meta, ensure_ascii=False), task["id"]))
                continue
            retries = int(meta.get("auto_retryable_block_retries") or 0) + 1
            meta["auto_retryable_block_retries"] = retries
            meta["last_auto_retryable_block_retry_at"] = now.isoformat()
            meta["last_auto_retryable_block_retry_reason"] = "retryable resource/traffic block exceeded retry window"
            requeued.append(str(task["id"]))
            if not args.dry_run:
                connection.execute(
                    "update task_nodes set status = 'queued', error_message = null, node_meta_json = ?, updated_at = ? where id = ?",
                    (json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
                )
                insert_event(
                    connection,
                    str(task["id"]),
                    "task.auto_retryable_block_requeued",
                    "Retryable resource/traffic block exceeded its retry window; task requeued by watchdog.",
                    {"root_id": args.root_id, "retry_after_seconds": retry_after, "retries": retries},
                )
                if task["parent_id"]:
                    insert_event(
                        connection,
                        str(task["parent_id"]),
                        "parent.auto_retryable_child_requeued",
                        "A retryable blocked child was requeued by watchdog.",
                        {"child_id": str(task["id"]), "root_id": args.root_id, "retry_after_seconds": retry_after},
                    )
        if not args.dry_run:
            connection.commit()
        print(json.dumps({"repaired": repaired, "adjusted_wait": adjusted_wait, "requeued": requeued}, ensure_ascii=False))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
