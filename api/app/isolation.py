"""跨节点泄露检测（侦探）：节点产物里出现了**兄弟节点的工作树路径**。

`docs/spec-task-isolation.md` §2.3。它在 R2 里的位置必须说清：**这不是防线，是证据**——
同账号下读隔离做不到（ACL 的粒度是用户不是进程），能做的就是"越权留下痕迹、痕迹可核对"。

两条硬约定（失效方向写明）：

1. **advisory，不是 blocking**：文本里出现一个路径**不等于**真的读了它（可能是复述提示词、
   或引用了集成目标里的合法路径）。拿猜测去拦一棵正在跑的树，比晚一步发现更糟。
2. **只精确匹配**：自己的路径、根目标、集成目标都排除；不做模糊包含。宁可漏报也不能误报——
   误报会把真泄露淹掉，而人一旦不信它就会把这条判据关掉。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from . import worktrees

MAX_EXCERPT = 120


def normalize(value: str) -> str:
    """Windows 上路径大小写与分隔符都不敏感：统一成正斜杠小写，便于精确匹配。"""
    return str(value or "").replace("\\", "/").rstrip("/").lower()


def leaks(
    text: str,
    *,
    own_paths: Iterable[str] = (),
    sibling_paths: Iterable[str] = (),
    limit: int = 5,
) -> list[dict[str, Any]]:
    """`text` 里出现的兄弟路径（去掉自己人之后）。

    `own_paths` 是**排除名单**：自己的 worktree、根目标、集成目标——它们出现在自己的产物里是正常的。
    `sibling_paths` 里排除名单命中的部分同样会被排除（一个路径既是兄弟又在排除名单里时，以排除为准）。
    """
    haystack = normalize(text)
    if not haystack.strip():
        return []
    excluded = {normalize(path) for path in own_paths if str(path or "").strip()}
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sibling_paths:
        raw = str(path or "").strip()
        needle = normalize(raw)
        if not needle or len(needle) < 8 or needle in seen:
            continue
        if any(needle == item or needle.startswith(item + "/") or item.startswith(needle + "/") for item in excluded):
            continue
        index = haystack.find(needle)
        if index < 0:
            continue
        seen.add(needle)
        start = max(0, index - 20)
        found.append(
            {
                "path": raw,
                "excerpt": text[start : index + len(needle)][:MAX_EXCERPT],
            }
        )
        if len(found) >= max(1, limit):
            break
    return found


def worktree_paths(workspace_dir: str, node_ids: Sequence[str]) -> dict[str, str]:
    """节点 id → 它的工作树路径。

    **必须与写入方同源**：委托给 `worktrees.worktree_path`（唯一构造点）。
    以前这里自己拼 `os.path.join(workspace_dir, "worktrees", id)`——那是同一个约定的
    第二份副本，一旦与 `orchestrator._assign_worktree` 漂移，
    本模块的泄漏检测就会对着**错误的路径**做匹配：要么永远查不出泄漏，要么满屏误报。
    两处的等价性由 `tests/test_worktree_path_single_source.py` 钉住。

    仍然刻意由调用方传 id 列表、而不是在这里查库：本模块不碰数据库
    （纯逻辑，可脱离调度器单测）。
    """
    return {node_id: str(worktrees.worktree_path(workspace_dir, node_id)) for node_id in node_ids}


def summary_line(found: Sequence[dict[str, Any]]) -> str:
    paths = [str(item.get("path") or "") for item in found]
    return (
        f"跨节点泄露检测：本轮产物里出现 {len(found)} 个兄弟节点路径（advisory，不阻断）：{paths[:5]}"
    )
