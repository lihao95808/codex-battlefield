"""集成冲突的识别、结构化记录与上浮。

**为什么需要这个模块**：`_integrate_child_outputs` 原来把子节点产物按 `changed_files` 拷进
父节点集成目标，遇到问题只有一条出路——静默。具体是三处：

1. 函数返回 `None`，父节点验收拿不到"有几个文件没进来"；
2. `node_meta["parent_integration"]` 只存 `*_count`，明细只进事件流，**不进验收 prompt**；
3. **同一路径被两个子节点产出时完全无标记**：后写 `shutil.copy2` 覆盖先写，
   而 `integrated` 里是两条"成功"记录。

第 3 条是真丢失：被覆盖的那份产物再也不会出现在任何地方。按 AgenticFlict（AIware '26）
实测，AI 生成 PR 的文本冲突率是 **27.67%**（29,609/107,026，共 336,380 处），
低 churn（中位 2 行）也有约 9.9%——即静默覆盖不是边角情况。

本模块只做**纯判定与文书**：不读文件、不写文件、不碰数据库，因此可以脱离 orchestrator 单测。
真实的拷贝动作仍留在 orchestrator 里。

依赖方向：只依赖标准库，**不得**导入 `orchestrator` 或 `models`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 冲突类型
CONFLICT_PATH_COLLISION = "path_collision"
CONFLICT_DESTRUCTIVE_OVERWRITE = "destructive_overwrite"

# 处理策略
STRATEGY_OVERWRITE = "overwrite"
STRATEGY_KEEP_BOTH = "keep_both"
STRATEGY_REPAIR = "repair"
VALID_STRATEGIES = (STRATEGY_OVERWRITE, STRATEGY_KEEP_BOTH, STRATEGY_REPAIR)
DEFAULT_STRATEGY = STRATEGY_OVERWRITE

# 上浮到验收 prompt 的明细条数上限。父节点验收是"真实验收"，但它不该被上万条冲突淹没；
# 超出部分只保留计数，并在 meta 里标 `truncated`，避免"看不见"被误当成"没有"。
DEFAULT_DETAIL_LIMIT = 20


def normalize_strategy(value: Any) -> str:
    """把配置值收敛到合法策略；非法值回退默认并保留可诊断信息。"""
    text = str(value or "").strip().lower()
    return text if text in VALID_STRATEGIES else DEFAULT_STRATEGY


def detect_path_collisions(integrated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出**同一相对路径被多个子节点产出**的情况。

    输入是每次成功拷贝的记录（含 `child_id` / `output_node_id` / `rel` / `destination`）。
    输出每个冲突路径一条记录，带全部产出方，便于父节点裁决而不是替它选一个。
    """
    by_rel: dict[str, list[dict[str, Any]]] = {}
    for entry in integrated:
        rel = str(entry.get("rel") or "")
        if not rel:
            continue
        by_rel.setdefault(rel, []).append(entry)

    conflicts: list[dict[str, Any]] = []
    for rel, entries in sorted(by_rel.items()):
        writers = list(dict.fromkeys(str(entry.get("output_node_id") or entry.get("child_id") or "") for entry in entries))
        if len(entries) < 2 or len(writers) < 2:
            # 同一个产出方重复写同一路径不算冲突（可能是 changed_files 里重复列出）
            continue
        conflicts.append(
            {
                "type": CONFLICT_PATH_COLLISION,
                "path": rel,
                "destination": str(entries[-1].get("destination") or ""),
                "writers": writers,
                "writer_count": len(writers),
                "modes": [str(entry.get("mode") or "") for entry in entries],
                "kept": str(entries[-1].get("output_node_id") or entries[-1].get("child_id") or ""),
                "overwritten": writers[:-1],
            }
        )
    return conflicts


def destructive_overwrite_conflict(
    *,
    rel: str,
    destination: str,
    mode: str,
    output_node_id: str,
    destination_existed: bool,
) -> dict[str, Any] | None:
    """目标已存在、且走了**无合并语义**的覆盖路径时，单独记一条。

    `project.godot` 与 `README` 走的是有合并语义的分支（section 合并 / 追加），不算破坏性覆盖；
    目录的 `rmtree + copytree` 与普通文件的 `copy2` 都是无条件覆盖，目标原有内容不会留下任何痕迹。
    """
    if not destination_existed or mode in {"merge_project_godot", "append_readme", "unchanged"}:
        return None
    return {
        "type": CONFLICT_DESTRUCTIVE_OVERWRITE,
        "path": rel,
        "destination": destination,
        "mode": mode,
        "overwriter": output_node_id,
    }


@dataclass
class IntegrationReport:
    """一次集成的结构化结果。返回给调用方，而不是丢在事件流里。"""

    target: str = ""
    target_kind: str = ""
    parent_worktree_path: str | None = None
    strategy: str = DEFAULT_STRATEGY
    integrated: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def blocking(self) -> bool:
        """`repair` 策略下冲突是**阻断性**的：父节点不得在未处理时视为通过。

        `overwrite` / `keep_both` 下冲突仍需上浮与裁决，但不阻断本次运行——
        这是用户选定的默认行为，避免改变现有跑批。
        """
        return self.strategy == STRATEGY_REPAIR and bool(self.conflicts)

    def to_meta(self, detail_limit: int = DEFAULT_DETAIL_LIMIT) -> dict[str, Any]:
        """写成 `node_meta["parent_integration"]`。

        保留原有的 `integrated_count` / `skipped_count` 键（调用方与既有测试依赖它们），
        并新增**明细**。明细必须进 meta：验收 prompt 直接读这个结构
        （`runner._acceptance_prompt` 的 `PARENT_INTEGRATION_JSON`），所以放进来就等于上浮。
        """
        return {
            "target": self.target,
            "target_kind": self.target_kind,
            "parent_worktree_path": self.parent_worktree_path,
            "strategy": self.strategy,
            "integrated_count": len(self.integrated),
            "skipped_count": len(self.skipped),
            "conflict_count": len(self.conflicts),
            "blocking": self.blocking,
            "conflicts": self.conflicts[:detail_limit],
            "conflicts_truncated": max(0, len(self.conflicts) - detail_limit),
            "skipped": self.skipped[:detail_limit],
            "skipped_truncated": max(0, len(self.skipped) - detail_limit),
            "integrated_files": sorted({str(entry.get("rel") or "") for entry in self.integrated if entry.get("rel")}),
        }

    def summary_text(self) -> str:
        """人类可读的一句话摘要，用于事件消息。"""
        parts = [f"整合 {len(self.integrated)} 个文件"]
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 个")
        if self.conflicts:
            parts.append(f"**冲突 {len(self.conflicts)} 处**")
        if self.blocking:
            parts.append("（repair 策略：阻断验收）")
        return "，".join(parts) + "。"


def collision_directive(meta: dict[str, Any]) -> str:
    """给父节点验收的**显式指令块**。

    为什么不能只靠 `PARENT_INTEGRATION_JSON`：那个 JSON 是"资料"，模型可以读也可以忽略。
    静默的反面不是"把数据放在 prompt 里"，而是"**要求它对每一条作出裁决**"。
    无冲突时返回空串，避免给绝大多数正常路径增加 prompt 负担。
    """
    conflicts = meta.get("conflicts") if isinstance(meta.get("conflicts"), list) else []
    if not conflicts:
        return ""
    truncated = int(meta.get("conflicts_truncated") or 0)
    blocking = bool(meta.get("blocking"))
    lines = [
        f"INTEGRATION_CONFLICT_RULE: 本次父节点集成检测到 {len(conflicts)} 处**路径冲突**"
        f"（同一路径被多个子节点产出，后写覆盖先写），另有 {truncated} 处未在下方列出。\n",
        "这些冲突**不是警告**：被覆盖的那份产物已经不在集成目标里。\n",
        "你必须在 acceptance 的 checks/evidence 中**逐条**说明每个冲突路径最终保留了哪一份、"
        "为什么这样取舍、被丢弃的那份是否有不可替代的内容。\n",
        "如果无法判定，或被覆盖的那份含有集成目标缺失的能力，必须返回 repair 并给出聚焦的 suggested_children；"
        "禁止在没有给出裁决理由的情况下判定通过。\n",
    ]
    if blocking:
        lines.append(
            "INTEGRATION_CONFLICT_BLOCKING: 当前策略为 repair，冲突未处理前**不得**判定通过。\n"
        )
    for conflict in conflicts:
        lines.append(
            f"- {conflict.get('path')}：保留 {conflict.get('kept')}，"
            f"被覆盖 {', '.join(str(item) for item in conflict.get('overwritten') or [])}\n"
        )
    if truncated:
        lines.append(f"- 其余 {truncated} 处见 PARENT_INTEGRATION_JSON 的 conflicts_truncated。\n")
    lines.append("\n")
    return "".join(lines)
