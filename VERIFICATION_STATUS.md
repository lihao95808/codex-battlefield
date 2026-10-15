# Codex Battlefield 当前验证状态

更新时间：2026-09-24（最新一次见下面的「2026-09-24 索引读面审计修正 + 端到端实证」；更早的记录保留原文）

> **时效说明**：2026-06-16 那一节是**那一天**的一次完整验证记录，数字是**当时的规模**（当时后端 18 例、前端 2 例）。
> 用例规模此后长了很多（当前规模见 `README.md` 的「跑测试」一节）。那里保留原文不改数字——
> 它记的是"那天跑过什么"，改成今天的数字就不再是证据了。

## 2026-09-24（A6）浏览器 console = 0：从"推理"变成"测量"（最新一次）

> 这一节记"用真 Chrome 把长期挂着**未验证**的 A6 测掉"这一轮。零 token。
> 与下面几节并列，不覆盖。量具：`scripts/verify_browser_console.py`（headless Chrome + CDP）。

### 环境（读数的前提，写下来才可复现）

| 项 | 值 |
|---|---|
| 浏览器 | `C:\Program Files\Google\Chrome\Application\chrome.exe`，`--headless=new` |
| API | 真 `uvicorn app.main:app` @ `127.0.0.1:8766`，库是**开发库的副本** `workspace/_a6_board.sqlite3`（3291 节点，不污染开发库） |
| 前端 | `vite` dev @ `localhost:5175`（仓库默认 5173→8765，本机 8765 被别的程序占着，故用临时配置换了一对端口） |
| 观察窗 | 导航后再等 **45 秒**（先用 10 秒跑过一次，那次树还没渲染完 —— `/api/watchdog/state` 实测 **13 秒**） |

### 读数

| 判据 | 结果 |
|---|---|
| 渲染证据 | `h1='L4 Navigator'`、body **4038** 字符、**42399** 个元素、23+ 个按钮 → **成立**（2264 节点的树真渲染完了） |
| 收集器自检（反面自检） | 注入的 `console.error(<哨兵>)` 抓到 **1** 次 → 监听面是活的 |
| **console error（排除哨兵）** | **0 条** |
| warning | 0 条（不进判据，仅留证） |
| `Network.loadingFailed` | 1 条：`net::ERR_ABORTED` / `type=EventSource` / `canceled=true` —— 关浏览器时 SSE 被取消，不进判据 |
| 证据文件 | `workspace/_a6_console_full.json`（全量事件 + DOM 摘录）、`workspace/_a6_board_full.png`（首屏截图） |

### 两个"方向"实测（不是推理）

- **首轮读数是 1 条而不是 0**：`GET /favicon.ico` 404（页面没声明图标 → 浏览器自己去要）。
  **没有**在量具里给 favicon 开后门（那是静默放宽判据），而是补了 `web/index.html` 的内联 data URI 图标，
  重跑为 0。
- **对着 `about:blank` 跑**：`h1=None` / body 0 字符 → 脚本 **exit 3 判失败**，而不是报"0 条 error 通过"。
  与此同时收集器自检仍抓到 1 次哨兵 —— 也就是说"监听是活的"与"页面是空的"被分开报了。
  这正是这条判据最贵的误读方向，实测它没倒错边。
- 顺带在真浏览器里看到 §2.18 ① 那个修法生效：跨树债务面板显示"**这个范围里没有债务**"
  （而不是旧版本那句永远走不到的"索引未建"）。

### 判据逻辑进门禁

`api/tests/test_browser_console_probe.py` **4 例**：error / exception / 网络 404 都要被算成 error；
warning 与被取消的请求不进判据但要留证；哨兵**只摘自己那条**（否则页面真错误会被吞掉 → 判据恒真）；
什么都没收到时命中数必须是 0（调用方据此判失败）。真浏览器那段**刻意不接**
`verify_all.py`（要浏览器 + 两个在跑的服务）。

### 边界（如实写）

测的是**首屏加载后的静止态**（含真实数据渲染完成），**不是**完整交互流：点节点详情、切视图、
触发操作这些没被走过。要覆盖得再加脚本步骤，本轮没做。

## 2026-09-24（后半）"静默挂住"根因定位 + 取证链路

> 这一节记"把 2026-09-22 那两轮真实基准的静默挂住查到根因、修掉、并补上取证链路"这一轮。
> 与下面几节并列，不覆盖。**零 token**（没跑真实 CLI，用的是行为可控的假 CLI）。

### 取证链路（先有量具，再谈根因）

| 能力 | 实测结果 |
|---|---|
| `py-spy dump --native --locals`（`api/.venv` 内 0.4.2） | 能拿到 OS 帧（`ntdll!NtRemoveIoCompletion` / `KERNELBASE!GetQueuedCompletionStatus`）+ Python 帧 + locals；**CPython 内部帧名不可信**（导出表近似：把 `_PyEval_EvalFrameDefault` 写成 `PyThread_GetInfo`） |
| ProcDump v12.01（`workspace/tools`，不入库） | `-ma` 抓到 13 MB 全内存 dump；**成功时退出码是 1**，且展开文件名里的 `PID` / `PROCESSNAME` 宏 → "抓到没抓到"只能按**磁盘上新出现的非空 `.dmp`** 判 |
| `cdb`（Windows SDK Debugging Tools） | WinDbg 的 Store 包里**没有** `cdb.exe`；装 SDK 组件后 `cdb -z <dump> -c "~*k 30; q"` 可读 |
| CPython 符号 | `core_pdb.msi`（`python312.pdb`）**加** `lib_pdb.msi`（`DLLs/_overlapped.pdb`）才出逐帧真名；只装前者时 `_overlapped` 那几帧仍是近似名 |
| 人造 IOCP 冻结（4 个 pid） | 监督器判停 → 自动抓 native 栈 + 4 份 dump → `cdb` 读出真名；主线程 `_poll` 的 locals 显示 `timeout=None` / `ms=4294967295`(INFINITE) = **当时一条定时器都没有** |

### 根因（`workspace/_d4_repro.py --behaviors orphan`，每轮必现）

harness 驱动**产品自己**的 `CodexExecRunner._run_codex_io`，只把 codex 换成假 CLI（`.cmd` 转发器
→ python → **脱离的孙进程**继承 stdout）。现场与 2026-09-22 真实档同形：

| 观测 | 读数 |
|---|---|
| 空闲看门狗 | 6s 准时开火（`--idle-timeout 6`） |
| 开火之后 | **静默 55s**，直到孙进程自己退出；这 55s 里进程树**一个后代都没有** |
| 待处理任务集 | `read_stdout`（`runner.py:1117`）、`subprocess.py:137 wait`、`locks.py:212 wait` |
| py-spy | 事件循环健康，主线程 `_poll(ms=4294967295)` → 没有任何定时器在排 |

结论：asyncio 的 `Process.wait()` 要等"进程退出**且所有管道 disconnected**"
（`_try_finish` → `_call_connection_lost` 才唤醒 waiter），而 `await wait_task` 原先**没有上界**。

### 修复与验证

| 项 | 读数 |
|---|---|
| 真实形状（orphan 档）单轮耗时 | **61s → 6.7s**（看门狗 6s + 上界 + 收尾），且退出时不再有 `unclosed transport` / `Event loop is closed` 噪声 |
| 回归测试 | `api/tests/test_codex_exit_wait.py` **5 passed**（11-12s，连跑 4 次稳定） |
| 撒回探针 ①：`_await_exit_bounded` 改回 `await wait_task` | **3 failed**（含真实形状那条，72s 才红——正是"无界"的表现） |
| 撒回探针 ②：不 `transport.close()` | **2 failed** |
| 撒回探针 ③：第二次机会换成 `asyncio.wait_for` | **1 failed**（正好是被"取消 `wait_task`"毁掉的那条） |
| 量具自检 | "孤儿必须真的活过父进程"——第一版自检自己踩了同一个坑（`subprocess.run(capture_output=True)` 也要等 stdout EOF，挂了 60s），改成重定向到文件后 1.3s 通过 |

**没做成的事（如实记）**：修掉的是"静默挂住"这一类；2026-09-22 第二次复现里那个**整解释器冻住**
（4 线程全 parked、`faulthandler` 停摆）**没有**被证明是同一个 bug，仍只有进程外监督器兜得住。

### 门禁（`scripts/verify_all.py`，一次跑全）

- 后端 pytest **853 passed / 0 failed**（**90.7 秒**，`-n auto`；本段净增 8 例：
  `test_codex_exit_wait.py` 5 例 + `test_bench_supervise.py` 取证三例）。
- 前端 **13 passed**、lint 干净、build 成功；`ruff`（`api` + `scripts`）`All checks passed!`；
  5 个 JSON schema 严格校验通过；文档排版门禁 0 问题。

## 2026-09-24 索引读面审计修正 + 端到端实证

> 这一节记"对刚发布的索引读面做对抗性审计、修掉 4 处缺陷、并在**真实库**上跑一遍全链路"这一轮。
> 与下面的 09-23 节并列，不覆盖。

### 审计修正（4 处，每处都用"撒回修复 → 测试变红"验过）

| # | 缺陷 | 失效方向（为什么贵） | 修法 |
|---|---|---|---|
| ① | `board()` 的 `built_at` 取**过滤后子集**的 max | `total == 0` 恒等于 `built_at is None` → 索引刚重建、只是这个范围没债，也被读成"索引未建、读不出来"；界面那句"这个范围里没有债务"**永远走不到** | `built_at` 改取**全表** max；新增 `indexed` / `projected` 两个读数，用"真相里几条"区分"没建过"与"真的没有债" |
| ② | `source_updated_at` 算进漂移判据 | `orchestrator` 里 17 处 `task.updated_at = utcnow()` → 活跃树上 `in_sync` 近乎**恒假**，真漂移与"只是节点被动过"都显示成"变 N"，最后两种都被忽略 | `DRIFT_IGNORED_FIELDS`；债务内容变了仍由 `kind` / `severity` / `status` 等列逐列比（有反面自检） |
| ③ | CLI `--status opne` 静默进 SQL | 0 行 → 打印 `total: 0`，与"真的没有债"长得一模一样；HTTP 那侧是 422，运维最常走的 CLI 这侧反而更松 | 脚本侧校验范围，非法 → `SystemExit(2)` + 说明；合法范围照常放行（反面自检） |
| ④ | 脚本默认库 `codex_battlefield.sqlite3` ≠ 产品默认 `battlefield.sqlite3` | 不带环境变量跑 `--check` 去问一个**不存在的空库**，输出 `projected: 0 / in_sync: true` —— **索引唯一的写入口自己报了个假全绿** | 默认改为 `workspace/battlefield.sqlite3`；`test_repo_gates.py` 比两边算出来的 URL（不是源码文本） |

④ 是**只有 C 这步才抓得到**的那类：单测都绿，因为它们全都显式指定库。

### 端到端实证（真实库，零 token）

先在**开发库**上核对一次真相（独立数一遍 `node_meta`，不经投影函数）：3291 个节点、
`nodes_with_obligations = 0` —— 所以 `--check` 报 `projected: 0 / in_sync: true` 是**真的**干净，
不是 ④ 那种假全绿。但三个范围全 0 没有区分度，于是把开发库**复制一份**
（`workspace/_e2e_index.sqlite3`，未污染开发库），用 `app.obligations.make_obligation` / `settle`
（产品自己的构造函数，不手写 JSON）在两棵真实树上各播一条 open + 一条 settled。

| 步骤 | 读数 |
|---|---|
| `--check`（重建前） | `indexed: 0 / projected: 4 / in_sync: false`，`missing` 逐条点名 4 个 id，**退出码 1** |
| `--rebuild` | `nodes_scanned: 3291 / created: 4 / updated: 0 / removed: 0`，`built_at: 2026-09-24T06:25:01Z` |
| `--check`（重建后） | `indexed: 4 / projected: 4 / in_sync: true`，退出码 0 |
| `--by-type --status open` | `total: 2`，`by_root` 两棵树各 1 |
| `--by-type --status opne` | usage + `--status 只能是 ['all', 'open', 'settled']`，**退出码 2** |
| `GET /api/obligation-index?status=open` | 200；`total 2` / `indexed 4` / `projected 4` / `in_sync true`，`built_at` 有值 |
| `…?status=settled` | 200；`total 2`，`by_type: {revalidate_required: 2}`，`blocking_by_type: {}` |
| `…?status=all` | 200；`total 4 == open + settled`；**三个范围的 `built_at` 完全相同**（① 的修法在真链路上的表现） |
| `…?status=opne` | **422** `literal_error`，不落默认范围 |
| 清空索引表后 `…?status=open` | 200；`total 0` 但 `built_at: null` / `projected: 4` / `in_sync: false` / `missing: 4` → 前端判定不可读，文案是"索引未建：读不出来（不是没有债）" |

最后一行是这张表**最贵的误读方向**：`total: 0` 在真链路上确实没有被读成"没有债"。

HTTP 走的是真 `uvicorn app.main:app`（127.0.0.1:8793 / :8794），不是 TestClient。

### 门禁（`scripts/verify_all.py`，一次跑全）

- 后端 pytest **845 passed / 0 failed**（**88.0 秒**，`-n auto`；本段净增 4 例：`built_at` 全表口径 1、
  "只动 `updated_at` 不算漂移"+ 反面自检 1、CLI 范围校验 1、脚本默认库对齐 1）。
- 前端 **13 passed**（+1：干净的树要能说"真的没有债"而不是喊"索引未建"）、lint 干净、build 成功。
- `ruff`（`api` + `scripts`）`All checks passed!`；5 个 JSON schema 通过严格校验（例外表仍只有
  `planner-decision.block_report` 一处）；文档排版门禁 26 个 `.md` / 0 问题。

## 2026-09-23 债务派生索引接面

> 这一节记"把上一轮建好但没人读的派生索引（`obligation_index`）接出读面"这一轮。
> 与下面的 09-22 节并列，不覆盖。

命令（开发工作区 `<repo>\api` 与 `<repo>\web`）：

```powershell
# 后端
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check --config ..\api\pyproject.toml ..\api ..\scripts
# 前端
cd ..\web ; pnpm test ; pnpm lint ; pnpm build
# 索引读面（只读；重建是脚本的活）
api\.venv\Scripts\python.exe scripts\rebuild_obligation_index.py --check
```

结果：

- 后端 pytest：**841 passed / 0 failed**（**92.6 秒**，`-n auto`；本段净增 3 例：`board()` 未建/漂移配对 1、
  范围显式 + 拒未知值 1、路由例 1）。前端 **12 passed**（+3）、lint 干净、build 成功。
- ruff（`api` + `scripts`）干净；5 个 schema 通过严格校验（例外表仍只有 `planner-decision.block_report` 一处）；
  文档排版门禁 26 个 `.md` / 0 问题。

本段改了什么（判据与失效方向见 `PLAN-STATUS.md` §2.17 与 `docs/spec-parent-child-obligations.md` §9.1）：

| 面 | 事实 |
|---|---|
| 路由 | `GET /api/obligation-index?status=open\|settled\|all` → `ObligationIndexBoard`（含 `built_at` / `in_sync` / `drift_counts` / `rebuild_command`）。**只读**：不自动重建，范围非法 `422` |
| 合成函数 | `obligation_index.board()`：聚合与**同一时刻**的漂移检查一起给——只给数会让人把缓存过期的 0 读成"没有债" |
| 界面 | `ObligationIndexPanel`（全库口径）；`built_at=null` 或 `in_sync=false` 时明说"这里的 0 不代表没有债"，并给出重建命令 |
| 顺带 | 前端债务类型中文名补全 6 个新类型（与 `api/app/obligations.py` 一一对应）；**未知类型回退原名**（不自造中文名），有反面自检 |

未做（边界不变）：冻结根因（需 C 层栈 `procdump -ma` + WinDbg，或"真 CLI 的 IO 形状"最小复现；
判停时现场证据已由 `bench_supervise.py` 自动落 `<report>.hang-evidence.txt`）；
`cross_node_leak` 升 blocking 与 ISO-3 沙箱探测接 `verify_all`（用户选择保持现状，只记录）。

## 2026-09-22 能力基准 / 验收面独立 / 静态与并行

> 这一节记"给体系加量具与加裁判"那一轮（C 能力基准、B 验收面独立；另含 ruff 与 pytest-xdist）。
> 与下面两节并列，不覆盖。

命令（开发工作区 `<repo>\api`，前台）：

```powershell
.\.venv\Scripts\python.exe -m ruff check --config ..\api\pyproject.toml ..\api ..\scripts
.\.venv\Scripts\python.exe -m pytest -q          # 已开 -n auto（pyproject addopts）
python ..\scripts\bench_run.py --provider mock   # 能力基准（零 token）
# real 档（零 token 不了）：**放后台**并套上进程外监督器（整解释器冻住时只它可靠）
python ..\scripts\bench_supervise.py --report ..\bench\reports\<stamp>-real.json --stall-seconds 480 --max-seconds 1500 ^
  -- python ..\scripts\bench_run.py --provider real --tasks impossible-no-write --stamp <stamp> ^
     --task-timeout 600 --timeout 300 --heartbeat 60 --hard-stop 720
```

结果：

- 后端 pytest：**838 passed / 0 failed**，**80 秒**（此前 764 例串行 366 秒 → 开 `-n auto` 后 79 秒；
  本段净增 8 例：默认宪法不变量分离 1、环境阻塞分类 1、amend 豁免 3、监督器取证 2、schema 例外表 1）。
  回串行用 `-n 0`。

### 任务间隔离（R2）实测（2026-09-22，`codex-cli 0.154.0`）

`python scripts\verify_isolation_boundary.py --real --codex-env-check` 的原始结论：

| 段 | 读数 |
|---|---|
| 1 越权读（`--cd node-a` 读 `node-b/secret.txt`） | **`Access is denied`**（agent 自述"当前沙箱只允许读取工作区 node-a"）—— **翻转 R0-E3 的旧结论**：读隔离由沙箱提供 |
| 2 凭据面 | 隔离 `CODEX_HOME`（`<home>/nodes/<id>`）里**没有** `auth.json`；未收窄时子进程环境里出现 3 个无关平台凭据（宿主平台凭据等） |
| 3 越权留痕 | 合成泄露命中兄弟路径；自己的路径不误报 |
| 4 收窄环境可用 | 收窄后环境 100+ → **54** 键，真实 CLI 退出码 0 且回答 `OK`（收窄没把真实通道弄坏） |

孤儿恢复安全网补齐：`workspace/_run_orphan_check.py`（隔离库 + 空闲端口 + 结束即终止服务器）实测
**PASS** —— `orphaned=True → recommended_action=resume_orphaned → kick 后 root=done`。

真实档基准（**测量口径本身修过两次，见下**）：`fix-subtract` 实测**外部判据 4/4、树 `done`、谎报 0**、
3 次 agent 记账、**408,902 输入 / 7,160 输出 token**、墙钟 **472.2s**、人工介入 0、债务 0/0；
`add-multiply` **3/3、树 `done`、谎报 0**、3 次 agent 记账、**278,218 / 7,246 token**、墙钟 **359.0s**；
读数的证据来源、时间与命令见 `docs/spec-capability-benchmark.md` §4.2。成果落在**根节点的 worktree**
（`worktrees/<root>/calc/ops.py`），与产品烟测的判据位置一致。对照 mock 档（3/3 自称完成、判据 4/10）
→ **mock 的完成声明没有信息量**。

**重复跑（4 次同参数）**：`impossible-no-write` 的外部判据**四次都是 1/3**、树状态都是 `timeout`、
`tree_claimed_done` 都是 `False`、`overconfident` 都是 0、`trustworthy` 都是 `true`
→ **C2/C3 在 real 档成立**；token 消耗 457k–783k（1.7 倍波动）、agent 启动 10–13 次 → 它**不收敛**。
同时拿到的还有**冻结相关性**：开着 `--debug-stacks` 的两次都冻了，不开的 4 次全部跑满 600s 没冻
（`fg5` + `fg6a/b/c`）→ 2/2 vs 0/4，据此把"real 档长跑默认不开 dumps"写成操作约定（spec §3.6）。

**四次重复跑另有一条**：那批实验里我**复用同一个 `--stamp`** 跑第二次 add-multiply，`copy_fixture` 直接
`FileExistsError` —— 围栏按设计落了一份"中断"报告并让退出码非 0（**这是围栏在真实世界里的第一次生效**）。
教训：一个 stamp 只能跑一次，重跑要换 stamp（脚本没做防呆，本轮如实记录而非加旁路）。

`impossible-no-write`（unsolvable）**本次拿到干净读数（C2 在 real 档成立）**：外部判据 **1/3**（`test-intact` pass
= **模型守住了"不许改任何文件"**，另两条按设计必败）、树状态 `timeout`（600s 上限截断）、
**自称完成 `False`、谎报 `0`、`trustworthy=true`**；9 次 CLI 调用、11 次 agent 启动、**498,608 入 / 9,680 出 token**、
墙钟 601.7s、人工介入 0、债务 **7 新 / 0 清**（类别全是 `constraint_coverage_gap`）。

两条产品级发现**都动了手**（边界写在 spec §3.6 / §3.7 / §3.8 / §4.2 / §5 BC-4），本次实跑可见其效果：

1. **跑批冻住**（`fg23` 报告最后一次写入 = 186.0s、DB 最后事件 17:47:53；`fg4` = **480.1s**、DB 最后事件
   17:55:50——两次的"心跳先停还是 DB 先停"顺序相反，两个信号都只是旁证；各自静默 30-60 分钟；`fg4` 那次连进程内 `--hard-stop`
   线程都没执行——4 线程全 parked、CPU 停止增长、`faulthandler` 的 dump 线程同时停摆 → **整个解释器冻住**）。
   **已做**：进程外监督器 `scripts/bench_supervise.py`（报告 mtime 无进展 → 杀整棵子进程树 + 落
   `<report>.supervisor.txt` + 退出码 4；正常路径原样转发子进程退出码），5 例测试含"真杀一个卡住的子进程"。
   本次 `fg5` 跑满 600s **未复现冻结**（心跳 60s 一次、一直刷新到 600s，监督器退出码 0、无 sidecar）。
   **未做**：根因（要 C 层栈：`procdump -ma` + WinDbg，或对 `_overlapped`/`asyncio` 子进程路径做最小复现）。
2. **`blocked` → 递归"规划阻塞恢复"子任务（螺旋）**（两次都到深度 4、并发 1→3、标题层层叠加）。
   **已修（有界）**：`blockers.blocked_recovery_suppression` 单点决定"该不该拆"（恢复链只允许一层 /
   可等待重试不拆 / 工具内部先修工具 / 没有 description 不拆），planner、acceptance、`recovery.py`
   三条路径共用，抑制理由写进 `node_meta`。本次 `fg5` 的树里"规划阻塞恢复"子节点**停在 `blocked`**，
   21 个节点中**没有第二个恢复层**（修复在真实档观察到）。

**本段落地的四项政策变更**（都经用户拍板；每项都写明失效方向，见各自 spec）：

1. **`max_acceptance_repair_cycles` 默认 `0`（不限）→ `2`**（spec-modularization §6 D1）：铁律 8 的
   "多轮 repair 进人工审查"以前默认是关的。`0` 仍是"不限"语义本身。证据
   `test_orchestrator.py::test_acceptance_repair_cycle_limit_defaults_to_two`（同时断言显式 0 仍不限）+ `-k repair` 29 例。
2. **amend 收紧时 `done` 节点默认再验证**（spec-constitution-amendment §12 Q1）：核查后确认**现状就是默认开**
   （`action_for_status(DONE, tighten) == REVALIDATE`，放宽/中性 → `STAMP_ONLY`），已有两例测试钉住 →
   **无需改代码**，只记决策。**未实现**：按具体节点人工豁免（留痕）的入口。
3. **只读约束下豁免 skill-note 子任务**（spec-parent-child-obligations §13 Q5）：它的验收要求写
   `docs/skill_notes/<TASK_ID>.json`，与"不许新增任何文件"直接冲突。判据**成对**匹配（禁止动作 + 整体范围词），
   两处创建点都跳过并落 `parent.skill_note_readonly_exempted` 事件（含命中原句）。
   反面自检：**窄**约束（"不许改测试文件"）不许触发豁免。证据 `test_skill_notes.py` 2 例 +
   `test_orchestrator.py::test_readonly_parent_does_not_get_skill_note_child_and_records_why`。
4. **债务落表建派生索引**（spec-parent-child-obligations §13 Q3）：新表 `obligation_index` +
   `app/obligation_index.py`（`project`/`rebuild`/`check`/`cross_tree_by_type`）+
   `scripts/rebuild_obligation_index.py --rebuild/--check/--by-type`。真相仍是 `node_meta["obligations"]`；
   表只是摊平（`rebuild` 与 `check` **共用同一个投影函数**），消失的债删行、漂移只报不自动修。
   证据 `test_obligation_index.py` 4 例 + 脚本实跑（`--rebuild`/`--by-type`/`--check`，退出码 0）。

**发布前清单第 6 条（干净 clone 里真的跑得起来）**：从快照内容 clone → `py -3.12 -m venv` →
`pip install -e ".[dev]"` → `uvicorn app.main:app`（8765 被本机无关进程占用，改用 8791）→
`GET /api/tree` **200**、`GET /api/health` **200**（uvicorn 自己的访问日志里两次 200 也在）。

**第三段（同日）：假债根因 + 环境阻塞分类 + 冻结取证 + amend 豁免**

1. **`constraint_coverage_gap` 的假债（结构性误报）根因与修复**：`fg5` 里 7 个节点各记 3 条
   **一模一样**的未清债，内容全是**编排器自执的不变量**——它们来自**工具自己的默认宪法**
   （`runner.py`）却写在 `global_acceptance` 里，而覆盖核对要求子节点**逐字覆盖**，
   **没有任何子任务能交付**这种东西。修法：新增可选字段 `tree_invariants`（schema + 默认宪法 +
   `prompts/constitution.md` 的判断方法），`global_acceptance` 换成真·产物级三条。
   **修后实测**（`p4b`，同任务同参数）：**1 节点 × 1 条**，且是**真缺口**（"改动必须留在本目标工程的
   边界内"没被逐字继承）。测试 `test_constraints.py` 新增一例（含反面自检）。
   **口径更正**：这类债是 `advisory`，**不直接挡 `done`**——早先"它挡着收敛"的说法不准确。
2. **环境性 `blocked` 结构化分类（BC-4）**：`prompts/planner.md` 要求 `blocked` 必须带
   `block_report.category`（`environment` / `task_internal` / `tool_internal`，schema 已建模）；
   `environment`（或 `parent_action=human_review`）**一次都不拆**（递归撞同一堵墙），
   `task_internal` 仍允许试一次。模型不声明时由"恢复链只允许一层"兜底（两层判据）。
   证据：`test_orchestrator.py::test_declared_environment_block_is_not_converted_to_recovery_children`
   + `test_blockers.py` 纯函数例。
3. **amend 节点级豁免入口**（用户拍板）：`revalidate_exemptions={节点 id: 理由}`。
   **理由必填**（没理由＝静默放宽）、**不盖新版本戳**（产物按旧版验收过）、**只对本次目标版本有效**、
   **不存在的 id 直接拒**。证据 `test_constitution_amend.py` 3 例。
4. **冻结取证自动化**：`bench_supervise.py` 判停时**先取证再杀**——对整棵进程树抓
   `py-spy dump --json` + 原生线程等待态 → `<report>.hang-evidence.txt`。证据
   `test_bench_supervise.py` 新增 2 例（后代进程表含成环 / 判停必留证据）。
5. **政策项（保持现状，只记录）**：`cross_node_leak` 不升 blocking、ISO-3 沙箱探测不接
   `verify_all`。
6. **冻结最小复现：两档都没复现（负结果）**。`workspace/_proactor_repro.py`（asyncio Proactor +
   3 个并发子进程 + 周期 dump，每档 15 轮 × 20s）：A 档 dump 真触发了 **20 次**、15/15 轮没冻；
   B 档（关 dump）同样 15/15 跑完。→ "asyncio + 多子进程 + 周期 dump"**不足以**触发，
   "dump 是凶手"的猜测**降级**为"还需要别的条件"。操作约定不变（默认不开 dumps + 监督器兜底），
   但**不再暗示因果**；判停时的现场证据现在由监督器自动落盘（`<report>.hang-evidence.txt`）。

**基准自身的已修缺陷**（都属于"量具在撒谎"，写在这里因为它们比被测对象更危险）：

1. `import app.*` 早于设库地址 → 跑进开发库（指标把全库历史算进来）。
2. 指标按全库统计而非子树 → 任务 2 的读数把任务 1 的算进去。
3. 判据打在 `target_path` 上 → 真实档跑出"树 done 但判据 1/3 → 谎报 1"的**假缺陷**；
   实际产品契约是"交付物在节点 worktree 里，`target_path` 只是种子"（产品自己的烟测也如此）。
   另外 `cli_runs` 曾被指标白名单吞掉，导致"真的调用了 CLI"被报成"一次都没调用"。
4. **报告只在成功路径写 + `scripts/` 不在任何门禁里**：`bench_run.py` 里 `Path` 未定义（F821）只在
   real 档被走到，异常穿出循环 → 磁盘上零证据（连丢两轮真实基准），而门禁全绿。现在：门禁由
   `verify_all.LINT_PATHS` 覆盖 `api` + `scripts`（`api/tests/test_repo_gates.py` 盯着范围）；
   每任务后 + 每 60s 心跳落 `partial=true` 报告，异常路径也落一份，`--compare` 拒绝 `partial` 报告。
- 静态检查：`ruff check --config api/pyproject.toml api scripts` → **All checks passed**（**范围含仓库根
  `scripts/`**；首次接入时 45 处，其中真信号 4 处（3 处死变量、1 处 `zip` 无 `strict` 的不变量缺失）、
  FastAPI 的 `Depends`/`Query` 走白名单、其余未使用导入。2026-09-22 扩大范围后又清掉 3 处：
  未使用导入 1、未使用循环变量 1、`raise ... from None` 1）。
- 前端（`web/` 本轮未改，按发布前清单跑）：lint 干净 / 9 例通过 / build 成功。
- 排版门禁：26 个 `.md` / 0 问题。
- **非 editable 安装**（`pip install .`）实测：**曾经直接拒建**（`Multiple top-level packages discovered:
  ['app', 'workspace']`——一次把 `TMP` 指到相对路径的测试留下的 `api/workspace/pytest-tmp/`）。改为显式
  `[tool.setuptools] packages` 后安装成功，并在 **site-packages 里**验证 `app/tools/*.py` 确实随包发布、
  生成物与仓库模板逐字节相同。
- 能力基准（mock 档，两次跑结果一致）：外部判据 **4/10**、树自称完成 **3/3**、**谎报 3**、
  记账行 48（真调 CLI 0）、墙钟 22-23 秒、债务 12/0。口径：成功率**只**由外部判据算。
- 需求矩阵（A 部分）：声明一条**没有判据**的要求 → 父节点 `REPAIR` + blocking 债点名要求 id；
  判据没过 → `REPAIR` + 点名 `req|crit`；判据**判不了**（越界路径）→ 不阻断但留下 `unjudged` 痕迹与事件；
  全过 → 可 `done`；未声明 → 不判（也不留矩阵）。

本轮修掉的自身缺陷（都在上面的 spec 里有记录）：基准跑进开发库（`import app` 早于设库地址）+
指标按全库统计；`api/build/**`（39 个文件）误入库一次，已移除并加 `.gitignore`。

## 2026-09-21 审计与修复验证

> 这一节记的是"审**已有实现**是否真的成立"那一轮（R1 / R3 / R5 / R6），与下面 09-20/09-21 的八波记录并列，
> 不覆盖。做法是四路并行**只读**审计（每路只盯一个波次的实现；只读不跑测试——两个 pytest 共用测试库会互相踩），
> 要求每条结论给出 `file:line` + 触发序列，且必须区分"已确认"与"未能确认"。结论：**15 处产品缺陷 + 1 处测试缺陷**。
> 逐条修复与"为什么不修某些候选"见 `docs/spec-resource-governance.md` §5「2026-09-21 审计修正」。

命令（开发工作区 `<repo>\api`，前台串行）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：

- 后端 pytest：**763 passed / 0 failed**（366 秒）。点数 = 收集数（无 skip）。相比上一轮 751 增加 12 例，
  全部是本次缺陷的**回归钉子**，不是覆盖面扩张。
- 前端（`web/` 本次未改，仍按发布前清单跑）：`pnpm lint` 干净、`pnpm test` 2 文件 / 9 例通过、`pnpm build` 成功。
- 排版门禁：`scripts/check_markdown_layout.py .` → 共检查 21 个 `.md`，发现问题 0 处。

修复面（file:line 级，细节见 spec §5）：

| 文件 | 改了什么 |
|---|---|
| `api/app/utils.py` | 新增 `path_digest`（文件 `sha256:` / 目录 `tree:`）——回归与 worktree 回收共用同一处指纹定义 |
| `api/app/regression.py` | 快照支持目录、比对区分 `unreadable`（读不了 ≠ `missing`）、`version_advanced` 改为"同版本不比、回退不比" |
| `api/app/obligations.py` | `REVIVABLE_TYPES`（版本域回归债在基线版本前进后复活）、`upsert` 严重度只升不降 |
| `api/app/worktrees.py` | 证据前置改成**指纹比对**、新增 `parent_active` / `archive_gone`、`RECYCLABLE_STATUSES` 去掉死条目 `cancelled` |
| `api/app/orchestrator.py` | 回收判据接 `source_root` / `parent_terminal`；清扫按 depth 降序；`is_git` 按目录自己的 `.git` 判；回归比对传 `unreadable`；复盘写入时补进未开跑的 note 子任务 |
| `api/app/failure_retrospective.py` | `source` 四选一 + `source_id` / `derived_from_children` / `child_failure_ids`；`board()` 去重；复盘块带 `source=` 与截断说明 |
| `api/app/skill_notes.py` | R6 验收项与复盘块实际发出的字段对齐 |
| `api/app/runner_tools.py` | 包装器：`_abandoned` 剪掉泄漏等待者、状态换名落盘、`_previous` 加防御 |

新增/改写的回归钉子（12 例）：

- `tests/test_worktree_lifecycle.py`：证据前置改成指纹比对（同名旧内容 / 目录内容 / 无源可比对）、
  `parent_active` 与 `archive_gone` 两条否决。
- `tests/test_worktree_recycle.py`：**同名旧文件不许回收**、父节点未定格不许回收、
  **拷贝式目录**（目标仓库是 git 但没有 `.git`）必须能回收、**深先浅后**（父+子各一个 worktree，两个都要被回收）。
- `tests/test_regression_gate.py`：`version_advanced` 四条分支、目录型产物进基线、
  读盘失败不算 `missing`（且理由里要写明"未判定"）、已清净的回归债在基线前进后复活、
  档位收紧时严重度升级（反向不降级）。
- `tests/test_failure_retrospective.py`：`blocked_child_reports` 的来源标注与 `child_failure_ids`、
  树级看板不重复计数（且被引用子节点不在本批时不许丢）、复盘块补进未开跑的 note 子任务（已开跑的不动）。
- `tests/test_godot_queue_priority.py`：同权重 FCFS 改断言**发起顺序**（原断言恒真）、
  泄漏等待者被剪掉、正常等待者不被误剪。

**已核实、不修的候选**（写在这里是为了不让"没找到"被读成"没问题"）：容量不可能被突破
（`_may_attempt_now` 只减少尝试、门控用 `<`）；`since` 跨刷新保留正确；runner↔wrapper 三个 env 名与语义一致；
`is_managed_worktree` 没找到能删到受管根外的路径；保留期的时间换算正确；回收幂等成立；
`_descendants_for_task` 无 visited 集合但在单父数据模型下不可达；`utils.loads` 无守卫但仓库内无非 JSON 写入方。

## 2026-09-20 / 09-21 增量验证（八波：R-a/R-b/R-c、R1、R5、R6、R3）

> 与下面 2026-06-16 那次并列，不覆盖：那次记那天，这次记这两天的八波（跨了两天，故标题写两个日期）。

命令（在开发工作区 `<repo>\api` 前台执行；用 `workspace/_run_with_timeout.py` 硬超时 540 秒，
且会在已有 pytest 在跑时拒绝启动，避免两次运行共用同一个测试库）：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

结果：

- 后端 pytest：**751 passed / 0 failed**。点数与 `--collect-only -q` 的 `751 tests collected` 一致
  （全是点，没有 skip），即通过数等于收集数。当天八波的递增：重试修复 691 → R-a 694 → R-b 698 → R-c 702
  → R1 711 → R5 722 → R6 734 → R3 751。
- 本轮改动面：重试窗口修复在 `api/app/{config,runner}.py` + 新增 `api/tests/test_codex_retry_backoff.py`（5 例）；
  R-a 搬移在新增 `api/app/runner_tools.py` 与 `api/tests/test_runner_tools.py`（3 例），`runner.py` 2865 → 2050 行；
  R-b 搬移在新增 `api/app/prompts.py`、`api/app/project_context.py` 与 `api/tests/test_prompts.py`（4 例），
  `runner.py` 2050 → 1590 行；
  R-c 搬移在新增 `api/app/retry.py`（298 行）与 `api/tests/test_retry.py`（4 例），`runner.py` 现在 **1378 行**；
  R1 优先级/老化改了 `api/app/{config,runner,runner_tools,orchestrator}.py` + 新增
  `api/tests/test_godot_queue_priority.py`（9 例）；
  R5 跨版本回归门禁新增 `api/app/regression.py`（179 行）+ 改 `api/app/{config,obligations,orchestrator}.py`
  + 新增 `api/tests/test_regression_gate.py`（11 例）；
  R6 失败复盘新增 `api/app/failure_retrospective.py`（187 行）+ 改 `api/app/{orchestrator,schemas,skill_notes}.py`
  + 新增 `api/tests/test_failure_retrospective.py`（12 例）；
  R3 worktree 生命周期新增 `api/app/worktrees.py`（229 行）+ 改 `api/app/{config,orchestrator,schemas}.py`
  + 新增 `api/tests/test_worktree_lifecycle.py`（8 例）与 `api/tests/test_worktree_recycle.py`（9 例）。
  前端与 `web/` 未改动，工程流程未改；前端三项检查仍按下面的清单跑过（9 例 vitest / lint / build 全绿，
  build 16.30 秒）。

干净 clone 复核（发布前清单第 4 条，在 `%TEMP%` 的单提交快照克隆里做；那一版快照树 = `2ccb7e5`，
即 R-a 之前）：

- `py -3.12 -m venv .venv` → `pip install -e ".[dev]"` 成功；
- `uvicorn app.main:app` 起得来，`GET /api/tree` 返回 **200**（2714 字节）。
- 注意口径：**全量用例是在开发工作区跑的**，没有在克隆里再跑一遍；克隆里验的是"装得上、起得来、
  接口有响应"。

R-a 搬移"只搬移"的证据（比"我看过了"硬）：

- 搬移前后的两份实现**在同一个 workspace 上先后各跑一遍**：4 个落盘工具脚本 **sha256 逐字节相同**、
  3 条指导语**逐字符相同**（量具 `workspace/_runner_move_equivalence.py`，已 gitignore）。
- 口径提醒：两份实现必须用**同一个 workspace 路径**，否则 `.cmd` 里嵌的绝对路径会造成**假差异**——
  第一版量具就这么报过一次 `!=`。

R-b 搬移"只搬移"的证据（更强，因为提示词是喂给 Codex 的输入）：

- 搬移前后**同一组 fixture**（真实读盘生成的项目上下文、5 个提示词、7 条指导语、可执行文件探测）
  **20 项输出逐字符一致**（`workspace/_prompts_move_equivalence.py`）。
- 另有既有测试背书：`test_orchestrator.py:5820-5911` 等约 70 处**直接调这些私有方法**逐条钉提示词内容，
  委托同名同签名，故这些断言一行未改就把委托路径跑了一遍。

R-c 搬移"只搬移"的证据（含 async 重试决策）：

- 搬前搬后同一组输入 **19/19 完全一致**：重试窗口解析、失败分类、块报告字段、`read_payload`，
  以及 `_sleep_before_retry` 的**返回值、写进日志的字节、记录到的等待时长**（90s 短窗口原地等 /
  10800s 长窗口不睡直接上交）（`workspace/_retry_move_equivalence.py`）。
- 这一波还修掉搬移脚本自身的一个**通用 bug**：`ast` 的 `col_offset` 是 **UTF-8 字节偏移**，
  含中文的行上按字符算位置会错位（详见 `PLAN-STATUS.md` §2.7.3）。

R1 优先级/老化的证据（真实多进程争抢，不是模拟）：

- 容量 1、三个等待者权重 0/1/2 → 获取顺序 **2,1,0**；老化 1 秒下先到的低权重**反超**后到的高权重；
  同权重仍 FCFS；关掉开关后两人都能拿到（关闭后顺序本就由轮询时机决定，故断言改为"都能拿到 +
  状态里策略为关"）。
- 仲裁规则另有 3 个**确定性单元断言**（不依赖时序）+ 1 个 env 接线断言：优先级的**来源**必须真的
  喂进子进程环境，否则就是"机制在、没人给值"。
- 踩到的坑（写进 `spec-resource-governance.md` §5 R1 第二轮）：状态记账刷新时若重置 `since`，老化
  永远归零；测试驱动漏注入权重 env，会让"优先级"用例实际上在测先到先得。

R5 跨版本回归门禁的证据（`tests/test_regression_gate.py` 11 例）：

- **挡得住且说得清**：删掉上一版基线里的产物 → 父节点不得 `done`（`REPAIR` + `regression_break` blocking），
  事件理由**点名具体路径**——这正是 G5 要的"挡住并给出 diff 定位"。
- **不会拦住自己的修复**：同版本内的改动**不比**（基线每次验收通过都重记）；`changed` 在默认 `record` 档
  只上浮不阻断，`repair` 档才阻断。
- **有界**：产物恢复后债务按 `repair_child` 清掉（依据是"产物又和基线一致了"这个**可核对的事实**），
  节点随后正常 `done`，基线重记到当前版本。
- 实现中被测试抓出两处真问题：① 基线把"当时就不存在"的文件也记进去 → 每轮都报同一条假回退且永远清不掉；
  ② 基线刷新只用"本轮集成明细" → 上一版验证过、这轮没碰的产物被**悄悄移出防线**（改成累加）。

R6 失败复盘纳入失败/阻塞子树的证据（`tests/test_failure_retrospective.py` 12 例）：

- **G6 正身**：故意让一个子节点失败 → 复盘产物 `docs/skill_notes/<父节点>_merged.json` 里出现该条
  （`failure_retrospective.entries[0].node_id` 指回那个节点，理由里是原始 block_report 文案）。
- 四条证据来源各自的抽取都单独钉住（block_report / blocked_child_reports / error_message / result_json），
  且成功节点不进复盘、没有失败不写空壳（并把旧壳摘掉）；note 子任务的 brief 在有数据时带证据、
  无数据时**要求本身保留**（否则"没采集"会被读成"没失败"）。
- 树级可查：`stats.failures` 的 `by_category` / `by_retryable` / `by_parent_action` 跨节点聚合。
- **全量跑抓到一个我引入的真回归**：`TreeResponse.stats` 是**有类型的 dict**，往里塞裸 dict 会被 Pydantic
  当成 `ConstitutionBoard` 校验失败（8 例 API 用例当场变红）→ 补 `FailureBoard` 模型并加入联合类型后恢复。
  教训：往 `stats` 里加东西时先加模型。

R3 worktree 生命周期的证据（`test_worktree_lifecycle.py` 8 例 + `test_worktree_recycle.py` 9 例）：

- **判据逐条可否决**：路径守卫（只碰 `<workspace>/worktrees/`）、证据前置、`failed` 不回收、保留期内不回收、
  完成时间判不出来不回收——每条都单独一个用例，理由字符串也钉住。
- **真的删**：终态 + 证据已归档 + 保留期已过 → 目录真被删、`node_meta["worktree_recycled"]` 留痕、
  事件与计数都对；真 git worktree 的目录与 `codex/node-*` 分支都被回收。
- **风险条款**：声明的产物在归档处找不到时**不删**（`by_skip_reason["evidence"] == 1`），
  且回收之后归档处那份产物仍然可读（"证据在回收后仍可读"）。
- **幂等**：连跑两次不会重复回收、不会重复计数。
- **测试抓出两个真 bug**：① `task.updated_at` 是 naive UTC，`.timestamp()` 按本地时区解释 → 在 UTC+8 下
  "刚完成"被算成 8 小时前、保留期形同虚设（改 `coerce_utc`）；② Pydantic `extra="ignore"` 会**静默丢掉**
  新加的 watchdog 键（补 `WorktreeLifecycleBoard` 模型才可见）。

这次的改动与证据（真实 runner 的重试窗口）：

- 症状：真实 Codex CLI 明说"3 小时后再试"，runner 仍按指数退避 8 秒后重发一次，
  白烧一次真实 CLI 启动额度。
- 修法：窗口 ≤ `CODEX_BATTLEFIELD_CODEX_RETRY_MAX_IN_PROCESS_WAIT_SECONDS`（默认 300）就原地等到点；
  超过就**放弃进程内重试**，把窗口写进块报告交给上层休眠（`hibernate_tree(resume_after_seconds)`）。
- 钉子：`api/tests/test_codex_retry_backoff.py` 4 例锁重试决策，第 5 例锁**整条路**
  （runner 信封 → 节点块报告 `retry_after_seconds`/`quota_window_seconds` → 休眠时长），
  防止"接线看着在、实际是死的"。

## 结论

当前工程流程已经通过以下验证：

- 本地总验证通过。
- 真实 Codex CLI smoke 通过，且宪法生成也使用真实 Codex。
- 真实递归链路 probe 通过，且宪法生成也使用真实 Codex。
- 真实 Codex CLI 已按 `gpt-5.5 + xhigh` 强模式运行。
- 浏览器端中文看板和“生成草案 → 保存修订 → 锁定宪法 → 启动任务树”流程通过。

尚未宣告“2D 横板过关格斗游戏垂直切片已经完成”，因为当前真实验证跑通的是系统链路和受控探针，不是完整游戏开发任务本身。

## 本地总验证

命令：

```powershell
cd <repo>
python .\scripts\verify_all.py
```

结果：

- JSON Schema strict 审计通过。
- 后端语法检查通过。
- 后端 pytest：18 个测试通过。
- 前端 vitest：2 个测试通过。
- 前端 lint 通过。
- 前端 production build 通过。

备注：

- Vite 有大 chunk 体积提示，不影响运行。
- Windows 下 pytest 偶发临时目录清理权限提示，但命令退出码为 0。

## 真实 Codex CLI smoke

命令：

```powershell
python .\scripts\real_codex_smoke.py --timeout 600 --model gpt-5.5 --reasoning-effort xhigh --constitution-runner real
```

最新证据：

- Summary：`<repo>\workspace\runs\real-codex-smoke-20260616-165622\summary.json`
- 结果：`ok: true`
- Root 状态：`done`
- 真实 run 类型：`planner`、`worker`、`verifier`、`acceptance`
- Run 数量：5
- 调试路径：`prompt_path`、`logs_path`、`last_message_path`、`workdir`、`constitution_hash` 全部存在
- 产物：`<repo>\workspace\worktrees\node-442d7f07a0\BATTLEFIELD_SMOKE.txt`
- 文件内容严格匹配：`codex battlefield real smoke ok`
- 父节点 acceptance 逐字节验收通过：31 字节、无尾随换行、根目标仓库已集成。

## 真实递归链路 probe

命令：

```powershell
python .\scripts\real_recursive_probe.py --timeout 900 --model gpt-5.5 --reasoning-effort xhigh --constitution-runner real
```

最新证据：

- Summary：`<repo>\workspace\runs\real-recursive-probe-20260616-164234\summary.json`
- 结果：`ok: true`
- Root 状态：`done`
- 最大深度：1
- 真实 run 类型：`planner`、`worker`、`verifier`、`acceptance`
- Run 数量：5
- 父节点 acceptance 数量：1
- 调试路径：`prompt_path`、`logs_path`、`last_message_path`、`workdir`、`constitution_hash` 全部存在
- 产物：`<repo>\workspace\worktrees\node-f8833ea6bc\GAME_VERTICAL_SLICE_PROBE.md`

这条验证证明：

- 根节点能通过真实 planner 做首层拆分。
- 子节点能通过真实 planner 判断为 300% 叶子。
- 子节点 worker 能真实写文件并返回证据。
- verifier 能真实检查文件内容和边界。
- 父节点会在 acceptance 前整合子节点声明的 `changed_files`。
- acceptance agent 能真实完成父节点整合验收并让 root 进入 `done`。
- 源头 Codex CLI 能真实生成项目宪法，并明确 `TARGET_PATH` 是原始根仓库上下文，叶子交付必须相对分配的 `WORKING_DIRECTORY`。

## 浏览器看板验证

目标：`http://127.0.0.1:5173`

结果：

- 中文按钮可见：`生成草案`、`保存修订`、`锁定宪法`、`启动任务树`。
- 任务树连线可见，DOM 中存在 5 条 React Flow edge。
- 生成草案后状态为 `草案`，保存和锁定按钮可用，启动按钮仍禁用。
- 保存修订后宪法版本从 `v1` 变为 `v2`，修订文本保留。
- 锁定后 Markdown 和结构化 JSON 编辑区变为禁用，保存和锁定按钮禁用，启动任务树按钮可用。
- 浏览器 console error 数量为 0。

## 本轮新增或修复

1. 强模式 Codex CLI
   - 新增 `CODEX_BATTLEFIELD_CODEX_MODEL`，默认 `gpt-5.5`。
   - 新增 `CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT`，默认 `xhigh`。
   - 真实脚本新增 `--model` 和 `--reasoning-effort` 参数。
   - 最终真实验证使用 `--constitution-runner real` 覆盖源头宪法生成。

2. 调试文件预览
   - 新增 `GET /api/runs/{run_id}/artifact?kind=prompt|logs|last_message`。
   - 只允许读取 battlefield 工作区内的调试文件。
   - 前端运行记录可直接查看 Prompt、日志、最后消息。

3. 递归防失控
   - 同一节点并发重复启动会跳过。
   - 重复子任务签名会进入人工审查。
   - 父节点 acceptance repair 超过上限会进入人工审查。

4. 父节点整合
   - acceptance 前会将子 worker 声明的相对 `changed_files` 从子 worktree 汇总到父级/根目标。
   - 绝对路径和越界路径会被跳过并记录事件。

5. 工作树边界契约
   - 宪法生成、planner、verifier prompt 明确：`TARGET_PATH` 是原始根仓库上下文；叶子只能写分配的 `WORKING_DIRECTORY`。
   - verifier 不会因为文件在叶子 worktree 而误判失败；父节点整合负责把 `changed_files` 合并回父级或根目标。
   - 新增 prompt 回归测试锁定该规则。

6. 宪法人工流程
   - 前端不再一键自动锁定宪法。
   - 用户可以先生成草案、编辑 Markdown 和结构化 JSON、保存修订、锁定后再启动任务树。

## 已知边界

- 当前系统链路已经验证完成，但还没有运行完整的 2D 横板过关格斗游戏大任务。
- `gpt-5.5 + xhigh` 会明显变慢；真实递归 probe 单轮大约需要数分钟。
- 前端 build 有 Vite 大 chunk 提示，当前不影响功能。
