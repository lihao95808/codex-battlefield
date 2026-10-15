# 父子门控与债务传递 SPEC（树状优先）

状态：设计草案，待你确认 v1 范围
续跑锚点：`PLAN-STATUS.md`
关系：本文件**吸收并取代** `docs/spec-constitution-amendment.md` 中 A2/A5 的"版本戳 + 再验证队列"部分（那两部分是这里的一个特例，不应各做一半）；R4 的**识别**部分已实现（`api/app/integration.py`，提交 `92d942c`），本文件负责它的**门控**部分。

## 1. 问题

任务树里，父节点"能否完成"今天由**子节点状态**判定。这漏掉三类真实问题：

| 缺口 | 今天的判据 | 后果 |
|---|---|---|
| R4b | 只看 `child.status ∈ {blocked, failed}`（`orchestrator.py:3981-3986`） | 子节点**全是 DONE** 但集成存在 `path_collision` 时，父节点照样进入验收 —— **R4 记了冲突，却没有任何人拦** |
| 宪法版本 | 没有版本维度 | 宪法收紧后，已完成的旧版产物无人复查 |
| 约束漂移 | 没有约束核对 | 父节点自己的 must/never 不下沉到子节点，也不做覆盖核对 |

三者的共同点：**坏消息没有到达"有权力做决定的那一层"**，或者到达了但没有阻断力。

## 2. 外部依据（附置信度，不混用）

| 来源 | 借鉴点 | 置信度 |
|---|---|---|
| MasDrift Benchmark（arXiv 2608.07556）、Agentic Drift | 约束沿委托链**逐跳侵蚀、随深度累积**；"委托≠安全边界"；治理建议含**约束传播** | 检索所得，未取原文 |
| HMAS 分类（arXiv 2508.12683） | 分层多 agent 的控制层级/信息流维度划分 | 检索所得，未取原文 |
| 终止条件研究综述 | **不能依赖 agent 自报告终止**；需硬限制 + 语义谓词双轨 | 检索所得，未取原文 |
| Kubernetes finalizer | 带 finalizer 的对象**删不掉**，直到控制器清除它 | 我按已知机制描述，本次未取原文 |
| Pregel / BSP halt | 收敛条件 = 所有顶点 vote to halt **且无 pending 消息** | 我按已知机制描述，本次未取原文 |
| Temporal Parent Close Policy | 父结束时子的三种命运：TERMINATE / ABANDON / REQUEST_CANCEL | 我按已知机制描述，本次未取原文 |
| 构建系统 affected 传播（Bazel/Nx） | 失效沿**反向依赖边**传播 | 已知机制 |

其中**与我们形态最贴切**的是最后四条：它们解决的都是"**有未清事项时，上层不得收敛**"。

## 3. 现状事实（全部实测，带行号）

| 事实 | 位置 | 含义 |
|---|---|---|
| 父节点**已有**"子节点阻塞/失败则不启动整合验收" | `_accept_parent` `:3981-3999`（含 `:3988` 的 synthetic run 文案"一个或多个子节点阻塞或失败，未启动整合验收。"） | **门控机制已存在**，问题只是判据太窄 |
| **已有**豁免机制：被已完成的修复子任务取代的失败子节点不算阻塞 | `_superseded_by_done_repair` `:2267`，用于 `:3967` / `:3985` | 豁免已存在，且有结构（`superseded_by_repair_child_ids`） |
| **已有**坏消息写入通道 | `_store_child_block_reports` → `node_meta["blocked_child_reports"]` | 债务表可以建在它上面，属**一般化**而非另起炉灶 |
| 宪法是**拷贝继承**而非文本重述 | 子节点自父拷贝 `:6715-6717` | 结构上**已抗漂移**（这点我们做对了） |
| R4 已能识别冲突与跳过明细 | `integration.py`；写入 `meta["parent_integration"]` 的 `conflicts` / `skipped` | 识别有了，**门控没有** |
| 重试性阻塞已有单独通路 | `_retryable_blocked_children` `:2254` | 属 `child_blocked` 债的一个子类 |

**结论：缺的不是机制，是判据。**

## 4. 统一抽象：债务（Obligation）

> **父节点能否验收/完成，不应由「子节点状态」决定，而应由「入站债务集合是否为空」决定。
> 子节点状态只是债务的一种来源。**

两个方向的传递，合起来才完整：

| 方向 | 传什么 | 载体 | 抗漂移要求 |
|---|---|---|---|
| **下沉** parent → child | 约束 + 版本 | 结构继承（`constitution_json`、`constitution_version`、父约束清单） | **禁止文本重述**；重述就会逐跳侵蚀 |
| **上浮** child → parent | 债务 | `node_meta["obligations"]` | 必须**结构化**且**幂等**；口头上报会丢 |

**为什么"债务"比"状态"好**：状态是节点**自己的**（done/blocked），债务是**属于父节点的**（入站）。
后者才能表达"子节点已经 DONE，但它留下了一件没解决的事"——而这正是 R4b 与版本债务的形态。

## 5. 债务表 schema

存于父节点的 `node_meta["obligations"]`（list），与既有 `blocked_child_reports` **并列并存**（后者保留兼容，逐步由前者派生）。

| 字段 | 说明 |
|---|---|
| `id` | 幂等键：`{type}:{source_node_id}:{discriminator}`，例如 `integration_conflict:node-a:src/shared.gd` |
| `type` | 见 §5.1 |
| `source_node_id` | 由谁产生（子节点或自身） |
| `severity` | `blocking` / `advisory`（见 §6.2） |
| `created_at` / `settled_at` | 生命周期 |
| `settled_by` / `settle_reason` / `settled_actor` | **清债必须留痕**；`settled_by` ∈ {`repair_child`, `version_catchup`, `round_clear`, `human`}。`human` 必须同时给 `reason` 与 `actor`（身份）——"放宽不是禁止的，**无声的**放宽才是" |
| `payload` | 类型专属明细（冲突路径、版本号、缺口描述…） |

### 5.1 债务类型

| 类型 | 来源 | severity | 今天 |
|---|---|---|---|
| `child_blocked` / `child_failed` | 子节点状态（**既有**） | blocking | 已门控 |
| `integration_conflict` | `meta["parent_integration"].conflicts`（`path_collision` / `destructive_overwrite`） | **由集成策略决定**：`repair` → blocking；`overwrite` / `keep_both` → advisory（见下方"严重度必须在生成时定"） | **R4b 已接线** |
| `integration_skipped` | 同上 `skipped` | 三档分明：`absolute path` → advisory；`outside integration boundary` → **blocking（与档位无关：越界是边界契约被破坏）**；`source missing` → **由档位决定**（同冲突） | **R4b 已接线** |
| `constitution_stale` | `node.constitution_version < root.effective_version` | blocking（tighten）/ advisory（loosen） | **已接线**（A4/A5） |
| `revalidate_required` | tighten 后已 `done` 的节点 | blocking | **已接线**（A5） |
| `constraint_coverage_gap` | 子节点集合没有逐字继承父节点的约束清单（`global_acceptance`） | **由规则显式声明**：默认 advisory（只记录并上浮），`constraint_coverage_gap_blocks=1` 时 blocking | **已接线**（O-A5，创建时就核对） |

**严重度必须在生成时定，不能由门控按开关过滤。** 这是实测踩到的一条：第一版把
`integration_conflict` 写进 `ALWAYS_BLOCKING`（即严重度恒为 blocking），却让门控按
`integration_conflict_strategy` 去**过滤类型**。后果是同一个父节点上"看板报 blocking、树照跑"
同时成立——看板就不再回答"树有没有被卡"，§9 存在的理由当场失效。修法是把严重度做成策略的函数
（`obligations.conflict_severity`），门控回到 §6.1 那条无条件判据。

**另外修正过一次 `source missing` 的严重度**（原文把 `source missing` 与越界一起一律列为
blocking）。改成"由档位决定"不是为了让测试变绿，而是原文把默认档悄悄变成了阻断档：实测有
4 条既有契约（worker 阻塞 → 叶子转父节点的用例，其 mock 子节点声明了并不落盘的
`changed_files`）当场从 `done` 变成 `repair`。用户在集成冲突上选定的是"默认 overwrite：
记录并上浮，后写仍覆盖"，默认档下把"声明了却没产出"硬挡下来与那个选择自相矛盾；
严格档（`repair`）仍然阻断。越界**不跟着放宽**——那是边界契约被破坏，不是产物完整性策略。

### 5.2 幂等与去重

同一冲突在多次集成中被重复识别时**不得重复记债**。以 `id` 为键 upsert：已存在且未清则只更新 `payload` 与时间戳。

## 6. 门控判据

### 6.1 父节点 `done` 的四个必要条件（全部可机器判定）

1. 所有子节点处于终态（既有）；
2. **入站债务中不存在 `severity == blocking` 且未清（`settled_at` 为空）的项**；
3. 父节点 acceptance agent 的真实验收通过（既有）；
4. 失败/阻塞子树已产出复盘条目（R6，后续）。

判据 2 就是本设计要替换掉的那一行：从 `any(child.status in {BLOCKED, FAILED})`
改为 `any(ob.settled_at is None and ob.severity == "blocking" for ob in obligations)`。

**判据 2 是无条件的**（第二版实现曾偏离过，已修）：门控不按类型/档位/开关去过滤，
只看 `severity`。"要不要阻断"这件事在**债务生成时**由规则定死，不在收尾处再判一次——
收尾处再判一次就会与看板说的不一致（见 §5.1 末的说明）。配置项只控制**行为**，不控制严重度：
例如 `constitution_revalidate_enabled=0` 只是"不要花 token 去跑再验证"，版本债照旧如期阻断，
最终由升级通路交给人——而不是被静默放行。

### 6.2 为什么必须分 severity 而不是一概阻断

一概阻断会把树卡死（例如一个 `absolute path` 的跳过本就无害）。
但**降级为 advisory 必须由规则显式声明**，不能由产出方自行判断——否则又回到"放宽断言变绿"。

### 6.3 清债的三个通路（与既有机制的关系）

| 通路 | 语义 | 与现状的关系 |
|---|---|---|
| `repair_child` | 修复子任务完成，债随之清掉 | **`_superseded_by_done_repair` 的一般化**——它从"特例判断"变成"债被清掉"的一种来源。已接线：`child_blocked` / `child_failed` 的清偿用它（§7.3） |
| `version_catchup` | 节点追平到当前版本后重新验收通过 | 新增（宪法传播 A5），**尚未接线** |
| `round_clear` | **本轮集成不再复现**该冲突/跳过 | 新增（§6.4 硬约束 2 的落地）。只作用于**轮次域**类型（`integration_conflict` / `integration_skipped`）；版本类债务不由轮次定义，绝不能被它清掉 |
| `human` | 人显式清债，必须给 `reason` **与 `actor`** | **已接线**（入口见 §8.2 实现状态）；K8s "移除 finalizer" 的对应物 |
| `upward_clear` | 那一支**已无未清阻断事项** | 新增（§7.2.3）：按事实清掉父节点上的上浮事实 |
| `coverage_cleared` | 子节点集合**已逐字继承**该约束 | 新增（§7.1.4）：按事实清掉覆盖缺口 |

### 6.4 接线时必须满足的两条硬约束（**实测教训，不是理论顾虑**）

第一版接线已实现并**回退**。它把门控放在"验收通过之后、标 done 之前"（位置是对的），
但在该分支里**自行创建修复子任务**，于是形成自增强循环：

| 环节 | 发生的事 |
|---|---|
| 第 N 轮 | 集成发现 `src/x.gd` 冲突 → 记债 → 门控拦下 → 创建"解决集成冲突"修复子任务 |
| 第 N+1 轮 | 修复子任务也是子节点，其 `changed_files` 含 `src/x.gd` → 集成看到**新的产出方** → 记一条**新债** → 再建修复子任务 |
| 结果 | 子任务与债务一起无限增长 |

**实测症状**（保留原始观测，不写成"有点慢"）：
单个用例跑 **5993 秒（1h39m53s）** 后失败，报 `RecursionError: maximum recursion depth exceeded`
（栈顶在 `sqlalchemy/sql/cache_key.py:272` —— 任务集合膨胀导致 Select 的 cache key 递归爆栈），
过程中同一测试进程持续空转，另一次全量跑耗时报 **46856 秒（13h）**。

**因此下一版接线必须满足**：

1. **门控不得创建子任务。** 它只在收尾处**拒绝标记 done**，并把裁决交给 agent（验收 prompt 里
   的冲突指令已经能让 agent 产出 repair 子任务）或交给人。系统自己注入子任务 = 自己给自己造新债。
2. **清债必须有界且可判。** 不能用"修复子任务完成"作为清债条件——修复子任务完成但冲突仍在时，
   门会永远关着。可用条件是**"本轮集成不再复现该冲突"**（同一件事在同一轮不再出现即清），
   它天然有界：不依赖谁完成了什么。

另需注意：`max_acceptance_repair_cycles` 默认 **0 = 不限次**（本文件 §3 的 D1 遗留问题），
所以**不能指望修复轮次上限兜住循环**——第一版正是踩了这一点。

已保留并测试的部分：`api/app/obligations.py`（纯模块：幂等键、severity 分级、清债留痕、
汇总恒等式、版本债务方向敏感、轮次域清债）与它的单元测试。

**第二版接线（已完成，见 `api/tests/test_obligation_gate.py`）**按上面两条硬约束重做，并额外
修掉一个让"有界"失效的缺陷：上一轮集成写进目标的文件，在第二轮会被读成"破坏了既有文件"
（`destructive_overwrite`），于是每重跑一次集成就重新报一次冲突——债务永远清不掉、门控永远
关着。现在按上一轮的 `integrated_files` 排除掉我们自己放的文件。这条不是理论顾虑：它是
"清债看似有界、实则永远不清"的直接成因，被 `test_conflict_that_no_longer_reproduces_settles_and_unblocks_done`
钉住。

## 7. 两条方向的传递规则

### 7.1 下沉（parent → child）

1. **版本必须单调继承**：`child.constitution_version = max(parent.constitution_version, 本节点建立时头的版本)`。防止子节点用旧版语义反向覆盖父节点已升级的判据。
2. **约束必须结构继承，不得文本重述**：宪法的 `constitution_json` 已经是拷贝（`:6715-6717`）——**保持这一点**，任何"让 planner 重新生成约束文本"的做法都是引入漂移。
3. **父节点自身的 must/never 也要下沉**：目前只传宪法，父节点自己的 `acceptance_json` 不下沉。

**实现状态（已完成，见 `constraints.inherit_down`）**

- 子节点拿到的是**祖先链 + 直接父节点**的并集（按归一化去重，祖先在前、直接父节点在后，
  越靠后越具体）。只看直接父节点的话，第 3 跳以后没人能说出第 1 跳原本要求什么——MasDrift 的
  核心发现就是这种逐跳侵蚀。
- 落在 `node_meta["inherited_constraints"]`，**不写进子节点的 `acceptance`**：acceptance 是验收
  判据，会直接进 worker/verifier 的判定；把父节点的整合级条目（例如"组合结果必须可玩"）压到
  叶子身上会让叶子验收必然失败——那是制造失败，不是防漂移。下沉的是**约束**，不是把父节点的
  判分标准摊给每个子节点。
- 约束通过 prompt 的**独立区块**给到每个 agent（`INHERITED_CONSTRAINTS_JSON`，planner / worker /
  verifier / acceptance 四处都有）：只躺在 `node_meta` 里等于没下沉——agent 看不到就不会遵守，
  改写了也没人发现。
4. **反向覆盖核对（MasDrift 的对策）**：子节点集合的 `acceptance` / `scope` 必须**覆盖**父节点约束清单；
   不覆盖则产生 `constraint_coverage_gap` 债。这是唯一一条"在创建时就拦"的债务，其余都是"完成时拦"。

**实现状态（已完成，见 `api/app/constraints.py`）**

- **判据是归一化后的严格相等，不是文本相似度**。相似度必然误报（"看起来相关"≠"同一条约束"），
  而一旦误报，人就会去调阈值，最后这条判据变成噪声。用严格相等还顺手把 §7.1.2 变成机器可判的：
  **改写 = 缺口**，这正是"重述就是侵蚀第一步"的形式化。刻意选的偏移方向是**宁可漏、不可误**：
  漏报会让人看到缺口去补，误报会让人学会忽略它。
- 归一化只做空白折叠与大小写统一；**不做**标点归一、同义词、词干化——每多一步"聪明"处理，
  就多一分把两条不同约束判成同一条的机会。
- **只核对宪法的 `global_acceptance`**，不核对父节点自己的 `acceptance`：父节点自己的条目里
  常混着"只有父节点才能判"的整合级判据（例如"组合结果必须可玩"），要求子节点逐字重复是错的。
- **严重度由规则显式声明**：默认 advisory，`constraint_coverage_gap_blocks=1` 才 blocking。
  这是量出来的：默认 blocking 之后全量回归**当场 14 条既有契约变红**——LLM 规划器会改写措辞，
  于是每棵树的父节点永远拿不到 `done`。默认阻断等于把"重述"这个现实判成每一棵树都失败。
  同时把"必须逐字继承"写进了**planner prompt**（生产侧的成因对策），两边一起才成立。
- **创建时就核对，但不拦创建**：同一个 prompt 再跑一次只会得到同一个改写，拦住创建等于把树钉死；
  缺口立即写成事件 + 债务 + 看板，让 agent 下一次规划有机会补、让人有机会裁定。
- 清债按事实：子节点集合已逐字继承即 `coverage_cleared`。

### 7.2 上浮（child → parent）

1. 子节点产生的债务写入**父节点**的 `obligations`（不是子节点自己的）。
2. **沿深度累积**：`constraint_coverage_gap` 与 `constitution_stale` 必须记下**发生的深度与链路**，否则事后无法判断漂移是第几跳引入的（MasDrift 的核心发现就是深度累积）。
3. 父节点的 `blocking` 债务会**继续上浮**为其父节点的债务（不是把子节点的债全量复制，而是"我这层还有未清事项"这一条事实）。避免债务在全树翻倍。

**§7.2.3 实现状态（已完成）**：`obligations.upward_blocking_debt(parent, child, ...)` 为
**每一支**记**一条**事实（幂等键 `child_blocked:{父}:{子}`，`payload` 只带计数与分类，不带明细），
`settle_upward_cleared` 按事实清债（那一支已无未清事项即清）。链条**逐层长出来**：本节点记它
直接子节点的情况，祖父那层跑同一个函数自然接上——所以"整棵树卡在第几层"可以按层读。

**位置踩过一次坑**：第一版把刷新放在"子节点全 `done` 之后"（集成那一步旁边）。结果这个节点停止
推进的**大多数**情形（子节点阻塞 / 修复中 / 等待）都会在那之前各自 `return`，于是刷新几乎不跑，
上浮事实基本不会出现——接线看起来在、实际是死的。现在放在 `_accept_parent` 的**流程入口**，
按事实重算、幂等、多跑无害。用例 `test_refresh_happens_on_the_natural_path_even_when_children_are_stuck`
专门钉住"子节点未完成时也要记"。

`integration_conflict` 这类债现在也带 `depth`（原先只有会沿委托链累积的类型带）：看板的
`by_depth` 要回答"债务集中在第几层"，缺了它就答不完整。

### 7.3 与现有 `blocked_child_reports` 的关系

保留 `blocked_child_reports`（兼容既有测试与看板），但**由债务表派生**：
`child_blocked` / `child_failed` 债务即为它的内容。这样不做"两套真相"。

**实现状态（已完成）**：`obligations.child_status_debt(...)` 为每个阻塞/失败子节点写一条
`child_blocked` / `child_failed` 债（幂等键 `{类型}:{父}:{子}`，`payload["report"]` 保留原来的
阻塞报告，所以派生的列表与原来**逐字同形**——既有消费方不用改）。
`obligations.child_status_reports(obligations)` 从**未清的**这类债重建报告列表，
`_refresh_child_status_obligations` 同时负责写与清（清债通路 `repair_child`，见 §6.3）。

**踩到的一处**："已被修复分支取代"的阻塞子节点不能再算"还堵着"。第一版只看 `status`，
于是修好了与还堵着同时成立，债永远清不掉——两条既有契约当场变红。现在沿用既有的
`_superseded_by_done_repair` 判据（同一个判据，不再是第二套）。

## 8. 死锁与逃生舱

1. **不设自动过期**。债务自动过期等于静默放宽，正是反缩水契约 §7.3 禁止的。
2. 逃生舱只有 `human` 清债，且**必须留 `reason` 与 `actor`**；清债写 `obligation.settled_human` 事件
   （事件名带通路：机器清债与人工清债在事件流里必须一眼分得开）。
   **实现状态（已完成）**：
   - `Orchestrator.settle_obligation(task_id, obligation_id, *, actor, reason)`
   - `POST /api/tasks/{task_id}/obligations/settle`（`obligation_id` 走**请求体**，不走路径参数：
     它形如 `integration_conflict:<父节点>:src/x.gd`，带冒号**和斜杠**，塞进路径会被切碎）
   - 债务明细在 `GET /api/tasks/{task_id}` 的 `obligations` 字段里列出——**不在详情里列出来，
     人就不知道该清哪一个**，逃生舱等于没有门把手。
   - 清债会把 `obligation_gate.consecutive_refusals` 归零。不归零的话，人这次放行只换来一次
     机会，下一轮同样的债立刻升级——那不是"打开门"，只是"门缝"。
   - 清债**不**自动把节点重新跑起来：`blocked` 是终态，继续干活是既有的
     `POST /api/tasks/{task_id}/retry`。"这条债无害"与"继续干"是两个判断，工具不替人合并它们。
3. **看板必须暴露**：树里有没有 `blocking` 债务、卡在哪一层、卡了多久（时效性是"卡死"与"在干活"的唯一区分）。
   **实现状态**：`get_tree` 的 `stats.obligations` 已完成（见 §9）；前端条带已完成。
4. 全树卡住时的兜底：现有 `blocked` 状态 + `human_review` parent_action 通路继续负责把它抬到人面前。

## 9. 全局看板（承接并扩展现有设计）

`get_tree` 的 `stats.obligations`：

| 字段 | 用途 |
|---|---|
| `blocking_total` / `advisory_total` | 一眼看出树有没有被卡 |
| `by_type` | `{type: count}`——区分"卡在冲突"还是"卡在版本" |
| `oldest_blocking_age_seconds` | **区分"在干活"与"卡死"** |
| `by_depth` | 漂移/债务集中在第几层（MasDrift 深度累积） |
| `revalidate_queue_depth` | 宪法再验证队列长度 |
| `settled_by_human_count` | 有多少债是被人手动清掉的——**这是"放宽"的可见计量** |

### 9.1 跨树聚合（派生索引的读面，2026-09-23）

树级看板回答"**这棵**树卡在哪"，回答不了"**所有**树的 `constraint_coverage_gap` 还剩几条"——
那种问题在 `node_meta` 的 JSON 里查不动。于是有 `obligation_index` 表（§13 Q3），读面只此一处：

- `GET /api/obligation-index?status=open|settled|all`（`api/app/obligation_index.board()`）：
  **聚合 + 同一时刻的漂移状态**一起给。范围必须显式；问错范围不会被静默当成默认范围（`422`）。
- 看板面板 `ObligationIndexPanel`（`web/src/App.tsx`），全库口径，与当前选中哪棵树无关。

**为什么"聚合"和"漂移"必须一起给**：这张表是缓存，"`total == 0`"在
`built_at is None`（从没重建过）或 `in_sync is False`（表已跟真相漂了）时**不构成读数**。
只给一个数会让人把 0 读成"没有债"——方向正好反了。所以界面在这种状态下必须明说
"这里的 0 不代表没有债"，并把重建命令一起给出来。

**失效方向**：路由**只读**，**不自动重建**（自动重建会把"为什么漂了"一起抹掉，下一次照样漂）；
重建仍只由 `scripts/rebuild_obligation_index.py --rebuild` 负责（索引的唯一写入口）。

证据：`api/tests/test_obligation_index.py`（`board()` 配对漂移 / 范围显式且拒未知值）+
`api/tests/test_api.py::test_obligation_index_endpoint_reports_drift_instead_of_a_zero` +
`web/src/model.test.ts`（未建/过期不许读成"没有债"、未知类型回退原名）。

### 9.2 审计修正（2026-09-24）

对 §9.1 这一波做了对抗性审计（只读、逐文件过一遍实现），确认 **4 处产品缺陷**。共同点与
2026-09-21 那轮一样：不是逻辑写错，而是**判据的失效方向没定**或**只看过一种输入**。

| # | 改了什么 | 为什么（失效方向） | 边界 / 已知限制 |
|---|---|---|---|
| ① | `board()` 的 `built_at` 从"过滤后子集的 max"改成**全表** max；新增 `indexed`（表里几条）与 `projected`（真相里几条） | 旧口径下 `total == 0` **恒等于** `built_at is None`：索引刚重建过、只是这个范围里确实没有债，也会被读成"索引未建、读不出来"——"确实清干净了"这个状态根本表达不出来（界面那句"这个范围里没有债务"是死分支）。现在可读性判据是：`in_sync` 且（`built_at` 有值 **或** `projected == 0`） | `cross_tree_by_type` 的 `built_at` 仍是子集口径（它按范围回答"这批行是哪会儿建的"），`board()` 刻意不用它——两处口径不同，已在 docstring 里写明 |
| ② | `source_updated_at` 移出漂移判据（`DRIFT_IGNORED_FIELDS`） | 它是"这行按哪一刻的节点算出来的"，不是债务内容；`orchestrator` 里有 17 处 `task.updated_at = utcnow()`（每次状态流转都动）→ 活跃树上 `in_sync` 近乎**恒假**，真漂移与"节点只是被动过"都显示成"变 N"，读的人分不开，最后两种一起被忽略 | 会不会漏掉真漂移？不会：债务内容变了必然改动 `kind` / `severity` / `status` / `recurrence_count` / `evidence_version` / `node_id` / `depth` / `root_id` 之一，那些仍逐列比。有反面自检（降级 severity 必须报 `in_sync=false`） |
| ③ | CLI `--status` 校验范围，非法值 `SystemExit(2)` | 旧行为直接进 SQL（`status == 'opne'`）→ 0 行 → 打印 `total: 0`，与"真的没有债"长得一模一样。HTTP 那侧是 `422`，运维最常走的 CLI 这侧不能更松 | 空串仍是"全都要"（沿用既有约定）；反面自检钉住"合法范围照常放行" |
| ④ | 脚本默认库改为 `workspace/battlefield.sqlite3`（原为 `codex_battlefield.sqlite3`） | 与 `app.config` 的默认**不是同一个库** → 不带环境变量跑 `--check` 会去问一个不存在的空库，输出 `projected: 0 / in_sync: true`，读起来正是"索引干净、没有债"：**索引唯一的写入口自己报了个假全绿** | 回归测试比的是两边**算出来的 URL**（`test_repo_gates.py::test_rebuild_index_script_defaults_to_the_product_database`），不是源码文本，改名也盯得住 |

④ 是单测抓不到的那一类（测试全都显式指定库），只有真跑一次才暴露——端到端读数见
`VERIFICATION_STATUS.md` 的「2026-09-24」一节（真实库副本 + 真 `uvicorn`，含"清空索引表后
`total: 0` 必须读成'索引未建'"这条最贵的方向）。

**已核实但不是缺陷**（否则"没找到"会被读成"没问题"）：`board()` 自己的 `ValueError` 与路由的
`Literal` 校验**不是**重复死码（前者覆盖进程内直接调用）；`check()` 与 `rebuild()` 确实共用
`project()`（判据不会各漂各的）；`_root_map` 对脏数据成环有 `seen` 防护；路由不自动重建这一点
在实现里确实成立。

## 10. 验收契约（O 系列，可反驳）

| 编号 | 判据 | 如何反驳偷工 |
|---|---|---|
| O1 | 判据替换生效：子节点全 `done` 但存在 `path_collision` 时，父节点**不得** done | 造冲突 + 全 DONE 子节点，断言父节点未 done |
| O2 | `advisory` 债务不阻断，`blocking` 阻断 | 两类各造一个，断言结果不同 |
| O3 | 幂等：同一冲突重复集成不产生重复债务 | 连续两次集成，断言债务数量不变 |
| O4 | 清债留痕：`human` 清债缺 `reason` 必须被拒 | 断言抛错 |
| O5 | 版本单调：子节点版本不早于父节点 | 造"父已升级、子仍旧版"的场景，断言被纠正 |
| O6 | 约束缺口：子节点集合不覆盖父约束时产生 `constraint_coverage_gap` | 造一个丢掉 must 约束的子任务，断言产生债务 |
| O7 | 既有机制不退化：`_superseded_by_done_repair` 的既有用例仍通过 | 既有测试必须继续绿，不删不改 |
| O8 | 看板恒等式：`by_type` 求和等于 `blocking_total + advisory_total` | 断言恒等式 |

## 11. 分步实施（v1 范围见 §12）

| 步 | 内容 | 验收 |
|---|---|---|
| **O-A1** | 债务表 schema + `obligations.py` 纯模块（生成/清债/幂等/severity 规则） | O2、O3、O4 的模块级用例 |
| **O-A2** | 把 R4 的 `conflicts` / `skipped` 接入债务表（R4b） | **O1** |
| **O-A3** | `_accept_parent` 判据替换为"未清 blocking 债务为空"；`_superseded_by_done_repair` 改为 `repair_child` 清债通路 | O1、O7 |
| **O-A4** | 版本下沉/上浮 + `constitution_stale` / `revalidate_required` | O5 |
| **O-A5** | 约束覆盖核对 + `constraint_coverage_gap` | O6 |
| **O-A6** | 看板 `stats.obligations` + 前端条带 | O8、前端 lint/test/build |

### 11.1 实施状态（按事实写，不写"基本完成"）

| 步 | 状态 | 事实 |
|---|---|---|
| O-A1 | **已完成** | `api/app/obligations.py` + 纯模块用例（提交 `71c3151`）。第一版接线已回退（原因见 §6.4），第二版按两条硬约束重做完成（提交 `099b035`，用例在 `tests/test_obligation_gate.py`） |
| O-A4 | **已完成** | `constitution_version` 落到 `TaskNode` / `RunRecord`（增量迁移，`test_incremental_migration_adds_version_columns_to_a_legacy_database` 覆盖旧库补列）；单调继承在 `_insert_child_nodes`；判读在 `api/app/constitution_versions.py` + 22 个用例。判读已有真实调用方：`amend_constitution`（A3）做分状态迁移 |
| O-A2 | **已完成** | R4 的 `conflicts` / `skipped` 在集成收尾处转成父节点的轮次域债务（用**完整**明细，不用被 `detail_limit` 截断过的 meta——否则第 21 条之后的冲突会静默不进债务表）。见 `_record_parent_integration_obligations` |
| O-A3 | **已完成** | 判据落在 `_accept_parent` 的"验收通过之后、标 done 之前"，且是 §6.1 判据 2 的**无条件形式**（只看 `severity`，不按类型/开关过滤——第二版曾按档位过滤，与看板说的不一致，已修）。两条硬约束都有机器判据：**不创建子任务**、**有界**（`round_clear` / `version_catchup` 两种"按事实清债" + 连续拦下 `obligation_gate_max_refusals` 次升级人工阻塞） |
| O-A5 | **已完成** | `api/app/constraints.py`（纯模块）+ 创建时核对接线 + planner prompt 的逐字继承要求 + 13 例用例。严重度默认 advisory，可配置升为 blocking（默认阻断实测 14 条既有契约变红） |
| O-A6 | **已完成** | `get_tree` 的 `stats.obligations`（七字段 + 树级合并取"最老"）+ 前端条带；`oldest_blocking_age_seconds` 判不出时返回 `None`，不用 0 冒充"刚记上" |
| O-A6+ 跨树读面 | **已完成**（2026-09-23；2026-09-24 审计修正 4 处，见 §9.2） | 债务派生索引（`obligation_index` 表，§13 Q3）的**唯一读面**：`GET /api/obligation-index`（`obligation_index.board()` 把聚合与漂移状态一起给）+ 看板面板 `ObligationIndexPanel`。**只读、不自动重建**；未建/漂移时界面明说"0 不代表没有债"（§9.1）。此前只做了表 + 脚本 + 测试，**没有任何读它的地方**——那段历史如实记在这里 |
| §7.2.3 上浮 | **已完成** | 见 §7.2.3 实现状态：一支一条事实、逐层长出、按事实清债；`by_depth` 因此能回答"卡在第几层" |

## 12. v1 范围选项（待定）

| 选项 | 范围 | 特点 |
|---|---|---|
| **窄** | O-A1 + O-A2 + O-A3 | 只统一抽象 + 把 R4 冲突接进门控。改动集中在债务表与 `_accept_parent`，不碰 planner 产出 |
| **中** | 窄 + O-A4 | 再加版本维度（即原宪法 SPEC 的 A2/A5），上下文新鲜度落地 |
| **宽** | 中 + O-A5 + O-A6 | 再加约束覆盖核对（MasDrift 对策）与看板。能力最完整，改动触及 planner 产出的约束继承 |

## 13. 未决

| 编号 | 决策 | 说明 |
|---|---|---|
| Q1 | v1 取窄/中/宽 | 见 §12 |
| Q2 | `integration_skipped` 里 `absolute path` 是否算 advisory | 我建议 advisory：它是子节点写法问题，不必然丢产物 |
| Q3 | 债务是否需要在 SQLite 建索引 | **已决（2026-09-22）：建，但只作为"派生索引"**。新表 `obligation_index`（`api/app/models.py`）+ 投影/重建/漂移检查（`api/app/obligation_index.py`）+ 脚本 `scripts/rebuild_obligation_index.py --rebuild/--check/--by-type`。边界写死在三处：①真相仍是 `node_meta["obligations"]`（门禁读它），本表只是摊平；②`rebuild()` 与 `check()` **共用同一个投影函数**，否则漂移判据自己会漂；③消失的债必须删行、漂移必须报出来（自动重建会抹掉"为什么漂了"）。证据 `api/tests/test_obligation_index.py` 4 例（含"漂移只报不改"与"消失的债删行"）。<br>**读面（2026-09-23 补）**：全库聚合的唯一接口是 `GET /api/obligation-index`（`status` 取 `open` / `settled` / `all`；`obligation_index.board()` 把聚合与同一时刻的漂移状态一起给；范围显式、非法值 `422`），看板面板 `ObligationIndexPanel`；**只读、不自动重建**，未建（`built_at=null`）或漂移（`in_sync=false`）时界面必须明说"0 不代表没有债"（§9.1）。证据：`api/tests/test_obligation_index.py` +2 例、`api/tests/test_api.py` 路由例、`web/src/model.test.ts` / `api.test.ts` |
| Q4 | `constraint_coverage_gap` 的核对是"创建时拦"还是"验收时拦" | **已决（2026-09-22）：保持"创建时核对"，但先修掉它的结构性误报**。取证：真实档 `fg5` 里 7 个节点各记 3 条**同样**未清的债，内容全是**编排器自执的不变量**（"任务树必须保留总目标的品质下限""父节点必须通过整合验收后才能 done""真实运行记录必须保存 prompt、结果、证据和失败原因"）——它们原本写在 `global_acceptance` 里，而核对要求子节点**逐字覆盖**，没有任何子任务能交付这种东西。<br>修法（结构性，不是文本启发式）：把不变量改成**工具侧常量**（`MockCodexRunner.ORCHESTRATION_INVARIANTS`，渲染进宪法 markdown 让节点仍看得见），**不再进 `global_acceptance`**；`prompts/constitution.md` 写明判断方法："子任务不做就算它没达标"→ `global_acceptance`；"不管子任务做什么系统都必须这么运转"→ 由编排器自执，不进委托链。（**不做成 schema 字段**：门禁规则是"声明即必填"，可选字段会把那条规则蛀空。）`delegable_constraints` 不变，仍只读 `global_acceptance`。<br>**修后实测**（`p4b`，同任务同参数）：`1 个节点 1 条`，且是真缺口。<br>边界：真模型若仍把不变量写进 `global_acceptance`，假债会回来——判断方法已写进 prompt；**不做**文本启发式过滤（会误伤真约束）。另注：这类债是 `advisory`，不直接挡 `done`（早先"它挡着收敛"的说法不准确，已更正） |
| Q5 | 只读（不许新增/修改任何文件）约束下，要不要豁免"递归经验归拢 Skill"子任务 | **已决（2026-09-22）：豁免**。它的验收要求"必须生成 `docs/skill_notes/<TASK_ID>.json`"，与"不许新增任何文件"直接冲突——真实档里 planner 只能报"节点约束内部矛盾"。实现：`skill_notes.readonly_constraint_reason`（**成对**匹配"禁止动作 + 整体范围词"，`read-only task` 这类整体说法单独认）+ 两处创建点（`with_parent_skill_note_child_ex` / `_backfill_parent_skill_note_child`）都会跳过并落 `parent.skill_note_readonly_exempted` 事件（含命中的原文）。失效方向：宁可漏豁免也不误伤（假阳性会让正常工程丢产物）。证据 `api/tests/test_skill_notes.py` 两例（正例 + **窄约束不许豁免**的反面自检）+ `api/tests/test_orchestrator.py::test_readonly_parent_does_not_get_skill_note_child_and_records_why` |
