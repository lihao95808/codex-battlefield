"""按角色分层模型档的回归测试（`docs/spec-verification-independence.md` §4，判据 V5/V6）。

**为什么值得单独钉**：分层是"用便宜模型干活、用强模型验收"的唯一入口。它一旦是死的
（env 设了但不生效、或者只对部分角色生效），人会在"以为已经分层"的前提下继续往下跑——
而这正是这个仓库最忌讳的那种缺陷：**接线看着在、实际是死的**。
所以这里断言的是**命令行里的模型名真的不一样**，而不是"配置读到了"。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner


def _settings(**overrides) -> Settings:
    base = {"workspace_dir": Path("workspace-test"), "codex_bin": "codex"}
    base.update(overrides)
    return Settings(**base)


def test_per_kind_model_overrides_the_global_one() -> None:
    settings = _settings(codex_model="global-model", codex_model_worker="cheap-model", codex_model_acceptance="strong-model")
    assert settings.model_for("worker") == "cheap-model"
    assert settings.model_for("acceptance") == "strong-model"
    # 没配的角色回退全局——不许"猜"一个别的
    assert settings.model_for("planner") == "global-model"
    assert settings.model_for("verifier") == "global-model"
    assert settings.model_for("constitution") == "global-model"
    # 未知 kind 也回退全局（不回退到某个角色的档）
    assert settings.model_for("nonsense") == "global-model"
    assert settings.model_for("") == "global-model"


def test_reasoning_effort_falls_back_to_the_global_one() -> None:
    settings = _settings(codex_reasoning_effort="medium", codex_reasoning_effort_acceptance="high")
    assert settings.reasoning_effort_for("acceptance") == "high"
    assert settings.reasoning_effort_for("worker") == "medium"


def test_exec_command_line_carries_a_different_model_per_kind(tmp_path: Path) -> None:
    """V5 的硬断言：两次调用的命令行里模型名必须**真的不同**。"""
    settings = _settings(codex_model="global-model", codex_model_worker="cheap-model", codex_model_acceptance="strong-model")
    runner = CodexExecRunner(settings)

    def args_for(kind: str) -> list[str]:
        return runner._codex_exec_args(tmp_path / "cwd", tmp_path / "s.json", tmp_path / "o.json", "workspace-write", kind)

    worker_args = args_for("worker")
    acceptance_args = args_for("acceptance")
    planner_args = args_for("planner")

    assert worker_args[worker_args.index("--model") + 1] == "cheap-model"
    assert acceptance_args[acceptance_args.index("--model") + 1] == "strong-model"
    assert planner_args[planner_args.index("--model") + 1] == "global-model"
    assert "--model" in worker_args and "--model" in acceptance_args


def test_exec_command_line_carries_the_per_kind_reasoning_effort(tmp_path: Path) -> None:
    settings = _settings(codex_reasoning_effort="medium", codex_reasoning_effort_worker="low")
    runner = CodexExecRunner(settings)
    worker_args = runner._codex_exec_args(tmp_path / "cwd", tmp_path / "s.json", tmp_path / "o.json", "workspace-write", "worker")
    acceptance_args = runner._codex_exec_args(tmp_path / "cwd", tmp_path / "s.json", tmp_path / "o.json", "workspace-write", "acceptance")
    assert 'model_reasoning_effort="low"' in worker_args
    assert 'model_reasoning_effort="medium"' in acceptance_args


def test_no_tier_configured_behaves_exactly_like_before(tmp_path: Path) -> None:
    """V6：什么都不配时与今天一致（不能因为加了分层就偷偷改了默认行为）。"""
    settings = _settings(codex_model="global-model")
    runner = CodexExecRunner(settings)
    for kind in ("", "worker", "verifier", "acceptance", "planner", "constitution"):
        args = runner._codex_exec_args(tmp_path / "cwd", tmp_path / "s.json", tmp_path / "o.json", "read-only", kind)
        assert args[args.index("--model") + 1] == "global-model", kind


def test_tier_warnings_report_facts_instead_of_ranking_models() -> None:
    """只报可自动判定的可疑之处，**不**给模型排强弱——名字里没有强度信息。"""
    assert _settings().model_tier_warnings() == []
    # worker 配了独立档、acceptance 还在全局档：典型的"以为分层了其实没分"
    mismatch = _settings(codex_model="global-model", codex_model_worker="cheap-model").model_tier_warnings()
    assert len(mismatch) == 1 and "acceptance" in mismatch[0]
    # 两边都配齐 = 期望的配比，没有可疑之处（沉默是这里正确的结果）
    assert (
        _settings(
            codex_model="global-model", codex_model_worker="cheap-model", codex_model_acceptance="strong-model"
        ).model_tier_warnings()
        == []
    )
    # 只有 acceptance 独立档（worker 走全局）也不算可疑，只是一句提示
    only_acceptance = _settings(codex_model="global-model", codex_model_acceptance="strong-model").model_tier_warnings()
    assert len(only_acceptance) == 1 and "期望的配比" in only_acceptance[0]


def test_tier_board_is_observable_without_a_database() -> None:
    """看板读数：`effective` 必须是**已解析**的（空档显示成"全局默认档"，不是空串）。"""
    board = Orchestrator(_settings(codex_model="global-model", codex_model_worker="cheap-model"))._model_tier_board()
    assert board["effective"]["worker"] == "cheap-model"
    assert board["effective"]["acceptance"] == "global-model"
    assert board["tiered"] == ["worker"]
    assert board["warnings"]

    untiered = Orchestrator(_settings())._model_tier_board()
    assert set(untiered["effective"]) == {"constitution", "planner", "worker", "verifier", "acceptance"}
    assert set(untiered["effective"].values()) == {"全局默认档"}
    assert untiered["tiered"] == []
    assert untiered["warnings"] == []
