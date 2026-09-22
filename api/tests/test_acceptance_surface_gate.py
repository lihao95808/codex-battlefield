"""验收面冻结与防篡改的接线测试（`docs/spec-verification-independence.md` §3，判据 V1–V4）。

**为什么这些用例必须真跑一遍验收**：这条判据的全部价值在于"产出方改了自己正要通过的考卷时，
父节点不能 `done`"。只测"函数算得对"是测不出这件事的——接线晚了、门禁读不到、或者债务严重度
被档位带跑，任何一处都会让它在真实链路里变成装饰品。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import obligations, verification_surface
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import BaseCodexRunner, RunnerEnvelope
from app.schemas import AcceptanceResult
from app.utils import dumps

SURFACE = "tests/acceptance_check.py"
SURFACE_V1 = "assert result == 1\n"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


class AcceptancePassOnlyRunner(BaseCodexRunner):
    """验收一律 pass：要拦住的正是"agent 说通过，但它改过判据"这种情况。"""

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


def _seed(tmp_path: Path, *, frozen: bool = True) -> tuple[Orchestrator, str, Path]:
    """根（持有目标目录与冻结基线）+ 父（在工作树里整合）+ 一个已完成子节点。"""
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    parent_artifacts = tmp_path / "parent-artifacts"
    child_worktree = tmp_path / "wt-child"
    _write(target / SURFACE, SURFACE_V1)
    _write(target / "docs/readme.md", "README")
    _write(parent_artifacts / "src/level.tscn", "SCENE")
    _write(child_worktree / "src/level.tscn", "SCENE")

    root_meta: dict = {}
    if frozen:
        root_meta[verification_surface.SURFACE_KEY] = verification_surface.freeze(
            target, [SURFACE], version=2, frozen_by="constitution_lock", now_iso="2026-09-22T00:00:00+00:00"
        )
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="as-root",
                title="Root",
                description="Owns target.",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
                scope_json=dumps([str(target)]),
                node_meta_json=dumps(root_meta),
            )
        )
        db.add(
            TaskNode(
                id="as-parent",
                parent_id="as-root",
                title="Parent",
                description="Integrate children.",
                depth=1,
                status=TaskStatus.ACCEPTING.value,
                worktree_path=str(parent_artifacts),
                constitution_version=2,
                constitution_hash="h2",
                acceptance_json=dumps(["判据文件不许被改"]),
            )
        )
        db.add(
            TaskNode(
                id="as-child",
                parent_id="as-parent",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(child_worktree),
                result_json=dumps({"worker": {"status": "succeeded", "summary": "done", "changed_files": ["src/level.tscn"]}}),
            )
        )
        db.commit()
    return orchestrator, "as-parent", target


def _parent_status() -> str:
    with SessionLocal() as db:
        return str(db.get(TaskNode, "as-parent").status)


def _parent_obligations() -> list[dict]:
    with SessionLocal() as db:
        meta = json.loads(db.get(TaskNode, "as-parent").node_meta_json)
    return [item for item in meta.get("obligations") or [] if item.get("type") == obligations.ACCEPTANCE_SURFACE_TAMPERED]


def _event_types() -> list[str]:
    with SessionLocal() as db:
        return [event.type for event in db.scalars(select(EventRecord)).all()]


def _event_messages(event_type: str) -> list[str]:
    with SessionLocal() as db:
        return [
            event.message
            for event in db.scalars(select(EventRecord)).all()
            if event.type == event_type
        ]


def _event_payload(event_type: str) -> dict:
    with SessionLocal() as db:
        events = [event for event in db.scalars(select(EventRecord)).all() if event.type == event_type]
    assert len(events) == 1, f"期望恰好一条 {event_type}，实际 {len(events)}"
    return json.loads(events[0].payload_json)


def _parent_meta() -> dict:
    with SessionLocal() as db:
        return json.loads(db.get(TaskNode, "as-parent").node_meta_json)


def test_tampering_with_the_declared_surface_blocks_done(tmp_path: Path) -> None:
    """V1：改判据文件 → 父节点不得 `done`，理由**点名路径**（连严重度都不接受档位）。"""
    orchestrator, parent_id, target = _seed(tmp_path)
    _write(target / SURFACE, "assert result == 1  # 我把它改成永远通过了\n")

    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1)))

    assert _parent_status() == TaskStatus.REPAIR.value
    debt = _parent_obligations()
    assert [item["payload"]["kind"] for item in debt] == ["changed"]
    assert debt[0]["payload"]["path"] == SURFACE
    assert debt[0]["severity"] == obligations.SEVERITY_BLOCKING
    assert obligations.has_blocking(_parent_meta()["obligations"]) is True
    messages = _event_messages("parent.acceptance_surface_tampered")
    assert messages and SURFACE in messages[0]  # 理由必须点名路径，否则没有可操作性
    assert "parent.obligation_gate_blocked" in _event_types()


def test_deleting_the_surface_file_also_blocks(tmp_path: Path) -> None:
    orchestrator, parent_id, target = _seed(tmp_path)
    (target / SURFACE).unlink()

    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1)))

    assert _parent_status() == TaskStatus.REPAIR.value
    assert [item["payload"]["kind"] for item in _parent_obligations()] == ["missing"]


def test_unreadable_surface_is_unjudged_not_tampering(tmp_path: Path, monkeypatch) -> None:
    """V2：读不了 ≠ 改了。一次瞬时占用不该把父节点打进修复，但必须留下"未判定"的记录。"""
    orchestrator, parent_id, target = _seed(tmp_path)
    real = verification_surface.path_digest

    def flaky(path):
        if Path(path).name == Path(SURFACE).name:
            raise PermissionError("被占用")
        return real(path)

    monkeypatch.setattr(verification_surface, "path_digest", flaky)

    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1)))

    assert _parent_status() == TaskStatus.DONE.value
    assert _parent_obligations() == []
    with SessionLocal() as db:
        meta = json.loads(db.get(TaskNode, "as-parent").node_meta_json)
    assert meta["acceptance_surface_unreadable"] == [SURFACE]
    assert _event_messages("parent.acceptance_surface_unjudged")


def test_undeclared_surface_is_not_judged(tmp_path: Path) -> None:
    """V3：没声明验收面 → 不判（但也不许把"没声明"读成"已通过"：看板/事件里要能看到未声明）。"""
    orchestrator, parent_id, _target = _seed(tmp_path, frozen=False)

    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1)))

    assert _parent_status() == TaskStatus.DONE.value
    assert _parent_obligations() == []


def test_restoring_the_surface_settles_the_debt(tmp_path: Path) -> None:
    """V4：面又和冻结值对上 → 按**事实**清债，节点可 `done`。"""
    orchestrator, parent_id, target = _seed(tmp_path)
    _write(target / SURFACE, "被改过了\n")
    semaphore = asyncio.Semaphore(1)
    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore))
    assert _parent_status() == TaskStatus.REPAIR.value

    _write(target / SURFACE, SURFACE_V1)
    asyncio.run(orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore))

    assert _parent_status() == TaskStatus.DONE.value
    debt = _parent_obligations()
    assert debt[0]["settled_at"]
    assert debt[0]["settled_by"] == obligations.SETTLED_BY_REPAIR_CHILD


def test_freeze_records_digests_and_reports_invalid_declarations(tmp_path: Path) -> None:
    """冻结：写进根节点 meta + 事件；越界/绝对路径**丢弃并报出来**，绝不静默忽略。"""
    orchestrator, _parent_id, target = _seed(tmp_path, frozen=False)
    with SessionLocal() as db:
        root = db.get(TaskNode, "as-root")
        frozen = orchestrator._freeze_acceptance_surface(
            db,
            root,
            data={"acceptance_surface": [SURFACE, "/abs/path.py", "../escape.py", SURFACE]},
            version=3,
            frozen_by="amend:human",
        )
        db.commit()
        meta = json.loads(root.node_meta_json)
    assert frozen is not None
    assert list(meta[verification_surface.SURFACE_KEY]["digests"]) == [SURFACE]  # 去重 + 只留合法项
    assert meta[verification_surface.SURFACE_KEY]["frozen_by"] == "amend:human"
    assert meta[verification_surface.SURFACE_KEY]["version"] == 3
    payload = _event_payload("constitution.acceptance_surface_frozen")
    assert payload["invalid"] == ["/abs/path.py", "../escape.py"]
    assert (target / SURFACE).exists()


def test_declared_list_change_is_what_allows_refreezing() -> None:
    """只有**声明的路径清单**变了才允许重新冻结：内容变了要走去篡改门禁。"""
    frozen = {"digests": {SURFACE: "sha256:x"}}
    assert verification_surface.declared_changed(frozen, [SURFACE]) is False
    assert verification_surface.declared_changed(frozen, [SURFACE, "tests/other.py"]) is True
    assert verification_surface.declared_changed(frozen, []) is True
    assert verification_surface.declared_changed(None, [SURFACE]) is True
