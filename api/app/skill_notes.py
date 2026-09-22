"""父层 Skill Note（递归经验归拢）子任务的识别与构造。

搬移自 `orchestrator.Orchestrator`。判据是 W4g-e 的 `--closure --state` 实测：
最终门禁簇 **70 方法 / 1239 行、70/70 零实例状态**；本模块取其中职责独立的一小片
（skill-note 识别与构造，约 87 行），以便单波体量可控。

- 只有 `_skill_note_tasks_enabled` 需要配置（`parent_skill_note_task_enabled` 与 `runner_mode`），
  其余全部为纯函数，因此这里的依赖面比 ports 窄得多，直接传 `settings` 即可。
- `SKILL_NOTE_COVERAGE_ID` / `SKILL_NOTE_VERIFICATION_KIND` 一并移入本模块；
  orchestrator 反向导入这两个名字，调用点零改动。

依赖方向：本模块只依赖 `models` / `schemas` / `utils`，**不得**导入 `orchestrator`。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import failure_retrospective
from .models import TaskNode
from .schemas import ChildTaskSpec
from .utils import loads, repair_mojibake_text

if TYPE_CHECKING:
    from .config import Settings


SKILL_NOTE_COVERAGE_ID = "codex-battlefield-skill-note"
SKILL_NOTE_VERIFICATION_KIND = "skill-note consolidation"


# "整份产物只读"的判据词表（2026-09-22 决策：只读约束下豁免 skill note 子任务）。
#
# 为什么要成对匹配而不是单个词：`不许改` 这类词在**很窄**的约束里也出现——实测那条
# fix-subtract 的验收写着"测试文件本身表达的是需求，不许改它"，那只是"别动测试文件"，
# 不是"整份产物只读"。拿它去豁免 skill note 就是**误伤**。
# 所以要求"禁止动作"与"整体范围词"同时命中（`read-only` / `只读` 这类本身就是整体性的，单独认）。
_READONLY_FORBID_ACTIONS = (
    "不许",
    "不得",
    "禁止",
    "不允许",
    "不能",
    "must not",
    "do not",
    "don't",
    "forbidden",
    "not allowed",
)
_READONLY_WHOLE_SCOPE = (
    "任何文件",
    "任何新文件",
    "任何改动",
    "任何变更",
    "任何写入",
    "所有文件",
    "全部文件",
    "任何 file",
    "any file",
    "all files",
    "any change",
    "any modification",
    "file system",
    "filesystem",
    "整个仓库",
    "整份",
)
_READONLY_STANDALONE = (
    "read-only task",
    "read only task",
    "readonly task",
    "只读任务",
    "只读模式",
    "no file modifications allowed",
)


def readonly_constraint_reason(parent: TaskNode) -> str:
    """该节点是否处在"整份产物只读"的约束下；返回命中的原文（空串 = 没有这条约束）。

    **为什么要有这个豁免**：实测（2026-09-22，能力基准 unsolvable 任务）根目标是"不许修改、新增、
    删除任何文件"，而继承下去的 skill-note 子任务验收要求"必须生成 `docs/skill_notes/<TASK_ID>.json`"
    ——**产品自己的两条要求互相打架**。planner 只能如实报告"节点约束内部矛盾"，于是这条虚假阻塞
    把整棵树拖成螺旋/不收敛（真实档里那条 blocked 的理由就是它）。

    失效方向写明：
    - 只在**显式命中**上表时才豁免：宁可漏豁免（那只是回到现状），也不要误伤正常任务（假阳性会让
      正常工程丢掉经验归拢产物）；
    - 命中的原文由调用方写进事件 payload——**不是静默跳过**；
    - 豁免只作用于 skill note **这一个**子任务：不动验收判据、不动门禁、不动别的子任务。
    """
    candidates: list[str] = [str(parent.title or ""), str(parent.description or "")]
    inherited = loads(parent.node_meta_json, {}).get("inherited_constraints")
    if isinstance(inherited, list):
        candidates.extend(str(item) for item in inherited)
    acceptance = loads(parent.acceptance_json, [])
    if isinstance(acceptance, list):
        candidates.extend(str(item) for item in acceptance)
    for candidate in candidates:
        text = repair_mojibake_text(candidate).strip()
        if not text:
            continue
        lowered = text.casefold()
        if any(marker in lowered for marker in _READONLY_STANDALONE):
            return text
        if any(action in lowered for action in _READONLY_FORBID_ACTIONS) and any(
            scope in lowered for scope in _READONLY_WHOLE_SCOPE
        ):
            return text
    return ""


def skill_note_tasks_enabled(settings: Settings) -> bool:
    return bool(settings.parent_skill_note_task_enabled and settings.runner_mode == "real")


def looks_like_skill_note_title(title: str) -> bool:
    text = str(title or "").strip().lower()
    if not text:
        return False
    explicit_markers = (
        "skill note",
        "skill-note",
        "skill_note",
        "docs/skill_notes",
        "\u6280\u80fd\u7b14\u8bb0",
        "\u9012\u5f52\u7ecf\u9a8c",
        "\u7ecf\u9a8c skill",
        "\u5f52\u62e2\u9012\u5f52",
        "\u5f52\u5e76\u672c\u8f6e\u9012\u5f52",
    )
    if any(marker in text for marker in explicit_markers):
        return True
    skill_text_markers = ("skill", "\u6280\u80fd")
    consolidation_markers = (
        "note",
        "\u7b14\u8bb0",
        "\u7ecf\u9a8c",
        "\u5f52\u62e2",
        "\u5f52\u5e76",
        "\u6c47\u603b",
        "\u8bb0\u5f55\u672c\u5c42",
        "\u8bb0\u5f55\u672c\u8282\u70b9",
    )
    return any(marker in text for marker in skill_text_markers) and any(
        marker in text for marker in consolidation_markers
    )


def is_skill_note_task(task: TaskNode, settings: Settings) -> bool:
    meta = loads(task.node_meta_json, {})
    coverage_ids = meta.get("coverage_ids") if isinstance(meta, dict) else []
    verification_kind = str(meta.get("verification_kind") or "") if isinstance(meta, dict) else ""
    return (
        SKILL_NOTE_COVERAGE_ID in set(str(item) for item in coverage_ids or [])
        or verification_kind == SKILL_NOTE_VERIFICATION_KIND
        or looks_like_skill_note_title(task.title)
    )


def is_skill_note_spec(child: ChildTaskSpec, settings: Settings) -> bool:
    return (
        SKILL_NOTE_COVERAGE_ID in set(str(item) for item in child.coverage_ids)
        or child.verification_kind == SKILL_NOTE_VERIFICATION_KIND
        or looks_like_skill_note_title(child.title)
    )


def has_skill_note_child(parent: TaskNode, settings: Settings) -> bool:
    return any(is_skill_note_task(child, settings) for child in parent.children)


def parent_skill_note_child(parent: TaskNode, siblings: list[ChildTaskSpec]) -> ChildTaskSpec:
    dependency_titles = [child.title for child in siblings]
    parent_scope = [str(item) for item in loads(parent.scope_json, [])]
    # R6：把失败/阻塞子树的原始证据**显式交给**这个子任务。
    # 原文只说"读已完成子树"——恰好把最该总结的失败路径排除在外。
    meta = loads(parent.node_meta_json, {})
    failure_block = failure_retrospective.retrospective_block(
        meta.get(failure_retrospective.RETROSPECTIVE_KEY) if isinstance(meta, dict) else None
    )
    description = (
        "读取本父节点所有已完成子树的产物、验证证据、阻塞报告和 docs/skill_notes/*.json，"
        "把下面节点形成的可复用经验合并成父层 skill note。必须去重并合并相似项，不要简单拼接日志。"
        "输出文件必须写到 docs/skill_notes/，文件名包含当前 TASK_ID。重点归拢：Windows/PowerShell 命令坑、"
        "中文 UTF-8/乱码坑、Godot 启动或静态校验坑、视觉验收不确定性、阻塞可重试性、需要提升到全局工程 skill 的规则。"
        "**失败/阻塞子树同样要归拢，不能只读已完成子树**：失败路径的经验比成功路径更值钱。"
    )
    if failure_block:
        description = f"{description}\n\n{failure_block}"
    return ChildTaskSpec(
        title=f"{parent.title}：归拢递归经验 Skill",
        description=description,
        scope=[*parent_scope, "docs/skill_notes", "parent skill-note merge"],
        acceptance=[
            "必须生成 docs/skill_notes/<TASK_ID>.json 或等价父层 skill note 文件，并在 changed_files 中列出。",
            "输出必须包含 source_child_ids 或 source_files，说明来自哪些子节点/子树。",
            "重复问题必须合并为一条，类似问题必须归为同一 category，并记录 merged_count 或 source_count。",
            "必须给出 promote_to_project_skill 列表；没有可提升项时写空列表并说明原因。",
            "必须验证 JSON 格式有效，并确认没有连续 `???`/`????` 问号乱码。",
            # R6
            "失败/阻塞子树必须各有一条经验（同一 category 可合并，但要记录 source_id 与 merged_count）；"
            "证据来源包括 block_report / blocked_child_reports / error_message / result_json"
            "（复盘块里每条都带 source=，被截断的部分按类别计数覆盖）。",
            "每条失败经验必须写明 category、retryable 与 parent_action，便于按类别跨树查询。",
        ],
        depends_on=dependency_titles,
        coverage_ids=[SKILL_NOTE_COVERAGE_ID],
        verification_kind=SKILL_NOTE_VERIFICATION_KIND,
        estimated_leaf=True,
    )


def with_parent_skill_note_child(
    parent: TaskNode,
    children: list[ChildTaskSpec],
    settings: Settings,
) -> list[ChildTaskSpec]:
    specs, _exemption = with_parent_skill_note_child_ex(parent, children, settings)
    return specs


def with_parent_skill_note_child_ex(
    parent: TaskNode,
    children: list[ChildTaskSpec],
    settings: Settings,
) -> tuple[list[ChildTaskSpec], str]:
    """返回 `(子任务清单, 只读豁免理由)`；理由非空 = **因为"整份产物只读"故意没建** skill note 子任务。

    返回理由而不是让调用方自己再判一次：理由必须与"真的没建"来自同一处判断，
    否则事件里会出现"声明豁免了、实际没跳过"这种自己骗自己的记录。
    """
    if not skill_note_tasks_enabled(settings):
        return children, ""
    if is_skill_note_task(parent, settings):
        return children, ""
    if any(is_skill_note_spec(child, settings) for child in children):
        return children, ""
    if has_skill_note_child(parent, settings):
        return children, ""
    # 只读约束下不建这个子任务：它的验收要求"必须生成 docs/skill_notes/<TASK_ID>.json"，
    # 与"不许新增任何文件"直接冲突（见 `readonly_constraint_reason` 的说明）。
    exemption = readonly_constraint_reason(parent)
    if exemption:
        return children, exemption
    main_children = [child for child in children if not is_skill_note_spec(child, settings)]
    if not main_children:
        return children, ""
    return [*children, parent_skill_note_child(parent, main_children)], ""
