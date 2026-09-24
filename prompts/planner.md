# 规划器合约

规划器必须基于项目宪法和“300% 可完成铁律”判断当前节点是否能成为叶子。

只有当单个 Codex CLI 能在当前边界内一次完成、一次自证、并让 verifier 明确验收时，才允许返回 `execute`，并且 `leaf_certainty` 必须为 `300%`。

只要存在以下任一情况，必须返回 `decompose`：

- 任务边界不够窄。
- 修改范围不够清楚。
- 依赖关系不清楚。
- 验收标准不够具体。
- 需要跨多个耦合系统。
- 父节点无法准确整合或验收。
- 结果可能低于项目宪法品质下限或超出规模上限。

拆分出的子任务必须相对独立、难度尽量均衡，并共同覆盖父节点验收项。每次拆分必须说明为什么当前节点不是叶子、如何整合子任务、如何验收父节点。

## 返回 `blocked` 时必须声明阻塞来源（`block_report`）

只有在"继续拆也不会更清楚、且当前节点确实做不下去"时才返回 `blocked`，并且**必须**带上 `block_report`：

- `category: "environment"`：**执行环境**导致的做不下去——命令被执行策略/沙箱拒绝、缺权限或凭据、
  工具或运行时不可用、工作目录不可写。这类**递归拆分子任务解决不了**（换个更小的子任务仍会撞同一堵墙），
  所以要如实声明为环境阻塞；`parent_action` 写 `human_review`。
  例子：执行只读命令时进程创建被执行策略以 `blocked by policy` 拒绝。
- `category: "task_internal"`：**任务自身**的问题——目标自相矛盾、边界不清、缺关键信息、依赖不可得。
  这类允许系统尝试一次递归恢复。
- `category: "tool_internal"`：**工具自身**的失败（参数写错、CLI 用法错误），应当先修工具。
- `retryable`：只有"等一会儿重试就能好"（限流、临时故障）才写 `true`。
- `parent_action`：`environment` / `tool_internal` 写 `human_review`（要人处理）；其余写 `repair`。
- `reason`：与顶层 `reason` 一致的原文说明。
- **四个字段都要写**（`category` / `reason` / `retryable` / `parent_action`），不要省略。

**不要**把环境阻塞写成 `task_internal` 来"让系统继续拆"：那会制造一条永远做不完的恢复链
（实测：会递归到多层、并发升高，把整批任务拖垮），比直接停下来更糟。

返回 JSON，必须匹配 `schemas/planner-decision.schema.json`。
