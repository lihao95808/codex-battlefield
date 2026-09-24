"""R5 跨版本回归门禁：记下"这一版验收通过时的产物指纹"，下一版验收时用来发现回退。

**为什么需要它**：现有门禁全是**单次状态检查**（Godot 集成门禁、成品门禁、债务门禁），
没有"和上一版比"的判据。于是"新版本把上一版已经验收过的产物搞坏了"只能靠 agent 自觉。

语义（刻意避开"把正常返工一起拦下"的写法）：

- 基线在**每次验收通过**时刷新（带版本号与 `constitution_hash`），所以同一版内的正常返工
  不会被判成回退；只有**版本跨过去了**才拿上一版基线来比。
- `missing`（上一版验证过的产物没了）→ 一律 **blocking**：正常返工不会"悄悄删掉已验收产物"。
- `changed`（内容变了）→ **由策略决定**：`record` 档只记录并上浮（默认），`repair` 档才阻断。
  理由与集成冲突同源（`obligations.conflict_severity`）：字节变化本身可能就是修复动作，
  默认档拦 `done` 等于把默认行为悄悄改成阻断档。

本模块只做纯逻辑（快照 / 比对 / 文案），不碰数据库、不写文件、不导入 orchestrator，
因此可以脱离调度器单测。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .utils import path_digest

# `node_meta` 里存基线的键
BASELINE_KEY = "regression_baseline"

# 产物在快照里的状态
STATUS_MISSING = "missing"
# 文件指纹前缀（目录用 `utils.path_digest` 的 `tree:` 前缀）
SHA_PREFIX = "sha256:"

# 策略：`record`（默认，只记录并上浮）/ `repair`（内容变化也阻断）
STRATEGY_RECORD = "record"
STRATEGY_REPAIR = "repair"
VALID_STRATEGIES = (STRATEGY_RECORD, STRATEGY_REPAIR)


def normalize_strategy(value: Any) -> str:
    """未知档位**回落到默认档**（`record`），而不是"当成严格档"——写错了不该悄悄变成阻断。"""
    return STRATEGY_REPAIR if str(value or "").strip().lower() == STRATEGY_REPAIR else STRATEGY_RECORD


def normalize_artifact_paths(root: Path, paths: Iterable[str]) -> list[str]:
    """把 `changed_files` 归一成"相对 root 的 posix 路径"，丢掉越界、绝对路径与空项。

    归一化是必须的：`changed_files` 由 agent 写，写法五花八门；不归一就会出现
    "同一个文件在基线里叫 `src\\a.gd`、这一轮叫 `src/a.gd`" 这种假回退。
    """
    root = Path(root)
    result: set[str] = set()
    for raw in paths or []:
        text = str(raw or "").strip().replace("\\", "/")
        if not text or text.startswith("/") or text.startswith("../") or ":" in text:
            continue
        try:
            relative = (root / text).resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if relative.as_posix() in ("", "."):
            continue
        result.add(relative.as_posix())
    return sorted(result)


def snapshot_artifacts(
    root: Path,
    paths: Sequence[str],
    unreadable: list[str] | None = None,
) -> dict[str, str]:
    """相对路径 → 指纹（文件 `sha256:`、目录 `tree:`）；**只记当时真的读到的产物**。

    为什么跳过不存在的：基线要回答"上一版验收通过时**有什么**"。当时就不在的文件不是回归
    （"声明了却没产出"是 `integration_skipped` 的职责），若把它记成 `missing` 写进基线，
    之后每一轮都会拿它报同一条假回退——门禁会变成一个永远清不掉的东西。

    目录也要记（`changed_files` 允许声明目录）：只认文件时"整个目录被删掉"不算回退。

    读盘失败也不抛：回归门禁读不了某个文件时，正确表现是"这个产物不进基线"，而不是打崩验收。
    **但同一个函数在两处被用**（记基线 / 比当前），后者把"读不了"当成"不存在"就会产出假 `missing`
    ——而 `missing` 是无条件 blocking，一次瞬时占用（Windows 上很常见）就能把父节点打进修复。
    所以读不了的路径收进 `unreadable`，交给 `diff_baseline` 排除出比对。
    """
    snapshot: dict[str, str] = {}
    for relative in paths:
        target = Path(root) / relative
        try:
            if not target.exists():
                continue
            snapshot[relative] = path_digest(target)
        except OSError:
            if unreadable is not None:
                unreadable.append(str(relative))
            continue
    return snapshot


def build_baseline(
    *,
    constitution_version: int | None,
    constitution_hash: str,
    artifacts: dict[str, str],
    assertions: Sequence[str] = (),
    root: str = "",
) -> dict[str, Any]:
    """基线 = 版本身份 + 产物指纹 + 当时的验收断言（断言只作定位线索，不在这里执行）。

    `root` 只作**诊断线索**（"这批指纹是在哪个目录上取的"），比对时按当前口径重新算根——
    否则工作树换位置会把整批产物报成缺失。
    """
    return {
        "constitution_version": constitution_version,
        "constitution_hash": str(constitution_hash or ""),
        "artifacts": {str(key): str(value) for key, value in dict(artifacts).items()},
        "artifact_count": len(artifacts),
        "assertions": [str(item) for item in assertions],
        "root": str(root or ""),
    }


def version_advanced(baseline: Any, *, current_version: int | None, current_hash: str) -> bool:
    """比对的时机：基线记的是**更早**的版本。

    - **同版本不比，hash 变了也不比**：同一版里的正常返工（修复子任务改文件）不该被自己的门禁拦下。
      真正的跨版本一定会带版本号——`_replace_constitution_snapshot` 总是同改 version 与 hash，
      所以"版本没动、hash 动了"在本工程里就是同一版的现场改动。
    - **版本回退也不比**：那份基线属于更晚的宪法，拿它比只会把回退动作本身报成产物回退。
    - 只有版本号缺失时才退回 hash 判据：那时没有别的信号可用，宁可严一点。
    """
    if not isinstance(baseline, dict) or not baseline.get("artifacts"):
        return False
    recorded_version = baseline.get("constitution_version")
    if isinstance(current_version, int) and isinstance(recorded_version, int):
        return current_version > recorded_version
    recorded_hash = str(baseline.get("constitution_hash") or "")
    current_hash = str(current_hash or "")
    return bool(recorded_hash) and bool(current_hash) and recorded_hash != current_hash


def diff_baseline(
    baseline: Any,
    current: dict[str, str],
    unreadable: Sequence[str] = (),
) -> dict[str, Any]:
    """比对结果只报事实（缺了什么、变了什么、没变什么），严重度由 `obligations` 决定。

    `unreadable` 里的路径**不算 `missing`**：读盘失败（Windows 上被另一个进程占用）不是"产物没了"，
    而 `missing` 是无条件 blocking——把它算进去就是拿一次瞬时占用去拦父节点验收。
    这些路径单列出来，进事件与看板，谁都没看见的问题才是问题。
    """
    recorded = baseline.get("artifacts") if isinstance(baseline, dict) and isinstance(baseline.get("artifacts"), dict) else {}
    skipped = {str(item) for item in unreadable}
    missing: list[dict[str, str]] = []
    changed: list[dict[str, str]] = []
    intact: list[str] = []
    unjudged: list[str] = []
    for path, was in sorted(recorded.items()):
        if str(path) in skipped:
            unjudged.append(str(path))
            continue
        now = str(current.get(path, STATUS_MISSING))
        if now == STATUS_MISSING:
            missing.append({"path": str(path), "was": str(was), "now": STATUS_MISSING})
        elif now != str(was):
            changed.append({"path": str(path), "was": str(was), "now": now})
        else:
            intact.append(str(path))
    return {
        "missing": missing,
        "changed": changed,
        "intact": intact,
        "unreadable": unjudged,
        "checked": len(recorded) - len(unjudged),
        "base_version": baseline.get("constitution_version") if isinstance(baseline, dict) else None,
    }


def breaking_entries(diff: dict[str, Any], strategy: Any) -> list[dict[str, str]]:
    """按策略挑出"算破坏"的条目：`missing` 一律算，`changed` 只在 `repair` 档算。"""
    entries = [dict(item) for item in diff.get("missing") or []]
    if normalize_strategy(strategy) == STRATEGY_REPAIR:
        entries.extend(dict(item) for item in diff.get("changed") or [])
    return entries


def diff_reason(diff: dict[str, Any], *, current_version: int | None) -> str:
    """挡住的同时必须能说清挡住了什么（G5 要求 diff 定位）。"""
    parts: list[str] = []
    for item in diff.get("missing") or []:
        was = str(item.get("was") or "")
        parts.append(f"{item.get('path')}（已缺失，基线 {was[:19]}…）")
    for item in diff.get("changed") or []:
        was = str(item.get("was") or "")
        now = str(item.get("now") or "")
        parts.append(f"{item.get('path')}（内容变化 {was[:19]}…→{now[:19]}…）")
    shown = "、".join(parts[:8]) if parts else "无"
    suffix = f" 等共 {len(parts)} 处" if len(parts) > 8 else ""
    unreadable = diff.get("unreadable") or []
    caveat = f"；另有 {len(unreadable)} 处读盘失败未判定（不计入回退）" if unreadable else ""
    base_version = diff.get("base_version")
    return (
        f"跨版本回归：v{base_version}→v{current_version} 基线里的已验收产物不再成立：{shown}{suffix}{caveat}"
    )
