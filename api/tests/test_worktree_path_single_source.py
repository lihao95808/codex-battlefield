"""工作树路径必须**只有一个构造点**，且它的约定不能漂移。

**事故形态**（`docs/spec-state-project-decoupling.md` §8.1）：同一个约定
`<workspace>/worktrees/<safe-id>` 曾被**三处各自**推导：

| 位置 | 角色 |
|---|---|
| `orchestrator._assign_worktree` | **写入方**（持久化进 `TaskNode.worktree_path`） |
| `worktrees.managed_root` / `is_managed_worktree` | **删除守卫** |
| `isolation.worktree_paths` | 跨节点泄漏检测 |

任何一处漂移都**不会报错**，只会静默改行为：

- 守卫 ≠ 实际路径 → 挡住清理；更糟的是**删错东西**（守卫存在的全部理由就是防止删到仓库外）
- 检测器 ≠ 实际路径 → 泄漏检测对着错误路径匹配，永远查不出（或满屏误报）

## 本文件为什么要写"独立期望值"

第一版这里写的是 `assert isolation.worktree_paths(...)[id] == str(worktrees.worktree_path(...))`
——**那是恒真的**：两边调同一个函数，永远相等，**证伪不出来**。
（实测：把构造点改成 `worktrees/nodes/<id>` 后仍然 4 passed，等于没测。）

所以这里改成：**在测试里独立写出契约**
（`managed_root(ws) / safe_node_id(id)`），再断言三处实现都与它一致。
这样只有"约定本身被动过"才会红——那正是要拦的东西。
"""
from __future__ import annotations

import pathlib

from app import isolation, worktrees


TRICKY_IDS = [
    "node-a",
    "root-abcdef",
    "tricky/id",          # id 里带分隔符
    "node-0875894a21",
]


def _contract(workspace: pathlib.Path, node_id: str) -> str:
    """**测试自己的**期望值，故意不复用被测函数。

    `str()` 而非 `Path` 是为了对齐 `isolation` 返回的 str 形态——
    这条也是契约的一部分（调用方拿它做字符串匹配）。
    """
    return str(pathlib.Path(workspace) / "worktrees" / node_id.replace("/", "-"))


def test_constructor_matches_the_documented_contract(tmp_path: pathlib.Path) -> None:
    """唯一构造点必须等于独立写出的契约（约定被改动时这里会红）。"""
    workspace = tmp_path / "workspace"
    for node_id in TRICKY_IDS:
        got = str(worktrees.worktree_path(workspace, node_id))
        assert got == _contract(workspace, node_id), (
            f"工作树约定变了：{got!r} != 契约 {_contract(workspace, node_id)!r}；"
            "改约定必须同时改守卫与泄漏检测，并更新本契约"
        )


def test_isolation_matches_the_documented_contract(tmp_path: pathlib.Path) -> None:
    """泄漏检测器必须等于同一个契约（它以前自己拼路径，是第二份副本）。"""
    workspace = tmp_path / "workspace"
    got = isolation.worktree_paths(str(workspace), TRICKY_IDS)
    for node_id in TRICKY_IDS:
        assert got[node_id] == _contract(workspace, node_id), (
            f"泄漏检测的路径与契约不一致：{got[node_id]!r} != {_contract(workspace, node_id)!r}"
        )


def test_write_and_guard_agree(tmp_path: pathlib.Path) -> None:
    """写入方写下的路径，必须能通过删除守卫——否则回收会被挡住。"""
    workspace = tmp_path / "workspace"
    for node_id in TRICKY_IDS:
        assigned = worktrees.worktree_path(workspace, node_id)
        assert worktrees.is_managed_worktree(workspace, assigned) is True, (
            f"写入方给的路径过不了守卫：{assigned}；这会让它的 worktree 永远回收不掉"
        )


def test_guard_rejects_a_worktree_outside_the_managed_root(tmp_path: pathlib.Path) -> None:
    """对照：守卫仍然拒绝托管根之外的路径（证明上一条不是恒真）。"""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "elsewhere" / "worktrees" / "node-a"
    assert worktrees.is_managed_worktree(workspace, outside) is False
    # 托管根本身也不算一个 worktree
    assert worktrees.is_managed_worktree(workspace, worktrees.managed_root(workspace)) is False
