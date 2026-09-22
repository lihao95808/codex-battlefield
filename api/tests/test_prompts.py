"""R-b 反漂移：`runner.py` 的提示词/指导语/上下文委托必须与 `prompts` / `project_context` 一致。

参照 `test_blockers.py` 与 `test_runner_tools.py`：断言**可观测结果一致**——提示词逐字符、
上下文摘要逐字符、默认宪法数据逐字段。提示词是喂给 Codex 的输入，差一个字就是行为变化。

另有一条反面自检：只比"委托的返回值 == 模块函数的返回值"是不够的，**参数传错位也能过**
（比如把 settings 当 task 传）。所以再断言提示词里真的出现了这条 task / worker_result 的内容。
"""

from __future__ import annotations

import json
import pathlib

from app import project_context, prompts, runner, runner_tools
from app.config import Settings
from app.models import TaskNode
from app.schemas import WorkerResult


def _settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(workspace_dir=tmp_path / "workspace")


def _executor(tmp_path: pathlib.Path) -> runner.CodexExecRunner:
    return runner.CodexExecRunner(_settings(tmp_path))


def _task() -> TaskNode:
    """字段全部显式给：Column(default=...) 只在写库时生效，未落库的实例读到的是 None。"""
    return TaskNode(
        id="node-r-b",
        title="叶子标题",
        description="把垂直切片做完。",
        depth=2,
        status="running",
        scope_json=json.dumps(["src/level.tscn", "not a path"], ensure_ascii=False),
        acceptance_json=json.dumps(["能跑起来"], ensure_ascii=False),
        node_meta_json=json.dumps({"inherited_constraints": [{"text": "约束一"}]}, ensure_ascii=False),
        constitution_json=json.dumps({"target_path": "", "global_acceptance": ["逐字继承"]}, ensure_ascii=False),
        constitution_hash="hash-r-b",
        worktree_path=None,
    )


def _worker_result() -> WorkerResult:
    return WorkerResult(status="succeeded", summary="worker 做完了", changed_files=["a.txt"])


def _children() -> list[dict]:
    return [{"id": "child-1", "title": "子任务", "status": "done", "result_json": "{}"}]


def test_prompt_delegates_agree_with_prompts_module(tmp_path: pathlib.Path) -> None:
    settings = _settings(tmp_path)
    executor = _executor(tmp_path)
    task = _task()
    worker_result = _worker_result()
    children = _children()

    assert executor._planner_prompt(task) == prompts.planner_prompt(settings, task)
    assert executor._worker_prompt(task) == prompts.worker_prompt(settings, task)
    assert executor._verifier_prompt(task, worker_result) == prompts.verifier_prompt(settings, task, worker_result)
    assert executor._constitution_prompt("目标", None) == prompts.constitution_prompt("目标", None)
    assert executor._acceptance_prompt(task, children) == prompts.acceptance_prompt(settings, task, children)
    assert executor._inherited_constraints_block(task) == prompts.inherited_constraints_block(task)
    assert executor._default_constitution_data("原因") == prompts.default_constitution_data("原因")


def test_guidance_delegates_agree_with_prompts_module(tmp_path: pathlib.Path) -> None:
    settings = _settings(tmp_path)
    executor = _executor(tmp_path)

    assert executor._windows_shell_guidance() == prompts.windows_shell_guidance()
    assert executor._visual_validation_guidance() == prompts.visual_validation_guidance()
    assert executor._text_integrity_guidance() == prompts.text_integrity_guidance()
    assert executor._read_only_validation_guidance() == prompts.read_only_validation_guidance()
    assert executor._skill_lookup_guidance() == prompts.skill_lookup_guidance(settings)
    assert executor._project_skill_guidance() == prompts.project_skill_guidance(settings)
    assert executor._godot_validation_guidance() == prompts.godot_validation_guidance(settings)


def test_context_and_executable_delegates_agree_with_their_modules(tmp_path: pathlib.Path) -> None:
    settings = _settings(tmp_path)
    executor = _executor(tmp_path)
    task = _task()
    sample = tmp_path / "sample.txt"
    sample.write_text("上下文样例", encoding="utf-8")

    assert executor._context_roots(task) == project_context.context_roots(task)
    assert executor._project_context_summary(task) == project_context.project_context_summary(task)
    assert executor._summarize_context_root("LABEL", tmp_path) == project_context.summarize_context_root("LABEL", tmp_path)
    assert executor._safe_text_excerpt(sample, 3) == project_context.safe_text_excerpt(sample, 3)
    assert executor._looks_like_scope_path("src/a.tscn") == project_context.looks_like_scope_path("src/a.tscn")
    assert executor._resolve_godot_executable() == runner_tools.resolve_godot_executable(settings)


def test_prompts_actually_carry_this_task_arguments(tmp_path: pathlib.Path) -> None:
    """反面自检：参数传错位（例如 settings 当 task 传）时，上面那些"两边一致"仍会通过。"""
    executor = _executor(tmp_path)
    task = _task()
    worker_result = _worker_result()

    worker = executor._worker_prompt(task)
    assert "node-r-b" in worker
    assert "叶子标题" in worker
    assert "INHERITED_CONSTRAINTS_JSON" in worker
    assert "约束一" in worker

    verifier = executor._verifier_prompt(task, worker_result)
    assert "worker 做完了" in verifier

    acceptance = executor._acceptance_prompt(task, _children())
    assert "child-1" in acceptance
