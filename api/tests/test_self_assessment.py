"""自评校准读数的回归测试（`docs/spec-verification-independence.md` §5，判据 V7）。

**这个文件存在的理由**：这是一条**只读**读数，最容易的错法不是算错，而是"算不出来时填 0"——
`overconfidence_ratio = 0.0` 会被读成"这个模型从不过度自信"，而真相是"还没有样本"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import calibration
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.utils import dumps


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


def _node(node_id: str, *, depth: int = 1, leaf_attempts: int = 0, failures: int = 0, converted: bool = False) -> TaskNode:
    return TaskNode(
        id=node_id,
        title=node_id,
        description="",
        depth=depth,
        status=TaskStatus.DONE.value,
        leaf_attempts=leaf_attempts,
        verification_failures=failures,
        converted_from_leaf=converted,
    )


def test_no_samples_reports_none_instead_of_zero() -> None:
    """**没有样本 → `None`**。填 0 会把"还没试过"读成"零失误"。"""
    summary = calibration.summarize([_node("a"), _node("b", leaf_attempts=0, failures=0)])
    assert summary["claims"] == 0
    assert summary["misses"] == 0
    assert summary["overconfidence_ratio"] is None
    assert summary["by_depth"] == {}


def test_counts_claims_and_misses_per_depth() -> None:
    summary = calibration.summarize(
        [
            _node("done-once", depth=1, leaf_attempts=1),
            _node("failed-verify", depth=1, leaf_attempts=2, failures=1),
            _node("converted", depth=2, leaf_attempts=1, converted=True),
            _node("never-tried", depth=2, leaf_attempts=0),
        ]
    )
    assert summary["claims"] == 3
    assert summary["misses"] == 2
    assert summary["overconfidence_ratio"] == pytest.approx(0.6667)
    assert summary["by_depth"]["1"] == {"claims": 2, "misses": 1}
    assert summary["by_depth"]["2"] == {"claims": 1, "misses": 1}


def test_policy_states_that_nothing_is_triggered() -> None:
    """读数必须自己写清"它不触发什么"——否则下一个人会以为它已经在自动拆分了。"""
    summary = calibration.summarize([_node("a", leaf_attempts=1, failures=1)])
    assert "只读" in summary["policy"]
    assert "不据此拆分" in summary["policy"]


def test_tree_stats_expose_the_readouts(tmp_path: Path) -> None:
    """接线：`stats` 里必须真的能看到（否则就是"机制看着在、实际是死的"）。"""
    settings = get_settings().__class__(mock_delay_seconds=0, workspace_dir=tmp_path / "workspace")
    orchestrator = Orchestrator(settings)
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="sa-root",
                title="Root",
                description="",
                depth=0,
                status=TaskStatus.DONE.value,
                scope_json=dumps([str(tmp_path / "target")]),
            )
        )
        db.add(
            TaskNode(
                id="sa-leaf",
                parent_id="sa-root",
                title="Leaf",
                description="",
                depth=1,
                status=TaskStatus.DONE.value,
                leaf_attempts=1,
                verification_failures=1,
            )
        )
        db.commit()
        tree = orchestrator.get_tree(db, root_id="sa-root")
    stats = tree["stats"]
    assert stats["self_assessment"]["claims"] == 1
    assert stats["self_assessment"]["misses"] == 1
    assert set(stats["model_tiers"]["effective"]) == {"constitution", "planner", "worker", "verifier", "acceptance"}
    # 有类型的 dict：塞进去的东西必须能被模型校验（`stats` 是联合类型，写错会被 Pydantic 当场打回）
    assert json.loads(json.dumps(stats["self_assessment"], ensure_ascii=False))["policy"]
