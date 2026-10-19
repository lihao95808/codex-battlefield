# 计划状态与断点续跑锚点

> 用途：任何会话中断（聊天被打断、窗口断连、换天继续）后，**从本文件恢复**，不要依赖对话记忆。
> 最后更新：2026-09-21

> **读之前先看这条**：本文件 §2 及之后是 **W4（模块化搬移）那一轮**的记录，里面的行数、用例数、
> 波次状态都是**当时的读数**，不是今天的。恢复会话时**不要**拿这里的数字当基线去判断"环境坏了"——
> 正确做法是跑下面的命令拿到**当时的真实数字**，再和 `README.md` 的「跑测试」一节对齐；
> 两者不一致就以实测为准，并把本文件与 README 一起改到事实。

## 左栏交互精简（2026-10-19）

默认聚焦目标与任务边界流程；运行选项和画布显示折叠，辅助工具归入四组。
首次展开才挂载，收起不清空输入；工程绑定使用稳定实例，未绑定时自动展开。
独立提醒条保留告警 / 待审批 / 工程快照失配，虚拟化省略和全局错误仍可见。
实现及验收边界见 `docs/frontend-split.md` §7；验证记录见 `VERIFICATION_STATUS.md` 的同名小节。

## 1. 立刻可核验的基线

```powershell
cd <repo>\api
.\.venv\Scripts\python.exe -m pytest -q
```

期望：**0 failed**。passed 数只增不减——**不写死在这里**（写死过一次：这里曾长期挂着
"期望 380 passed，若不符先停下查因"，而当时实际早已 686，照它恢复会话会当场误停）。
要一个具体数，看 `README.md` 的「跑测试」一节（那里标了日期，是快照不是承诺）。

前端：

```powershell
cd <repo>\web
pnpm lint ; pnpm test ; pnpm build
```

`orchestrator.py` 行数（A3 指标，命令固定，**不要用 `Measure-Object -Line`**）：

```powershell
python -c "import pathlib;print(len(pathlib.Path('api/app/orchestrator.py').read_text(encoding='utf-8').splitlines()))"
```

- 基线（提交 `64cd8d7`）：**10934**
- W4 收尾时的读数：**8711**（W4 起点 10934，累计 **−2223**）
- **当前（2026-09-24 实测）：10587** —— W4 之后 R1/R3/R5/R6 与后续几波又加了功能，行数继续涨。
  这**不影响**"W4 按 C1–C5 达标"的结论（A3 早已按 C4 降级为读数）：它只说明行数不是目标，职责边界才是。
  要拿它当基线，**现跑上面那条命令**，别抄这里的数（这一行被抄旧过一次：长期挂着 2026-09-21 的 10134，
  而实际早已 10587）。
- **完判据已修订为 C1–C5（内聚优先），A3 降级为读数** —— 见 `docs/spec-modularization.md` §7.8 与本文件 §2.6
- 不再达标条件：`< 7000` 无论怎么搬都会过（全搬完预计 3376，不可搬核心仅 2119 行），已失去区分度
- 可行性见 §4 决策 A-2：普查实测**全部搬完后预计 3434 行**，因此 A3 可达，风险在分组而非行数

## 1.5 提交记录（每波一个提交）

| 提交 | 内容 |
|---|---|
| `64cd8d7` | 基线：遗留改动 + W0/W1/W2a + Hub 集成 |
| `319aebd` | R1 Godot 容量可配 + 排队可见 |
| `14cee6e` | W2.5 供应商无关的用量与成本记账 |
| `ca95d20` | W3 信号单一定义（修掉 429 正则漂移） |
| `d16b952` | W5 交互精简 |
| `c7b3ee6` | 停止跟踪生成的工具脚本（移除 764 行过期生成码） |
| `04ab534` | W4a `godot_files.py`（净 −54） |
| `05c2130` | W4b `runtime_markers.py`（净 −147） |
| `d12f206` | 锚点更新 |
| `b4960bb` | W4c `godot_gates.py`（净 −164） |
| `8c85f32` | W4d `checkpoint.py`（净 −64） |
| `27367a7` | 普查脚本加 `--names` / `--closure` |
| `f6e5a37` | 记录 W4e 执行方案（标题误用 `27367a7` 的同一条信息，见 §5 陷阱） |
| `e3bc7df` | W4e `budget.py` + `utils.coerce_utc`（净 −103） |
| `23d6c0e` | 锚点更新：W4e 完成 |
| `eb370e8` | W4f `watchdog.py`（净 −81） |
| `8b2b480` | W4g-a `blockers.py`（净 −503） |
| `ba9e6d9` | 锚点更新：W4g-a 完成 |
| `78fc498` | W4g-b 看门狗钩子 + Codex 健康快照（净 −198） |
| `35b5054` | 锚点更新：W4g-b 完成 + W4g-c/d/e 顺序 |
| `d11b221` | 普查脚本新增 `--state`（状态耦合实测） |
| `e78344c` | W4g-c1 `runtime_logs.py`（净 −242） |
| `b0f83e2` | W4g-c2 `concurrency.py`（净 −233） |
| `49e994a` | W4g-d `recovery.py`（净 −288） |
| `05a9bb2` | 锚点更新：W4g-c/d 完成 + W4g-e 拆分 + A3 未达标 |
| `2ff6274` | W4g-e1 `skill_notes.py`（净 −72） |
| `26c662f` | 锚点更新：W4g-e1 完成 |
| `be2dedf` | W4g-e2 `convergence.py`（净 −166） |

> 上表停在 W4 那轮。**W4 之后（R-a/R-b/R-c、R1、R3–R6、发布快照、本轮审计）的提交见
> `git log --oneline -20`**；波次与提交的对应关系在 §2 表（每行的证据列写了提交号）与 §2.7–§2.8。
> 不再往里补行：这份表越长越容易和事实脱节，`git log` 才是唯一不会过期的来源。

## 2. 波次状态

> **读这张表前先知道它会怎么坏**：表里的"状态"是**写下那一刻**的判断，而后续波次往往顺手把某一行
> 真正做掉（R2 由 `6370b30` 做掉、W2b 随 B 波 `ba6b0af` 做掉），却**没人回来改这张表**——于是
> §2.x 的小节写着"做了"，表里还挂着"未开工 / 暂缓"，同一份文档自相矛盾。这个方向特别坏：
> 接手的人按表挑"下一件待做"，会去**重做已经做完的东西**。2026-09-24 核实并修正了上面两行。
> **所以每波收口的动作清单里必须有一条：回看本表，把被这一波改变了状态的行一起改掉**
> （包括把"做了一半"如实写成做了一半）。

| 波次 | 内容 | 状态 | 验收证据 |
|---|---|---|---|
| Hub 集成 | 本工程 codex CLI 走 自定义 provider 便宜模型 | **完成** | 真实烟测 `ok: true`、root `done`；`docs/provider-setup.md` |
| W0 | 基线修复（7 个既有失败 + 宿主内存依赖） | **完成** | 324 passed / 0 failed |
| W1 | 配置语义修正（死配置、验收次数语义、资源保护文档） | **完成** | 324 passed / 0 failed |
| W2a | token/成本记账（usage 落库） | **完成** | `RunnerEnvelope.usage` + `usage_from_codex_logs()` + `RunRecord.usage_json` + 迁移项；pytest 绿 |
| W2b | 按角色模型分层（planner/worker/verifier/acceptance 各用不同模型） | **已完成**（`ba6b0af`，随 B 波一起做；原标"暂缓（用户指示）"是**过期标记**，2026-09-24 核实） | `codex_model_{constitution,planner,worker,verifier,acceptance}` + reasoning effort 同族，`kind` 流到 `_codex_exec_args`；看板 `model_tiers` 报**已解析**配比（空档显示"全局默认档"，不是空串），刻意不给模型排强弱。证据 `tests/test_model_tiers.py` 7 例（含"两次命令行里的模型名真的不同"）。详见 §2.10 |
| W2.5 | 成本/用量接到看板（供应商无关） | **完成** | 新增 `api/app/usage.py` 作为唯一换算边界；`get_tree`/`get_detail` 返回 `usage`；看板 dock 显示用量与分币种金额；后端 **334 绿**，前端 lint/test/build 绿。详见 `docs/usage-accounting.md` |
| W3 | 消除 runner/orchestrator 重复分类逻辑 | **完成** | 抽出 `api/app/codex_signals.py`；顺手修掉 429 正则的**漂移**；pytest **339 绿**（新增 5 个含反漂移测试）。范围比原计划窄——原清单有误，已逐项核实并记录 |
| W4a | 抽出 `godot_files.py`（Godot 文件读写纯函数） | **完成** | 净 −54 行；新增 11 个测试；pytest 350 绿 |
| W4b | 抽出 `runtime_markers.py`（标记表 + 阻塞分类判定） | **完成** | 净 −147 行；新增 9 个测试；pytest 359 绿 |
| W4c | 抽出 `godot_gates.py`（Godot 校验纯函数 + 4 个常量） | **完成** | 净 −164 行；新增 10 个测试；pytest 369 绿。门禁本体因需 `db`/`task` 留在 orchestrator |
| W4d | 抽出 `checkpoint.py`（文件系统 / 解析那一半） | **完成** | 净 −64 行；新增 11 个测试；pytest 380 绿。写入/恢复那一半**不可搬**（见 §4 A-2） |
| W4e | 抽出 `budget.py` + `utils.coerce_utc` | **完成** | 净 −103 行；新增 16 个测试；pytest **396 绿**。`set_budget_guard`(48 行)**实测不可搬**，故模块只含 guard 状态推导那一半 |
| W4f | 抽出 `watchdog.py` | **完成** | 净 −81 行；新增 15 个测试；pytest **411 绿**。只搬可搬的 115 行闭包；`_can_hibernate_task`(4 行)**实测会把 495 行的阻塞分类簇拖进闭包**，故留给 W4g |
| W4g-c | 自动并发 | **完成**（c1+c2） | 净 −475；`runtime_logs.py` + `concurrency.py`；pytest **469 绿**。5 个状态耦合方法用 **ports 端口对象**留薄壳，零调用点改动 |
| W4g-d | 恢复已完成消息 | **完成** | 净 −288；`recovery.py`（5 个 `recover_finished_*`）；`STOPPED_STATUSES` 移入 `models.py`；pytest **490 绿** |
| W4g-e | 最终成品门禁 | **可选，未做**（e1/e2 已完成） | e3 `final_gate.py` ~740 行属**清晰度优化**，不是达标条件：W4 的判据 C1–C5 已满足（§2.6），且 e3 的成员职责本就是成品门禁判定，搬出去只是换个壳。要做时按 §2.5 的手势先测闭包 |
| W5 | 交互精简（blocked 条带、事件 ticker、磁贴收敛、模型与通道、证据徽章、详情用量） | **完成** | 前端 lint/test/build 绿；后端 339 绿；A6（浏览器 console=0）原标"**未验证**，无浏览器可测"是**过期标记**（2026-09-24 核实）——已用 `scripts/verify_browser_console.py` 在真 Chrome 里测过：2264 节点全渲染（42399 个元素）、**0 条 console error**，含"收集器自检"反面自检。详见 §2.20 与 `docs/spec-modularization.md` §5.1 |
| R0 | 沙箱与隔离能力实测（E1–E9） | **全部完成** | 结果见 `spec-resource-governance.md` §4。核心：**写隔离天然成立**；**读隔离 codex 不提供，须落 OS 层**；**凭据用隔离 `CODEX_HOME`+`env_key` 可规避** |
| R1 | 命名资源租约与优先队列 | **全部完成**（容量可配 + 排队可见 + 静态优先级 + 老化） | 第一轮：4 进程争 capacity=2 → 实测最大并发 2；状态含 holders/waiters。第二轮（`abef3a0`）：优先级按**角色**权重（acceptance 3 > verifier 2 > worker 1 > planner 0）+ 应用层仲裁 + 老化防饥饿；实测容量 1 下权重 0/1/2 的获取顺序 **2,1,0**，老化让先到的低权重反超。高优**预留容量**仍不做（`spec-resource-governance.md` §7 E-3）。pytest **711 绿** |
| R2 | 任务间隔离（同账号下互不越权） | **已完成**（`6370b30`；原标"未开工：属用户决策级改动"是**过期标记**，2026-09-24 核实） | 第一个动作是**把 R0 的旧结论拿去重测并推翻它**：`codex-cli 0.154.0` 下 `--cd node-a` 读 `node-b` → `Access is denied`，**读隔离本来就成立**（所以原计划里的 Windows ACL 不需要做）。落地三件 + 一件顺带修：每节点隔离 `CODEX_HOME`、跨节点泄露检测（advisory `cross_node_leak`）、断言命令行无 `--add-dir`、子进程环境**默认收窄**（100+ 键 → 54 键，真实 CLI 验证过收窄后仍能跑）。**未实现项如实列出**：`env_key` 凭据仍走环境变量、出网不在范围、读隔离依赖沙箱行为故留手工重跑步骤 ISO-3。证据 `tests/test_task_isolation.py` 11 例 + `scripts/verify_isolation_boundary.py`。详见 §2.12 |
| R3 | worktree 生命周期（评估 codex 原生 `--worktree`） | **已完成**（`d7bd28d` + `04088da`；不采用原生 `--worktree` 由 §7 E-1 定） | 判据保守：路径守卫（只碰 `<workspace>/worktrees/`）、**证据前置**（声明产物必须在父节点集成目标里找得到，找不到就不删）、只回收 `done`/`canceled`（`failed` 现场留着）、保留期 86400s 可配。创建串行化按目标仓库取租约，拿不到就不硬上 git（退化拷贝目录、不阻塞调度）。回收 git 目标走 `worktree remove --force` + `prune` + 只删 `codex/node-*` 分支；拷贝式直接删目录；删完还在就当失败不谎报；`node_meta["worktree_recycled"]` 留痕保证幂等。看板 `get_watchdog_state()["worktrees"]`（新 `WorktreeLifecycleBoard`）。证据 `test_worktree_lifecycle.py` 8 例 + `test_worktree_recycle.py` 9 例；pytest **751 绿** |
| R4 | 合并冲突不再静默 | **已完成**（原标"待做"是过期标记，2026-09-20 核实） | 识别 `integration.detect_path_collisions` + 结构化明细（kept/overwritten/writers）+ 三档策略 `integration_conflict_strategy`（`repair` 档才阻断）+ 父节点轮次域债务上浮（用完整明细，不受 `detail_limit` 截断）+ `repair` 档收尾门禁 + `collision_directive` 进父节点提示词。证据：`test_integration_conflicts.py` / `test_obligation_gate.py` / `test_obligations.py` |
| R5 | 跨版本回归门禁 | **已完成**（`2314019`） | 基线（版本 + `constitution_hash` + 产物 sha256）在每次验收通过时重记，**只在跨版本时**比对（同版本内返工不比，避免拦下自己的修复）。`missing` 一律 blocking；`changed` 由 `integration_regression_strategy` 决定（`record` 默认只上浮 / `repair` 阻断）。债务走既有门禁→repair 通路，产物恢复后按事实清债；不在轮次域也不在版本域（两条都是静默放宽的入口）。实测：删产物必挡且理由点名路径；同版本改动不比。证据 `test_regression_gate.py` 11 例；pytest **722 绿** |
| R6 | 失败复盘纳入失败/阻塞子树 | **已完成**（`ddce929`） | `failure_retrospective.py` 抽四条证据（block_report / blocked_child_reports / error_message / result_json），按 category / retryable / parent_action 聚合；父层在"子节点全 done 即将验收"时写入 `node_meta[failure_retrospective]`（与 skill note 开关解耦，无失败不写空壳）；note 子任务采集范围改为显式带 `FAILURE_RETROSPECTIVE`；复盘产物 `docs/skill_notes/<parent>_merged.json` 里带该段；`stats.failures`（新 `FailureBoard` 模型）按类别跨树可查。证据 `test_failure_retrospective.py` 12 例；pytest **734 绿** |

#### W4e 已定执行方案（开工前实测，实施后已核对）

用 `scripts/audit_orchestrator_movable_mass.py --closure ...` 实测得到：

- 入口集（guard + burst）：`_budget_guard_path`、`_read_budget_guard`、`_write_budget_guard`、`_active_budget_guard`、`budget_guard_state`、`_agent_burst_limit`、`_agent_burst_effective_reserve`、`_agent_burst_work_limit`、`_auto_concurrency_bounds`、`_budget_warning_state`
- **闭包 = 11 个方法 / 103 行函数体，不触达任何不可搬方法** ✓
- 但闭包含 `_coerce_utc`(6 行)，它**被预算簇之外的调用者共用**（实测 16 处调用点）→ 移入 `utils.py`（低层），而不是 `budget.py`
- 若把 `_agent_burst_used_for_root` / `_should_preserve_agent_budget` 也纳入，闭包会带出 `_subtree`、`_available_memory_mb`、`_reconstruct_agent_burst_used`，**闭包近乎翻倍** —— 这两个入口建议留到后续一步

实施结果（与方案一致）：
① `_coerce_utc` → `utils.coerce_utc`（`api/app/utils.py:71`），orchestrator 留 2 行委托，16 个调用点**零改动**；
② 10 个方法 → `api/app/budget.py`（179 行，含 `clear_budget_guard` 与模块文档），orchestrator 各留一行委托。
③ `set_budget_guard`(48 行) 实测**不可搬**（写事件/调 `hibernate_tree`），因此 budget 模块只含 `clear_budget_guard` 那一半；这是实测结论，不是疏漏。
④ `_auto_concurrency_decision`(154 行) 与 `_auto_concurrency_fuel_state`(80 行) 实测**可搬**，但闭包会显著扩大，留给单独一步。

#### W4g 实测切分（先测，再决定搬什么）

| 子步 | 入口集 | 实测闭包 | 触达不可搬 | 结论 |
|---|---|---|---|---|
| W4g-a 阻塞分类 | `_can_hibernate_task`、`_block_category`、`_normalize_block_report`、`_build_block_report`、`_child_block_report` | **16 个方法 / 495 行** | 否 | **可搬**，且是剩余最大一块 |
| W4g-b 资源压力/健康 | `_codex_health_state`、`_resource_pressure_state`、`_available_memory_mb` 等 | 与 W4g-a 重叠 | 否 | 待 W4g-a 落定后再测 |
| W4g-c 并发决策 | `_auto_concurrency_decision`、`_auto_concurrency_fuel_state` | **57 个方法 / 1312 行** | 否 | 可搬，但含 W4g-a + `_external_codex_processes` + 槽位记账簇；须排在 W4g-a 之后 |
| W4g-d 调度/休眠恢复 | `schedule_hibernation_wakeups`、`_auto_hibernate_retryable_resource_blocks`、`_schedule_restored_hibernation_if_needed`、`normalize_hibernation_reason` | **284 个方法 / 7187 行** | **是**（`_accept_parent`、`_process_node_unlocked`、`_execute_leaf`、`_reserve_agent_start`、`hibernate_tree`、`_process_node`、`_hold_agent_start_for_machine_safety`、`_process_children_with_dependencies`） | **不是"只搬移"能做的手术**。要抽出它等于重写调度核心；按 A7「W4 只做搬移」的定义，**W4 到此为止，不搬它**。这同样是实测结论 |

即：W4 的最终形态 = W4a–W4f + W4g-a（+ 视情况 W4g-b/c）。`hibernate_tree` / `resume_hibernated_tree` / `recover_deferred_parent_resumes` / `run_tree` 等留在核心。

#### W4g-a 落地后解锁的两步（已实测，可直接开工）

`blockers.py` 搬走之后，之前被它拖住的两处闭包**立刻变小**，实测如下：

| 子步 | 入口集 | 落地前闭包 | 落地后实测闭包 | 结论 |
|---|---|---|---|---|
| W4f-b 休眠原因 + burst 重建 | `_normalize_hibernation_reason`、`_watchdog_state_snapshot_due` | 610 行 / 触达阻塞簇 | **9 个方法 / 64 行**（`_subtree` 15、`_agent_burst_used_for_root` 15、`_reconstruct_agent_burst_used` 14 …） | 可搬。`_subtree` 调用点很多，需留一行委托 |
| W4g-b 看门狗钩子 + 健康快照 | `_watchdog_hook_state` | 500+ 行 / 触达阻塞簇 | **10 个方法 / 218 行** | **已完成**（`78fc498`，净 −198） |

教训：闭包大小是**当前依赖图**的函数，不是方法的固有属性；搬走一族会解锁另一族。所以顺序应该由实测决定，且每搬一步都要重测。

#### W4g-c / d / e 已定顺序（用户已确认：按序推完 W4 再回 R2）

以下三簇在 `blockers.py` + W4g-b 落地后重测，均**不触达不可搬方法**：

| 波次 | 入口集 | 实测闭包 |
|---|---|---|
| W4g-c 自动并发 | `_auto_concurrency_decision`、`_auto_concurrency_fuel_state`、`_auto_concurrency_backlog_target` | 45 方法 / **827 行** |
| W4g-d 恢复已完成消息 | `_recover_finished_agent_messages`、`_recover_finished_{worker,verifier,planner}_message`、`_recover_finished_acceptance_messages`、`_read_recoverable_last_message` | 59 方法 / **1297 行** |
| W4g-e 最终成品门禁 | `_final_root_product_gate`、`_proactive_root_final_gate_children`、`_targeted_final_gate_repair_children`、`_create_final_gate_repair_children`、`_settle_passed_root_final_gate_branches` | 70 方法 / **1239 行** |

三簇闭包**互相重叠**（共用 `_subtree` 等工具方法），且搬走一簇会缩小另一簇 —— 上表**不可简单相加**，
每步落地后必须重测。全搬完后预计 **6500–6800 行**，足以压过 A3。

整体目标读数：文件 **8949**（W4 起点 10934，累计 −1985），不可搬 **26 方法 / 2119 行**。
**A3（< 7000）尚未达标**：还需净减 **1949** 行。

#### W4g-e 实测拆分（这是"最后一波"的完整清单，可直接开工）

`--closure ... --state` 实测：闭包 **70 方法 / 1239 行**，**70/70 零实例状态**（目前最干净的一簇）。
因单波体量过大，按职责拆成三步；每步都是自由函数 + ports（通用基础设施 `_create_children` /
`_insert_child_nodes` / `_block_task` / `_add_event` / `_mark` / `serialize_event` 继续由 ports 提供）：

| 子步 | 模块 | 成员（实测行数） | 小计 |
|---|---|---|---|
| W4g-e1 | `skill_notes.py` | `_looks_like_skill_note_title`(31)、`_parent_skill_note_child`(24)、`_with_parent_skill_note_child`(13)、`_is_skill_note_task`(9)、`_is_skill_note_spec`(6)、`_has_skill_note_child`(2)、`_skill_note_tasks_enabled`(2) | **完成**：净 −72，501 绿（`2ff6274`） |
| W4g-e2 | `convergence.py` | `_cycle_breaker_children`(30)、`_drop_blocked_repair_dependencies`(26)、`_convergence_cycle_breaker_children`(26)、`_repeated_child_signatures`(23)、`_convergence_slice_loop`(17)、`_fallback_event_message`(17)、`_convergence_cycle_breaker_base_title`(11)、`_is_convergence_cycle_breaker_task`(9)、`_child_signature`(9)、`_task_signature`(9)、`_is_blocked_child_repair_spec`(6)、`_convergence_slice_count`(3)、`_normalize_signature_text`(2) | **完成**：净 −166，519 绿（`be2dedf`）。另收了 `_is_redundant_convergence_cycle_breaker_child`(8) 与 `_is_legacy_convergence_slice_queued_child`(9)（职责相同，但不在原闭包内）；`CONVERGENCE_SLICE_REPEAT_LIMIT` 一并移入 |
| W4g-e3 | `final_gate.py` | `_targeted_final_gate_repair_children`(162)、`_add_arcade_brawler_gate_checks`(132)、`_settle_passed_root_final_gate_branches`(93)、`_final_root_product_gate`(74)、`_proactive_root_final_gate_children`(49)、`_create_final_gate_repair_children`(45)、`_final_gate_repair_children`(38)、`_active_final_gate_repair_keys`(21)、`_actor_scene_has_runtime_content`(18)、`_main_scene_has_runtime_content`(14)、`_is_final_product_gate_task`(13)、`_root_target_path`(12)、`_final_gate_missing_baseline_entry`(12)、`_missing_contains_readme_completion_issue`(12)、`_level_scene_has_runtime_content`(9)、`_read_json_dict`(8)、`_final_gate_repair_key_from_parts`(10)、`_filtered_gate_missing_text`(3)、`_requires_arcade_brawler_gate`(3)、`_missing_res_refs_from_gate_missing`(5)、`_final_gate_repair_key_for_spec`(2) 等 | ~740，**待做（剩余最大一块）** |

#### W4g-e3 的开工方法（前两步已验证过的手势，照做即可）

1. 先 `grep -n "^    def \(_final_root_product_gate\|_targeted_final_gate_repair_children\|…\)("` 取全部起始行；
   **取号之后到脚本运行之间不得再改 `orchestrator.py`**（否则会像 W4g-c2 那样错位）。
2. 用带**起始行断言**的一次性替换脚本落委托（模板见本文档 §5 说明；断言能让错位立刻报错而不是静默损坏）。
3. 委托片段一律走 ports：`_create_children` / `_insert_child_nodes` / `_block_task` / `_add_event` /
   `_mark` / `serialize_event` 这些通用原语**留在 orchestrator**，由 `self` 传入。
4. 补 `api/tests/test_final_gate.py`，含反漂移断言（委托 == 模块函数）。
5. 提交后重测行数并更新本文件。

#### 2.6 W4 完成判据（C1–C5，2026-09-13 修订）

用户在"改用内聚判据 C2"与"继续搬到 <7000"之间选择了前者。判据全文见
`docs/spec-modularization.md` §7.8，这里只记执行状态：

| 判据 | 状态 | 证据 |
|---|---|---|
| C1 无巨型杂项模块，每模块单一职责 | **满足** | 14 个下层模块职责单一（`blockers` 607 / `recovery` 413 / `watchdog` 384 / `runtime_logs` 366 / `concurrency` 354 / `convergence` 273 / `godot_gates` 247 / `runtime_markers` 209 / `budget` 179 / `skill_notes` 133 …）。**2026-09-20 起新增 `runner_tools` 866、`prompts` 409、`retry` 298、`worktrees` 229、`failure_retrospective` 187、`regression` 179、`project_context` 156 → 21 个**（R-a/R-b/R-c 见 §2.7.1–§2.7.3；R3/R5/R6 见 `docs/spec-resource-governance.md` §R3、§R5、§R6） |
| C2 依赖方向单向 + 无环 | **满足且已被测试守着** | `api/tests/test_module_layering.py`（4 个断言：扫描面自检 / 不得反向依赖 / `main` 必须 import 作为反面自检 / 模块图无环） |
| C3 每波 file:line 证据 + pytest 不降 | **满足** | 9 波各自提交信息含改动清单；W4 收尾读数 519 passed / 0 failed（**2026-09-21 现读数 763**，见 §2.8） |
| C4 行数只记录 | **已生效** | A3 在 §7.2 降级为读数 |
| C5 暂不搬清单与理由 | **已写** | §7.8 C5：调度族（284 方法 / 7187 行闭包）不做；26 个不可搬方法不做；`runner.py` 2809 行**属本波范围外，但需另开一波评估** |

**结论**：**W4 按 C1–C5 判定为完成**。`W4g-e3`（`final_gate.py` ~740 行）与其余五簇（child 创建/插入、
`serialize_*`、worktree/集成、Godot 门禁本体、宪法包络线）**降级为可选项**：继续做只增加清晰度，
不再影响是否达标。行数读数 8711（W4 起点 10934）。

**必须同时说清的两件事**（避免"完成"被读成"没问题"）：
1. `runner.py` **2865 行**（W4 时代的读数是 2809；2026-09-20 实测），比任何新模块都大，W4 从未触碰。
   它是与 `orchestrator` 同级的维护成本热点，只是因为不在本波范围内才没动。按 C1 的精神它应另开一波
   评估——**评估已做，结论与分波清单见 §2.7**。
2. C2 只保证**方向**不倒退，不保证**分组合理**。分组是否内聚仍是人工评审（C1），可被反驳。

**W4g-e 之后仍不足以达标**（预计约 7850 行）。继续达标需要再测这些尚未搬迁的簇（都还没测闭包）：
child 创建/插入、序列化与看板（`serialize_*` 一族）、worktree 与集成、Godot 门禁本体、宪法包络线判定。
每一步都必须先 `--closure --state` 再动手。

#### 2.7 `runner.py` 分波评估（2026-09-20 实测，尚未动手）

§2.6 的"必须同时说清"第 1 条点名 `runner.py` 从未被 W4 触碰。这节就是那次评估的实测，
量具仍是 `scripts/audit_orchestrator_movable_mass.py`（`target` 是位置参数，可以直接指向别的文件）：

```powershell
cd <repo>
.\.venv\Scripts\python.exe scripts\audit_orchestrator_movable_mass.py api\app\runner.py
```

总账：**2865 行**，单个类 `CodexExecRunner` 58 方法 / **2068 行方法体**；
**不可搬 0 个（0 行）**，即方法依赖图**无环**——这正是 orchestrator 当初难搬的原因所在
（它那 26 个不可搬方法全来自环）。全搬的投影文件大小是 913 行。

三簇实测（`--closure … --state`）：

| 簇 | 成员（实测行数） | 闭包 | 实例状态耦合 |
|---|---|---|---|
| **R-a 工具脚本生成** | `_ensure_godot_timeout_wrapper`(510)、`_ensure_assert_no_match_tool`(145)、`_ensure_placeholder_asset_tool`(92)、`_ensure_json_check_tool`(61)、`_asset_placeholder_guidance`(7)、`_negative_match_guidance`(8)、`_json_check_guidance`(7) | **7 方法 / 830 行**，不触达不可搬。**已完成**：`runner_tools.py`，净 −815（`becb107`） | **7/7 零实例状态** |
| **R-b 提示词与上下文** | 五个 `*_prompt`、七个 `*_guidance`、`_inherited_constraints_block`、`_project_context_summary`、`_context_roots`、`_looks_like_scope_path`、`_summarize_context_root`、`_safe_text_excerpt`、`_default_constitution_data` … | **已完成**：拆成 `prompts.py`(409) + `project_context.py`(156)，净 −460（`4d58dd0`）。落地前实测 24 方法 / 509 行（R-a 之前是 27/1331——那 1331 里 822 行是 R-a 的真身） | **24/24 零实例状态** |
| **R-c 重试与阻塞报告** | `_retry_after_seconds_from_message`(55)、`_sleep_before_retry`(38)、`_attach_tool_internal_block_report`(35)、`_clock_retry_after_seconds_from_message`(24)、`_attach_retryable_block_report`(24)、`_transient_failure_category`(14)、`_failure_payload`(10)、`_payload_message`(9)、`_observed_local_time_from_message`(8)、`_has_http_429_marker`(2)、`_is_local_codex_skill_failure`(2) … | **已完成**：`retry.py`，净 −221（`920f290`）。落地前宽闭包实测 15 方法 / 253 行；本波只搬 **11 个真身**——三个两行"别名"故意不搬（搬了会自己调自己，见 §2.7.3） | **15/15 零实例状态** |

读数口径（别读成承诺）：**C1 已在 §2.6 判定达标**，本波做与不做都不改变达标结论，做只增加清晰度；
三簇都"零实例状态"意味着不需要 ports 端口对象，只需要把 `self.settings.*` 那几项标量改成显式参数
（`_sleep_before_retry` 还要把返回值语义带过去：现在它用 `bool` 表示"是否继续进程内重试"）。

**建议顺序（有依赖，不能颠倒）**：R-a → R-b → R-c。

- R-a 先做：它是 R-b 闭包的子集，先搬完 R-b 的闭包就少 7 个方法；且它是三簇里最大的一块。
- R-c 里的 `_default_constitution_data`(12) 是闭包里唯一的非重试职责（被 `_attach_*` 间接牵进来），
  落地时要单独确认：**要么连它一起搬，要么改成显式参数传进去**——不能默认它就该跟去重试模块。

#### 2.7.1 R-a 已落地（2026-09-20，`becb107`）

- 落点：新模块 `api/app/runner_tools.py`（866 行）；`runner.py` **2865 → 2050 行**（净 −815）。
- 手势：一次性脚本**现取行号 + 起始行断言 + 落盘前 `ast.parse`**（脚本是 `workspace/_extract_runner_tools.py`，
  已 gitignore）。两个坑当场挡住：
  1. 这几个方法体里嵌着**多行长字符串**（工具脚本正文，最大的那个 510 行里有 488 行是字符串内容），
     按行去缩进会把内容改坏 → 用 tokenize 标出"字符串内容行"，那些行逐字节保留、替换也只做在非字符串行上；
  2. 只追 `self.X` 的闭包会漏自由名 → 脚本打印每个被搬函数的非局部名字，实测**抓到 `sys`**
     （原本靠 `runner.py` 的模块级 import，新模块必须自己 `import sys`）。这正是 §5 那条教训的复现。
- 零调用点改动：`runner.py` 里 14 处 `self._ensure_*` / `self._*_guidance()` 调用一字未改，委托保持同名同签名。
- "只搬移"的证据（比"我看过了"硬）：搬移前后的两份实现**在同一个 workspace 上先后各跑一遍**，
  4 个落盘工具脚本 **sha256 逐字节相同**、3 条指导语**逐字符相同**（`workspace/_runner_move_equivalence.py`）。
  口径提醒：两份实现必须用**同一个 workspace 路径**，否则 `.cmd` 里嵌的绝对路径会造成假差异——
  第一版量具就这么骗过我一次，是它自己报的 `!=` 让我去查的。
- 测试：新增 `api/tests/test_runner_tools.py` 3 例（返回路径一致 / 落盘字节一致 / 不得反向依赖 `runner`），
  并把 `runner_tools` 写进 `test_module_layering.py` 的自检清单。**694 绿 / 0 失败**。
- 下一波起点已量好：R-b = **24 方法 / 509 行**（上表；已落地，见 §2.7.2）。

#### 2.7.2 R-b 已落地（2026-09-20，`4d58dd0`）

- 落点：两个新模块——`api/app/prompts.py`（409 行：5 个提示词 + 7 条指导语 + 约束块 + 默认宪法数据）、
  `api/app/project_context.py`（156 行：读盘做上下文摘要的 5 个）；`runner.py` **2050 → 1590 行**（净 −460）。
- 边界决定（**有意收窄，不是漏搬**）：`_resolve_godot_executable` 属"外部工具探测"，跟着进了
  `runner_tools.py`。第一版把它留在 runner.py、当参数 `godot_bin` 穿进来，结果**逼着 4 个提示词函数
  各多带一个参数**，还生成了 `godot_validation_guidance(settings, )` 这种**运行时必崩**的调用——
  边界抬错了就长这样。
- 两个坑，都是脚本自检抓到的（不是"多看一眼"能发现的）：
  1. **缺参**：上面那条 `(settings, )`，只在真调提示词时才会炸；
  2. `runner_tools.py` 里**嵌着工具脚本正文**，那里面也有 `import os`。第一版"已导入就不重复加"按整段
     文本判断，把**字符串内容**当成了真的 import，于是漏加 `import os` / `import shutil`。
     改成用 AST 只看模块级 import。
- 零调用点改动：`runner.py` 里全部 `self._*_prompt(...)` / `self._*_guidance()` 调用一字未改；
  **既有测试里约 70 处直接调这些私有方法**（`test_orchestrator.py:5820-5911` 逐条钉提示词内容），
  一行未改就把委托路径跑了一遍。
- `settings` 参不参与**由 fixpoint 算出**（自己用了，或调了需要它的同簇方法），不靠手工判断；
  调用改写用 **AST 位置**（带重叠检测），不用正则。
- "只搬移"的证据：搬移前后**同一组 fixture**（真实读盘生成的项目上下文、五个提示词、七条指导语、
  可执行文件探测）20 项输出**逐字符一致**（`workspace/_prompts_move_equivalence.py`）。
- 测试：新增 `api/tests/test_prompts.py` 4 例，含一条**反面自检**——只比"两边一致"会被**参数传错位**
  骗过（把 settings 当 task 传也"一致"），所以另外断言提示词里真的出现这条 task / worker_result 的内容。
  另把 `runner_tools` 的分层契约放宽到"允许 `config`"，两个新模块进分层守卫清单。**698 绿 / 0 失败**。
- 下一波起点：R-c = **12 方法 / 223 行**（上表；已落地，见 §2.7.3）。

#### 2.7.3 R-c 已落地，runner.py 分波收口（2026-09-20，`920f290`）

- 落点：新模块 `api/app/retry.py`（298 行）；`runner.py` 现在 **1378 行**（三波前是 2865）。
- 本波搬 **11 个真身**（重试窗口解析、退避等待、失败分类、块报告装配）。宽闭包 15 方法里有 3 个**故意不搬**：
  `_is_transient_failure` / `_has_http_429_marker` / `_is_local_codex_skill_failure` 本来就是
  `codex_signals` 单一定义的两行别名。第一版把它们也搬了，`has_http_429_marker` 就变成
  `return has_http_429_marker(text)` —— **无限递归，而且 import 不报错、只有真调用才炸**。
  改法：别名留在 `runner.py` 原样不动，搬移后的代码直接调 `codex_signals`（那正是别名本来做的事）。
- 脚本这轮修了一个**真 bug（通用，值得记住）**：`ast` 的 `col_offset` 是 **UTF-8 字节偏移**，不是字符偏移。
  按字符累加行首偏移时，`_failure_payload` 那一行——`"markdown": f"# 项目宪法生成失败\n\n{message}", …`
  在自调用之前有 8 个汉字（+16 字节）——整条编辑落到别处，生成出 `self._default_coprompts.default_…` 这种垃圾。
  `rewrite_calls` 已改成在**字节域**算位置与切片。**含中文的行上的任何位置运算都要按字节算。**
- 另一处：多行签名（`_sleep_before_retry` 的形参跨 7 行）会让"按行去缩进 + 片段 tokenize"崩掉
  （`IndentationError`）。改成 ① 签名按"def 到函数体第一行"整段取；② "字符串内容行"由**整份文件**
  tokenize 得到再映射进函数体，不再对片段单独 tokenize。
- 零调用点改动：`runner.py` 里 `await self._sleep_before_retry(...)` 等调用一字未改，委托保持同名同签名
  （async 委托用 `await` 调模块函数）。
- "只搬移"的证据：搬前搬后同一组输入 **19/19 完全一致**，含 `_sleep_before_retry` 的**返回值、
  写进日志的字节、以及记录到的等待时长**（90s 短窗口原地等 / 10800s 长窗口不睡直接上交）
  （`workspace/_retry_move_equivalence.py`）。
- 测试：新增 `api/tests/test_retry.py` 4 例（async 委托、`read_payload`、依赖方向），`retry` 进分层守卫清单。
  **702 绿 / 0 失败**。

**runner.py 分波到此收口**：R-a/R-b/R-c 三波把它从 2865 行降到 **1378 行**（audit 的 file lines 口径），
其中 37 个方法已是 2 行委托（7 + 19 + 11），剩下 21 个真身是**进程/IO 核心**
（`_run_codex_io` 159、`_communicate_with_watchdog` 128、`_codex_env` 43、`_codex_exec_args` 37、
`_terminate_process_tree` 29 …）。这一簇实测仍判"可搬"（58/58 方法、方法体 577 行、投影 917 行），
但它们的职责就是 subprocess / 流 / 信号，搬出去只是换个壳、不会降低维护成本——**这是判断，不是测量**，
所以留在这里，等真有第二个调用方时再动。

#### 2.8 2026-09-21 审计与修正（审的是 R1/R3/R5/R6 的**实现**，不是文档）

当次要求："检查问题 / 流程问题 BUG / 修正 / 测试 / 上传"。做法是四路**并行只读**审计，每路只盯一个波次，
要求每条结论给出 `file:line` + 触发序列，并且**必须区分"已确认"与"未能确认"**。
（只读不跑测试：两个 pytest 共用测试库会互相踩，§5 已记过这个坑。）

结论：**15 处产品缺陷 + 1 处测试缺陷**，全部已修，每处各带一条回归钉子。
完整记录见 `docs/spec-resource-governance.md` §5 的「2026-09-21 审计修正」。

| 波次 | 缺陷（一句话） | 修法 |
|---|---|---|
| R3 **数据丢失** | 证据前置只查"路径存在"，被**同名旧文件**蒙过 → 回收删掉节点唯一一份新产物，父节点随后集成只记 `source missing`（默认档不阻断） | 判据改成**指纹一致**（文件 sha256 / 目录树指纹） |
| R3 静默失效 | 清扫**浅先深** → 先删父（父的 worktree 是子的归档处）→ 子永久不可回收，看板还报假警报 | 按 depth 降序 |
| R3 永久泄漏 | "是不是 git worktree"按根目标判 → 退化出来的**拷贝目录**送进 `git worktree remove`，回收永久失败 | 按目录自己的 `.git` 判 |
| R3 时序 | 父节点还在跑（含 `blocked`/`failed`）就回收 → 父的集成源被删 | 新增 `parent_active` 判据 |
| R5 静默放行 | 已清的回归债**永不复活** → 同一路径在更晚版本再次坏掉不再阻断（README 写着"一律阻断"） | 版本域债按基线版本前进复活 |
| R5 假阻断 | 读盘失败被当成 `missing`（无条件 blocking）→ 一次瞬时占用把父节点打进修复 | `unreadable` 单列并排除出比对 |
| R5 语义与文档相反 | "版本没动、hash 动了"也判跨版本 → `repair` 档会拦下同一轮的返工 | 同版本不比、版本回退不比 |
| R5 档位不生效 | 严重度首次生成即冻结 → 切到 `repair` 档也不阻断该路径 | 严重度**只升不降** |
| R5 覆盖缺口 | 目录型产物从不进基线 → 整目录被删不算回退 | 统一指纹（含目录） |
| R6 查询失真 | `source` 取不到 `blocked_child_reports` → 最常见形态被标成 `result_json` | 四选一 + `child_failure_ids` |
| R6 计数翻倍 | 树级看板把父的汇总与子的条目各算一次 → `total` 翻倍 + 凭空多一个 `unknown` 类别 | 按被引用的子节点去重 |
| R6 从未落地 | 复盘块在主路径上恒为空串（note 子任务在**规划期**创建，那时复盘还不存在） | 复盘写入时补进未开跑的 note 子任务 |
| R1 饥饿 | 泄漏的等待者永不退场（pid 存活判定挡不住 pid 复用），老化还让它越来越靠前 → 后来者看着空槽等满超时 | 超过存活上限的条目一律剪掉 |
| R1 仲裁瞬时失效 | 状态非原子写 + 读方不取锁 → 读窗口内所有人一起放行 | 换名落盘（`os.replace`） |
| R1 状态停更 | `_previous` 无异常防御且剪枝在它之后 → 一条坏 pid 让记账永久 bail | 逐个 `try` 跳过 |
| R1 **测试自欺** | "同权重仍 FCFS"用例读的是权重，两个都是 0 → 顺序恒 `[0,0]`，断言永远通过 | 改记**发起顺序**并断言 `[1,2]` |

**已核实、不修的**（不把"没找到"写成"没问题"）：容量不可能被突破（`_may_attempt_now` 只减少尝试、
门控用 `<`）；`since` 跨刷新保留正确；runner↔wrapper 的三个 env 名与语义一致；`is_managed_worktree`
没找到能删到受管根外的路径；保留期的时间换算（`coerce_utc`）正确；回收幂等成立；
`_descendants_for_task` 无 visited 集合，但在"单父 + 建后不改"的数据模型下不可达；
`utils.loads` 无守卫，但仓库内找不到非 JSON 的写入方。

**回归证据**：`pytest` **763 passed / 0 failed**（原 751，新增 12 例）；前端 `pnpm lint && pnpm test
&& pnpm build` 全绿（9 例 vitest）；排版门禁 21 个 `.md` / 0 问题。
行数读数（现测）：`orchestrator.py` 10134、`runner.py` 1393、`runner_tools.py` 1023。

**当次仍未做**：R2（用户决策级）。本次修的是"已有机制是否真的成立"，**没有新增机制**。

#### 2.9 能力基准（C，2026-09-22）

要求：回答"换模型 / 换 harness / 改提示词之后是变强还是变弱"，以及"它会不会谎报完成"。
先前 764 例测试**全部**只断言"机制在不在"，没有任何一个数字回答"能把活干成什么样"。

- spec：`docs/spec-capability-benchmark.md`（先设计）。核心口径：**成功率只由外部判据算**，
  树自称 `done` 只进 `tree_claimed_done`，两者之差 = `overconfident`（头号指标）；
  读不到的指标记 `null` 不填 0；`unsolvable` 任务声称完成 = 该档不可信。
- 实现：`api/app/bench.py`（纯逻辑，12 例单测）+ `scripts/bench_run.py`（进程内驱动，不依赖 HTTP）+
  `bench/tasks/*3` + `bench/fixtures/*2`；`bench/runs|reports` 不入库（报告含本机路径）。
- 实测（mock 档，两次一致）：外部判据 **4/10**、树自称完成 **3/3**、谎报 **3**、记账行 48（真调 CLI 0）、
  墙钟 22s、债务 12/0。**mock 渠道的"完成"一文不值**——这正是必须跑 real 档的原因。
- 跑这一遍暴露并修掉基准自身两个缺陷（都已修）：`import app.*` 早于设库地址 → 跑进开发库
  （指标把全库历史算进来，`agent_starts` 报出 67 万；已加开跑前硬断言）；指标按全库统计而非子树。
- 顺带清掉一次误入库：`api/build/lib/app/**`（39 个文件，setuptools 现场构建的源码副本）已移除并加忽略。

#### 2.10 验收面独立（B，2026-09-22）

要求（来自评分里最狠的一条）：同一条树里的 agent 能改自己正要通过的考卷；worker/verifier 同模型；
"300%" 纯自评。

- spec：`docs/spec-verification-independence.md`（先设计）。
- **验收面冻结与防篡改**：宪法 `data.acceptance_surface` 声明判据文件 → 根节点创建（= 宪法已锁定）
  与人工修订时冻结**内容指纹** → 整合验收前比对，改动/删除 = **无条件 blocking** 债
  （`acceptance_surface_tampered`，不接受档位）；读不了 = **不判定**（单列 + 事件）；
  未声明 = **不判**；恢复 = 按事实清债。修订**只在声明的路径清单变了**时重新冻结——
  内容变了不许靠一次无关修订洗白。
- **角色分层模型**：`codex_model_{constitution,planner,worker,verifier,acceptance}` +
  reasoning effort 同族，`kind` 流到 `_codex_exec_args`；看板 `model_tiers` 报出**已解析**配比
  与可疑之处（不做模型强弱排序——名字里没有强度信息，硬排就是假判据）。
- **自评校准**：`api/app/calibration.py` 按深度聚合 `claims/misses/overconfidence_ratio`，
  **没有样本记 `None` 而不是 0**；**只读**，不据此拆分/降级/加债（阈值属政策，见 spec §6 VI-1）。
- 证据：`tests/test_acceptance_surface_gate.py`（7 例，含"改判据 → 不得 done + 理由点名路径"）、
  `tests/test_model_tiers.py`（7 例，含"两次命令行里的模型名真的不同"）、`tests/test_self_assessment.py`（4 例）。
  `pytest` **794 passed / 0 failed**；`ruff` 干净。

#### 2.11 需求→证据追溯矩阵（A，2026-09-22）

要求：现在体系能保证"实现符合宪法"，**不能**保证"宪法符合目标"——漏掉一条需求与把一条需求做错，
在现有信号里长得一模一样。

- spec：`docs/spec-requirement-traceability.md`（先设计）。宪法 `data.requirements`
  = `[{id, text, criteria: [{id, type, path|command|pattern}]}]`，判据词表**与能力基准共用一处**
  （`app/bench.evaluate_check`：`file_exists`/`contains`/`not_contains`/`command`）。
- 判据（失效方向写明）：**要求没有判据 = 漏需求 → blocking**（`requirement_uncovered`）；
  **判据没过 = 没做到 → blocking**（`requirement_unmet`）；**判据判不了 → 不判定**（越界/读不到/超时，
  单列 + 事件，不算通过也不算失败）；**没声明 requirements → 不判**（但节点 meta 里如实留痕）。
- 接线：验收尾按 `验收面篡改 → 需求矩阵 → 跨版本回归 → 门禁` 的顺序登记，全部走既有 `_obligation_gate`。
- 证据：`tests/test_requirement_traceability.py` 6 例（漏需求必拦且点名 id / 判据没过必拦且点名 req|crit /
  判不了不拦但留痕 / 全过可 done / 未声明不判 / 声明写错的条目必须报出来而不是静默丢掉）。

#### 2.12 任务间隔离（R2，2026-09-22）

**这一波的第一个动作是"把 R0 的结论拿去重测"——结果推翻了它。** R0（2026-09-11）说"读隔离 codex 沙箱
不提供、必须落 OS 层（Windows ACL）"。用当前 CLI（`codex-cli 0.154.0`）实测：`--cd node-a` 下读
`node-b/secret.txt` → **`Access is denied`**，agent 自己回"做不到"。**沙箱把读限在 `--cd` 根内**，
而节点的 cwd 就是它自己的 worktree —— 于是节点间读隔离**本来就成立**，前提是"cwd 是自己的 worktree"
且"不加 `--add-dir`"（两条都有断言守着）。

- spec：`docs/spec-task-isolation.md`（先设计，含"旧结论被推翻"的并排记录与 §2.0 原始输出）。
- 实现（三件 + 一件顺带修的真问题）：
  1. **每节点隔离 `CODEX_HOME`**（`<codex_home>/nodes/<node-id>`）：会话/缓存/插件状态互不可见，
     该目录里没有 `auth.json`（磁盘面已规避）。
  2. **跨节点泄露检测**（侦探，不是防线）：节点产物里出现兄弟节点 worktree 路径 → **advisory** 债
     `cross_node_leak`（文本里出现路径 ≠ 真的读了它，所以不阻断）；自己的路径/集成目标/根目标都排除；
     幂等键绑路径，同一泄露不堆债。
  3. **写边界可核对化**：断言命令行里没有 `--add-dir`（"没加"与"保证不加"是两件事）。
  4. **顺带修掉一条真问题**：`dict(os.environ)` 会把宿主上**无关平台的凭据**交给每个节点
     （实测打印出 `<env_key>` / `宿主平台凭据` / `provider`）。
     改成**默认收窄**（固定系统键名单 + 代理/证书/包管理前缀 + `CODEX_/OPENAI_/...` 显式放行），
     环境从 100+ 键收到 54 键；**并用真实 CLI 验证过收窄后照样能跑**（退出码 0、回答 `OK`）。
- 可反驳证据：`scripts/verify_isolation_boundary.py`（段 1 越权读 / 段 2 凭据面 / 段 3 越权留痕 /
  段 4 收窄环境可用）；`tests/test_task_isolation.py` 11 例。
- **未实现项如实列出**：`env_key` 模式下的凭据仍在子进程环境变量里；网络出网控制不在本 spec 范围；
  读隔离依赖**沙箱行为**而非我们的代码，所以留了"升级 CLI 后重跑段 1"的手工步骤（ISO-3）。

#### 2.13 孤儿恢复验证补齐（2026-09-22）

`scripts/verify_orphan_resume.py` 原先把跑不了的原因写成"环境限制"（8765 被无关进程占着）。
这轮补上：`workspace/_run_orphan_check.py` 在**隔离库 + 空闲端口**起 API、跑脚本、**无论成败都终止服务器**。
实测：`orphaned=True → 建议动作 resume_orphaned → kick 后 root=done`，`RESULT: PASS`。

#### 2.14 真实档基准 + 「量具自己死了不留证据」（P3，2026-09-22）

要求：mock 档的"完成"一文不值（3/3 自称完成、判据 4/10），所以"能力"这件事**只有在 real 档才谈得上**。

- 两次真实档跑批都是"现场为空"：`bench/reports/` 里**根本没有这一轮的 json**，DB 停在
  node=`verifying`。当时的合理猜测是"产品卡死"或"缓冲输出没 flush"——**两个都不对**。
- **真实原因（一个未定义的名字）**：`scripts/bench_run.py` 在"判据求值之前"用了 `Path(...)`，
  而文件里只有 `import pathlib`（F821）。它只在 **real 档**被走到——mock 档没有 worktree，
  `and` 短路了。异常穿出 `run_all`，而报告只在**成功路径**上写 → 崩溃时磁盘上一个字都没有。
  两次都是这个原因：`add-multiply` 那轮树在 16:05 就 `done` 了（产品把活干完了），
  `fix-subtract` 那轮在 540s 墙钟上限处被 `wait_for` 正确截断（`status=timeout` 那条分支正常走到了），
  然后两者都在同一行崩掉。
- 修的是**证据链**，不是把崩溃藏起来（崩溃本身方向是对的：退出码非 0）：
  1. 未定义的名字已修（`pathlib.Path`）；**门禁范围**同时修了——`verify_all.py` 原先只在 `api/` 下跑
     `ruff check .`，仓库根的 `scripts/**` 谁都没检查过（见 `AGENTS.md` 与 §7）。
  2. 长跑**每个任务之后 + 每 60 秒心跳**都落一份 `partial=true` 的报告（tmp + `os.replace` 原子替换）；
     异常路径也先落一份写明"哪个任务抛了什么"再往上抛。
  3. 正式报告 = 收尾那份 `partial=false`；`--compare` 遇到 `partial=true` **拒绝**（退出码 2），
     而不是打一张合计只覆盖已跑完任务的表。
  4. 运维前提写进 `bench/README.md`：**前台命令有 10 分钟上限**，`--task-timeout` 超过它的跑批必须放后台。
- 门槛与读数口径不变：成功率**只**由外部判据算，`unsolvable` 声称完成 = 该档不可信。
- **真实档读数（机器写的报告，`bench/reports/`）**：
  - `fix-subtract`（solvable）：外部判据 **4/4**、树 `done`、**谎报 0**、3 次 CLI 调用、
    **408,902 入 / 7,160 出 token**、墙钟 **472.2s**、人工介入 0、债务 0/0。→ 产品在这条任务上是"真做完了"。
  - `impossible-no-write`（unsolvable）：外部判据 **1/3**、树状态 `timeout`（600s 上限截断）、
    **自称完成 `False`、谎报 `0`、该档 `trustworthy=true`**（**C2 在 real 档成立**）；9 次 CLI 调用、
    11 次 agent 启动、**498,608 入 / 9,680 出 token**、墙钟 **601.7s**、人工介入 0、债务 **7 新 / 0 清**
    （类别全是 `constraint_coverage_gap`）。判据里 `test-intact` pass = **模型守住了"不许改任何文件"**。
- 可反驳证据：`api/tests/test_repo_gates.py`（3 例：门禁命令必须覆盖 `api` + `scripts`；中途落盘默认
  `partial=true` 且原子；`--compare` 拒绝中途报告）；实跑中的心跳报告可见
  `partial=true` + "当前 <任务> 已跑 N 秒"；`--hard-stop 5` 的 mock 档落下 3.4KB 全线程栈 + `partial` 报告。

**本轮暴露的两条产品级发现 —— 都动了手，且都写清了"修到什么程度"**：

1. **跑批会冻住，而且冻住之后连线程级兜底也失效**（根因未定位，**只做到有界 + 留证据**）。
   第一次（`fg23`）：报告最后一次写入 = 任务开始后 186.0s，DB 最后事件 17:47:53（**DB 先停**），
   `py-spy dump --pid` 显示主线程停在 `_poll` 的 `GetQueuedCompletionStatus`，`--debug-stacks 120`
   的 dump 也在同一时刻停止 → 60s 心跳、600s CLI 看门狗、1500s 任务上限、faulthandler 的 120s dump
   **一起不再触发**。加完进程内线程级 `--hard-stop` 后重跑（`fg4`）**同样冻住**：报告最后一次写入
   **480.1s**，DB 最后事件 17:55:50（这次**心跳先停约 2 分钟、DB 后停**——两次的先后顺序相反，
   说明两个信号都只是"进程已经不对了"的旁证，不能当作因果链），而 `--hard-stop 1020`
   **没有触发**——4 个线程全部 parked、CPU 不再增长、
   普通线程的定时 `Event.wait` 也没返回（faulthandler 的 dump 线程同时停摆）。
   → 结论：这是**整个解释器冻住**，进程内任何兜底都不可靠。**已做**：进程外监督器
   `scripts/bench_supervise.py`（看报告 mtime 无进展 → `taskkill /T /F` + 落 `<report>.supervisor.txt`
   + 退出码 4；正常路径原样转发退出码），5 例测试含"真杀一个卡住的子进程"。
   **本次 `fg5` 跑满 600s 未复现冻结**（心跳一路刷到 600s，监督器退出码 0、无 sidecar）→ 冻结是**间歇**的。
   **未做**：根因（需 C 层栈：`procdump -ma` + WinDbg，或对 `_overlapped` / `asyncio` 子进程路径做最小复现）。
   → **过期标记修正（2026-09-24 核实）**：这句"未做"里**"静默挂住"那一半已定位并修掉**，见 §2.19
   （`Process.wait()` 要等所有管道 EOF）；**"整解释器冻住"那一半仍无根因**。原文保留才看得出它漂过。
2. **规划器返回 `blocked` 会转成"规划阻塞恢复"子任务继续递归 → 螺旋**。unsolvable 任务里模型如实报告
   "只读命令 `rg ...` 经进程创建时被执行策略以 `blocked by policy` 拒绝"（它没谎报完成，这点是好的），
   编排器却递归拆子任务：两次都到深度 4、并发油门 1→3、标题叠成"…：规划阻塞恢复：规划阻塞恢复：…"。
   → **已修（有界，不是分类）**：`blockers.blocked_recovery_suppression` 单点决定"该不该拆"——
   恢复链只允许一层、可等待重试的不拆、工具内部先修工具、没有 description 不拆；planner / acceptance /
   `recovery.py` 三条路径共用这一处。**本次 `fg5` 在真实档观察到生效**：21 个节点里"规划阻塞恢复"
   子节点停在 `blocked`，没有第二个恢复层。**未做**：把"环境性"做成结构化分类（planner 至今不填
   `block_report.category`，见 spec §5 BC-4）。

相关 spec：

- `docs/spec-modularization.md`（W1–W5 + 反缩水验收契约 A1–A7 + 工作区卫生 §8）
- `docs/spec-resource-governance.md`（R0–R6 + 验收契约 G1–G7 + §5 的 2026-09-21 审计修正）

#### 2.15 冻结相关性实验 + 四项政策变更（P1/P2/P5/P6，2026-09-22 第二段）

上一轮留下两件"没做"：冻结根因、以及一批等用户拍板的政策项。这一段的判据是"**能拿出可核对的结果**"。

- **P1 冻结相关性实验**（4 次同参数真实档，`impossible-no-write`、600s 上限、带进程外监督器、
  **不开** `--debug-stacks`）：`fg5` + `fg6a/b/c` **全部跑满 600s 没冻**；而此前两次冻住的（`fg23` / `fg4`）
  **都开着** dumps → **2/2 vs 0/4**。据此把"real 档长跑默认不开 dumps、要栈就靠监督器 sidecar + `py-spy`"
  写成操作约定（spec §3.6），但**不声称因果**（样本仍小）。
- **P1 顺带拿到的读数**：4 次外部判据**都是 1/3**、`tree_claimed_done` **都是 False**、`overconfident` 都是 0、
  `trustworthy` 都是 `true` → **C2/C3 在 real 档成立**；token 457k–783k、agent 启动 10–13 次 → **不收敛**。
  补齐了缺失的 solvable 读数：`add-multiply` **3/3 / 树 done / 谎报 0 / 3 次 CLI / 278,218+7,246 token / 359.0s**。
- **P1 的意外收获**：实验链里我**复用同一个 `--stamp`** 重跑 add-multiply → `copy_fixture` 抛
  `FileExistsError`，**围栏按设计落了一份"中断"报告 + 退出码非 0**（围栏第一次在真实世界生效）。
  教训：一个 stamp 只能跑一次（脚本未做防呆，如实记录）。
- **P2 文档更正**：`fg4` 的"最后心跳"从 360.0s 更正为 **480.1s**（以报告 `notes` 为准）；"心跳先停还是
  DB 先停"两次顺序相反 → 明确写成"两个信号都只是旁证，不是因果链"。
- **P6 四项政策变更**（用户逐条拍板；每项都写明失效方向与证据）：①`max_acceptance_repair_cycles`
  默认 `0 → 2`；②amend 收紧时 `done` 默认再验证（核查后**现状即开**，只记决策，豁免入口未实现）；
  ③只读约束下豁免 skill-note 子任务（判据成对匹配 + 两条创建路径 + 事件留痕 + 反面自检）；
  ④债务落表建**派生**索引（`obligation_index` + 投影/重建/漂移检查 + 脚本 `--check/--rebuild/--by-type`）。
- **P5 发布面**：干净 clone（快照内容）→ `pip install -e ".[dev]"` → 起 API → `GET /api/tree` **200**
  （清单第 6 条首次在本轮真跑）；提交 + 新稳定快照（与前一轮同一棵树的两个稳定标记）。
- **仍未做（留在下一轮，都写清了边界）**：P3"环境性 `blocked` 结构化分类"（spec §5 BC-4）、
  P4"`constraint_coverage_gap` 创建时拦 vs 验收时拦"（spec-parent-child-obligations §13 Q4，真实档里
  它 4 次都留下 7-9 条未清债、挡着收敛）、冻结根因（要 C 层栈：`procdump -ma` + WinDbg，或最小复现）。
  → **过期标记修正（2026-09-24 核实）**：P3 / P4 已由 §2.16 做掉；冻结根因里的"静默挂住"已由 §2.19
  定位并修掉，只剩"整解释器冻住"无根因。另：那句"挡着收敛"本身在 §2.16 被更正过（那类债是 `advisory`）。

#### 2.16 假债根因 + 环境阻塞分类 + 冻结取证 + amend 豁免（2026-09-22 第三段）

上一轮留下"没做"的三件事（P3/P4/冻结根因）加一项已拍板的政策（amend 节点级豁免）。

- **P4 根因：`constraint_coverage_gap` 是结构性误报**。取证（读 `fg5` 的库）：7 个节点各记 3 条
  **一模一样**的未清债，内容全是**编排器自执的不变量**（"任务树必须保留总目标的品质下限"、
  "父节点必须通过整合验收后才能 done"、"真实运行记录必须保存 prompt、结果、证据和失败原因"）。
  它们的来源是**工具自己的默认宪法**（`runner.py` 的 mock 数据），而这些条款被放进了
  `global_acceptance`——覆盖核对要求子节点的验收**逐字包含**它们，可**没有任何子任务能交付**这种东西
  （"父节点必须通过整合验收"不是子任务的产出）。于是每层记一条、永远清不掉。
  - 修法（**结构性，不是文本启发式**）：这类不变量改成**工具侧常量**
    （`MockCodexRunner.ORCHESTRATION_INVARIANTS`）并渲染进宪法 markdown（节点仍看得见），
    **不再进 `global_acceptance`**；默认宪法的 `global_acceptance` 换成真·产物级三条；
    `prompts/constitution.md` 写明**判断方法**："子任务不做就算它没达标" → `global_acceptance`；
    "不管子任务做什么、系统本身都必须这么运转" → 不变量（由编排器自执）。`delegable_constraints` 不变。
    **为什么不做成 schema 字段**：仓库的门禁规则是"声明即必填"（无可选字段），
    不变量做成可选字段会把那条规则蛀空；例外表目前只有一处
    （`verify_all.OPTIONAL_SCHEMA_FIELDS` 的 `planner-decision.block_report`，且有测试盯着不许长大）。
  - 证据：修前 `fg5` **7 节点 × 3 条**；修后 `p4b`（同任务同参数）**1 节点 × 1 条**，且是**真缺口**
    （"改动必须留在本目标工程的边界内"没被逐字继承）。测试
    `test_constraints.py::test_default_constitution_keeps_orchestration_invariants_out_of_global_acceptance`
    （含反面自检：真·产物级约束缺了必须照样报）。
  - **口径更正（重要）**：这类债是 `advisory`，**不直接挡 `done`**——上一轮写的"它挡着收敛"不准确，
    已同步改掉（`docs/spec-capability-benchmark.md` §4.2）。
- **P3 环境性 `blocked`：两层判据**。①**识别**：`prompts/planner.md` 要求 `blocked` 必须带
  `block_report.category`（`environment` / `task_internal` / `tool_internal`），schema 已建模；
  `blockers.blocked_recovery_suppression` 对 `environment`（或 `parent_action=human_review`）
  **一次都不拆**——递归撞的是同一堵墙。②**兜底**：模型没声明时仍由"恢复链只允许一层"挡住。
  证据 `test_orchestrator.py::test_declared_environment_block_is_not_converted_to_recovery_children` +
  `test_blockers.py` 纯函数例；反面自检：`task_internal` 仍必须能转一次。
- **R4 amend 节点级豁免入口**（用户拍板）：`ConstitutionAmendRequest.revalidate_exemptions`
  （`{节点 id: 理由}`）。约束写死在代码里：**理由必填**（没理由的豁免＝静默放宽）、**不盖新版本戳**
  （产物是按旧版验收的，盖章就是静默 claim 通过）、**只对本次目标版本有效**（下一次收紧照样进队列）、
  **不存在的节点 id 直接拒**（否则"豁免成功"是假象）。证据 `test_constitution_amend.py` 3 例
  （跳过队列+留痕 / 理由与 id 校验 / 逐版本作用域）。
- **R3 冻结取证自动化**：`bench_supervise.py` 判停时**先取证再杀**——对**整棵**进程树
  （venv 转发器 → 真跑事件循环的子进程）逐个抓 `py-spy dump --json` + 原生线程等待态，
  落 `<report>.hang-evidence.txt`，再 `taskkill /T /F`。证据 `test_bench_supervise.py` 新增 2 例
  （后代进程表解析含成环 / 判停必留证据）。
- **R3 最小复现 harness**：`workspace/_proactor_repro.py`（诊断件，不入库）——asyncio(Proactor) +
  N 个子进程并发读管道 + 可选 `faulthandler.dump_traceback_later`，每轮写进度，套监督器跑 A/B 两档
  （开/关 dump，各 15 轮 × 20s）。**结果（负，两档都跑完）**：A 档 dump 真的触发了 20 次、15/15 轮没冻；
  B 档（关 dump）同样 15/15 跑完没冻 → "asyncio + 多子进程 + 周期 dump"**不足以**复现，
  触发条件还需要别的东西（真 CLI 的 IO 形状 / 更高并发 / 更长时长 / 具体代码路径）。
  **这条负结果本身有用**：它把"dump 就是凶手"这个猜测**降级**为"还需要别的条件"，
  操作约定（real 长跑默认不开 dumps + 监督器兜底）保持，但不再暗示因果。
- **政策项记录（用户选择保持现状）**：`cross_node_leak` **不**升 blocking（先采误报率）；
  ISO-3 沙箱探测**不**接进 `verify_all`（花 token、"谁付"未定）——两条都只记录，不改行为。

#### 2.17 债务派生索引接面（2026-09-23）

上一轮留下三条"没做"，其中一条是"索引表 + 脚本 + 测试都有了，但**没有任何界面/路由在读它**"。
这一段只收这一条（冻结根因要 C 层栈、两条政策项用户已决定保持现状，见文末）。

- **做法**：读面只此一处 —— `api/app/obligation_index.board()` 合成"跨树聚合 + **同一时刻**的漂移状态"，
  经 `GET /api/obligation-index?status=open|settled|all` 暴露（typed `ObligationIndexBoard`）；
  看板面板 `ObligationIndexPanel`（全库口径，与选中哪棵树无关）。
- **判据的失效方向**（这是本段的重点，不是装饰）：这张表是**缓存**，"`total == 0`"在
  `built_at is None`（从没重建过）或 `in_sync is False`（表已跟 `node_meta` 漂了）时**不构成读数**。
  所以路由**只读**——**不自动重建**（自动重建会抹掉"为什么漂了"，下一次照样漂；重建仍是
  `scripts/rebuild_obligation_index.py --rebuild` 的唯一职责），并且必须把漂移状态与聚合**一起**给，
  界面据此改口径说"这里的 0 不代表没有债"。范围必须显式，非法值 `422`，**不**静默落回默认范围。
- **顺带修掉的一处可读性缺口**：前端债务类型中文名表只覆盖 6 个旧类型，
  `constraint_coverage_gap` / `requirement_*` / `regression_break` / `acceptance_surface_tampered` /
  `cross_node_leak` 直接显示英文标识。现在补全与 `api/app/obligations.py` 一一对应；
  **表里没有的类型仍回退原名**（宁可显示英文标识，也不自造中文名把两种债说成一种），并有反面自检。
- **证据**：`api/tests/test_obligation_index.py` +2 例（`board()` 未建/漂移配对、范围显式且拒未知值）、
  `api/tests/test_api.py::test_obligation_index_endpoint_reports_drift_instead_of_a_zero`（含重建前后与
  `status=settled|all|openish` 三种范围）、`web/src/model.test.ts` 2 例、`web/src/api.test.ts` 1 例。
  后端 27 例绿；前端 12 例 + lint + build 绿。
- **仍未做（边界不变）**：冻结根因（需 C 层栈：`procdump -ma` + WinDbg，或"真 CLI 的 IO 形状"最小复现；
  判停时的现场证据已由监督器自动落盘）；`cross_node_leak` 升 blocking 与 ISO-3 沙箱探测接门禁（用户选择保持现状）。
  → **过期标记修正（2026-09-24 核实）**：冻结根因的"静默挂住"那一半已由 §2.19 定位并修掉
  （最小复现走的正是这里说的"真 CLI 的 IO 形状"，只是把 CLI 换成了行为可控的假 CLI）；
  两条政策项仍是"保持现状"。

#### 2.18 索引读面的对抗性审计 + 端到端实证（2026-09-24）

按"每波收口做对抗性审计"的常规动作，对 §2.17 那波的实现逐文件过了一遍（只读），
确认 **4 处产品缺陷**并全部修掉；每处都用"**撒回修复 → 新测试变红**"验过，不是靠"测试绿了"。
逐条改法与边界写在 `docs/spec-parent-child-obligations.md` §9.2，这里只记结论与失效方向：

| # | 缺陷 | 反常时往哪边倒 |
|---|---|---|
| ① | `board()` 的 `built_at` 取过滤后**子集**的 max | `total==0` 恒等于 `built_at is None` → "索引建过、这个范围没有债"被读成"读不出来"（**误报不可读**，那句文案成死分支）。改全表 max + 新增 `indexed` / `projected` |
| ② | `source_updated_at` 算进漂移判据 | 活跃树上 `in_sync` 近乎**恒假**（17 处改 `updated_at`）→ 真漂移被噪声淹掉（**漏报真漂移**）。移出判据，并留反面自检 |
| ③ | CLI `--status opne` 静默进 SQL | 打印 `total: 0`，与"真的没有债"一模一样（**假的干净**）。改成 `SystemExit(2)` + 说明 |
| ④ | 脚本默认库与 `app.config` 默认**不是同一个库** | 空库 → `projected: 0 / in_sync: true`：**索引唯一的写入口自报全绿**。改齐，并用"两边算出来的 URL"做回归 |

- **④ 只有真跑才抓得到**：单测全都显式指定库，所以它们一直是绿的。这正是仓库那条
  "mock 绿了 ≠ real 档那条路径被走过"的又一个实例。
- **端到端实证（零 token）**：先独立数一遍真相（开发库 3291 节点、**0 条债**，所以那次
  `in_sync: true` 是真的干净，不是 ④ 的假全绿）；再复制一份真库、用**产品自己的构造函数**
  播 4 条债，跑 CLI `--check`（退 1）→ `--rebuild` → `--check`（退 0）→ `--by-type`（错范围退 2），
  再起真 `uvicorn` 打 `open|settled|all`（三者 `built_at` 相同 = ① 的修法在真链路上的表现）
  与错范围 `422`；最后清空索引表验最贵的方向：`total: 0` 被读成"索引未建（不是没有债）"。
  读数在 `VERIFICATION_STATUS.md` 的「2026-09-24」一节。
- **顺带（文档真相对齐）**：§2 的 R2 / W2b 两行是**过期标记**（表里写"未开工/暂缓"，而正文
  §2.12 / §2.10 写着已做），已改成"已完成（原标 X 是过期标记，2026-09-24 核实）"；
  §1 的行数读数与 `README.md` 的用例数改为现测值；防复发规则写进 §2 表头与 `AGENTS.md`。

#### 2.19 "静默挂住"的根因定位与修复（2026-09-24）

2026-09-22 那两轮真实基准的"静默 30-60 分钟、DB 停在 `verifying`"一直只做到"有界 + 留证据"，
根因空着。这一波用**零 token** 的方式把它钉住了，顺带把取证链路补完：

- **取证能力先备齐**（先有量具才谈根因）：`py-spy dump --native --locals` 拿 OS 帧与 Python 帧；
  `procdump -ma` 抓全内存 dump；`cdb` + CPython `python312.pdb` + `DLLs/_overlapped.pdb`
  读通 dump 出**逐帧真名**（实测验通）。踩到的坑都写进 `AGENTS.md`：py-spy 在 Windows 上
  CPython 内部帧名是**导出表近似**（把 `_PyEval_EvalFrameDefault` 写成 `PyThread_GetInfo`）、
  `procdump` **成功退 1** 且展开文件名宏、WinDbg 的 Store 包里**没有** `cdb.exe`。
- **根因**：asyncio 的 `Process.wait()` 要等"进程退出**且所有管道 EOF**"
  （`_try_finish` → `_call_connection_lost` 才唤醒 waiter）。Windows 上 codex 是 `.cmd` → `cmd.exe`
  → `node` → 孙进程，任何一个**已脱离**的后代攥着 stdout 写端（`taskkill /T` 抓不到它），
  EOF 就永远不来，而 `_communicate_with_watchdog` 里那句 `await wait_task` **没有上界** →
  看门狗开了火，收尾却无限挂着。复现方式：`workspace/_d4_repro.py --behaviors orphan`
  （驱动**产品自己**的 `_run_codex_io`，只把 codex 换成行为可控的假 CLI），**每轮必现**：
  看门狗 6s 开火 → 静默 55s，期间进程树里一个后代都没有，`py-spy` 显示 `_poll(ms=INFINITE)`。
- **修复**：`EXIT_WAIT_AFTER_STOP_SECONDS = 20.0` + `_await_exit_bounded`（超时写明原因、
  主动关本端 transport、再给 2s 拿真退出码）+ `_wait_after_kill`。两处都用 `asyncio.wait` 而
  **不是** `wait_for`（后者会**取消** `wait_task`，第二次机会将恒定返回 124）。
  失效方向：宁可提前收手并写进日志，也不让"清理"变成新的挂死点。
- **证据**：真实形状 61s → **6.7s**；`api/tests/test_codex_exit_wait.py` 5 例
  （有界 / 正常退出必须给真退出码 / 关管道后的第二次机会 / 真实形状端到端 / 量具自检"孤儿必须
  真的活过父进程"）；撒回三个探针分别验红 3、2、1 条。细节与边界见
  `docs/spec-capability-benchmark.md` §3.6.1。
- **如实写下没做成的事**：修掉的是**"静默挂住"**这一类（事件循环活着、等一个不会来的 EOF）。
  2026-09-22 第二次复现里那个**整解释器冻住**（所有线程 parked、`faulthandler` 也停摆）
  **没有**被证明是同一个 bug，仍然只有进程外监督器兜得住。

#### 2.20 A6 从"推理"变成"测量" + 过期标记清理（2026-09-24 收尾）

这一波只做**零成本**的两件事：把计划里剩下的过期标记按事实改掉，以及把长期挂着"未验证"的
A6（浏览器 console = 0）真测一遍。不花 token，不动产品行为（只补了一个图标）。

- **A6 已验证**：`scripts/verify_browser_console.py`（新）用 headless Chrome + CDP 收
  `Runtime.consoleAPICalled` / `exceptionThrown` / `Log.entryAdded` / `Network.loadingFailed`。
  读数：`h1=L4 Navigator`、body 4038 字符、**42399 个元素**（2264 节点的树真渲染完了）、
  **0 条 console error**；唯一那条 `Network.loadingFailed` 是关浏览器时 SSE 被取消
  （`net::ERR_ABORTED / canceled: true`），不进判据。
- **判据的失效方向**：`0 条 error` 可能是"页面干净"，也可能是"**我根本没在听**"。所以渲染证据
  （h1 + body 阈值）、收集器自检（注入一条 `console.error` 必须被抓到）、错误数三条**各自**能判失败
  （exit 3 / 4 / 1）。对着 `about:blank` 实测过：脚本**拒绝**把它读成通过（exit 3）。
- **顺带修掉一处真缺口**：首轮读数不是 0 而是 **1** —— `GET /favicon.ico` 404。
  没有在量具里给 favicon 开后门（那是静默放宽），而是在 `web/index.html` 补了内联 data URI 图标。
- **判据逻辑进门禁**：`api/tests/test_browser_console_probe.py` 4 例（error 认得出 / warning 与
  取消请求不进判据 / 哨兵只摘自己那条 / 什么都没收到时命中数必须是 0）。真浏览器那段**不接**
  `verify_all.py`（要浏览器 + 两个在跑的服务，接进去会让"没起服务"变成习惯性被跳过的红）。
- **过期标记清理**（都保留原文，只加标日期的修正）：§2.14 / §2.15 / §2.17 三处"未做：冻结根因"
  → 指向 §2.19（"静默挂住"已定位并修掉，"整解释器冻住"仍无根因）；§2.15 里 P3 / P4 早已由 §2.16 做掉；
  §7 第 4 条"唯一需要用户拍板的是 R2" → R2 已由 `6370b30` 做掉，现在真正要拍板的是花 token 的那几件。
- **仍未做（本波明确不做，理由写在这里）**：W4g-e（可选的模块化收尾，`final_gate.py` ~740 行）——
  §2 表里写着它属**清晰度优化**、不是达标条件（W4 的 C1–C5 已满足），本波没有为了"表格全绿"去做它；
  真实档基准重跑（要 token）、整解释器冻结根因（要现场）、两条政策项（用户保持现状）。

## 3. W2a 已实施内容（W2b 见 §2.10）

**W2b（按角色模型分层）已完成**（`ba6b0af`）：五个角色各有 `codex_model_*` 与推理档，
由 `kind` 选档（`_codex_exec_args`），看板有 `model_tiers`，证据 `api/tests/test_model_tiers.py` 7 例。
这里原先写的"暂缓：用户指示统一用便宜模型"是**过期标记**（2026-09-24 核实）——保留这句话才看得出它漂过。

W2a 实际改动点（已完成）：

| 动作 | 位置 |
|---|---|
| `RunnerEnvelope` 增 `usage: dict \| None` | `api/app/runner.py` 数据类 |
| 新增模块级解析函数 `usage_from_codex_logs()`（读日志尾部取最后一次 `turn.completed.usage`） | `api/app/runner.py:54` |
| `_run_codex_io` 每次尝试后提取 usage，并回填到全部 5 个有日志的 envelope 构造点 | `api/app/runner.py`（循环内 `usage` 初始化 + 各 return） |
| `RunRecord` 增 `usage_json` 列 | `api/app/models.py` |
| 增量迁移项 `usage_json: "TEXT"` | `api/app/database.py` 的 `run_additions` |
| `_record_run` 写入 `usage_json` | `api/app/orchestrator.py` |
| 两条恢复路径（`_recovered_envelope`、acceptance 恢复）同步回填 usage | `api/app/orchestrator.py` |

设计取舍：**从日志尾部解析，而非改流式循环**——不改 `_communicate_with_watchdog` 的返回签名，也不依赖分块边界。

**未采纳（原计划中的 W2b）**：`CODEX_BATTLEFIELD_CODEX_MODEL_{PLANNER,WORKER,VERIFIER,ACCEPTANCE}` 之类的按角色模型覆盖、`kind` 传入 `_codex_exec_args`。原因见上。

## 4. 未决决策（等用户）

| 编号 | 决策 | 现状 |
|---|---|---|
| B-1 | 是否把当前工作区提交为基线提交 | **已解决**：提交 `64cd8d7`（28 文件，+1724/−57），提交信息里写明"同时包含 06-23/07-07 遗留改动（含 `codex_home`）与 W0/W1/W2a"。此后**每波一个提交** |
| D1 | `max_acceptance_repair_cycles` 默认是否由 0（不限）改为 2 | **已解决（2026-09-22）**：改并落地（默认 2；`0` 仍表示"不限"，要旧行为显式设 0）。见 §2.15 与 `docs/spec-modularization.md` §6 D1 |
| E-1 | R3 是否改用 codex 原生 `Worktrees`（`--worktree`） | **已定：不采用**。主因：`exec --json` 不上报工作树路径 + 与 `--ephemeral` 互斥；叠加不回收、detached HEAD、experimental。**"路径不可控"的说法已修正**：根可用 `$CODEX_HOME/worktrees/` 控制 |
| E-2 | 读隔离实现层（codex 沙箱 / 私有数据根 / OS ACL） | **已解决**：R0/E3 与 R2 实测推翻了"沙箱不提供读隔离"——`--cd <own worktree>` 下跨目录读被 OS 拒绝，所以**不需要**再加 ACL/私有数据根（见 §2.12、`docs/spec-task-isolation.md`） |
| E-3 | 优先级策略（静态优先级 / 老化 / 高优预留） | 未定：高优**预留容量**仍不做（会降利用率）；静态优先级 + 老化已落地（R1 第二轮） |
| E-4 | 执行顺序 | **已定：先 R0 实测，再回 W2** |
| A-1 | 是否把集成机制从拷贝改为 git merge | **已定：不改**。内轨保留拷贝式集成，git 仅用于外轨里程碑（`spec-resource-governance.md` §9） |
| A-2 | **A3（`orchestrator.py` < 7000 行）是否可行** | **已定：可行，且余量很大**。普查实测（`scripts/audit_orchestrator_movable_mass.py`）：376 个方法中 **350 个可搬**（6035 行函数体），不可搬的只有 **26 个**（2119 行，即真正的核心：`get_watchdog_state` / `_accept_parent` / `_process_node_unlocked` / `_execute_leaf` / `get_tree` …）。**全部搬完后预计 3376 行**。 |
| **A-3** | **A3 达标是否等于 W4 结束** | **已定：不等于，A3 已降级为读数**。理由三条：①A3 是代理指标，目标（§1）是"职责清晰、降低维护成本"，把几千行塞进一个 `misc.py` 就能达标——数字过、目标死；②它只是 A1–A7 之一，不能顶替 A1/A4/A7；③**实测已使它失去区分度**：全搬完预计 3376，门槛 7000 无论怎么搬都会过。完成判据改为 **C1–C5（内聚优先）**，见 `docs/spec-modularization.md` §7.8，其中 **C2「依赖方向单向」由 `api/tests/test_module_layering.py` 机器判定**：`api/app/` 下除 `orchestrator` 自身与入口 `main` 外，任何模块不得 import `orchestrator`，且模块依赖图无环 |

> **A-2 的一段自我更正（保留记录）**：我曾据 `checkpoint` 一步的实测（估算 590 行、实际仅 64 行可搬）推断"大模块普遍如此、A3 很可能不可达"。**这个推断是错的**——我从单个函数的不可搬性过度概括到了整组。普查显示不可搬性只集中在 26 个方法里，其余 350 个传递可搬。**教训**：在耦合的类里，耦合点的数量必须**测**，不能由个例外推；这也是把普查脚本固化下来的原因。
>
> 普查的"可搬"只回答**行数可达**，不等于**分组合理**。W4e/W4f/W4g 仍需按职责设计模块边界，风险在分组与内聚，而非可行性。

## 5. 必须记住的方法论陷阱（已踩过）

| 陷阱 | 后果 | 规避 |
|---|---|---|
| PowerShell `Measure-Object -Line` 跳过空行 | 少算约 500 行，A3 基线差点被改错 | A3 固定用 `python -c "...read_text().splitlines()"` |
| 只信产出方自述，不查文件系统 | "看起来完成"的假验收 | E1/E2 类实验一律以磁盘状态为判据 |
| 测试依赖宿主空闲内存 | 本机 4018MB < 默认阈值 4096MB → 20 个测试假失败 | `api/tests/conftest.py` 已把守卫默认关掉 |
| Windows 下缺 `[windows] sandbox="unelevated"` | `workspace-write` 静默退化只读，worker 变 `blocked` | 隔离 `CODEX_HOME` 的 `config.toml` 必须带该段 |
| 长跑命令会随会话断连被杀 | 等两小时才发现进程早死了 | 长任务必须落盘 summary，靠文件而非终端状态判断 |
| `core.autocrlf=true` 下用 git 操作产物 | git 会改写 LF/CRLF，破坏字节精确验收（宪法反缩水条款明令此类解释不通过） | **已实证**：12 字节 LF 经 `git checkout --` 变 14 字节 CRLF。内轨禁用 git 操作 |
| Hub 模型白名单**会变且静默失败** | 2026-09-11 `deepseek-v4.1-flash` 被移除换成 `deepseek-flash`，所有 Hub 调用立刻全失败，终端却只是"没有输出" | 长跑前先刷白名单；查 `workspace/runs/<node>/<kind>.jsonl` 的 `turn.failed`；见 `docs/provider-setup.md` |
| 只看终端输出判断成败 | 失败时终端可能完全空白，会被误判成"卡住"或"成功" | 一律以 `--json` 事件流与磁盘状态为判据 |
| `--strict-config` 认定的合法字段 ≠ 在 exec 模式生效 | `desktop.git-worktree-root` / `desktop.worktree-auto-cleanup-enabled` 均通过校验，但 `codex exec` 下完全不生效（桌面版作用域） | 配置键必须**实测效果**，不能只看是否被接受 |
| 隔离 `config.toml` 混入未知字段 | `model_max_output_tokens` 非合法字段，被静默忽略（我当初为消警告加的） | 例行加 `--strict-config` 校验隔离配置 |
| 用 `--help` 判定"能力不可控"**不充分** | 我用 `--help` 无配套参数 + 拒绝猜键名，就下了"路径不可控、不回收"的结论；实际上配置键存在（`desktop.*`），是靠**扫二进制字符串**才发现的 | 能力勘察的标准方法：`--help` → `codex features list` → **扫 `codex.exe` 字符串** → 最后实测效果。三者都要做 |
| Windows 字节范围锁在**空文件**上无效 | `truncate()` 后锁文件变 0 字节，两个进程可同时"抢到"同一槽位，后写者 `ERROR_LOCK_VIOLATION` | 锁文件保证非空、**永不 truncate**；记账写入包 `try/except OSError`，绝不中断主流程 |
| 前端 lint 自基线起就是**红的** | `web/src/api.test.ts` 用 `_input`/`_init` 标注"有意不用"，但 eslint 配置不认该约定（`pnpm lint` 失败 2 处），而 A2 要求 lint 必须绿 | eslint 配置加 `argsIgnorePattern: '^_'`；已修。**A2 现已真实为绿** |
| 用 `git commit -F <同一个文件>` 连提两次 | `f6e5a37` 复用了 `27367a7` 的提交信息文件，标题与内容不符，日后无法据标题归因 | 每次提交**单独写**信息文件；提交后立刻 `git log -1 --oneline` 复核 |
| 并行跑两个 `pytest` | 两个进程共用 `workspace/` 状态，出现 30 个 `FOREIGN KEY constraint failed` 假失败（重跑即 396 全绿） | 同一次会话里**串行**跑测试；长跑用 `> 文件` 落盘后再读，不靠终端滚动 |
| 用 `edit` 时把「方法首行」当 `old_string` | 只替换了首行，方法体残留成孤儿语句，文件语法坏掉 | 换整段时 `old_string` 必须覆盖到方法末尾；或先按行号删、再插入委托（W4g-a 用了后者） |
| 按行号删除时凭记忆定范围 | W4g-a 一次多删了 3 个**不在闭包内**的兄弟方法（`_skill_lookup_recovery_text` / `_is_human_required_blocker` / `_visual_uncertain_payload`），靠测试报 `AttributeError` 才发现 | 删除前先 `grep` 出**下一个方法名**作为右边界锚点，并在脚本里 `assert` 两端内容；删完先跑一次 `from app import orchestrator` |
| **取行号之后又改了同一个文件** | W4g-c2 用过期行号做区间替换，方法整体上移 12 行 → 替换错位、文件语法损坏（`SyntaxError: unmatched ')'`） | **取行号与用行号之间不得再动目标文件**；必须先做完所有小改动、再统一取号、再一次性替换。损坏后的恢复手段是 `git checkout -- <file>` 回到上一提交重做 |
| 普查依赖图只追 `self.X` | `Orchestrator._runtime_marker_candidate_line(...)` 这种**类级调用**不是边，方法不在闭包里却被依赖 | 闭包结果必须与「被搬方法实际引用的名字」二次核对；W4g-a/c1 都做了一次人工对表 |
| 搬移会把**测试接缝**一起搬走 | `_available_memory_mb` 有 5 处实例级 monkeypatch，首轮搬走后 4 个用例转红 | 修法是把探针作为**显式参数**传下去保住原接缝，而不是改测试；并补一条用例把接缝锁死 |
| 把闭包当成"该模块的职责" | W4g-d 闭包 58 方法里有 `serialize_task`/`_add_event`/`_mark` 等通用基础设施，全搬等于按调用图切分 | 按**职责**定边界，通用原语用 ports 留下来；范围收窄后如实写进提交信息 |
| **门禁的覆盖范围**没人看 + 报告只在成功路径写 | `scripts/bench_run.py` 里 `Path` 未定义（F821）只在 real 档触发（mock 档无 worktree，`and` 短路），而门禁只在 `api/` 下跑 ruff → 连丢两轮真实基准，磁盘上零证据，现场看起来像"产品卡死" | 门禁路径由 `LINT_PATHS` 单一来源决定并有测试盯着（`api/tests/test_repo_gates.py`）；长跑每任务后 + 每 60s 落 `partial=true` 报告，异常路径也落一份 |

## 6. codex CLI 0.154 关键能力（实测）

| 能力 | 结论 |
|---|---|
| `--cd` / `--add-dir` | 工作根可控；额外可写目录可显式添加 |
| `--sandbox` | 仅 `read-only` / `workspace-write` / `danger-full-access`，**无法表达读白名单** |
| `--worktree` | **codex 自带 managed worktree**，项目当前却在手搓 `git worktree add`（`orchestrator.py:10725`） |
| `--profile` | 按 `$CODEX_HOME/<name>.config.toml` 叠加 |
| `--ignore-user-config` | 说明写明 `auth still uses CODEX_HOME`，**不隔离凭据** |
| `--strict-config` | 可校验隔离 config.toml 无未知字段 |
| `-c sandbox_permissions=[...]` | 存在但语义未明，待 R0 实测 |

## 6.5 R0 隔离实测结论（决定 R2 形态）

| 维度 | 实测结论 | 因此 |
|---|---|---|
| **写隔离** | `--cd <own worktree>` 已把可写范围限死在 cwd；越界写入被 **OS 拒绝**（`UnauthorizedAccessException`） | **无需新机制**，只需禁止添加多余 `--add-dir` |
| **`--add-dir`** | 精确扩展可写范围，加了也不会越界到兄弟目录 | 可作显式白名单 |
| **读隔离** | 跨目录读取**全部放行**（兄弟目录内容可被完整读出） | codex 沙箱**做不到**，**必须下沉到 Windows ACL** |
| **凭据** | 全局 `~/.codex/auth.json` 对节点**可见**（3977 字节） | 隔离 `CODEX_HOME`+`env_key` 可规避；但暴露面会转移到**子进程环境变量** |
| **设计边界** | 验证者/父节点**必须**能读自己的工作树与集成后的目标树 | 读隔离须**精确到兄弟/分支目录**，不可全局禁止 |
| **间接泄露** | E1 只证明写入被拒；"读兄弟内容→写进自己产物"仍可行 | 越权检测必须覆盖此形态 |

## 7. 续跑清单

1. 读本文件，不要靠对话记忆。
2. 跑 §1 的基线命令：要求 **0 failed**，passed 数**只增不减**。**不要在这里写死数字**——
   本文件曾经长期挂着"期望 380 passed，若不符先停下查因"，而当时实际早已 686，照它恢复会话会当场误停。
   要一个具体数看 `README.md` 的「跑测试」一节（标了日期，是快照不是承诺）。
3. 按 §4 的 B-1 / 用户指示决定是否提交基线（已解决，见 §4 B-1）。
4. 执行 §2 中状态为「待做 / 未开工」的下一项。**R0 的 E1–E9 已全部完成**（不再是卡点，也不需要批准）；
   原先这里写"唯一需要用户拍板的是 **R2**"，那是**过期标记**（2026-09-24 核实）：R2 已由 `6370b30` 做掉。
   截至 2026-09-24，§2 表里只剩 **W4g-e**（可选的模块化收尾，明确**不是**达标条件）没做；
   要用户拍板的是**花 token 的那几件**（真实档基准重跑、ISO-3 沙箱探测接门禁），不是 R2。
5. **每完成一波，回来更新 §2 状态表、§4 未决决策、§5 方法论陷阱**；文档里的"当前读数"必须现测后再写。
