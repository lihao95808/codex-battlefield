# 宪法变更与存量节点传播 SPEC

状态：设计已定（用户选定 **方案 B：按状态分治迁移 + 再验证队列**），待实施
续跑锚点：`PLAN-STATUS.md`
相关：`docs/spec-modularization.md` §7（反缩水验收契约）、`FINAL_GOAL.md`

> **范围变更（2026-09-14）**：本文件的 **§5 版本戳** 与 **`revalidate_required` 队列** 两部分，
> 已由 `docs/spec-parent-child-obligations.md` 的**债务表（obligation）**统一吸收——因为
> "宪法版本落后"与"集成冲突"是同一类问题（入站事项未清），不应各做一半。
>
> 保留在本文件的独立内容：**§6 按状态分治裁决表**（6 类节点各自的迁移动作）、**§7 方向敏感规则**
> （tighten 必触发再验证 / loosen 不得追溯洗白）、**§7 权限边界**（人声明方向、root 只能建议）。
> 这三部分与债务无关，仍是本文件的范围。
>
> 实施顺序：先做债务表（O-A1..O-A3），再把本文件的裁决表接上去作为**债务的产生规则**。

## 1. 问题

任务树跑起来之后，人在对话里与最顶层的 Codex 讨论，产出了**对宪法的修改**。
需要回答三件事：

1. 改动如何**正确**传导到所有已存在的子节点？
2. 哪些节点**不能**改、哪些**必须**重做？
3. 是否需要全局看板？

## 2. 现状事实（全部实测，带行号）

| 事实 | 位置 | 含义 |
|---|---|---|
| `update_constitution` 在 `status == "locked"` 时抛错 | `orchestrator.py:389-390` | **唯一的修改入口**。树一旦启动，宪法永久冻结 → 该能力**今天不存在** |
| `start_project` 要求宪法已锁定 | `:8688-8689` | 先锁后跑，路径单向 |
| `_resolve_locked_constitution` 取「全库最近更新的 locked 记录」 | `:8681-8685` | **按 root 无隔离**：多项目并存时会选错宪法 |
| `get_constitution(db, None)` 同样是全库最近一条 | `:8662` | 同上 |
| `ConstitutionRecord.version` 已存在，每次 update +1 | `:393` | 版本号已经在了，**没有任何人消费它** |
| 节点持有宪法的**快照副本** | 根 `:447-449`；子节点自父拷贝 `:6715-6717` | 改根记录**碰不到任何已存在节点** |
| `TaskNode` 只存 `constitution_hash` / `constitution_json`，**不存 version** | `models.py` TaskNode | 无法判定"落后几版" |
| 4 类 prompt 各自嵌入本节点快照 | `runner.py:2596-2597`(planner)、`:2656-2657`(worker)、`:2710-2711`(verifier)、`:2786-2787`(acceptance) | 版本错配会产出"半旧半新"的产物 |
| `constitution_verdict` 字段存在但**未与版本绑定落库** | verifier schema / run result | 事后无法回答"这份产物按哪版验收" |
| `stale` / `revalidate` / `amendment` 机制 | 全库 grep **零命中** | 从未实现 |
| 对话通道 | 路由表 33 个端点，**无 chat** | "跟最顶层通话"亦需新通道（本 spec 不含，另行设计） |

## 3. 目标 / 非目标

**目标**
1. 宪法可**在树运行中修订**，且修订有版本、有方向、有影响范围、可追溯、可续跑。
2. 存量节点按状态分治迁移，**不产生半旧半新的产物**。
3. 收紧时**必须**触发再验证；放宽时**不得**追溯洗白已失败/阻塞节点。
4. 提供全局看板，使"传播是否完成"可被回答，而不是靠自述。

**非目标**
- 不做对话界面本身（只做它落地后的传播语义）。
- 不做宪法的自动改写（root agent 只能建议，见 §7）。
- 不改递归任务树的铁律（宪法包络线、300% 叶子、验证失败转父节点）。

## 4. 前置缺陷（必须先修，否则传播无处落地）

| 编号 | 缺陷 | 修法 |
|---|---|---|
| P1 | `_resolve_locked_constitution` 跨 root 无隔离 | 先按 `root_id` 过滤；`root_id` 为空时**必须显式报错**而不是猜最近一条 |
| P2 | `get_constitution(db, None)` 跨 root 无隔离 | 同样按 `root_id` 过滤 |
| P3 | 节点不存宪法版本号 | `TaskNode` 增 `constitution_version`（见 §5） |

P1/P2 不修，则"当前生效版本"本身指不准，后面所有判定都建在沙子上。

## 5. 数据模型变更

**`ConstitutionRecord`（已有字段全部保留）**

| 列 | 说明 |
|---|---|
| `version`（已有） | 单调递增 |
| `root_id`（已有） | 本版宪法所属的树 |
| `supersedes_id`（新增） | 上一版 id，构成链 |
| `amendment_direction`（新增） | `tighten` / `loosen` / `mixed` / `neutral`——由**显式声明**决定，不由代码推断 |
| `effective_at`（新增） | 本版开始生效的时刻（amend 事务提交时刻） |

**`TaskNode`（新增列，走 `database.py` 的增量迁移机制）**

| 列 | 说明 |
|---|---|
| `constitution_version` | 建立该节点时使用的版本号 |

**`RunRecord`（新增列）**

| 列 | 说明 |
|---|---|
| `constitution_version` | 该次运行实际使用的版本号 |

**迁移裁决需要一个可查询的派生视图**（不新增表，由 `get_tree` 计算）：
`stale = node.constitution_version < root.effective_version`。

## 6. 迁移裁决表（本 spec 的核心）

**「所有子节点」不是一个整数，是 6 类，不能一个循环刷下去。**

| 节点状态 | 裁决 | 理由 |
|---|---|---|
| `planned` / `queued` / `repair` | 换快照 → 按新宪法**重规划** | 尚无产物，改动代价为零 |
| `waiting_children` | 只换自身快照；子节点各自按本表裁决 | 避免重复遍历与重复迁移 |
| `running` / `working` / `verifying` / `accepting` | **不动**，让旧版跑完；跑完后**按旧版验收**，随后进 `revalidate_required`（仅当方向为 tighten） | 中途换版 → 产物半旧半新，且 `constitution_verdict` 与实际不符 |
| `done` | **不自动重做**；`tighten` → 进 `revalidate_required`；`loosen`/`neutral` → 只更新版本戳 | 产物是既成事实；是否重做须由父节点用新宪法裁决 |
| `blocked` / `failed` | 按新宪法**重新分类**并重试 | 旧版下的分类可能已失效 |
| `hibernating` | 恢复时用新版 | 自然衔接 |

**`revalidate_required` 是一个显式队列**，不是标记位：父节点必须用新宪法对入队子节点重新跑一次真实验收，
产出新的 `constitution_verdict`；**未验收完不得把父节点标记为 done**。

## 7. 方向敏感规则（最易违反 §7.3 禁令之处）

| 方向 | 对存量节点的效果 |
|---|---|
| **tighten**（抬 floor / 压 ceiling） | 已完成产物可能不再合规 → **必须**触发再验证。不触发即等于静默 claim 通过 |
| **loosen**（降 floor / 扩 ceiling） | 已完成产物仍合规，**不必**重做；**但绝不允许拿新宪法追溯"洗白"已 `blocked`/`failed` 的节点** —— 那正是 `spec-modularization.md` §7.3 禁止的"放宽断言来变绿"。**放宽只对未来节点生效** |
| `mixed` | 按方向逐条拆分；无法拆分的条目按 **tighten** 处理（保守优先） |

**权限边界**：`amendment_direction` 由**人**在发起时显式声明，`root` codex **不能**自己改宪法，
只能产出**修订建议**（`constitution.suggested_amendment` 事件）。否则等于产出方自己放水——
这是 `spec-modularization.md` §7.1 立约的第一条结构性成因。

## 8. 原子性与可追溯

一次 amend 必须是**一个事务**，任一步失败整体回滚：

1. 校验：`direction` 已声明、发起者是人、`root_id` 明确（P1 已修）。
2. 落库新版本，`supersedes_id` 指向旧版，`effective_at = now()`。
3. 写事件 `constitution.amended`，payload 含 `from_version` / `to_version` / `direction` / `affected_by_status`（各状态计数）/ `revalidate_required_ids`。
4. 按 §6 裁决表批量更新可立即迁移的节点（快照 + version）。
5. 建 `revalidate_required` 队列。
6. 执行中节点**只登记不打断**；它们跑完后由 `_process_node_unlocked` 的收尾路径入队。

**可追溯**：`RunRecord.constitution_version` 与 `constitution_verdict` 必须落库且带版本，
使"这份产物按哪版验收"可回答。

**续跑**：amend 事务中断时，`constitution.amended` 事件与节点 version 戳一致才视为完成；
不一致则重放（幂等：以 `(root_id, to_version)` 为幂等键）。

## 9. 全局看板（回答"传播完成了吗"的唯一手段）

判定为**判据而非装饰**。`get_tree` 的 `stats` 增 `constitution` 子块：

| 字段 | 用途 |
|---|---|
| `effective_version` / `effective_hash` | 当前生效版本 |
| `version_distribution` | `{version: node_count}`——**一眼看出几个版本并存** |
| `stale_total` | 落后节点总数 |
| `stale_by_action` | 分桶：`migrate_now` / `wait_running` / `revalidate` / `stamp_only` |
| `running_on_old_version` | **必须等**的那部分（执行中且落后） |
| `revalidate_queue_depth` | 再验证队列长度 |
| `last_amendment` | 最近一次 amend 的 `direction` / `affected` / `at` |

没有这些，"传播完成"只能靠自述 —— 这正是 §7.1 指认的结构性成因。

## 10. 验收契约（可测、可反驳）

| 编号 | 判据 | 如何反驳偷工 |
|---|---|---|
| K1 | 状态分治正确：6 类状态各自命中 §6 预期裁决 | 逐状态建节点 + 断言裁决结果，不允许只测 happy path |
| K2 | **不产生半旧半新**：`running` 节点在 amend 前后 `constitution_hash` 不变，其 run 记录的 version 一致 | 断言 amend 期间执行中节点的 hash/version 像素级不变 |
| K3 | **tighten 必触发再验证**：`done` 节点入 `revalidate_required`，且父节点在队列清空前不得 done | 断言队列非空时父节点状态不是 `done` |
| K4 | **loosen 不洗白**：`blocked`/`failed` 节点在 loosen 后仍为 `blocked`/`failed` | 断言状态不变；这是反对"放宽断言变绿"的硬测试 |
| K5 | 跨 root 隔离：两个 root 共存时，各自解析到自己的宪法版本 | 建两个 root + 两版宪法，断言互不串味（P1/P2 的回归测试） |
| K6 | 原子性：注入中途失败后，无"事件已写但版本戳未更新"的中间态 | 故障注入测试 |
| K7 | 看板字段齐备且数值自洽 | 断言 `sum(version_distribution.values()) == 节点总数` 等恒等式 |
| K8 | 既有契约不退化 | pytest 全绿，passed 数不下降 |

## 11. 分步实施（每步可独立验收、独立提交）

| 步 | 内容 | 验收 |
|---|---|---|
| **A1** | 修 P1/P2（root 隔离），补回归测试 | K5；pytest 全绿 |
| **A2** | 加 `constitution_version`（TaskNode / RunRecord）+ 增量迁移 + 写入点 | 新建节点带 version；迁移项在旧库上可安全落地 |
| **A3** | `amend_constitution(...)` 事务 + 事件 + 幂等键 | K6；单元测试覆盖回滚 |
| **A4** | 按 §6 裁决表实现迁移，分状态测试 | K1、K2 |
| **A5** | `revalidate_required` 队列 + 父节点门控 | K3 |
| **A6** | 方向敏感规则 | K4 |
| **A7** | 看板 `stats.constitution` | K7 |
| **A8** | 前端：宪法版本条 + 落后节点条带 | 前端 lint/test/build 绿 |

### 11.1 实施状态（按事实写）

| 步 | 状态 | 事实 |
|---|---|---|
| A1 | **已完成** | P1 `_resolve_locked_constitution` 去歧义（`92d942c`）；**P2 已完成**：`get_constitution(db, None)` 不再取"全库最近一条"，改成"要么显式给 id/root_id，要么不歧义"，多棵树并存时**显式 409 不猜**。K5 用例在 `tests/test_constitution_isolation.py`（5 例）。前端 `fetchLatestConstitution(rootId)` 带上 root，否则界面会因读错那一份把树误判成"宪法陈旧" |
| A2 | **已完成** | 两列 + 增量迁移 + 三处写入点（根、子节点、两处 run 记录；快照恢复也带上）。O-A4 一并做了 A5 的**产出**半边与 §6/§7 的判读函数 |
| A3 | **已完成** | `Orchestrator.amend_constitution`（一个事务：新增版本行 + `supersedes_id` / `amendment_direction` / `effective_at` + `constitution.amended` 事件 + 按 §6 判读分状态迁移 + 再验证登记）+ `POST /api/projects/{root_id}/constitution/amend`。13 个事务用例（含中途失败整体回滚、幂等、方向敏感）。**幂等键改为 `(root_id, content_hash)`**：头版本会移动，"下一版号"不能当键 |
| A4 | **已完成** | §6 判读表以机器可读形式落在 `api/app/constitution_versions.py`（7 个动作桶、覆盖全部 16 个状态、未知状态保守处理），A3 按它分状态迁移。`blocked`/`failed` 的"重新分类并重试"**已接线**：收紧方向把节点换快照 + 清 `error_message` + 重新排队（`task.reclassified_by_amendment`），起跑由路由在事务提交后 kick；**放宽/中性方向只换快照、状态不动**（K4：放宽不得追溯洗白已失败/阻塞的节点） |
| A5 | **已完成（范围见下）** | 再验证真的跑起来了：`_revalidate_stale_leaves` 在**父节点自己验收之前**对"落后且可再验证"的 `done` 叶子重跑 verifier；通过即由 `_stamp_constitution_version_on_completion` 追平版本戳，债务由 `version_catchup` 清掉（可判、有界）；不通过走既有修复通路。版本债纳入同一门控（`constitution_revalidate_enabled`，默认开）。**范围**：只覆盖"留有成功 worker 产物、且无子节点"的叶子；有子节点的已完成分支仍只产生 blocking 债务并走升级通路 |
| A6 | 已完成 | 方向敏感规则在 `constitution_versions.forces_revalidation` + `obligations.stale_severity` + A3 的分状态裁决里；放宽不排再验证队列 |
| A7 | **已完成** | `get_tree` 的 `stats.constitution` 七个字段齐备：`effective_version` / `effective_hash` / `node_total` / `version_distribution`（含 `unknown` 桶）/ `stale_total` / `stale_by_action`（七个桶全给）/ `running_on_old_version` / `revalidate_queue_depth` / `last_amendment`。K7 的三条恒等式有用例钉住（`tests/test_constitution_versions.py`）。数字来自三处不同事实源（节点 / 债务表 / 事件流），刻意不合并 |
| A8 | **已完成** | 前端宪法面板加"传播"一栏：`constitutionPropagationHeadline` 把"落后者"与"执行中必须等"**分开说**（混成一句会往两个相反方向误判）。`effective_version` 为 `null` 时显示"未绑定宪法"，不显示 v0 |

**A5 里踩到并修掉的一件事**（写下来免得下一个人再踩）：
再验证一开始看着"跑一遍 verifier 就行"，其实不行——verifier 的 prompt 里嵌的是**本节点的
`constitution_json`**，而收紧修订刻意没给 `done` 节点换快照，所以直接重跑等于拿**旧判据**复查，
白跑一次。正确顺序是**先换快照、再验证、通过才盖版本戳**：换快照后若 verdict 不通过，节点会带着
新快照进入修复通路，而版本戳仍停在旧值——"产物是在旧版下通过的"这个事实没有被抹掉。
`test_revalidation_runs_the_verifier_against_the_new_constitution` 钉住这一点。

另一件：`_try_mark_for_agent` 会拒绝终态节点（`done` 是终态），所以再验证一开始一步都走不出去。
新增 `_try_mark_for_revalidation`，只放开"允许从 `done` 出来"这一条。

执行中节点跑完后的入队（`revalidate_after_run`）**已接线**：修订当场只登记；节点**完成**时
`_enqueue_revalidate_after_run` 把登记兑现——收紧方向写 `constitution_stale` + `revalidate_required`
到它的父节点上（于是既有的 `_revalidate_stale_leaves` 会在父节点验收前重跑它，通过即追平清债），
放宽方向只把版本戳追平；若登记期间它自己已经追平，就按事实清掉之前记下的落后债。

**踩到的一处（第三次同类型）**：再验证原先要求"节点还留着一个**没验过**的 worker 产物"
（`_pending_worker_result`）。而"跑完入队"的节点恰恰**已经验过一次、有 verifier 结论**——
用那个判据一条都匹配不上：登记写了、队列空了，接线看着在、实际是死的。新增
`_revalidation_worker_result`（只要 worker 产物成功，不要求没有 verifier 结论）。

## 12. 未决

| 编号 | 决策 | 说明 |
|---|---|---|
| Q1 | tighten 时 `done` 节点的再验证是否默认开启 | 关＝改动便宜但可能静默放过；开＝诚实但成本真实。**建议开**，并允许人在 amend 时对**具体**节点显式豁免（豁免必须留痕） |
| Q2 | 是否允许"全树重规划"作为 amend 的强度选项（方案 C） | 大幅改动时省心，代价是废弃进行中工作。建议作为显式选项，不作默认 |
| Q3 | 对话通道（人在对话框与 root codex 通话）的形态 | 本 spec 不含，另行设计 |
