# 管家（steward）SPEC

状态：第一版已落地（只读）+ 动作审批回路 + 主动提醒
前置：`docs/spec-state-project-decoupling.md`（状态与工程解耦）
相关：`docs/spec-resource-governance.md`（L4 监护者，与管家分工不同）

## 1. 它是什么

**管家 = 这个工具的常驻全局 agent。** 它了解工具的全局状态、对用户负责、
处理全局事务、和用户对话。

它**不是**树里的一个节点，也**不是** worker：

| | 树的节点（planner/worker/verifier/acceptance） | 管家（steward） |
|---|---|---|
| 是什么 | **任务**：被拆解、被验收 | **对话**：讨论树、不参与树 |
| 受什么约束 | 300% 铁律、验收契约、并发槽位 | 只受权限约束 |
| 生命周期 | 随树开始与结束 | 常驻（跨多棵树） |
| 输出 | 结构化决策（schema） | 对用户说的话 + 建议 |

**为什么不把它做成树里的节点**（这条路很诱人，但会坏掉两件事）：

1. **"树"这个概念会失真**：凭空多一个不受验收约束、不被拆解的节点。
   树的语义是"待办事项的分解结构"，管家不是待办事项。
2. **它会跟 worker 抢并发槽位**：用户跟管家聊天时，如果树正满载，
   管家就得排队 —— 而"了解状态"这件事不该被状态本身阻塞。

## 2. 与 L4 监护者的分工

两者**共存、不互替**：

| | L4 监护者（已有） | 管家（本 spec） |
|---|---|---|
| 性质 | 自动、按规则 | 对话、按判断 |
| 管什么 | 资源压力、额度、续跑/休眠 | 全局事务、对用户负责 |
| 触发 | 定时 / 事件 | 用户说话 |
| 模型 | 无（纯规则） | 有（LLM） |

一句话：**L4 处理"机器撑不撑得住"，管家处理"这事该怎么办"。**

## 3. 权限（第一版：只读）

用户选定：**只读**——能看、能说、能建议，不能动树。

理由是失效方向：

* 管家跑在与状态**同权限**的位置（它能读写状态根）。"能改"就等于"能改坏状态"。
* 而它是个 LLM —— 判断会有错的时候。先让它证明判断可靠，再逐级放开。

能力目录（`app/steward_capabilities.py`）里**改状态的能力照列**，但只读模式下
标成 `available: false`，并说明需要什么条件才能执行。**"存在但被禁用"比
"假装不存在"更有用**：agent 才能告诉用户"要做这个得先放开写权限"。

> 实测（2026-10-15，真实模型）：只读模式下它回复
> "当前只读模式下无法执行，需要你先放开写权限" —— 权限声明被 agent 正确读懂了。

## 4. 它怎么看状态

新增 `GET /api/steward/status`：**一次性**给它判断局势所需的全部事实。

为什么不让它自己探索：实测过 API 之外的 shell 里 codex 受限令牌建不起来
（`CreateProcessAsUserW failed: 5`），自己探索**不可靠**。喂结构化快照既可靠，
又天然把视野限定在我们愿意给的范围里（权限设计上的好处）。

快照内容：工程/状态根/库、树的计数、**被阻塞节点及原因**、用量、最近事件、
**树的紧凑轮廓**。

两条硬规矩：

1. **取不到就如实标注**（`available: false`）。填 0 会被读成"没花钱"，
   而用户会基于它做决定 —— 假数据比"不知道"坏得多。
2. **截断要说出来**（`outline_truncated` / `outline_omitted`）。
   不说的话 agent 会以为看到的就是全部，做出"树已经完整"的错误结论。

## 5. 对话怎么跑

```
用户消息
  -> 取状态快照（只读，每轮重建）      steward_status.build_status
  -> 取能力目录（含权限声明）          steward_capabilities.catalog
  -> 取最近 HISTORY_WINDOW 轮对话       steward.recent_messages
  -> 交给 runner（codex exec）          runner.steward_chat
  -> 校验 payload                       steward.parse_chat
  -> 落库（用户 + 管家两条 + metadata）  steward.append_message
```

四个刻意的选择：

| 选择 | 理由 |
|---|---|
| **每轮重建快照** | 缓存会让它拿旧状态回答新问题 —— 而这种错误极难发现，它的回答看起来永远合理 |
| **历史存我们库里** | 可审计：谁说了什么、当时看到的是哪份快照都落盘。靠 codex session 续接等于把上下文放在别人状态里 |
| **契约不符仍回话** | 一次 schema 不符不该让对话崩成 500。退化成"我解析不了，已记录" |
| **失败也落库** | 否则用户看到"我说了但记录里没有"，比报错更让人不信任 |

模型档：**默认跟 acceptance 同级**（`model_for("steward")` 的回退链是
自己 → acceptance → 全局）。可用 `CODEX_BATTLEFIELD_CODEX_MODEL_STEWARD` 单独配。

## 6. 动作审批回路（从"只能建议"到"你批准后能执行"）

第 3 节说第一版只读。这一节是只读之上加的那一层：**提议可以，执行要你点。**
它不是把只读放开了 —— 它把"能不能执行"变成了**每一次都要你亲自决定**。

```
管家提议  ->  用户批准  ->  服务端执行
   |             |             |
  入库 pending  入库 decided   入库 executed / failed
```

**管家永远不能自己执行。** 就算哪天真把它的权限放开，那也是"它能自己批准"，
而不是"绕过了审批"—— 这两件事在代码里是分开的（`propose` 与 `run_approved`）。

### 6.1 两道校验，缺一不可

| 时机 | 查什么 | 为什么不能只查一次 |
|---|---|---|
| **提议时** | 动作名在白名单里？参数齐？ | 拦掉模型幻觉出的动作名，别在界面上摆一个"点了会失败"的按钮 |
| **执行时** | **再查一次** | 提议与批准之间可能过了很久（用户第二天才点），而执行是最危险的时刻 —— 它必须自己判，不能只信"当时验过" |

非法建议**不入库**，而是把原因**回给用户**，并且**列出可用动作**：
只说"这个动作不合法"用户没法纠正，说了可选什么才能纠正。

`ActionSpec.params` 与 `rationale` **复用 `steward_capabilities` 的同一份声明**，
不另写一份 —— 分成两处迟早漂移，而漂移的后果是"文档说不可用、实际能执行"。

### 6.2 "批准了但没做成"必须可见

库里 `decided_at` 与 `executed_at` **分开写**，并且执行失败会把
`status=failed` + `error`（含异常类型名）落库。理由只有一条：

> 用户点了"批准"，心理上就认为事情做完了。如果失败被静默掉，
> 他会带着"已经处理了"的判断继续往下走 —— 这比报错坏得多。

界面因此必须区分"已执行"和"执行失败"，并让后者比前者更显眼。

### 6.3 只读与可执行是两道独立的闸

| 闸 | 管什么 | 默认 |
|---|---|---|
| `frozen_mode` | 整机冻结（谁都别动） | 关 |
| `steward_read_only` | **管家**能不能代你执行 | **开** |

必须分开判：只看 `frozen_mode` 的话，默认配置下"只读的管家"批准完就能动树 ——
界面上写着"只读"、实际能执行，这是最坏的一种误导。

只读**不影响提议**：只读禁的是执行，不是建议。建议是管家最有价值的产出之一，
禁掉它等于把管家废了。所以只读模式下收件箱照样有人排队，只是按钮禁用并写明怎么打开。

### 6.4 一个实测教训：漏 `await` 会"报告成功但什么都没做"

`cancel_all`、`set_budget_guard`、`hibernate_tree` 在 orchestrator 上都是 **async**。
执行器里漏掉 `await` 时的表现是：

* 不抛异常；
* `run_approved` 记 `executed`（**成功**）；
* `f"{count}"` 打印出一个协程对象；
* 唯一痕迹是一条 `RuntimeWarning: coroutine ... was never awaited`，而 pytest
  默认只当 warning，**测试照样绿**。

三个执行器**原先都漏了**（2026-10-15 用异步替身抓出来）。所以验收用的替身
必须把方法写成 `async`：同步替身上漏 `await` 完全看不出来（`cancel_all()` 会
立刻返回一个整数，执行"成功"，测试绿）。

## 7. 主动推送（不等用户问）

`GET /api/steward/alerts/stream`：**SSE 流**。提醒变化时推，没变化就不说话。

### 7.1 为什么是 SSE 不是 WebSocket

这是**单向**的（服务端 → 界面）。SSE 自带断线重连，而且就是普通 HTTP ——
为了一个单向通知流引入双向协议是没必要的复杂度。

### 7.2 节流：为什么必须**边沿触发**

如果只是"有问题就推"，那么一个持续阻塞的节点会每轮都推一次，一分钟一次，
用户很快就学会无视提醒 —— 那时提醒就等于没有。所以三条规则：

| 规则 | 做法 | 防的是什么 |
|---|---|---|
| **边沿触发** | 只在「提醒集合或等级」**变化**时推 | 持续存在的状况反复弹 |
| **阈值** | 只推 `steward_push_min_level` 及以上（默认 `action`） | 参考信息把"要你动手"的那条淹掉 |
| **冷却** | 两次推送至少隔 `steward_push_throttle_seconds`（默认 30s） | 短时间内反复打扰 |

**指纹只取 `kind` 与 `level`，不取 message**（`alerts_digest`）：消息里带数字
（"3 个节点失败"→"4 个节点失败"）会让指纹每轮都变，节流就退化成轮询了。

每次推送带 `reason`（为什么现在推）。用户看到一条提醒时，最想知道的是
**"这跟我上次看到的有何不同"** —— 没有这句，"3 个节点失败"是新出现的
还是已经挂了两小时，无从判断。

### 7.3 拉取与推送共用同一份判定

`/api/steward/alerts`（拉取）与流（推送）都调 `steward_alerts.evaluate()`。
分成两份实现迟早漂移，而漂移的后果是"界面上看到的"和"推送来的"不是同一件事。

### 7.4 评估必须在事件循环之外

`evaluate()` 是**同步**的（SQLAlchemy 同步会话 + 文件 IO）。直接在事件循环里调
会阻塞整个 API —— 症状是"推送开着的时候其他请求变慢"，而这极难归因到推送。
所以流里用 `asyncio.to_thread` 调它。

配置项：`CODEX_BATTLEFIELD_STEWARD_STREAM_INTERVAL_SECONDS`（默认 5）、
`_PUSH_MIN_LEVEL`（默认 `action`）、`_PUSH_THROTTLE_SECONDS`（默认 30）。

## 9. 主动汇报（不等用户问就开口）

`GET /api/steward/alerts/stream` 推的是**原始信号**（轻，只是界面上多一行）。
本节讲的是**让管家解释一句**（贵：一次真实模型调用 + 一条对话消息）。

为什么要它：只会应答的东西不是"管家"，是个查询界面。真正有价值的是它在出事时
**先开口**。但主动开口有个硬约束 —— **不能打扰**，因为用户对噪声的反应是
关掉它，那时连原本有用的部分也一起失效。

所以触发条件是**重要边沿**，不是"有变化"：

| 触发 | 为什么算重要 |
|---|---|
| 出现 `action` 级提醒 | 唯一需要用户**动手**的一类 |
| 出现 `warning` 级提醒且上次没有 | 状态在变坏 |
| 重要提醒**全部清空** | 用户可能在等"好了没"，这是好消息 |
| 重要等级没变但**种类**变了 | 从"节点失败"变成"看门狗停摆"是另一回事 |

**刻意不算**：`info` 级变化（供参考的）、同一个 kind 等级没变只是数字变了
（那已由 `alerts_digest` 处理）、以及**进程启动时的第一次评估** ——
否则每次重启 API 都来一句"你好，现在的情况是…"，而用户什么都没做。

冷却是 `steward_proactive_min_interval_seconds`（默认 900s = 15 分钟），
比推送的 30s 长得多：主动汇报的代价高一个数量级。

### 9.1 它是真的常驻，不是"你盯着它它才主动"

跑在一个和 SSE 流**无关**的后台任务里（`lifespan` 里启动、关闭时取消）。
挂在流里等于"打开界面它才主动"，那还是查询界面。

理由（2026-10-15 实测踩到）：第一版里配置的冷却时间算出来却忘了赋给引擎，
于是 `steward_proactive_min_interval_seconds` **完全不起作用**。这个 bug 没有任何
症状，只是"配置不生效" —— 是 ruff 的"未使用变量"抓到的。

### 9.2 默认关

`CODEX_BATTLEFIELD_STEWARD_PROACTIVE_ENABLED` 默认 `0`。它会花钱、会打扰，
这种能力该由用户显式打开，而不是装完就开始。

实测（真实模型）：注入一个 `pending_actions` 级提醒后，管家自己开口说
"阻塞探针节点…事件流父子节点状态为 blocked…需要你决定" ——
措辞是**针对当下状态**的，不是模板。

## 10. 验收（可反驳）

| 编号 | 判据 | 怎么反驳 |
|---|---|---|
| S1 | 只读模式下**每一个**改状态的能力都 `available=false` | `test_steward.py::test_s1_*`（逐个核对，不是抽查） |
| S2 | 空库上用量的 `available` 是 `false` 而不是 0 | `test_s2_snapshot_is_honest_about_missing_usage` |
| S3 | payload 不符契约时仍产出非空 `reply` | `test_s3_*` |
| S4 | 轮廓超上限时 `outline_truncated=true` 且省略数正确 | `test_s4_outline_truncation_is_reported`（真造 MAX+5 个节点） |
| S5 | 对话落库带 `read_only_mode` 元数据 | `test_endpoints_chat_roundtrip_and_persist` |
| S6 | 清对话**不**删树 | `test_clear_history_only_clears_the_conversation` |
| A1 | 白名单与执行器**双向**一一对应 | `test_steward_actions.py::test_a1_*`（两个方向都查） |
| A2 | 幻觉动作名不入库、且回复里列出可用动作 | `test_a2_hallucinated_action_is_rejected_with_the_valid_options` |
| A2 | 一批里混了坏建议时，好的那些不被连坐 | `test_a2_one_bad_proposal_does_not_discard_the_good_ones` |
| A3 | 只读模式下批准动作返回 **423** 且状态不变 | `test_a3_execution_itself_is_blocked_by_the_endpoint_in_read_only` |
| A4 | 未批准就执行必须被拒 | `test_a4_running_an_unapproved_action_is_refused` |
| A5 | 执行失败落 `failed` + 异常类型名 + 时间 | `test_a5_execution_failure_is_recorded_not_swallowed` |
| A6 | 不能重复决定同一个动作 | `test_a6_deciding_twice_is_refused` |
| A7 | 每个执行器都**真的**把 orchestrator 调起来了（抓漏 `await`） | `test_a7_*`（异步替身 + 九个动作逐个跑） |
| P1 | 同一状况在 5 轮里**只推 1 次**；措辞变化不算新情况 | `test_steward_push.py::test_p1_*` |
| P4 | 每次推送带 `reason`，四种情形文案不同 | `test_p4_push_events_carry_a_reason` |
| P5 | 推送流用 `asyncio.to_thread`（不阻塞事件循环） | `test_p5_the_stream_evaluates_off_the_event_loop` |
| W1 | 心跳过期 → `stale=true` 且阈值可解释 | `test_w1_old_heartbeat_is_stale_with_an_explanation` |
| W2 | **没有心跳文件不判停摆**（新装不该假警） | `test_w2_missing_heartbeat_does_not_claim_staleness` |
| R1 | 进程启动时第一次评估**不开口** | `test_proactive_stays_silent_on_first_evaluation` |
| R2 | 同一重要状况持续存在 → **只开口 1 次** | `test_proactive_does_not_repeat_itself` |
| R3 | 冷却生效；`info` 级变化不触发 | `test_proactive_respects_the_cooldown` / `test_proactive_ignores_info_only_changes` |
| R4 | 冷却配置**真的接到引擎上**（回归：曾经是死的） | `test_proactive_interval_setting_reaches_the_engine` |
| R5 | 后台任务在关闭时被取消 | `test_proactive_loop_exists_and_is_cancelled_on_shutdown` |

## 9. 未做与下一步

| 项 | 为什么没做 | 要真做需要什么 |
|---|---|---|
| 工具调用（function calling）而不是喂快照 | 快照更可靠（不受 shell 沙箱限制），且视野可控 | 验证 codex CLI 的工具调用面，并处理沙箱限制 |
| 跨工程/跨树的全局视野 | 管家存工程库里，天然只看得见当前工程 | 需要一个全局索引（当前 `projects/` 下每工程一库，跨库查询要单独设计） |
| 主动**推送**（不等界面刷新） | 现在是拉取式（进面板即刷、答完即刷） | SSE/WebSocket + 节流 + 用户可配的打扰阈值 |
| 动作的**批量**批准 | 一次一个是有意的：每个动作的代价不同，批量点等于没看 | 只在动作风险分级可靠之后才做 |
