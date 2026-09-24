"""仓库级门禁本身的回归测试：**门禁的范围不许安静地变弱**。

这条测试的来历是一次真事故：`scripts/bench_run.py` 里一个未定义的名字（`Path`）逃过了整条发布门禁，
因为门禁只从 `api/` 里跑 `ruff check .`——仓库根的 `scripts/` 谁都没检查过。后果是连续两轮真实基准
都在"判据求值之前"崩掉，连报告一起带走，而所有门禁全程是绿的。

所以这里钉两件事：
1. 静态检查**必须覆盖仓库根的 `scripts/`**（覆盖范围由 `verify_all.LINT_PATHS` 单一来源决定，
   测试断言的是"用它拼出来的命令"，不是源码文本）；
2. **中途报告（`partial=true`）不许被当结论用**：对照工具必须拒绝，因为它的合计只覆盖已跑完的任务。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name: str, path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载不了 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lint_command_covers_repo_scripts() -> None:
    """门禁命令必须同时覆盖 `api/` 与仓库根的 `scripts/`。

    反面自检：这条断言会不会恒真？——`LINT_PATHS` 若被改成只剩 `api`，这里就会红；
    而"少一个目录"这种改动不会让门禁报错，只会让它安静地变弱，所以必须由测试来盯。
    """
    verify_all = _load("verify_all_under_test", ROOT / "scripts" / "verify_all.py")
    command = verify_all.lint_command()
    targets = {pathlib.Path(part).resolve() for part in command}
    assert (ROOT / "api").resolve() in targets
    assert (ROOT / "scripts").resolve() in targets


def test_optional_schema_fields_allowlist_stays_minimal() -> None:
    """「声明即必填」的**唯一例外表**不许静默长大。

    例外本身有理由：`planner-decision` 的 `block_report` 只在 `decision=blocked` 时才该出现，
    强制必填会逼真实 CLI 每次回一个 `null`（模型常省略 → schema 校验失败 → 整轮调用失败）。
    但例外表一旦能被随手加项，"声明即必填"这条门禁就被蛀空了，所以把它钉死在这里：
    加一条就要改这条测试，改的人必须自己解释一遍理由。
    """
    verify_all = _load("verify_all_optional_fields", ROOT / "scripts" / "verify_all.py")
    assert verify_all.OPTIONAL_SCHEMA_FIELDS == {"planner-decision.schema.json": {"block_report"}}
    schema = json.loads((ROOT / "schemas" / "planner-decision.schema.json").read_text(encoding="utf-8"))
    assert "block_report" in schema["properties"]
    assert "block_report" not in schema["required"]


def test_write_report_marks_midrun_report_partial(tmp_path: pathlib.Path) -> None:
    """默认落盘 = 中途读数（`partial=true`）；只有显式声明才是正式报告。

    为什么要有这个标志：被外部掐掉的进程会留下一份"看着完整"的 json，而它的合计只算了几行。
    机器可读的标记比在 `notes` 里写句中文可靠——注释会被读成结论，字段不会。
    """
    bench_run = _load("bench_run_under_test", ROOT / "scripts" / "bench_run.py")
    path = tmp_path / "report.json"
    bench_run.write_report(path, provider="mock", tasks=[], summaries=[], started_at="s0", notes="中途")
    assert json.loads(path.read_text(encoding="utf-8"))["partial"] is True
    bench_run.write_report(
        path, provider="mock", tasks=[], summaries=[], started_at="s0", notes="收尾", partial=False
    )
    assert json.loads(path.read_text(encoding="utf-8"))["partial"] is False
    # 原子落盘：心跳会反复重写这份文件，读到一个半截的 json 比没有报告更误导
    assert not (tmp_path / "report.json.tmp").exists()


def test_compare_refuses_partial_report(tmp_path: pathlib.Path, capsys: Any) -> None:
    """中途报告参加对照 → **拒绝**（非 0 退出 + 说明原因），而不是打一张少算几行的表。"""
    bench_run = _load("bench_run_under_test2", ROOT / "scripts" / "bench_run.py")
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps({"provider": "mock", "partial": True, "tasks": [], "totals": {}}), encoding="utf-8"
    )
    assert bench_run.compare_reports([str(path)]) == 2
    assert "拒绝对照" in capsys.readouterr().err


def test_rebuild_index_script_validates_the_asked_scope(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    """CLI 侧的"范围必须显式"和 HTTP 侧一样严：问错范围**不许静默给 0**。

    旧行为：`--status opne` 直接进 SQL（`status == 'opne'`）→ 0 行 → 打印 `total: 0`，
    与"真的没有债"长得一模一样。HTTP 那侧问错范围是 422，运维最常走的 CLI 这侧不能更松。
    """
    script = _load("rebuild_index_under_test", ROOT / "scripts" / "rebuild_obligation_index.py")
    monkeypatch.setattr(sys, "argv", ["rebuild_obligation_index.py", "--by-type", "--status", "opne"])
    with pytest.raises(SystemExit) as rejected:
        script.main()
    assert rejected.value.code == 2
    assert "--status" in capsys.readouterr().err

    # 反面自检：合法范围必须照常放行，否则上面那条断言只是证明"什么都拒"。
    monkeypatch.setattr(sys, "argv", ["rebuild_obligation_index.py", "--by-type", "--status", "settled"])
    assert script.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "settled"


def test_rebuild_index_script_defaults_to_the_product_database() -> None:
    """脚本的默认库必须**就是**产品的默认库，否则它会安静地去问一个空库。

    实测 2026-09-24：脚本默认写成 `workspace/codex_battlefield.sqlite3`，而 `app.config` 的默认是
    `workspace/battlefield.sqlite3` → 不带环境变量跑 `--check`，得到 `projected: 0 / in_sync: true`，
    读起来正是"索引干净、一条债都没有"。**唯一的写入口自己报了个假全绿**，方向正好反。
    断言比的是两边算出来的 URL，不是源码文本（改名了也照样盯得住）。
    """
    monkeypatch_env = os.environ.pop("CODEX_BATTLEFIELD_DATABASE_URL", None)
    try:
        script = _load("rebuild_index_db_default", ROOT / "scripts" / "rebuild_obligation_index.py")
        script.prepare_imports()
        script_url = os.environ["CODEX_BATTLEFIELD_DATABASE_URL"]
    finally:
        os.environ.pop("CODEX_BATTLEFIELD_DATABASE_URL", None)
        if monkeypatch_env is not None:
            os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = monkeypatch_env

    from app.config import WORKSPACE_DIR

    assert script_url == f"sqlite:///{(WORKSPACE_DIR / 'battlefield.sqlite3').as_posix()}"
