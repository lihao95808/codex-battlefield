"""能力基准纯逻辑的回归测试（`docs/spec-capability-benchmark.md` C4）。

**这个文件存在的理由**：基准本身是"用来判断别的东西"的量具，量具错了比被测对象错了更贵——
分数会到处传，而没人会去怀疑量具。所以这里把两条最容易写错的规则单独钉死：

1. **成功率只由外部判据算**：树自称 `done` 不进成功率，只进 `tree_claimed_done`；
2. **读不到 ≠ 0**：判不了的（`error`/`invalid_path`）单列，缺的指标记 `None`。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from app import bench


def _task(**overrides) -> dict:
    raw = {
        "id": "t1",
        "kind": "solvable",
        "goal": "做点什么",
        "checks": [{"id": "f", "type": "file_exists", "path": "a.txt"}],
    }
    raw.update(overrides)
    return bench.normalize_task(raw)


def test_normalize_task_rejects_bad_definitions() -> None:
    """定义不合法必须**抛**：跑一个定义错的任务，得到的分数是垃圾。"""
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task({"kind": "solvable", "goal": "x", "checks": []})  # 没有 id
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task({"id": "a", "kind": "maybe", "goal": "x", "checks": [{"type": "file_exists", "path": "a"}]})
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task({"id": "a", "goal": "x", "checks": []})  # 没有外部判据
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task({"id": "a", "goal": "x", "checks": [{"type": "nope", "path": "a"}]})
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task({"id": "a", "goal": "x", "checks": [{"type": "contains", "path": "a"}]})  # 少 pattern
    with pytest.raises(bench.TaskSpecError):
        bench.normalize_task(
            {
                "id": "a",
                "goal": "x",
                "checks": [
                    {"id": "same", "type": "file_exists", "path": "a"},
                    {"id": "same", "type": "file_exists", "path": "b"},
                ],
            }
        )


def test_normalize_task_fills_defaults_without_hiding_errors() -> None:
    task = _task()
    assert task["kind"] == "solvable"
    assert task["max_agent_starts"] is None
    assert task["checks"][0]["id"] == "f"


def test_repo_tasks_are_valid() -> None:
    """仓库里真实的任务定义必须过校验——否则基准跑到一半才发现考卷是坏的。"""
    tasks = [bench.load_task(path) for path in sorted((pathlib.Path(__file__).resolve().parents[2] / "bench" / "tasks").glob("*/task.json"))]
    assert {task["id"] for task in tasks} == {"add-multiply", "fix-subtract", "impossible-no-write"}
    kinds = {task["id"]: task["kind"] for task in tasks}
    assert kinds["impossible-no-write"] == "unsolvable"
    assert sum(1 for task in tasks if task["kind"] == "solvable") == 2


def test_task_set_hash_tracks_the_exam_not_just_the_score(tmp_path: pathlib.Path) -> None:
    first = bench.task_set_hash([_task()])
    second = bench.task_set_hash([_task(goal="换个目标")])
    assert first != second
    assert first == bench.task_set_hash([_task()])


def test_evaluate_check_four_types(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    ok = bench.evaluate_check({"id": "e", "type": "file_exists", "path": "a.txt"}, tmp_path)
    missing = bench.evaluate_check({"id": "e", "type": "file_exists", "path": "nope.txt"}, tmp_path)
    contains = bench.evaluate_check({"id": "c", "type": "contains", "path": "a.txt", "pattern": "hello"}, tmp_path)
    not_contains = bench.evaluate_check({"id": "n", "type": "not_contains", "path": "a.txt", "pattern": "hello"}, tmp_path)
    command_ok = bench.evaluate_check(
        {"id": "cmd", "type": "command", "command": ["{python}", "-c", "raise SystemExit(0)"], "expect_exit": 0},
        tmp_path,
        python_executable=sys.executable,
    )
    command_failed = bench.evaluate_check(
        {"id": "cmd", "type": "command", "command": ["{python}", "-c", "raise SystemExit(3)"], "expect_exit": 0},
        tmp_path,
        python_executable=sys.executable,
    )
    assert ok["status"] == bench.STATUS_PASS
    assert missing["status"] == bench.STATUS_FAIL
    assert contains["ok"] is True
    assert not_contains["ok"] is False  # 反向判据同样是"判了，不成立"，不是"判不了"
    assert command_ok["status"] == bench.STATUS_PASS
    assert command_failed["status"] == bench.STATUS_FAIL
    assert "exit=3" in command_failed["detail"]


def test_unjudged_is_not_a_failure_and_not_a_pass(tmp_path: pathlib.Path) -> None:
    """"判不了"必须与"判了不成立"分开——否则一次读盘失败会被读成"模型做错了"。"""
    unreadable = bench.evaluate_check({"id": "c", "type": "contains", "path": "gone.txt", "pattern": "x"}, tmp_path)
    escaping = bench.evaluate_check({"id": "c", "type": "file_exists", "path": "../outside.txt"}, tmp_path)
    absolute = bench.evaluate_check({"id": "c", "type": "file_exists", "path": "/etc/passwd"}, tmp_path)
    assert unreadable["status"] == bench.STATUS_ERROR
    assert escaping["status"] == bench.STATUS_INVALID_PATH
    assert absolute["status"] == bench.STATUS_INVALID_PATH
    counts = bench.count_checks([unreadable, escaping, absolute])
    assert counts["passed"] == 0
    assert counts["unjudged"] == 3
    assert counts[bench.STATUS_FAIL] == 0


def test_overconfident_is_the_headline_metric() -> None:
    """它说完成了、外部判据说没完成 → 这条比成功率更重要。"""
    task = _task()
    flaky = [{"id": "f", "type": "file_exists", "ok": False, "status": bench.STATUS_FAIL, "detail": ""}]
    summary = bench.summarize_task(task, flaky, tree_status="done", metrics={})
    assert summary["tree_claimed_done"] is True
    assert summary["overconfident"] is True
    assert summary["checks_all_passed"] is False

    honest = bench.summarize_task(task, flaky, tree_status="blocked", metrics={})
    assert honest["tree_claimed_done"] is False
    assert honest["overconfident"] is False

    # 判据"判不了"时不算完成：既不通过，也不构成"谎报"
    unjudged = bench.summarize_task(
        task, [{"id": "f", "type": "file_exists", "ok": False, "status": bench.STATUS_ERROR, "detail": ""}],
        tree_status="done", metrics={},
    )
    assert unjudged["checks_passed"] == 0
    assert unjudged["checks_unjudged"] == 1


def test_unsolvable_claiming_done_makes_the_whole_run_untrustworthy() -> None:
    """`unsolvable` 任务声称完成 → 整档不可信（这是"不谎报完成"的唯一可反驳形式）。"""
    task = _task(kind="unsolvable")
    failed = [{"id": "f", "type": "file_exists", "ok": False, "status": bench.STATUS_FAIL, "detail": ""}]
    bad = bench.summarize_task(task, failed, tree_status="done", metrics={})
    good = bench.summarize_task(task, failed, tree_status="blocked", metrics={})

    report_bad = bench.build_report(
        provider="mock", task_set_hash_value="sha256:x", summaries=[bad], started_at="", finished_at=""
    )
    report_good = bench.build_report(
        provider="mock", task_set_hash_value="sha256:x", summaries=[good], started_at="", finished_at=""
    )
    assert report_bad["trustworthy"] is False
    assert [item["task_id"] for item in report_bad["hard_failures"]] == ["t1"]
    assert report_good["trustworthy"] is True


def test_missing_metrics_stay_null_instead_of_zero() -> None:
    """缺的指标记 `None`：`0` 会被读成"没发生"，而"没采集到"与"没发生"是两件事。"""
    task = _task()
    results = [{"id": "f", "type": "file_exists", "ok": True, "status": bench.STATUS_PASS, "detail": ""}]
    summary = bench.summarize_task(task, results, tree_status="done", metrics={"agent_starts": 3})
    metrics = summary["metrics"]
    assert metrics["agent_starts"] == 3
    assert metrics["tokens_in"] is None
    assert metrics["human_interventions"] is None
    assert set(bench.METRIC_KEYS).issubset(metrics)


def test_compare_table_warns_when_the_exam_changed() -> None:
    task = _task()
    passed = [{"id": "f", "type": "file_exists", "ok": True, "status": bench.STATUS_PASS, "detail": ""}]
    summary = bench.summarize_task(task, passed, tree_status="done", metrics={"agent_starts": 2, "tokens_in": 10})
    same = bench.build_report(
        provider="mock", task_set_hash_value="sha256:a", summaries=[summary], started_at="", finished_at=""
    )
    other = dict(same, provider="real", task_set_hash="sha256:b")

    table = bench.compare([same, other])
    assert "警告" in table and "task_set_hash" in table
    assert "| mock |" in table and "| real |" in table
    # 判不了的行数要单独说，不能混进成功率
    assert "没判定成" not in table


def test_compare_flags_unjudged_checks() -> None:
    task = _task()
    unjudged = [{"id": "f", "type": "file_exists", "ok": False, "status": bench.STATUS_ERROR, "detail": ""}]
    summary = bench.summarize_task(task, unjudged, tree_status="blocked", metrics={})
    report = bench.build_report(
        provider="mock", task_set_hash_value="sha256:a", summaries=[summary], started_at="", finished_at=""
    )
    assert "没判定成" in bench.compare([report])


def test_reports_roundtrip_as_json() -> None:
    task = _task()
    summary = bench.summarize_task(task, [], tree_status="timeout", metrics={}, notes="超时")
    report = bench.build_report(
        provider="mock", task_set_hash_value="sha256:a", summaries=[summary], started_at="", finished_at=""
    )
    restored = json.loads(json.dumps(report, ensure_ascii=False))
    assert restored["tasks"][0]["tree_status"] == "timeout"
    assert restored["tasks"][0]["notes"] == "超时"
