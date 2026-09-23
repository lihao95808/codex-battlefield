from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BAD_DEPENDENCY_STATUSES = {"blocked", "failed", "canceled"}
REPAIRABLE_TASK_STATUSES = {"planned", "queued", "blocked"}
RETRYABLE_CATEGORIES = {"capacity", "timeout", "rate_limit", "transient", "protocol", "workspace_sandbox"}
OPTIONAL_SKILL_LOOKUP_MARKERS = (
    "skill_notes",
    "skill note",
    "skill/km",
    "skill km",
    "skill_km_url",
    "技能笔记",
    "技能沉淀",
    "自救",
    "授权",
    "权限",
)
CATEGORY_PRIORITY = {
    "tool_internal": 0,
    "validation_environment": 1,
    "human_auth": 2,
    "visual_uncertain": 3,
    "external": 4,
    "manual_review": 5,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair blocked-child replacement dependencies and inherited block categories.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--root-id", required=True)
    parser.add_argument("--limit", type=int, default=13)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def utc_now_db() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def load_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def subtree(rows: list[sqlite3.Row], root_id: str) -> list[sqlite3.Row]:
    by_id = {str(row["id"]): row for row in rows}
    children: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        parent_id = row["parent_id"]
        if parent_id:
            children[str(parent_id)].append(str(row["id"]))
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


def parent_children(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    children: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        parent_id = row["parent_id"]
        if parent_id:
            children[str(parent_id)].append(row)
    return children


def is_blocked_child_repair(meta: dict[str, Any], title: str) -> bool:
    coverage_ids = meta.get("coverage_ids")
    if isinstance(coverage_ids, list) and any(str(item).strip().lower() == "blocked-child-repair" for item in coverage_ids):
        return True
    verification_kind = str(meta.get("verification_kind") or "")
    marker_text = f"{title} {verification_kind}".lower()
    return "blocked child repair" in marker_text or "blocked-child-repair" in marker_text


def is_optional_skill_lookup_dependency(row: sqlite3.Row) -> bool:
    if str(row["status"]) != "canceled":
        return False
    text = normalize(f"{row['title'] or ''} {row['error_message'] or ''}")
    return any(marker in text for marker in OPTIONAL_SKILL_LOOKUP_MARKERS)


def drop_dependency_references(meta: dict[str, Any], dependencies: list[sqlite3.Row]) -> None:
    dependency_ids = {str(dependency["id"]) for dependency in dependencies}
    dependency_titles = {normalize(str(dependency["title"] or "")) for dependency in dependencies}
    depends_on_ids = [str(item) for item in meta.get("depends_on_ids", []) if str(item)]
    depends_on = [str(item) for item in meta.get("depends_on", []) if str(item)]
    meta["depends_on_ids"] = [dependency_id for dependency_id in depends_on_ids if dependency_id not in dependency_ids]
    meta["depends_on"] = [
        dependency
        for dependency in depends_on
        if dependency not in dependency_ids and normalize(dependency) not in dependency_titles
    ]
    skipped = meta.get("optional_dependencies_skipped")
    if not isinstance(skipped, list):
        skipped = []
    skipped.extend(
        {
            "task_id": str(dependency["id"]),
            "title": str(dependency["title"] or ""),
            "status": str(dependency["status"]),
            "skipped_at": datetime.now(timezone.utc).isoformat(),
            "reason": "optional_skill_lookup_dependency_canceled",
        }
        for dependency in dependencies
    )
    meta["optional_dependencies_skipped"] = skipped[-20:]


def parent_action_for(category: str) -> str:
    if category in RETRYABLE_CATEGORIES:
        return "wait_and_retry"
    return {
        "tool_internal": "pause_tool_fix",
        "validation_environment": "recursive_recovery_static_wrapper",
        "human_auth": "human_auth_required",
        "visual_uncertain": "human_visual_review",
    }.get(category, "manual_review")


def category_from_action(parent_action: str) -> str:
    return {
        "wait_and_retry": "rate_limit",
        "pause_tool_fix": "tool_internal",
        "recursive_recovery_static_wrapper": "validation_environment",
        "human_auth_required": "human_auth",
        "human_visual_review": "visual_uncertain",
        "manual_review": "external",
    }.get(parent_action, "")


def category_from_reason(reason: str) -> str:
    lowered = reason.lower()
    if is_workspace_sandbox_reason(lowered):
        return "workspace_sandbox"
    if any(
        marker in lowered
        for marker in (
            "current available tools",
            "available tools",
            "no command execution capability",
            "does not provide command execution",
            "response format requires",
            "codex_output_schema",
            "没有提供命令执行能力",
            "无法执行验证命令",
            "无法执行必要的独立验证命令",
            "拒绝运行必需",
            "工具调用只允许读权限",
            "不能独立核验证据",
        )
    ):
        return "protocol"
    if any(marker in lowered for marker in ("usage: codex exec", "unknown option", "unrecognized option", "failed to load skill", "tool internal", "transport channel closed", "unexpectedcontenttype")):
        return "tool_internal"
    if any(marker in lowered for marker in ("access denied", "godot_timeout", "could not create directory: 'user://logs'", "program crashed with signal")):
        return "validation_environment"
    if any(marker in lowered for marker in ("rate limit", "quota", "429", "too many requests", "retry after")):
        return "rate_limit"
    if any(marker in lowered for marker in ("sso", "login required", "authentication required", "authorization required", "unauthorized", "forbidden")):
        return "human_auth"
    if "human visual review" in lowered or "visual_uncertain" in lowered:
        return "visual_uncertain"
    return "external"


def is_workspace_sandbox_reason(lowered: str) -> bool:
    readonly_markers = ("read-only", "read only", "只读", "restricted read")
    write_denied_markers = (
        "filesystem sandbox",
        "read-only sandbox",
        "文件系统权限",
        "文件系统沙箱",
        "文件系统为只读",
        "文件系统是只读",
        "工作区沙箱为只读",
        "只读沙箱",
        "apply_patch 被沙箱拒绝",
        "apply_patch 写入被拒绝",
        "写入补丁被拒绝",
        "写入被拒绝",
        "被沙箱拒绝",
        "无法创建",
        "无法写入",
        "拒绝",
    )
    return any(marker in lowered for marker in readonly_markers) and any(marker in lowered for marker in write_denied_markers)


def contexts_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    contexts: list[dict[str, Any]] = []
    category = str(report.get("category") or "").strip()
    parent_action = str(report.get("parent_action") or "").strip()
    reason_category = category_from_reason(str(report.get("reason") or ""))
    reason_override_categories = {"tool_internal", "validation_environment", "rate_limit", "workspace_sandbox", "protocol"}
    if not category and reason_category in reason_override_categories:
        category = reason_category
    if not category and parent_action:
        category = category_from_action(parent_action)
    if not category:
        category = reason_category
    if category in {"external", "manual_review"} and reason_category in reason_override_categories:
        category = reason_category
    if category == "human_auth" and reason_category in reason_override_categories:
        category = reason_category
    if parent_action and category_from_action(parent_action) != category and category in reason_override_categories:
        parent_action = parent_action_for(category)
    if not parent_action:
        parent_action = parent_action_for(category)
    retryable = bool(report.get("retryable")) or category in RETRYABLE_CATEGORIES or parent_action == "wait_and_retry"
    if category:
        contexts.append({"category": category, "parent_action": parent_action, "retryable": retryable})
    nested = report.get("dependency_reports")
    if isinstance(nested, list):
        for item in nested:
            if isinstance(item, dict):
                contexts.extend(contexts_from_report(item))
    return contexts


def inherited_dependency_context(report: dict[str, Any]) -> dict[str, Any] | None:
    nested = report.get("dependency_reports")
    if not isinstance(nested, list) or not nested:
        return None
    contexts: list[dict[str, Any]] = []
    for item in nested:
        if isinstance(item, dict):
            contexts.extend(contexts_from_report(item))
    if not contexts:
        return None
    if contexts and all(bool(item.get("retryable")) for item in contexts):
        categories = {str(item.get("category") or "") for item in contexts}
        categories.discard("")
        if len(categories) == 1:
            category = next(iter(categories))
            return {"category": category, "parent_action": parent_action_for(category), "retryable": True}
        return {"category": "rate_limit", "parent_action": "wait_and_retry", "retryable": True}
    return min(contexts, key=lambda item: CATEGORY_PRIORITY.get(str(item.get("category") or ""), 99))


def insert_event(connection: sqlite3.Connection, task_id: str | None, event_type: str, message: str, payload: dict[str, Any]) -> None:
    connection.execute(
        "insert into event_records(task_id, type, message, payload_json, created_at) values (?, ?, ?, ?, ?)",
        (task_id, event_type, message, json.dumps(payload, ensure_ascii=False), utc_now_db()),
    )


def repair_replacement_dependencies(
    connection: sqlite3.Connection,
    tasks: list[sqlite3.Row],
    args: argparse.Namespace,
) -> tuple[list[str], list[str]]:
    rows_by_id = {str(row["id"]): row for row in tasks}
    siblings_by_parent = parent_children(tasks)
    dependency_fixes: list[str] = []
    requeued: list[str] = []
    for task in tasks:
        if str(task["status"]) not in REPAIRABLE_TASK_STATUSES:
            continue
        meta = load_json(task["node_meta_json"], {})
        if not isinstance(meta, dict) or not is_blocked_child_repair(meta, str(task["title"] or "")):
            continue
        parent_id = str(task["parent_id"] or "")
        siblings = siblings_by_parent.get(parent_id, [])
        siblings_by_id = {str(row["id"]): row for row in siblings}
        siblings_by_title = {normalize(str(row["title"] or "")): str(row["id"]) for row in siblings}

        depends_on_ids = [str(item) for item in meta.get("depends_on_ids", []) if str(item)]
        depends_on = [str(item) for item in meta.get("depends_on", []) if str(item)]
        dropped = [
            dependency_id
            for dependency_id in depends_on_ids
            if (dependency := siblings_by_id.get(dependency_id)) is not None
            and str(dependency["status"]) in BAD_DEPENDENCY_STATUSES
        ]
        if not dropped:
            continue
        if len(dependency_fixes) >= max(1, args.limit):
            break
        dropped_set = set(dropped)
        filtered_depends_on_ids = [dependency_id for dependency_id in depends_on_ids if dependency_id not in dropped_set]
        filtered_depends_on = []
        for dependency in depends_on:
            dependency_id = dependency if dependency in rows_by_id else siblings_by_title.get(normalize(dependency))
            if dependency_id in dropped_set:
                continue
            filtered_depends_on.append(dependency)
        previous_dropped = [str(item) for item in meta.get("dropped_blocked_repair_dependency_ids", []) if str(item)]
        meta["depends_on_ids"] = filtered_depends_on_ids
        meta["depends_on"] = filtered_depends_on
        meta["dropped_blocked_repair_dependency_ids"] = sorted(set(previous_dropped) | dropped_set)
        meta["blocker_recovery_dependency_repaired_at"] = datetime.now(timezone.utc).isoformat()
        meta["blocker_recovery_dependency_repaired_by"] = "repair_blocker_recovery_dependencies.py"
        next_status = str(task["status"])
        should_requeue = next_status == "blocked" and not filtered_depends_on_ids
        if should_requeue:
            next_status = "queued"
            requeued.append(str(task["id"]))
        dependency_fixes.append(str(task["id"]))
        if args.dry_run:
            continue
        if should_requeue:
            connection.execute(
                "update task_nodes set status = ?, error_message = null, node_meta_json = ?, updated_at = ? where id = ?",
                (next_status, json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
            )
        else:
            connection.execute(
                "update task_nodes set node_meta_json = ?, updated_at = ? where id = ?",
                (json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
            )
        insert_event(
            connection,
            str(task["id"]),
            "task.blocked_repair_dependencies_pruned",
            "Blocked-child repair task dependency list was pruned so it can replace blocked siblings.",
            {"root_id": args.root_id, "dropped_dependency_ids": dropped, "requeued": should_requeue},
        )
    return dependency_fixes, requeued


def repair_dependency_categories(
    connection: sqlite3.Connection,
    tasks: list[sqlite3.Row],
    args: argparse.Namespace,
) -> list[str]:
    reclassified: list[str] = []
    for task in tasks:
        if str(task["status"]) != "blocked":
            continue
        meta = load_json(task["node_meta_json"], {})
        if not isinstance(meta, dict):
            continue
        report = meta.get("block_report")
        if not isinstance(report, dict) or report.get("source") != "dependency.blocked":
            continue
        inherited = inherited_dependency_context(report)
        if inherited is None:
            continue
        category = str(inherited.get("category") or "")
        parent_action = str(inherited.get("parent_action") or parent_action_for(category))
        retryable = bool(inherited.get("retryable")) or category in RETRYABLE_CATEGORIES or parent_action == "wait_and_retry"
        if report.get("category") == category and report.get("parent_action") == parent_action and bool(report.get("retryable")) == retryable:
            continue
        if len(reclassified) >= max(1, args.limit):
            break
        report["category"] = category
        report["parent_action"] = parent_action
        report["retryable"] = retryable
        report["reclassified_at"] = datetime.now(timezone.utc).isoformat()
        report["reclassified_by"] = "repair_blocker_recovery_dependencies.py"
        if category not in {"human_auth", "visual_uncertain"} and parent_action not in {"human_auth_required", "human_visual_review"}:
            report.pop("human_action_required", None)
        elif category in {"human_auth", "visual_uncertain"}:
            report["human_action_required"] = True
        meta["block_report"] = report
        reclassified.append(str(task["id"]))
        if args.dry_run:
            continue
        connection.execute(
            "update task_nodes set node_meta_json = ?, updated_at = ? where id = ?",
            (json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
        )
        insert_event(
            connection,
            str(task["id"]),
            "task.dependency_block_category_reclassified",
            "Dependency-blocked task inherited the dependency block category instead of re-parsing the combined reason.",
            {"root_id": args.root_id, "category": category, "parent_action": parent_action},
        )
    return reclassified


def repair_dependency_replacements(
    connection: sqlite3.Connection,
    tasks: list[sqlite3.Row],
    args: argparse.Namespace,
) -> list[str]:
    rows_by_id = {str(row["id"]): row for row in tasks}
    repaired: list[str] = []
    for task in tasks:
        if str(task["status"]) != "blocked":
            continue
        meta = load_json(task["node_meta_json"], {})
        if not isinstance(meta, dict):
            continue
        own_repair_ids = meta.get("superseded_by_repair_child_ids")
        if isinstance(own_repair_ids, list) and any(
            (repair := rows_by_id.get(str(repair_id))) is not None
            and str(repair["status"]) not in {"failed", "blocked", "canceled", "paused", "hibernating"}
            for repair_id in own_repair_ids
        ):
            continue
        report = meta.get("block_report")
        if not isinstance(report, dict) or report.get("source") != "dependency.blocked":
            continue
        depends_on_ids = [str(item) for item in meta.get("depends_on_ids", []) if str(item)]
        if not depends_on_ids:
            continue
        changed = False
        next_depends_on_ids: list[str] = []
        replacement_map: dict[str, list[str]] = {}
        for dependency_id in depends_on_ids:
            dependency = rows_by_id.get(dependency_id)
            if dependency is None or str(dependency["status"]) not in BAD_DEPENDENCY_STATUSES:
                next_depends_on_ids.append(dependency_id)
                continue
            dependency_meta = load_json(dependency["node_meta_json"], {})
            repair_ids = dependency_meta.get("superseded_by_repair_child_ids") if isinstance(dependency_meta, dict) else None
            usable_repair_ids: list[str] = []
            if isinstance(repair_ids, list):
                for repair_id_raw in repair_ids:
                    repair_id = str(repair_id_raw)
                    repair = rows_by_id.get(repair_id)
                    if repair is None:
                        continue
                    if str(repair["status"]) in {"failed", "blocked", "canceled", "paused", "hibernating"}:
                        continue
                    usable_repair_ids.append(repair_id)
            if usable_repair_ids:
                changed = True
                replacement_map[dependency_id] = usable_repair_ids
                for repair_id in usable_repair_ids:
                    if repair_id not in next_depends_on_ids and repair_id != str(task["id"]):
                        next_depends_on_ids.append(repair_id)
            else:
                next_depends_on_ids.append(dependency_id)
        if not changed:
            continue
        if len(repaired) >= max(1, args.limit):
            break
        next_depends_on: list[str] = []
        for dependency_id in next_depends_on_ids:
            dependency = rows_by_id.get(dependency_id)
            if dependency is not None:
                next_depends_on.append(str(dependency["title"] or dependency_id))
        previous_block_report = meta.get("block_report")
        meta["depends_on_ids"] = next_depends_on_ids
        meta["depends_on"] = next_depends_on
        meta["dependency_block_retargeted_at"] = datetime.now(timezone.utc).isoformat()
        meta["dependency_block_retargeted_by"] = "repair_blocker_recovery_dependencies.py"
        meta["dependency_replacement_map"] = replacement_map
        if isinstance(previous_block_report, dict):
            meta["previous_dependency_block_report"] = previous_block_report
        meta.pop("block_report", None)
        repaired.append(str(task["id"]))
        if args.dry_run:
            continue
        connection.execute(
            "update task_nodes set status = 'queued', error_message = null, node_meta_json = ?, updated_at = ? where id = ?",
            (json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
        )
        insert_event(
            connection,
            str(task["id"]),
            "task.dependency_block_retargeted_to_repair",
            "Dependency-blocked task was requeued against the repair child that supersedes its blocked dependency.",
            {"root_id": args.root_id, "replacement_map": replacement_map, "depends_on_ids": next_depends_on_ids},
        )
        parent_id = task["parent_id"]
        if parent_id:
            insert_event(
                connection,
                str(parent_id),
                "parent.dependency_block_retargeted_to_repair",
                "A dependency-blocked child was requeued against superseding repair children.",
                {"root_id": args.root_id, "child_id": str(task["id"]), "replacement_map": replacement_map},
            )
    return repaired


def requeue_unblocked_dependency_waiters(
    connection: sqlite3.Connection,
    tasks: list[sqlite3.Row],
    args: argparse.Namespace,
) -> tuple[list[str], list[str]]:
    rows_by_id = {str(row["id"]): row for row in tasks}
    requeued: list[str] = []
    skipped_optional: list[str] = []
    for task in tasks:
        if str(task["status"]) != "blocked":
            continue
        meta = load_json(task["node_meta_json"], {})
        if not isinstance(meta, dict):
            continue
        report = meta.get("block_report")
        if not isinstance(report, dict) or report.get("source") != "dependency.blocked":
            continue
        depends_on_ids = [str(item) for item in meta.get("depends_on_ids", []) if str(item)]
        if not depends_on_ids:
            continue
        dependencies = [rows_by_id.get(dependency_id) for dependency_id in depends_on_ids]
        if any(dependency is None for dependency in dependencies):
            continue
        present_dependencies = [dependency for dependency in dependencies if dependency is not None]
        optional_dependencies = [
            dependency
            for dependency in present_dependencies
            if str(dependency["status"]) in BAD_DEPENDENCY_STATUSES and is_optional_skill_lookup_dependency(dependency)
        ]
        if optional_dependencies:
            drop_dependency_references(meta, optional_dependencies)
            optional_ids = {str(dependency["id"]) for dependency in optional_dependencies}
            depends_on_ids = [dependency_id for dependency_id in depends_on_ids if dependency_id not in optional_ids]
            present_dependencies = [dependency for dependency in present_dependencies if str(dependency["id"]) not in optional_ids]
            skipped_optional.append(str(task["id"]))
        if any(str(dependency["status"]) in BAD_DEPENDENCY_STATUSES for dependency in present_dependencies):
            continue
        if len(requeued) >= max(1, args.limit):
            break
        meta["previous_dependency_block_report"] = report
        meta["dependency_block_unblocked_at"] = datetime.now(timezone.utc).isoformat()
        meta["dependency_block_unblocked_by"] = "repair_blocker_recovery_dependencies.py"
        meta.pop("block_report", None)
        requeued.append(str(task["id"]))
        if args.dry_run:
            continue
        connection.execute(
            "update task_nodes set status = 'queued', error_message = null, node_meta_json = ?, updated_at = ? where id = ?",
            (json.dumps(meta, ensure_ascii=False), utc_now_db(), task["id"]),
        )
        insert_event(
            connection,
            str(task["id"]),
            "task.dependency_block_unblocked_requeued",
            "Dependency-blocked task was requeued because its dependencies are no longer blocked, failed, or canceled.",
            {"root_id": args.root_id, "depends_on_ids": depends_on_ids},
        )
        if optional_dependencies:
            insert_event(
                connection,
                str(task["id"]),
                "task.optional_dependency_skipped",
                "Canceled optional Skill/KM dependency was removed before requeue.",
                {"root_id": args.root_id, "dependencies": [str(dependency["id"]) for dependency in optional_dependencies]},
            )
    return requeued, skipped_optional


def main() -> int:
    args = parse_args()
    connection = sqlite3.connect(str(Path(args.db)))
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "select id, parent_id, status, title, error_message, node_meta_json, updated_at "
                "from task_nodes order by depth, updated_at"
            )
        )
        tasks = subtree(rows, args.root_id)
        dependency_fixes, requeued = repair_replacement_dependencies(connection, tasks, args)
        reclassified = repair_dependency_categories(connection, tasks, args)
        retargeted = repair_dependency_replacements(connection, tasks, args)
        unblocked, skipped_optional = requeue_unblocked_dependency_waiters(connection, tasks, args)
        requeued = requeued + unblocked
        if not args.dry_run:
            connection.commit()
        print(
            json.dumps(
                {
                    "dependency_fixes": dependency_fixes,
                    "reclassified": reclassified,
                    "retargeted": retargeted,
                    "skipped_optional": skipped_optional,
                    "unblocked": unblocked,
                    "requeued": requeued,
                },
                ensure_ascii=False,
            )
        )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
