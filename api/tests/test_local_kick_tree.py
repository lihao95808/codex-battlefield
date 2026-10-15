from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _load_local_kick_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "local_kick_tree.py"
    spec = importlib.util.spec_from_file_location("local_kick_tree_under_test", script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_secondary_lock_can_top_off_while_primary_lock_is_busy(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    primary = module.acquire_root_lock("root-under-test")
    assert primary is not None
    try:
        assert module.acquire_root_lock("root-under-test") is None
        secondary = module.acquire_root_lock("root-under-test", ".secondary")
        assert secondary is not None
        try:
            assert module.acquire_root_lock("root-under-test", ".secondary") is None
        finally:
            module.release_root_lock(secondary)
    finally:
        module.release_root_lock(primary)


def test_controller_lock_refuses_primary_when_secondary_is_active(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    secondary = module.acquire_root_lock("root-under-test", ".secondary")
    assert secondary is not None
    try:
        lock_file, lock_suffix, busy_reason = module.acquire_controller_lock("root-under-test")
    finally:
        module.release_root_lock(secondary)

    assert lock_file is None
    assert lock_suffix == ""
    assert busy_reason == "secondary_active"


def test_controller_lock_can_take_primary_when_secondary_is_active_for_watchdog_topoff(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    secondary = module.acquire_root_lock("root-under-test", ".secondary")
    assert secondary is not None
    try:
        lock_file, lock_suffix, busy_reason = module.acquire_controller_lock(
            "root-under-test",
            allow_secondary=False,
            allow_primary_with_secondary=True,
        )
        assert lock_file is not None
        try:
            assert lock_suffix == ""
            assert busy_reason == "secondary_active_takeover"
        finally:
            module.release_root_lock(lock_file)
    finally:
        module.release_root_lock(secondary)


def test_controller_lock_uses_secondary_when_primary_is_active(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    primary = module.acquire_root_lock("root-under-test")
    assert primary is not None
    try:
        lock_file, lock_suffix, busy_reason = module.acquire_controller_lock("root-under-test")
        assert lock_file is not None
        try:
            assert lock_suffix == ".secondary"
            assert busy_reason == ""
        finally:
            module.release_root_lock(lock_file)
    finally:
        module.release_root_lock(primary)


def test_controller_lock_can_refuse_secondary_for_watchdog_fallback(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    primary = module.acquire_root_lock("root-under-test")
    assert primary is not None
    try:
        lock_file, lock_suffix, busy_reason = module.acquire_controller_lock(
            "root-under-test",
            allow_secondary=False,
        )
    finally:
        module.release_root_lock(primary)

    assert lock_file is None
    assert lock_suffix == ""
    assert busy_reason == "primary_active"


def test_watchdog_topoff_uses_exclusive_local_kick_when_capacity_is_one():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "battlefield_watchdog.ps1"
    script = script_path.read_text(encoding="utf-8")
    start = script.index("function Start-LocalSlotTopOffFallback")
    end = script.index("function Test-GoalModeLocalTopOffFirst", start)
    topoff_block = script[start:end]

    assert "if ($Capacity -le 1)" in topoff_block
    assert '"--disable-secondary-lock"' in topoff_block
    assert '"--allow-primary-with-secondary"' in topoff_block
    assert '"--topoff-only"' in topoff_block
    assert '"--no-controller-lock"' not in topoff_block


def test_launch_capacity_uses_shared_effective_slots_without_double_counting():
    module = _load_local_kick_module()

    assert module._has_launch_capacity(latest_slots=12, capacity=13)
    assert not module._has_launch_capacity(latest_slots=13, capacity=13)
    assert module._has_launch_capacity(latest_slots=8, capacity=13)


def test_effective_slot_count_ignores_stale_db_active_statuses():
    module = _load_local_kick_module()

    assert module._effective_slot_count(process_slots=12, active_statuses=13) == 12
    assert module._effective_slot_count(process_slots=14, active_statuses=13) == 14
    assert module._has_launch_capacity(module._effective_slot_count(12, 13), capacity=13)


def test_recent_starts_expire_without_pinballing_active_tasks():
    module = _load_local_kick_module()
    recent = {
        "old-idle": 0.0,
        "old-active": 0.0,
        "recent-idle": 95.0,
    }

    module._prune_recent_starts(
        recent,
        {"old-active"},
        now=130.0,
        ttl_seconds=60.0,
    )

    assert recent == {
        "old-active": 0.0,
        "recent-idle": 95.0,
    }


def test_pending_background_tasks_filters_done_and_non_tasks():
    module = _load_local_kick_module()

    async def scenario():
        done_task = asyncio.create_task(asyncio.sleep(0))
        await done_task
        pending_task = asyncio.create_task(asyncio.sleep(30))

        class FakeOrchestrator:
            _background_tasks = {done_task, pending_task, object()}

        try:
            assert module._pending_background_tasks(FakeOrchestrator()) == {pending_task}
        finally:
            pending_task.cancel()
            try:
                await pending_task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_local_kick_drain_flag_can_target_one_root(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path

    (tmp_path / "local_kick_drain.json").write_text('{"root_ids":["drain-root"]}', encoding="utf-8")

    assert module._local_kick_drain_requested("drain-root")
    assert not module._local_kick_drain_requested("other-root")


def test_local_kick_tool_head_drain_keeps_replacement_scheduler(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path
    module.LOCAL_KICK_START_TOOL_HEAD = "new-head"

    (tmp_path / "local_kick_drain.json").write_text(
        '{"root_ids":["drain-root"],"replacement_tool_head":"new-head","drain":true}',
        encoding="utf-8",
    )

    assert not module._local_kick_drain_requested("drain-root")

    module.LOCAL_KICK_START_TOOL_HEAD = "old-head"

    assert module._local_kick_drain_requested("drain-root")


def test_local_kick_tool_head_drain_keeps_descendant_scheduler(tmp_path):
    module = _load_local_kick_module()
    module.WORKSPACE = tmp_path
    module.LOCAL_KICK_START_TOOL_HEAD = "descendant-head"

    calls: list[tuple[str, str]] = []

    def fake_head_check(replacement_head: str, start_head: str) -> bool:
        calls.append((replacement_head, start_head))
        return replacement_head == "ancestor-head" and start_head == "descendant-head"

    module._tool_head_matches_or_descends_from = fake_head_check
    (tmp_path / "local_kick_drain.json").write_text(
        '{"root_ids":["drain-root"],"replacement_tool_head":"ancestor-head","drain":true}',
        encoding="utf-8",
    )

    assert not module._local_kick_drain_requested("drain-root")
    assert calls == [("ancestor-head", "descendant-head")]


def test_local_kick_runs_recovery_hooks_before_topoff():
    module = _load_local_kick_module()
    calls: list[str] = []

    class FakeOrchestrator:
        def settle_unreachable_descendants_under_terminal_ancestors(self, root_id):
            calls.append(f"settle:{root_id}")
            return {"status": "settled", "count": 1, "task_ids": ["stale-child"]}

        def backfill_skill_note_children(self, root_id):
            calls.append(f"skill:{root_id}")
            return {"status": "backfilled", "count": 0}

        def recover_deferred_parent_resumes(self, root_id):
            calls.append(f"deferred:{root_id}")
            return {"status": "scheduled", "count": 1, "scheduled": [{"parent_id": "parent", "child_ids": []}]}

        def recover_finished_agent_messages(self, root_id):
            calls.append(f"agent:{root_id}")
            return 0

        def recover_finished_acceptance_messages(self, root_id):
            calls.append(f"acceptance:{root_id}")
            return 0

        def cleanup_stalled_external_codex_agents(self, root_id):
            calls.append(f"cleanup:{root_id}")
            return {"status": "ok", "requeued": 0}

        def _auto_retry_fast_retryable_blocks(self, root_id):
            calls.append(f"retry:{root_id}")
            return {"status": "checked", "count": 0}

    results = module._run_local_recovery_hooks(FakeOrchestrator(), "root-hook")

    assert calls == [
        "settle:root-hook",
        "skill:root-hook",
        "deferred:root-hook",
        "agent:root-hook",
        "acceptance:root-hook",
        "cleanup:root-hook",
        "retry:root-hook",
    ]
    assert results["recover_deferred_parent_resumes"]["count"] == 1


def test_watchdog_requests_local_kick_drain_for_tool_head_change():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "battlefield_watchdog.ps1"
    script = script_path.read_text(encoding="utf-8")

    assert "$LocalKickDrainPath" in script
    assert "function Test-LocalKickDrainCoversToolHead" in script
    assert "function Invoke-LocalKickDrainIfToolHeadChanged" in script
    assert "function Stop-DrainedLocalKickSchedulers" in script
    assert "replacement_tool_head" in script
    assert "CODEX_BATTLEFIELD_LOCAL_KICK_TOOL_HEAD" in script
    drain_start = script.index("function Invoke-LocalKickDrainIfToolHeadChanged")
    drain_end = script.index("function Invoke-FullCheckpointIfToolHeadChanged", drain_start)
    drain_block = script[drain_start:drain_end]
    assert "Test-LocalKickDrainCoversToolHead" in drain_block
    assert "Test-LatestCheckpointCoversBattlefieldHead" in drain_block
    stop_start = script.index("function Stop-DrainedLocalKickSchedulers")
    stop_end = script.index("function Get-MachineSafetyState", stop_start)
    stop_block = script[stop_start:stop_end]
    assert "$pid =" not in stop_block.lower()
    root_drain = script.index("Invoke-LocalKickDrainIfToolHeadChanged -EffectiveRootId $effectiveRootId")
    root_stop = script.index("Stop-DrainedLocalKickSchedulers -EffectiveRootId $effectiveRootId", root_drain)
    root_topoff = script.index("Invoke-SlotTopOffIfNeeded -State $state -EffectiveRootId $effectiveRootId", root_stop)
    auto_drain = script.index("Invoke-LocalKickDrainIfToolHeadChanged -EffectiveRootId $state.root_id")
    auto_stop = script.index("Stop-DrainedLocalKickSchedulers -EffectiveRootId $state.root_id", auto_drain)
    auto_topoff = script.index("Invoke-SlotTopOffIfNeeded -State $state -EffectiveRootId $state.root_id", auto_stop)
    assert root_drain < root_stop < root_topoff
    assert auto_drain < auto_stop < auto_topoff


def test_codex_resource_block_active_until_observed_reset_time():
    module = _load_local_kick_module()

    class FakeOrchestrator:
        def _codex_health_state(self):
            return {
                "codex_health_status": "resource_block",
                "codex_quota_reset_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            }

    assert module._codex_resource_block_active(FakeOrchestrator())


def test_codex_resource_block_clears_after_observed_reset_time():
    module = _load_local_kick_module()

    class FakeOrchestrator:
        def _codex_health_state(self):
            return {
                "codex_health_status": "resource_block",
                "codex_quota_reset_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            }

    assert not module._codex_resource_block_active(FakeOrchestrator())
