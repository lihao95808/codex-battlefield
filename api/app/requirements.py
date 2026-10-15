"""需求→证据追溯：把"每条原始要求都有可核对判据、且判据真的过了"做成可拦的判据。

`docs/spec-requirement-traceability.md`。**要解决的问题**：体系能保证"符合宪法"，
但不能保证"宪法符合目标"——漏掉一条需求与把一条需求做错，在现有信号里长得一模一样。

判据词表**与能力基准共用一处**（`app.bench.evaluate_check`：`file_exists` / `contains` /
`not_contains` / `command`）。同一语义两份实现必然漂移，这是 W3 的教训。

失效方向（每条判据都必须写清，见 AGENTS.md 第 5 条）：

| 情况 | 结论 |
|---|---|
| 要求没有判据 | **blocking**：没有判据的要求等于没有要求（"漏需求"） |
| 判据没过 | **blocking**：这就是"没做到" |
| 判据判不了（读不到 / 命令超时 / 路径越界） | **不判定**：判不了 ≠ 没做到，单列 + 事件 |
| 宪法没声明 `requirements` | **不判**，但如实显示"未声明要求矩阵" |
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import bench

DECLARED_KEY = "requirements"


def declared(data: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """宪法里的 `requirements` → `(规范化要求, 被丢弃的判据说明)`。

    定义错的要求**必须报出来**而不是静默丢掉：丢掉等于"这条需求从矩阵里消失"。
    要求本身缺 `id` 或 `text` 才是真的无法处理，也要报。
    """
    problems: list[str] = []
    result: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return result, problems
    raw = data.get(DECLARED_KEY)
    if not isinstance(raw, list):
        return result, problems
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 条要求不是对象")
            continue
        requirement_id = str(item.get("id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not requirement_id:
            problems.append(f"第 {index} 条要求缺少 id")
            continue
        if not text:
            problems.append(f"{requirement_id} 缺少 text")
            continue
        criteria: list[dict[str, Any]] = []
        raw_criteria = item.get("criteria")
        if raw_criteria is not None and not isinstance(raw_criteria, list):
            problems.append(f"{requirement_id} 的 criteria 不是数组")
            raw_criteria = []
        for criterion_index, criterion in enumerate(raw_criteria or []):
            if not isinstance(criterion, dict):
                problems.append(f"{requirement_id} 第 {criterion_index} 条判据不是对象")
                continue
            criterion_type = str(criterion.get("type") or "").strip()
            if criterion_type not in bench.CHECK_TYPES:
                problems.append(f"{requirement_id} 判据类型不合法：{criterion_type!r}")
                continue
            normalized = dict(criterion)
            normalized["id"] = str(criterion.get("id") or f"c{criterion_index}").strip()
            criteria.append(normalized)
        result.append({"id": requirement_id, "text": text, "criteria": criteria})
    return result, problems


def uncovered(requirements: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """一条可核对判据都没有的要求——这就是"漏需求"。"""
    return [{"id": item["id"], "text": item.get("text") or ""} for item in requirements if not item.get("criteria")]


def evaluate(
    requirements: Sequence[dict[str, Any]],
    root: str | Path,
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    """跑全部判据，按**要求**归口。

    返回 `{"met": [...], "unmet": [...], "unjudged": [...]}`：
    - `met` 是"要求 id + 判据 id"清单（用来按事实清债）；
    - `unmet` 是判了但不成立的；
    - `unjudged` 是**判不了**的（读不到 / 超时 / 越界）——它既不进 `met` 也不进 `unmet`。
    """
    met: list[dict[str, Any]] = []
    unmet: list[dict[str, Any]] = []
    unjudged: list[dict[str, Any]] = []
    for requirement in requirements:
        for criterion in requirement.get("criteria") or []:
            result = bench.evaluate_check(criterion, Path(root), python_executable=python_executable)
            entry = {
                "requirement_id": requirement["id"],
                "criterion_id": str(criterion.get("id") or ""),
                "type": str(criterion.get("type") or ""),
                "path": criterion.get("path"),
                "detail": result.get("detail"),
                "status": result.get("status"),
            }
            if result.get("status") == bench.STATUS_PASS:
                met.append(entry)
            elif result.get("status") in (bench.STATUS_ERROR, bench.STATUS_INVALID_PATH):
                unjudged.append(entry)
            else:
                unmet.append(entry)
    return {"met": met, "unmet": unmet, "unjudged": unjudged}


def summary_line(requirements: Sequence[dict[str, Any]], result: dict[str, Any]) -> str:
    """给事件/看板的一句话。**必须点名要求 id**——"有需求没覆盖"这种话没有可操作性。"""
    missing = [item["id"] for item in uncovered(requirements)]
    unmet = sorted({str(item.get("requirement_id")) for item in result.get("unmet") or []})
    parts = [f"声明 {len(requirements)} 条"]
    if missing:
        parts.append(f"没判据 {len(missing)} 条（{missing[:5]}）")
    if unmet:
        parts.append(f"判据没过 {len(unmet)} 条（{unmet[:5]}）")
    if not missing and not unmet:
        parts.append("全部有判据且都过了")
    return "需求矩阵：" + "；".join(parts)
