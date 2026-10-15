"""R3 worktree 生命周期：**能不能回收**的判据 + 创建/回收共用的命名资源租约。

背景（`docs/spec-resource-governance.md` §R3）：`_assign_worktree` 会在目标仓库上跑
`git worktree add -b <branch>`（非 git 目标则退化成拷贝目录），但**创建没有串行化**、
**从不回收**。于是长跑里 worktrees 只增不减，并发创建还可能互相踩。

本模块只做两件事：

1. **判据**（纯逻辑）：一个节点的 worktree 现在能不能回收。默认保守——任何一条不确定就**不回收**，
   宁可多留。硬前置是 spec 的风险条款：**证据归档之后才允许回收**。
2. **租约**：创建与回收都要在同一个命名锁下做，避免并发 `git worktree` 互相踩。

刻意不做的事：不删任何"不在我们自己的 worktrees 根下"的目录（路径守卫），
不回收 failed 节点的 worktree（失败现场正是修复/复盘要读的证据）。
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .utils import path_digest

MANAGED_ROOT_NAME = "worktrees"
LEASES_DIR_NAME = "resource_leases"

# 只有这些终态允许回收：`failed` **不在**其中——失败现场是修复与复盘要读的证据。
# 同一组也用来判"父节点是否已定格"：`TERMINAL_STATUSES` 含 `blocked`/`failed`，而这两者都可能被
# 重新推起来接着整合子节点，拿它们当"父节点已完成"会让父节点的集成读到已被删掉的源目录。
RECYCLABLE_STATUSES = ("done", "canceled")

DECISION_RECYCLE = "recycle"


def managed_root(workspace_dir: str | Path) -> Path:
    return Path(workspace_dir) / MANAGED_ROOT_NAME


def safe_node_id(node_id: str) -> str:
    """节点 id → 可用作目录名的一段。`/` 会被替换（id 里少见但允许）。"""
    return str(node_id).replace("/", "-")


def worktree_path(workspace_dir: str | Path, node_id: str) -> Path:
    """**工作树路径的唯一构造点**：`<workspace>/worktrees/<safe-id>`。

    为什么必须集中在这里（2026-10-14）：同一个约定曾被**三处各自**推导——

      * `orchestrator._assign_worktree` 写 `TaskNode.worktree_path`（**写入方**）
      * `worktrees.managed_root` / `is_managed_worktree`（**删除守卫**）
      * `isolation.worktree_paths`（跨节点泄漏检测）

    任何一处漂移都不会报错，只会静默地把行为改掉：
    守卫与实际路径不一致 → 挡住清理，或者更糟——**删错东西**；
    泄漏检测与实际路径不一致 → 检测形同虚设（或满屏误报）。
    所以写入方、守卫、检测器必须共用这一个函数。
    """
    return managed_root(workspace_dir) / safe_node_id(node_id)


def is_managed_worktree(workspace_dir: str | Path, worktree_path: str | Path | None) -> bool:
    """路径守卫：只认 `<workspace>/worktrees/` 底下的目录。

    这条不是形式主义——回收是**删目录**。没有它，一个被写坏或被外部改过的
    `worktree_path` 就能让系统删掉仓库外的东西。
    """
    if not worktree_path:
        return False
    root = managed_root(workspace_dir).resolve()
    try:
        candidate = Path(worktree_path).resolve()
    except OSError:
        return False
    if candidate == root:
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def evidence_gaps(
    archive_root: str | Path | None,
    changed_files: Iterable[str],
    source_root: str | Path | None = None,
) -> list[str]:
    """哪些"声明产出的文件"**没能在归档处验明身份**。

    归档处 = 集成目标（父节点自己的工作树，没有就是根目标路径）。列表非空 → 不许回收：
    那些文件只存在于节点的 worktree 里，删掉就真没了。

    **判据是内容一致，不是"这个名字在不在"**。只查 `exists()` 会被"同名旧文件"蒙过：节点改的
    往往就是工程里本来就有的文件，归档处当然存在这个路径 → 判据说"已归档" → worktree 被删 →
    父节点随后集成时源文件已经没了（`_integrate_child_outputs` 的 `source missing`，
    默认档只上浮不阻断 → 产物静默丢失）。这是这个判据唯一真正要挡的情形，所以它必须比"存在"强。

    - `source_root`（节点自己的 worktree）：给出时与归档处做**指纹比对**（文件 `sha256:`、
      目录 `tree:`）。没给出时无法验明身份，一律算未判定——宁可多留一个目录，
      也不能把"没验过"当成"已归档"。
    - **没有归档处可判时**（`archive_root is None`）：只要有产物声明就整批算未判定。
    """
    declared = [
        str(item or "").strip().replace("\\", "/")
        for item in (changed_files or [])
        if str(item or "").strip()
    ]
    if archive_root is None:
        return declared
    root = Path(archive_root)
    origin_root = Path(source_root) if source_root is not None else None
    gaps: list[str] = []
    for text in declared:
        if text.startswith("/") or ":" in text:
            # 绝对路径：判不了"归档处有没有" → 当作未判定项（不许据此放行）
            gaps.append(text)
            continue
        try:
            candidate = (root / text).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            gaps.append(text)
            continue
        if not candidate.exists():
            gaps.append(text)
            continue
        if origin_root is None:
            # 没有源可比对：判不了"归档处这份是不是它写上去的"
            gaps.append(text)
            continue
        try:
            origin = (origin_root / text).resolve()
            origin.relative_to(origin_root.resolve())
        except (OSError, ValueError):
            gaps.append(text)
            continue
        try:
            if not origin.exists() or path_digest(origin) != path_digest(candidate):
                gaps.append(text)
        except OSError:
            gaps.append(text)
    return gaps


def decide(
    *,
    enabled: bool,
    status: str,
    worktree_path: str | Path | None,
    workspace_dir: str | Path,
    changed_files: Sequence[str] = (),
    archive_root: str | Path | None = None,
    source_root: str | Path | None = None,
    parent_terminal: bool | None = None,
    finished_at: float | None = None,
    now: float | None = None,
    retention_seconds: int = 0,
) -> dict[str, object]:
    """能不能回收这个节点的 worktree。返回 `{"recycle": bool, "reason": str, ...}`。

    判据顺序刻意是"先否决后同意"：每一条否决都写清原因，便于看板回答"为什么它还留着"。
    """
    if not enabled:
        return {"recycle": False, "reason": "disabled", "detail": "回收开关关闭"}
    if status not in RECYCLABLE_STATUSES:
        return {"recycle": False, "reason": "status", "detail": f"状态 {status} 不是可回收终态"}
    if not is_managed_worktree(workspace_dir, worktree_path):
        return {"recycle": False, "reason": "path_guard", "detail": "不在受管 worktrees 根下"}
    if parent_terminal is False:
        # 父节点还没进终态 → 它的整合/再验证还要读这个 worktree 的产物。
        # `_integrate_child_outputs` 的源就是子节点的 worktree（`child_root = output_node.worktree_path`），
        # 而整合发生在父节点验收里；`_revalidate_stale_leaves` 也会用 `-C <worktree>` 重跑 verifier。
        return {
            "recycle": False,
            "reason": "parent_active",
            "detail": "父节点尚未进入终态：它还没有把这个 worktree 的产物整合/再验证完",
        }
    if archive_root is not None and not Path(archive_root).exists():
        return {
            "recycle": False,
            "reason": "archive_gone",
            "detail": "归档处已不存在（父节点 worktree 已被回收），无法验明产物是否已归档",
        }
    gaps = evidence_gaps(archive_root, changed_files, source_root)
    if gaps:
        return {
            "recycle": False,
            "reason": "evidence",
            "detail": f"有 {len(gaps)} 个声明产物未能在归档处验明（缺失或指纹不同）：{gaps[:5]}",
            "evidence_gaps": gaps,
        }
    if retention_seconds > 0:
        reference = finished_at if isinstance(finished_at, (int, float)) else None
        if reference is None:
            return {"recycle": False, "reason": "retention", "detail": "没有可判定的完成时间，保守起见不回收"}
        waited = (now if isinstance(now, (int, float)) else time.time()) - float(reference)
        if waited < retention_seconds:
            return {
                "recycle": False,
                "reason": "retention",
                "detail": f"保留期未到（已等 {int(waited)}s / {retention_seconds}s）",
                "waited_seconds": int(waited),
            }
    return {
        "recycle": True,
        "reason": DECISION_RECYCLE,
        "detail": "终态 + 父节点已终态 + 产物与归档处指纹一致 + 保留期已过",
    }


def lease_key(root_target: str | Path | None) -> str:
    """租约按**目标仓库**取：并发创建冲突只发生在同一个仓库上。"""
    text = str(root_target or "unknown")
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return f"worktree-{digest}"


def lease_path(workspace_dir: str | Path, key: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in key)
    return Path(workspace_dir) / LEASES_DIR_NAME / f"{safe}.lock"


@contextmanager
def resource_lease(
    workspace_dir: str | Path,
    key: str,
    *,
    wait_seconds: float = 60.0,
    poll_seconds: float = 0.1,
) -> Iterator[bool]:
    """命名资源租约：`git worktree add` / `worktree remove` 必须在同一把锁下做。

    与 Godot 槽位用的是同一套思路（非空锁文件 + OS 字节范围锁），因为它们要防的是同一类事：
    多个执行体同时操作同一个共享资源。**拿不到锁只跳过这一步，不阻塞调度**——
    worktree 创建失败会退化成拷贝目录（既有行为），回收失败下一轮再来。
    """
    path = lease_path(workspace_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                # Windows 的字节范围锁在空文件上锁不住任何字节（R1 实测过）。
                handle.write(".")
                handle.flush()
            handle.seek(0)
        except OSError:
            pass
        while True:
            if _try_lock(handle):
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.01, poll_seconds))
        yield acquired
    finally:
        if acquired:
            _unlock(handle)
        handle.close()


def _try_lock(handle) -> bool:
    if sys.platform.startswith("win"):
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
