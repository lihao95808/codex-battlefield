"""P6-4：弱模型/异构模型的**可用性实测**工具与记录（判据 W1–W9）。

这一组测试不动模型（不花钱、不需要凭据），它钉住的是**实测这件事本身可信**：

* 判据必须从 `schemas/planner-decision.schema.json` 里读契约，不能自己写一份 ——
  第一版把 `leaf_certainty` 当成 0–100 的百分比范围，于是把 deepseek-flash
  完全正确的 `300%` 判成了失败（`300%` 正是 planner 提示词与 schema 规定的
  "有把握一次做完"）。判据比契约更聪明 = 判据是错的。
* 报告必须显式写清"覆盖到什么"（`scope` / `not_covered`）—— 一次 planner 调用
  只能证明"会按契约输出规划"，**证明不了**"能改对文件"。
* 取不到的费用必须是 `null`，不是 `0`（0 会被读成"免费"）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe_weak_model_planner.py"
SCHEMA = json.loads((ROOT / "schemas" / "planner-decision.schema.json").read_text(encoding="utf-8"))


def _load_module():
    spec = importlib.util.spec_from_file_location("probe_weak_model_planner", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_module()
ENUMS = harness.schema_enums()


def _summary(**overrides) -> dict:
    base = {
        "ok": True,
        "exit_code": 0,
        "decision": "execute",
        "leaf_certainty": "300%",
        "children": [],
    }
    base.update(overrides)
    return base


# --- W1 契约来自 schema --------------------------------------------------------


def test_w1_contract_comes_from_the_schema_not_from_the_harness():
    assert ENUMS["decision"] == SCHEMA["properties"]["decision"]["enum"]
    assert ENUMS["leaf_certainty"] == SCHEMA["properties"]["leaf_certainty"]["enum"]
    # 这两个取值是实测会出现的形态，写死在这里是为了让"契约变了"这件事必须被看见。
    assert "300%" in ENUMS["leaf_certainty"]
    assert "not_sure" in ENUMS["leaf_certainty"]
    assert "decompose" in ENUMS["decision"]


def test_w1_source_does_not_hardcode_the_certainty_range():
    """防回归：不要再出现"0–100 才是合法"的那种自造判据。"""

    source = SCRIPT.read_text(encoding="utf-8")
    assert 'WIRE_API_REJECTED' not in source  # 无关代码不该被搬进来
    assert "超出 0–100" not in source
    assert "schema_enums" in source


# --- W2 合法的 planner 输出必须通过 --------------------------------------------


@pytest.mark.parametrize("certainty", ["300%", "not_sure"])
def test_w2_valid_certainty_passes(certainty):
    assert harness.judge(_summary(leaf_certainty=certainty), ENUMS) == []


def test_w2_deepseek_flash_real_output_passes():
    """实测记录：deepseek-flash 对"新增一个 markdown"给出的 execute + 300%。"""

    summary = _summary(decision="execute", leaf_certainty="300%")
    assert harness.judge(summary, ENUMS) == []


# --- W3 契约违规必须被抓住 -----------------------------------------------------


def test_w3_unknown_decision_is_rejected():
    failures = harness.judge(_summary(decision="split"), ENUMS)
    assert failures and "enum" in failures[0]


def test_w3_numeric_certainty_is_rejected():
    failures = harness.judge(_summary(leaf_certainty=95), ENUMS)
    assert any("leaf_certainty" in item for item in failures)


def test_w3_decompose_without_children_is_rejected():
    failures = harness.judge(_summary(decision="decompose", children=[]), ENUMS)
    assert any("children" in item for item in failures)


def test_w3_execute_with_children_is_rejected():
    failures = harness.judge(_summary(decision="execute", children=[{"title": "x"}]), ENUMS)
    assert any("children" in item for item in failures)


def test_w3_blocked_is_not_counted_as_usable():
    failures = harness.judge(_summary(decision="blocked"), ENUMS)
    assert failures, "blocked 是合法结论，但不能算'这档模型可用'"


def test_w3_cli_exit_code_wins_over_everything():
    failures = harness.judge(_summary(exit_code=1, decision="garbage"), ENUMS)
    assert len(failures) == 1
    assert "exit_code" in failures[0]


# --- W4 报告边界写死 -----------------------------------------------------------


def test_w4_script_declares_what_it_does_not_cover():
    source = SCRIPT.read_text(encoding="utf-8")
    for token in ("planner_contract_only", "worker_edits_files", "verifier_acceptance"):
        assert token in source, token


def test_w4_script_does_not_fabricate_cost():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "不推算金额" in source
    assert 'record["cost"] = 0' not in source


# --- W5 成本探针：取不到就是 null ----------------------------------------------


def test_w5_cost_probe_digs_paths_and_returns_none_when_missing():
    spec = importlib.util.spec_from_file_location("probe_model_cost", ROOT / "scripts" / "probe_model_cost.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    payload = {"a": {"b": {"total": "0.0001"}}, "usage": {"input_tokens": 5}}
    assert module.dig(payload, "a.b.total") == "0.0001"
    assert module.dig(payload, "usage.input_tokens") == 5
    assert module.dig(payload, "a.b.nope") is None
    assert module.dig(payload, "missing.path") is None


# --- W6 文档与 ROADMAP --------------------------------------------------------


def test_w6_weak_model_doc_exists_with_the_measured_table():
    doc = (ROOT / "docs" / "weak-model-usability.md").read_text(encoding="utf-8")
    for token in ("planner", "deepseek-flash", "300%", "没验", "worker"):
        assert token in doc, token


def test_w6_roadmap_records_the_measured_scope(tmp_path):
    text = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    assert "P6-4" in text
    assert "planner" in text
