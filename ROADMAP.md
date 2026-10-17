# 长期开发路线（ROADMAP）

> 这份文档回答一个问题：**接下来按什么顺序做什么，为什么是这个顺序。**
> `PLAN-STATUS.md` 是**往回看**的状态锚点（做过什么、证据在哪）；本文件是**往前看**的排期。
> 判据优先于功能：每个阶段都写清"怎么算做完了"，而不是"做了哪些东西"。

## 0. 出发点（2026-10-15 实测事实）

先把"现在到底在哪"钉住，否则排期就是空谈。

| 维度 | 现状 | 证据 |
|---|---|---|
| 能力 | 目标 → 宪法 → 任务树 → 多角色 agent → 父子债务验收；管家（可批准执行 + 主动汇报）；看门狗（心跳 + 停摆可判） | 本文件所在提交 |
| 质量 | `verify_all` 全绿 **1036 passed**；含 PowerShell 编码门禁、schema 严格校验 | `scripts/verify_all.py` |
| 干净 clone | `py -3.12 -m venv` + `pip install -e ".[dev]"` → 起 API → 管家对话/建树全部跑通（mock，无需凭据） | 本次实测 |
| 运行形态 | 需要 **4 个进程**：API(8765)、Web dev(5173)、看门狗计划任务、模型代理(可选) | `Get-NetTCPConnection` |
| 前端 | 单文件 `App.tsx` **2883 行**；轮询 `setInterval` 10s；手写 CSS | `web/src` |
| 后端 | `orchestrator.py` 万行级；35 个 venv 包 | `pip list` |
| 分发 | **无**打包/安装器；使用者需自备 Python 3.12+ / Node 20+ / pnpm | 仓库根目录 |
| 公开性 | 本仓库 = 可公开的通用版（厂商词 0）；内部 provider 构建在**独立的私有仓库** | `git log`、扫描脚本 |

**一句话结论**：内核已经能干活，卡的是**"别人怎么装、怎么看、怎么不被它烧钱"**。

## 阶段划分总览

```
P1 上手      ── 一条命令跑起来          （解锁"发给别人试"）
P2 操作      ── 不碰命令行也能管         （解锁"不写代码的人用"）
P3 规模      ── 多工程 + 花可控          （解锁"当日常工具用"）
P4 管家×看门狗 ── 从各自为政到分工协作    （解锁"它自己看住自己"）
P5 工程化    ── 性能 / 测试 / 数据        （解锁"长期跑不烂"）
P6 兼容      ── 跨平台 / 跨模型 / 容器    （解锁"谁都能部署"）
```

顺序理由：**P1 不解决，后面全是自娱自乐**（没人拿得到）；**P3 不解决，用两天就弃用**（烧钱且看不见全局）。P4/P5/P6 是真需求但可以晚。

---

## P1 上手：一条命令跑起来

**要解决的痛点**：今天要 clone、建 venv、装 pip 依赖、装 pnpm 依赖、起两个进程、再开浏览器。给同事试，第一步就劝退。

| # | 交付物 | 判据（怎么算做完） | 状态 |
|---|---|---|---|
| P1-1 | `scripts/setup.ps1` 一键准备：探测 Python/Node 版本、建 venv、装依赖、打印下一步 | 在**全新机器**上从 clone 到可启动 ≤ 1 条命令；版本不符时给**可执行**的修复建议（不是"请安装 Python"） | 已落地（`-Check` / `-SkipWeb`） |
| P1-2 | `scripts/start.ps1` 一键启动：API + Web，健康检查通过后再打开浏览器 | 一条命令后浏览器自动打开到可用界面；重复执行不重复起进程 | 已落地（幂等 + 失败打日志尾部） |
| P1-3 | API 托管前端产物（可选静态托管） | `pnpm build` 后只起 API 即可用完整界面（**不再需要 Vite dev**） | 已落地（`StaticFiles(html=True)` 挂在 `/`） |
| P1-4 | `scripts/doctor.ps1` 自检：版本、端口占用、状态根可写、provider 可达、看门狗注册状态 | 每项给出"OK/问题 + 怎么修"；退出码能用于 CI | 已落地（warn 与 fail 分开，`-Json`） |
| P1-5 | 首次运行引导（界面内）：未绑定工程时给出下一步动作 | 新用户打开界面不会只看到"未绑定"而不知道该干嘛 | 已落地（`ProjectOnboarding` + `GET/POST /api/projects`） |

> **P1 已全部落地。** P1-5 实现时抓到一个真实缺陷：`POST /api/projects/bind`
> 起初会把**不存在的目录**成功挂载（`Z:\definitely-not-here` → 200），
> 用户在界面上打错一个字母只会看到"绑定成功"。现在界面绑定要求目录存在
> （CLI 保留"先绑后建"），并由 `test_project_binding.py` 的 B1 钉住。

**工作量估计**：2–4 天（主要是边界情况：Node/Python 缺失、权限、代理、中文路径）。
**风险**：`setup.ps1` 容易变成"在我机器上能跑"。对策：判据必须在一台**没有本项目环境**的机器上验一遍。

> 已修的实际断点：`setup.ps1` 第一版从**仓库根**跑 `pip install -e .`，而
> `pyproject.toml` 在 `api/` 下 —— 报 "does not appear to be a Python project"，
> 且那句话不会提示路径错了。这类错误只有"真的在干净 clone 里跑一遍"才抓得到。

---

## P2 操作：不碰命令行也能管

**要解决的痛点**：能跑起来之后，日常操作仍要敲命令（绑工程、看日志、改配置、清状态）。非开发者到这里就停了。

| # | 交付物 | 判据 | 状态 |
|---|---|---|---|
| P2-1 | 界面内**绑定/切换工程**（现在只能 `battlefield-project use`） | 不敲命令行可完成绑定、看到当前绑定的绝对路径 | 已落地（P1-5 顺带完成） |
| P2-2 | **运行时配置面板**：模型档位、沙箱、推送阈值、主动汇报开关 | 改完有明确生效说明（哪些要重启）；有"恢复默认" | 已落地（`/api/settings` + `SettingsPanel`，19 项） |
| P2-3 | **日志与产物查看器**：节点 → 运行记录 → prompt / 事件流 / 最后消息 | 一次点击能看到"这一步为什么错了"，不需要手工翻 JSONL | 已落地（`RunInspector`，复用已有产物端点） |
| P2-4 | **一键导出诊断包**：状态快照 + 相关日志 + 版本信息（脱敏） | 出问题时能把这个包发给别人，不用来回问 | 已落地（GET /api/diagnostics + 界面按钮） |
| P2-5 | 状态根/数据**迁移与重置**引导 | 换机器、清空重来都有界面路径，降低"搞坏了不敢动"的恐惧 | 已落地（`/api/state/*` + `StateAdminPanel`：概览只读、迁移先预演、重置要抄 key） |
| P2-6 | 界面双语（中/英）+ 术语表 | 外部使用者能看懂关键词（债务、宪法、验收、休眠…） | 已落地（中英切换 + 15 条术语的"跟日常用法的差别"；覆盖范围由 `localized.ts` 声明，8 条不变量 + 语言检测共 32 例，逐条做过变异测试） |

**工作量估计**：1.5–3 周（P2-2/P2-3 是主要工作量）。
**风险**：配置面板一旦"看起来能改"就会有人改坏。对策：**危险项（沙箱降级、并发上限）需要二次确认 + 写明代价**。

---

## P3 规模：多工程 + 花可控

**要解决的痛点**：两个真实弃用原因 ——（a）**烧钱不可控**，跑起来才发现停不住；（b）**看不见全局**，多工程时管家只看得见当前工程。

| # | 交付物 | 判据 |
|---|---|---|
| P3-1 | **每棵树预算上限**：token / 金额硬闸（不是事后统计） | 达到上限时**自动暂停并明确告知**；有"允许再花 X"的一次性放行。**部分落地（2026-10-18 纠正一处口径）**：原来这里写的是“token / 金额硬闸已落地”，**核对代码后不成立** —— `budget.py` 的守卫是**时间窗**（`set_budget_guard` 收的是 `resume_after_seconds`，把整树休眠到额度刷新时刻），加上 **agent burst**（每窗口允许起几个 agent），**没有**“累计 token 超过上限就暂停”这道闸。**已落地的三件**：① 时间窗守卫（触发时先写事件再整树休眠，理由落在节点与事件里，判据 `api/tests/test_budget.py`）；② agent burst 上限与预警档（`budget_warning_state`）；③ 一次性放行走 `clear_budget_guard` + `/api/steward/pre-authorization`（不是把上限改大）。**第二道闸已落地（2026-10-18，`budget_token_ceiling`，默认 0=关）**：每次起 agent 前用 `cost.cost_report` 统计这棵树的累计 token（与 P3-3 同一套规范化，不另写口径），超上限就 `set_budget_guard` 整树休眠 + 写 `budget.token_ceiling` 事件（带 used/ceiling/phase），放行仍走人工清守卫或预授权；判据 `api/tests/test_budget_ceiling.py` B1–B6（B4 是接线判据：删掉钩子就红；B3b 守“守卫期间不叠事件”）。代价写进代码与文档：每次起 agent 扫一遍该树的 run_records，所以默认关闭。**金额闸也已落地（同日，`budget_cost_ceiling` + `budget_cost_currency`，默认 0=关）**：金额只有端点自报、币种还可能不同，所以它比 token 闸多一条**必须先回答的问题**（“我知道花了多少吗”）：读不到金额 / 没配币种 / 记过的是别的币种 → **不暂停**（拿猜测的币种停树就是误报），但写一条 `budget.cost_unreadable` / `budget.cost_currency_absent` 事件说明为什么没生效；真超了才休眠，事件里带 used/ceiling/currency 与 `other_currencies`。判据 `api/tests/test_budget_cost_ceiling.py` C1–C8（C7 接线判据、C8 钉“不跨币种换算”：别的币种再大也不许拿去暂停树）。**仍未做**：跨币种换算（**有意不做**，需要汇率来源与口径；C8 把“不换算”这件事钉住了，见 `docs/cost-accounting.md` §2）（P6-4 的实测给了它一个具体的成本场景：一轮递归探针自己拆到 6 层、吃掉 24.1M input —— 现在只能靠人事先设时间窗或 burst 上限，不能靠“这棵树已经花了多少”自动停下）
| P3-2 | 成本**预测**：按当前进度外推"跑完还要多少" | 界面显示"预计还需 X"，与最终实际值在同一量级。**已落地**：给区间而不是单个数（下界=已完成均单价×剩余节点，上界再乘重试系数×2），样本不足时**拒绝给数**；界面带样本量与"很粗"提醒 |
| P3-3 | 成本归因：按节点/角色/模型拆开 | 能回答"钱花在哪一层"，且与网关账单口径对得上。**已落地**：三维归因 + 界面面板；取不到模型名的运行**单独计数不造假桶**，币种分开不跨币种相加 |
| P3-4 | **跨工程总览**：所有工程的树状态、花费、待办动作 | 管家能回答"我所有工程现在怎么样"（现在只能答当前工程）。**已落地**：后端只读聚合（不建库、按币种分桶、读不到≠0，16 例）+ 界面面板（按"要不要我动手"排序，17 例） |
| P3-5 | 自动策略：连续失败自动停、收益递减自动降并发、空闲自动休眠 | 每条策略可关；触发时在界面与对话里都留痕。**已落地**：4 条具名策略（+僵死槽位回收），4 个开关进设置面板；`GET /api/auto-policy` 报告**每条**策略的开关、理由与触发依据（能分辨"没开"与"没到条件"），界面三态显示 |
| P3-6 | 大规模压力：千节点树的列表/轮廓/查询性能 | 1000 节点下界面可交互（首屏 < 2s），状态快照不超上下文预算。**后端已落地**：真实 1000 节点树实测首屏端点 130ms、荷载 640KB（阈值 700ms/1MB，4–7 倍余量），14 例判据含规模比（< 8，抓 O(n²)），变异测试能红；**前端首屏：结构性成本已定位、部分改善，但"< 2s"**未稳定达标**（2026-10-17 多次实测 `cold=1601 / 1891 / 1813 / 2236 / 2294 / 2351ms`；且同一版代码里"页面取树 TTFB"会在 **239–1119ms** 之间摆 —— **变异测试 + 重复测量推翻了我先前"已达标"的说法**，见下）。**已修掉抖动源**（2026-10-18）：把统计口径的缓存 TTL 从 5 秒放宽到 20 秒（`_codex_slots_active`），请求路径不再同步跑那次 ~0.7 秒的 WMI 扫描 —— 调度决策那条仍用 0.5 秒窗口现场扫（判据 `api/tests/test_external_scan_staleness.py` S1–S3，变异改回 5 秒 → S1 红）；改后两次复测：取树 **270–420ms**、`cold` **1696 / 1236ms**（都达标），见 `docs/scale-baseline.md` §5 ⑪。这条是一路量出来的：先修掉"与树并发的两条侧栏重活"（`auto-policy` 1343ms、`watchdog/state` 1295ms）→ cold 4406→3033ms；再用 cProfile 定住"第一次 `get_tree` 的 0.895s 是 **PowerShell/WMI 全表扫进程**（缓存 TTL 只有 2s）"→ 改成 WQL 服务端过滤（0.895→0.672s），并加**启动预热 + 后台每 15 秒刷新那份缓存（cadence 由 1.5s 放宽，见 docs/scale-baseline.md §5 ⑨）**。**但后者的效果不可复现**：首次加载仍常见 ~1.1s（"久静之后第一次请求付扫描"这一形态还在），第二次加载稳定 ~250ms —— 也就是说缓存刷新这条路只覆盖了一部分。判据在 `web/e2e/perf-first-screen.spec.ts`：目标 `TARGET_MS=2000`、棘轮 `RATCHET_MS=6000`、回归钉子"两次加载里**较好的一次**取树 TTFB < 800ms"（**变异验证过**：把后台刷新注释掉 → 判据红，1098ms）。**未验证**：慢机器/CI 上的首屏（判据在 `process.env.CI` 下跳过）；**仍未查清**：久静后第一次请求那 ~1.1 秒的真正归属（客户端渲染已用 CDP profiler 排掉大半：a11y 是测具的账、应用自身 ~0.2–0.3s、GC ~0.26s） |

**工作量估计**：2–4 周。
**风险**：P3-1 的"硬闸"要防**误停**（把正常长跑当成超支）。对策：默认上限宽松 + 触发前先预警 + 一键放行。
**说明**：`RESOURCE_POOL_DESIGN.md` 里那份"资源池"设计（共享账号/额度池）属于本阶段，但它依赖外部账号体系，**不要**作为 P3 的前置。

---

## P4 管家 × 看门狗：从各自为政到分工协作

**要解决的痛点**：现在两者互不打扰，但**不能互相委派**。管家想"起一棵树/调并发"只能建议你去点；看门狗发现问题也只能写日志。

| # | 交付物 | 判据 |
|---|---|---|
| P4-1 | **双通道**：管家可主动投递目标并跟踪进度 | 管家起树后能持续报告"第几步、卡在哪"，而不是起完就断线。**已落地**：`goal_deliveries` 表 + `GET/POST /api/deliveries`；起树时服务端**自动记一次投递**；管家上下文里带 `delivery` 段（每次被问到时读**实时**进度，不做后台推送）；界面 `DeliveryPanel`（卡点默认展开） |
| P4-2 | 看门狗**可调**：并发档、额度闸、休眠窗口可从界面/管家处设定并留痕 | 调整有审计记录（谁、何时、为什么），可回滚。**已落地**：8 项档位封闭登记表 + 硬边界（并发 0 直接拒）+ **先写审计再改配置** + 一键回滚（目标=上一次的旧值）+ 界面面板 |
| P4-3 | **双向可见性闭环**：管家主动汇报里包含看门狗结论；看门狗尊重管家标记的"人工介入中" | 双方都不会在对方已处理的状况上重复动作。**已落地**：共享「注意账」（`attention_claims`）—— 谁要处理谁记账、处理前先查账；认领**必须过期**、只有认领人能释放、强制解除要留名；管家侧早已带看门狗结论（`steward_status._watchdog`），查询入口 `GET /api/attention` + 界面 |
| P4-4 | 动作**风险分级**：低风险（重试/续跑）可预授权，高风险（取消/全停）永远需确认 | 预授权有明确范围与期限；越界一律拒绝。**已落地**：三档分级 + `PreAuthorization`（范围/期限/署名三样必填，**没有无限期形态**）；高风险是**常量判断**不可预授权；`GET/POST/DELETE /api/steward/pre-authorization` + 界面面板（高风险不可勾） |
| P4-5 | 管家可写**结论文档**：把一次事故的来龙去脉整理成可归档记录 | 事后不需要问"当时为什么这么决定"。**已落地**：`incident_conclusions` 表 + `GET /api/conclusions/draft`（**不落库**，代码抽事实、人的判断留空）+ `POST /api/conclusions`（`authored` **必填**）+ `.md` 导出 + 界面面板 |

**工作量估计**：2–3 周。
**风险**：预授权是权限模型的实质性放宽（当前默认只读是有意选的）。对策：**默认关**，开启时界面反复确认，且高风险动作永不可预授权。

---

## P5 工程化：性能 / 测试 / 数据

**要解决的痛点**：长期跑的三个隐患 —— 慢、测不到、长不干净。

| # | 交付物 | 判据 |
|---|---|---|
| P5-1 | 后端异步化与慢查询治理 | 明确给出 p95 延迟；最慢 5 个端点有基线且不再退化。**已落地（量）**：请求中间件按**路由模板**记 p95（滑动窗口 + 有界），`GET /api/metrics` 报最慢端点与**相对基线的退化**（1.5× 容忍）；基线 `bench/endpoint_baseline.json`（实测 26 个端点，**全部 < 130ms —— 没有需要立刻治理的慢查询**） |
| P5-5 | 可观测性：HTTP 指标、关键路径 trace id、结构化日志 | 一个请求从界面到子进程可串起来。**已落地**：x-request-id 透传/生成 + 响应头回写 + CORS expose_headers；结构化 JSON 日志只记 /api/*；**子进程侧已串**：app/trace.py 的 contextvar + RunRecord.request_id（可空，NULL = 不是请求触发的）+ 子进程环境变量 CODEX_BATTLEFIELD_REQUEST_ID；10 例判据（T1–T6）+ **四组变异全部能红**。**边界**：后台任务看不到请求 ID（contextvars 在 asyncio 里是复制的），这点写在代码与 docs/observability.md §4.1 |
| P5-2 | 前端拆分与数据层：`App.tsx` 拆分、轮询改按需/SSE、长列表虚拟化 | 单文件不再超 500 行；空闲时**不产生**周期性请求。**已落地**：`App.tsx` 2924 → **15**；`types.ts` 948 → 8 / `api.ts` 833 → 10（聚合出口，类型与函数按领域各拆 4~5 个文件）；`useBattlefieldController.ts` 783 → 488（派生层 → `useBattlefieldDerived`，刷新 → `useTreeRefresh`）；**全仓无一个非测试文件超 500 行**，棘轮预算表 5061 → **0**；轮询 SSE 为主 + 60 秒兜底 + 不可见不拉；**长列表虚拟化已做**：`virtualization.ts` 把交给 elkjs 的节点限在 400 以内，并把挡掉的数量显示出来（13 例判据 + 变异验证；E2E 另加一条“树小时不出声”）。**未验**：“千节点下首屏 < 2s”**本机两连跑已达标**（`cold=1601 / 1891ms`；同一判据此前是 2.4–4.8s）—— 关键是三处结构性修复：侧栏重活改成"树到位再挂"、WMI 全表扫描改成服务端过滤、**启动预热 + 后台每 1.5 秒刷新进程扫描缓存**（页面取树 TTFB 1165→253–277ms）。判据里 `TARGET_MS=2000` / `RATCHET_MS=6000` / `TTFB<700ms`（回归钉子）三层都在。**仍未验**：慢机器与 CI 上的首屏（CI 下跳过；下一步最大项是客户端渲染 ~1.0s 长任务），数字与过程写在 `docs/scale-baseline.md` §5 |
| P5-3 | **端到端测试**（真浏览器，Playwright） | 覆盖：绑定工程 → 起树 → 看到进度 → 批准动作 → 导出诊断。**已落地（有口径限制）**：`web/e2e/l4-flow.spec.ts` 5 例，起**真后端**（自己的端口 8877 + 隔离状态根 `web/.e2e/` + `RUNNER=mock`）与**真前端**，实测 23s 全绿；判据覆盖 `/api/health` 精确形状与 `x-request-id`、绑定引导可见、**画布里真的渲染出节点**、预授权面板里高风险动作 `disabled`（变异验证：把 `!item.pre_authorizable` 改成 `false` 立刻红）、`/api/diagnostics?format=json` 非空壳。**后半句已补上（2026-10-18）**：`scripts/verify_steward_real_turn.py` 用**真实模型**（便宜档 `deepseek-flash`）跑一轮管家对话，调的是产品的 `steward.build_context` → `runner.steward_chat` → `steward.parse_chat` → `steward_actions.propose` → `action_risk` —— 实测它从快照里读出“子节点因容量超时 blocked、看门狗心跳过期 120881 秒”，提议 `retry_node` + `kick_root`（都 low、可预授权）；**没授权时两条都 `pending`**，给了覆盖“该动作 + 该目标”的预授权后同一动作落 `approved` 且 `result` 为 `[预授权] 命中预授权（… 还剩 600s）`；高风险 `cancel_all` 连 `make_grant` 都被当场拒绝；`ok=true`（要花额度，所以不进 `verify_all` 默认步骤）。执行一步（`run_approved`）**故意没做**，理由是它会把真实 Codex 拉起来、与验闸门无关（脚本里写了 `execute_skipped_reason`）。**仍然未覆盖**：`RUNNER=mock` 的 E2E 不证明真实模型能跑完树（那条由 `scripts/real_recursive_probe.py` 覆盖，见 P6-4）；`web/e2e/l4-flow.spec.ts` 本身没有改成真实模型档 |
| P5-4 | 数据保留策略：runs/快照/账本按大小与时间清理，且**不破坏还原能力** | 清理后崩溃恢复仍可用；有"保留窗口"配置。**已落地**：`GET /api/retention`（纯只读预演，带**受保护清单及理由**）+ `POST /api/retention/sweep`（**默认预演**）；保留窗口可配（默认 7 天，0 = 只留锚点）；**R9 判据真的清理后读回快照比对**；表行与每棵树最新快照永不清理 |
| P5-6 | 测试能力补强：并发/压力、崩溃注入（杀进程/断电式中断） | 崩溃注入下"不丢已验收结果"可复现。**已落地（还原锚点这一层）**：`checkpoint.atomic_write_json` 原子写 + 保留 `.backup` + 读者退备份并出声；`test_crash_injection.py` 在**每个写入阶段**用真子进程硬杀（`os._exit`）各验一次"锚点仍然可读"，另加两进程并发写。**未覆盖**：**已补上**：对**真实** Codex 进程树（`cmd.exe → node → codex.exe`，实测 **7 个进程**）跑`_stop_external_codex_process_tree` —— 杀完 5 秒内 **0 个存活**、被杀的调用带失败返回（关掉重试后）；判据 `api/tests/test_external_process_kill.py`（默认 **skip**，`CODEX_BATTLEFIELD_REAL_PROCESS_KILL=1` 才跑，skip 时写明原因）。两次变异：把整段杀换成 no-op → **红**（失败信息列出 7 个存活 pid）；去掉 `taskkill` 的 `/T` → **仍然绿**（这个形状下父进程一死、`node` 读到 stdin EOF 自己退，所以 `/T` 不是必要条件 —— 用例证明的是结果，如实记在 `docs/crash-injection.md` §7.4）。**仍未验**：杀毒/受限权限环境里 `taskkill` 会不会被拦（本机没有那个环境） |

**工作量估计**：3–5 周（P5-3 是独立大项）。
**风险**：数据清理最容易**悄悄破坏恢复能力**。对策：清理策略必须先有"还原测试"再上线。

---

## P6 兼容：跨平台 / 跨模型 / 容器

**要解决的痛点**：当前是 Windows 优先 + 依赖本机 Codex CLI。要更多人用、要进 CI/服务器，就需要容器与跨平台。

| # | 交付物 | 判据 |
|---|---|---|
| P6-1 | **容器化**：`Dockerfile` + `compose`（API + Web + 可选代理） | `docker compose up` 后界面可用；卷挂载状态根；文档写清沙箱在容器内的差异。**已落地（运行期已验证，2026-10-18）**：`Dockerfile`（两段构建：node 段 `pnpm build` → python 段装 api + 拷 `web/dist`）、`docker-compose.yml`（单服务、`127.0.0.1:8765` 回环绑定、命名卷 `battlefield-state:/state`）、`.dockerignore`、`docs/container.md`、21 例判据（端口/非 root/卷与 env 一致/两段构建产物落点/镜像版本对齐 `requires-python` 与 CI node/healthcheck/每个 `COPY` 源存在）。另有 `test_container_static.py`（D1–D8）核**语法与自洽**（指令关键字、无 sudo、缓存层顺序、三处端口一致、入口与源码对得上；五组变异全部能红）。**运行期已验证**（2026-10-18，WSL Ubuntu 里的 Docker 29.1.3）：`scripts/verify_container_up.py` 五条判据全绿 —— compose 起得来、`/api/health` 200、`GET /` 200 且含 `id="root"`、`/assets/*.js` 200 且不是 `text/html`、容器内 `id -u` = 10001。**第一次真跑就抓到五个缺陷**（pnpm 允许列表键名不存在 + 该文件没被 COPY；`corepack enable` 漂到 pnpm 12 而 CI 钉 10；editable 安装在只拷了 pyproject 的层里执行；运行时无脑 mkdir 老工作区导致非 root 容器 503），修法与证据写在 `docs/container.md` 的"运行期实测"一节，判据是 `api/tests/test_container.py` 的 K9–K12。**仍未验**：镜像推送/拉取（本机没有 registry）、多架构、macOS 上的 Docker Desktop |
| P6-3 | CI：GitHub Actions 跑 `verify_all`（Windows + Linux 双平台） | PR 上可见；挡住"只有本机能过"的提交。**已落地**：`.github/workflows/verify.yml`（ubuntu + windows，`fail-fast: false`，跑同一套 `verify_all.py`，显式 `RUNNER=mock`，失败收集诊断）+ 18 例判据（含**工作流与 `verify_all` 步骤逐字同步**这条防分叉的检查，以及 W10/W11 的**子进程编码**）。**已抓到真问题（这条 CI 的核心价值）**：把 Actions 日志拉下来发现 CI **一直在跑**（`on: push`，20 次运行），但 17 次被下一次推送的 `concurrency: cancel-in-progress` 取消 —— 旧结论"本机没有触发 Actions 的手段"是错的，真相是"推完就走、没人看"；唯一一次给出结论的 `failure` 是 **Windows runner 的 cp1252 把门禁死在第 1 步**（`scripts/check_markdown_layout.py:96` 打印中文摘要 → `UnicodeEncodeError: 'charmap' codec can't encode ...`），本机中文 Windows（cp936）永远看不到 —— 正是双平台 CI 该抓的"只有本机能过"。修法已落地（`verify_all` 给每个子进程钉 `PYTHONIOENCODING=utf-8`；脚本自己兜底；workflow 也设），并由 W10/W11 钉住（**去掉修复就红，报错与 CI 日志逐字相同**）。**未验证**：Linux 侧**没有一次跑完的记录**，但**原因与量级都查清了**：注解原文 `The job has exceeded the maximum execution time of 30m0s`；把预算提到 **45 分钟**再跑（run 38973770051 / commit 8d48868）—— **Windows 侧 16.4 分钟 success（连续第三次全绿，且带着"更快的 godot 用例"）**，ubuntu 侧 **>44.9 分钟仍没跑完**。所以 Linux 要的不是更大的超时，而是**结构性减负**。**已做处置**：`verify_all --group {python,web}` 把同一套门禁拆成两个**并行作业**（`verify-python` / `verify-web`，各自带双平台矩阵与 45 分钟上限），并由 **W13 两条判据**钉住"**两组并集等于全部无条件步骤、两组不重叠、两个作业各自覆盖 Windows+Linux**"——拆组**不是放宽**。**顺带修掉一处静默变弱**：抽步骤名的代码原在单行上 `re.match`，而 `provider 矩阵配置检查` 是多行调用 → W9 一直**少看一步**（改 DOTALL 后才第一次看全 10 步）。**Windows 侧已经拿到全绿**（run 38956861930，18.5 分钟，含 pytest + 前端 build + E2E 全部步骤），而它绿之前 CI 自己抓出并逼我们修了三处"只有本机能过"（cp1252 编码、未用 import、审计 `normalise` 不认"取不到"）。两个平台的原话、耗时与教训记在 `docs/ci.md` §5–§10。**拆组当次就拿到首次 Linux 绿灯**（verify-web ubuntu **2.5 分钟** success；windows 两个作业也全绿），只剩"Python 组在 Linux 上 45 分钟跑不完"（同一份 Windows 6.7 分钟、本机 83 秒）——已给每条用例装 `--timeout=120`，下次会指名道姓。**另有账号侧阻塞**：`run 38985269338` 四个作业 3 秒内全失败（注解 `The job was not started because recent account payments have failed or your spending limit needs to be increased...`）——**不是代码问题**，是 Actions 额度/账单需要所有者处理；自我记账与"一轮只推一次"的纪律见 `docs/ci.md` §11 |
| P6-2 | Linux/macOS 支持：脚本与路径判断去 Windows 假设 | 仓库脚本在 Linux 跑通核心路径；`verify_all` 在 Linux CI 绿。**部分落地**：审计出真 Windows 假设两处（`api/.venv/Scripts/python.exe`、`verify_browser_console.py` 的 Chrome/Edge 候选只有 `C:\Program Files\...`），收进 `scripts/platform_support.py`（平台是**参数**，不靠改写 `sys.platform` —— 那样会让 pathlib 换成 PosixPath 直接炸），17 例判据 L1–L9；另有 `test_cross_platform_windows_only.py`（W1–W4）静态扫出所有 Windows-only 调用点并要求**要么有平台守卫、要么进允许清单** —— 扫描第一次跑就抓到两处无守卫的 `powershell.exe`（看门狗两个入口），已改成显式 `RuntimeError` + 可行动提示；四组变异全部能红；`local_kick_tree.py` 的 `msvcrt`/`fcntl` 分支等**已经对了**的地方明确不动。**未验证**：本机没有 Linux/WSL/可用容器，**没有在 Linux 上跑过任何东西** —— “脚本在 Linux 跑通”“CI 在 Linux 绿”这两句都仍然没有证据，唯一发现机制是 P6-3 的双平台 CI（而它自己也没有一次真实绿灯记录）。口径与缺什么写在 `docs/cross-platform.md` §5 |
| P6-4 | 弱模型/异构模型可用性：小模型跑 worker 的成功率与降级策略 | 给出"哪些档位能用便宜模型"的**实测**结论，含失败模式。**已落地（planner 粒度）**：`scripts/probe_weak_model_planner.py`（判据从 `schemas/planner-decision.schema.json` 读，逐条不合成分数）+ `scripts/probe_model_cost.py` + `docs/weak-model-usability.md`。实测：`deepseek-flash` **可用**（execute/300%，10.2s）、`glm-5.3-flash` **可用**（execute/not_sure，34.3s）、`qwen3.8-flash` **不可用** —— 顶层键写成 `verdict` 而不是 `decision`，`PlannerDecision` 校验直接抛 `Field required`（**失败模式是结构漂移，不是理解力**，`api/app/retry.py` 的校验反馈重试能自愈）；`deepseek-v4-pro` / `gpt-5.6-luna` 本次 `exit_code=1` 属**通道问题**，如实记为"未验成"而不是"模型不行"。成本：同一端点自报 0.017–0.035 CNY/次短探针，**planner 单次金额没量到就不写**。**worker 档已实测通过**（2026-10-17，scripts/real_codex_smoke.py 完整链路、单节点一轮）：planner / worker / verifier 三次真实运行全 exit_code=0，worker 交回 changed_files=[BATTLEFIELD_SMOKE.txt]、31 字节逐字节正确，verifier 独立 Format-Hex 复核 verdict=pass，root_status=done、debug_paths_ok=true，整链约 40 秒（详见 docs/weak-model-usability.md §7）。**递归链路已实测通过**（2026-10-18，`scripts/real_recursive_probe.py`，`deepseek-flash`，修完后**十轮全绿**）：每轮 8–31 次真实运行（planner×3 / worker×2 / verifier×2 / **acceptance×1**）全 `exit_code=0`，`root_status=done`（十轮里 7 轮深度 1、2 轮深度 2、1 轮**深度 6**）、`acceptance_run_count=1`（真实运行，`synthetic_run_count=0`）、`debug_paths_ok=true`、`probe_files` 命中两个 worktree，墙钟 9–16 分钟。**失败样本也拿到了**：修之前一轮子 planner 的 `--output-last-message` 多了一个 `}`（`Extra data … char 1042`），被判 `blocked` → 父节点转成“递归指导”子任务、树形状走偏；已加**结构漂移自纠重试**（`retry.parse_failure_feedback` + `api/tests/test_parse_failure_retry.py` P1–P4，去掉修复红 3 条），并修掉探针把 synthetic 运行当成 acceptance 证据的错。**仍未覆盖**：长跑、并发压测、以及“树该拆多深”的成本上限（第 8 轮自己拆到 6 层、31 次运行、24.1M input —— 形状由模型当场决定）；成功率只敢说“修后 10/10”（n=10）；费用金额仍是 `null`（端点不回传）。详见 `docs/weak-model-usability.md` §8 |
| P6-5 | provider 矩阵：官方 / 自建网关 / 本地推理（Ollama 类） | 每种都有可复制的配置与验证脚本（`verify_provider_planner.py` 已通用）。**已落地（未验项只剩厂商云端端点）**：`docs/provider-matrix.md`（三类端点的可复制配置 + 判据 + 验证边界）+ `scripts/verify_provider_matrix.py`（离线生成三份隔离配置并逐项验结构，**不发任何请求**，已进 `verify_all` 与 CI）+ 25 例判据（含 M5 系列的坏配置变异、M10 的 `env_key` 放行名单双向验证）。**顺手修掉一条真实缺陷**：两个脚本原来默认 `wire_api = "chat"`，而新版 codex 已移除该取值 —— 实测 `codex doctor` 报 `config could not be loaded / error invalid data`，症状是**所有节点一起起不来**且日志无线索（上游 openai/codex#41816）；默认值已改成 `responses`，显式传 `chat` 会告警。**另加离线实测（不需要凭据）**：`api/tests/test_provider_wire_format.py`（F1–F4）用本机假网关 + **真实 Codex CLI** 试出了**端点契约**：必须支持 **SSE 流式**并以 `response.completed` 收尾、事件里必须有 `response.output_item.done`，请求打到 `POST <base_url>/responses` 且 body 用 `input` 而不是 `messages` —— 两条都是先失败（`stream disconnected before completion` / `no last agent message`）再改对的，写进了 `docs/provider-matrix.md` §4.0。**真实端点已实测**（2026-10-18，`docs/provider-matrix.md` §4.3）：正样本（本机 `127.0.0.1:15721` 上的 OpenAI 兼容代理，不带 key）跑 `verify_provider_planner.py` → `ok=true`、`exit_code=0`、`decision=execute` / `300%`、约 15 秒；负样本（把 `base_url` 指到无监听的端口）→ `ok=false`、`exit_code=124`、理由 `hard timeout after 60s`（codex 会退避重试，所以“不通”表现为慢失败，而不是连接被拒）。**鉴权那一半也验了**（§4.4）：假网关新增 `require_token`，**F5**（网关要 `good-token`、环境里放 `wrong-token`）→ 真实 Codex 调用 `exit_code != 0` 且日志里出现 401/unauthorized，**F6**（换成对的 key）→ `exit_code == 0`；变异把网关校验改成 `return False` → F5 当场红。**仍未验证（剩下的是环境）**：某家厂商的云端端点本身，以及厂商特有的鉴权细节（签名 / 组织-项目头 / IP 白名单 / 按 key 限额） |

**工作量估计**：3–6 周。
**风险**：容器里跑 agent 改代码涉及挂载与权限，容易做成"能跑但不安全"。对策：默认最小权限，危险模式要显式开启并在界面标注。

---

## 明确不做（避免范围蔓延）

| 不做 | 理由 |
|---|---|
| 自建模型 / 训练 | 本工具的价值在**编排与验收**，不在模型本身 |
| 替代 Git 平台（PR/评审/发布流水线） | 与现有平台重叠；本工具只到"可验收的产物"为止 |
| 通用 IDE / 代码编辑器 | 已有工具做得更好；这里只做"任务树 + 证据" |
| 无审批的全自动动树 | 权限模型的核心取舍。**可以放宽到预授权，但高风险动作永远要人点** |

## 里程碑（相对时间，可调）

| 里程碑 | 内容 | 大致投入 |
|---|---|---|
| **M1 可分发** | P1 全部 | 2–4 天 |
| **M2 可日常用** | P2 + P3-1/2 | 3–4 周 |
| **M3 能自管** | P3 剩余 + P4 | 3–4 周 |
| **M4 可长期跑** | P5 | 3–5 周 |
| **M5 谁都能部署** | P6 | 3–6 周 |

## 每阶段的共同规矩（沿用现有习惯）

1. **先写判据再写实现**：每个交付物必须能"被反驳"（怎么算失败）。
2. **判据不能是恒真的**：像 `test_a7_*` 用**异步替身**抓漏 `await`、`test_a1_*` 双向核对白名单与执行器 —— 测试要能真的红。
3. **失败方向优先**：宁可漏报也不要假警（`watchdog_stale` 只在有活可看时报、主动汇报默认关、冷却存在）。
4. **不编数据**：取不到就标 `available: false`，绝不填 0。
5. **公开版与内部版分仓**：本仓库只放可公开的通用版；任何公司内部的 provider /
   网关 / 凭据约定都放在**独立的私有仓库**，不进这里。改动先落本仓库，再同步到私有仓。
6. **发布前扫厂商词**：用 `scripts/check_public_tree.py` 扫一遍，确认公开树 0 命中。

```powershell
python scripts/check_public_tree.py --terms-file <私有词表路径>
```

> 词表为什么**不放在这个仓库里**：把内部标识当成字符串写进公开文件，本身就是泄漏
> —— 哪怕它写在"扫描规则"里。所以脚本只留方法与判据，词表由 `--terms-file`
> 指向私有位置；不给参数时它也会跑（用通用词，如邮箱、绝对路径、token 形态）。
