# AGENTS.md — 在这个仓库里干活前先读这页

本仓库是 **Codex Battlefield**：一个本地控制塔，驱动**真实 Codex CLI** 递归拆解任务树、
逐叶验证、按事实清债、跨休眠自恢复。所以这里的代码风格与"先跑起来再说"的工程不同：
**证据优先、静默失败视为缺陷**。

## 阅读顺序

1. `docs/README.md` —— 阅读地图（哪份文档回答哪个问题、有哪些反缩水约定）。
2. `PLAN-STATUS.md` —— 分步计划与**按事实写的**完成状态（含"已回退/未做"的标记）。
3. `VERIFICATION_STATUS.md` —— 最近一次验证的证据与口径。
4. 具体设计看 `docs/spec-*.md`；发布流程看 `docs/README.md` 的「给维护者：再发布一次」。

## 五条硬约定

1. **文档即事实**：能标日期的历史证据不动；自称"当前"的读数必须跟事实走。改完代码要同步
   `PLAN-STATUS.md`、`VERIFICATION_STATUS.md` 与相关 spec 的"实施状态"小节——**包括如实写下没做成的事**。
2. **验证比声明重要**：搬移/重构类改动要给出"搬前搬后等价"的证据（sha256 / 逐字符比对等）；
   接线类改动要断言**可观测结果**（不是"函数被调用过"）。这一带最常见的坑是"接线看着在、实际是死的"。
3. **测试自己也会撒谎**：先证明测试真的在测你要测的东西（曾因漏注入一个 env 变量，让"优先级"用例
   实际在测先到先得）。反面自检（参数传错位也要能测出来）值得写。
4. **不静默放宽**：任何"自动过期/自动放行/自动清债"都要有**可核对的事实**做依据；债务类对象不设自动过期。
5. **判据必须写明失效方向**：每加一条判据（门禁 / 回收 / 清债 / 隔离 / 仲裁），代码里必须写清
   **反常时往哪边倒**（放行还是阻断）与**默认动作**，并为它写一条"**这条断言会不会恒真 / 恒假**"的反面自检。
   这条来自 2026-09-21 那轮审计的总结：15 处缺陷里大多数不是逻辑写错，而是**方向没定**或**只看过一种输入**——
   例：读不到文件被当成"文件没了"（那边是**无条件阻断**）→ 一次文件占用把父节点打进修复；
   回收判据只查"路径存在"→ 同名旧文件让它放行，删掉节点唯一一份新产物；
   一条断言两边都是 0 → 恒真，永远通过。

## 对抗性审计（每波收口做，不是出事才做）

2026-09-21 那轮：4 个**只读** subagent，每个只盯一个波次的**实现**，约 1 小时，确认 15 处产品缺陷
（其中一条会**静默丢掉节点产物**）。代价远低于价值，所以固定成常规动作：

1. 每路给清：目标波次 + 允许读的文件清单 + 禁止事项（**只读、不许跑测试**——两个 pytest 会共用测试库）。
2. 每条结论必须给出 `file:line` + **触发序列**（什么状态、什么调用顺序），并显式区分"已确认"与"**未能确认**"。
3. 必须同时给出"**已核实但不是缺陷**"的清单：否则"没找到"会被读成"没问题"。
4. 收口时把结论写进 spec 的「审计修正」小节：改了什么、为什么、**边界在哪（含已知限制）**。

## 重构手势（抽模块时照这个走）

1. 先跑量具测闭包（**不要**按方法组表面行数估）：
   `python scripts/audit_orchestrator_movable_mass.py <文件> --closure <入口方法名,逗号分隔> --state`
2. 一次性脚本：现取行号 + **起始行断言** + 落盘前 `ast.parse` + 字符串内容行逐字节保护。
3. 原位留**同名薄委托**（零调用点改动），必要时用 ports 传配置标量。
4. 补反漂移测试（委托与模块函数给出同一答案）+ 搬前搬后等价性证据。

## 常用命令

```powershell
cd api ; .\.venv\Scripts\python.exe -m pytest -q          # 后端全量
.\.venv\Scripts\python.exe scripts\verify_all.py          # schema + 排版 + 语法 + pytest + 前端三项
.\.venv\Scripts\python.exe scripts\check_markdown_layout.py .   # 文档排版自检
python scripts\scan_history_privacy.py --paths-only       # 发布前隐私扫描（只读历史）
```

## 环境注意（Windows）

- cmd 里中文输出乱码是**代码页**问题：命令前加 `chcp 65001 >nul &`。
- 后台任务的完成通知只在**下一次动作**时可见，没有定时唤醒 → 需要结论就**前台阻塞**跑
  （仓库里有 `workspace/_run_with_timeout.py <秒> <pytest 参数>`：硬超时 + 拒绝并发）。

## 已知坑速查（每条都有对应的 spec 小节记录）

- `ast` 的 `col_offset` 是 **UTF-8 字节偏移**——含中文的行上按字符累加会让整条编辑错位。
- `TaskNode.updated_at` 是 **naive UTC**，直接 `.timestamp()` 会按本地时区解释（UTC+8 下"刚完成"变成 8 小时前）。
- Pydantic 默认 `extra="ignore"` 会**静默丢掉**未建模的返回键；`TreeResponse.stats` 是**有类型的 dict**，
  往里加字段必须先加模型。
- markdown 表格里代码跨度的 `\|` 是合法字面竖线，切单元格时必须跳过。
- **"证据在不在"要验内容，不能只验路径存在**：删目录/清债这类判据若只查 `exists()`，会被**同名旧文件**蒙过
  （节点改的多半是工程里本来就有的文件）→ 判成"已归档"然后删掉唯一一份新产物。
- Windows 上 `subprocess.Popen.pid` 与子进程自己的 `os.getpid()` **未必相同**（`sys.executable` 可能是转发器）：
  测试里拿 pid 当身份会得到一条看起来像"顺序错了"的假失败。要断言"谁先发起"就传一个显式序号。
- 两处复用同一个 `snapshot` 函数、一边是"记基线"一边是"查回退"时，函数内的 `except: continue` 语义会反掉
  （记不了 = 少一条防线；查不了 = **假回退**，而假回退往往是无条件阻断）。读盘失败必须单列。
- `pip install .`（非 editable）会在源码树里留下 `build/lib/app/**` 的**源码副本**——它长得像源码但不是源码，
  改错文件的代价很高。所以：**不要用 `git add -A` 一把梭**，先看 `git status --short` 再决定加什么；
  `build/` 已在 `.gitignore` 里（2026-09-22 有一次 39 个文件误入库，已移除）。
- 进程内驱动 `Orchestrator` 时，`CODEX_BATTLEFIELD_DATABASE_URL` 必须**早于任何 `import app.*`** 设置：
  `app.config` 的字段默认值是在**导入时**求值的，晚一步就悄悄连到别的库去了（实测：一整批基准跑进了开发库）。
- **目标目录若位于另一个 git 仓库内部**，`_assign_worktree` 会对外层仓库执行 `git worktree add`：
  节点拿到的是**外层仓库的完整检出**，而不是目标目录本身（实测：基准的目标目录放在控制塔仓库里，
  被测 agent 在 worktree 里看到 `bench/fixtures/**`，就把它当工程改了）。目标要自成一体，
  或放在任何仓库之外；CLI 也要显式指定 schema 目录（`workspace_dir.parent/schemas`）。
- **交付物在节点 worktree 里，`target_path` 只是种子**：父/根节点把子节点产物集成进**自己的 worktree**，
  没有任何一步回写 `target_path`（产品自己的烟测也只在各节点 worktree 里找产物）。
  断言打在 `target_path` 上会得到"树说 done、外部判据说没交付"的**假缺陷**（本轮踩过一次）。
- Windows 上 `codex` 是 npm 的 `.cmd`：`subprocess.run(["codex", ...])` 直接 `FileNotFoundError`
  （`CreateProcess` 不认 `.cmd`，也不做 `PATHEXT` 解析）。要嘛用 `shutil.which` 解析出的完整路径，
  要嘛走 `shell=True`。写诊断脚本时最容易漏这条，而它的表现是"真实通道跑不起来"的假结论。
- **门禁的"覆盖范围"和门禁本身一样会坏**：`verify_all.py` 曾经是 `cwd=api` 下跑 `ruff check .`——
  仓库根的 `scripts/**` 从没被任何门禁看过。代价是一次真事故（2026-09-22）：`bench_run.py` 里
  `Path(...)` 少一个 import（F821），只在 **real 档**才走到那一行（mock 档没有 worktree，短路了），
  于是连续两轮真实基准都在"判据求值前"崩掉，而门禁全绿。现在门禁路径由 `LINT_PATHS` 单一来源决定，
  并有测试盯着它（`api/tests/test_repo_gates.py`）。
- **"进程无声消失"要先问"它有没有留下中间态"，而不是先猜"卡住了"**：那次两轮真实基准的现场是
  "DB 停在 node=`verifying`、报告文件根本不存在"，看起来非常像"产品卡死"——真实原因是异常穿出了循环
  而报告只在**成功**路径上写。现在基准**每个任务后 + 每 60 秒心跳**都落一份 `partial=true` 的报告，
  并在异常路径上也落一份（写到一半的 json 比没有 json 更误导，所以落盘走 tmp + `os.replace`）。
  另一个前提：**前台命令有 10 分钟上限**，超长的基准必须放后台跑，否则被工具层掐掉时连 traceback 都看不到。
- **同一份代码在 mock 档与 real 档会走不同分支**（mock 没有 worktree / 没有真 CLI），
  所以"mock 绿了"不等于"real 档那条路径被走过"。凡是 `if real 才走到` 的新代码，验证时必须真跑一次 real。
- **事件循环一旦卡住，asyncio 的定时器是"一起失效"的**（实测 2026-09-22，两次复现）：真实档跑
  unsolvable 任务时，60s 心跳、600s CLI 看门狗、1500s 任务上限、连 `faulthandler` 的 120s dump
  **全部不再触发**；`py-spy dump` 显示主线程停在 `_poll` 的 `GetQueuedCompletionStatus`，子进程
  （codex）后来自己退出了，父进程照样不醒。**结论：兜底超时不能用被它保护的东西实现** ——
  进程内是 `scripts/bench_run.py --hard-stop N`（普通线程 + `os._exit(3)`，先落报告与全线程栈），
  进程外是 `scripts/bench_supervise.py`（看报告 mtime 无进展就 `taskkill /T /F` 并落监督说明）。
  **但边界必须记住**：第二次复现时连硬停线程都失效了——4 个线程全部 parked、CPU 不再增长、
  普通线程的定时 `Event.wait` 也不返回（`faulthandler` 的 dump 线程同时停摆）→ 那是**整个解释器冻住**，
  只有进程外的监督器可靠。查这类现场：`py-spy dump --pid <pid> --json`（`api/.venv` 里装了 `py-spy`）
  + `Get-Process -Id <pid> | %{ $_.Threads }` 看原生等待状态；要 C 层栈得上 `procdump`/WinDbg（没做）。
- **规划器返回 `blocked` 时默认会转成"规划阻塞恢复"子任务继续递归**（环境性阻塞下会变成螺旋：
  实测 3-5 分钟到深度 4、并发油门 1→3、标题叠成"…：规划阻塞恢复：规划阻塞恢复：…"）。
  现在由 `blockers.blocked_recovery_suppression` 单一处决定"该不该拆"：恢复链**只允许一层**、
  可等待重试的不拆、工具内部先修工具、没有 description 不拆；planner / acceptance / `recovery.py`
  三条路径共用这一处（改漏一条，螺旋就从那条路回来）。**另外**：`prompts/planner.md` 现在要求
  `blocked` 必须带 `block_report.category`——`environment`（策略/沙箱/权限/工具不可用）**一次都不拆**
  （递归撞的是同一堵墙），`task_internal` 才允许试一次。识别 + 兜底是两层判据，缺一层都不行。
- **"编排器自执的不变量"混进 `global_acceptance` → 每层一条永远清不掉的假债**（实测 2026-09-22：
  7 个节点各记 3 条 `constraint_coverage_gap`，内容全是"父节点必须通过整合验收后才能 done"这类
  **没有任何子任务能交付**的东西）。凡加一条会被"逐字继承核对"的约束，先问：**谁能交付它**？
  子任务不做就算它没达标 → `global_acceptance`；不管子任务做什么、系统本身都必须这么运转 →
  由编排器自执（工具侧常量 + 渲染进宪法 markdown，**不进** `global_acceptance`）。
  顺带一条口径更正：这类债是 `advisory`，**不直接挡 `done`**——别把"看板上有债"说成"它挡着收敛"。
- **"声明即必填"是 schema 门禁的规则，例外表只有一处且被测试盯着**
  （`verify_all.OPTIONAL_SCHEMA_FIELDS`：`planner-decision.block_report` 只在 `blocked` 时才该出现，
  强制必填会逼真实 CLI 每次回 `null`）。想加例外就得改
  `api/tests/test_repo_gates.py::test_optional_schema_fields_allowlist_stays_minimal`
  ——也就是说"加一条"必须自己解释一遍理由。嵌套对象里的字段如果是"有报告才该有"，
  **在嵌套对象内设 required**（例如 `blockReport` 的四个字段），而不是往例外表里塞。
- **派生缓存的读面必须把"快照时刻 + 与真相的差异"和数一起给**（`obligation_index` → `GET /api/obligation-index`，
  spec `spec-parent-child-obligations.md` §9.1）：缓存没建过（`built_at=null`）或已跟真相漂了（`in_sync=false`）时，
  `total=0` 与"真的没有债"长得一模一样 → 界面会把 0 读成"很干净"，方向正好反了。
  所以：读面**只读、不自动重建**（自动重建会抹掉"为什么漂了"，下一次照样漂；重建留给显式脚本），
  范围必须显式（问错范围返回 422，不静默落默认），并且**测试要盯着"未建/漂移时不许读成没有债"**。
  **补充（2026-09-24 审计）**：这类读面上"可信度"字段的口径本身也会坏 ——
  `built_at` 若取**过滤后子集**的 max，`total==0` 就恒等于 `built_at is None`，
  于是"索引建过、只是这个范围没有债"永远显示成"读不出来"（那句文案是死分支）。
  口径要定成：`built_at` = **全表**快照时刻，再配一个"真相里几条"（`projected`）来区分
  "没建过"与"真的没有债"。同理，**别把"什么时候被动过"算进漂移判据**：
  `source_updated_at` 一进去，活跃树上 `in_sync` 近乎恒假（`orchestrator` 里 17 处改 `updated_at`），
  真漂移与"节点只是被动过"混成一句 → 两种都被忽略。

- **工具脚本的"默认库/默认路径"必须和产品算的是同一个**（实测 2026-09-24：
  `scripts/rebuild_obligation_index.py` 默认 `workspace/codex_battlefield.sqlite3`，
  而 `app.config` 默认 `workspace/battlefield.sqlite3`）：不带环境变量跑 `--check` 就会去问一个
  **不存在的空库**，输出 `projected: 0 / in_sync: true` —— 索引唯一的写入口自己报了个**假全绿**，
  失效方向是最坏的那个。回归测试要比**两边算出来的值**（URL / 路径），不是源码文本。
  这类缺陷单测抓不到（测试全都显式指定库），只有真跑一次才暴露 —— 又一条"mock 绿 ≠ 真跑过"。

- **状态表会被后面的波次悄悄作废**：`PLAN-STATUS.md` §2 的状态是"写下那一刻"的判断，后续波次顺手把
  某一行做掉后**没人回来改表**（实测 2026-09-24：R2 表里写"未开工"而 §2.12 写着 `6370b30` 已做；
  W2b 表里写"暂缓"而 §2.10 写着按角色分层已做并有 7 例测试）。失效方向很坏：接手的人按表挑
  "下一件待做"→ **重做已经做完的东西**。所以**每波收口必须回看 §2 表**，并把过期行改成
  "已完成（原标 X 是过期标记，<日期> 核实）"——保留原标记才看得出漂过。
  同理，自称"当前"的读数（如 `orchestrator.py` 行数、pytest 用例数）要么现测现写，要么标日期降级为快照。

- **asyncio 的 `Process.wait()` 等的是"进程退出 + 所有管道 EOF"，不是"进程退出"**
  （2026-09-24 根因，代价是 09-22 两轮真实基准的"静默 30-60 分钟"）：`BaseSubprocessTransport._try_finish()`
  只有在进程已退出**且所有管道 disconnected** 时才 `_call_connection_lost` → 才唤醒 `wait()` 的 waiter。
  Windows 上 codex 是 `.cmd` → `cmd.exe` → `node` → 孙进程，任何一个**已脱离**的后代攥着 stdout 写端
  （`taskkill /T /F` 抓不到它），EOF 就永远不来 → 无超时的 `await process.wait()` 无限挂着，
  而事件循环**一条定时器都没有**（`_poll(ms=4294967295)`），看起来就像"卡死"。
  所以**停止路径上的任何等待都必须有上界**，超时要写明原因并主动 `transport.close()` 关掉本端
  （不关就一直被孙进程绑着，退出时留 `unclosed transport` + `Event loop is closed` = 每次放弃漏一副句柄）。
  连带一条：`subprocess.run(capture_output=True)` / `communicate()` 同样要等 stdout EOF，
  **也会被同一个孤儿卡住**（写这条的自检脚本自己先挂了 60s）——要"只等父进程退出"就把 stdout 重定向到文件。
- **`asyncio.wait_for` 超时会把里面那个任务 `cancel` 掉，`asyncio.wait` 不会**：想在超时后**再给一次机会**
  拿同一个结果，只能用 `asyncio.wait({task}, timeout=…)`。用 `wait_for` 的话第二次 `await` 永远拿不到值，
  那条判据就**恒定**返回兜底值（本仓库的例子：退出码恒 124，"关了管道还是拿不到码"与"真的拿不到"再也分不开）。
- **取证工具的读数本身也要先验一遍**（2026-09-24 实测）：
  `py-spy dump --native` 的 **OS 帧与 Python 帧可信，CPython 内部帧名不可信**（Windows 上按导出表近似，
  把 `_PyEval_EvalFrameDefault` 写成 `PyThread_GetInfo`）；要准确原生帧就读 dump：
  `cdb -z <dump> -c "~*k 30; q"` + 符号路径 `srv*C:\Symbols*https://msdl.microsoft.com/download/symbols;<pdb 目录>`，
  CPython 的 pdb 要 `core_pdb.msi`（`python312.pdb`）**加** `lib_pdb.msi`（`DLLs/_overlapped.pdb`，
  扩展模块要它才出真名）；`cdb.exe` 来自 **Windows SDK 的 Debugging Tools**，WinDbg 那个 Store 包里没有。
  `procdump` **成功时退出码是 1**、还会展开文件名里的 `PID`/`PROCESSNAME` 宏 → 判"抓到没抓到"
  只能看**磁盘上新出现的非空 `.dmp`**，按退出码判会得到相反的结论。
- **cmd 里 `%CD%` 和 `%ERRORLEVEL%` 一样是解析期展开**：`cd /d X && msiexec /a "%CD%\y.msi"` 里的
  `%CD%` 是**改目录之前**的值，于是拿到一个不存在的路径，报错却是"此安装包无效"（指向完全错误的方向）。
  用绝对路径，或 `cmd /v:on` + `!VAR!`。顺带：`timeout /t` 在重定向环境下报
  "Input redirection is not supported"，要等就用 `ping -n`。

## 会话交接

每次会话的收工交接放在 `workspace/HANDOFF-<日期>.md`（该目录已 gitignore，不进发布快照）。
新会话的起手式：读本文件 + `PLAN-STATUS.md` + 最近的 `workspace/HANDOFF-*.md`。
