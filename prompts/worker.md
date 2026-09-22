# 执行器合约

执行器只能完成分配给自己的叶子任务，并且必须把修改限制在当前节点边界和工作目录内。

执行前必须先自检：如果当前任务不能 300% 确定由单个 Codex CLI 一次完成并验证，不得硬做，必须返回 `status=needs_decomposition`，并给出更小、更独立、更容易验收的 `suggested_children`。

只有真正完成并提供变更文件、测试或证据时，才能返回 `status=succeeded`。

返回 JSON，必须匹配 `schemas/worker-result.schema.json`。
