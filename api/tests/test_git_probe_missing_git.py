"""Git 探测在缺 git 的主机上不得崩溃（2026-10-14 实测事故的回归钉子）。

**事故**：在一台 `PATH` 上没有 `git` 的机器上跑真实任务树，`_is_git_repo` 里的
`subprocess.run(["git", ...])` 抛 `FileNotFoundError`。该异常从 `_execute_leaf` 一路逃到
`run_tree`，而所有后台树任务注册的是 `add_done_callback(self._background_tasks.discard)`——
`set.discard` 永不抛异常，所以**异常从未被取走**：只留下一条
`Task exception was never retrieved` 在 uvicorn stderr。

**症状**（为什么这个 bug 值钱）：
  * 4 个节点停在 `planning`，`active_agents` 仍计 4 → 看门狗看不见它
  * `blocked=0`、事件为空 → 看板也不报
  * 树靠 `/api/tasks/{root}/kick` 才恢复

**根因不是"没写降级"**：`_assign_worktree` 本来就有完整的拷贝降级路径。问题是这段代码写在
`_is_git_repo` **之后**，而降级只覆盖"git 在但命令失败"（`returncode != 0`），
**没覆盖"git 根本不在"**（异常）。探针的"否"必须包含三种情况，三种都由调用方同样处理。

本文件钉住：
| 编号 | 要钉住的事 |
|---|---|
| G1 | git 不在（`FileNotFoundError`）→ 探针返回 False，**不抛** |
| G2 | 不是 git 仓库（`returncode != 0`）→ False |
| G3 | 是 git 仓库 → True |
| G4 | 探针超时（`TimeoutExpired`）→ False，不抛 |
| G5 | 权限类 `OSError` → False，不抛 |
| G6 | **接线**：`_assign_worktree` 在 git 缺失时走拷贝降级，不抛 |
| G7 | 探针**必须真的调 git**，不能只查 `.git` 存在性（嵌套仓库的情形两者答案不同） |
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.config import get_settings
from app.orchestrator import Orchestrator


@pytest.fixture()
def orchestrator(tmp_path: Path) -> Orchestrator:
    settings = get_settings().__class__(
        mock_delay_seconds=0,
        workspace_dir=tmp_path / "workspace",
    )
    return Orchestrator(settings)


def _fake_run_missing_git(*args, **kwargs):
    """模拟 `PATH` 上没有 git：CreateProcess 失败。"""
    raise FileNotFoundError(2, "系统找不到指定的文件。")


def _fake_run_result(returncode: int, stdout: str = ""):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=returncode, stdout=stdout, stderr="")

    return _run


def test_g1_git_missing_returns_false_without_raising(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G1：这是本次事故的直接原因。"""
    monkeypatch.setattr(subprocess, "run", _fake_run_missing_git)
    assert orchestrator._is_git_repo(tmp_path) is False


def test_g2_not_a_repo_returns_false(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G2：git 在，但路径不是仓库（正常返回非零）。"""
    monkeypatch.setattr(subprocess, "run", _fake_run_result(128))
    assert orchestrator._is_git_repo(tmp_path) is False


def test_g3_is_a_repo_returns_true(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G3：真的仓库必须仍返回 True——修复不能把功能一起吞掉。"""
    monkeypatch.setattr(subprocess, "run", _fake_run_result(0, "true\n"))
    assert orchestrator._is_git_repo(tmp_path) is True


def test_g4_timeout_returns_false(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G4：探针超时不得让树崩溃（网络盘 / 卡住的 git 会走到这里）。"""

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=15)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert orchestrator._is_git_repo(tmp_path) is False


def test_g5_permission_error_returns_false(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G5：一般 OSError 同样按"不是仓库"处理，不抛。"""

    def _denied(*args, **kwargs):
        raise PermissionError(13, "拒绝访问。")

    monkeypatch.setattr(subprocess, "run", _denied)
    assert orchestrator._is_git_repo(tmp_path) is False


def test_g6_assign_worktree_degrades_to_copy_when_git_is_missing(
    orchestrator: Orchestrator, monkeypatch, tmp_path: Path
):
    """G6：接线——git 缺失时 `_assign_worktree` 走拷贝降级，而不是抛出去。

    这条比 G1 更接近事故现场：G1 只证明探针不抛，G6 证明**整条赋值路径**能在缺 git 的主机上活下来，
    并且把目标目录的内容真的复制进隔离 worktree。
    """
    # 目标目录（非 git）：里面放一个文件，期待它被复制进 worktree
    target = tmp_path / "target"
    (target / "src").mkdir(parents=True)
    (target / "src" / "a.gd").write_text("A", encoding="utf-8")

    monkeypatch.setattr(subprocess, "run", _fake_run_missing_git)

    class _Task:
        id = "node-g6"
        title = "G6"
        worktree_path = None
        branch = None

    task = _Task()
    monkeypatch.setattr(orchestrator, "_root_target_path", lambda db, t: target)

    processed: list = []

    class _DB:
        def add(self, obj):
            processed.append(obj)

        def flush(self):
            pass

        def commit(self):
            pass

    orchestrator._assign_worktree(_DB(), task)

    assert task.worktree_path, "worktree 路径必须被赋值（降级也要给出隔离目录）"
    copied = Path(task.worktree_path) / "src" / "a.gd"
    assert copied.is_file(), f"降级拷贝没有落地：{task.worktree_path}"
    assert copied.read_text(encoding="utf-8") == "A"


def test_g7_probe_actually_invokes_git(orchestrator: Orchestrator, monkeypatch, tmp_path: Path):
    """G7：探针必须真的调 git，不能退化成查 `.git` 目录是否存在。

    反例：一个嵌套在别的仓库里、但自己没有 `.git` 的目录——`Path('.git').exists()` 说"不是"，
    而 `git rev-parse --is-inside-work-tree` 说"是"。这里问的是"能不能对它跑 git worktree add"，
    所以必须以 git 的答案为准。
    """
    seen: list[list[str]] = []

    def _record(args, **kwargs):
        seen.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="true\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _record)
    # 一个没有 .git 的普通目录，但探针仍应去问 git
    assert orchestrator._is_git_repo(tmp_path) is True
    assert seen, "探针没有调用任何子进程"
    assert seen[0][0] == "git", f"第一个参数应是 git，实际 {seen[0][0]!r}"
    assert "rev-parse" in seen[0]
