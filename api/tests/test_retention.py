"""数据保留的判据（P5-4）。

ROADMAP 的判据是两句：**"清理后崩溃恢复仍可用"** 与 **"有保留窗口配置"**。
第一句是本文件的重点，因为它最容易做成"看起来干净了，其实恢复不了"。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| R1 | 每棵树**最新那份快照永不清理** | 表被清空时真的没得还原 —— 而那时离清理已经很久 |
| R2 | 计划里**必须列出受保护项及理由** | "我没删掉不该删的"变成一句无法核对的承诺 |
| R3 | 库文件与库里的行**永不进候选** | 任务树/宪法/债务被清掉，不可逆 |
| R4 | 工作树**不归这里管** | 两套清理规则作用于同一目录，"谁删的"查不清 |
| R5 | 保留窗口可配、边界明确（0 合法，负数/超上限拒绝） | 配置错了没人拦 |
| R6 | 保留期内的东西**不进候选** | 刚写完的日志被删 |
| R7 | 删除**默认是预演**（dry-run） | 一次误点不可逆 |
| R8 | 读不到修改时间 → 进 `unknown`，**不猜** | 猜"它大概很旧了"会删掉正在用的东西 |
| R9 | **清理之后快照还原仍然成功** | 这就是那条判据本身 |
"""
from __future__ import annotations

import json
import os
import pathlib
import time
from datetime import datetime, timezone

import pytest

from app import retention

# ⚠️ `NOW` 必须**跟着真实时钟走**：夹具用真实 mtime（`_age` 用 `time.time() - days*86400`），
# 而这里是 `now=NOW` 注入的"当前时刻"。曾经写死成 `datetime(2026, 10, 16, 12, 0)` ——
# 到了 **2026-10-17 晚上**，"8 天前"的 mtime 只比 `NOW - 7 天` 早不到 1 天，R6 的边界判据
# 从绿变成**稳定红**（不是 flaky，是判据到期了：写死的时间会过期）。
# 那一轮的真实报错：`assert inside not in removable` 通过、`assert outside in removable` 红
# ——因为 `outside` 的 mtime 落在了 cutoff 之后。
# 用真实时钟之后，两侧余量（6 天 vs 8 天，窗口 7 天）与设计意图一致，且**不会过期**。
NOW = datetime.now(timezone.utc)


def _age(path: pathlib.Path, *, days: float) -> None:
    """把文件/目录的 mtime 设成 `days` 天前。**用真实 mtime**，不改实现里的时钟读取。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    for child in path.rglob("*") if path.is_dir() else []:
        os.utime(child, (stamp, stamp))


@pytest.fixture()
def project(tmp_path: pathlib.Path) -> pathlib.Path:
    """一个够真实的工程状态目录骨架。"""
    root = tmp_path / "projects" / "demo"
    for name in retention.SWEEPABLE_DIRS:
        (root / name).mkdir(parents=True)
    (root / retention.ANCHOR_DIR / "root-1").mkdir(parents=True)
    (root / "worktrees").mkdir()
    (root / "battlefield.sqlite3").write_text("sqlite", encoding="utf-8")
    (root / "project.json").write_text("{}", encoding="utf-8")
    return root


# ---------- R1 锚点永不清 ----------


def test_r1_the_newest_snapshot_per_root_is_protected(project: pathlib.Path) -> None:
    """**这条是整层的核心**：锚点删了，表被清空时就真的没得还原。"""
    snapshots = project / retention.ANCHOR_DIR / "root-1"
    old = snapshots / "2026-10-01T00-00-00-tree.snapshot.json"
    anchor = snapshots / "2026-10-15T00-00-00-tree.full_checkpoint.json"
    old.write_text("{}", encoding="utf-8")
    anchor.write_text("{}", encoding="utf-8")
    _age(old, days=30)
    _age(anchor, days=30)  # **连锚点也很旧** —— 它仍然不能被删

    result = retention.plan(project_dir=project, retention_days=1, now=NOW)

    removable = {item.path for item in result.removable}
    protected = {item.path for item in result.protected}
    assert anchor in protected, "最新的快照必须受保护，哪怕它很旧"
    assert old in removable
    assert anchor not in removable


def test_r1_each_root_keeps_its_own_anchor(project: pathlib.Path) -> None:
    """多棵树各有各的锚点 —— 只看全局最新会让别的树失去锚。"""
    for root_id in ("root-1", "root-2"):
        target = project / retention.ANCHOR_DIR / root_id
        target.mkdir(parents=True, exist_ok=True)
        for index in range(3):
            item = target / f"2026-10-0{index + 1}T00-00-00-snap.json"
            item.write_text("{}", encoding="utf-8")
            _age(item, days=30)

    result = retention.plan(project_dir=project, retention_days=1, now=NOW)
    # ⚠️ 按**路径**数，不按文件名：两棵树的最新快照同名（`2026-10-03...`），
    # 用名字去重会把 2 份数成 1 份（第一版就是这么错的）。
    protected_snapshots = [
        item.path for item in result.protected if item.path.name.endswith("snap.json")
    ]
    assert len(protected_snapshots) == 2, protected_snapshots
    assert {item.parent.name for item in protected_snapshots} == {"root-1", "root-2"}
    assert len([item for item in result.removable if item.kind == "state_snapshot"]) == 4


# ---------- R2 受保护项要列出来 ----------


def test_r2_protected_items_carry_reasons(project: pathlib.Path) -> None:
    """"我没删掉不该删的"必须是可核对的，而不是一句承诺。"""
    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    assert result.protected, "计划里没有受保护清单 —— 那句话就无从核对"
    for item in result.protected:
        assert item.reason, f"{item.path} 没有说明为什么受保护"
        assert len(item.reason) > 5


def test_r2_payload_shape_is_complete(project: pathlib.Path) -> None:
    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    body = result.payload()
    assert set(body) >= {
        "removable",
        "protected",
        "unknown",
        "removable_count",
        "removable_bytes",
        "protected_count",
    }
    assert body["removable_count"] == len(body["removable"])
    assert body["removable_bytes"] == result.bytes_total


# ---------- R3 库永不进候选 ----------


def test_r3_the_database_is_protected_not_removable(project: pathlib.Path) -> None:
    _age(project / "battlefield.sqlite3", days=999)
    result = retention.plan(project_dir=project, retention_days=1, now=NOW)
    removable = {item.path.name for item in result.removable}
    assert "battlefield.sqlite3" not in removable
    assert any(item.path.name == "battlefield.sqlite3" for item in result.protected)


def test_r3_every_precious_table_is_declared_never_truncated() -> None:
    """表行永不清理 —— 而且这个清单要**包含**所有承载"删了就没有"的数据的表。"""
    for table in (
        "task_nodes",
        "constitutions",
        "event_records",
        "steward_messages",
        "steward_actions",
        "goal_deliveries",
        "incident_conclusions",
    ):
        assert table in retention.NEVER_TRUNCATE_TABLES, table


def test_r3_there_is_no_code_that_deletes_rows() -> None:
    """**源码级判据**：本模块不许出现删行/删表的 SQL。

    行为测试只能覆盖它跑到的那些路径；而"某天有人加了一句清理行的代码"
    是最容易悄悄发生的退步。写法很土，但它挡的正是这类退步。

    ⚠️ 判据里**不能**直接找 `TRUNCATE` 这个词：本文件的常量就叫
    `NEVER_TRUNCATE_TABLES`，第一版因此在常量名上误报。
    找的是"真的会删东西的那几种写法"。
    """
    source = pathlib.Path(retention.__file__).read_text(encoding="utf-8")
    for forbidden in ("DELETE FROM", "DROP TABLE", "TRUNCATE TABLE", "delete(", "drop_all"):
        assert forbidden not in source, f"保留模块里出现了 {forbidden} —— 表行永不该被清理"


def test_r3_tables_report_lists_rows_for_every_precious_table(tmp_path: pathlib.Path) -> None:
    """`tables_report` 能读行数（判据用它证明"没被清理"），表不存在时如实给 None。"""
    import sqlite3

    db = tmp_path / "x.sqlite3"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE task_nodes (id TEXT)")
        con.execute("INSERT INTO task_nodes VALUES ('a')")
        con.commit()
        rows = retention.tables_report(con)
    finally:
        con.close()

    by_table = {item["table"]: item["rows"] for item in rows}
    assert by_table["task_nodes"] == 1
    # 不存在的表给 None，**不给 0** —— 0 会被读成"表是空的"。
    assert by_table["constitutions"] is None


# ---------- R4 不碰工作树 ----------


def test_r4_worktrees_are_protected_and_explicitly_out_of_scope(project: pathlib.Path) -> None:
    stale = project / "worktrees" / "node-old"
    stale.mkdir()
    (stale / "file.txt").write_text("x", encoding="utf-8")
    _age(stale, days=999)

    result = retention.plan(project_dir=project, retention_days=1, now=NOW)
    removable = {str(item.path) for item in result.removable}
    assert not any("worktrees" in path for path in removable), "工作树不该由保留策略清理"
    worktree_protection = [item for item in result.protected if item.path.name == "worktrees"]
    assert worktree_protection, "工作树目录应当出现在受保护清单里"
    assert "worktrees.decide" in worktree_protection[0].reason


def test_r4_sweepable_dirs_do_not_include_worktrees() -> None:
    assert "worktrees" not in retention.SWEEPABLE_DIRS
    assert retention.ANCHOR_DIR == "state_snapshots"


# ---------- R5 窗口配置 ----------


@pytest.mark.parametrize(("value", "expected"), [(0, 0), (7, 7), ("30", 30), (365, 365)])
def test_r5_valid_windows_are_accepted(value, expected) -> None:
    assert retention.normalize_days(value) == expected


@pytest.mark.parametrize("value", [-1, -100, 366, 10000])
def test_r5_invalid_windows_are_refused(value) -> None:
    with pytest.raises(retention.RetentionError):
        retention.normalize_days(value)


def test_r5_non_numeric_windows_are_refused() -> None:
    with pytest.raises(retention.RetentionError):
        retention.normalize_days("很久")


def test_r5_zero_window_still_protects_the_anchor(project: pathlib.Path) -> None:
    """窗口为 0 = "只留锚点"，**不是**"什么都不留"。

    ⚠️ 用**远早于窗口**的年龄（30 天 vs 窗口 0..1 天），不要卡在边界上：
    夹具用真实 mtime，而 "30 天前" 与 `now=NOW` 差着几小时（NOW 早于真实当前时间），
    卡边界会让判据取决于跑测试的时刻（第一版卡在 1 天 / 7 天的边界上，实测红）。
    边界本身另有一条判据专门钉。
    """
    snapshots = project / retention.ANCHOR_DIR / "root-1"
    # ⚠️ 名字要能体现**时间序**：锚点是"名字最大"的那份（快照文件名以时间戳开头）。
    # 第一版用了 `old.json` / `new.json`，按字典序 `old` > `new`，于是锚点变成了
    # 我以为的"历史快照" —— 判据看着在测锚点保护，其实测的是另一份。
    old = snapshots / "2026-01-01T00-00-00-snap.json"
    anchor = snapshots / "2026-10-15T00-00-00-snap.json"
    old.write_text("{}", encoding="utf-8")
    anchor.write_text("{}", encoding="utf-8")
    _age(old, days=30)
    _age(anchor, days=30)  # 锚点也很旧 —— 仍然必须受保护

    result = retention.plan(project_dir=project, retention_days=0, now=NOW)
    assert anchor in {item.path for item in result.protected}
    assert old in {item.path for item in result.removable}


# ---------- R6 保留期内不动 ----------


def test_r6_recent_artifacts_are_not_candidates(project: pathlib.Path) -> None:
    fresh = project / "runs" / "recent-run"
    fresh.mkdir()
    (fresh / "logs.jsonl").write_text("{}", encoding="utf-8")
    _age(fresh, days=1)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    assert fresh not in {item.path for item in result.removable}


def test_r6_old_artifacts_are_candidates_with_a_reason(project: pathlib.Path) -> None:
    stale = project / "runs" / "old-run"
    stale.mkdir()
    (stale / "logs.jsonl").write_text("x" * 2048, encoding="utf-8")
    _age(stale, days=30)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    match = next((item for item in result.removable if item.path == stale), None)
    assert match is not None
    assert match.kind == "runs"
    assert match.bytes >= 2048
    assert "可重建" in match.reason


def test_r6_the_cutoff_boundary_is_inclusive_of_the_window(project: pathlib.Path) -> None:
    """保留期**之内**（含刚好等于）的东西不被清理，更早的才清。

    这条用**明确的相对年龄**（6 天 vs 8 天，窗口 7 天）而不是卡在 7.0 天上：
    夹具用真实 mtime，而 `now=NOW` 早于真实当前时间几小时，卡边界会让判据
    取决于跑测试的时刻。边界本身的意义是"窗口之内保留"，用两侧各差一天来表达。
    """
    inside = project / "runs" / "inside-window"
    inside.mkdir()
    _age(inside, days=6)
    outside = project / "runs" / "outside-window"
    outside.mkdir()
    _age(outside, days=8)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    removable = {item.path for item in result.removable}
    assert inside not in removable, "保留期内的目录被列进候选了"
    assert outside in removable


# ---------- R7 默认预演 ----------


def test_r7_sweep_defaults_to_dry_run_and_deletes_nothing(project: pathlib.Path) -> None:
    stale = project / "runs" / "old-run"
    stale.mkdir()
    (stale / "a.txt").write_text("x", encoding="utf-8")
    _age(stale, days=30)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    assert result.removable, "夹具没有造出可清理项 —— 这条判据会空转"

    report = retention.sweep(result)  # 默认 dry_run=True
    assert report["dry_run"] is True
    assert report["removed_count"] == 0
    assert stale.is_dir(), "预演删了东西"


def test_r7_sweep_with_dry_run_false_removes_only_the_plan(project: pathlib.Path) -> None:
    stale = project / "runs" / "old-run"
    stale.mkdir()
    (stale / "a.txt").write_text("x" * 1024, encoding="utf-8")
    _age(stale, days=30)
    keep = project / "runs" / "fresh-run"
    keep.mkdir()
    _age(keep, days=1)
    anchor = project / retention.ANCHOR_DIR / "root-1" / "anchor.json"
    anchor.write_text("{}", encoding="utf-8")
    _age(anchor, days=30)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    report = retention.sweep(result, dry_run=False)

    assert report["dry_run"] is False
    assert not stale.exists()
    assert keep.is_dir(), "保留期内的目录被删了"
    assert anchor.is_file(), "锚点被删了"
    assert report["freed_bytes"] >= 1024
    assert report["planned_count"] == report["removed_count"]


# ---------- R8 拿不到时间不猜 ----------


def test_r8_unreadable_timestamps_land_in_unknown(project: pathlib.Path, monkeypatch) -> None:
    """拿不到修改时间时**不猜** —— 猜"它大概很旧了"会删掉正在用的东西。"""
    stale = project / "runs" / "mystery"
    stale.mkdir()
    (stale / "a.txt").write_text("x", encoding="utf-8")

    original = retention._modified

    def _flaky(path: pathlib.Path):
        if path.name == "mystery":
            return None
        return original(path)

    monkeypatch.setattr(retention, "_modified", _flaky)
    result = retention.plan(project_dir=project, retention_days=7, now=NOW)

    assert any("mystery" in text for text in result.unknown)
    assert stale not in {item.path for item in result.removable}


# ---------- R9 清理之后还原仍然成功 ----------


def test_r9_snapshot_restore_still_works_after_a_sweep(project: pathlib.Path) -> None:
    """**这是 ROADMAP 那条判据本身**："清理后崩溃恢复仍可用"。

    做法：造一份**真的能用的**快照（含 tasks/edges/events/stats），再放几个可清理项，
    清理之后从快照里读回那棵树 —— 必须一模一样。

    为什么必须**真的读回来**：只看"锚点文件还在"是自欺 —— 它可能在，
    但已经读不出内容（比如清理顺手改了权限，或者把同目录的兄弟文件删得让它解析失败）。
    """
    root_id = "root-1"
    snapshots = project / retention.ANCHOR_DIR / root_id
    payload = {
        "snapshot_version": 1,
        "root_id": root_id,
        "reason": "test",
        "created_at": "2026-10-16T00:00:00+00:00",
        "stats": {"total": 2, "done": 1},
        "tasks": [
            {"id": "root-1", "status": "done", "depth": 0},
            {"id": "root-1-a", "status": "done", "depth": 1},
        ],
        "edges": [{"source": "root-1", "target": "root-1-a"}],
        "events": [],
    }
    anchor = snapshots / "2026-10-15T00-00-00-tree.full_checkpoint.json"
    anchor.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    older = snapshots / "2026-10-01T00-00-00-tree.snapshot.json"
    older.write_text(json.dumps({"tasks": []}), encoding="utf-8")
    _age(anchor, days=30)
    _age(older, days=30)

    for name in ("runs", "full_checkpoints", "checkpoint_ledger", "codex-home"):
        item = project / name / "old"
        item.mkdir()
        (item / "x").write_text("y" * 512, encoding="utf-8")
        _age(item, days=30)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    assert len(result.removable) >= 5, "可清理项不够，这条判据没有真正清理过东西"
    retention.sweep(result, dry_run=False)

    # 清理之后：锚点还在，且**读得回来**。
    assert anchor.is_file()
    restored = json.loads(anchor.read_text(encoding="utf-8"))
    assert restored == payload
    assert len(restored["tasks"]) == 2
    # 而更早的历史快照确实被清掉了（证明这次清理真的发生了）。
    assert not older.exists()


def test_r9_sweep_reports_failures_instead_of_hiding_them(project: pathlib.Path, monkeypatch) -> None:
    """删不掉时要**报出来** —— 静默跳过会让"清理过了"与"其实没清"看起来一样。"""
    stale = project / "runs" / "locked"
    stale.mkdir()
    (stale / "a.txt").write_text("x", encoding="utf-8")
    _age(stale, days=30)

    result = retention.plan(project_dir=project, retention_days=7, now=NOW)

    def _boom(_path: pathlib.Path) -> None:
        raise OSError("被占用")

    monkeypatch.setattr(retention, "_remove_tree", _boom)
    report = retention.sweep(result, dry_run=False)

    assert report["removed_count"] == 0
    assert report["failed"], "删不掉却没有任何记录"
    assert "被占用" in report["failed"][0]


# ---------- 摘要 ----------


def test_summary_is_readable(project: pathlib.Path) -> None:
    result = retention.plan(project_dir=project, retention_days=7, now=NOW)
    text = retention.summary(result, retention_days=7)
    assert "7 天" in text
    assert "受保护" in text
    assert "判不了" in text


def test_planning_an_empty_directory_does_not_crash(tmp_path: pathlib.Path) -> None:
    result = retention.plan(project_dir=tmp_path / "nothing-here", retention_days=7, now=NOW)
    assert result.removable == []
    assert isinstance(result.protected, list)
