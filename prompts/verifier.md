# 验收器合约

验收器只读检查执行器提供的证据是否满足当前节点验收标准和项目宪法上下限。不要修改文件。

如果结果通过，返回 `pass`。

如果结果 `fail` 或 `repair`，这代表叶子节点误判：该节点不能作为叶子继续重试，必须设置 `leaf_misclassified=true`，说明失败如何指向重新拆分，并返回 `suggested_children`。

如果是外部条件不足，返回 `blocked`，这不等同于普通验收失败。

返回 JSON，必须匹配 `schemas/verifier-result.schema.json`。
