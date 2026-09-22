# Codex Battlefield 资源池设计

## 目标

递归任务树的本质是长时间、多阶段、多角色的自动开发流程。单账号、单队列能保证稳定，但在大型游戏开发里会遇到三个问题：

1. 单个 Codex 账号或 API 池容易触发容量、速率、会员弹性或余额限制。
2. 不同任务需要不同能力：规划、编码、验收、生图、资产整理、文档汇总，不应该全部用同一个高成本通道。
3. 阻塞必须能上报给父节点，并由系统选择等待、换池、降级、重试或拆分，而不是直接失败。

资源池的目标不是盲目提高并发，而是让每一次真实消耗模型的调用都经过统一调度、可观测、可追责、可恢复。

## 核心概念

### 资源单元 Provider Slot

每一个可调用能力都抽象成一个 slot。

```yaml
id: codex-default-1
kind: codex_cli
label: Codex 默认账号 1
enabled: true
weight: 10
max_concurrency: 1
cooldown_seconds: 60
daily_budget_units: 100
capabilities:
  - planner
  - worker
  - verifier
  - acceptance
cost_tier: normal
env:
  CODEX_HOME: C:\Users\<user>\.codex-account-1
command:
  bin: codex
  extra_args: []
```

slot 不等于账号。一个账号可以暴露多个 slot，例如默认模式、强模式、只读验收模式。一个 API 网关也可以暴露多个 slot，例如便宜模型、强模型、生图模型。

### 能力类型 Capability

建议先定义这些能力：

- `planner`: 判断是否叶子、拆任务。
- `worker`: 真正改代码和生成内容。
- `verifier`: 叶子验收。
- `acceptance`: 父节点整合验收。
- `image_generation`: 生图、贴图、概念图、精灵图草案。
- `asset_validation`: 图片尺寸、格式、导入规则检查。
- `cheap_summary`: 低成本总结、日志压缩、状态摘要。

任务节点不是直接指定账号，而是声明需要什么能力、风险等级和成本偏好。

### 租约 Lease

每次模型调用前必须先拿资源租约。

```json
{
  "lease_id": "lease-...",
  "slot_id": "codex-default-1",
  "task_id": "node-...",
  "kind": "worker",
  "started_at": "...",
  "state": "active"
}
```

租约负责：

- 防止同一个账号同时被多个高消耗 Codex 进程打爆。
- 记录这次调用用了哪个账号、哪个 API、什么命令。
- 调用失败时能把失败归因到资源池，而不是混进任务质量问题。

## 调度策略

### 第一层：全局并发

保留当前 `max_concurrency=1` 作为最稳模式。未来可以扩展成：

- `global_active_model_calls`: 同时真实模型调用总数。
- `codex_cli_active_calls`: Codex CLI 调用总数。
- `image_active_calls`: 生图调用总数。

父节点等待子节点时不占用模型资源，只占 UI 状态。

### 第二层：slot 并发

每个账号 slot 单独限流。

```yaml
slots:
  codex-account-a:
    max_concurrency: 1
  codex-account-b:
    max_concurrency: 1
  aigw-image:
    max_concurrency: 2
```

这保证多个账号可以轮换，但单个账号不会被自身并发压垮。

### 第三层：角色路由

不同角色走不同池：

- planner：优先便宜、稳定、默认档。
- worker：默认档优先；高风险节点可升档。
- verifier：便宜且严格；尽量少跑工具探索。
- acceptance：父节点整合，优先稳定账号。
- image_generation：只走 AIGW 或图像专用 API。

### 第四层：健康状态

slot 有健康状态：

- `healthy`: 可用。
- `cooling_down`: 刚遇到 429、overloaded、quota、timeout，暂时冷却。
- `degraded`: 可用但近期失败率高，降低权重。
- `disabled`: 人工关闭。
- `exhausted`: 今日预算或余额耗尽。

调度时跳过不可用 slot，并把原因写进节点事件。

## 失败处理

### 容量类失败

比如 overloaded、429、rate limit、会员弹性不足、网关繁忙。

处理：

1. 标记当前 slot `cooling_down`。
2. 如果同能力还有其他健康 slot，换 slot 重试。
3. 如果没有，节点进入 `blocked`，上报：
   - 来源：`resource.capacity`
   - 分类：`external`
   - 可重试：是
   - 父层动作：等待冷却后重试，或切换资源池

### 质量类失败

比如 verifier 不满意、验收失败。

处理：

1. 不换账号作为第一反应。
2. 按当前铁律：叶子验收失败一次，必须转父节点拆分。
3. 父节点生成修复子任务。

### 工具类失败

比如 Godot 缺失、命令被策略拒绝、PowerShell 编码问题。

处理：

1. 先判断是否可由工具层修复。
2. 如果是环境问题，生成明确阻塞上报。
3. 如果是 prompt 或 runner 机制问题，优先修工具本身。

## 成本策略

### 默认耐用策略

当前建议：

- Codex CLI 不指定模型和 reasoning effort，走账号默认档。
- 全局真实 Codex 并发保持 1。
- 生图和资源生成可以独立池，但不要抢 Codex worker 槽。

### 升档条件

只有满足任一条件时才使用强 slot：

- 父节点 acceptance 连续失败，且证据显示是整合复杂度问题。
- verifier 发现跨模块架构风险，需要强推理做审查。
- 根节点或高层宪法生成，需要决定全局边界。
- 用户手动把某节点标为“强模式重试”。

### 降档条件

满足任一条件就降档：

- 节点只是文档、README、检查脚本、目录整理。
- verifier 只需读文件和检查结构。
- planner 只是判断一个很小范围是否叶子。

## 生图池设计

生图不要伪装成 Codex worker。它应该是单独 capability：`image_generation`。

推荐流程：

1. Codex worker 生成资产需求 JSON：

```json
{
  "asset_id": "hero_idle_sprite",
  "purpose": "player idle animation sprite sheet",
  "style_contract": "2D arcade brawler, crisp pixel/painted hybrid",
  "size": "1024x1024",
  "frames": 8,
  "acceptance": [
    "transparent background",
    "readable silhouette",
    "consistent frame alignment"
  ]
}
```

2. Orchestrator 创建 `image_generation` 子节点。
3. AIGW slot 生成图像。
4. asset verifier 检查尺寸、透明度、文件名、导入路径。
5. 父节点 acceptance 才把图像资产合入 Godot 项目。

这样 Codex 不需要自己拿生图密钥，也不会把生图失败误判成代码失败。

## 数据模型建议

新增表：

### `resource_slots`

- `id`
- `kind`
- `label`
- `enabled`
- `weight`
- `max_concurrency`
- `cooldown_until`
- `health_status`
- `daily_budget_units`
- `used_budget_units`
- `capabilities_json`
- `config_json`
- `created_at`
- `updated_at`

### `resource_leases`

- `id`
- `slot_id`
- `task_id`
- `run_id`
- `kind`
- `status`
- `started_at`
- `finished_at`
- `failure_category`
- `failure_message`

### `resource_events`

- `slot_id`
- `task_id`
- `event_type`
- `message`
- `payload_json`
- `created_at`

## 与当前代码的接入点

当前最适合的改造点：

1. `Orchestrator` 不再只创建一个裸 `asyncio.Semaphore`，而是创建 `ResourceScheduler`。
2. `_run_codex_io()` 调用前向 scheduler 请求 lease。
3. lease 返回 slot 后，runner 根据 slot 生成命令和环境变量。
4. 调用完成后 scheduler 根据结果更新 slot 健康状态。
5. `stats` 返回资源池摘要，让 Web 看板显示：
   - 当前使用 slot
   - 冷却中 slot
   - 今日消耗
   - 最近失败原因

## 推荐最小版本

第一版不要做太大，先实现这些：

1. 配置文件 `resource_pool.yaml`。
2. 支持多个 `codex_cli` slot。
3. 每个 slot 有独立 env 和 max_concurrency。
4. 遇到 transient/capacity 失败自动换 slot 重试。
5. 看板显示当前 slot、冷却 slot、可用 slot 数量。
6. 节点阻塞原因明确写出是任务质量问题还是资源池问题。

第二版再做：

1. API key / AIGW HTTP provider。
2. 生图 capability。
3. 预算统计。
4. 节点级升档/降档。
5. 用户在看板上手动禁用/启用 slot。

## 人类怎么用

人类不应该直接管每个节点用哪个账号。人的玩法应该是：

1. 在资源池页面配置账号、API、AIGW。
2. 设置默认策略：省钱优先、速度优先、质量优先。
3. 启动任务树。
4. 看板观察当前战场在哪个节点、哪个 slot 在跑。
5. 如果资源阻塞，选择：
   - 等待冷却
   - 切换账号池
   - 降档继续
   - 手动强模式重试

## 关键原则

- 账号池只解决资源问题，不替代任务拆分质量。
- 质量失败必须递归拆分，不应该靠换账号硬冲。
- 资源失败必须可重试、可换池、可上报。
- 生图是独立能力，不应该混在 Codex CLI worker 里。
- 默认耐用，必要时升档。
- 所有资源调用都要有租约、日志和失败归因。
