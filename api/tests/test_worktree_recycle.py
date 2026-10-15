"""R3 worktree 回收的接线测试：**真的删**，且每条前置都单独钉住。

判据的纯逻辑在 `test_worktree_lifecycle.py`；这里测的是"接线真的按判据动手了"：

| 编号 | 要钉住的事 |
|---|---|
| 回收 | 终态 + 产物已归档 + 保留期已过 → 目录被删、`node_meta` 留痕、事件与计数都对 |
| 证据前置（缺失） | 声明产物在归档处找不到 → **不删**（spec §R3 风险条款），并给出 skip 原因 |
| 证据前置（同名旧文件） | 归档处有同名文件但内容还是旧的 → **不删**。这是 R3 最贵的一个缺口：节点改的多半是工程里本来就有的文件，只查"路径在不在"会判成"已归档"，删掉之后父节点集成时源已消失 |
| 父节点未定格 | 父节点还在跑（它的集成要读这个 worktree）→ **不删** |
| 保守 | `failed` 不删；保留期内不删 |
| 证据可读 | 回收之后，归档处的那份产物仍然可读（"证据在回收后仍可读"） |
| git 路径 | 真 git worktree：目录与 `codex/node-*` 分支都被回收 |
| 拷贝式路径 | 目标仓库是 git，但这个 worktree 是**退化拷贝目录**（没有 `.git`）→ 走删目录，不硬上 git |
| 深先浅后 | 父的 worktree 是子的归档处：同一次清扫必须**先子后父**，否则子永久不可回收 |
| 幂等 | 连跑两次不会重复回收、不会重复计数 |
| 可观测 | 生命周期看板能回答"为什么它还留着" |
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import shutil

import pytest
from sqlalchemy import select

from app import worktrees
from app.config import get_settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.utils import dumps

DECLARED = "src/a.gd"
WORKSPACE = Path("workspace")


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


def _orchestrator(tmp_path: Path, *, retention: int = 0) -> Orchestrator:
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / WORKSPACE,
        worktree_retention_seconds=retention,
    )
    return Orchestrator(settings)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(
    tmp_path: Path,
    *,
    child_status: str = TaskStatus.DONE.value,
    parent_status: str = TaskStatus.DONE.value,
    archive_state: str = "identical",
    declared: list[str] | None = None,
) -> tuple[Path, Path]:
    """根 + 父（无自身工作树）+ 一个带 worktree 的子节点。返回 (target, child_worktree)。

    `archive_state` 三种形态都要能造出来，因为它们对应三种**不同**的结论：
    `identical`（真已归档）可以回收；`missing`（归档处没有）与 `stale`（有同名旧文件）都不许回收。
    """
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    worktree = Path(tmp_path / WORKSPACE / "worktrees" / "child-1")
    _write(worktree / DECLARED, "WORKTREE_COPY")
    _write(worktree / "only-here.gd", "ONLY HERE")
    if archive_state == "identical":
        _write(target / DECLARED, "WORKTREE_COPY")
    elif archive_state == "stale":
        _write(target / DECLARED, "ARCHIVED")

    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="wt-root",
                title="Root",
                description="",
                depth=0,
                status=TaskStatus.DONE.value,
                scope_json=dumps([str(target)]),
            )
        )
        db.add(
            TaskNode(
                id="wt-parent",
                parent_id="wt-root",
                title="Parent",
                description="",
                depth=1,
                status=parent_status,
            )
        )
        db.add(
            TaskNode(
                id="child-1",
                parent_id="wt-parent",
                title="Child",
                description="",
                depth=2,
                status=child_status,
                worktree_path=str(worktree),
                branch="codex/node-child-1",
                result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": declared if declared is not None else [DECLARED]}}),
            )
        )
        db.commit()
    return target, worktree


def _sweep(orchestrator: Orchestrator) -> dict:
    with SessionLocal() as db:
        summary = orchestrator._recycle_finished_worktrees(db, "wt-root")
        db.commit()
    return summary


def _meta(node_id: str) -> dict:
    with SessionLocal() as db:
        task = db.get(TaskNode, node_id)
        assert task is not None
        return json.loads(task.node_meta_json)


def _event_types() -> list[str]:
    with SessionLocal() as db:
        return [event.type for event in db.scalars(select(EventRecord)).all()]


def test_sweep_recycles_a_finished_worktree_once_evidence_is_archived(tmp_path: Path) -> None:
    _, worktree = _seed(tmp_path)
    orchestrator = _orchestrator(tmp_path)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 1
    assert worktree.exists() is False
    record = _meta("child-1")["worktree_recycled"]
    assert record["path"] == str(worktree)
    assert "worktree.recycled" in _event_types()
    assert "worktree.swept" in _event_types()


def test_sweep_keeps_the_worktree_when_the_declared_output_is_not_archived(tmp_path: Path) -> None:
    """风险条款：产物没归档就删 = 把证据删了。这条必须挡住。"""
    _, worktree = _seed(tmp_path, archive_state="missing")
    orchestrator = _orchestrator(tmp_path)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 0
    assert summary["by_skip_reason"]["evidence"] == 1
    assert worktree.exists() is True
    assert "worktree_recycled" not in _meta("child-1")


def test_sweep_keeps_the_worktree_when_the_archive_copy_is_stale(tmp_path: Path) -> None:
    """归档处有**同名旧文件**不算已归档——这是 R3 唯一真正要挡的情形。

    节点改的多半是工程里本来就有的文件，所以"路径存在"这条判据几乎总是成立；
    拿它当证据前置，等于回收直接把节点唯一一份新产物删掉，而 `_integrate_child_outputs`
    随后只会记一条 `source missing`（默认档只上浮不阻断）→ 产物静默丢失。
    """
    _, worktree = _seed(tmp_path, archive_state="stale")
    orchestrator = _orchestrator(tmp_path)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 0
    assert summary["by_skip_reason"]["evidence"] == 1
    assert worktree.exists() is True
    assert (worktree / DECLARED).read_text(encoding="utf-8") == "WORKTREE_COPY"


def test_sweep_keeps_the_worktree_while_the_parent_is_still_running(tmp_path: Path) -> None:
    """父节点没定格就不删：它还没把这个 worktree 的产物整合/再验证完。"""
    _, worktree = _seed(tmp_path, parent_status=TaskStatus.WAITING_CHILDREN.value)
    orchestrator = _orchestrator(tmp_path)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 0
    assert summary["by_skip_reason"]["parent_active"] == 1
    assert worktree.exists() is True


def test_evidence_stays_readable_after_the_recycle(tmp_path: Path) -> None:
    target, worktree = _seed(tmp_path)
    orchestrator = _orchestrator(tmp_path)

    _sweep(orchestrator)

    assert worktree.exists() is False
    assert (target / DECLARED).read_text(encoding="utf-8") == "WORKTREE_COPY"  # 归档那份还在，且内容一致


def test_sweep_keeps_failed_worktrees(tmp_path: Path) -> None:
    """失败现场是修复与复盘要读的证据：`failed` 的 worktree 一律不回收。"""
    _seed(tmp_path, child_status=TaskStatus.FAILED.value)
    orchestrator = _orchestrator(tmp_path)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 0
    assert summary["by_skip_reason"]["status"] == 1
    assert Path(tmp_path / WORKSPACE / "worktrees" / "child-1").exists() is True


def test_sweep_respects_the_retention_window(tmp_path: Path) -> None:
    """刚完成的节点先留着：长跑里"立刻回收"意味着出事时没有任何现场可看。"""
    _seed(tmp_path)
    orchestrator = _orchestrator(tmp_path, retention=3600)

    summary = _sweep(orchestrator)

    assert summary["recycled"] == 0
    assert summary["by_skip_reason"]["retention"] == 1
    assert Path(tmp_path / WORKSPACE / "worktrees" / "child-1").exists() is True


def test_sweep_is_idempotent(tmp_path: Path) -> None:
    _seed(tmp_path)
    orchestrator = _orchestrator(tmp_path)

    first = _sweep(orchestrator)
    second = _sweep(orchestrator)

    assert first["recycled"] == 1
    assert second["recycled"] == 0
    assert second["scanned"] == 0  # 已回收的节点不再进扫描面


def test_lifecycle_board_explains_why_a_worktree_stays(tmp_path: Path) -> None:
    _seed(tmp_path, archive_state="missing")
    orchestrator = _orchestrator(tmp_path)
    with SessionLocal() as db:
        tasks = list(db.scalars(select(TaskNode)).all())
        board = orchestrator._worktree_lifecycle_state(db, tasks)

    assert board["with_worktree"] == 1
    assert board["recycled"] == 0
    assert board["recyclable_now"] == 0
    assert board["by_skip_reason"] == {"evidence": 1}


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="worktree 回收用例需要真的调 git（与仓库其它 git 用例保持一致）",
)
def test_git_worktree_and_its_branch_are_recycled(tmp_path: Path) -> None:
    """真 git worktree：`worktree remove` 与 `branch -D` 都要发生，且只删我们自己那条分支。"""
    target = tmp_path / "repo"
    target.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=60)

    git("init")
    _write(target / DECLARED, "WORKTREE_COPY")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "init")

    worktree = Path(tmp_path / WORKSPACE / "worktrees" / "child-git")
    added = git("worktree", "add", "-b", "codex/node-child-git", str(worktree))
    assert added.returncode == 0, added.stderr
    assert worktree.exists()
    assert (worktree / ".git").exists()

    with SessionLocal() as db:
        db.add(TaskNode(id="wt-root", title="Root", description="", depth=0, status=TaskStatus.DONE.value, scope_json=dumps([str(target)])))
        db.add(TaskNode(id="wt-parent", parent_id="wt-root", title="Parent", description="", depth=1, status=TaskStatus.DONE.value))
        db.add(
            TaskNode(
                id="child-git",
                parent_id="wt-parent",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(worktree),
                branch="codex/node-child-git",
                result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": [DECLARED]}}),
            )
        )
        db.commit()

    orchestrator = _orchestrator(tmp_path)
    summary = _sweep(orchestrator)

    assert summary["recycled"] == 1
    assert worktree.exists() is False
    assert _meta("child-git")["worktree_recycled"]["branch_deleted"] is True
    branches = git("branch", "--list", "codex/node-*").stdout.strip()
    assert branches == ""
    assert (target / DECLARED).read_text(encoding="utf-8") == "WORKTREE_COPY"


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="worktree 回收用例需要真的调 git（与仓库其它 git 用例保持一致）",
)
def test_copy_mode_worktree_is_recycled_even_though_the_target_is_a_git_repo(tmp_path: Path) -> None:
    """退化路径：`_assign_worktree` 拿不到租约（或 `git worktree add` 失败）时是**拷贝目录**。

    判"这是不是 git worktree"必须看**这个目录自己**有没有 `.git`。按"根目标是不是 git 仓库"判，
    这种目录会被送进 `git worktree remove`，拿到 `fatal: not a working tree`，于是永久留着、
    每轮还多一条失败事件——回收开关等于是坏的。
    """
    target = tmp_path / "repo"
    target.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True, timeout=60)

    git("init")
    _write(target / DECLARED, "WORKTREE_COPY")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "init")

    worktree = Path(tmp_path / WORKSPACE / "worktrees" / "child-copy")
    _write(worktree / DECLARED, "WORKTREE_COPY")  # 拷贝式：没有 .git

    with SessionLocal() as db:
        db.add(TaskNode(id="wt-root", title="Root", description="", depth=0, status=TaskStatus.DONE.value, scope_json=dumps([str(target)])))
        db.add(TaskNode(id="wt-parent", parent_id="wt-root", title="Parent", description="", depth=1, status=TaskStatus.DONE.value))
        db.add(
            TaskNode(
                id="child-copy",
                parent_id="wt-parent",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(worktree),
                branch="codex/node-child-copy",
                result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": [DECLARED]}}),
            )
        )
        db.commit()

    orchestrator = _orchestrator(tmp_path)
    summary = _sweep(orchestrator)

    assert summary["recycled"] == 1, summary
    assert worktree.exists() is False
    assert _meta("child-copy")["worktree_recycled"]["branch_deleted"] is False


def test_sweep_recycles_children_before_their_parent(tmp_path: Path) -> None:
    """清扫必须**深先浅后**：父节点的 worktree 就是子节点的归档处。

    先删父，子的证据判据随即失败，而归档处不会回来 → 子永久不可回收，看板还会把它报成
    "证据找不到（那本身就是要处理的问题）"——一条由我们自己制造的假警报。
    """
    target = tmp_path / "target"
    target.mkdir(parents=True, exist_ok=True)
    parent_tree = Path(tmp_path / WORKSPACE / "worktrees" / "parent-1")
    child_tree = Path(tmp_path / WORKSPACE / "worktrees" / "child-1")

    # 父的产物归档在根目标里；子的产物归档在**父的 worktree** 里
    _write(target / "src" / "p.gd", "P")
    _write(parent_tree / "src" / "p.gd", "P")
    _write(parent_tree / "src" / "a.gd", "A")
    _write(child_tree / "src" / "a.gd", "A")

    with SessionLocal() as db:
        db.add(TaskNode(id="wt-root", title="Root", description="", depth=0, status=TaskStatus.DONE.value, scope_json=dumps([str(target)])))
        db.add(
            TaskNode(
                id="parent-1",
                parent_id="wt-root",
                title="Parent",
                description="",
                depth=1,
                status=TaskStatus.DONE.value,
                worktree_path=str(parent_tree),
                branch="codex/node-parent-1",
                node_meta_json=dumps({"parent_integration": {"integrated_files": ["src/p.gd"]}}),
            )
        )
        db.add(
            TaskNode(
                id="child-1",
                parent_id="parent-1",
                title="Child",
                description="",
                depth=2,
                status=TaskStatus.DONE.value,
                worktree_path=str(child_tree),
                branch="codex/node-child-1",
                result_json=dumps({"worker": {"status": "succeeded", "summary": "s", "changed_files": ["src/a.gd"]}}),
            )
        )
        db.commit()

    orchestrator = _orchestrator(tmp_path)
    summary = _sweep(orchestrator)

    assert summary["recycled"] == 2, summary
    assert child_tree.exists() is False
    assert parent_tree.exists() is False


def test_lease_key_is_used_for_the_target_repository(tmp_path: Path) -> None:
    """租约按目标仓库取：同一仓库串行、不同仓库互不影响（纯判据，防回归到"全局一把锁"）。"""
    assert worktrees.lease_key(str(tmp_path / "a")) != worktrees.lease_key(str(tmp_path / "b"))
