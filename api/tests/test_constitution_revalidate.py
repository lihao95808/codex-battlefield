"""A5 回归测试：宪法修订后的**再验证**与版本债清债。

要证明的四件事：
1. **再验证真的用新宪法判**——verifier 的 prompt 里嵌的是本节点的快照，所以再验证前必须换快照；
   不换就是拿旧判据复查，白跑一次（`test_revalidation_runs_the_verifier_against_the_new_constitution`）。
2. **通过即追平**——再验证通过后版本戳追平，债务由 `version_catchup` 清掉，父节点得以完成（K3）。
3. **不通过就走既有修复通路**——不是门控自己发明子任务（§6.4 硬约束 1）。
4. **清债有界且可判**——判据是"节点当前记着的版本不再落后"，与谁完成了什么无关。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest
from sqlalchemy import select

from app import obligations
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, ConstitutionRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import BaseCodexRunner, RunnerEnvelope
from app.schemas import AcceptanceResult, VerifierResult, WorkerResult
from app.utils import dumps


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


MARKDOWN_V2 = "# 项目宪法 v2\n\n收紧：叶子必须给出可复现命令。\n"


class RevalidationRunner(BaseCodexRunner):
    """记录 verifier 看到的宪法快照，并按要求给出 verdict。"""

    def __init__(self, verdict: str = "pass") -> None:
        self.verdict = verdict
        self.verify_constitutions: list[dict] = []
        self.accept_count = 0

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def verify(self, task: TaskNode, worker_result) -> RunnerEnvelope:
        self.verify_constitutions.append(json.loads(task.constitution_json or "{}"))
        return RunnerEnvelope(
            command="test verify",
            exit_code=0,
            logs_path="logs/verify.jsonl",
            prompt_path="prompts/verify.txt",
            last_message_path="last/verify.json",
            workdir="workdirs/verify",
            payload=VerifierResult(verdict=self.verdict, reason=f"revalidation:{self.verdict}").model_dump(),
        )

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        self.accept_count += 1
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


async def _seed(tmp_path: Path, *, revalidate_enabled: bool = True):
    """根 → 父节点 → 已完成叶子；叶子留有成功的 worker 产物所以可再验证。"""
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=get_settings().workspace_dir,
        constitution_revalidate_enabled=revalidate_enabled,
    )
    orchestrator = Orchestrator(settings)
    leaf_worktree = tmp_path / "leaf-worktree"
    _write(leaf_worktree / "docs" / "note.md", "artifact")
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="rv-root",
                title="Root",
                description="Owns everything.",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
                scope_json=dumps([str(tmp_path / "target")]),
            )
        )
        db.add(
            TaskNode(
                id="rv-parent",
                parent_id="rv-root",
                title="Parent",
                description="Integrate leaf.",
                depth=1,
                status=TaskStatus.ACCEPTING.value,
            )
        )
        db.add(
            TaskNode(
                id="rv-leaf",
                parent_id="rv-parent",
                title="Leaf",
                description="Deliver the artifact.",
                depth=2,
                status=TaskStatus.DONE.value,
                acceptance_json=dumps(["必须给出可复现命令。"]),
                worktree_path=str(leaf_worktree),
                result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": []}}),
            )
        )
        db.commit()

    constitution_id = await orchestrator.draft_constitution("再验证探针。", runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    # 把 v1 绑到 rv-root 上，三个节点也都在 v1（等价于这棵树原本就建在这个宪法上）
    with SessionLocal() as db:
        record = db.get(ConstitutionRecord, constitution_id)
        assert record is not None
        record.root_id = "rv-root"
        for node_id in ("rv-root", "rv-parent", "rv-leaf"):
            node = db.get(TaskNode, node_id)
            assert node is not None
            node.constitution_id = constitution_id
            node.constitution_hash = "hash-v1"
            node.constitution_json = dumps({"global_acceptance": ["原始下限"]})
            node.constitution_version = 1
        db.commit()
    return orchestrator, constitution_id


def _node(node_id: str) -> TaskNode:
    with SessionLocal() as db:
        node = db.get(TaskNode, node_id)
        assert node is not None
        db.expunge(node)
        return node


def _parent_obligations(node_id: str = "rv-parent") -> list[dict]:
    return json.loads(_node(node_id).node_meta_json).get("obligations") or []


# --- 1 & 2：再验证用新宪法、通过即追平 --------------------------------------


@pytest.mark.asyncio
async def test_revalidation_runs_the_verifier_against_the_new_constitution(tmp_path: Path) -> None:
    orchestrator, _ = await _seed(tmp_path)
    amended = orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )
    assert amended["to_version"] == 2
    # 收紧方向上 done 节点**不给版本戳**（拿新版本号给旧产物盖章就是静默 claim 通过）
    assert _node("rv-leaf").constitution_version == 1
    assert {item["type"] for item in _parent_obligations()} == {
        obligations.CONSTITUTION_STALE,
        obligations.REVALIDATE_REQUIRED,
    }

    runner = RevalidationRunner(verdict="pass")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    # 再验证时 verifier 必须看到**新**宪法，否则这次复查判的是旧判据，等于白跑
    assert runner.verify_constitutions == [{"global_acceptance": ["收紧后的下限"]}]
    leaf = _node("rv-leaf")
    assert leaf.status == TaskStatus.DONE.value
    assert leaf.constitution_version == 2
    assert leaf.constitution_hash == amended["content_hash"]
    # 通过即追平 → 债务被 version_catchup 清掉 → 父节点得以完成
    debt = _parent_obligations()
    assert [item["settled_by"] for item in debt] == [obligations.SETTLED_BY_VERSION_CATCHUP] * 2
    assert obligations.summarize(debt)["blocking_total"] == 0
    assert _node("rv-parent").status == TaskStatus.DONE.value


@pytest.mark.asyncio
async def test_parent_is_not_completed_while_a_stale_leaf_stays_behind(tmp_path: Path) -> None:
    """没有可再验证的通路时（叶子没有 worker 产物），版本债必须真的拦下父节点。"""
    orchestrator, _ = await _seed(tmp_path)
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        # 去掉 worker 产物 → 无从再验证，只剩"人工"这条逃生舱
        leaf.result_json = dumps({"verifier": {"verdict": "pass", "reason": "old"}})
        db.commit()

    runner = RevalidationRunner(verdict="pass")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    assert runner.verify_constitutions == []
    assert _node("rv-parent").status == TaskStatus.REPAIR.value
    assert _node("rv-leaf").constitution_version == 1
    assert obligations.has_blocking(_parent_obligations()) is True


# --- 3：不通过走既有修复通路 ------------------------------------------------


@pytest.mark.asyncio
async def test_failed_revalidation_goes_through_the_existing_repair_path(tmp_path: Path) -> None:
    orchestrator, _ = await _seed(tmp_path)
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )

    runner = RevalidationRunner(verdict="repair")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    leaf = _node("rv-leaf")
    # 修复子任务来自 verifier 的 verdict，而不是门控自己发明（§6.4 硬约束 1）
    assert leaf.status != TaskStatus.DONE.value
    assert leaf.constitution_version == 1  # 没有通过，版本戳不许动
    with SessionLocal() as db:
        repair_children = list(db.scalars(select(TaskNode).where(TaskNode.parent_id == "rv-leaf")))
    assert repair_children, "verdict=repair 必须产出修复子任务，否则树没有前进的路"
    assert _node("rv-parent").status != TaskStatus.DONE.value


# --- 4：开关关掉时版本债不参与门控 ------------------------------------------


@pytest.mark.asyncio
async def test_disabling_revalidation_saves_tokens_but_never_opens_the_gate_silently(tmp_path: Path) -> None:
    orchestrator, _ = await _seed(tmp_path, revalidate_enabled=False)
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )

    runner = RevalidationRunner(verdict="pass")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    assert runner.verify_constitutions == []  # 不动 token
    # 关掉再验证**不得**变成静默放行：债照旧阻断，父节点完成不了，最后由升级通路交给人。
    # 严重度是债务的属性（收紧＝blocking），不是门控开关的属性——否则看板会"报阻断但树照跑"。
    assert _node("rv-parent").status != TaskStatus.DONE.value
    assert obligations.has_blocking(_parent_obligations()) is True
    assert _node("rv-leaf").constitution_version == 1


# --- 执行中节点跑完后的入队（A5 遗留） ------------------------------------


def _verifier_envelope(reason: str = "revalidated") -> RunnerEnvelope:
    return RunnerEnvelope(
        command="test verify",
        exit_code=0,
        logs_path="logs/verify.jsonl",
        prompt_path="prompts/verify.txt",
        last_message_path="last/verify.json",
        workdir="workdirs/verify",
        payload=VerifierResult(verdict="pass", reason=reason).model_dump(),
    )


def _worker_result() -> WorkerResult:
    return WorkerResult(status="succeeded", summary="s", changed_files=[])


def _finish_leaf(orchestrator: Orchestrator) -> None:
    """走**真实**的完成通路（verifier 结论），而不是直接改状态。"""
    orchestrator._record_verifier_result("rv-leaf", _worker_result(), _verifier_envelope(), VerifierResult(verdict="pass", reason="ok"))


@pytest.mark.asyncio
async def test_in_flight_node_finishing_after_a_tighten_is_enqueued_for_revalidation(tmp_path: Path) -> None:
    """修订当场只登记；节点跑完时把登记兑现成再验证债（否则登记就是死的）。"""
    orchestrator, _ = await _seed(tmp_path)
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        leaf.status = TaskStatus.WORKING.value
        db.commit()
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )
    assert json.loads(_node("rv-leaf").node_meta_json)["revalidate_after_run"]["version"] == 2

    _finish_leaf(orchestrator)

    leaf = _node("rv-leaf")
    assert "revalidate_after_run" not in json.loads(leaf.node_meta_json)  # 登记已兑现
    assert leaf.status == TaskStatus.DONE.value
    types = {item["type"] for item in _parent_obligations()}
    assert types == {obligations.CONSTITUTION_STALE, obligations.REVALIDATE_REQUIRED}


@pytest.mark.asyncio
async def test_enqueued_node_is_actually_picked_up_by_the_revalidation_pass(tmp_path: Path) -> None:
    """登记的下一步必须真的有人做：跑完 → 入队 → 父节点验收前被重跑 verifier → 追平清债。"""
    orchestrator, _ = await _seed(tmp_path)
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        leaf.status = TaskStatus.WORKING.value
        db.commit()
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )
    _finish_leaf(orchestrator)
    assert obligations.has_blocking(_parent_obligations()) is True

    runner = RevalidationRunner(verdict="pass")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    assert runner.verify_constitutions == [{"global_acceptance": ["收紧后的下限"]}]
    leaf = _node("rv-leaf")
    assert leaf.constitution_version == 2
    assert [item["settled_by"] for item in _parent_obligations()] == [
        obligations.SETTLED_BY_VERSION_CATCHUP
    ] * len(_parent_obligations())
    assert _node("rv-parent").status == TaskStatus.DONE.value


@pytest.mark.asyncio
async def test_in_flight_node_finishing_after_a_loosen_is_stamped_not_revalidated(tmp_path: Path) -> None:
    """放宽下产物仍合规：只把版本戳追平，不复查、不产生债。"""
    orchestrator, _ = await _seed(tmp_path)
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        leaf.status = TaskStatus.WORKING.value
        db.commit()
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["放宽后的下限"]}, direction="loosen", actor="operator"
    )

    _finish_leaf(orchestrator)

    leaf = _node("rv-leaf")
    assert "revalidate_after_run" not in json.loads(leaf.node_meta_json)
    assert leaf.constitution_version == 2
    assert obligations.blocking(_parent_obligations()) == []


@pytest.mark.asyncio
async def test_a_node_that_already_caught_up_is_not_enqueued(tmp_path: Path) -> None:
    """登记期间它自己追平了（例如中途重跑过一次），就不该再挂一条债。"""
    orchestrator, _ = await _seed(tmp_path)
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        leaf.status = TaskStatus.WORKING.value
        db.commit()
    orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["收紧后的下限"]}, direction="tighten", actor="operator"
    )
    with SessionLocal() as db:
        leaf = db.get(TaskNode, "rv-leaf")
        assert leaf is not None
        v2 = orchestrator._constitution_head_record(db, leaf.constitution_id)
        assert v2 is not None and v2.version == 2
        # 真的追平 = 它已经在**新版快照**下重新判过一次，而不是只把版本号改大
        orchestrator._replace_constitution_snapshot(leaf, v2)
        db.commit()

    _finish_leaf(orchestrator)

    assert "revalidate_after_run" not in json.loads(_node("rv-leaf").node_meta_json)
    unsettled_version_debt = [
        item
        for item in _parent_obligations()
        if item["type"] in obligations.VERSION_SCOPED_TYPES and not item["settled_at"]
    ]
    assert unsettled_version_debt == []


# --- 放宽不触发再验证 ------------------------------------------------------


@pytest.mark.asyncio
async def test_loosening_amendment_does_not_trigger_revalidation(tmp_path: Path) -> None:
    orchestrator, _ = await _seed(tmp_path)
    summary = orchestrator.amend_constitution(
        "rv-root", markdown=MARKDOWN_V2, data={"global_acceptance": ["放宽后的下限"]}, direction="loosen", actor="operator"
    )
    # 放宽把 `done` 叶子直接盖章追平，也就没有任何节点需要排队再验证
    assert summary["stamped_count"] == 1
    assert summary["revalidate_queued_count"] == 1  # 只有"执行中"的父节点被登记（它正在验收）
    runner = RevalidationRunner(verdict="pass")
    await orchestrator._accept_parent("rv-parent", runner, asyncio.Semaphore(1))

    assert runner.verify_constitutions == []
    assert _node("rv-leaf").constitution_version == 2  # 放宽只更新版本戳
    assert obligations.blocking(_parent_obligations()) == []
    assert _node("rv-parent").status == TaskStatus.DONE.value
