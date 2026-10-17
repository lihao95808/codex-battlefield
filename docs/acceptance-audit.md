# 整体验收审计（P2–P6）

这份文件回答一个问题：**目标里那些交付物，哪些真的有可反驳的判据，哪些还缺条件。**

它本身也受判据约束：`api/tests/test_acceptance_audit.py` 的 Q1–Q14 会检查
（每条交付物都说了状态、声明的判据文件真的存在**且里面有断言**、未验项没被悄悄删掉、
双仓差异仍是 7 个文件、**本文件与生成器的输出一致**、文档里的「27 项」这类数量与代码一致、文档点名的文件、/api 端点与环境变量都真实存在）。**文档与代码脱节时会红**，这是这份文件存在的意义。

> **Q10 是 2026-10-18 才补上的**：在这之前，`P5-2`（前端体量/虚拟化）与 `P5-3`（端到端）
> 指向的是**无关文件**（`test_provider_matrix.py` / `test_ci_workflow.py`），而 Q2 只查
> "文件在不在" —— 于是"判据指着别的模块"也能绿。现在它们指向真正的前端判据，
> 并且每个判据文件都必须真的有断言（变异：把 P5-2 指回 `docs/README.md` → Q10 红、Q2 仍绿）。

审计时的两个远端 tip：公开仓 `f676b4874addd33995fd351ba9f412fcb274e2b2`、内部仓 `8cc791f9a4f72d190ed5587b36f3b006dc5fca56`。
（本文件由 `scripts/gen_acceptance_audit.py` 生成 —— 手改会在下一次生成时被覆盖。）

> **这里刻意不写“本地 HEAD”**：写了它会自相矛盾 —— 文档里含 HEAD 的哈希，
> 而把这个文件提交进去又会让 HEAD 变化，于是 `--check` 永远不过（实测踩到）。
> 两个远端 tip 是“已经推上去的事实”，不会因为一次本地提交而失效。

## 1. 逐条对照

| 交付物 | 状态 | 判据（在哪） |
|---|---|---|
| P2-1 | 已落地 | `ProjectOnboarding.tsx` + `web/e2e/l4-flow.spec.ts`（绑定引导可见） |
| P2-2 | 已落地 | `api/tests/test_settings_panel.py`（19 项设置 + 生效说明） |
| P2-3 | 已落地 | `api/tests/test_runtime_logs.py` |
| P2-4 | 已落地 | `api/tests/test_diagnostics.py`（脱敏 + 非空壳） |
| P2-5 | 已落地 | `api/tests/test_state_admin.py`（迁移先预演、重置要抄 key） |
| P2-6 | 已落地 | `web/src/i18n.test.ts` + `i18n.detect.test.ts`（32 例，逐条变异过） |
| P3-1 | 部分落地 | `api/tests/test_budget.py`（16 例：时间窗守卫 / agent burst / 预警档）+ `test_budget_ceiling.py`（B1–B6：树级 token 上限，含接线判据）+ `test_budget_cost_ceiling.py`（C1–C8：树级金额上限 + “读不到不误报” + “不跨币种换算”）+ `test_settings_panel.py`（额度项）—— **没有** token 金额硬闸，见缺口清单 |
| P3-2 | 已落地 | `api/tests/test_cost.py`（区间而不是单值；样本不足拒绝给数） |
| P3-3 | 已落地 | `api/tests/test_cost.py`（三维归因；取不到模型名不造假桶） |
| P3-4 | 已落地 | `api/tests/test_cross_project.py`（按币种分桶、读不到≠0） |
| P3-5 | 已落地 | `api/tests/test_auto_policy.py`（4 条策略 + 开关 + 触发依据） |
| P3-6 | 已落地（有未验项） | `api/tests/test_scale.py`（1000 节点：130ms/640KB，规模比 < 8）+ `web/e2e/perf-first-screen.spec.ts`（真浏览器首屏实测 + 棘轮） |
| P4-1 | 已落地 | `api/tests/test_delivery.py` |
| P4-2 | 已落地 | `api/tests/test_watchdog_tuning.py`（记录先于生效） |
| P4-3 | 已落地 | `api/tests/test_attention.py`（注意账会过期；时间冻结过的用例） |
| P4-4 | 已落地 | `api/tests/test_action_risk.py`（高风险不可预授权） |
| P4-5 | 已落地 | `api/tests/test_conclusions.py`（事实与判断分开；空段≠没有） |
| P5-1 | 已落地 | `api/tests/test_metrics.py`（按路由模板记 p95 + 相对基线退化） |
| P5-2 | 已落地（有未验项） | `web/src/fileSize.test.ts`（棘轮，**预算表现在是空的**）+ `virtualization.test.ts`（13 例）+ `web/e2e/perf-first-screen.spec.ts`（千节点首屏实测） |
| P5-3 | 已落地 | `web/e2e/l4-flow.spec.ts`（6 例，真后端 + 真浏览器） + `scripts/verify_steward_real_turn.py`（**真实模型**一轮管家对话 → 风险分级 → 预授权闭环；默认不进 verify_all） |
| P5-4 | 已落地 | `api/tests/test_retention.py`（清理后仍能还原） |
| P5-5 | 已落地 | `api/tests/test_trace_correlation.py`（10 例 + 四组变异） |
| P5-6 | 已落地（有未验项） | `api/tests/test_crash_injection.py`（每个写入阶段硬杀一次，锚点仍可读） + `api/tests/test_external_process_kill.py`（**真实** Codex 进程树被 `taskkill /T /F` 打掉、无孤儿；默认 skip） |
| P6-1 | 已落地（有未验项） | `api/tests/test_container.py`（25 例，含 K9–K12）+ `test_container_static.py`（D1–D8）+ `scripts/verify_container_up.py`（运行期五条：compose 起来 / health / 界面 / 资产 / 非 root）+ `test_web_dist_serving.py`（W1–W4 **运行期**：API 真的托管 `web/dist`，且 `/api/*` 没被挂载吃掉） |
| P6-2 | 已落地（有未验项） | `api/tests/test_platform_support.py`（17 例）+ `test_cross_platform_windows_only.py`（W1–W4） + **Linux 实测记录**（`docs/cross-platform.md` §7：WSL Ubuntu 上后端整套 1729 通过 / 33 跳过 / 0 失败） |
| P6-3 | 已落地（有未验项） | `api/tests/test_ci_workflow.py`（18 例，含步骤名逐字同步 + W10/W11 子进程编码） |
| P6-4 | 已落地（有未验项） | `scripts/probe_weak_model_planner.py`（planner 矩阵）+ `scripts/real_codex_smoke.py`（单节点完整链路） + `scripts/real_recursive_probe.py`（**多节点 + 真 acceptance**，`ok` 判据含 `acceptance_run_count` 与 `debug_paths_ok` + `api/tests/test_parse_failure_retry.py`（P1–P4 结构漂移自纠） + `docs/weak-model-usability.md`（§3 planner 矩阵、§7 worker 档、§8 递归链路） |
| P6-5 | 已落地（有未验项） | `api/tests/test_provider_matrix.py`（25 例）+ `test_provider_wire_format.py`（F1–F4：假网关 + 真实 CLI 试出端点契约；F5/F6：错 key → 401 可见、对 key → 成功） |

## 2. 缺口清单（**没有证据的部分**）

这些条目的 ROADMAP 行里带“未做/未验证”字样，逐条写清缺什么条件。
**不把它们藏起来**是这份审计的主要目的：把未验写成已验比不写更坏。

| 交付物 | 缺什么 |
|---|---|
| P3-1 | **四道闸的现状（2026-10-18 两次纠正之后）**：① **时间窗**守卫（额度耗尽 → 整树休眠到刷新，`api/tests/test_budget.py` 16 例）；② **agent burst**（每窗口允许起几个 agent，同文件）；③ **树级 token 上限**（`budget_token_ceiling`，默认关，`test_budget_ceiling.py` B1–B6，B4 是接线判据）；④ **树级金额上限**（`budget_cost_ceiling` + `budget_cost_currency`，默认关，`test_budget_cost_ceiling.py` C1–C8；读不到金额 / 币种不匹配时**不误报**，而是写事件说明原因；C8 专门钉住“不跨币种换算”这条决定）。**仍未做**：跨币种换算 —— **有意不做**（需要汇率来源与口径，属新依赖），C8 钉住“不换算”。历史上这里一度把“token / 金额硬闸”写成已落地而代码里只有时间窗守卫 —— 那次纠正的记录见 `docs/cost-accounting.md` §2。 |
| P3-6 | 后端规模判据达标（1000 节点 130ms/640KB）。**前端首屏不是稳定达标**：cold 在 **1601–2403ms** 之间摆（四次实测 1601 / 1891 / 2403 / 1775ms，命中目标 3/4；2026-10-18 那次 2403ms `meets_target=false`），原因是取树正好落在“外部进程扫描缓存已过期”上（那 ~0.7s 又回到请求路径）—— 一路是量出来的 —— 侧栏重活改成“树到位再挂”（4406→3033ms）、cProfile 定住第一次 `get_tree` 的 0.895s 是 **PowerShell/WMI 全表扫进程**（缓存 TTL 2 秒）→ 换 WQL 服务端过滤（0.895→0.672s）+ **启动预热 + 后台每 15 秒刷新缓存** → 页面取树 TTFB **1165→253–277ms**。稳定通过的是判据的三条钉子：棘轮 6000ms、TTFB<800ms、服务端 p50<650ms；“目标 2000ms”只是 `meets_target` 的观测量。**已修掉抖动源**（2026-10-18）：统计口径缓存 TTL **5 秒 → 20 秒**（`_codex_slots_active`），请求路径不再同步跑那次 ~0.7 秒的 WMI 扫描；调度决策仍用 0.5 秒窗口现场扫（`api/tests/test_external_scan_staleness.py` S1–S3，变异改回 5 秒 → S1 红）。改后两次复测：取树 **270–420ms**（原先会窜到 1385ms）、`cold` **1696 / 1236ms**（都达标）。**仍未验证**：慢机器/CI 上的首屏（CI 下跳过；2 vCPU runner 上千节点 >180s，量的是 runner 容量）。见 `docs/scale-baseline.md` §5 ⑨⑩⑪ |
| P5-2 | 长列表虚拟化已做（交给布局器的节点有界且会说出挡住多少）；**“千节点首屏 < 2s”不是一个稳定事实**（本机四次 cold 1601 / 1891 / 2403 / 1775ms，命中目标 3/4；棘轮 6000ms 与 TTFB<800ms 稳定通过），判据 `web/e2e/perf-first-screen.spec.ts` 除目标与棘轮外还多了一条回归钉子（页面取树 TTFB < 800ms）；**仍未验证**：慢机器/CI 上的首屏（CI 下跳过） |
| P5-6 | **两个阶段都实测过了**：`api/tests/test_crash_injection.py` 在每个写入阶段硬杀一次进程，还原锚点仍可读；`api/tests/test_external_process_kill.py`（默认 skip，`CODEX_BATTLEFIELD_REAL_PROCESS_KILL=1` 才跑）对**真实** Codex 进程树（`cmd.exe → node → codex.exe`，实测 **7 个进程**）跑产品自己的 `_stop_external_codex_process_tree`：5 秒内 **0 个存活**、被杀的调用带失败返回（关掉重试后）。两次变异：换成 no-op → **红**（列出 7 个存活 pid）；去掉 `/T` → **仍绿**（该形状下父进程一死、`node` 读到 stdin EOF 自己退，所以 `/T` 不是必要条件）。**仍未验**：杀毒软件 / 受限权限环境里 `taskkill` 会不会被拦（本机没有那个环境）。详见 `docs/crash-injection.md` §7 |
| P6-1 | **运行期已验证**（2026-10-18，WSL Ubuntu 里的 Docker 29.1.3）：`scripts/verify_container_up.py` 五条判据全绿 —— compose 起得来、`/api/health` 200、`GET /` 200 且含 `id="root"`、`/assets/*.js` 200 且不是 `text/html`、容器内 `id -u` = 10001。第一次真跑抓到五个缺陷（pnpm 允许列表键名不存在、该文件没进构建上下文、pnpm 12 vs CI 的 10、editable 安装早于源码 COPY、非 root 下 mkdir 老工作区导致 503），修法与证据在 `docs/container.md`，判据 K9–K12。**仍未验**：镜像推送/拉取、多架构、macOS 的 Docker Desktop。 |
| P6-2 | **Linux 运行期已验证**（2026-10-18，WSL Ubuntu 24.04 / Python 3.12）：后端整套 **1729 通过 / 33 跳过 / 0 失败（1 分 43 秒）**；清零过程中抓到两处真缺陷（监督器的 killpg 因缺 start_new_session 而杀掉调用方；迁移重写 worktree 路径写死反斜杠）。口径与逐类处理见 `docs/cross-platform.md` §7。**仍未验证**：macOS（本机没有）、真 Linux CI （`ubuntu-latest`，等 GitHub 账单）。 |
| P6-3 | **Windows 侧已全绿**（run 38956861930，18.5 分钟，含 pytest + 前端 build + E2E），绿之前 CI 自己抓出并逼我们修了三处“只有本机能过”（cp1252 编码、未用 import、审计 `normalise` 不认“取不到”）。**Linux 侧仍无完整记录**，但**原因与量级都查清了**：加 `timeout-minutes` 后注解给出原文`The job has exceeded the maximum execution time of 30m0s`，把预算提到 **45 分钟**再跑（run 38973770051 / commit 8d48868）—— Windows 侧 **16.4 分钟 success**（连续第三次全绿），ubuntu 侧 **>44.9 分钟仍没跑完**。**已做结构性处置**：`verify_all --group {python,web}` 把同一套门禁拆成两个并行作业（W13 盯着“并集等于全部步骤、两组不重叠、两个作业各自覆盖双平台”），顺带修掉一处静默变弱（抽步骤名原来漏掉多行的 `provider 矩阵配置检查`）。**仍未验证**：拆组之后 Linux 侧的第一次完整记录；拆组当次已拿到 **首次 Linux 绿灯**（verify-web ubuntu 2.5 分钟），只剩“Python 组在 Linux 上 45 分钟跑不完”（同一份 Windows 6.7 分钟、本机 83 秒）——已给每条用例装 `--timeout=120`，下次会指名道姓。**另有账号侧阻塞**：`run 38985269338` 四个作业 3 秒内全失败，注解 `The job was not started because recent account payments have failed or your spending limit needs to be increased...` —— **不是代码问题**，是 Actions 额度/账单需要仓库所有者处理（自我记账与据此定的“一轮只推一次”纪律见 `docs/ci.md` §11）。另：旧结论“本机没有触发 Actions 的手段”是错的 —— CI 一直在跑（`on: push`），是“推完就走”把运行自己取消了 |
| P6-4 | **递归链路已实测通过**（2026-10-18，`scripts/real_recursive_probe.py`，`deepseek-flash`，修完后**十一轮全绿**）：十一轮共 114 次真实运行（每轮 8–33 次，含每轮的真实 acceptance）全 `exit_code=0`，`root_status=done`（深度 1 七轮 / 2 两轮 / 3 一轮 / **6** 一轮）、`acceptance_run_count>=1`（**真实**运行，`synthetic_run_count=0`）、`debug_paths_ok=true`、`probe_files` 非空，墙钟 9–16 分钟。修之前两轮都不可用：一轮被并发跑的另一个真实探针抢走 Codex 槽位（事件 `scheduler.external_codex_slots_full`，`run_count=0`），一轮子 planner 输出多一个 `}`（`Extra data … char 1042`）被判 `blocked`、把树带到 `waiting_children`；已加**结构漂移自纠重试**（`retry.parse_failure_feedback` + `api/tests/test_parse_failure_retry.py` P1–P4，去掉修复红 3 条）。**仍未覆盖**：长跑 / 并发 / “树该拆多深”的成本上限（一次自己拆到 6 层、31 次运行、24.1M input）；成功率只敢说“修后 11/11”（n=11）；费用金额仍 `null`（端点不回传，不推算）。详见 `docs/weak-model-usability.md` §8 |
| P6-5 | **真实端点可达性已实测**（2026-10-18，`docs/provider-matrix.md` §4.3）：正样本（本机 `127.0.0.1:15721` 的 OpenAI 兼容代理，不带 key）→ `ok=true` / `exit_code=0` / `decision=execute` / `300%` / 约 15 秒；负样本（`base_url` 指向无监听端口）→ `ok=false` / `exit_code=124` / `hard timeout after 60s`（codex 会退避重试，所以“不通”表现为慢失败）。**鉴权那一半也验了**（`docs/provider-matrix.md` §4.4）：假网关强制 `Authorization`，F5（错 key）→ 真实调用 `exit_code != 0` 且日志里有 401；F6（对 key）→ `exit_code == 0`；变异把校验改成 `return False` → F5 红。**仍未验**：厂商云端端点本身与其特有鉴权细节。端点契约（SSE 流式 + `response.output_item.done`）此前已用本机假网关离线实测（F1–F4） |

## 3. 双仓状态

* 公开仓：`f676b4874addd33995fd351ba9f412fcb274e2b2`（`origin/main`）
* 内部仓：`8cc791f9a4f72d190ed5587b36f3b006dc5fca56`（`internal/main`）
* 差异必须**恰好 7 个文件**（公开仓 + 7 个 overlay）：其中 2 个是公开树里也有的
  文件（内部仓改写了它们），另外 5 个只存在于内部仓。
  多一个少一个都说明同步出了问题（Q5 会红，它会把具体文件名打在失败信息里）。

> **这里刻意不列出那 5 个内部文件的名字。** 本文件会提交到公开仓，而它们的名字就是
> 内部标识；第一版把 7 个名字全列了出来，等于亲手把内部标识推上公开仓
> （`scripts/check_public_tree.py` 的私有词表会命中）。要核对名字请跑 Q5 或
> `git diff --name-status origin/main internal/main` —— 运行输出不属于公开树。

## 4. 结论

**P2–P5 的交付物都有判据，且判据在 `verify_all` 里跑。** P6 的五条里，
两条（容器化 / 跨平台）**都已经真跑了**：容器在 WSL 的 Docker 里跑通（五条判据全绿、顺手抓到五个只在真构建/真启动时暴露的缺陷）；后端整套测试也在 WSL Ubuntu 上跑通（清零后 **1729 通过 / 33 跳过 / 0 失败**，过程中修掉两处 POSIX 真缺陷）。
CI 那条**有了新证据**：拆组后 Windows 两个作业全绿、**Linux 前端侧第一次全绿**
（`verify-web (ubuntu)` 2.5 分钟），只剩“Python 组在 Linux 上 45 分钟跑不完”；
而且账号侧被账单挡住（2026-10-18 00:41 / 01:37 / 02:08 / 03:17 四次运行都是同一条注解），**Linux 侧完整记录仍拿不到**。
弱模型那条**补到了递归链路**：11 轮共 114 次真实运行全绿（树最深拆到 6 层 31 次运行）、每轮都有真 acceptance，并拿到并修掉了“结构漂移”这个失败模式；
provider 矩阵那条**补到了真实端点可达性**（正/负样本）与**鉴权的失败方向**（错 key → 401 可见、对 key → 成功，`docs/provider-matrix.md` §4.3/§4.4），
仍然没验的只有“某家厂商的云端端点本身”。

所以整体目标**不标记完成**：缺口是**环境**造成的（没有 Docker、没有 Linux、Actions 账单、没有需要凭据的云端端点）。注意“没有 CI 触发手段”这条**已作废** ——
CI 从来就在跑，是我们没等它跑完（见 `docs/ci.md` §5）。补上它们的成本按信息量排序：

1. **在 GitHub 侧解开 Actions 账单 / 额度** —— 现在四个作业都启动不了（注解固定）；解开后第一件事是
   拿 Linux 侧 Python 组的完整记录（`--timeout=120` 已装好，卡住的那条会指名道姓）；
2. 一台有 Docker 的机器跑 `docker compose up` —— 验 P6-1 的“界面可用”；
3. 一个需要凭据的云端端点跑 `verify_provider_planner.py` —— 验 P6-5 的鉴权那一半；
4. 真实模型额度下多跑几轮递归探针 —— 把 P6-4 的成功率从 n=3 变成有统计意义。
