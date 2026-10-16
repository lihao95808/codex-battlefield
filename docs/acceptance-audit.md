# 整体验收审计（P2–P6）

这份文件回答一个问题：**目标里那些交付物，哪些真的有可反驳的判据，哪些还缺条件。**

它本身也受判据约束：`api/tests/test_acceptance_audit.py` 的 Q1–Q6 会检查
（每条交付物都说了状态、声明的判据文件真的存在、未验项没被悄悄删掉、双仓差异仍是 7 个文件、
**本文件与生成器的输出一致**）。**文档与代码脱节时会红**，这是这份文件存在的意义。

审计时的两个远端 tip：公开仓 `e2721abdc3e1d7fe8c72996d91d474472c8a5653`、内部仓 `1042668a2304e8c5b6357fc6e5d2590fc716b046`。
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
| P3-1 | 已落地 | `api/tests/` 的预算硬闸用例 + `test_settings_panel.py`（额度项） |
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
| P5-3 | 已落地 | `web/e2e/l4-flow.spec.ts`（6 例，真后端 + 真浏览器） |
| P5-4 | 已落地 | `api/tests/test_retention.py`（清理后仍能还原） |
| P5-5 | 已落地 | `api/tests/test_trace_correlation.py`（10 例 + 四组变异） |
| P5-6 | 已落地 | `api/tests/test_crash_injection.py`（每个写入阶段硬杀一次） |
| P6-1 | 部分落地 | `api/tests/test_container.py`（21 例）+ `test_container_static.py`（D1–D8 语法与自洽） |
| P6-2 | 部分落地 | `api/tests/test_platform_support.py`（17 例）+ `test_cross_platform_windows_only.py`（W1–W4） |
| P6-3 | 已落地（有未验项） | `api/tests/test_ci_workflow.py`（18 例，含步骤名逐字同步 + W10/W11 子进程编码） |
| P6-4 | 已落地（有未验项） | `scripts/probe_weak_model_planner.py` + `docs/weak-model-usability.md`（实测矩阵） |
| P6-5 | 部分落地 | `api/tests/test_provider_matrix.py`（25 例）+ `test_provider_wire_format.py`（F1–F4：假网关 + 真实 CLI 试出端点契约） |

## 2. 缺口清单（**没有证据的部分**）

这些条目的 ROADMAP 行里带“未做/未验证”字样，逐条写清缺什么条件。
**不把它们藏起来**是这份审计的主要目的：把未验写成已验比不写更坏。

| 交付物 | 缺什么 |
|---|---|
| P3-6 | 后端规模判据达标（1000 节点 130ms/640KB）；**前端首屏未达标**：真浏览器实测 cold 2.4–4.8s / warm 2.2–3.9s（目标 < 2s；最好的一次 2422ms 也在目标之上）。判据里留了目标值与 6s 棘轮（只防变慢），下一步线索：前端一次首屏**取了两次树**、服务端在跑 orchestrator 时同一棵树 0.2–2.3s （单元判据 130ms）、渲染成本尚未单独量 —— 见 `docs/scale-baseline.md` §5 |
| P5-2 | 长列表虚拟化已做（交给布局器的节点有界且会说出挡住多少）；**“千节点首屏 < 2s”**已实测**未达标**（cold 2.4–4.8s，最好的一次 2422ms），判据 `web/e2e/perf-first-screen.spec.ts` 目前只保证不变慢 |
| P6-1 | 本机**没有 Docker**，`docker compose up` 一次都没跑过（语法与自洽已用 D1–D8 静态验过） |
| P6-2 | 本机**没有 Linux/WSL/可用容器**，没在 Linux 上跑过任何东西（平台分派已用 L1–L9 验过） |
| P6-3 | **Windows 侧已全绿**（run 38956861930，18.5 分钟，含 pytest + 前端 build + E2E），绿之前 CI 自己抓出并逼我们修了三处“只有本机能过”（cp1252 编码、未用 import、审计 `normalise` 不认“取不到”）。**Linux 侧仍无完整记录**：作业在「跑 verify_all」里 45–46 分钟后以 runner 掉线告终（注解原文见 `docs/ci.md` §9.2），步骤无结论、日志 404 —— 连“跑到哪个用例”都不知道；本轮已加 `timeout-minutes: 30` 让下次失败可诊断（W12）。另：旧结论“本机没有触发 Actions 的手段”是错的 —— CI 一直在跑（`on: push`），是“推完就走”把运行自己取消了 |
| P6-4 | 只到 **planner 粒度**；worker 能否改对文件、verifier 验收、长跑稳定性未测 |
| P6-5 | 三类端点的**真实可达性与鉴权未验**（本机没凭据）；**端点契约已离线实测**（F1–F4：必须支持 SSE 流式 + `response.output_item.done`） |

## 3. 双仓状态

* 公开仓：`e2721abdc3e1d7fe8c72996d91d474472c8a5653`（`origin/main`）
* 内部仓：`1042668a2304e8c5b6357fc6e5d2590fc716b046`（`internal/main`）
* 差异必须**恰好 7 个文件**（公开仓 + 7 个 overlay）：其中 2 个是公开树里也有的
  文件（内部仓改写了它们），另外 5 个只存在于内部仓。
  多一个少一个都说明同步出了问题（Q5 会红，它会把具体文件名打在失败信息里）。

> **这里刻意不列出那 5 个内部文件的名字。** 本文件会提交到公开仓，而它们的名字就是
> 内部标识；第一版把 7 个名字全列了出来，等于亲手把内部标识推上公开仓
> （`scripts/check_public_tree.py` 的私有词表会命中）。要核对名字请跑 Q5 或
> `git diff --name-status origin/main internal/main` —— 运行输出不属于公开树。

## 4. 结论

**P2–P5 的交付物都有判据，且判据在 `verify_all` 里跑。** P6 的五条里，
两条（容器化 / 跨平台）缺的是**执行环境**：本机没有 Docker、没有 Linux，
所以“界面可用”“在 Linux 上跑通”这两句都没有证据（语法、平台分派、工作流结构都已静态验过）。
CI 那条**有了新证据**：它一直在跑（`on: push`），唯一一次给出结论的运行抓到了
**Windows 侧 cp1252 把门禁死在第 1 步**（已修，W10/W11 钉住）；但 **Linux 侧没有一次跑完的记录**
（45 分钟未完成、日志 404）。一条（弱模型）只到 planner 粒度，一条（provider 矩阵）
缺真实端点的可达性与鉴权（**端点契约**已用本机假网关实测）。

所以整体目标**不标记完成**：缺口是**环境**造成的（没有 Docker、没有 Linux、
没有真实端点凭据、没有真实模型额度）。注意“没有 CI 触发手段”这条**已作废** ——
CI 从来就在跑，是我们没等它跑完（见 `docs/ci.md` §5）。补上它们的成本按信息量排序：

1. **等一次完整的双平台 CI 跑完** —— 现在它真的会跑，代价只是时间；
   能把 P6-2 与 P6-3 的一半假设变成事实或反例；
2. 一台有 Docker 的机器跑 `docker compose up` —— 验 P6-1 的“界面可用”；
3. 一个真实端点（任意 OpenAI 兼容）跑 `verify_provider_planner.py` —— 验 P6-5 的通道；
4. 授权真实模型额度后跑 `probe_weak_model_planner.py` 的 worker 档 —— 验 P6-4 的后半段。
