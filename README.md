# Codex 任务战场

Codex Battlefield 是一个本地控制台，用来管理动态递归的 Codex 任务树。它支持从高层目标开始，先生成项目宪法，再让每个节点判断自己是否能被单个 Codex CLI 以 300% 把握完成；不能完成就继续拆分；叶子验证失败一次就转父节点；父节点必须真实整合验收子节点成果后才允许完成。

> 接下来做什么：见 [ROADMAP.md](ROADMAP.md)（分阶段排期与判据）。

## 这是什么

一句话：**把一个大目标递归拆成一棵可验收的任务树，并且每一层都必须拿真实证据才能往上收**。

节点的完成条件不是"子节点都 done"，而是**入站债务为空 + 整合验收通过**；子节点的约束、版本与
证据沿委托链结构继承，不许改写重述。设计与判据见 `docs/README.md` 的阅读地图。

**不是什么**：它不替你写业务代码。它调度的是本机的 Codex CLI——没有 Codex CLI 时，可以用
**模拟 runner** 完整体验拆分、验收、修复、债务与看板（不调用任何外部模型，也不需要联网）。

## 设计前提

这个系统是按一种特定的开发方式设计的，机制长成什么样基本都是它的推论：

| 前提 | 含义 | 对应机制 |
|---|---|---|
| 模型**经济但够强** | 允许长时间多轮试错，但单次决策的可靠性低于"最强模型" | 判据全部可机器核对（债务表、版本戳、证据），不把"信任"当机制；**改写、缩写、重述一律按漂移处理** |
| **长时间**运行 | 一次跑几小时到几天，跨休眠、跨进程重启 | 额度池 + 自动休眠/自启动、机器安全守卫、检查点与 Git 账本、快照恢复 |
| **多 agent 并行** | 同时有若干 agent 在各自 worktree 里工作 | 单并发保底 + 空闲看门狗、按状态分治迁移、"执行中的不动、跑完再复查" |
| 无人盯着 | 出问题时人可能不在现场 | 阻断性债务 + 升级到人工、人工清债必须留 `actor`/`reason`、看板暴露"卡在第几层、卡了多久" |

一句话：**模型越便宜、跑得越久、并发越多，"靠信任"就越不成立**。所以这里的主线是
"把要求变成可核对的债务与证据"——父节点的完成条件是**入站债务为空 + 整合验收通过**，
而不是"子节点都 done"。

## 先决条件

| 组件 | 要求 | 说明 |
|---|---|---|
| Python | 3.12–3.13 | `api/pyproject.toml` 的 `requires-python` |
| Node | 20+ | Web 看板 |
| pnpm 或 npm | 任意版本 | 仓库带 `pnpm-lock.yaml`，用 pnpm 最一致 |
| Codex CLI | **可选** | 只有真实 runner 模式需要；模拟模式不需要 |
| Git | 任意 | worktree 隔离与检查点账本会用到 |
| 操作系统 | Windows 优先 | 脚本与路径判断按 Windows 写，测试在 Windows 上跑通 |

## 安装

API（Python）——**venv 建在 `api/` 下**：

```powershell
cd <repo>\api
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

> venv 放 `api/.venv` 是**约定位置**：看门狗与 goal 模式下的分离式 scheduler 都按这个位置找解释器
> （找不到会退回系统 `python`，而后者通常没装依赖，表现为"起了个进程立刻失败"）。
> 放在仓库根 `.venv` 也能跑——代码与脚本会把两种布局都探一遍。

Web（Node）：

```powershell
cd <repo>\web
pnpm install
```

## 5 分钟上手（模拟 runner，不需要 Codex CLI）

```powershell
# 1) 起 API
cd <repo>\api
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8765 --host 127.0.0.1

# 2) 另开一个窗口起 Web
cd <repo>\web
pnpm dev --host 127.0.0.1
```

> API 必须在 **8765**：Web 的 vite 代理（`web/vite.config.ts`）把它写成了
> `http://127.0.0.1:8765`。换端口就要同步改那里，否则页面能开、所有接口都失败。
> 后端**不兼服**前端构建产物（没有 StaticFiles）：`pnpm build` 出来的 `dist/` 需要你自己用静态服务器托管。

打开 `http://127.0.0.1:5173`，然后：

1. 填根目标，runner 选 **`模拟`**；
2. `生成草案` → `保存修订` → `锁定宪法` → `启动任务树`；
3. 观察左侧三块：**项目宪法**（含传播栏：`全部节点在 v1`）、**未清债务**（阻断/提示/卡了多久）、
   以及画布上节点的状态流转。

模拟 runner 会走完整状态机（拆分 → 执行 → 验证 → 整合验收 → 修复 → 债务 → 再验证），
适合先把链路跑通，再切到真实 Codex。

首次启动会自动创建 `workspace/`（SQLite 账本、跑批产物、检查点），该目录已 gitignore。

## 跑测试

```powershell
cd <repo>\api
.\.venv\Scripts\python.exe -m pytest -q        # 841 passed（2026-09-23 实测，`-n auto` 16 worker 约 93 秒）

cd <repo>\web
pnpm test && pnpm lint && pnpm build
```

测试库按 pytest 进程号分配（`tests/conftest.py`），所以**中途 Ctrl-C 或强杀一次测试运行不会
影响下一次**，两个终端同时跑也不会互相踩。这条是特意修的：写死一个共享测试库时，被杀掉的运行
留下的 `-wal`/锁会让下一次运行静默挂在第一个用例上（干净克隆里复现过）。

## 目录结构

```
api/app/        调度器（orchestrator.py）、runner、以及按判据拆出的纯模块
                （obligations / constitution_versions / constraints / integration / blockers …）
api/tests/      841 个用例、51 个文件（2026-09-23 实测）：纯模块层与接线层分开测，接线类用例断言"拦截不创建子任务"等硬约束
web/src/        React 看板（React Flow + elkjs 画树）
docs/           设计文档与实施状态；先看 docs/README.md 的阅读地图
scripts/        运维脚本：看门狗、检查点 Git 锚点、历史隐私扫描等
skills/         随仓库发布的 Codex 技能说明
workspace/      运行时状态（已 gitignore，首次启动自动创建）
```

## 换一台机器

两件事要分清，代价完全不同：

| 目的 | 需要什么 |
|---|---|
| **用这套工具开新任务** | clone 本仓库 → 按上面「安装」装依赖 → 起 API/Web。就这些。默认 `mock`，**不需要 Codex CLI，也不需要 GitHub 凭据**（除了 clone 私有仓库本身） |
| **接着现在这棵树继续跑** | 还要一起搬 `workspace/`（SQLite + worktrees + 检查点）**和目标工程目录**，并且**尽量保持同样的绝对路径**：数据库里存的是绝对路径（`worktree_path`、宪法里的 `target_path`），换到不同盘符或不同用户目录后这些会失效 |

其余要点：

- **GitHub 只用在两处**：clone 这个私有仓库、以及（可选、手工）推送快照。
  **程序本身从不 push**——`git push` 只出现在文档里，不在代码里。
- **Codex CLI 只在真实 runner 模式下需要**：新机器上装好 `codex` 并完成它自己的登录
  （凭据默认在 `~/.codex`，可用 `CODEX_BATTLEFIELD_CODEX_HOME` 指定）；模拟模式全程不需要它。
- **平台**：Windows 优先。看门狗安装、外部 codex 进程探测这类功能是 Windows 专属
  （代码里有 `os.name == "nt"` / `sys.platform.startswith("win")` 分支，非 Windows 走另一条路，
  但**没有在 macOS/Linux 上验证过**）。Godot 校验、PowerShell 相关工作流同理。
- **仓库里不带环境**：`.venv/`、`node_modules/`、`workspace/` 都不入库，新机器上重新装、重新生成。
  工作目录是从仓库位置推出来的（`config.py` 用 `__file__` 求 `ROOT_DIR`），所以 clone 到哪个目录都能跑。
- **新机器上装完依赖，先自检这两个位置**：`api/.venv`（约定位置）与仓库根 `.venv`。二者都没有时，
  看门狗与分离式 scheduler 会退回系统 `python`，那是长跑里最容易被忽略的一种停摆。

## 技术栈

- API：FastAPI、SQLite、SQLAlchemy、Pydantic
- Web：Vite、React、TypeScript、React Flow、elkjs
- Runner：Mock runner 用于低成本测试；真实 runner 通过 `codex exec` 调用本机 Codex CLI

## 启动之后

看板中的启动顺序：

1. 填写根目标和目标路径。
2. 选择 `模拟` 或 `真实 Codex`。
3. 点击 `生成草案`。
4. 按需修订项目宪法 Markdown 和结构化边界 JSON。
5. 点击 `保存修订`，确认无误后点击 `锁定宪法`。
6. 宪法锁定后再点击 `启动任务树`。

任务树跑起来之后，左侧面板的三块值得盯：

- **项目宪法**：当前生效版本 + 传播状态（几个版本并存、多少节点落后、其中多少是"执行中必须等"）。
- **未清债务**：阻断/提示计数、按类型的分布、**最早一条卡了多久**（区分"在干活"和"卡死"）。
- **跨树债务索引**：债务的**派生**缓存（`obligation_index` 表）按类型跨树聚合，读面是
  `GET /api/obligation-index?status=open|settled|all`。它是缓存**不是**事实来源：索引没建过
  （`built_at` 为空）或已跟 `node_meta` 漂了（`in_sync=false`）时，`total` **不构成读数**，
  界面会明说"这里的 0 不代表没有债"。重建（唯一写入口）：`scripts/rebuild_obligation_index.py --rebuild`；
  只查漂移：`--check`（漂移时退出码 1）。
- 节点详情里的 `obligations`：每条债的 id、类型、严重度、来源节点。人清债走
  `POST /api/tasks/{id}/obligations/settle`（必须给 `actor` 与 `reason`）。

## 默认耐用模式 Codex CLI

默认真实 runner 使用 Codex CLI 的账号默认档位，适合递归探索长跑：

```powershell
$env:CODEX_BATTLEFIELD_RUNNER="real"
$env:CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START="1"
$env:CODEX_BATTLEFIELD_PREVIEW_URL="http://127.0.0.1:5173/"
```

默认耐用模式不设置 `CODEX_BATTLEFIELD_CODEX_MODEL` 和
`CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT`，由 Codex CLI 使用账号当前默认档位。
如果某次任务确实需要强模式，再手动设置这两个环境变量。

从 Web 看板点击 `启动任务树` 时，前端会自动打开一个新的观察页，并把当前页与观察页都绑定到新建任务树的 `root_id`。从 API 或脚本启动任务树时，设置 `CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START=1` 后，后端也会自动打开对应的 `?root_id=...` 预览页。

## 自动休眠、自启动和保留额度

长时间递归探索时，推荐让真实 Codex 并发保持 `1`，再用启动预算控制一轮最多启动多少个 Codex agent：

```powershell
$env:CODEX_BATTLEFIELD_MAX_CONCURRENCY="1"
$env:CODEX_BATTLEFIELD_AGENT_BURST_LIMIT="8"
$env:CODEX_BATTLEFIELD_AGENT_BURST_RESERVE="1"
$env:CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS="18000"
```

含义：

- `AGENT_BURST_LIMIT=8`：这一轮最多按 8 次 Codex agent 启动来估算池子。
- `AGENT_BURST_RESERVE=1`：其中 1 次保留给自动恢复钩子或人工接管。
- 实际工作额度会显示为 `已用/7`；到 7 后任务树自动保存状态并休眠。
- `AGENT_BURST_RESUME_AFTER_SECONDS=18000`：5 小时后自动恢复，恢复时会把本轮启动计数清零。

如果你已经确认账号额度充足，希望像 Codex Goal 一样长跑到收敛，可以开启 Goal 长跑模式：

```powershell
$env:CODEX_BATTLEFIELD_GOAL_MODE="1"
$env:CODEX_BATTLEFIELD_AGENT_BURST_LIMIT="0"
$env:CODEX_BATTLEFIELD_AGENT_BURST_RESERVE="0"
```

Goal 长跑模式不会限制本轮 Codex agent 启动次数；仍然保留单并发、空闲看门狗、资源阻塞自动休眠和本地自启动巡检。Web 看板的「战场统计」和「自动续跑」面板会显示当前是 `长跑` 还是 `保守`。

### 失败重试能花掉多少额度

一次 agent 启动内部如果失败，真实 runner 会重试，次数与等待都由这几个变量决定：

```powershell
$env:CODEX_BATTLEFIELD_CODEX_RETRY_ATTEMPTS="2"                          # 最多 2 次重试 = 最多 3 次尝试
$env:CODEX_BATTLEFIELD_CODEX_RETRY_BASE_DELAY_SECONDS="8"                # 指数退避基数
$env:CODEX_BATTLEFIELD_CODEX_RETRY_MAX_IN_PROCESS_WAIT_SECONDS="300"     # 能在原地等的窗口上限
```

等待时间分两种情况：

- **CLI 没给窗口**（如 502、`service unavailable`）：指数退避——8s、16s、32s…，每一次等待后都要真的再拉起一次 CLI。
- **CLI 明说了窗口**（如 `usage limit ... retry after 10800 seconds`）：按 CLI 给的窗口等。
  窗口 ≤ 上限就在**原地等到点**（一条 60s 的 429 不值得让整棵树休眠）；窗口 > 上限则**放弃进程内重试**，
  把窗口写进块报告交给上层休眠——否则会等 8 秒再拉一次 Codex，额度没回来，却白花一次启动。
  设为 `0` 表示一律不进程内重试，全部交给休眠。

**额度口径**：`AGENT_BURST_LIMIT` 的预算按**逻辑阶段**计数（一次 planner/worker/verifier/acceptance 各算一次），
阶段内部的重试**不额外计数**。所以按预算估额度时，上限要乘上 `1 + RETRY_ATTEMPTS` 才是真实最多的
Codex CLI 调用次数；要严格卡额度就把 `RETRY_ATTEMPTS` 调小，或把 `AGENT_BURST_LIMIT` 留在默认档以下。

## 资源保护与递归边界

### 真实 Godot 的槽位排队：谁先拿

同一时间能跑几个真实 Godot 由 `CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY` 决定（默认 `1`）。
槽位满了以后，**不是谁先轮询到谁拿**，而是按优先级排队，并且用老化保证没人被饿死：

```powershell
$env:CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY="1"      # 默认开；设 0 回到纯先来后到
$env:CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS="300"  # 每等这么久，等效优先级 +1；设 0 = 不老化
```

- **静态优先级按角色**：`acceptance`（成品门禁）3 > `verifier` 2 > `worker` 1 > `planner` 0。
  理由：门禁/验收卡住会让整棵树都不动，而 worker 的探索性运行可以等一会儿。
- **老化防饥饿**：等得越久等效优先级越高，所以长等低优最终会超过新到的高优；
  同时在等的两个人同速上涨，不会互相改变差距。
- **泄漏条目会退场**：真实等待者活不过等待上限（超时那条路它会自己退场），所以比上限更老的条目一律剪掉。
  没有这条，一个"进程已走但 pid 仍可打开"的僵尸条目会永久占着排名第一——老化还让它越来越靠前——
  于是后来者只能看着**已经空着**的锁等满超时。
- **容量上限不受影响**：槽位互斥由操作系统锁保证，优先级只决定"谁先去抢"，**任何情况下都不会超容量**；
  读不到排队状态时一律放行（仲裁是优化，坏了不能让人永远等）。状态文件换名落盘，读方不会读到半截 JSON。
- **看板可见**：等待者带上权重、等待时长与等效优先级，槽位状态里还写着当前生效的队列策略。

### 跨版本回归门禁：上一版验收过的产物不能悄悄坏掉

宪法改版（v1→v2）之后，上一版已经验收过的产物如果坏了，光看"这一轮验收通过"是发现不了的——
所以每次验收通过时会记一份**产物指纹基线**，跨版本再验收时拿它比对：

```powershell
$env:CODEX_BATTLEFIELD_INTEGRATION_REGRESSION="1"                 # 默认开
$env:CODEX_BATTLEFIELD_INTEGRATION_REGRESSION_STRATEGY="record"   # record（默认）| repair
```

- **`missing`（上一版验证过的产物没了）**：一律阻断验收 → 转修复。正常返工不会悄悄删掉已验收产物。
- **`changed`（内容变了）**：`record` 档只记录并上浮；`repair` 档才阻断。字节变化本身可能正是修复动作，
  默认档拦下就等于把默认行为悄悄改成阻断档。
- **同一版内的改动不比**（版本号没动、连 `constitution_hash` 变了也不比）：基线每次验收通过都会重记，
  所以正常返工不触发门禁——否则门禁第一个拦下的就是它自己的修复子任务。版本**回退**同样不比：
  那份基线属于更晚的宪法。
- **第二次坏掉照样挡**：回归债是**版本域**的——同一条路径在更晚的版本里再次坏掉是另一件事，
  所以已清的回归债会在基线版本前进后**复活**；基线没动则不复活（否则"清了又报"会让人反复清债）。
- **读不了 ≠ 没了**：文件被别的进程占用时不计入回退（读盘失败不是"产物缺失"），但会写进理由与事件
  （"另有 N 处未判定"）——一次瞬时占用不该把父节点打进修复。
- **目录也算产物**：整目录被删/内容变了同样算回退（指纹按目录树算）。
- 产物恢复后债务自动清掉（依据是"产物又和基线一致了"这个可核对的事实）；切档位从 `record` 到 `repair`
  时，已登记的债会跟着升级为阻断（但**不会**因为切回 `record` 就自动降级）；连续拦下超过
  `OBLIGATION_GATE_MAX_REFUSALS` 次仍会升级为人工审查，不会无限轮回。

### 失败复盘：失败路径也要进经验库

父层 skill note 原先只要求"读已完成子树"，恰好把最该总结的失败路径排除在外。现在的口径是
**失败/阻塞子树必须各有一条经验**：

- 证据来源四条线都收，**每条都标注它来自哪条线**（`source` = `block_report` / `blocked_child_reports` /
  `error_message` / `result_json`）：父节点名下挂着阻塞子节点是最常见的形态，没有这个标注就没法按来源查。
- 父节点在"子节点全完成、即将整合验收"时把**整棵子树**的失败证据收成复盘，写进
  `node_meta.failure_retrospective`，并在复盘产物 `docs/skill_notes/<父节点>_merged.json` 里带出这一段；
  复盘块还会**补进当时还没开跑的 skill note 子任务**（那个子任务是在复盘产生之前创建的，
  不补这一刀，"失败路径必须进经验库"在主链路上其实从未生效）。
- 按 `category` / `retryable` / `parent_action` 聚合，看板的 `stats.failures` 可直接按类别回答
  "这棵树在哪些类别上失败过"。父节点的汇总条目与被引用的子节点条目**只算一次**——否则同一件事翻倍，
  还会凭空多出一个派生的 `unknown` 类别。
- 没有失败**不写空壳**——否则分不清"确实没失败"和"根本没采集"。

### worktree 生命周期：终态之后才回收，回收前先确认证据已归档

每个节点在独立 worktree 里干活（git 目标下是真 `git worktree` + `codex/node-<id>` 分支，
非 git 目标下退化成拷贝目录）。它们**不会**只增不减：

```powershell
$env:CODEX_BATTLEFIELD_WORKTREE_RECYCLE="1"                 # 默认开
$env:CODEX_BATTLEFIELD_WORKTREE_RETENTION_SECONDS="86400"   # 保留期，默认 24 小时
$env:CODEX_BATTLEFIELD_WORKTREE_RECYCLE_LIMIT="20"          # 每轮清扫最多回收几个
```

回收的五条判据（任一不满足就**留着**，并记下原因）：

1. **路径守卫**：只回收 `<workspace>/worktrees/` 底下的目录——写坏的工作树路径不能让系统删仓库外的东西；
2. **父节点已定格**：父节点还在跑（或在 `blocked`/`failed` 等**可能被重新推起来**的状态）就不回收——
   整合的源就是这个 worktree（`child_root = output_node.worktree_path`），再验证也会在它里面重跑 verifier；
3. **证据前置**：节点声明的产物必须能在父节点的集成目标里**对上内容**（文件按 sha256、目录按目录树指纹），
   只是"同名路径存在"不算——节点改的多半是工程里本来就有的文件，只查存在性会把"旧版本还在那儿"读成
   "已归档"，删掉的就是这个节点唯一一份新产物；
4. **终态**：只回收 `done` / `canceled`。`failed` **一律不回收**——失败现场正是修复与复盘要读的证据；
5. **保留期**：默认 24 小时，且完成时间判不出来时也不回收（不拿"现在"冒充完成时间）。

另外：`git worktree` 的创建与回收都在**按目标仓库**取的命名租约下做（拿不到租约就不硬上 git，
退化成拷贝目录，不阻塞调度）；回收只删我们自己那条 `codex/node-*` 分支，且删完还**在**就当失败，
不谎报"已回收"。**清扫按深度从深到浅**做（父的 worktree 是子节点的归档处，先删父会让子永久不可回收）；
"这是不是 git worktree"按**目录自己**有没有 `.git` 判——退化出来的拷贝目录送进 `git worktree remove`
只会拿到 `fatal: not a working tree`，回收永久失败。看板的 `/api/watchdog/state` 里 `worktrees` 一节
写明还有多少、已回收多少、以及**为什么还被留着**（`evidence` / `parent_active` / `retention` /
`archive_gone` 等）。

### 按角色分层模型：便宜的执行 + 强的验收

```powershell
$env:CODEX_BATTLEFIELD_CODEX_MODEL_ACCEPTANCE="deepseek-v4.1"   # 验收/门禁用强档
$env:CODEX_BATTLEFIELD_CODEX_MODEL_WORKER="deepseek-flash"      # 执行用便宜档
```

- 五个角色各自可配：`constitution` / `planner` / `worker` / `verifier` / `acceptance`；**空则回退全局**
  `CODEX_BATTLEFIELD_CODEX_MODEL`。推理档同族（`CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_ACCEPTANCE` 等）。
- `GET /api/watchdog/state` 的 `model_tiers` 报出**已解析**的配比（空档显示成"全局默认档"，不是空串）
  与可疑之处。它刻意**不**给模型排强弱——名字里没有强度信息，硬排就是假判据。
- 为什么重要：**`acceptance` 是唯一不能降级的角色**（它决定能不能往上收）。"便宜模型干活 + 强模型验收"
  是 flash 级模型吃大工程的前提。

### 验收面冻结：产出方不能改自己正要通过的考卷

宪法 `data.acceptance_surface` 声明"哪些文件是判据"（测试、校验脚本）。根节点创建（= 宪法已锁定）与
**人工**修订宪法时把它们的内容指纹冻结下来；整合验收前比对：

- **内容变了 / 没了** → **无条件阻断**（`acceptance_surface_tampered`，与任何档位无关）→ 转修复；
- **读不了**（被占用/权限）→ **不判定**：单列 + 事件，不阻断（一次瞬时占用不该把父节点打进修复）；
- **没声明** → 不判（看板/事件里如实显示"未声明"，不许读成"已通过"）；**恢复了** → 按事实清债。

修订宪法**只在声明的路径清单变了**时重新冻结：内容变了不许靠一次无关的修订洗白。

### 需求→证据追溯：漏需求第一次变成可拦的东西

宪法 `data.requirements` 把每条原始要求绑到可核对判据上：

```json
{ "id": "R1", "text": "击杀后回到待机",
  "criteria": [{ "id": "c1", "type": "contains", "path": "scripts/player.gd", "pattern": "state = IDLE" }] }
```

- **要求没有判据** = 漏需求 → **阻断**；**判据没过** = 没做到 → **阻断**；**判据判不了**（越界/读不到/超时）
  → 不判定（单列 + 事件）；**没声明** → 不判。
- 判据词表与能力基准共用一处（`file_exists` / `contains` / `not_contains` / `command`）。
- 它回答的是"宪法**符合目标**吗"；前面那些门禁回答的是"实现**符合宪法**吗"。

### 能力基准：换模型 / 换 harness 是变强还是变弱

```powershell
python scripts\bench_run.py --provider mock     # 零 token，固定任务集 + 外部判据
python scripts\bench_run.py --provider real     # 真实 Codex CLI
python scripts\bench_run.py --compare bench\reports\a.json bench\reports\b.json
```

两条铁律：**成功率只由外部判据算**（树自称完成只进 `tree_claimed_done`，两者之差就是"谎报"次数）；
**故意做不完的任务一旦声称完成，该档整体判不可信**。任务集与判据见 `docs/spec-capability-benchmark.md`。

### 任务间隔离：节点之间拿不到对方的东西

同一账号下，节点之间互不越权。**大部分是"本来就成立"，只是没人把它写成判据**：

| 面 | 现状 | 靠什么 |
|---|---|---|
| 写边界 | 做到 | `--cd <自己的工作树>`，且命令行里**没有** `--add-dir`（有测试断言） |
| 读对方产物 | 做到 | codex 沙箱把读限在 `--cd` 根内：读兄弟目录 → `Access is denied`（`codex-cli 0.154.0` 实测） |
| 凭据（磁盘） | 做到 | **每节点隔离 `CODEX_HOME`**（`<codex_home>/nodes/<节点id>`），该目录里没有 `auth.json` |
| 宿主上无关的凭据 | 做到 | 子进程环境**默认收窄**（见下） |
| 环境变量里的凭据 | **做不到** | `env_key` 模式只能经环境变量下发（`docs/spec-task-isolation.md` §3 如实列出） |

```powershell
$env:CODEX_BATTLEFIELD_CODEX_ENV_STRICT="1"        # 默认开：不把宿主上无关的凭据交给节点
$env:CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES="CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_"
```

- 收窄实测：宿主上三个无关平台 token（宿主平台凭据等）**不再下发**；环境从 100+ 键收到 54 键；
  并且**用真实 CLI 验证过收窄后照样能跑**。要回到老行为设 `..._ENV_STRICT=0`。
- **越权留痕**（侦探，不是防线）：节点产物里出现兄弟节点 worktree 路径 → 记一条 **advisory** 债
  `cross_node_leak`（不阻断：文本里出现路径 ≠ 真的读了它）。
- 定期重测：`python scripts\verify_isolation_boundary.py --real --codex-env-check`
  （读隔离是**沙箱**给的能力，升级 CLI 后必须重跑，否则"隔离还在"只是假设）。

### 机器安全守卫

每次启动新的 Codex agent 前，系统会用**当前空闲物理内存**与阈值比较；低于阈值时整棵树会休眠一段时间，避免本机被拖垮：

```powershell
$env:CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB="4096"
$env:CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS="1800"
```

- 默认阈值 4096 MB，默认休眠 30 分钟后尝试恢复。
- 该守卫**不受 Goal 长跑模式影响**。对同时开着 IDE、浏览器和多个服务的开发机，4096 MB 很容易被跨过——此时任务树会在启动 agent 时反复休眠。
- 如果本机空闲内存长期低于默认阈值，请按实际情况调低该值，或设为 `0` 关闭守卫。

### 递归深度与宽度

本系统**没有固定最大深度和最大宽度**，这是有意设计：是否继续拆分由每个节点依据项目宪法和「300% 可完成铁律」自行判断。防失控依靠内容级信号（重复子任务签名、资源阻塞、启动预算），而不是深度/宽度上限——深度上限要么砍掉真实工作，要么拦不住烧额度的循环。

早期版本存在 `CODEX_BATTLEFIELD_MAX_DEPTH` 和 `CODEX_BATTLEFIELD_MAX_CHILDREN`，但它们从未被任何代码读取，已移除。

### 父节点整合验收次数

父节点连续 acceptance repair 的次数上限：

```powershell
$env:CODEX_BATTLEFIELD_MAX_ACCEPTANCE_REPAIR_CYCLES="2"
```

**默认 `2`**（2026-09-22 起；以前默认 `0` = 不限）：达到该次数即转入人工审查状态——铁律 8 要的就是这条保护，
默认不限等于把它默认关掉。`0` 仍然是"不限次"这个语义本身，要旧行为就显式设 `0`。
当前生效值可在 `GET /api/watchdog/state` 的 `acceptance_repair_cycle_limit` 字段查看。

一次性检查并拉起服务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\battlefield_watchdog.ps1 -RootId <root-id> -AgentBurstLimit 8 -AgentBurstReserve 1
```

Goal 长跑模式的一次性巡检写法：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\battlefield_watchdog.ps1 -RootId <root-id> -GoalMode
```

注册 Windows 定时看门狗，默认每 1 分钟检查一次 API/Web 是否还活着（间隔由 `CODEX_BATTLEFIELD_WATCHDOG_INTERVAL_MINUTES` 控制，最小 1 分钟）；如果休眠到期但内存排程丢失，会补打一遍恢复钩子：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\register_battlefield_watchdog_task.ps1 -RootId <root-id> -IntervalMinutes 10 -AgentBurstLimit 8 -AgentBurstReserve 1
```

Goal 长跑模式注册看门狗：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\register_battlefield_watchdog_task.ps1 -RootId <root-id> -IntervalMinutes 10 -GoalMode
```

如果希望电脑重启后重新登录也自动巡检一次，加上登录钩子：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File <repo>\scripts\register_battlefield_watchdog_task.ps1 -RootId <root-id> -IntervalMinutes 10 -AgentBurstLimit 8 -AgentBurstReserve 1 -WithLogonTrigger
```

脚本会优先注册 Windows `ONLOGON` 计划任务；如果当前权限被系统拒绝，会自动退回到当前用户的「启动」文件夹写入一个 `.cmd` 钩子。

自启动钩子的判断入口：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/watchdog/state?root_id=<root-id>"
```

这个接口会返回当前 root、是否休眠到期、是否应该自动恢复、剩余启动预算和推荐动作。计划任务会优先使用它；如果传入的 root 已经结束，会自动发现最新的活跃 root。如果 API 重启后后台调度丢失，但数据库里仍有超过 5 分钟未推进的 queued/planned 前线任务，它也会建议 `resume_orphaned` 自动续跑。每次后台检查都会写入：

```text
<repo>\workspace\watchdog.log
```

Web 看板左侧的「自动续跑」面板会显示同一份状态，包括推荐动作、休眠原因、启动预算、预计恢复时间和剩余等待时间。

真实 runner 会调用 `codex exec -`，通过 stdin 传入 prompt，并保存：

- prompt
- JSONL 日志
- last-message
- 工作目录
- 宪法 hash

这些都能在右侧节点详情的运行记录里预览。

## 长时间运行的磁盘占用

长时间自主运行最先撞到的常常不是 token，而是**磁盘**。实测一个中等 Godot 目标，`workspace/` 合计约 **16.8 GB**：

| 目录 | 实测 | 会自动回收吗 |
|---|---|---|
| `worktrees/` | 6.8 GB | **不会**。每个叶子一个完整检出，而且树的推进**依赖它们**（父节点要整合子节点产物）——即使节点已 done，它的 worktree 也可能被父节点的整合/再验证用到。所以**只在整棵树进入终态后**才适合回收 |
| `full_checkpoints/` | 9.6 GB（15 份） | 会：按 `CODEX_BATTLEFIELD_FULL_CHECKPOINT_MAX_COUNT` 保留（默认已从 48 降到 12）。每份都含数据库拷贝 + 目标工程拷贝，所以占用 ≈ 份数 × 目标体积 |
| `runs/` | 414 MB | **不会**。只用于查看运行日志与产物预览，删掉不影响推进，可定期清 |
| `checkpoint_ledger/` | 0.3 MB | 轻量账本，留着 |
| `watchdog.log` | 2.5 MB | 会：超过 8 MB 自动轮转，只保留一份 `watchdog.log.1` |

回收顺序建议：先清 `runs/` 里的旧目录（安全）→ 整棵树进终态后再清 `worktrees/` → 还需要空间时手工删旧检查点
（**保留最新那份**，恢复只用最新）。不确定就先把 `workspace/` 整个留在原地，只删其中最老的那几类。

## 全套验证

不消耗 token 的完整验证：

```powershell
cd <repo>
python .\scripts\verify_all.py
```

真实 Codex CLI 文件修改烟测：

```powershell
python .\scripts\real_codex_smoke.py --timeout 600 --constitution-runner real
```

真实递归链路探针：

```powershell
python .\scripts\real_recursive_probe.py --timeout 900 --constitution-runner real
```

"中枢挂掉后能不能自己接着干"这条安全网（不花 token；会自动造一棵停住的树、把最后活动时间往回拨，
验证看门狗认得出孤儿树、且 `kick` 能把它推回推进，最后清理掉验证用任务树）：

```powershell
python .\scripts\verify_orphan_resume.py
```

一条命令跑本地验证 + 真实 Codex 两条链路：

```powershell
python .\scripts\verify_all.py --real-smoke --real-recursive-probe --real-constitution --timeout 900
```

最新通过证据见 `<repo>\VERIFICATION_STATUS.md`。当前最新真实 smoke 和递归 probe 都使用了真实宪法生成。

## API

任务树：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/tree"
```

自动化看门狗状态：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/watchdog/state"
```

节点详情：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/tasks/<task-id>"
```

运行调试文件：

```powershell
Invoke-RestMethod "http://127.0.0.1:8765/api/runs/<run-id>/artifact?kind=prompt"
Invoke-RestMethod "http://127.0.0.1:8765/api/runs/<run-id>/artifact?kind=logs"
Invoke-RestMethod "http://127.0.0.1:8765/api/runs/<run-id>/artifact?kind=last_message"
```

## 当前验证证据

详见：

- `<repo>\FINAL_GOAL.md`
- `<repo>\VERIFICATION_STATUS.md`
