"""R4 回归测试：集成冲突不再静默。

**要证明的三件事**（缺一即未达成 spec 的验收）：
1. 冲突被**识别**——同一路径被两个子节点产出时，产生 `path_collision` 且两个产出方都在记录里；
2. 冲突被**上浮**——明细写进 `node_meta["parent_integration"]`，并出现在父节点验收 prompt 里
   （这才是"不再静默"的反面：数据必须到达做决定的那一方）；
3. 默认策略**行为不变**——后写仍然覆盖（用户选定的默认档），只是覆盖不再无声。

另测 `keep_both` 与 `repair` 两个可配档，以及 destructive overwrite 的识别。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app import integration
from app.database import engine, init_db, SessionLocal
from app.models import Base, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner
from app.utils import dumps


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


# ---- 纯模块层：判定与文书 ----------------


def test_detect_path_collisions_reports_both_writers() -> None:
    integrated = [
        {"child_id": "c1", "output_node_id": "n1", "rel": "src/a.gd", "destination": "/t/src/a.gd", "mode": "copy"},
        {"child_id": "c2", "output_node_id": "n2", "rel": "src/a.gd", "destination": "/t/src/a.gd", "mode": "copy"},
    ]
    conflicts = integration.detect_path_collisions(integrated)
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["type"] == integration.CONFLICT_PATH_COLLISION
    assert conflict["path"] == "src/a.gd"
    assert conflict["writers"] == ["n1", "n2"]
    # 后写者留、先写者被覆盖——必须说清是哪一份消失了
    assert conflict["kept"] == "n2"
    assert conflict["overwritten"] == ["n1"]


def test_detect_path_collisions_ignores_single_writer_repeats() -> None:
    """同一产出方重复列出同一路径不算冲突（changed_files 可能有重复）。"""
    integrated = [
        {"child_id": "c1", "output_node_id": "n1", "rel": "a.gd"},
        {"child_id": "c1", "output_node_id": "n1", "rel": "a.gd"},
    ]
    assert integration.detect_path_collisions(integrated) == []


def test_detect_path_collisions_ignores_distinct_paths() -> None:
    integrated = [
        {"child_id": "c1", "output_node_id": "n1", "rel": "a.gd"},
        {"child_id": "c2", "output_node_id": "n2", "rel": "b.gd"},
    ]
    assert integration.detect_path_collisions(integrated) == []


def test_destructive_overwrite_conflict_only_for_non_merging_modes() -> None:
    def build(mode: str, existed: bool) -> dict | None:
        return integration.destructive_overwrite_conflict(
            rel="x.txt", destination="/t/x.txt", mode=mode, output_node_id="n1", destination_existed=existed
        )

    assert build("copy", True) is not None
    assert build("copytree", True) is not None
    # 有合并语义的分支不算破坏性覆盖
    assert build("merge_project_godot", True) is None
    assert build("append_readme", True) is None
    assert build("unchanged", True) is None
    # 目标本来不存在就无所谓覆盖
    assert build("copy", False) is None


def test_strategy_normalization_falls_back_to_default() -> None:
    assert integration.normalize_strategy("keep_both") == "keep_both"
    assert integration.normalize_strategy("REPAIR") == "repair"
    assert integration.normalize_strategy("nonsense") == integration.DEFAULT_STRATEGY
    assert integration.normalize_strategy(None) == integration.DEFAULT_STRATEGY


def test_report_meta_keeps_backward_compatible_counts_and_adds_detail() -> None:
    report = integration.IntegrationReport(target="/t", target_kind="root_target")
    report.integrated.append({"rel": "a.gd", "child_id": "c1"})
    report.skipped.append({"path": "x", "reason": "source missing"})
    report.conflicts.append({"type": integration.CONFLICT_PATH_COLLISION, "path": "a.gd"})

    meta = report.to_meta()
    # 旧键必须在：既有调用方与看板依赖它们
    assert meta["integrated_count"] == 1
    assert meta["skipped_count"] == 1
    # 新键：明细上浮
    assert meta["conflict_count"] == 1
    assert meta["conflicts"][0]["path"] == "a.gd"
    assert meta["skipped"][0]["reason"] == "source missing"
    assert meta["integrated_files"] == ["a.gd"]
    assert meta["blocking"] is False


def test_report_meta_truncates_detail_but_keeps_count() -> None:
    report = integration.IntegrationReport()
    for index in range(7):
        report.conflicts.append({"type": integration.CONFLICT_PATH_COLLISION, "path": f"p{index}"})
    meta = report.to_meta(detail_limit=3)
    assert meta["conflict_count"] == 7
    assert len(meta["conflicts"]) == 3
    # 被截断的事实本身必须可见，否则"看不见"会被误读成"没有"
    assert meta["conflicts_truncated"] == 4


def test_repair_strategy_marks_blocking() -> None:
    report = integration.IntegrationReport(strategy=integration.STRATEGY_REPAIR)
    assert report.blocking is False
    report.conflicts.append({"type": integration.CONFLICT_PATH_COLLISION, "path": "a.gd"})
    assert report.blocking is True
    assert report.to_meta()["blocking"] is True


def test_collision_directive_is_empty_without_conflicts() -> None:
    """无冲突时不得给 prompt 增加任何负担。"""
    assert integration.collision_directive({}) == ""
    assert integration.collision_directive({"conflicts": []}) == ""


def test_collision_directive_states_paths_and_requires_verdict() -> None:
    meta = {
        "conflicts": [
            {
                "type": integration.CONFLICT_PATH_COLLISION,
                "path": "src/a.gd",
                "kept": "n2",
                "overwritten": ["n1"],
            }
        ],
        "conflicts_truncated": 0,
        "blocking": False,
    }
    directive = integration.collision_directive(meta)
    assert "src/a.gd" in directive
    assert "n2" in directive and "n1" in directive
    # 关键：要求逐条裁决，而不是"仅供参考"
    assert "逐条" in directive
    # 非阻断策略下不得出现阻断措辞
    assert "BLOCKING" not in directive

    blocking = integration.collision_directive({**meta, "blocking": True})
    assert "INTEGRATION_CONFLICT_BLOCKING" in blocking


def test_collision_directive_mentions_truncation() -> None:
    directive = integration.collision_directive(
        {"conflicts": [{"path": "a", "kept": "n1", "overwritten": ["n0"]}], "conflicts_truncated": 5}
    )
    assert "5" in directive


# ---- 集成路径层：真实冲突 ----------------


def _child(node_id: str, worktree: Path, changed_files: list[str]) -> TaskNode:
    return TaskNode(
        id=node_id,
        title=node_id,
        status=TaskStatus.DONE.value,
        depth=1,
        worktree_path=str(worktree),
        node_meta_json="{}",
        result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": changed_files}}),
    )


def _parent(target: Path) -> TaskNode:
    return TaskNode(
        id="parent-1",
        title="父节点",
        status=TaskStatus.ACCEPTING.value,
        depth=0,
        scope_json=dumps([str(target)]),
        node_meta_json="{}",
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_integration_records_real_path_collision(tmp_path: Path) -> None:
    """Spec 的验收：造一次真实冲突，父节点能看到明细，且不再静默。"""
    target = tmp_path / "target"
    child_a = tmp_path / "wt-a"
    child_b = tmp_path / "wt-b"
    _write(child_a / "src" / "shared.gd", "FROM_A")
    _write(child_b / "src" / "shared.gd", "FROM_B")

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        report = orchestrator._integrate_child_outputs(
            db, parent, [_child("c-a", child_a, ["src/shared.gd"]), _child("c-b", child_b, ["src/shared.gd"])]
        )
        db.commit()
        meta = json.loads(parent.node_meta_json)["parent_integration"]

    assert report is not None
    assert report.has_conflicts
    collisions = [item for item in report.conflicts if item["type"] == integration.CONFLICT_PATH_COLLISION]
    assert len(collisions) == 1
    assert sorted(collisions[0]["writers"]) == ["c-a", "c-b"]

    # 上浮：明细进了 meta（验收 prompt 直接读这个结构）
    assert meta["conflict_count"] >= 1
    assert meta["conflicts"][0]["path"] == "src/shared.gd"
    # 默认档行为不变：后写覆盖
    assert (target / "src" / "shared.gd").read_text(encoding="utf-8") == "FROM_B"


@pytest.mark.asyncio
async def test_default_overwrite_still_overwrites_but_never_silently(tmp_path: Path) -> None:
    """默认 overwrite：产物仍是后写者，但冲突必须留下记录。"""
    target = tmp_path / "target"
    child_a = tmp_path / "wt-a"
    child_b = tmp_path / "wt-b"
    _write(child_a / "x.txt", "A")
    _write(child_b / "x.txt", "B")

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        report = orchestrator._integrate_child_outputs(
            db, parent, [_child("c-a", child_a, ["x.txt"]), _child("c-b", child_b, ["x.txt"])]
        )
        db.commit()
        events = [event.type for event in parent.events]
        meta = json.loads(parent.node_meta_json)["parent_integration"]

    assert (target / "x.txt").read_text(encoding="utf-8") == "B"
    assert report is not None and report.strategy == integration.STRATEGY_OVERWRITE
    assert meta["strategy"] == "overwrite"
    # 冲突有专门事件，不再只混在 parent.integration 里
    assert "parent.integration_conflict" in events


@pytest.mark.asyncio
async def test_acceptance_prompt_carries_conflict_directive(tmp_path: Path) -> None:
    """端到端的关键一环：冲突必须真的出现在**验收 prompt** 里。"""
    target = tmp_path / "target"
    child_a = tmp_path / "wt-a"
    child_b = tmp_path / "wt-b"
    _write(child_a / "x.txt", "A")
    _write(child_b / "x.txt", "B")

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        orchestrator._integrate_child_outputs(
            db, parent, [_child("c-a", child_a, ["x.txt"]), _child("c-b", child_b, ["x.txt"])]
        )
        db.commit()
        db.refresh(parent)
        prompt = CodexExecRunner(orchestrator.settings)._acceptance_prompt(parent, [])

    assert "INTEGRATION_CONFLICT_RULE" in prompt
    assert "x.txt" in prompt
    assert "逐条" in prompt


@pytest.mark.asyncio
async def test_keep_both_strategy_renames_instead_of_overwriting(tmp_path: Path) -> None:
    target = tmp_path / "target"
    child_a = tmp_path / "wt-a"
    child_b = tmp_path / "wt-b"
    _write(child_a / "x.txt", "A")
    _write(child_b / "x.txt", "B")

    orchestrator = Orchestrator()
    orchestrator.settings = replace(orchestrator.settings, integration_conflict_strategy="keep_both")
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        report = orchestrator._integrate_child_outputs(
            db, parent, [_child("c-a", child_a, ["x.txt"]), _child("c-b", child_b, ["x.txt"])]
        )
        db.commit()

    assert report is not None
    # 两份都在：原名给了先写者，后写者落到带 id 的文件名
    assert (target / "x.txt").read_text(encoding="utf-8") == "A"
    renamed = list(target.glob("x.c-b*.txt"))
    assert len(renamed) == 1
    assert renamed[0].read_text(encoding="utf-8") == "B"
    # 冲突仍然被记录（保留两份不等于没有冲突，父节点仍需裁决哪份进最终产物）
    assert report.has_conflicts


@pytest.mark.asyncio
async def test_skipped_detail_reaches_meta(tmp_path: Path) -> None:
    """跳过明细也必须上浮，这是原先"只进事件流"的那一类。"""
    target = tmp_path / "target"
    child = tmp_path / "wt-a"

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        orchestrator._integrate_child_outputs(db, parent, [_child("c-a", child, ["missing.gd"])])
        db.commit()
        meta = json.loads(parent.node_meta_json)["parent_integration"]

    assert meta["skipped_count"] == 1
    assert meta["skipped"][0]["reason"] == "source missing"
    assert meta["skipped_truncated"] == 0


@pytest.mark.asyncio
async def test_destructive_overwrite_of_existing_file_is_recorded(tmp_path: Path) -> None:
    """目标预先存在且被无合并语义地覆盖时，单独记一条破坏性覆盖。"""
    target = tmp_path / "target"
    _write(target / "x.txt", "PREEXISTING")
    child = tmp_path / "wt-a"
    _write(child / "x.txt", "NEW")

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        report = orchestrator._integrate_child_outputs(db, parent, [_child("c-a", child, ["x.txt"])])
        db.commit()

    assert report is not None
    kinds = {item["type"] for item in report.conflicts}
    assert integration.CONFLICT_DESTRUCTIVE_OVERWRITE in kinds
    assert (target / "x.txt").read_text(encoding="utf-8") == "NEW"


@pytest.mark.asyncio
async def test_disjoint_children_produce_no_conflicts(tmp_path: Path) -> None:
    """无冲突的常见路径不得产生噪音记录。"""
    target = tmp_path / "target"
    child_a = tmp_path / "wt-a"
    child_b = tmp_path / "wt-b"
    _write(child_a / "a.txt", "A")
    _write(child_b / "b.txt", "B")

    orchestrator = Orchestrator()
    with SessionLocal() as db:
        parent = _parent(target)
        db.add(parent)
        db.commit()
        report = orchestrator._integrate_child_outputs(
            db, parent, [_child("c-a", child_a, ["a.txt"]), _child("c-b", child_b, ["b.txt"])]
        )
        db.commit()
        meta = json.loads(parent.node_meta_json)["parent_integration"]

    assert report is not None
    assert report.conflicts == []
    assert meta["conflict_count"] == 0
    assert meta["integrated_count"] == 2
