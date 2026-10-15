"""旧布局 → 新布局的迁移判据（`docs/spec-state-project-decoupling.md` §10）。

最要紧的一条是 **worktree_path 重写**（§11-H7）：`TaskNode.worktree_path` 是持久化列，
值里带旧 worktree 根。不重写不会报错，但 `worktrees.is_managed_worktree`
（**删除守卫**）会与实际路径不一致——挡住清理，或者更糟：**删错东西**。

所以这里的判据不是"字符串被换了"，而是**换完之后守卫还认不认这些路径**。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from app import migration, worktrees


@pytest.fixture()
def home(tmp_path: pathlib.Path, monkeypatch) -> pathlib.Path:
    target = tmp_path / "state-home"
    target.mkdir()
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(target))
    return target


def _make_legacy_repo(tmp_path: pathlib.Path, worktree_ids: list[str]) -> pathlib.Path:
    """造一个带旧库的仓库骨架。"""
    repo = tmp_path / "repo"
    workspace = repo / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "worktrees").mkdir()

    db = workspace / "battlefield.sqlite3"
    con = sqlite3.connect(str(db))
    try:
        con.execute("CREATE TABLE task_nodes (id TEXT PRIMARY KEY, worktree_path TEXT)")
        con.execute("CREATE TABLE constitutions (id TEXT PRIMARY KEY, target_path TEXT)")
        for node_id in worktree_ids:
            con.execute(
                "INSERT INTO task_nodes (id, worktree_path) VALUES (?, ?)",
                (node_id, str(workspace / "worktrees" / node_id)),
            )
        con.execute(
            "INSERT INTO constitutions (id, target_path) VALUES (?, ?)",
            ("const-1", str(workspace / "projects" / "one-shot-run")),
        )
        con.commit()
    finally:
        con.close()
    return repo


# ---------- 不猜工程根 ----------


def test_insider_target_path_is_marked_unusable(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """`workspace/projects/...` 这种一次性目录**不能**当工程根，且要说清为什么。"""
    repo = _make_legacy_repo(tmp_path, ["node-a"])
    plan = migration.build_plan(repo)
    assert plan is not None

    by_source = {item.source: item for item in plan.candidates}
    insider = by_source["constitutions.target_path"]
    assert insider.usable is False
    assert "一次性" in insider.reason or "内部" in insider.reason


def test_repo_root_is_offered_as_a_candidate(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    repo = _make_legacy_repo(tmp_path, ["node-a"])
    plan = migration.build_plan(repo)
    assert plan is not None
    assert any(item.path == repo and item.usable for item in plan.candidates)


def test_build_plan_reports_counts(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    repo = _make_legacy_repo(tmp_path, ["node-a", "node-b"])
    plan = migration.build_plan(repo)
    assert plan is not None
    assert plan.task_count == 2
    assert plan.worktree_rows == 2


def test_build_plan_returns_none_without_a_legacy_db(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    assert migration.build_plan(repo) is None


# ---------- 迁移本体 ----------


def test_migrate_copies_and_rewrites_worktrees(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """核心判据：迁完之后每个 worktree 路径都还能过**删除守卫**。

    这才是有意义的断言——"字符串换了"不说明守卫认它。
    """
    repo = _make_legacy_repo(tmp_path, ["node-a", "node-b"])
    legacy_db = repo / "workspace" / "battlefield.sqlite3"
    before_sha = migration.sha256_file(legacy_db)

    result = migration.migrate(repo_root=repo, project_root=repo)

    assert result.copied is True
    assert result.worktree_rows_rewritten == 2
    assert result.project.db_path.is_file()

    # 旧库必须**原样还在**（复制，不移动）
    assert legacy_db.is_file()
    assert migration.sha256_file(legacy_db) == before_sha

    con = sqlite3.connect(str(result.project.db_path))
    try:
        rows = con.execute("SELECT id, worktree_path FROM task_nodes ORDER BY id").fetchall()
    finally:
        con.close()

    for node_id, raw in rows:
        assert raw, f"{node_id} 的 worktree_path 被弄空了"
        assert "workspace" not in pathlib.Path(raw).parts, f"{node_id} 仍指向旧工作区：{raw}"
        # 关键：守卫必须认
        assert worktrees.is_managed_worktree(result.project.project_dir, raw) is True, (
            f"{node_id} 的 worktree 路径过不了删除守卫：{raw}\n"
            f"  守卫根是 {result.project.project_dir}"
        )


def test_migrate_is_reentrant(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """重复跑不许产生第二份副本（按 sha256 判断）。"""
    repo = _make_legacy_repo(tmp_path, ["node-a"])

    first = migration.migrate(repo_root=repo, project_root=repo)
    assert first.copied is True

    second = migration.migrate(repo_root=repo, project_root=repo)
    assert second.copied is False
    assert "已经迁过" in second.skipped_reason
    assert second.project.key == first.project.key
    # 仍然只有一个工程目录
    projects = list((home / "projects").iterdir())
    assert len(projects) == 1, f"重复迁移产生了多个工程目录：{projects}"


def test_migrate_writes_a_marker_with_provenance(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    repo = _make_legacy_repo(tmp_path, ["node-a"])
    result = migration.migrate(repo_root=repo, project_root=repo)

    marker = json.loads((result.project.project_dir / migration.MIGRATION_MARKER).read_text(encoding="utf-8"))
    assert marker["source_sha256"] == result.source_sha256
    assert marker["old_worktree_root"].endswith("workspace\\worktrees") or marker[
        "old_worktree_root"
    ].endswith("workspace/worktrees")
    assert marker["new_worktree_root"] == str(result.project.project_dir / "worktrees")


def test_migrate_carries_the_isolated_codex_home(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """Hub 的隔离 CODEX_HOME 必须一起搬，否则重演"provider 配置丢失 -> 401"。"""
    repo = _make_legacy_repo(tmp_path, ["node-a"])
    legacy_home = repo / "workspace" / "codex-home"
    legacy_home.mkdir()
    (legacy_home / "config.toml").write_text('model_provider = "mygw"\n', encoding="utf-8")

    result = migration.migrate(repo_root=repo, project_root=repo)

    assert result.codex_home_copied is True
    copied = result.project.project_dir / "codex-home" / "config.toml"
    assert copied.is_file()
    assert "mygw" in copied.read_text(encoding="utf-8")


def test_migrate_without_a_legacy_db_raises(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    with pytest.raises(FileNotFoundError):
        migration.migrate(repo_root=repo, project_root=repo)


def test_rewrite_is_idempotent(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """重写两次不该继续改动（第二次没有旧前缀可匹配）。"""
    repo = _make_legacy_repo(tmp_path, ["node-a"])
    result = migration.migrate(repo_root=repo, project_root=repo)

    old_root = repo / "workspace" / "worktrees"
    new_root = result.project.project_dir / "worktrees"
    again = migration.rewrite_worktree_paths(result.project.db_path, old_root=old_root, new_root=new_root)

    assert again == 0, "重写不幂等：第二次仍然改动了行"


def test_rewrite_leaves_foreign_paths_alone(tmp_path: pathlib.Path, home: pathlib.Path) -> None:
    """不匹配旧根的路径必须原样保留（不能误改成别的工程的位置）。"""
    repo = _make_legacy_repo(tmp_path, [])
    db = repo / "workspace" / "battlefield.sqlite3"
    foreign = r"D:\somewhere\else\worktrees\node-x"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "INSERT INTO task_nodes (id, worktree_path) VALUES (?, ?)", ("node-x", foreign)
        )
        con.commit()
    finally:
        con.close()

    changed = migration.rewrite_worktree_paths(
        db, old_root=repo / "workspace" / "worktrees", new_root=tmp_path / "new"
    )
    assert changed == 0

    con = sqlite3.connect(str(db))
    try:
        raw = con.execute("SELECT worktree_path FROM task_nodes WHERE id='node-x'").fetchone()[0]
    finally:
        con.close()
    assert raw == foreign


# ---------- 两个实测踩到的 bug 的回归 ----------


def test_migration_survives_a_legacy_db_without_task_nodes(
    tmp_path: pathlib.Path, home: pathlib.Path
) -> None:
    """旧库可能**根本没有 `task_nodes`**。

    实测报错：对刚复制的空库直接改写 -> `no such table: task_nodes`。
    这正是"为什么必须先 ensure_schema 再改写"的判据。
    """
    repo = tmp_path / "repo"
    workspace = repo / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "worktrees").mkdir()
    # 造一个**空** SQLite（一个业务表都没有）
    sqlite3.connect(str(workspace / "battlefield.sqlite3")).close()

    result = migration.migrate(repo_root=repo, project_root=repo)

    assert result.copied is True
    assert result.worktree_rows_rewritten == 0
    con = sqlite3.connect(str(result.project.db_path))
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "task_nodes" in tables, "迁移后目标库没有建出 task_nodes —— ensure_schema 没生效"


def test_migration_keeps_all_rows_and_non_null_worktrees(
    tmp_path: pathlib.Path, home: pathlib.Path
) -> None:
    """行数与 `worktree_path` 的非空数量，迁移前后必须一致。

    **如实说明本用例的射程**：`ensure_schema` 去掉后它**仍然通过**
    （源库自带 `task_nodes`，改写照样能跑），所以它**不是**那个建表顺序 bug 的
    证伪用例——真正的证伪是
    `test_migration_survives_a_legacy_db_without_task_nodes`（去掉修复即 FAIL）。

    留着它的理由是另一件事：它挡"改写把行弄丢/把 worktree_path 弄成 NULL"这类
    回归。当初之所以要写，是因为 CLI 只报"worktree 重写: 0 行"，
    看起来像正常完成——所以判据必须是"行还在不在"，而不是"有没有报错"。
    """
    ids = ["node-a", "node-b", "node-c"]
    repo = _make_legacy_repo(tmp_path, ids)

    before = sqlite3.connect(str(repo / "workspace" / "battlefield.sqlite3"))
    count_before = before.execute("SELECT COUNT(*) FROM task_nodes").fetchone()[0]
    before.close()

    result = migration.migrate(repo_root=repo, project_root=repo)

    after = sqlite3.connect(str(result.project.db_path))
    try:
        count_after = after.execute("SELECT COUNT(*) FROM task_nodes").fetchone()[0]
        worktrees = after.execute(
            "SELECT COUNT(*) FROM task_nodes WHERE worktree_path IS NOT NULL"
        ).fetchone()[0]
    finally:
        after.close()

    assert count_before == 3
    assert count_after == count_before, f"迁移丢了行：{count_before} -> {count_after}"
    assert worktrees == 3, "有节点的 worktree_path 变成 NULL 了"
    assert result.worktree_rows_rewritten == 3
