"""A3 回归测试：宪法修订事务 `amend_constitution`。

**要证明的东西**（对应 `docs/spec-constitution-amendment.md` §6/§7/§8）：

1. **原子性**（K6）——事务中途失败必须整体回滚：不留版本行、不留事件、不动任何节点。
2. **按状态分治**（§6）——不是一个循环刷下去：可立即迁移的换快照，执行中的只登记不打断
   （K2：不产生半旧半新），已完成的收紧时进再验证队列而**不给版本戳**。
3. **方向敏感**（§7）——放宽只更新版本戳且不产生 `revalidate_required`，收紧必须有它。
4. **权限边界**——方向与发起者必须由人显式给出，缺一即拒；不做"漏填就默认"的补全。
5. **幂等**（`(root_id, to_version)` 为键）——同一次修订重复调用不得落第二版。
"""

from __future__ import annotations


# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app import constitution_versions, obligations
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, ConstitutionRecord, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec
from app.utils import loads


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


MARKDOWN_V2 = "# 项目宪法 v2\n\n## 品质下限\n\n提高：所有叶子必须附可复现命令。\n"


def _orchestrator(frozen_mode: bool = False) -> Orchestrator:
    return Orchestrator(
        get_settings().__class__(
            mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir, frozen_mode=frozen_mode
        )
    )


def _version_scoped(items: list[dict]) -> list[dict]:
    """只看**版本域**的债。

    节点上同时可能挂着别的类型（例如约束覆盖缺口），那是另一件事、另一条判据；
    把断言写成"这个列表必须恰好等于版本债"会让另一类债一出现就误报。
    """
    return [item for item in items if item.get("type") in obligations.VERSION_SCOPED_TYPES]


async def _seed(orchestrator: Orchestrator, statuses: dict[str, str]) -> tuple[str, dict[str, str]]:
    """造一棵"根 + 若干指定状态的子节点"的树，全部落在 v1。"""
    constitution_id = await orchestrator.draft_constitution("修订事务探针。", runner_mode="mock")
    orchestrator.lock_constitution(constitution_id)
    root_id = await orchestrator.start_project(
        goal="修订事务探针。",
        target_path=None,
        runner_mode="mock",
        constitution_id=constitution_id,
        auto_run=False,
    )
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        orchestrator._create_children(
            db,
            root,
            [
                ChildTaskSpec(
                    title=title,
                    description="占位。",
                    acceptance=["必须给出证据。"],
                    coverage_ids=["evidence"],
                    estimated_leaf=True,
                )
                for title in statuses
            ],
        )
        db.commit()
        rows = db.query(TaskNode).filter(TaskNode.parent_id == root_id).all()
        ids: dict[str, str] = {}
        for row in rows:
            if str(row.title) in statuses:
                row.status = statuses[str(row.title)]
                ids[str(row.title)] = str(row.id)
        db.commit()
    assert set(ids) == set(statuses)
    return root_id, ids


def _task(task_id: str) -> TaskNode:
    with SessionLocal() as db:
        task = db.get(TaskNode, task_id)
        assert task is not None
        db.expunge(task)
        return task


def _amended_events() -> list[EventRecord]:
    with SessionLocal() as db:
        return list(db.query(EventRecord).filter(EventRecord.type == "constitution.amended"))


def _event_types() -> list[str]:
    with SessionLocal() as db:
        return [event.type for event in db.query(EventRecord).all()]


def _amend(orchestrator: Orchestrator, root_id: str, *, direction: str = "tighten", actor: str = "operator"):
    return orchestrator.amend_constitution(
        root_id,
        markdown=MARKDOWN_V2,
        data={"global_acceptance": ["所有叶子必须附可复现命令"]},
        direction=direction,
        actor=actor,
        note="提高验收下限",
    )


# --- 4. 权限边界 ------------------------------------------------------------


@pytest.mark.asyncio
async def test_amendment_requires_an_explicitly_declared_direction():
    orchestrator = _orchestrator()
    root_id, _ = await _seed(orchestrator, {})
    for bad in ("", "TIGHTEN", "由 agent 自行判断", None):
        with pytest.raises(ValueError):
            orchestrator.amend_constitution(
                root_id, markdown=MARKDOWN_V2, data={}, direction=bad, actor="operator"  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_amendment_requires_a_human_actor():
    orchestrator = _orchestrator()
    root_id, _ = await _seed(orchestrator, {})
    with pytest.raises(ValueError):
        orchestrator.amend_constitution(root_id, markdown=MARKDOWN_V2, data={}, direction="tighten", actor="   ")


def test_amendment_refuses_in_frozen_mode():
    orchestrator = _orchestrator(frozen_mode=True)
    with pytest.raises(RuntimeError):
        orchestrator.amend_constitution("whatever", markdown=MARKDOWN_V2, data={}, direction="tighten", actor="operator")


# --- 2. 按状态分治 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_amend_migrates_idle_nodes_and_leaves_a_version_chain():
    orchestrator = _orchestrator()
    root_id, ids = await _seed(
        orchestrator, {"排队中的子任务": TaskStatus.QUEUED.value, "规划中的子任务": TaskStatus.PLANNED.value}
    )
    before_root = _task(root_id)

    summary = _amend(orchestrator, root_id)

    assert summary["status"] == "amended"
    assert (summary["from_version"], summary["to_version"]) == (1, 2)
    assert summary["migrated_count"] == 3  # 根（planned → migrate_now）+ 两个子节点
    assert summary["affected_by_status"] == {TaskStatus.PLANNED.value: 2, TaskStatus.QUEUED.value: 1}
    assert summary["affected_by_action"] == {constitution_versions.MIGRATE_NOW: 3}

    with SessionLocal() as db:
        amendment = db.get(ConstitutionRecord, summary["constitution_id"])
        head = db.get(ConstitutionRecord, before_root.constitution_id or "")
        assert amendment is not None and head is not None
        assert amendment.version == 2
        assert amendment.supersedes_id == head.id
        assert amendment.amendment_direction == "tighten"
        assert amendment.effective_at is not None
        assert head.version == 1 and head.status == "locked"  # 旧版原封不动

        for node_id in [root_id, *ids.values()]:
            node = db.get(TaskNode, node_id)
            assert node is not None
            assert node.constitution_version == 2
            assert node.constitution_id == amendment.id
            assert node.constitution_hash == amendment.content_hash
            assert loads(node.constitution_json, {}) == {"global_acceptance": ["所有叶子必须附可复现命令"]}


@pytest.mark.asyncio
async def test_amend_does_not_touch_running_nodes_and_registers_them_instead():
    """K2：执行中节点前后 `constitution_hash` 必须像素级不变，否则产物半旧半新。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"执行中的子任务": TaskStatus.WORKING.value})
    child_id = ids["执行中的子任务"]
    before = _task(child_id)

    summary = _amend(orchestrator, root_id)

    assert summary["registered_count"] == 1
    assert summary["affected_by_status"] == {
        TaskStatus.WORKING.value: 1,
        TaskStatus.PLANNED.value: 1,  # 根节点
    }
    after = _task(child_id)
    assert after.constitution_hash == before.constitution_hash
    assert after.constitution_json == before.constitution_json
    assert after.constitution_id == before.constitution_id
    assert after.constitution_version == 1
    assert loads(after.node_meta_json, {})["revalidate_after_run"] == {
        "version": 2,
        "direction": "tighten",
        "actor": "operator",
    }
    # 它仍然落后，所以父节点上有一条 blocking 的版本债在提醒——登记不等于结清
    root = _task(root_id)
    debt = loads(root.node_meta_json, {})["obligations"]
    assert obligations.has_blocking(debt) is True
    # 只看版本域的债：这个节点上还可能有别的类型（例如约束覆盖缺口），那是另一件事
    assert [item["id"] for item in _version_scoped(debt)] == ["constitution_stale:" + child_id + ":v1->v2"]


@pytest.mark.asyncio
async def test_tightening_queues_done_nodes_without_stamping_them():
    """收紧时**不得**用新版本号给旧产物盖章——盖章就是静默 claim 通过。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"已完成的子任务": TaskStatus.DONE.value})
    child_id = ids["已完成的子任务"]

    summary = _amend(orchestrator, root_id)

    assert summary["revalidate_queued_count"] == 1
    child = _task(child_id)
    assert child.constitution_version == 1  # 没有被盖章
    root = _task(root_id)
    debt = loads(root.node_meta_json, {})["obligations"]
    assert sorted(item["type"] for item in _version_scoped(debt)) == sorted(
        [obligations.REVALIDATE_REQUIRED, obligations.CONSTITUTION_STALE]
    )
    assert obligations.has_blocking(debt) is True
    event = _amended_events()[0]
    assert loads(event.payload_json, {})["revalidate_required_ids"] == [child_id]


@pytest.mark.asyncio
async def test_loosening_stamps_done_nodes_and_produces_no_revalidation():
    """放宽只对未来生效：产物仍合规，不复查，也不得追溯洗白。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(
        orchestrator,
        {"已完成的子任务": TaskStatus.DONE.value, "已失败的子任务": TaskStatus.FAILED.value},
    )
    failed_id = ids["已失败的子任务"]

    summary = _amend(orchestrator, root_id, direction="loosen")

    assert summary["stamped_count"] == 1
    # 放宽下 failed 节点**不重新排队**（K4：放宽不得追溯洗白），只换快照
    assert summary["revalidate_queued_count"] == 0
    assert summary["migrated_count"] == 2  # 根 + failed 子节点（都只换快照）
    assert summary["reclassified_count"] == 0
    assert _task(ids["已完成的子任务"]).constitution_version == 2
    # 放宽没有留下任何**版本域**未清事项：已完成的被盖章追平，失败的换快照等重试
    all_debt: list[dict] = []
    for owner_id in (root_id, failed_id):
        all_debt.extend(loads(_task(owner_id).node_meta_json, {}).get("obligations") or [])
    assert _version_scoped(all_debt) == []
    assert obligations.blocking(all_debt) == []
    # 但"不得洗白"仍然成立：失败的节点没有被放宽变成绿的
    assert _task(failed_id).status == TaskStatus.FAILED.value
    event_payload = loads(_amended_events()[0].payload_json, {})
    assert event_payload["revalidate_required_ids"] == []
    assert event_payload["direction"] == "loosen"


@pytest.mark.asyncio
async def test_failed_nodes_are_reclassified_and_requeued_but_not_kicked():
    """`blocked`/`failed` 必须真的回到可重跑状态（§6），但起跑不在事务里做。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"失败待重分类": TaskStatus.FAILED.value})
    child_id = ids["失败待重分类"]

    summary = _amend(orchestrator, root_id)

    assert summary["revalidate_queued_count"] == 0  # 重分类不等于"进再验证队列"
    assert summary["affected_by_action"] == {
        constitution_versions.RECLASSIFY: 1,
        constitution_versions.MIGRATE_NOW: 1,  # 根节点
    }
    child = _task(child_id)
    # §6「按新宪法重新分类**并重试**」：只换快照不算重新分类，节点必须回到可重跑的状态。
    # （这里与第一版的取舍相反：那一版只登记、留待后续步骤，等于把"重试"这件事悬空。）
    assert child.status == TaskStatus.QUEUED.value
    assert child.error_message is None
    assert child.constitution_version == 2  # 快照已换到新版，重试时用新版跑
    assert loads(child.node_meta_json, {})["last_reclassify_version"] == 2
    assert summary["reclassified_ids"] == [child_id]
    assert "task.reclassified_by_amendment" in _event_types()


@pytest.mark.asyncio
async def test_unknown_version_nodes_are_listed_not_stamped():
    """存量行（版本未登记）不是"落后"：不得被顺手盖章成"已知"。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"存量子任务": TaskStatus.QUEUED.value})
    child_id = ids["存量子任务"]
    with SessionLocal() as db:
        child = db.get(TaskNode, child_id)
        assert child is not None
        child.constitution_version = None
        db.commit()

    summary = _amend(orchestrator, root_id)

    assert summary["unknown_version_count"] == 1
    assert _task(child_id).constitution_version is None
    assert loads(_amended_events()[0].payload_json, {})["unknown_version_ids"] == [child_id]


# --- 1. 原子性 --------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_amendment_rolls_back_everything(monkeypatch):
    """K6：中途失败不得留下"半新半旧"——不留版本行、不留事件、不动任何节点。"""
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"排队中的子任务": TaskStatus.QUEUED.value})
    child_id = ids["排队中的子任务"]
    before = _task(child_id)

    def explode(*_args, **_kwargs):
        raise RuntimeError("模拟修订事务中途失败")

    monkeypatch.setattr(orchestrator, "_add_event", explode)

    with pytest.raises(RuntimeError):
        _amend(orchestrator, root_id)

    with SessionLocal() as db:
        assert db.query(ConstitutionRecord).filter(ConstitutionRecord.root_id == root_id).count() == 1
    assert _amended_events() == []
    after = _task(child_id)
    assert after.constitution_version == before.constitution_version
    assert after.constitution_hash == before.constitution_hash
    assert after.constitution_id == before.constitution_id


@pytest.mark.asyncio
async def test_repeated_amendment_is_idempotent_per_root_and_target_version():
    orchestrator = _orchestrator()
    root_id, ids = await _seed(orchestrator, {"排队中的子任务": TaskStatus.QUEUED.value})

    first = _amend(orchestrator, root_id)
    second = _amend(orchestrator, root_id)

    assert first["status"] == "amended"
    assert second["status"] == "already_applied"
    assert second["version"] == 2
    assert second["constitution_id"] == first["constitution_id"]
    assert len(_amended_events()) == 1
    with SessionLocal() as db:
        assert db.query(ConstitutionRecord).filter(ConstitutionRecord.root_id == root_id).count() == 2
    assert _task(ids["排队中的子任务"]).constitution_version == 2


@pytest.mark.asyncio
async def test_amendment_on_a_tree_without_constitution_is_rejected():
    orchestrator = _orchestrator()
    with SessionLocal() as db:
        db.add(TaskNode(id="orphan-root", title="Root", description="", depth=0, status=TaskStatus.PLANNED.value))
        db.commit()
    with pytest.raises(ValueError):
        _amend(orchestrator, "orphan-root")


# --- 3. 与 O-A4 的接缝：修订之后新建的节点必须落在新版 -----------------------


@pytest.mark.asyncio
async def test_children_created_after_an_amendment_inherit_the_new_version_and_snapshot():
    orchestrator = _orchestrator()
    root_id, _ = await _seed(orchestrator, {})
    summary = _amend(orchestrator, root_id)
    with SessionLocal() as db:
        root = db.get(TaskNode, root_id)
        assert root is not None
        orchestrator._create_children(
            db,
            root,
            [
                ChildTaskSpec(
                    title="修订后新建的子任务",
                    description="占位。",
                    acceptance=["必须给出证据。"],
                    coverage_ids=["evidence"],
                    estimated_leaf=True,
                )
            ],
        )
        db.commit()
        child = db.query(TaskNode).filter(TaskNode.parent_id == root_id).one()
    assert child.constitution_version == 2
    assert child.constitution_id == summary["constitution_id"]
    assert loads(child.constitution_json, {}) == {"global_acceptance": ["所有叶子必须附可复现命令"]}
