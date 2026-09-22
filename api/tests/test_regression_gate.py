"""R5 跨版本回归门禁：验收通过时记产物指纹，**跨版本**再验收时比对，回退即登记债务。

**这个文件的存在理由**：回归门禁最容易被写成"拦住自己的修复"——新版本下的返工本来就要改文件，
如果拿"字节变了"当回退，门禁在真实长跑里会立刻卡死正常进度。所以这里钉住的不是"能拦住"，
而是**两侧都钉住**：

| 编号 | 要钉住的事 |
|---|---|
| G5 | 人为破坏（删除）上一版验收过的产物 → 父节点**不得** done，且理由**定位到具体路径** |
| 不拦返工 | 同一版本内的改动**不比**（基线每次验收通过都重记），`changed` 在默认档只上浮不阻断 |
| 有界 | 产物恢复后债被 `repair_child` 清掉，节点能正常 done（不会永远卡着） |
| 档位 | `repair` 档下 `changed` 也阻断；`record` 档下不阻断 |
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

# 测试库路径由 tests/conftest.py 按进程号分配。
import pytest
from sqlalchemy import select

from app import obligations, regression
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import BaseCodexRunner, RunnerEnvelope
from app.schemas import AcceptanceResult
from app.utils import dumps

LEVEL = "src/level.tscn"
LEGACY = "src/legacy.gd"
LEVEL_CONTENT = "SCENE_V1"
LEGACY_CONTENT = "LEGACY_V1"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


class AcceptancePassOnlyRunner(BaseCodexRunner):
    """验收一律 pass：门禁要拦住的正是"agent 说 pass，但上一版产物已经坏了"这种情况。"""

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


def _seed(
    tmp_path: Path,
    *,
    strategy: str = regression.STRATEGY_RECORD,
    parent_version: int = 2,
    parent_hash: str = "h2",
    baseline_version: int = 1,
    baseline_hash: str = "h1",
) -> tuple[Orchestrator, str]:
    """根 + 父（有自己的工作树，基线产物就在里面）+ 一个成功子节点。

    基线里的两个产物：
    - `src/level.tscn`：子节点也产出它（内容相同）→ 集成后仍然一致，用作"没坏"的对照；
    - `src/legacy.gd`：没有任何子节点产出它 → 用例可以**删掉或改坏**它来制造回退。
    """
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
        integration_regression_strategy=strategy,
    )
    orchestrator = Orchestrator(settings)
    target = tmp_path / "target"
    parent_artifacts = tmp_path / "parent-artifacts"
    child_worktree = tmp_path / "wt-child"
    _write(parent_artifacts / LEVEL, LEVEL_CONTENT)
    _write(parent_artifacts / LEGACY, LEGACY_CONTENT)
    _write(child_worktree / LEVEL, LEVEL_CONTENT)

    baseline = regression.build_baseline(
        constitution_version=baseline_version,
        constitution_hash=baseline_hash,
        artifacts=regression.snapshot_artifacts(parent_artifacts, [LEVEL, LEGACY]),
        assertions=["能跑起来"],
        root=str(parent_artifacts),
    )
    assert len(baseline["artifacts"]) == 2

    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="rg-root",
                title="Root",
                description="Owns target.",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
                scope_json=dumps([str(target)]),
            )
        )
        db.add(
            TaskNode(
                id="rg-parent",
                parent_id="rg-root",
                title="Parent",
                description="Integrate children.",
                depth=1,
                status=TaskStatus.ACCEPTING.value,
                worktree_path=str(parent_artifacts),
                constitution_version=parent_version,
                constitution_hash=parent_hash,
                acceptance_json=dumps(["能跑起来"]),
                node_meta_json=dumps({regression.BASELINE_KEY: baseline}),
            )
        )
        db.add(
            TaskNode(
                id="rg-child",
                parent_id="rg-parent",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(child_worktree),
                result_json=dumps({"worker": {"status": "succeeded", "summary": "done", "changed_files": [LEVEL]}}),
            )
        )
        db.commit()
    return orchestrator, "rg-parent"


def _parent() -> TaskNode:
    with SessionLocal() as db:
        task = db.get(TaskNode, "rg-parent")
        assert task is not None
        db.expunge(task)
        return task


def _meta() -> dict:
    return json.loads(_parent().node_meta_json)


def _regression_obligations() -> list[dict]:
    return [item for item in _meta().get("obligations") or [] if item.get("type") == obligations.REGRESSION_BREAK]


def _event_types() -> list[str]:
    with SessionLocal() as db:
        return [event.type for event in db.scalars(select(EventRecord)).all()]


def _event_messages(event_type: str) -> list[str]:
    with SessionLocal() as db:
        return [event.message for event in db.scalars(select(EventRecord)).all() if event.type == event_type]


# --- G5：破坏产物必须挡住，并且定位到路径 ---------------------------------


@pytest.mark.asyncio
async def test_missing_artifact_blocks_done_and_names_the_path(tmp_path: Path) -> None:
    orchestrator, parent_id = _seed(tmp_path)
    (tmp_path / "parent-artifacts" / LEGACY).unlink()

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    parent = _parent()
    assert parent.status == TaskStatus.REPAIR.value
    assert parent.status != TaskStatus.DONE.value
    debt = _regression_obligations()
    assert [item["payload"]["kind"] for item in debt] == ["missing"]
    assert debt[0]["payload"]["path"] == LEGACY
    assert debt[0]["severity"] == obligations.SEVERITY_BLOCKING
    assert obligations.has_blocking(_meta()["obligations"]) is True
    assert "parent.obligation_gate_blocked" in _event_types()
    # G5 要求"给出 diff 定位"：事件里的理由必须点名那个文件
    messages = _event_messages("parent.regression_break")
    assert messages and LEGACY in messages[0]
    assert (tmp_path / "parent-artifacts" / LEVEL).is_file()  # 没坏的产物不动


@pytest.mark.asyncio
async def test_restoring_the_artifact_settles_and_allows_done(tmp_path: Path) -> None:
    """有界：产物恢复后债被清掉，节点能正常 done——门禁不能变成永远清不掉的东西。"""
    orchestrator, parent_id = _seed(tmp_path)
    artifact = tmp_path / "parent-artifacts" / LEGACY
    artifact.unlink()
    semaphore = asyncio.Semaphore(1)
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)
    assert _parent().status == TaskStatus.REPAIR.value
    assert _regression_obligations()[0]["settled_at"] is None

    artifact.write_text(LEGACY_CONTENT, encoding="utf-8")
    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), semaphore)

    parent = _parent()
    assert parent.status == TaskStatus.DONE.value
    debt = _regression_obligations()
    assert debt[0]["settled_by"] == obligations.SETTLED_BY_REPAIR_CHILD
    assert debt[0]["settled_at"]
    # 通过后基线被重记到当前版本：下一版比对的是"刚刚验收过的状态"
    baseline = _meta()[regression.BASELINE_KEY]
    assert baseline["constitution_version"] == 2
    assert sorted(baseline["artifacts"]) == sorted([LEVEL, LEGACY])


# --- 不拦返工：默认档的 changed 只上浮，同版本不比 ------------------------


@pytest.mark.asyncio
async def test_changed_artifact_is_advisory_in_record_strategy(tmp_path: Path) -> None:
    """字节变化可能是修复动作本身——默认档拦 done 等于把默认行为悄悄改成阻断。"""
    orchestrator, parent_id = _seed(tmp_path, strategy=regression.STRATEGY_RECORD)
    (tmp_path / "parent-artifacts" / LEGACY).write_text("REWORKED", encoding="utf-8")

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    assert _parent().status == TaskStatus.DONE.value
    debt = _regression_obligations()
    assert [item["payload"]["kind"] for item in debt] == ["changed"]
    assert debt[0]["severity"] == obligations.SEVERITY_ADVISORY
    assert obligations.has_blocking(_meta()["obligations"]) is False


@pytest.mark.asyncio
async def test_changed_artifact_blocks_in_repair_strategy(tmp_path: Path) -> None:
    orchestrator, parent_id = _seed(tmp_path, strategy=regression.STRATEGY_REPAIR)
    (tmp_path / "parent-artifacts" / LEGACY).write_text("REWORKED", encoding="utf-8")

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    assert _parent().status == TaskStatus.REPAIR.value
    assert _regression_obligations()[0]["severity"] == obligations.SEVERITY_BLOCKING


@pytest.mark.asyncio
async def test_same_version_does_not_compare(tmp_path: Path) -> None:
    """同一版内的改动不比：否则门禁第一个拦下的就是它自己的修复子任务。"""
    orchestrator, parent_id = _seed(tmp_path, parent_version=1, parent_hash="h1")
    (tmp_path / "parent-artifacts" / LEGACY).write_text("REWORKED", encoding="utf-8")

    await orchestrator._accept_parent(parent_id, AcceptancePassOnlyRunner(), asyncio.Semaphore(1))

    assert _parent().status == TaskStatus.DONE.value
    assert _regression_obligations() == []


# --- 纯模块 ---------------------------------------------------------------


def test_snapshot_only_records_existing_artifacts(tmp_path: Path) -> None:
    """基线只记当时真的读到的产物：把"当时就不在"的记进去，之后每一轮都会报同一条假回退。"""
    _write(tmp_path / "a.txt", "A")
    snapshot = regression.snapshot_artifacts(tmp_path, ["a.txt", "gone.txt"])
    assert sorted(snapshot) == ["a.txt"]
    assert snapshot["a.txt"].startswith(regression.SHA_PREFIX)


def test_normalize_artifact_paths_drops_escapes_and_absolute(tmp_path: Path) -> None:
    paths = regression.normalize_artifact_paths(
        tmp_path,
        ["src\\a.gd", "/abs/b.gd", "../evil.gd", "", "src/a.gd"],
    )
    assert paths == ["src/a.gd"]


def test_version_advanced_rules() -> None:
    baseline = regression.build_baseline(
        constitution_version=1,
        constitution_hash="h1",
        artifacts={"a.gd": "sha256:x"},
        assertions=[],
    )
    assert regression.version_advanced(baseline, current_version=1, current_hash="h1") is False
    assert regression.version_advanced(baseline, current_version=2, current_hash="h1") is True
    # 同版本、hash 变了 → **不比**。README 写着"同一版内的改动不比"，而真正的跨版本
    # （`_replace_constitution_snapshot`）总是同改 version+hash；拿"版本没动、hash 动了"当跨版本，
    # 拦下的正是同一轮里自己的返工。
    assert regression.version_advanced(baseline, current_version=1, current_hash="h2") is False
    # 版本回退也不比：那份基线属于更晚的宪法，拿它比会把回退动作本身报成产物回退
    assert regression.version_advanced(baseline, current_version=0, current_hash="h1") is False
    # 版本号缺失时才退回 hash 判据——那时没有别的信号可用
    assert regression.version_advanced(baseline, current_version=None, current_hash="h2") is True
    assert regression.version_advanced(baseline, current_version=None, current_hash="h1") is False
    # 没有产物的基线不算基线：不该拿空基线去说"有回退"
    assert regression.version_advanced(
        {"constitution_version": 1, "artifacts": {}}, current_version=3, current_hash="h3"
    ) is False


def test_snapshot_records_directories_too(tmp_path: Path) -> None:
    """目录也是产物（`changed_files` 允许声明目录）：只认文件时"整目录被删掉"不算回退。"""
    _write(tmp_path / "assets" / "x.png", "X")
    snapshot = regression.snapshot_artifacts(tmp_path, ["assets"])
    assert list(snapshot) == ["assets"]
    assert snapshot["assets"].startswith("tree:")

    baseline = regression.build_baseline(constitution_version=1, constitution_hash="h1", artifacts=snapshot, assertions=[])
    assert regression.diff_baseline(baseline, regression.snapshot_artifacts(tmp_path, ["assets"]))["missing"] == []
    (tmp_path / "assets" / "x.png").write_text("Y", encoding="utf-8")
    changed = regression.diff_baseline(baseline, regression.snapshot_artifacts(tmp_path, ["assets"]))
    assert [item["path"] for item in changed["changed"]] == ["assets"]


def test_unreadable_artifact_is_not_reported_as_missing(tmp_path: Path, monkeypatch) -> None:
    """读盘失败 ≠ 产物没了。

    `missing` 是**无条件 blocking**，而"文件被另一个进程占着"（Windows 上很常见）是瞬时状态；
    把两者混同，一次占用就能把父节点打进修复，理由还写着"已缺失"。
    """
    _write(tmp_path / "a.gd", "A")
    baseline = regression.build_baseline(
        constitution_version=1,
        constitution_hash="h1",
        artifacts={"a.gd": "sha256:old"},
        assertions=[],
    )
    real = regression.path_digest

    def flaky(path):
        if Path(path).name == "a.gd":
            raise PermissionError("被占用")
        return real(path)

    monkeypatch.setattr(regression, "path_digest", flaky)
    unreadable: list[str] = []
    current = regression.snapshot_artifacts(tmp_path, ["a.gd"], unreadable)
    assert unreadable == ["a.gd"] and current == {}

    diff = regression.diff_baseline(baseline, current, unreadable)
    assert diff["missing"] == []
    assert diff["changed"] == []
    assert diff["unreadable"] == ["a.gd"]
    assert diff["checked"] == 0
    # 理由里也要说清"有 X 处没判定"，别让人以为全都比过了
    assert "未判定" in regression.diff_reason(diff, current_version=2)


def test_settled_regression_obligation_revives_when_the_baseline_moves() -> None:
    """第二次坏掉必须重新阻断。

    幂等键绑在路径上，而"清掉的不复活"对**版本域**的回归债是错的：第一次修好之后它已清，
    再坏一次（基线又前进了一版）就再也拦不住——README 写着 `missing` 一律阻断，实际却是静默放行。
    """
    settled = [
        {
            "id": "regression_break:n1:missing|a.gd",
            "type": obligations.REGRESSION_BREAK,
            "severity": obligations.SEVERITY_BLOCKING,
            "settled_at": "2026-09-20T00:00:00+00:00",
            "settled_by": obligations.SETTLED_BY_REPAIR_CHILD,
            "recurrence_count": 0,
            "payload": {"kind": "missing", "path": "a.gd", "baseline_version": 1},
        }
    ]
    same_version = obligations.obligations_from_regression(
        node_id="n1",
        diff={"missing": [{"path": "a.gd", "was": "sha256:x", "now": "missing"}], "changed": [], "intact": [], "base_version": 1},
        current_version=2,
        strategy="record",
    )
    still_settled = obligations.upsert([dict(item) for item in settled], same_version)
    assert still_settled[0]["settled_at"]  # 基线没动 → 不复活（否则会被反复清、反复报）
    assert still_settled[0]["recurrence_count"] == 1

    next_version = obligations.obligations_from_regression(
        node_id="n1",
        diff={"missing": [{"path": "a.gd", "was": "sha256:x", "now": "missing"}], "changed": [], "intact": [], "base_version": 2},
        current_version=3,
        strategy="record",
    )
    revived = obligations.upsert([dict(item) for item in settled], next_version)
    assert revived[0].get("settled_at") is None
    assert revived[0].get("settled_by") is None
    assert revived[0]["recurrence_count"] == 1
    assert obligations.has_blocking(revived) is True


def test_severity_escalates_when_the_strategy_tightens() -> None:
    """档位从 `record` 切到 `repair`，已登记的 `changed` 债必须跟着变阻断——否则"看板报阻断、树照跑"。"""
    settled = [
        {
            "id": "regression_break:n1:changed|a.gd",
            "type": obligations.REGRESSION_BREAK,
            "severity": obligations.SEVERITY_ADVISORY,
            "payload": {"kind": "changed", "path": "a.gd", "baseline_version": 1},
        }
    ]
    incoming = obligations.obligations_from_regression(
        node_id="n1",
        diff={"missing": [], "changed": [{"path": "a.gd", "was": "sha256:x", "now": "sha256:y"}], "intact": [], "base_version": 1},
        current_version=2,
        strategy="repair",
    )
    merged = obligations.upsert([dict(item) for item in settled], incoming)
    assert merged[0]["severity"] == obligations.SEVERITY_BLOCKING

    # 反向（repair → record）**不**降级：严重度一旦按政策记成阻断，让它自己掉回 advisory 就是静默放宽
    back = obligations.upsert([dict(item) for item in merged], [
        {**incoming[0], "severity": obligations.SEVERITY_ADVISORY}
    ])
    assert back[0]["severity"] == obligations.SEVERITY_BLOCKING


def test_diff_and_reason_locate_the_path() -> None:
    baseline = regression.build_baseline(
        constitution_version=1,
        constitution_hash="h1",
        artifacts={"a.gd": "sha256:aaa", "b.gd": "sha256:bbb", "c.gd": "sha256:ccc"},
        assertions=[],
    )
    diff = regression.diff_baseline(baseline, {"a.gd": "sha256:aaa", "b.gd": "sha256:zzz"})
    assert [item["path"] for item in diff["missing"]] == ["c.gd"]
    assert [item["path"] for item in diff["changed"]] == ["b.gd"]
    assert diff["intact"] == ["a.gd"]
    reason = regression.diff_reason(diff, current_version=2)
    assert "c.gd" in reason and "b.gd" in reason and "v1" in reason


def test_breaking_entries_are_policy_sensitive() -> None:
    diff = {
        "missing": [{"path": "gone.gd", "was": "sha256:g", "now": "missing"}],
        "changed": [{"path": "chg.gd", "was": "sha256:a", "now": "sha256:b"}],
        "intact": [],
        "base_version": 1,
    }
    assert [item["path"] for item in regression.breaking_entries(diff, regression.STRATEGY_RECORD)] == ["gone.gd"]
    assert sorted(item["path"] for item in regression.breaking_entries(diff, regression.STRATEGY_REPAIR)) == [
        "chg.gd",
        "gone.gd",
    ]


def test_regression_obligation_ids_are_stable_across_rounds() -> None:
    """幂等键绑在产物路径上：否则修复子任务会成为新产出方，债务与子任务一起无限增长。"""
    baseline = regression.build_baseline(
        constitution_version=1,
        constitution_hash="h1",
        artifacts={"a.gd": "sha256:a"},
        assertions=[],
    )
    diff = regression.diff_baseline(baseline, {})
    first = obligations.obligations_from_regression(node_id="n1", diff=diff, current_version=2, strategy="record")
    second = obligations.obligations_from_regression(node_id="n1", diff=diff, current_version=3, strategy="record")
    assert [item["id"] for item in first] == [item["id"] for item in second] == ["regression_break:n1:missing|a.gd"]
    assert first[0]["severity"] == obligations.SEVERITY_BLOCKING
