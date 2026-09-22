from __future__ import annotations

import json

from app import skill_notes
from app.models import TaskNode
from app.orchestrator import Orchestrator
from app.schemas import ChildTaskSpec


class _Settings:
    def __init__(self, **overrides: object) -> None:
        self.parent_skill_note_task_enabled = True
        self.runner_mode = "real"
        for key, value in overrides.items():
            setattr(self, key, value)


def _task(title: str = "普通节点", *, meta: dict | None = None, scope: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id="node-1",
        title=title,
        node_meta_json=json.dumps(meta or {}),
        scope_json=json.dumps(scope or []),
    )


def _spec(title: str = "普通子任务", **overrides: object) -> ChildTaskSpec:
    return ChildTaskSpec(title=title, **overrides)


def test_skill_note_tasks_enabled_requires_real_runner() -> None:
    assert skill_notes.skill_note_tasks_enabled(_Settings()) is True
    assert skill_notes.skill_note_tasks_enabled(_Settings(runner_mode="mock")) is False
    assert skill_notes.skill_note_tasks_enabled(_Settings(parent_skill_note_task_enabled=False)) is False


def test_looks_like_skill_note_title_explicit_markers() -> None:
    assert skill_notes.looks_like_skill_note_title("Skill Note: 归拢") is True
    assert skill_notes.looks_like_skill_note_title("docs/skill_notes/x.json") is True
    assert skill_notes.looks_like_skill_note_title("技能笔记 归拢") is True
    assert skill_notes.looks_like_skill_note_title("") is False
    assert skill_notes.looks_like_skill_note_title("   ") is False
    assert skill_notes.looks_like_skill_note_title("实现战斗系统") is False


def test_looks_like_skill_note_title_requires_both_skill_and_consolidation() -> None:
    # 只有 skill 不够
    assert skill_notes.looks_like_skill_note_title("skill 实现") is False
    # 只有归拢类词也不够
    assert skill_notes.looks_like_skill_note_title("经验汇总") is False
    # 两者都有才算（大小写无关）
    assert skill_notes.looks_like_skill_note_title("SKILL 经验汇总") is True
    assert skill_notes.looks_like_skill_note_title("技能 归并本轮递归") is True


def test_is_skill_note_task_via_meta_and_title() -> None:
    settings = _Settings()
    assert skill_notes.is_skill_note_task(_task(meta={"coverage_ids": [skill_notes.SKILL_NOTE_COVERAGE_ID]}), settings) is True
    assert skill_notes.is_skill_note_task(_task(meta={"verification_kind": skill_notes.SKILL_NOTE_VERIFICATION_KIND}), settings) is True
    assert skill_notes.is_skill_note_task(_task("skill note 归拢"), settings) is True
    assert skill_notes.is_skill_note_task(_task(), settings) is False


def test_is_skill_note_spec_via_fields_and_title() -> None:
    settings = _Settings()
    assert skill_notes.is_skill_note_spec(_spec(coverage_ids=[skill_notes.SKILL_NOTE_COVERAGE_ID]), settings) is True
    assert skill_notes.is_skill_note_spec(_spec(verification_kind=skill_notes.SKILL_NOTE_VERIFICATION_KIND), settings) is True
    assert skill_notes.is_skill_note_spec(_spec("技能笔记"), settings) is True
    assert skill_notes.is_skill_note_spec(_spec(), settings) is False


def test_has_skill_note_child() -> None:
    settings = _Settings()
    parent = _task()
    assert skill_notes.has_skill_note_child(parent, settings) is False
    parent.children = [_task(meta={"coverage_ids": [skill_notes.SKILL_NOTE_COVERAGE_ID]})]
    assert skill_notes.has_skill_note_child(parent, settings) is True


def test_parent_skill_note_child_composition() -> None:
    parent = _task("战斗系统", scope=["art", "src"])
    siblings = [_spec("子任务甲"), _spec("子任务乙")]
    spec = skill_notes.parent_skill_note_child(parent, siblings)

    assert spec.title == "战斗系统：归拢递归经验 Skill"
    assert spec.depends_on == ["子任务甲", "子任务乙"]
    assert spec.coverage_ids == [skill_notes.SKILL_NOTE_COVERAGE_ID]
    assert spec.verification_kind == skill_notes.SKILL_NOTE_VERIFICATION_KIND
    assert spec.estimated_leaf is True
    # 作用域继承父节点并追加 skill note 目录
    assert spec.scope[:2] == ["art", "src"]
    assert "docs/skill_notes" in spec.scope
    assert any("docs/skill_notes" in item for item in spec.acceptance)


def test_with_parent_skill_note_child_gates() -> None:
    settings = _Settings()
    parent = _task("父节点")
    children = [_spec("甲"), _spec("乙")]

    # 未启用 → 原样返回
    assert skill_notes.with_parent_skill_note_child(parent, children, _Settings(parent_skill_note_task_enabled=False)) == children
    # 父节点自己就是 skill note → 原样返回
    assert skill_notes.with_parent_skill_note_child(_task("技能笔记"), children, settings) == children
    # 已有 skill-note 子任务 → 原样返回
    existing = [*children, _spec("技能笔记")]
    assert skill_notes.with_parent_skill_note_child(parent, existing, settings) == existing


def test_with_parent_skill_note_child_appends_one() -> None:
    settings = _Settings()
    parent = _task("父节点")
    parent.children = []
    children = [_spec("甲"), _spec("乙")]
    result = skill_notes.with_parent_skill_note_child(parent, children, settings)

    assert len(result) == 3
    assert result[:2] == children
    assert result[2].coverage_ids == [skill_notes.SKILL_NOTE_COVERAGE_ID]
    # depends_on 只包含主任务
    assert result[2].depends_on == ["甲", "乙"]


def test_with_parent_skill_note_child_returns_children_when_only_skill_notes() -> None:
    settings = _Settings()
    parent = _task("父节点")
    parent.children = []
    only_notes = [_spec("技能笔记")]
    # main_children 为空 → 原样返回（同一对象，未新增）
    assert skill_notes.with_parent_skill_note_child(parent, only_notes, settings) == only_notes
    assert len(skill_notes.with_parent_skill_note_child(parent, only_notes, settings)) == 1


def test_orchestrator_delegates_agree_with_module() -> None:
    """反漂移：orchestrator 的委托必须与模块函数给出同一答案。"""
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator.settings = _Settings()

    parent = _task("战斗系统")
    parent.children = []
    children = [_spec("甲")]
    assert orchestrator._skill_note_tasks_enabled() == skill_notes.skill_note_tasks_enabled(orchestrator.settings)
    assert orchestrator._looks_like_skill_note_title("技能笔记") == skill_notes.looks_like_skill_note_title("技能笔记")
    assert orchestrator._is_skill_note_task(_task()) == skill_notes.is_skill_note_task(_task(), orchestrator.settings)
    assert orchestrator._is_skill_note_spec(_spec()) == skill_notes.is_skill_note_spec(_spec(), orchestrator.settings)
    assert orchestrator._has_skill_note_child(parent) == skill_notes.has_skill_note_child(parent, orchestrator.settings)
    assert orchestrator._parent_skill_note_child(parent, children) == skill_notes.parent_skill_note_child(parent, children)
    assert orchestrator._with_parent_skill_note_child(parent, children) == (
        skill_notes.with_parent_skill_note_child(parent, children, orchestrator.settings)
    )
