# Codex Battlefield 资源治理与任务隔离 SPEC

状态：待评审（R0 未开始；实测脚本已就绪，待批准运行）
前置 spec：`docs/spec-modularization.md`（W1–W5 模块化，W1 已完成）
续跑锚点：**`PLAN-STATUS.md`**（中断后从该文件恢复，不要依赖对话记忆）
基线：`api` pytest **324 passed / 0 failed**

## 1. 目标

1. **统一分配外部重资源**：Godot、以及未来任何"一台机器同时只能开 N 个"的资源，由调度器统一排队分配，容量可配、排队可见、可优先。
2. **任务间隔离**：同一账号下，节点之间互不越权——不读对方凭据、不写对方边界、不读对方产物。
3. **worktree 生命周期可控**：创建串行化、结束后回收、分支清理。
4. **合并可控**：冲突不再静默丢弃。
5. **跨版本回归门禁**：合并前有版本基线可比。
6. **失败复盘**：失败与阻塞子树纳入父主导的联合复盘，且可跨树查询。

## 2. 非目标

- **不做多账号路由**。用户已明确：隔离指"同账号下任务间互不越权"，不是节点绑定不同账号。
- 不替换 codex CLI 的沙箱实现；只使用它暴露的能力，缺什么如实记录。
- 不引入消息队列/分布式调度器等外部基础设施。现有「文件锁 + JSON」模式已够用。
- 不做完整 `RESOURCE_POOL_DESIGN.md`（Provider Slot / Capability / Lease 全模型）。只做其中被本 spec 直接需要的最小切片。

## 3. 现状事实（已核实，带行号）

### 3.1 外部资源

| 事实 | 位置 |
|---|---|
| **Godot 全局互斥已存在**：`godot.global.lock` | `runner.py:1496-1568` |
| 但容量**硬编码为 1**，只可调等待、不可调数量 | `runner.py:1546`、`1503-1508` |
| 锁**藏在生成脚本内**（`_ensure_godot_timeout_wrapper`），orchestrator 从不获取/释放 | `runner.py:1477-1834` |
| **静态检查路径不拿锁** —— 经核实**不是缺陷**：`_static_project_check` 只做纯文件检查，**不启动 Godot 进程**，故不应占用槽位 | `runner.py` 包装器内 `main()` 的分派 |
| Godot 超时被强制 `≤45s` | `runner.py:1757` |
| 其他共享 OS 资源：`codex` CLI 自身（受 slot 约束）、PowerShell 进程扫描、`git`、`local_kick`、watchdog。无服务器/浏览器/端口绑定 | `runner.py:839`、`orchestrator.py:1948/7149/7197/777/10736` |

### 3.2 可复用的既有机制（重要）

| 事实 | 位置 |
|---|---|
| slot 记账 = **进程内 dict + 跨进程 JSON 文件 + 文件锁** | `orchestrator.py:320/325-329`、`_mutate_shared_pending_codex_slot_reservations:2080-2111` |
| 跨 orchestrator 实例可见，已有测试验证 | `test_orchestrator.py:10171-10231` |
| 容量抽象 `DynamicConcurrencyLimiter`（asyncio.Condition，可 `set_capacity`） | `orchestrator.py:269-301`、`283` |

**命名资源租约管理器可直接复制此模式**，无需另造基础设施。

### 3.3 授权与隔离

| 事实 | 位置 |
|---|---|
| `CODEX_HOME` 是**全局单值**，所有节点共享 | `config.py:20`、`runner.py:2165-2168` |
| `codex_profile` 同样全局单值 | `config.py:21`、`runner.py:958-959` |
| 无身份实体、无 per-task 覆盖字段 | `models.py` |
| 隔离的是**工作目录**而非凭据：`cwd=task.worktree_path` | `runner.py:799` |
| 声明式边界校验存在：`changed_files` 必须相对且 `relative_to` 通过，否则 `skipped` | `orchestrator.py:9944-9954`、`10513-10517` |
| `read-only` 沙箱下会追加 `--ignore-user-config`（会忽略 provider 配置） | `runner.py:988-989` |

### 3.4 codex CLI 能力（实测 `codex exec --help`，0.154.0）

| 能力 | 结论 |
|---|---|
| `--cd <DIR>` | 工作根可控 |
| `--add-dir <DIR>` | **额外可写目录**——不加即写不出边界 |
| `--sandbox` | 仅 `read-only` / `workspace-write` / `danger-full-access` 三档，**无法表达读白名单** |
| `--worktree` | **codex 自带 managed worktree**（项目当前手搓 `git worktree add`） |
| `--profile` | 校验隔离配置无未知字段 |
| `-c sandbox_permissions=[...]` | 存在权限列表，**语义未明，必须实测** |

### 3.5 worktree 与合并

| 事实 | 位置 |
|---|---|
| 创建：`git worktree add -b codex/node-<id> <path>`，60s 超时 | `orchestrator.py:10725-10736` |
| 非 git 仓库回退：`mkdir` + `shutil.copy2` 播种 | `orchestrator.py:10747-10775` |
| **从不回收**：全库无 `worktree remove` / `prune` / 分支删除 | 全局 |
| **创建未串行化**：13 节点可同时对同一 target 执行 | `orchestrator.py:10725` |
| 合并 = **纯文件拷贝**，无 git apply/cherry-pick/merge | `_integrate_child_outputs:9911-9995`、`_copy_integrated_path:10568-10592` |
| 仅 `project.godot` 文本合并、readme 追加，其余 `copy2` | `10594-10613`、`10615/10632` |
| 越界/冲突**静默 `skipped`** | `9944-9962` |

### 3.6 优先级

| 事实 | 位置 |
|---|---|
| **Codex agent 启动额度**这一层**无资源优先级**，纯 FCFS，满则标 `QUEUED` 等下一轮 | `_reserve_agent_start:2290`、`_try_reserve_pending_codex_slot_with_capacity:2212` |
| 唯一排序只作用于**同一父节点内兄弟** | `_scheduler_ready_priority:8614-8630` |
| `TaskNode` 无 priority/weight 字段（优先级由**角色**推出，不落库） | `models.py:60-85`、`runner.GODOT_QUEUE_ROLE_PRIORITY` |
| **例外（2026-09-20 起）**：真实 Godot **命名槽位**已有静态优先级 + 老化——按角色权重（acceptance 3 > verifier 2 > worker 1 > planner 0）、应用层仲裁、老化防饥饿 | `runner.GODOT_QUEUE_ROLE_PRIORITY`、`runner_tools.py` 包装器的 `_may_attempt_now` / `_effective_priority` |
| 高优**预留容量**仍不做（会降利用率，见 §7 E-3） | 全局 |

### 3.7 验收门禁与失败复盘

| 事实 | 位置 |
|---|---|
| 父 Godot 集成门禁（纯文本/引用检查，无进程启动） | `_parent_godot_integration_gate:9037-9099`、触发判定 `9101-9116` |
| 根最终成品门禁（同为静态检查） | `_final_root_product_gate:9159-9232` |
| **skill_note 已是父主导**：父创建 note 子任务 + 父层合并 | `_with_parent_skill_note_child:8541`、`_parent_skill_note_child:8682`、`_consolidate_parent_skill_notes:9997-10059` |
| note 子任务规格要求读**全部已完成子树**产物、验证证据、阻塞报告 | `_parent_skill_note_child:8688-8691` |
| **但只归拢已完成子树，失败/阻塞子树经验不进入 merged** | `10061-10067` |
| 父层无 `failure_retrospective` 字段 | `models.py` |
| 失败数据分散在 5 处，无统一索引/查询 | `node_meta["block_report"]`、`error_message`、`RunRecord.result_json`、`EventRecord.payload_json`、`node_meta["blocked_child_reports"]` |
| `state_snapshots` 仅用于崩溃恢复，**非版本回归** | `_write_state_snapshot:5620+`、`_restore_snapshot_if_missing:5984-6066` |
| `constitution_hash` 已落库但**从未用于比较** | `models.py:125`、`orchestrator.py:10809` |

## 4. R0：沙箱与隔离能力实测（**前置，必须最先做**）

R2 的方案取决于实测结果，不接受在假设上设计。每项实验产出**原始输出**为证据。

| 编号 | 实验 | 判定标准 |
|---|---|---|
| E1 | `--cd <A> --sandbox workspace-write`，令 agent 写 `<A>/ok.txt` 与兄弟目录 `<B>/bad.txt` | 两者是否都被允许 → 决定写边界是否天然成立 |
| E2 | 同上但加 `--add-dir <C>` | `<C>` 可写、`<B>` 仍不可写 → 确认可写根语义 |
| E3 | 令 agent 读 `<B>/secret.txt` 并回显内容 | 是否可读 → 决定读隔离是否必须靠 OS 层 |
| E4 | 令 agent 判断 `$CODEX_HOME/auth.json` **是否存在**（**只测存在性，不回显内容**） | 决定凭据是否对节点可见 |
| E5 | 在 git 仓库中用 `--worktree` 跑一次，观察目录位置、分支名、会话结束后是否自动清理 | 决定 R3 是否改为委托 codex |
| E6 | `--strict-config` 校验隔离 `config.toml` | 是否有未识别字段 |
| E7 | 观察 `--worktree` 创建的目录位置与分支名**是否可控** | 不可控则无法与既有 `workspace/worktrees/<node-id>` 契约对齐 |
| E8 | **多个 codex 会话在同一仓库并发各自 `--worktree`** | 是否安全 → 决定 13 并发可行性（git index.lock 竞争） |
| E9 | 当前 `core.autocrlf=true` 下，实验：`git checkout` / 合并同一文件前后比对**字节序列与 hash** | 若字节被改写，则内轨不得引入任何 git 操作（详见 §9） |

已知一半证据：`core.autocrlf = true`（仓库级已实测），且 `git diff --stat` 已对多个文件发出
`LF will be replaced by CRLF the next time Git touches it` 告警。E9 用于确认具体字节影响。

### R0 实测结果（2026-09-11）

**前置条件（E5 第一次尝试即撞上）**：`--worktree requires the worktrees feature`，必须加
`--enable worktrees`。`codex features list` 显示 `worktrees` = **experimental / 有效值 false**。

#### E5 `--worktree` 生命周期

| 观测项 | 结果 |
|---|---|
| 工作树位置 | `$CODEX_HOME/worktrees/<4 位随机 hex>/<仓库目录名>` |
| 分支 | **detached HEAD，不创建分支** |
| 会话 cwd | 确为该工作树（由 agent 执行 `pwd` 的 `command_execution` 记录证实） |
| 会话结束后 | **工作树保留，不自动删除**（本轮累积 8 个） |
| 源仓库残留 | `.git/worktrees/` 留下 8 条 stale 注册 |

#### E7 可控性 —— 命令行不可控

- `codex exec --help` 中 `--worktree` **无任何配套参数**，无路径/分支选项。
- 位置仅间接受 `$CODEX_HOME` 影响；末级是随机 4 位 hex，**路径不可预测**。
- **codex 不在 `--json` 事件流中上报工作树路径**——本次只能靠让 agent 自己执行 `pwd` 才得知。
- 是否存在可控配置键**未验证**：`codex debug` 仅有 `models`/`app-server`/`prompt-input`，无配置 dump，猜键名不可靠。

#### E8 并发 —— 技术上安全

- 同仓库 3 个并发 `--worktree` 会话：全部成功，**stderr 无 `index.lock` / 冲突**。
- 各自获得独立工作树，且各自只写自己的工作树（3 个 marker 分落 3 个不同工作树）→ **文件系统层面隔离成立**。
- 但同样**不回收**。

#### E9 autocrlf 字节改写 —— 结论确凿

| 文件 | 操作前 | 操作后 |
|---|---|---|
| `lf.txt` | len=12, sha=2751A3A2F303AD21, crlf=0, bare_lf=2 | **len=14, sha=4AD3EF64DFB83F7A, crlf=2, bare_lf=0** |
| `crlf.txt` | len=14, sha=4AD3EF64DFB83F7A | 不变 |

`rm` + `git checkout --` 后，纯 LF 文件被改写为 CRLF，且**与 CRLF 版本字节完全相同**。
→ 在 `autocrlf=true` 下 git 确实改写字节，**内轨不得引入任何 git 操作**（§9 由推论升级为实证）。

### R0 结论对设计的影响

| 影响 | 内容 |
|---|---|
| **R3 不采用 `--worktree`** | 决定性理由：codex **不上报工作树路径**，而 orchestrator 必须知道该路径才能让 verifier/acceptance 定位、才能集成 `changed_files`、才能归档证据。叠加它是 experimental、detached HEAD（无分支可追溯）、同样不回收，收益不足。 |
| R3 继续自建 | 保留 `git worktree add -b codex/node-<id> <path>`（路径与分支均受控），补串行化与回收。 |
| §9 双轨 | 由推论升级为**实证**。 |
| 新增运维坑 | Hub 模型白名单会变且**静默失败**，处置见 `docs/codemaker-hub-provider.md`。 |

**E4 的安全约束：只检测文件存在性与大小，禁止把凭据内容写入 prompt、日志或产物。**

### R0 追加实测：`Worktrees` 特性全貌（2026-09-11 二次）

#### 特性正式名称

二进制内文案：

```
Worktrees
Create isolated Git worktrees and group sessions by repository.
NEW: Worktrees can now be enabled from /experimental. Restart Codex after enabling it.
```

即：**按仓库分组会话，每个会话一个隔离 Git 工作树**。

#### 「对话 ↔ worktree 绑定」（用户记忆的「对话 worktree 分离」）

- `codex-thread.json` 内含 `ownerThreadId`；错误文案 `worktree already belongs to thread`、`worktree owner thread ID cannot be empty`。
- TUI 文案：`Preserve this conversation in the new checkout`、`Continue current conversation`、
  `Resume an owner thread or copy a working directory`、`Select a worktree to resume its owner or copy its working directory`。
- 结论：**一个会话（对话）绑定一个工作树**，该能力确实存在，受 `/experimental` 开关控制。

#### 发现的配置键（均被 `--strict-config` 认定为**合法**字段）

| 键 | 约束 |
|---|---|
| `desktop.git-worktree-root` | 必须是绝对路径 |
| `desktop.worktree-auto-cleanup-enabled` | 布尔 |
| `desktop.worktree-keep-count` | 必须是正整数 |

#### 实测：`desktop.*` 键在 `codex exec` 模式下**不生效**

指定 `-c desktop.git-worktree-root="<自定义根>"` 与 `-c desktop.worktree-auto-cleanup-enabled=true` 后：

- 工作树仍落在 `$CODEX_HOME/worktrees/bf47/gitrepo`，**自定义根为空**；
- 工作树**仍然保留**，未回收。

→ 这批键是**桌面版作用域**。exec 模式下**唯一可控的是根位置，即 `$CODEX_HOME/worktrees/`**。

#### exec 模式下的互斥约束（二进制文案）

| 约束 | 对本工程的影响 |
|---|---|
| `--worktree cannot be combined with --ephemeral` | runner 默认 `codex_ephemeral=True`（`config.py:58`）→ 采用前必须先关闭 |
| `--worktree cannot be combined with --ignore-user-config` | Hub 模式已设 `IGNORE_USER_CONFIG=0`，兼容 |
| `--worktree is not supported with codex exec resume` / `review` | 与 runner 现有调用方式不冲突 |
| `--worktree requires local execution` / `only supported for local sessions` | 与本地 runner 兼容 |
| `Creating a worktree requires an idle primary session without queued input` | 疑为 TUI 约束，exec 下未触发 |

#### 失败时会留下未使用的检出

```
Could not start a session in the managed worktree. A checkout was retained at ...
remove it with `git worktree remove <checkout-path>` from the source repository
```

→ 失败路径会留残留，需手动清理。

#### E-1 结论修正

**仍不采用，但主因收窄为两条**：

1. **路径不上报**：`codex exec --json` 不给出工作树路径（本次只能靠让 agent 自己跑 `pwd` 才得知）。
2. **`--ephemeral` 互斥**：runner 默认开启，需为之改变现有调用契约。

叠加 exec 模式**不回收**、detached HEAD（无分支可追溯）、experimental 开关。

**同时修正一条先前不准确的说法**：早先写"路径不可控"**不成立**。根位置可通过
`$CODEX_HOME/worktrees/` 控制（`desktop.git-worktree-root` 在 exec 下无效，但 `CODEX_HOME` 有效）。
因此若将来 per-node `CODEX_HOME` 落地（R2），每节点工作树根是可控的——这条为 R3 保留了未来选项。

#### `--strict-config` 已被证明有用

它查出本工程隔离 `config.toml` 中的 `model_max_output_tokens` 是**未知字段**
（`model_context_window` 合法）。该键已删除。**建议纳入例行校验**（原 E6）。

### R0 结果：E1–E4 写 / 读 / 凭据（2026-09-11）

前置：模型名已修正为 `deepseek-flash`；只终止了本次派生的 CLI 残留进程，**保留桌面版 `app-server`**。

| 编号 | 实验 | 结果 | 判据 |
|---|---|---|---|
| E1 | `--cd A`，写 `A/ok.txt` 与 `B/bad.txt` | `A/ok.txt` **写入成功**；`B/bad.txt` **被拒** | 以**文件系统实际状态**为准 + 拒绝原文 |
| E2 | 追加 `--add-dir C`，写 `C/ok.txt` 与 `B/bad.txt` | `C/ok.txt` **成功**；`B/bad.txt` **仍被拒** | 同上 |
| E3 | 读兄弟目录 `B/secret.txt` | **成功，内容完整泄露**（`READ_ALLOWED`） | agent 回显原文 |
| E4 | 检测 `~/.codex/auth.json` | **存在，3977 字节**，与独立实测一致 | agent 只报大小，未回显内容 |

E1 的拒绝来自 **OS 层而非 codex 策略层**：

```
Set-Content : 对路径"G:\...\_r0\B\bad.txt"的访问被拒绝。
+ CategoryInfo : PermissionDenied ... [Set-Content], UnauthorizedAccessException
```

#### 结论

| 维度 | 结论 | 对 R2 的影响 |
|---|---|---|
| **写隔离** | **天然成立**。`--cd <own worktree>` 已把可写范围限死在 cwd；`--add-dir` 是**精确扩展**，加了也不会越界到兄弟目录 | **无需新机制**，只需约束"不添加多余 `--add-dir`" |
| **读隔离** | **codex 沙箱不提供**：跨目录读取全部放行 | **必须落在 OS 层**（Windows ACL）。私有数据根只能降低"可发现性"，**不能**阻止已知路径的读取 |
| **凭据隔离** | 全局 `~/.codex/auth.json` **对节点可见** | 但若使用隔离 `CODEX_HOME` + `env_key`（本工程 Hub 模式正是如此，其目录**不含 auth.json**），凭据文件暴露**已被自然规避** |

#### 设计边界（避免隔离过度）

1. **验证者与父节点必须能读**自己的工作树与**集成后的目标树**。因此读隔离必须**精确到兄弟/分支路径**，不能一刀切全局禁止——这正是"用 ACL 限定特定目录"而非"禁止读取"的理由。
2. E1 只证明了**写入**被拒。"读取兄弟内容后复制进自己产物"这类**间接泄露仍然可行**，越权检测必须覆盖此形态。
3. E4 的测试对象是**全局** `~/.codex/auth.json`。若 battlefield 走隔离 `CODEX_HOME`，该文件不存在，凭据改由环境变量承载——此时真正的暴露面是**子进程环境变量**，而不再是磁盘文件。

## 5. 波次

### R1 命名资源租约与优先队列

**状态：容量可配 + 排队可见 + 静态优先级 + 老化 已完成（2026-09-20）；高优预留容量仍不做（见 §7 E-3）。**

- **动机**：Godot 容量硬编码 1、锁不可见；13 节点争抢时无排队、无优先级。
- **实际改动点**（与原计划的差异：**没有把锁上提到调度器**，而是在包装器内保留，只参数化）：

| 动作 | 位置 |
|---|---|
| 新增 `godot_max_concurrency` 设置（默认 1） | `api/app/config.py` |
| 注入 `CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY` / `GODOT_STATE_PATH` | `api/app/runner.py` 的 `_codex_env` |
| 单文件锁 → **N 槽位锁**（`<base>.slot0..slotN-1`），并新增容量/状态/存活探测/排队记账 | `runner.py` 包装器字符串内 |
| 排队状态暴露为 `godot_resource`（capacity / holders / waiters / updated_at / stale_seconds） | `orchestrator._godot_resource_state()` + `get_watchdog_state` |
| 新增回归测试 | `api/tests/test_godot_slot_capacity.py` |

- **验收（已达成）**：
  - 容量实测生效：4 进程争 capacity=2，实测最大并发 **2**，两波清晰的持有-释放序列；
  - 排队可见：争抢期间状态文件正确显示 2 holders（含 slot 号）+ 2 waiters；
  - `pytest` **326 passed / 0 failed**（原 324 + 新增 2）。
- **一处必须记录的措辞更正**：原计划写的"封住静态检查旁路"**不成立**。核实后确认
  `_static_project_check` 只做纯文件检查、**不启动 Godot 进程**，因此不占用槽位才是正确行为。
- **实施中发现的真实缺陷（由测试抓出，非推理）**：原实现会 `truncate()` 锁文件。**Windows 的字节范围锁在空文件上无字节可锁**，导致两个进程可同时"抢到"同一槽位，后写者触发
  `ERROR_LOCK_VIOLATION`（`PermissionError` on flush）。修法：槽位文件保证非空、**永不 truncate**，并把该信息性写入包进 `try/except OSError`，确保记账失败**绝不中断真实 Godot 运行**。
- **仍有风险**：占用槽位的进程被强杀时，OS 锁会随进程释放，但状态 JSON 中的 holder 需靠存活探测清理（已实现 `_pid_alive`）。
- **第一轮未做**（第二轮见下）：优先级、老化、高优预留；锁上提到调度器（保留在包装器内，改动面更小）。

#### R1 第二轮：静态优先级 + 老化（2026-09-20 落地，`abef3a0`）

- **优先级的来源（§7 E-3 已决）**：按**角色**——`acceptance`(3) > `verifier`(2) > `worker`(1) > `planner`(0)。
  理由：门禁/验收在完成关键路径上，卡住会让整棵树都不动；而 worker 的探索性 Godot 运行可以等一会儿。
  不新增 `TaskNode.priority` 字段（那是 E-3 的第三个选项，需迁移且改动面最大）：权重在 `_run_codex` 由
  `kind` 算出，经 `_codex_env(kind)` 注入 `CODEX_BATTLEFIELD_GODOT_QUEUE_WEIGHT`。
  **机制一落地就有人喂值**——这是刻意避免"接线看着在、实际是死的"那条老坑。
- **仲裁在应用层**：OS 字节锁只保证**互斥**（容量绝不超过），它不认识优先级；原来的实现是"谁先轮询到释放的
  空槽谁拿"。包装器新增 `_may_attempt_now`：只有按等效优先级排前 `capacity` 的等待者去抢槽，其余继续等；
  **读不到状态 / 解析失败一律放行**——仲裁是优化，不能因为它坏了就让人永远等。
- **老化**：`CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS`（默认 300）——每等这么久，等效优先级 +1；
  设 0 = 纯静态优先级。口径注意：老化对**同时在等**的人不改变差距（两人同速上涨），它拉开的是
  "先到者 vs 后来者"，所以它**防饥饿**，但不改变高优对同到者的领先。
- **可见性**：状态文件里的等待者带 `priority` / `waited_seconds` / `effective_priority` / `aged`，
  顶层带 `queue_policy`；`_godot_resource_state()` 把 `queue_policy` 透传给看板（缺了就留 `None`，不用默认值冒充事实）。
- **开关**：`CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY`（默认开），关掉即回纯 FCFS，便于回退与对照。
- **两个坑，都由测试抓出**：
  1. `since` 必须**跨刷新保留**：状态记账每 60 秒刷新一次用于展示，若刷新时重置 `since`，
     老化时间永远归零 = 等于没有老化。
  2. 测试驱动一开始漏了注入 `..._QUEUE_WEIGHT`，于是所有进程权重都是 0，"优先级"用例实际在测先到先得
     （实测顺序 `[0,2,1]`，看起来像时序抖动）。**测试自己也会撒谎**：先证明它真的在测你要测的东西。
- **验收（实测）**：容量 1 下三个等待者权重 0/1/2 → 获取顺序 **2,1,0**；老化 1 秒下先到的低权重**反超**
  后到的高权重；同权重仍 FCFS；关掉开关后两人都能拿到（关掉后顺序本就由轮询时机决定，故不断言顺序，
  改断言"都能拿到 + 状态里策略为关"）。仲裁规则另有 **3 个确定性单元断言**（不依赖时序）+ 1 个 env 接线断言。
  `pytest` **711 passed / 0 failed**。

### R2 任务间隔离

- **动机**：同账号下节点互不越权。
- **实测基线（R0 E1–E4）**：

| 维度 | 实测 | 因此 |
|---|---|---|
| 写边界 | `--cd` 已限死 cwd，越界写入被 OS 拒绝（E1） | **无需新机制** |
| `--add-dir` | 精确扩展，加了也不越界到兄弟目录（E2） | 可用作显式白名单 |
| 读边界 | 跨目录读**全部放行**（E3） | 沙箱层面**做不到**，必须下沉到 OS |
| 凭据 | 全局 `~/.codex/auth.json` 对节点可见（E4） | 隔离 `CODEX_HOME` + `env_key` 可规避 |

- **改动点**：
  1. **可写边界（低成本，先做）**：叶子只拿 `--cd <own worktree>`，**不添加任何多余 `--add-dir`**。已核实 `api/app` 全目录**零处** `--add-dir`，故此项**现状即成立**——要做的是加约束与回归断言，防止日后有人添加而破坏它。
  2. **凭据隔离**：以隔离 `CODEX_HOME`（不含 `auth.json`）+ `env_key` 为默认路径；对必须使用真实账号的场景，改为父进程注入短期凭据，任务结束即失效。**注意：真正的暴露面会从磁盘文件转移到子进程环境变量**，两者都要管。
  3. **读隔离（必须 OS 层）**：用 Windows ACL 对**兄弟节点/分支目录**设拒绝，而不是全局禁止读取——因为验证者与父节点**必须**读自己的工作树与集成后的目标树。私有数据根（`workspace/nodes/<id>/`）用于降低可发现性，是补充而非替代。
  4. **越权检测**：运行前后对工作区做快照，检测边界外**实际**写入；同时检测"读取兄弟内容后复制进自己产物"的**间接泄露**（E1 只证明了写入被拒，间接泄露仍可行）——现状只校验**声明**的 `changed_files`（`9944-9954`）。
  5. **产物 ACL**：`GET /api/runs/{run_id}/artifact` 增加节点归属校验（当前仅做 workspace 路径约束）。
- **验收**：E1–E4 实验脚本转为回归测试；越权写入可被检出并留证；兄弟节点读不到对方产物；**验证者/父节点的合法读取不被破坏**。
- **风险**：OS ACL 在 Windows 上要求同一卷、且 `worktrees` 与 `runs` 需同卷；若跨卷则 ACL 方案退化，需回退到"路径不可知 + 间接泄露检测"。

### R3 worktree 生命周期

**状态：已完成（2026-09-21，`d7bd28d` + `04088da`）。** 改动点 3 由 §7 E-1 决定：**不采用** codex 原生 `--worktree`。

- **动机**：创建未串行化；从不回收。
- **架构边界（见 §9）**：本波**只修生命周期**，不把集成机制从拷贝改为 git merge。git 只用于外轨里程碑锚点。
- **改动点**：
  1. 对 `git worktree add`（`orchestrator.py:10725`）加命名资源租约串行化（复用 R1 机制）。
  2. 新增回收：节点终态后 `git worktree remove` + `prune` + 分支删除；保留期可配。
  3. 是否改用 codex 原生 `--worktree` **取决于 R0-E5/E7/E8**：必须证明它不会自动删除证据、目录与分支名可控、并发安全，否则不采用。
- **验收**：并发创建不报错；终态节点后 worktree 与分支被回收；回收数量可观测；证据在回收后仍可读。
- **风险**：回收过早会毁掉证据（verifier/acceptance 需读 worktree 内产物）。回收必须发生在证据归档之后。

#### R3 落地记录（2026-09-21）

- **落点**：`api/app/worktrees.py`（判据 + 命名资源租约）+ `orchestrator._assign_worktree` / `_recycle_finished_worktrees`
  / `_recycle_one_worktree` / `_worktree_lifecycle_state` + `WatchdogStateResponse.worktrees`。
- **判据（默认保守，每条否决都写清原因）**：
  1. **路径守卫**：只回收 `<workspace>/worktrees/` 底下的目录。回收是删目录，写坏或被外部改过的
     `worktree_path` 不能让系统删仓库外的东西；
  2. **证据前置**（风险条款的硬前置）：节点**自己声明**的产物必须都能在归档处（它父节点的**集成目标**）找到。
     找不到就**不回收**，并记 `evidence`。**没有归档处可判时，只要有产物声明就整批算未判定**——
     不许因为"判不了"就当成已归档；
  3. **终态**：只回收 `done` / `canceled`。`failed` **一律不回收**——失败现场正是修复与复盘要读的证据；
  4. **保留期**：`CODEX_BATTLEFIELD_WORKTREE_RETENTION_SECONDS`（默认 86400 = 24h）；完成时间判不出来也不回收。
- **创建串行化**：`_assign_worktree` 的 git 段在**按目标仓库**取的租约下执行（不同仓库互不影响）；
  拿不到租约**不硬上**——退化成既有的拷贝目录路径，不阻塞调度。租约与 Godot 槽位同一套思路
  （非空锁文件 + OS 字节范围锁），因为要防的是同一类事。
- **回收动作**：git 目标走 `worktree remove --force` + `worktree prune`，并**只删我们自己那条** `codex/node-*` 分支
  （先 `branch --list` 确认存在再删）；拷贝式 worktree 直接删目录。删完路径还**在**就当失败，**不谎报"已回收"**。
  `remove` 失败时不再动 prune 与分支——删一半比不删更糟。留痕在 `node_meta["worktree_recycled"]`，因此幂等。
- **可观测**：`get_watchdog_state()["worktrees"]`（新模型 `WorktreeLifecycleBoard`）给出
  `with_worktree` / `recycled` / `recyclable_now` / `by_skip_reason`——**只说"还剩 12 个"没法判断是正常
  （保留期内 / failed 现场）还是卡住了（证据找不到，那本身就是要处理的问题）**。
- **测试抓出的两个真 bug**：
  1. `task.updated_at` 是 **naive UTC**，直接 `.timestamp()` 会按本地时区解释：在 UTC+8 下"刚完成"被算成
     8 小时前 → **保留期形同虚设**（本来该最保守的一条判据反而最先放行）。改用 `utils.coerce_utc`。
  2. Pydantic 默认 `extra="ignore"`：往 `get_watchdog_state` 的返回里加键，**不补模型字段就会被静默丢掉**——
     又一个"接线看着在、实际是死的"。补 `WorktreeLifecycleBoard` 才对。
- **验收（实测）**：`tests/test_worktree_lifecycle.py` 8 例（判据逐条可否决 + 租约排他与异常释放）、
  `tests/test_worktree_recycle.py` 9 例（真删目录 / **证据没归档就不删** / `failed` 不删 / 保留期不删 /
  回收后归档产物仍可读 / 连跑两次幂等 / 真 git worktree 的目录与分支都被回收 / 看板能解释"为什么还留着"）。
  `pytest` **751 passed / 0 failed**。

### R4 合并策略

**状态：已完成（2026-09-20 核实：`PLAN-STATUS` 原标的"待做"是过期标记）。** 识别是
`api/app/integration.py`（`detect_path_collisions` / `destructive_overwrite_conflict`，记 `kept` / `overwritten` /
`writers`），策略三档 `integration_conflict_strategy=overwrite|keep_both|repair`（`normalize_strategy`，
严重度由策略决定：`repair` 才阻断），明细在集成收尾由 `_record_parent_integration_obligations`
转成父节点轮次域债务（**用完整明细，不用被 `detail_limit` 截断过的 meta**，否则第 21 条之后会静默），
`collision_directive` 把冲突路径与"必须给结论"写进父节点验收提示词。证据：
`tests/test_integration_conflicts.py`（检测 / 明细 / 截断记账 / 指令 / 端到端 `parent.integration_conflict` 事件）、
`tests/test_obligation_gate.py`（O-A2/O-A3 债务门控）、`tests/test_obligations.py`（按策略定严重度）。

- **动机**：冲突静默 `skipped`，无法收敛。
- **改动点**：把 `9944-9962` 的静默跳过改为**结构化冲突记录**（哪些路径、为何跳过），上浮到父节点；提供可控策略（覆盖 / 保留两者 / 冲突即 repair）。
- **验收**：制造一次真实冲突，能在父节点看到冲突明细，且不再静默。

### R5 跨版本回归门禁

**状态：已完成（2026-09-20，`2314019`）。**

- **动机**：门禁全是单次状态检查，无版本比较。
- **改动点**：落地回归基线（`constitution_hash` + 产物清单 + 关键产物 hash 快照）；新增"合并前回归"门禁：重跑上一版基线中的验收断言，失败即转 repair。
- **验收**：人为破坏一个产物，回归门禁能挡住并给出 diff 定位。

#### R5 落地记录（2026-09-20）

- **落点**：`api/app/regression.py`（纯逻辑：产物快照 / 跨版本比对 / diff 定位文案）+ `obligations.py` 的
  `regression_break` 一族（`regression_severity` / `obligations_from_regression` / `settle_regression_restored`），
  接线在 `_accept_parent` 收尾——**登记债务必须在 `_obligation_gate` 之前**，重记基线必须在版本戳之后。
- **基线**：验收通过时记进 `node_meta["regression_baseline"]` = 版本号 + `constitution_hash` + **产物指纹**
  （sha256）+ 当时的验收断言（只作定位线索）+ 取指纹的根（诊断用）。产物来源**两处基准不同**，必须分开取：
  集成明细（`integrated_files`）相对**集成目标**（父节点自己的工作树，没有就是根目标路径），叶子自己的
  `changed_files` 相对**它自己的工作树**。混用会让基线指到不存在的文件 → 快照变空 → 门禁**静默失效**
  （"接线看着在、实际是死的"）。
- **比对时机**：只在**版本跨过去**时比（`regression.version_advanced`）。同一版内的返工本来就要改文件，
  拿它当回退会把门禁写成"拦住自己的修复"。
- **严重度**（`regression_severity`）：`missing`（上一版验收过的产物没了）**一律 blocking**；
  `changed` 由策略决定——`CODEX_BATTLEFIELD_INTEGRATION_REGRESSION_STRATEGY=record`（默认）只记录并上浮，
  `repair` 才阻断。理由与 `conflict_severity` 同源：字节变化本身可能正是修复动作。
- **有界性**：产物恢复到基线后由 `settle_regression_restored` 清债（**可核对的事实**，不是"谁完成了什么"），
  节点能正常 `done`；连续拦下 `obligation_gate_max_refusals` 次仍走既有的升级人工通路。
  它**刻意不进** `ROUND_SCOPED_TYPES`（这一轮没提到它不许清）也**不进** `VERSION_SCOPED_TYPES`
  （版本追平不等于产物又对了）——两者都是静默放宽的入口。
- **实现中被测试抓出的两处真问题**：
  1. 基线把"当时就不存在"的文件也记进去 → 之后每一轮都报同一条**假回退**，永远清不掉。
     基线只记**当时真的读到**的产物。
  2. 基线刷新只用"本轮集成明细" → 上一版验证过、这一轮没碰的产物被**悄悄移出防线**，下一版删掉它都不算回退。
     改成**累加**：上一版还在的产物继续留在基线里。
- **验收（实测）**：删掉上一版基线里的产物 → 父节点不得 `done`（`REPAIR` + `regression_break` blocking），
  事件理由**点名具体路径**（G5 的 diff 定位）；产物恢复 → 债按 `repair_child` 清掉、节点正常 `done`，
  基线重记到当前版本；`changed` 在默认档只上浮、`repair` 档阻断；同版本改动**不比**。
  `tests/test_regression_gate.py` 11 例；`pytest` **722 passed / 0 failed**。

### R6 失败复盘（父子共同，父主导）

**状态：已完成（2026-09-20，`ddce929`）。**

- **动机**：skill_note 已是父主导且要求读子树证据，但**排除失败/阻塞子树**——恰好丢掉最该总结的部分。
- **改动点**：
  1. 扩展 `_parent_skill_note_child:8682` 的采集范围，纳入 `block_report` / `blocked_child_reports` / `error_message` / `RunRecord.result_json`。
  2. 新增 `node_meta["failure_retrospective"]`，由父层主导写入，子层提供原始证据。
  3. 失败溯源可查询：为 `block_report.category` / `retryable` / `parent_action` 建立索引与聚合查询。
- **验收**：故意让一个子节点验证失败，复盘产物中必须出现该失败的经验条目，且能按类别跨树查出。

#### R6 落地记录（2026-09-20）

- **落点**：`api/app/failure_retrospective.py`（纯逻辑：证据抽取 / 三条线聚合 / 父层复盘 / 提示词文本块）
  + `skill_notes.parent_skill_note_child` 的采集范围 + `orchestrator._write_failure_retrospective`
  + `stats.failures`（`FailureBoard`）。
- **证据来源四条线都收**（按 spec 原文）：`node_meta["block_report"]`、`node_meta["blocked_child_reports"]`、
  `error_message`、`result_json`（worker 的失败状态）。抽出来的每条都带 `source` 字段说明它来自哪一条，
  理由是**截断后的**原文——复盘要能读出问题，不是把日志抄一遍。
- **父层主导写入、与 skill note 开关解耦**：父节点在"子节点全 `done`、即将整合验收"那一刻把**整棵子树**
  的失败证据收成复盘（`node_meta["failure_retrospective"]`）。刻意不看 skill note 的开关——
  复盘是"哪里出过错"的事实记录，不该因为"这轮没开 skill note"就不写。
  **没有失败条目时不写空壳**，并把旧的一并摘掉：空复盘会让人分不清"确实没失败"和"根本没采集"。
- **采集范围**：note 子任务的说明里改成"**失败/阻塞子树同样要归拢，不能只读已完成子树**"，
  并在有复盘数据时把 `FAILURE_RETROSPECTIVE`（条目 + 三条线聚合）直接塞进它的 brief；
  验收项追加两条（失败/阻塞子树各一条经验、每条写明 `category`/`retryable`/`parent_action`）。
  没有复盘数据时**要求本身保留**——否则"没采集"会被读成"没失败"。
- **复盘产物**：`docs/skill_notes/<parent>_merged.json` 里带 `failure_retrospective` 段
  （`_consolidate_parent_skill_notes` 在归拢时把父节点 meta 上的复盘并进产物）。
- **按类别跨树查出**：`get_tree` 的 `stats.failures` = 整棵树的 `by_category` / `by_retryable` /
  `by_parent_action` + 有界的最浅若干条明细（新增 `FailureBoard` 模型）。
- **全量跑抓到一个真回归**：`TreeResponse.stats` 是**有类型的 dict**，往里塞裸 dict 会被 Pydantic
  当成 `ConstitutionBoard` 校验并失败（8 例 API 用例当场变红）。补 `FailureBoard` 模型并加入联合类型后恢复。
  **教训**：往 `stats` 里加东西时，先加模型——"看着是个 dict"不等于能随便塞。
- **验收（实测）**：`tests/test_failure_retrospective.py` 12 例——四条证据来源各自的抽取、成功节点不进复盘、
  三条线聚合、明细有界、无失败不写空壳且摘掉旧壳、note brief 带证据（无数据时保留要求）、
  复盘产物里出现失败条目（G6 正身）、树级 `stats.failures` 可按类别查出。`pytest` **734 passed / 0 failed**。

#### 2026-09-21 审计修正（R1 / R3 / R5 / R6）

四路只读审计（每路盯一个波次的**实现**，不是盯文档）共确认 14 处缺陷，全部已修并各自带一条回归钉子。
**审计方法**：不问"做完了没"，只问"哪条判据能被反驳"——每条结论都要求 `file:line` + 触发序列。

**R1 排队仲裁**（`runner_tools.py` 内嵌的包装器脚本）：

1. **泄漏的等待者永不退场**：`_pid_alive` 对"已退出但句柄仍可 OpenProcess 的 pid"返回 True，pid 还可能被复用；
   老化又让老条目越等越靠前 → 一条僵尸条目永久占着排名第一，别人只能看着**已经空着**的锁等满
   `LOCK_WAIT_SECONDS` 然后超时退出（退出码 125，被上层读成容量阻塞）。修：`_abandoned`——
   比"存活上限 + 60s"更老的条目一律剪掉（真实等待者活不过上限，超时那条路它会自己 `drop`）。
2. **状态写非原子**：`write_text` 是"截断后写"，而读方 `_may_attempt_now` 不取 guard 锁 → 撞上写窗口会读到
   半截 JSON → 解析失败 → 那一刻**所有人一起放行**，优先级静默失效。修：换名落盘（`os.replace`）。
3. **一条坏 pid 让状态永久停更**：`_previous` 在剪枝**之前**执行且没有异常防御，遇到非数字 pid 直接抛，
   被最外层 `except Exception: return` 吞掉；而那条坏条目永远不会被剪掉 → 之后每次记账都在同一处 bail
   （不是 fail-open，是**停更**）。修：逐个 `try/except` 跳过。
4. **测试自己在撒谎**：`test_same_weight_stays_first_come_first_served` 读的是**权重**，两个都是 0 →
   顺序恒为 `[0, 0]`，断言永远通过、什么也没验证。修：改记**发起顺序**并断言 `[1, 2]`。
   （第一版改成记 pid，当场**假失败**：Windows 上 `sys.executable` 可能是转发器，`Popen.pid` 与子进程
   自己的 `getpid()` 未必相同——测试要断言的是"谁先发起"，不是"谁的 pid 是多少"。）

**R3 worktree 回收**：

5. **证据前置只查 `exists()`，被"同名旧文件"蒙过**（最贵的一条）：节点改的多半是工程里本来就有的文件，
   归档处当然存在这个路径 → 判"已归档" → worktree 被删 → 父节点随后集成时 `source missing`
   （默认档只上浮不阻断）→ **节点唯一一份产物静默丢失**。修：判据改成**指纹一致**
   （`utils.path_digest`：文件 `sha256:`、目录 `tree:`）；没有源可比对时一律算未判定。
6. **父节点没定格就回收**：`_integrate_child_outputs` 的源就是子节点的 worktree（`child_root = output_node.worktree_path`），
   整合发生在父节点验收里；`_revalidate_stale_leaves` 也会用 `-C <worktree>` 重跑 verifier。修：新增
   `parent_active` 判据。**不能拿 `TERMINAL_STATUSES` 当"父已完成"**——它含 `blocked`/`failed`，
   而这两者都可能被重新推起来接着整合。
7. **清扫顺序是浅先深**：父的 worktree 同时是子的归档处，先删父 → 子的证据判据随即失败、**永久**不可回收，
   而且看板把它报成"证据找不到（那本身就是要处理的问题）"——一条我们自己制造的假警报。修：按 depth 降序。
8. **`is_git` 判错层**：原来问"根目标是不是 git 仓库"。而 `_assign_worktree` 在拿不到租约或 `git worktree add`
   失败时会退化成**拷贝目录**（从没被 git 注册）→ 送进 `git worktree remove` 只会拿到
   `fatal: not a working tree` → 回收永久失败、目录永远留着，每轮还多一条失败事件。修：问**这个目录自己**
   有没有 `.git`。
9. 归档处已消失（父的 worktree 已被回收）时无法与"产物真的丢了"区分。修：单列 `archive_gone`，可诊断。
   **已知限制（如实记录）**：这种情况不会自动回收那个子目录，它会一直留着——保守方向，且原因可见。

**R5 跨版本回归门禁**：

10. **已清的回归债永不复活**：`upsert` 的"不复活"规则本来是为**轮次域**债（同一件事每轮都会重报）写的，
    而 `regression_break` 是**版本域**——同一个 `(kind, path)` 在 v2 与 v3 是两件独立的事。结果：一条路径被
    修好清债后，它在任何更晚的版本里**再次**坏掉都不会再阻断（README 写着 `missing` 一律阻断，实际静默放行）。
    修：新增 `REVIVABLE_TYPES`——基线版本前进后复活；基线没动则仍不复活（否则"清了又报"会把人拖进反复清债）。
11. **读盘失败被当成"产物没了"**：`snapshot_artifacts` 同时服务"记基线"和"比当前"，`OSError` 一律吞；
    比对时那是**假 `missing`**，而 `missing` 无条件 blocking → 一次瞬时占用（Windows 上很常见）就能把父节点
    打进修复，理由还写着"已缺失"。修：`unreadable` 单列，`diff_baseline` 把它排除出 `missing/changed`，
    理由与事件里注明"另有 N 处未判定"。
12. **同版本也判"跨版本"**：原实现的 hash 分支在"版本没动、hash 变了"时返回 True，与 README
    「同一版内的改动不比」相反，`repair` 档会拦下同一轮里的返工。修：版本相同**不比**、版本回退也**不比**
    （那份基线属于更晚的宪法）；只有版本号缺失时才退回 hash 判据。
13. **严重度首次生成即冻结**：档位从 `record` 切到 `repair` 后，已登记的 `changed` 债仍是 advisory →
    "看板报阻断、树照跑"。修：`upsert` 里严重度**只升不降**（降级不跟着走是刻意的：严重度一旦按旧档位
    记成阻断，让它自己掉回 advisory 就是静默放宽）。
14. **目录型产物从不进基线**：`snapshot_artifacts` 只认 `is_file()` → 整目录被删不算回退（且 R3 的目录类证据
    也判不了）。修：`utils.path_digest` 统一指纹（文件 / 目录），回归与 worktree **共用同一处定义**
    （同一语义两份实现必然漂移——W3 的教训）。

**R6 失败复盘**：

15. **`source` 永远取不到 `blocked_child_reports`**：原来的三选一里没有它，于是"父节点名下挂着阻塞子节点"
    （最常见的形态）被标成 `result_json`——这个节点根本没有 worker 结果，而按 `source` 过滤的人永远查不到
    这一类，与"四条线都收"对不上。修：四选一 + `derived_from_children` + `child_failure_ids`；
    汇总条目自己没有类别时从被引用的子节点取（写 `unknown` 会让按类别查询凭空多一个假类别）。
16. **树级看板重复计数**：父的汇总条目 + 子自己的条目 = 同一件事两次 → `total` 翻倍、`by_category` 多一个
    派生的 `unknown`，而"这棵树在哪些类别上失败过"正是它要回答的问题。修：`board()` 按被引用的子节点去重
    （被引用的子节点不在这批任务里时不丢，例如只看父这一层）。
17. **复盘块在主路径上从来是空串**：note 子任务在**规划期**创建，那时复盘还不存在；之后也不会重建
    （父下已有 note 子节点就直接早退）。也就是说"失败路径必须进经验库"从未真正落地，只有"父已有复盘 +
    父无 note 子节点"的补建路径能带出来。修：复盘写入时把块补进**未开跑**的 note 子任务描述
    （事件 `parent.failure_retrospective_injected`）；已开跑的不动（改不动一个正在执行的进程）。
18. **验收项要的字段，复盘块不发**：块里现在每条带 `source=`，并说清"被截断的部分按类别计数覆盖"，
    与验收项（`source_id` / `merged_count`）对齐。

**证据**：`pytest` **763 passed / 0 failed**（原 751，新增 12 例回归钉子）；前端 `pnpm lint && pnpm test && pnpm build` 全绿；
排版门禁 21 个 `.md` / 0 问题。

## 6. 验收契约（本 spec 的 G 系列）

沿用 `spec-modularization.md` §7 的反缩水原则，并追加：

| 编号 | 指标 | 门槛 |
|---|---|---|
| G1 | 后端测试 | `pytest` 0 failed，passed 数不低于 324 |
| G2 | R0 证据 | 六项实验的**原始输出**必须附上；禁止只给结论 |
| G3 | 容量可配可验 | 变更容量配置后，实测并发数随之变化（给出日志证据） |
| G4 | 隔离可反驳 | 提供**越权尝试失败**的原始输出，而不是"已隔离"的声明 |
| G5 | 回归门禁 | 人为破坏产物后门禁必须挡住（给出挡住的输出） |
| G6 | 失败复盘 | 制造真实失败后，复盘产物中必须出现对应条目 |
| G7 | 禁止事项 | 不得删除或放宽现有测试；不得以"环境限制"为由跳过 G4/G5/G6 |
| G8 | 判据失效方向 | 每条新判据必须写明**反常时往哪边倒**（放行/阻断）与**默认动作**，并给一条"**该断言会不会恒真/恒假**"的反面自检。依据：2026-09-21 审计的 15 处缺陷里，多数不是逻辑写错，而是方向没定或只看过一种输入 |
| G9 | 对抗性审计 | 每波收口做一次**只读**审计（见 `AGENTS.md` 的「对抗性审计」）：多路并行、每路只盯一个波次的实现、每条结论带 `file:line` + 触发序列，并显式区分"已确认 / 未能确认 / 已核实不是缺陷" |

## 7. 未决决策

| 编号 | 决策 | 推荐 | 理由 |
|---|---|---|---|
| E-1 | R3 是否改用 codex 原生 `Worktrees`（`--worktree`） | **已定：不采用**（见 §4 追加实测） | 主因收窄为两条：**`codex exec --json` 不上报工作树路径**（orchestrator 无法定位以跑 verifier/acceptance、集成 `changed_files`、归档证据）；**`--worktree` 与 `--ephemeral` 互斥**而 runner 默认开启。叠加 exec 模式不回收、detached HEAD、experimental。**早先"路径不可控"的说法已修正**：根位置可用 `$CODEX_HOME/worktrees/` 控制 |
| E-2 | 读隔离的实现层（codex 沙箱 / 私有数据根 / OS ACL） | 私有数据根优先 | 不依赖 codex 版本能力，且同时解决"读得到"与"知道路径" |
| E-3 | 优先级策略（静态优先级 / 老化 / 高优预留容量） | **已决：静态优先级（按角色）+ 老化**（2026-09-20 落地，见 §5 R1 第二轮）；高优预留仍不做 | 预留容量会降低利用率；老化可防饥饿。优先级取角色（门禁/验收 > 验证 > 实现）而不落库字段，避免迁移与 planner 改动 |
| E-4 | 本 spec 与 `spec-modularization.md` 的执行顺序 | **先做 R0 实测，再回 W2** | R0 零代码改动、成本极小，却决定 R2 的设计能否成立（读隔离可表达性）。前置验证先行，避免在假设上设计 |

## 8. 依赖决策登记

| 波次 | 需求 | 候选 | 决策 | 理由 |
|---|---|---|---|---|
| R1 | 跨进程资源租约 | 自建文件锁+JSON / 文件锁+SQLite / 外部队列 | 沿用文件锁+JSON | `orchestrator.py:2080-2111` 已证可行，零新依赖 |
| R3 | worktree 管理 | codex `--worktree` / 自建 `git worktree` | 待 R0-E5 | 见 E-1 |
| R5 | 回归基线存储 | 复用 `state_snapshots` / 新增产物 hash 清单 | 新增 hash 清单 | `state_snapshots` 语义是崩溃恢复，混用会污染两套语义 |

## 9. 架构决策：双轨（内轨拷贝式集成 / 外轨 git 里程碑）

### 9.1 决策

| 轨道 | 机制 | 粒度 | 凭据 |
|---|---|---|---|
| **内轨** | worktree per 节点 + `--cd` 单一工作根 + **拷贝式集成** + 字节精确校验 | 每节点 | 节点自身凭据，不外发 |
| **外轨** | git commit + 轻量 ledger + annotated tag +（可选）PR | root / 里程碑 / 子树完成 | **仅 orchestrator 持有**，节点永不接触 |

外轨不是新发明：`docs/checkpoint-git-anchor.md` 已实现「轻量 ledger 可提交 + 双仓库 tag + GitHub Desktop 推送」，本决策只是**明确它的粒度不许下沉到每节点**。

### 9.2 为什么内轨不引入 git merge（三条机制性理由）

**（1）与字节精确验收自相矛盾**

- 实测：`core.autocrlf = true`（仓库级）。
- 实测：`git diff --stat` 对多个文件发出 `warning: ... LF will be replaced by CRLF the next time Git touches it`。
- 而宪法反缩水条款明确把「用『Git 会规范化』解释差异」列为**不通过**。
- 现有烟测验收的是 **31 字节、无尾随换行**。用会改写字节的工具去满足字节精确门禁，方向相反。

**（2）与并行模型冲突，且资源文件会"干净地坏掉"**

- 子节点不只从父分叉，还被**依赖播种**（`_seed_worktree_from_dependencies:10480`）。这在 git 下等价于 cherry-pick DAG，复杂度显著高于拷贝。
- Godot `.tscn` / `.tres` 是「引用 + 子资源 id」结构，非行导向文本。两分支各自新增节点/资源 → `sub_resource id` 重复、`ExtResource` 引用错位。**3-way merge 会产出语法干净、语义损坏的结果**，比显式冲突更危险。
- 项目已被迫特判 `project.godot` 文本合并、readme 追加（`_copy_integrated_path:10594-10613`）。现在只需特判 2 种文件；换 git merge 则需为**所有**场景/资源类型解决同一问题。
- 13 节点并行 → 13 分支汇入父节点，冲突面积随并行度平方增长。

**（3）每节点 PR/CI 会破坏刚确立的 R2**

- 门禁需**秒级**决定"继续 or 转父节点"；PR + CI 是**分钟级**且引入网络失败面。
- 推送需要 token。**节点若能推送，每个节点都能读到 token**，直接违反「任务间隔离：同账号下互不越权」。若只有父层推送，则 GitHub 就不是每节点机制，只是检查点——那就应当老实按外轨的粒度用。

### 9.3 内轨：保留什么、改什么

| 保留 | 变更 |
|---|---|
| worktree per 节点 | 创建串行化（R1 租约） |
| `--cd` 单一工作根 + 不加多余 `--add-dir` | 新增回收与分支清理（R3） |
| 拷贝式集成 + 字节精确校验 | 冲突不再静默（R4） |
| `changed_files` 边界校验（`9944-9954`） | 增补"实际写入"越权检测（R2） |

### 9.4 若将来要转向 git 流：前置条件

1. `core.autocrlf` 必须置 `false`，并用 `.gitattributes` 显式声明，否则字节精确门禁不可用。
2. 必须先解决场景/资源文件的 3-way 合并语义，否则产出"干净地坏掉"的工程。
3. 必须给出依赖播种在 git 下的等价实现（cherry-pick DAG）。
4. 必须证明多会话并发 worktree 安全（§4 E8）。
5. 验收契约必须随之改写（`changed_files` 白名单 → commit 范围）。
6. 推送凭据必须继续只由 orchestrator 持有，否则破坏 R2。

以上任一条不满足，**不启动转向**。

