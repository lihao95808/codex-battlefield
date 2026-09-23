"""需求→证据追溯的接线测试（`docs/spec-requirement-traceability.md`，判据 T1–T6）。

**为什么必须真跑验收**：这条判据的全部价值在于"**漏需求**与**做错需求**都拦得住"。
只测 `uncovered()` 返回什么，测不出"它有没有真的进到门禁前面"。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app import obligations, requirements
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import BaseCodexRunner, RunnerEnvelope
from app.schemas import AcceptanceResult
from app.utils import dumps

CHECK_FILE = "checks/acceptance.txt"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


class AcceptancePassOnlyRunner(BaseCodexRunner):
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def verify(self, task: TaskNode, worker_result) -> RunnerEnvelope:
        raise NotImplementedError

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        return RunnerEnvelope(
            command="test acceptance",
            exit_code=0,
            logs_path="logs/acceptance.jsonl",
            prompt_path="prompts/acceptance.txt",
            last_message_path="last/acceptance.json",
            workdir="workdirs/acceptance",
            payload=AcceptanceResult(verdict="pass", reason="Accepted deliverables.").model_dump(),
        )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(tmp_path: Path, *, declared_requirements: list[dict] | None) -> tuple[Orchestrator, str, Path]:
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    parent_artifacts = tmp_path / "parent-artifacts"
    child_worktree = tmp_path / "wt-child"
    _write(target / CHECK_FILE, "IDLE\n")
    _write(parent_artifacts / "src/level.tscn", "SCENE")
    _write(child_worktree / "src/level.tscn", "SCENE")
    constitution_data: dict = {}
    if declared_requirements is not None:
        constitution_data["requirements"] = declared_requirements
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="rt-root",
                title="Root",
                description="Owns target.",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
                scope_json=dumps([str(target)]),
                constitution_json=dumps(constitution_data),
                constitution_version=2,
            )
        )
        db.add(
            TaskNode(
                id="rt-parent",
                parent_id="rt-root",
                title="Parent",
                description="",
                depth=1,
                status=TaskStatus.ACCEPTING.value,
                worktree_path=str(parent_artifacts),
                constitution_version=2,
                constitution_hash="h2",
                acceptance_json=dumps(["按需求矩阵验收"]),
            )
        )
        db.add(
            TaskNode(
                id="rt-child",
                parent_id="rt-parent",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(child_worktree),
                result_json=dumps({"worker": {"status": "succeeded", "summary": "done", "changed_files": ["src/level.tscn"]}}),
            )
        )
        db.commit()
    return orchestrator, "rt-parent", target


def _accept(orchestrator: Orchestrator, parent_id: str) -> None:
    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1)))


def _status() -> str:
    with SessionLocal() as db:
        return str(db.get(TaskNode, "rt-parent").status)


def _meta() -> dict:
    with SessionLocal() as db:
        return json.loads(db.get(TaskNode, "rt-parent").node_meta_json)


def _debts(kind: str) -> list[dict]:
    return [item for item in _meta().get("obligations") or [] if item.get("type") == kind]


def _messages(event_type: str) -> list[str]:
    with SessionLocal() as db:
        return [event.message for event in db.scalars(select(EventRecord)).all() if event.type == event_type]


def test_requirement_without_criteria_blocks_done(tmp_path: Path) -> None:
    """T1：没有判据的要求 = 漏需求 → 必须拦，且理由点名要求 id。"""
    orchestrator, parent_id, _target = _seed(
        tmp_path, declared_requirements=[{"id": "R1", "text": "击杀后回到待机", "criteria": []}]
    )

    _accept(orchestrator, parent_id)

    assert _status() == TaskStatus.REPAIR.value
    debt = _debts(obligations.REQUIREMENT_UNCOVERED)
    assert [item["payload"]["requirement_id"] for item in debt] == ["R1"]
    assert debt[0]["severity"] == obligations.SEVERITY_BLOCKING
    assert _meta()["requirement_matrix"]["uncovered"] == 1
    assert "R1" in _messages("parent.requirement_gap")[0]


def test_failing_criterion_blocks_done(tmp_path: Path) -> None:
    """T2：有判据但没过 = 没做到 → 必须拦，且点名要求 id + 判据 id。"""
    orchestrator, parent_id, _target = _seed(
        tmp_path,
        declared_requirements=[
            {
                "id": "R1",
                "text": "必须出现 READY 标记",
                "criteria": [{"id": "c1", "type": "contains", "path": CHECK_FILE, "pattern": "READY"}],
            }
        ],
    )

    _accept(orchestrator, parent_id)

    assert _status() == TaskStatus.REPAIR.value
    debt = _debts(obligations.REQUIREMENT_UNMET)
    assert debt[0]["payload"]["requirement_id"] == "R1"
    assert debt[0]["payload"]["criterion_id"] == "c1"
    assert debt[0]["severity"] == obligations.SEVERITY_BLOCKING


def test_met_criteria_allow_done_and_settle(tmp_path: Path) -> None:
    """T4：全部有判据且都过 → 可 `done`，矩阵显示 0 缺口。"""
    orchestrator, parent_id, _target = _seed(
        tmp_path,
        declared_requirements=[
            {
                "id": "R1",
                "text": "必须出现 IDLE 标记",
                "criteria": [{"id": "c1", "type": "contains", "path": CHECK_FILE, "pattern": "IDLE"}],
            }
        ],
    )

    _accept(orchestrator, parent_id)

    assert _status() == TaskStatus.DONE.value
    assert _meta()["requirement_matrix"]["unmet"] == 0
    assert _debts(obligations.REQUIREMENT_UNMET) == []


def test_unjudged_criterion_is_not_a_failure(tmp_path: Path) -> None:
    """T3：判据**判不了**（越界路径）→ 不阻断，但要留痕说清"没判定成"。"""
    orchestrator, parent_id, _target = _seed(
        tmp_path,
        declared_requirements=[
            {
                "id": "R1",
                "text": "越界路径判不了",
                "criteria": [{"id": "c1", "type": "file_exists", "path": "../outside.txt"}],
            }
        ],
    )

    _accept(orchestrator, parent_id)

    assert _status() == TaskStatus.DONE.value
    assert _debts(obligations.REQUIREMENT_UNMET) == []
    assert _meta()["requirement_matrix"]["unjudged"] == ["R1|c1"]
    assert _messages("parent.requirement_unjudged")


def test_undeclared_requirements_are_not_judged(tmp_path: Path) -> None:
    """T5：没声明 `requirements` → 不判（也不留矩阵），不许把"没声明"读成"已覆盖"。"""
    orchestrator, parent_id, _target = _seed(tmp_path, declared_requirements=None)

    _accept(orchestrator, parent_id)

    assert _status() == TaskStatus.DONE.value
    assert "requirement_matrix" not in _meta()
    assert _debts(obligations.REQUIREMENT_UNCOVERED) == []


def test_declaration_problems_are_reported_not_dropped(tmp_path: Path) -> None:
    """定义错的要求**必须报出来**——静默丢掉等于"这条需求从矩阵里消失"了。"""
    declared, problems = requirements.declared(
        {
            "requirements": [
                {"id": "R1", "text": "ok", "criteria": [{"id": "c1", "type": "contains", "path": CHECK_FILE, "pattern": "x"}]},
                {"id": "R2", "text": "", "criteria": []},  # 缺 text
                {"text": "缺 id", "criteria": []},
                {"id": "R3", "text": "判据类型不合法", "criteria": [{"id": "c1", "type": "regex"}]},
            ]
        }
    )
    assert [item["id"] for item in declared] == ["R1", "R3"]
    assert any("缺少 text" in item for item in problems)
    assert any("缺少 id" in item for item in problems)
    assert any("判据类型不合法" in item for item in problems)

    _write(tmp_path / CHECK_FILE, "IDLE\n")
    summary = requirements.evaluate(
        [{"id": "R1", "text": "t", "criteria": [{"id": "c1", "type": "file_exists", "path": CHECK_FILE}]}],
        tmp_path,
        python_executable=sys.executable,
    )
    assert summary["met"] and not summary["unmet"] and not summary["unjudged"]
