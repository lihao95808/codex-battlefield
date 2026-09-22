"""约束覆盖核对（§7.1.4 / O6）的单元测试。

**这一条最容易被做假**：用相似度就能"永远算覆盖了"，用关键词命中也能把噪声做成"通过"。
所以这里的用例重点是**判别力**：改写过的约束必须被算成缺口，而不是被算成"差不多覆盖了"。
"""

from __future__ import annotations

import json

# 测试库路径由 tests/conftest.py 按进程号分配（写死一个共享文件会让被杀掉的运行
# 把下一次运行挂住）。这里不再覆盖它。

import pytest

from app import constraints, obligations
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)

CONSTITUTION = {
    "global_acceptance": [
        "任务树必须保留总目标的品质下限。",
        "真实运行记录必须保存 prompt、结果、证据和失败原因。",
    ],
    "quality_floor": ["这条不属于必须下沉的约束清单。"],
}


def test_normalization_only_does_whitespace_and_case() -> None:
    assert constraints.normalize_constraint("  A   b  ") == "a b"
    assert constraints.normalize_constraint("Tab\t分隔") == "tab 分隔"
    assert constraints.normalize_constraint(None) == ""
    assert constraints.normalize_constraint(42) == ""


def test_only_global_acceptance_is_delegable() -> None:
    """父节点自己的整合级判据不该要求子节点逐字重复——那只会变成噪声。"""
    assert constraints.delegable_constraints(CONSTITUTION) == CONSTITUTION["global_acceptance"]
    assert constraints.delegable_constraints({"quality_floor": ["x"]}) == []
    assert constraints.delegable_constraints(None) == []


def test_verbatim_inheritance_counts_as_covered() -> None:
    assert (
        constraints.coverage_gap(
            CONSTITUTION["global_acceptance"],
            [["任务树必须保留总目标的品质下限。"]],
        )
        == ["真实运行记录必须保存 prompt、结果、证据和失败原因。"]
    )


def test_whitespace_and_case_differences_still_count_as_covered() -> None:
    gap = constraints.coverage_gap(
        CONSTITUTION["global_acceptance"],
        [["  任务树必须保留总目标的品质下限。  ", "真实运行记录必须保存 PROMPT、结果、证据和失败原因。"]],
    )
    assert gap == []


def test_coverage_is_over_the_child_set_not_each_child() -> None:
    """§7.1.4 说的是**集合**覆盖：两条约束分给两个子节点各带一条就算覆盖。"""
    gap = constraints.coverage_gap(
        CONSTITUTION["global_acceptance"],
        [["任务树必须保留总目标的品质下限。"], ["真实运行记录必须保存 prompt、结果、证据和失败原因。"]],
    )
    assert gap == []


def test_a_reworded_constraint_is_a_gap_not_a_match() -> None:
    """判别力的核心：改写就是侵蚀的第一步，必须算缺口。

    如果这条用例"通过"（也就是把改写算成覆盖），整条判据就变成装饰。
    """
    paraphrase = ["必须保证任务树的品质下限不被稀释。", "需要保留运行记录。"]
    gap = constraints.coverage_gap(CONSTITUTION["global_acceptance"], [paraphrase])
    assert gap == CONSTITUTION["global_acceptance"]
    # 反过来：只要有一条真的逐字带上了，它就出缺口清单（不许"部分命中算全中"）
    partially = [paraphrase + ["任务树必须保留总目标的品质下限。"]]
    assert constraints.coverage_gap(CONSTITUTION["global_acceptance"], partially) == [
        "真实运行记录必须保存 prompt、结果、证据和失败原因。"
    ]


def test_gap_debt_is_idempotent_and_carries_the_missing_lines() -> None:
    first = constraints.coverage_gap_debt("parent-1", ["约束 A"], depth=2)
    second = constraints.coverage_gap_debt("parent-1", ["约束 A", "约束 B"], depth=2)
    assert first is not None and second is not None
    # 缺口清单会随子节点变化，把它写进 id 会每轮生出新债
    assert first["id"] == second["id"]
    assert second["payload"]["missing"] == ["约束 A", "约束 B"]
    assert second["depth"] == 2


def test_gap_severity_is_decided_by_an_explicit_rule() -> None:
    """默认只记录并上浮；要阻断必须显式声明（§6.2：降级/升级都由规则明说）。

    默认阻断是**量出来不行的**：LLM 规划器会改写措辞，于是每棵树的父节点都永远拿不到 done
    （全量回归当场 14 条既有契约变红）。
    """
    default = constraints.coverage_gap_debt("parent-1", ["约束 A"])
    assert default is not None
    assert default["severity"] == obligations.SEVERITY_ADVISORY
    assert obligations.blocking([default]) == []

    strict = constraints.coverage_gap_debt("parent-1", ["约束 A"], blocks=True)
    assert strict is not None
    assert strict["severity"] == obligations.SEVERITY_BLOCKING
    assert strict["payload"]["blocks"] is True
    assert obligations.has_blocking([strict]) is True


def test_no_gap_no_debt() -> None:
    assert constraints.coverage_gap_debt("parent-1", []) is None


def test_gap_debt_clears_when_the_children_cover_again() -> None:
    debt = constraints.coverage_gap_debt("parent-1", ["约束 A"])
    assert debt is not None
    stored = obligations.upsert([], [debt])
    still = constraints.settle_coverage_cleared(stored, still_missing=["约束 A"])
    assert still[0]["settled_at"] is None
    cleared = constraints.settle_coverage_cleared(stored, still_missing=[])
    assert cleared[0]["settled_by"] == obligations.SETTLED_BY_COVERAGE_CLEARED
    assert obligations.blocking(cleared) == []


def test_settle_path_is_registered_as_a_named_path() -> None:
    assert obligations.SETTLED_BY_COVERAGE_CLEARED in obligations.VALID_SETTLED_BY
    # 严重度在**生成时**由规则定，不写死在"天然阻断"表里——否则门控按开关过滤时，
    # 看板就会出现"报阻断但树照跑"（这个坑在冲突债上踩过一次）
    assert obligations.CONSTRAINT_COVERAGE_GAP not in obligations.ALWAYS_BLOCKING


# --- §7.1.3 父节点自身的 must/never 也要下沉（逐层累积） --------------------


def test_downward_inheritance_accumulates_along_the_chain() -> None:
    """只看直接父节点的话，第 3 跳以后没人能说出第 1 跳原本要求什么。"""
    grandparent_lines = ["不许把目标扩张为完整商业产品。"]
    parent_lines = ["组合结果必须可玩。"]
    child = constraints.inherit_down(parent_lines, grandparent_lines)
    assert child == ["不许把目标扩张为完整商业产品。", "组合结果必须可玩。"]
    # 再往下传时是"祖先链 + 本层"，不是只看本层
    grandchild = constraints.inherit_down(["叶子必须有可复现命令。"], child)
    assert grandchild == [
        "不许把目标扩张为完整商业产品。",
        "组合结果必须可玩。",
        "叶子必须有可复现命令。",
    ]


def test_downward_inheritance_dedupes_by_normalized_form() -> None:
    child = constraints.inherit_down(["  组合结果必须可玩。  "], ["组合结果必须可玩。"])
    assert child == ["组合结果必须可玩。"]


@pytest.mark.asyncio
async def test_downward_inheritance_does_not_pollute_child_acceptance() -> None:
    """下沉的是**约束**，不是把父节点的判分标准摊给每个子节点。

    父节点的整合级条目压进叶子 acceptance 会让叶子验收必然失败——那是制造失败，不是防漂移。
    所以它落在 `node_meta["inherited_constraints"]`，并通过 prompt 的独立区块给 agent 看。
    """
    root = TaskNode(
        id="inh-root",
        title="Root",
        description="",
        depth=0,
        status="planned",
        acceptance_json='["组合结果必须可玩。"]',
        constitution_json='{"global_acceptance": ["任务树必须保留总目标的品质下限。"]}',
    )
    await _decompose(root, ["叶子自己的验收。"])
    with SessionLocal() as db:
        child = db.query(TaskNode).filter(TaskNode.parent_id == "inh-root").one()
        inherited = json.loads(child.node_meta_json)["inherited_constraints"]
        acceptance = json.loads(child.acceptance_json)
    assert inherited == ["组合结果必须可玩。"]
    assert acceptance == ["叶子自己的验收。"]


@pytest.mark.asyncio
async def test_inherited_constraints_accumulate_across_two_hops() -> None:
    """第二跳的子节点必须同时看到祖父与本层的约束。"""
    from app.runner import CodexExecRunner

    root = TaskNode(
        id="hop-root",
        title="Root",
        description="",
        depth=0,
        status="planned",
        acceptance_json='["祖父的 must。"]',
    )
    await _decompose(root, ["中间层的 must。"])
    with SessionLocal() as db:
        middle = db.query(TaskNode).filter(TaskNode.parent_id == "hop-root").one()
        runner = CodexExecRunner(get_settings())
        assert runner._inherited_constraints_block(middle).count("祖父的 must。") == 1


def test_inherited_constraints_reach_every_agent_prompt() -> None:
    """只躺在 node_meta 里等于没下沉：agent 看不到就不会遵守，改写了也没人发现。"""
    from app.runner import CodexExecRunner

    task = TaskNode(
        id="prompt-child",
        title="Leaf",
        description="Do the thing.",
        depth=1,
        status="queued",
        acceptance_json='["叶子自己的验收。"]',
        node_meta_json='{"inherited_constraints": ["组合结果必须可玩。"]}',
    )
    runner = CodexExecRunner(get_settings())
    block = runner._inherited_constraints_block(task)
    assert "INHERITED_CONSTRAINTS_JSON" in block
    assert "组合结果必须可玩。" in block
    assert "不得改写或放宽" in block
    assert runner._inherited_constraints_block(TaskNode(id="x", title="t", description="", depth=0)) == ""


async def _decompose(root: TaskNode, acceptance: list[str]) -> list[dict]:
    orchestrator = Orchestrator(
        get_settings().__class__(mock_delay_seconds=0, workspace_dir=get_settings().workspace_dir)
    )
    with SessionLocal() as db:
        db.add(root)
        db.commit()
        orchestrator._create_children(
            db, root, [ChildTaskSpec(title="子任务", description="", acceptance=acceptance)]
        )
        db.commit()
        return json.loads(root.node_meta_json).get("obligations") or []


@pytest.mark.asyncio
async def test_decomposition_records_the_coverage_gap_at_creation_time() -> None:
    """创建时就写出缺口，而不是等到验收才发现——那时已经浪费了一整轮。

    默认档下不拦创建、也不拦完成：缺口的价值在于**立刻可见**（事件 + 债务 + 看板），
    让 agent 下一次规划有机会补上、让人有机会裁定。
    """
    root = TaskNode(
        id="cov-root",
        title="Root",
        description="",
        depth=0,
        status="planned",
        acceptance_json='["父节点自己的整合判据。"]',
        constitution_json='{"global_acceptance": ["任务树必须保留总目标的品质下限。"]}',
    )
    items = await _decompose(root, ["保证品质下限不被稀释。"])

    gap = [item for item in items if item["type"] == obligations.CONSTRAINT_COVERAGE_GAP]
    assert len(gap) == 1
    assert gap[0]["payload"]["missing"] == ["任务树必须保留总目标的品质下限。"]
    # 默认不阻断：可见但不卡树（默认阻断实测会让每棵树的父节点永远拿不到 done）
    assert gap[0]["severity"] == obligations.SEVERITY_ADVISORY
    with SessionLocal() as db:
        assert "parent.constraint_coverage_gap" in [event.type for event in db.query(EventRecord).all()]


@pytest.mark.asyncio
async def test_verbatim_inheritance_leaves_no_gap() -> None:
    root = TaskNode(
        id="cov-root",
        title="Root",
        description="",
        depth=0,
        status="planned",
        constitution_json='{"global_acceptance": ["任务树必须保留总目标的品质下限。"]}',
    )
    items = await _decompose(root, ["任务树必须保留总目标的品质下限。"])
    assert [item for item in items if item["type"] == obligations.CONSTRAINT_COVERAGE_GAP] == []
