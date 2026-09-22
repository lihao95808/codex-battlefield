# Codex Battlefield 模块化与完善 SPEC

状态：执行中（W0、W1 已完成；W2 待做）
基线：`api` pytest **324 passed / 0 failed**（约 3 分 45 秒），且不依赖宿主空闲内存
续跑锚点：**`PLAN-STATUS.md`**（中断后从该文件恢复，不要依赖对话记忆）

## 1. 目标

1. 把 `orchestrator.py`（10914 行、单类约 390 方法）拆成职责清晰的模块，降低维护与调试成本。
2. 消除"配置写了不生效""语义不明"的隐性缺陷，让运行时行为可观测、可解释。
3. 支持**按角色分层用模型**（planner/acceptance 用强模型，worker/verifier 用便宜模型），并把 token/成本记账落库。
4. 在交互上**精简地**呈现关键信号，而不是堆 30 个磁贴。

## 2. 非目标

- 不改动递归任务树的铁律与判定逻辑（宪法包络线、300% 叶子、验证失败转父节点、父节点真实验收）。
- 不做"大爆炸"式重写。每一波都必须保持 pytest 全绿，且重构波次只做**代码搬移**、不夹带行为变更。
- 不实现 `RESOURCE_POOL_DESIGN.md` 里的完整资源池（lease/slot/capability 路由）。只做其中投入产出比最高的两块：按角色模型路由 + 金额记账。

## 3. 现状事实（已核实，带行号）

| 事实 | 位置 | 含义 |
|---|---|---|
| `max_children` 全代码库零引用 | `config.py:60` | 纯死配置 |
| `max_depth` 仅作统计量出现，与配置字段无关 | `orchestrator.py:5672/5968/6288` | 死配置；`FINAL_GOAL.md` 铁律 2 本就要求"不设置固定最大深度/宽度" |
| `max_acceptance_repair_cycles` 默认 0，代码为 `if repair_cycle_limit > 0 and ...` | `config.py:95`、`orchestrator.py:5561-5563` | 0 等价于**不限次**，而铁律 8 要求"父节点多轮 repair 进入人工审查" |
| `kind` 未传入 `_codex_exec_args` | `runner.py:950`、调用点 `:824` | 所有角色共用同一全局模型，无法分层 |
| `turn.completed.usage` 从未解析 | `runner.py:1120-1142` 是唯一 stdout 解析处 | token/成本从未落库 |
| 已有增量迁移机制 | `database.py:64-92`（`task_additions`/`run_additions`） | 加列可安全落地，无需引入迁移框架 |
| `_is_local_codex_skill_failure` 与 `_is_local_codex_skill_failure_text` 逐字节相同 | `runner.py:1408`、`orchestrator.py:7670` | 重复实现 |
| 瞬态/配额分类、block-report 构造在 runner 与 orchestrator 各有一套 | 见 W3 | 重复实现，词表易漂移 |
| `App.tsx` 单组件 958 行；`tree.events` 已取数但从未渲染 | `App.tsx:406-1364` | 交互层可精简且有无成本可捡的信号 |

## 4. 全局验收门槛

本节是**每波的最低门槛**；反缩水的完整硬指标见 §7.2（A1–A7），两者都必须满足。

每完成一波，必须全部通过：

| 门槛 | 命令 | 适用 |
|---|---|---|
| 后端全绿 | `cd api && .venv\Scripts\python.exe -m pytest -q` | 每波 |
| 前端全绿 | `cd web && pnpm lint && pnpm test && pnpm build` | 涉及 `web/` 的波次 |
| 真实通道探针 | `. .\scripts\use_codemaker_hub.ps1; python .\scripts\verify_codemaker_hub_planner.py` | 涉及 runner/模型的波次 |
| 重构无行为变更 | 搬移前后 pytest 结果一致，且 diff 不含逻辑改写 | W4 |

## 5. 波次

### W0 基线修复（已完成）

- 修 3 处真实缺陷：`has_topoff_headroom` 漏 `frozen_mode`；`machine_safety_waiting` 的 `goal_mode` 门控与启动守卫不一致；`_accept_parent` 中 `final_gate_repair_gate` 未初始化导致 `UnboundLocalError` 潜伏崩溃。
- 修 4 处过期/环境依赖测试；新增 `api/tests/conftest.py` 使测试套件不依赖宿主空闲内存。
- 结果：324 passed / 0 failed。

### W1 配置语义修正

| 编号 | 动作 | 依据 | 验收 |
|---|---|---|---|
| W1.1 | 删除 `max_depth` / `max_children`（config.py:59-60） | 死配置；铁律 2 本就禁止固定深度/宽度；无代码/文档/测试引用 | pytest 全绿；README 明确"无固定深度/宽度是有意设计" |
| W1.2 | `max_acceptance_repair_cycles` 语义显式化：把"0 = 不限次"写成显式常量与注释，并把有效上限暴露到 watchdog state | 当前语义只存在于一行 `> 0` 判断里，操作者不可见 | pytest 全绿；watchdog state 含 `acceptance_repair_cycle_limit` |
| W1.3 | 机器安全守卫保持**无 goal_mode 门控**，并把阈值与当前空闲内存做成文档化运维旋钮 | `_hold_agent_start_for_machine_safety`(2360) 本就无门控；本机空闲 4018MB < 默认 4096MB 会整树休眠 30 分钟 | 文档记录；不改默认值（改默认会静默影响他人环境） |

风险：删除配置项会影响仍设置该 env 的使用者。缓解：README 明确说明其已被移除且行为由设计决定。

### W2 按角色模型分层 + 成本记账

| 编号 | 动作 | 改动点 |
|---|---|---|
| W2.1 | 新增按角色的模型/推理档位，空则回退全局 | `config.py:22-23` 附近新增 `codex_model_{constitution,planner,worker,verifier,acceptance}` 与对应 effort，env 形如 `CODEX_BATTLEFIELD_CODEX_MODEL_PLANNER` |
| W2.2 | 让 `kind` 流到参数构建处 | `runner.py:950` 签名加 `kind`；`:824` 传入；`:975-978` 按 kind 选择 |
| W2.3 | 解析并保留 usage | `RunnerEnvelope` 加 `usage: dict \| None`；新增 helper 扫描 `turn.completed`；在所有 `RunnerEnvelope(...)` 构造点与 `orchestrator._recovered_envelope`(1730) 回填 |
| W2.4 | usage 落库 | `models.py:110-127` `RunRecord` 加 `usage_json`；`database.py:76-81` `run_additions` 加迁移项；`orchestrator._record_run`(10785) 写入 |
| W2.5 | 状态可见 | `orchestrator.py:6311-6314` 状态快照按角色展示模型；`get_tree` stats  |

验收：单测覆盖"按 kind 选模型 + 回退全局"；真实调用后 DB 有 `usage_json`；`verify_codemaker_hub_planner.py` 通过；前端 stats 可读到模型与 token。

### W3 消除重复实现

**状态：已完成。** 抽出 `api/app/codex_signals.py` 作为信号词表与正则的**单一定义处**。

#### 实际范围（比原计划收窄 —— 原清单有误，已逐项核实）

| 项 | 结论 |
|---|---|
| skill 失败判定（`runner._is_local_codex_skill_failure` / `orchestrator._is_local_codex_skill_failure_text`） | **真重复**（逐字节相同）→ 已抽为 `is_local_skill_failure()` |
| HTTP 429 正则 | **真重复且已漂移** → 已抽为 `HTTP_429_RE`，并统一到更严的一版 |
| `TRANSIENT_CODEX_FAILURE_MARKERS` / `TIME_UNIT_SECONDS` | 原只在 runner，现统一迁入 `codex_signals` |
| `orchestrator._retry_after_seconds_from_reports` | **不是重复**：它读的是**结构化字段**（`retry_after_seconds` / `quota_window_seconds`），与 runner 的**自由文本解析**输入不同 |
| `orchestrator._has_timeout_marker` / `_has_resource_limit_marker` / `_has_strong_resource_limit_marker` | **不是重复**：含 MCP 特例与 shell 工具退出码处理，语义与 runner 的"是否该重试"不同，故留在原处 |
| `_clock_retry_after_seconds_from_message` / `_observed_local_time_from_message` | 只在 runner，无重复 |
| block-report 构造 | 只在 orchestrator，无重复 |

#### 修掉的真实缺陷（漂移）

429 正则第 4 组：

| 位置 | 形式 |
|---|---|
| `runner`（正确） | `(?![\d.]\|ms\b\|milliseconds?\b)` —— 排除 `429ms` 这类**计时串** |
| `orchestrator`（副本） | `(?![\d.])` —— **没有排除**，会把耗时误判成限流 |

统一后两边判定一致。这是"同一语义两份实现"必然漂移的实例。

#### 做法

- 抽出词表与正则；`runner` / `orchestrator` 各保留**一行委托**，避免大范围改调用点（降低 W3 引入行为变更的风险）。
- `orchestrator._has_resource_limit_marker` 的内联正则改为使用共享 `HTTP_429_RE`。

#### 验收（已达成）

- `pytest` **339 passed / 0 failed**（334 + 新增 5 个信号测试）。
- 新增 `api/tests/test_codex_signals.py`，含**反漂移测试**：对同一组输入，runner、orchestrator、`codex_signals` 的判定必须完全一致；并锁定"`429ms` 不得被当成 HTTP 429"。
- runner 与 orchestrator **不再各自持有**分类词表。

### W4 模块化拆分（只做搬移）

按抽取风险从低到高，每步单独验证：

| 步 | 模块 | 行数 | 风险 |
|---|---|---|---|
| 4a | `godot_files.py` | ~294 | 最低（纯 `Path`/`str` 函数） |
| 4b | `runtime_markers.py` | ~155 | 低（静态文本分类） |
| 4c | `godot_gates.py` | ~580 | 低-中 |
| 4d | `checkpoint.py` | ~590 | 中 |
| 4e | `budget.py` | ~230 | 中 |
| 4f | `watchdog.py` | ~1330 | 中 |
| 4g | `scheduling` / `recovery` / `blockers` | ~2585 | 最高，单独波次 |

手法：`Orchestrator` 保留 `_mark`、`_add_event`、db 会话与 `self.settings` 作为基座；被抽出的逻辑以 mixin 或自由函数形式接入。`blockers` 被 5 处以上调用，必须最后抽。

验收：每步 pytest 全绿，且 `orchestrator.py` 行数单调下降。

### W5 交互精简

**状态：已完成。**

| 编号 | 动作 | 数据来源 | 结果 |
|---|---|---|---|
| W5.1 | `GlobalNavigationDock` 增加 blocked 条带（数量 + 首要原因） | 已算好的 `nodeBlockSummary`（新增 `summarizeBlockedNodes`） | 完成，无新 API |
| W5.2 | 渲染全局事件 ticker | `tree.events`（**此前已取数但从未渲染**） | 完成，左栏「最近事件」，按严重度着色 |
| W5.3 | 32 个 stat 磁贴收敛为 6 主项 + `<details>` 折叠 | `stats` | 完成，**信息零丢失**（折叠而非删除） |
| W5.4 | 显示模型与通道 | `stats.codex_model` / `stats.codex_home` | 完成，dock 显示「模型 · 通道」 |
| W5.5 | 每节点证据徽章 + 详情面板用量 | `task.result.evidence` / `changed_files` / `tests`、`detail.usage` | 完成 |
| W5.6 | 成本读数 | `usage_json` 汇总（W2.5 已落） | W2.5 已完成，W5 补详情面板展示 |

#### 设计取舍

- **主项与 dock 互补而非重复**：`阻塞`、`槽位` 已在 dock 常驻可见，故左栏主项取「规模与并发」六项（总数/完成/槽位/运行节点/并发上限/自动油门），其余 26 项折叠进「更多指标」。
- **折叠而非删除**：UI 精简的目标是降低扫视负担，不是丢信息；用原生 `<details>` 实现，无需额外 state。

#### 一处必要的后端补齐（W5.4 的使能项）

`stats.codex_model` 原先在未设 `CODEX_BATTLEFIELD_CODEX_MODEL` 时只报「默认」。但 Hub 模式**故意**清空该环境变量、让隔离 `config.toml` 决定模型——于是看板无法回答"现在跑在哪个模型上"。

新增 `orchestrator._effective_codex_setting(key)`：环境变量为空时回读隔离 `CODEX_HOME/config.toml` 的对应键。实测 Hub 模式下现在上报 `deepseek-flash`。

范围上也刻意收窄：`codex_home` 字段**无需新增**，它早已在 stats 中，通道标签只需取路径末段并给完整路径做 `title`。

#### 验收

| 门槛 | 结果 |
|---|---|
| A2 前端 | `pnpm lint` 干净；`pnpm test` 2 文件 / 5 测试通过；`pnpm build` 成功 |
| A1 后端 | **339 passed / 0 failed**（W5.4 的后端补齐之后重跑） |
| 数据契约 | 用已完成烟测库核对看板所依赖的字段：`usage`（runs=3、总 216,658）、`events`（10 条，`id/type/message/created_at` 齐全）、`codex_model=deepseek-flash`、`codex_home=<隔离路径>`、`stats` 仍为标量字典 |
| **A6 浏览器 console = 0** | **未验证——我没有浏览器可执行 JS**。改动全部是纯渲染（未新增 `useEffect` / 异步 / fetch），故新增 console error 面为零，但这是**推理而非测量**，不得声称已通过。核对方法：启动 `api` 与 `web` 后打开 `http://127.0.0.1:5173`，看 DevTools Console 是否为空 |

## 6. 未决决策

| 编号 | 决策 | 推荐 | 理由 |
|---|---|---|---|
| D1 | `max_acceptance_repair_cycles` 默认是否由 0（不限）改为 2 | **已决（2026-09-22）：改，且已落地** | 铁律 8 要求"父节点多轮 repair 进入人工审查"，默认不限等于默认关闭该保护。实现：`config.py` 默认值 `0 → 2`（`0` 仍是"不限"这个语义本身，不再当默认）；证据 `api/tests/test_orchestrator.py::test_acceptance_repair_cycle_limit_defaults_to_two`（同时断言显式 0 仍是"不限"）+ `-k repair` 29 例全绿 |
| D2 | 机器安全阈值默认 4096MB 是否下调 | **保持默认**，改运维侧 `CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB` | 改默认会静默改变他人机器行为；阈值本质是环境相关参数 |
| D3 | W4 采用 mixin 还是自由函数 + 门面 | 自由函数 + 门面 | 自由函数无隐式 `self` 依赖，便于单测；mixin 会让"哪些方法属于哪个模块"继续模糊 |

## 7. 反缩水验收契约

### 7.1 立约原因

产出方自证完成，是"大目标偷偷缩成 demo"的结构性成因。共四条：

1. **产出方自证完成**，没有任何对抗性角色拿成品去对原目标。
2. **上下文预算有限**，一旦需要压缩，先丢的就是"还有 N 个边界没处理"。
3. **最省成本路径永远偏向最小可交付**：能"看起来完成"的 demo 比完整交付便宜几十倍。
4. **目标没有可测验收面**时，模型只能对齐"看起来完成"这个软目标。

因此每一波都必须有**可测、可被反驳**的验收面，而不是自述。

### 7.2 硬指标（必须全部满足）

| 编号 | 指标 | 门槛 | 如何反驳偷工 |
|---|---|---|---|
| A1 | 后端测试 | `pytest` **0 failed**，passed 数 **不低于 324** | 不允许删测试、跳测试、放宽断言来变绿 |
| A2 | 前端 | `pnpm lint && pnpm test && pnpm build` 全绿 | 同上 |
| A3 | 模块化实效 | **代理指标，不再作为开关**：`orchestrator.py` 行数作结果记录 | 命令固定为 `python -c "import pathlib;print(len(pathlib.Path('api/app/orchestrator.py').read_text(encoding='utf-8').splitlines()))"`。**禁止用 PowerShell `Measure-Object -Line`**，它跳过空行、会少算约 500 行。基线 10914，当前 8711，全搬完预计 3376 —— 即 `< 7000` 无论怎么搬都会过，**已失去区分度**。真正的完成判据见 §7.8 |
| A4 | 改动可核 | 每波给出 `file:line` 级 diff 摘要 | 不接受"已完成""基本完成"这类措辞 |
| A5 | 模型分层生效 | 贴出 worker 与 planner 两次**真实调用**的 command 行 + DB 中 `usage_json` | 必须证明两者模型名不同，否则等于没做 |
| A6 | 交互有效 | 浏览器 **console error = 0**，并给出可复现的 DOM/截图证据 | 不接受"看起来对" |
| A7 | 无行为变更（仅 W4） | 搬移前后 pytest 结果一致，diff 只含 import 与移动 | 逐文件确认没有夹带逻辑改写 |

### 7.3 禁止事项

- 用"部分完成""基本完成""大方向已通"包装失败。
- 以耗时为由降低 A1–A6 门槛。
- 删测试、放宽或把硬断言改成软断言来换取变绿。
- 声称验证通过却拿不出原始输出。

### 7.4 失败处理

任一门槛未过 → **立即停止该波**，报告原始输出与根因，不进入下一波。

### 7.5 成本约束

单次真实 Codex 调用成本量级（Hub / `deepseek-v4.1-flash` 实测）：planner 调用 29227 in / 1718 out；简单问答 53k token ≈ 0.03 元。

整体目标：**自动化的累计成本显著低于同等人工投入**。

### 7.6 尝试方向授权

允许沿「先查生态 → 整合 → 兼容 → 优化 → 必要时自研」的路径尝试。每引入一个依赖必须在下方决策表登记：候选方案、许可证、整合成本 vs 自研成本、决策理由。

采购：**评估由我给出，下单由用户执行**；优先官方/授权渠道，避免项目继承许可证风险。

### 7.7 依赖决策登记

| 波次 | 需求 | 候选 | 决策 | 理由 |
|---|---|---|---|---|
| W4 | SQLite 迁移 | 自建增量迁移（现有）/ alembic | 沿用自建 | `database.py:64-92` 已够用，引 alembic 属净增成本 |

### 7.8 W4 完成判据（内聚优先，2026-09-13 修订）

#### 为什么改

A3 原本写作「`orchestrator.py` < 7000 行」。它是一个**代理指标**，而 §1 的目标是「拆成职责清晰的模块，
降低维护与调试成本」。两者会背离，且实测已经背离：

| 事实 | 数值 |
|---|---|
| `orchestrator.py` 当前 | 8711 |
| 不可搬核心（26 方法，即真正要留在里面的东西） | 2119 行 |
| 可搬 | 6035 行（350 方法） |
| 全部搬完后预计 | **3376** |

即 `< 7000` **无论怎么搬都会过**。一个"怎么做都能达标"的门槛无法区分完成与未完成，而且它鼓励最短路径：
**把几千行塞进一个 `misc.py` 就能立刻达标** —— 数字过，目标死。

#### 判据（C1–C5）

| 编号 | 判据 | 可反驳性 |
|---|---|---|
| **C1** | 无巨型杂项模块；每个抽出的模块有单一职责 | 人工评审，可提交异议清单 |
| **C2** | **依赖方向单向**：`api/app/` 下除 `orchestrator` 自身与入口 `main` 外，任何模块不得 import `orchestrator`；模块依赖图无环 | **机器判定**：`api/tests/test_module_layering.py` |
| **C3** | 每波给出 `file:line` 级证据，且 pytest 全绿、passed 数不下降 | 已有（A1/A4/A7） |
| **C4** | **行数只作结果记录，不作开关**（A3 降级为读数） | — |
| **C5** | 明确列出"暂不搬"的清单与理由 | 见下 |

C2 是本次修订的核心：反向依赖是比行数更隐蔽的倒退 —— 下层模块一旦 import `orchestrator`，
搬出去的东西又被核心绑住，模块化等于白做。它现在有测试守着，而不是靠自觉。

#### C5：暂不搬清单与理由（均为实测）

| 目标 | 实测 | 结论 |
|---|---|---|
| 调度 / 休眠恢复族（`hibernate_tree`、`_schedule_auto_resume`、`resume_hibernated_tree`、`_process_node*` …） | 闭包 **284 方法 / 7187 行**，触达 8 个不可搬核心 | **不是"只搬移"能做的手术**。按 A7 的定义，W4 不碰它 |
| `set_budget_guard`(48)、`_write_state_snapshot`、`_restore_snapshot_if_missing`、`get_watchdog_state`(341)、`_accept_parent`(336)、`_execute_leaf`(180) | 属 26 个不可搬方法 | 它们是**大量子系统的消费者**，不是内聚的一片 |
| `runner.py`（**2809 行**，比任何新模块都大） | W4 从未触碰 | **本波范围外**。但按 C1 的精神它同样是维护成本热点，应另开一波评估，不能因为"行数已达标"就当作不存在 |

#### 本波已交付（9 波，全部 pytest 全绿）

| 波次 | 模块 | 净行 |
|---|---|---|
| W4e | `budget.py` + `utils.coerce_utc` | −103 |
| W4f | `watchdog.py` | −81 |
| W4g-a | `blockers.py` | −503 |
| W4g-b | 看门狗钩子 + Codex 健康快照 | −198 |
| W4g-c1 | `runtime_logs.py` | −242 |
| W4g-c2 | `concurrency.py` | −233 |
| W4g-d | `recovery.py` | −288 |
| W4g-e1 | `skill_notes.py` | −72 |
| W4g-e2 | `convergence.py` | −166 |

`orchestrator.py`：10934 → **8711**（累计 −2223），测试 324 → **519 passed / 0 failed**。

#### 未做但已规划（保留为可选项，不再是"完成"的必要条件）

`W4g-e3 final_gate.py`（约 740 行、21+ 方法，实测闭包不触达不可搬方法）；`final_gate` 之后仍有
child 创建/插入、`serialize_*` 一族、worktree 与集成、Godot 门禁本体、宪法包络线五簇未测。
这些**继续做只增加清晰度，不再影响是否达标**。

## 8. 工作区卫生与基线要求

### 8.1 现状（2026-09-11 实测）

- 最后一次提交：`048132e`（2026-06-22）。
- 工作区存在**数周前的未提交改动**，与本次工作无关：

| 文件 | 改动 | 最后修改 |
|---|---|---|
| `api/app/main.py` | +62 | 2026-07-07 |
| `api/app/runner.py` | +2 | 2026-06-23 |
| `web/src/model.ts` | +130 | 2026-07-07 |
| `web/src/styles.css` | +69 | 2026-07-07 |
| `web/src/App.tsx` | +14 | 2026-07-07 |
| `web/src/model.test.ts` | +41 | 2026-07-07 |
| `scripts/battlefield_watchdog.ps1` | +18 | 2026-06-23 |
| `scripts/register_battlefield_watchdog_task.ps1` | +5 | 2026-06-23 |

- **该批未提交改动包含 `codex_home` 特性**：`config.py:20`（配置项）、`runner.py:2165-2168`（注入子进程 env）、`orchestrator.py` 的 watchdog/kick 参数传递（`6975/7160/7208`）、状态上报 `6312`，以及测试 `test_orchestrator.py:6124`。
- **而 CodeMaker Hub 集成正建立在此特性之上**（`docs/codemaker-hub-provider.md` 依赖 `CODEX_BATTLEFIELD_CODEX_HOME` 做隔离）。也就是说：Hub 方案的实际依赖是「未提交的工作」。归因前必须先确认该特性已随基线提交，否则 Hub 集成的可复现性没有保障。
- 注意 `state_snapshots`（`_write_state_snapshot:5620+`）语义是崩溃恢复，**不可**挪作版本基线用，否则两套语义互相污染。

### 8.2 为什么这是验收风险

A4（每波 `file:line` 证据）与 A7（W4 只做搬移、无行为变更）都依赖**能区分"本次改的"与"先前改的"**。当前 `git diff` 混着两份来源不同的改动，无法据其归因；而 A1 的 324 passed 是在「脏工作区 + 本次改动」上测得的，不代表已提交状态可用。

### 8.3 要求

1. 推进 W2 之前，先把当前工作区提交为一个**基线提交**，此后**每波一个提交**。
2. 该基线提交的信息必须写明：同时包含「数周前遗留的未提交改动」与「W0/W1 修复」，避免日后误判归属。
3. 未经用户明确确认，不得整理、回退或重写那批遗留改动，也不得改写历史。


