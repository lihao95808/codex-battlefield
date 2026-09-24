"""验收面冻结：让"裁判"独立于"选手"（`docs/spec-verification-independence.md` §3）。

**问题**：树的完成条件是"入站债务为空 + 整合验收通过"，而**验收判据（测试、校验脚本）就在产出方
可写的目标工程里**——也就是说，产出方能改自己正要通过的考卷。

**做法**：宪法显式声明哪些文件是"验收面"；**人**锁宪法/修订宪法时把这些文件的**内容指纹**冻结下来；
整合验收前比对当前指纹与冻结指纹，不一致就登记 **blocking** 债务（走既有的 `_obligation_gate`，
不新造门禁）。

失效方向（每条判据都必须写清，见 AGENTS.md 第 5 条）：

| 情况 | 结论 |
|---|---|
| 面文件内容变了 / 没了 | **无条件阻断**（与档位无关）——这就是"改考卷" |
| 面文件读不到（占用/权限） | **不判定**：读盘失败不是"改了"；单列 + 事件 |
| 未声明验收面 / 路径非法 | **不判定**：判不了就说判不了，不拿"没声明"当通过 |
| 面又和冻结值对上了 | 按**事实**清债（不是"谁完成了什么"） |

本模块只做纯逻辑（声明解析 / 冻结 / 比对 / 文案），不碰数据库、不建子任务、不导入 `orchestrator`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import path_digest

# 树根节点 `node_meta` 里存冻结基线的键
SURFACE_KEY = "acceptance_surface"

STATUS_MISSING = "missing"
STATUS_CHANGED = "changed"

# 宪法 data 里声明验收面的键
DECLARED_KEY = "acceptance_surface"


def declared_paths(items: Any) -> tuple[list[str], list[str]]:
    """宪法里的验收面声明 → `(合法路径, 被丢弃的写法)`。

    绝对路径与带 `..` 的写法**一律丢弃并单独报出来**：它们判不了"归档处有没有"，留着只会
    在比对时产生假结论（与 `changed_files`、判据路径同一条边界规则）。
    """
    valid: list[str] = []
    invalid: list[str] = []
    if not isinstance(items, list):
        return valid, invalid
    for raw in items:
        text = str(raw or "").strip().replace("\\", "/")
        if not text:
            continue
        if text.startswith("/") or ":" in text or any(part == ".." for part in text.split("/")):
            invalid.append(text)
            continue
        if text not in valid:
            valid.append(text)
    return valid, invalid


def freeze(root: str | Path, paths: list[str], *, version: int | None, frozen_by: str, now_iso: str) -> dict[str, Any]:
    """把验收面的**内容指纹**冻结下来。

    - 读不到的路径**不丢**，进 `unreadable`：丢了它就等于"这条判据悄悄消失了"。
    - 冻结基线里同时记版本与冻结者：将来要回答"这条面是谁在哪一版上批的"。
    """
    digests: dict[str, str] = {}
    unreadable: list[str] = []
    base = Path(root)
    for path in paths:
        target = base / path
        try:
            if not target.exists():
                # 声明了但当时就不在：不写进基线（写进去等于给自己埋一条永远清不掉的假债），
                # 但要记下来——它是"声明坏了"的证据，不是"面被改了"。
                unreadable.append(path)
                continue
            digests[path] = path_digest(target)
        except OSError:
            unreadable.append(path)
    return {
        "version": version,
        "frozen_at": now_iso,
        "frozen_by": frozen_by,
        "digests": digests,
        "unreadable": unreadable,
    }


def diff(frozen: dict[str, Any] | None, root: str | Path) -> dict[str, Any]:
    """当前状态 vs 冻结基线。

    返回 `{"changed": [...], "missing": [...], "intact": [...], "unreadable": [...]}`，
    每项是 `{"path": ..., "was": ..., "now": ...}`（`intact`/`unreadable` 只给路径）。
    """
    frozen = frozen if isinstance(frozen, dict) else {}
    recorded = frozen.get("digests") if isinstance(frozen.get("digests"), dict) else {}
    base = Path(root)
    changed: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    intact: list[str] = []
    unreadable: list[str] = []
    for path, was in sorted(recorded.items()):
        target = base / str(path)
        try:
            if not target.exists():
                missing.append({"path": str(path), "was": str(was), "now": STATUS_MISSING})
            else:
                now = path_digest(target)
                if now == str(was):
                    intact.append(str(path))
                else:
                    changed.append({"path": str(path), "was": str(was), "now": now})
        except OSError:
            # 读不了 ≠ 改了：单列，交给调用方记事件（**不**阻断）
            unreadable.append(str(path))
    return {
        "changed": changed,
        "missing": missing,
        "intact": intact,
        "unreadable": unreadable,
        "frozen_version": frozen.get("version"),
        "frozen_by": frozen.get("frozen_by"),
        "declared": len(recorded),
    }


def reason(diff_result: dict[str, Any], *, current_version: int | None) -> str:
    """给事件/债务用的理由。**必须点名路径**——"验收面被改了"这种话没有可操作性。"""
    parts = [
        f"{item['path']}（{kind}）"
        for kind, key in (("改了", STATUS_CHANGED), ("没了", STATUS_MISSING))
        for item in diff_result.get(key) or []
    ]
    shown = "、".join(parts[:8]) if parts else "无"
    suffix = f" 等共 {len(parts)} 处" if len(parts) > 8 else ""
    unreadable = diff_result.get("unreadable") or []
    caveat = f"；另有 {len(unreadable)} 处读盘失败未判定（不计入篡改）" if unreadable else ""
    return (
        f"验收面被改动（冻结于 v{diff_result.get('frozen_version')}，由 {diff_result.get('frozen_by')} 批；"
        f"当前 v{current_version}）：{shown}{suffix}{caveat}"
    )


def declared_changed(previous: dict[str, Any] | None, paths: list[str]) -> bool:
    """声明的**路径清单**变了吗。

    这条判据决定"修订宪法时要不要重新冻结"：清单变了（加/删判据）是改需求，允许重新冻结；
    清单没变但**内容**变了则是改考卷，**不许**靠一次无关的修订把它洗白。
    """
    previous = previous if isinstance(previous, dict) else {}
    recorded = previous.get("digests") if isinstance(previous.get("digests"), dict) else {}
    return sorted(str(path) for path in recorded) != sorted(paths)
