# 整体验收审计（P2–P6）

这份文件回答一个问题：**目标里那些交付物，哪些真的有可反驳的判据，哪些还缺条件。**

它本身也受判据约束：`api/tests/test_acceptance_audit.py` 的 Q1–Q6 会检查
（每条交付物都说了状态、声明的判据文件真的存在、未验项没被悄悄删掉、双仓差异仍是 7 个文件、
**本文件与生成器的输出一致**）。**文档与代码脱节时会红**，这是这份文件存在的意义。

审计时的两个远端 tip：公开仓 `8db29c819f2aa7c416361634dcbc22b8f28823d1`、内部仓 `41efa1bfe87fb6216cae2bcce73d6545e3f45ec9`。
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
| P3-6 | 已落地 | `api/tests/test_scale.py`（1000 节点：130ms/640KB，规模比 < 8） |
| P4-1 | 已落地 | `api/tests/test_delivery.py` |
| P4-2 | 已落地 | `api/tests/test_watchdog_tuning.py`（记录先于生效） |
| P4-3 | 已落地 | `api/tests/test_attention.py`（注意账会过期；时间冻结过的用例） |
| P4-4 | 已落地 | `api/tests/test_action_risk.py`（高风险不可预授权） |
| P4-5 | 已落地 | `api/tests/test_conclusions.py`（事实与判断分开；空段≠没有） |
| P5-1 | 已落地 | `api/tests/test_metrics.py`（按路由模板记 p95 + 相对基线退化） |
| P5-2 | 已落地（有未验项） | `web/src/fileSize.test.ts`（棘轮，**预算表现在是空的**）+ `virtualization.test.ts`（13 例） |
| P5-3 | 已落地 | `web/e2e/l4-flow.spec.ts`（6 例，真后端 + 真浏览器） |
| P5-4 | 已落地 | `api/tests/test_retention.py`（清理后仍能还原） |
| P5-5 | 已落地 | `api/tests/test_trace_correlation.py`（10 例 + 四组变异） |
| P5-6 | 已落地 | `api/tests/test_crash_injection.py`（每个写入阶段硬杀一次） |
| P6-1 | 部分落地 | `api/tests/test_container.py`（21 例）+ `test_container_static.py`（D1–D8 语法与自洽） |
| P6-2 | 部分落地 | `api/tests/test_platform_support.py`（17 例）+ `test_cross_platform_windows_only.py`（W1–W4） |
| P6-3 | 已落地（有未验项） | `api/tests/test_ci_workflow.py`（15 例，含步骤名逐字同步） |
| P6-4 | 已落地（有未验项） | `scripts/probe_weak_model_planner.py` + `docs/weak-model-usability.md`（实测矩阵） |
| P6-5 | 部分落地 | `api/tests/test_provider_matrix.py`（25 例）+ `test_provider_wire_format.py`（F1–F4：假网关 + 真实 CLI 试出端点契约） |

## 2. 缺口清单（**没有证据的部分**）

这些条目的 ROADMAP 行里带“未做/未验证”字样，逐条写清缺什么条件。
**不把它们藏起来**是这份审计的主要目的：把未验写成已验比不写更坏。

| 交付物 | 缺什么 |
|---|---|
| P5-2 | 长列表虚拟化已做；**“千节点首屏 < 2s”仍未量**（E2E 验的是接缝不是耗时） |
| P6-1 | 本机**没有 Docker**，`docker compose up` 一次都没跑过（语法与自洽已用 D1–D8 静态验过） |
| P6-2 | 本机**没有 Linux/WSL/可用容器**，没在 Linux 上跑过任何东西（平台分派已用 L1–L9 验过） |
| P6-3 | 本机**没有触发 Actions 的手段**，CI 没有一次真实绿灯记录（工作流结构已用 W1–W9 验过） |
| P6-4 | 只到 **planner 粒度**；worker 能否改对文件、verifier 验收、长跑稳定性未测 |
| P6-5 | 三类端点的**真实可达性与鉴权未验**（本机没凭据）；**端点契约已离线实测**（F1–F4：必须支持 SSE 流式 + `response.output_item.done`） |

## 3. 双仓状态

* 公开仓：`8db29c819f2aa7c416361634dcbc22b8f28823d1`（`origin/main`）
* 内部仓：`41efa1bfe87fb6216cae2bcce73d6545e3f45ec9`（`internal/main`）
* 差异必须**恰好 7 个文件**（公开仓 + 7 个 overlay）：
  `BRANCH-codemaker-hub.md`、`api/app/config.py`、`api/tests/test_node_home_config_seed.py`、
  `docs/codemaker-hub-provider.md`、`scripts/codemaker-hub.codex.toml`、
  `scripts/use_codemaker_hub.ps1`、`scripts/verify_codemaker_hub_planner.py`。
  多一个少一个都说明同步出了问题（Q5 会红）。

## 4. 结论

**P2–P5 的交付物都有判据，且判据在 `verify_all` 里跑。** P6 的五条里，
三条（容器化 / 跨平台 / CI）**结构无法在本机实测**（但语法、平台分派、工作流结构都已静态验过），
一条（弱模型）只到 planner 粒度，一条（provider 矩阵）缺真实端点的可达性与鉴权
（**端点契约**已用本机假网关实测）。

所以整体目标**不标记完成**：缺口是**环境**造成的（没有 Docker、没有 Linux、
没有 CI 触发手段、没有真实端点凭据、没有真实模型额度），不是实现没写。
补上它们的成本按信息量排序：

1. **让双平台 CI 真跑一次** —— 能把 P6-2 与 P6-3 的一半假设变成事实或反例，成本几乎为零；
2. 一台有 Docker 的机器跑 `docker compose up` —— 验 P6-1 的“界面可用”；
3. 一个真实端点（任意 OpenAI 兼容）跑 `verify_provider_planner.py` —— 验 P6-5 的通道；
4. 授权真实模型额度后跑 `probe_weak_model_planner.py` 的 worker 档 —— 验 P6-4 的后半段。
