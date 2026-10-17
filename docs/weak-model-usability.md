# 弱模型 / 异构模型可用性（P6-4）

本文回答一个问题：**哪些档位的模型能当这个战场的 planner 用，哪些不能，以及"能用"这句话
到底覆盖到什么。**

## 1. 先划边界：这里的"能用"只指一件事

判据只有一条链：**隔离 `CODEX_HOME` → Codex CLI → 模型 → 产出符合
`schemas/planner-decision.schema.json` 的规划决策**。

它**不覆盖**：

| 没验的事 | 为什么没验 | 要验就用 |
|---|---|---|
| worker 能改对文件 | 那是"能干活"，要真写文件、跑测试 | `scripts/real_codex_smoke.py` |
| verifier 验收通过 | 同上 | 同上 |
| 长跑稳定性（几百节点后还准不准） | 那要跑很久、花很多 token | `scripts/probe_weak_model_planner.py` 多轮 + `bench/` |
| 成本可预测 | 逐次费用只有端点自己知道 | 见 §4 |

把这张表放在最前面，是因为"某模型能用"这句话最容易**被扩大解释**：
实测过的只有"它在一次 planner 调用里按契约输出了"。别把它读成"它能替我把活干完"。

## 2. 实测怎么跑的

```powershell
# 1) 给每档模型生成一份隔离配置（不碰 ~/.codex，也不改当前 shell 的环境变量）
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "<你的 OpenAI 兼容端点>/v1" -EnvKey "OPENAI_API_KEY" `
    -Model "deepseek-flash" -ProviderId "gw" -ProviderName "Local gateway" `
    -WireApi responses -ReasoningEffort low -ContextWindow 128000 `
    -WindowsSandbox unelevated -CodexHome .tmp\model-homes\deepseek-flash -NoEnv -Force

# 2) 让这一档去回答同一个规划问题（root 目标的样例见脚本注释）
$env:CODEX_BATTLEFIELD_CODEX_HOME = "$PWD\.tmp\model-homes\deepseek-flash"
python scripts\probe_weak_model_planner.py --models "deepseek-flash" --timeout 120 --json-out report.json
```

每档的判据（逐条，不合成一个分数）：

* `exit_code == 0`（否则先按"通道问题"查，不是模型问题）；
* `decision ∈ {"execute","decompose","blocked","repair"}`；
* `leaf_certainty ∈ {"300%","not_sure"}`（**这两个取值是从 schema 里读的**）；
* `decision=execute` 时 `children` 必须为空；`decompose`/`repair` 时必须非空；
* `decision=blocked` **不计入可用** —— CLI 通了不等于模型给出了可用规划。

> 判据自己踩过一次坑，值得写下来：第一版把 `leaf_certainty` 当成 0–100 的百分比范围，
> 于是把一个模型**完全正确**的 `300%` 判成了失败。`300%` 正是 `prompts/planner.md` 与
> schema 共同规定的"有把握一次做完"的取值。**判据比契约更聪明 = 判据是错的**，
> 所以现在契约从 `schemas/planner-decision.schema.json` 里读，测试
> `api/tests/test_weak_model_usability.py::test_w1_*` 钉住这一点。

## 3. 实测结果（同一端点、同一提示词、同一轮次）

| 模型档 | 结论 | `decision` | `leaf_certainty` | 耗时 | 失败形态 |
|---|---|---|---|---|---|
| `deepseek-flash` | **可用** | `execute` | `300%` | 10.2s | — |
| `glm-5.3-flash` | **可用** | `execute` | `not_sure` | 34.3s | — |
| `qwen3.8-flash` | **不可用（结构不合契约）** | — | — | 19.0s | 顶层用 `verdict` 而不是 `decision`，`PlannerDecision.model_validate` 直接抛 `Field required` |
| `deepseek-v4-pro` | **本次未验成** | `blocked` | `not_sure` | 2.3s | `exit_code=1`：CLI 层就失败了，**不是模型结论** |
| `gpt-5.6-luna` | **本次未验成** | `blocked` | `not_sure` | 90.6s | 同上 |

读法（这三条都很重要）：

1. **两档便宜模型能当 planner 用**：`deepseek-flash` 给了"一次做完 + 300%"，
   `glm-5.3-flash` 给了"一次做完 + 不确定"。后者虽然保守，但**是合法且自洽的决策**——
   "不确定就说不确定"正是契约要的行为，不该被当成失败。
2. **失败模式是"结构漂移"，不是"答得不好"**：`qwen3.8-flash` 把 JSON 的键名从
   `decision` 写成了 `verdict`。它读懂了任务、给了正确的语义（execute），
   但**键名对不上 schema**，于是整条链路直接在校验处炸掉。
   这类失败**可自愈**：`api/app/retry.py` 的重试上下文会把校验错误回给模型。
   也就是说小模型的脆弱点是"格式"，而不是"理解力"——这比"小模型不会规划"乐观得多，
   也指出了该往哪修（schema 反馈重试，而不是换更大的模型）。
3. **两档"没验成"必须如实标记，不能算作模型的锅**：`deepseek-v4-pro` 与
   `gpt-5.6-luna` 的 `exit_code=1` 说明 Codex CLI 那一层就没跑通。
   本脚本把 `exit_code != 0` 单列为"通道问题"正是为了这个：
   把"端点/CLI 的问题"记成"模型不行"，比不测更糟。

## 4. 钱花了多少（同一端点自报的账）

用同一个短提示（22 字）在同一个本地网关上各要一次，网关回报的用量与费用：

| 模型 | prompt tokens | completion tokens | 本次费用（端点自报） |
|---|---|---|---|
| `deepseek-flash` | 37 | 79（其中推理 56） | 0.0353 CNY |
| `glm-5.3-flash` | 20 | 128（其中推理 127） | 0.0262 CNY |
| `qwen3.8-flash` | 100 | 60（其中推理 20） | 0.0169 CNY |

**这些数字只说明"便宜到什么量级"，不说明 planner 单次成本**：planner 的提示词是几千
token 的规则清单，比上表这个 22 字的探针大两个数量级。要单次成本，得让端点回报**真实
planner 调用**的账 —— 那条路径上 Codex CLI 会把费用元数据丢掉，所以本文**不给**
planner 的单次金额：**没量到就不写**。

## 5. 结论：怎么用小模型

* **planner 可以交给便宜档**，但要留两条护栏：
  （a）schema 校验失败必须走**带校验错误的自动重试**（`api/app/retry.py`），而不是直接
  把节点判死；（b）`decision=blocked` 要能被父节点看见并降级处理，而不是当成"模型坏了"。
* **不同的便宜档不要混着当"同一档"**：实测里 `deepseek-flash` 敢下 `300%` 结论，
  `glm-5.3-flash` 只肯给 `not_sure`。它们在"敢不敢下结论"上的差别会直接影响树的形状
  （`not_sure` 更容易被拆开），所以**换模型会换树形**，这是可预期的影响，不是 bug。
* **`provider` 层面的坑与模型无关**（`wire_api` 只有 `responses`、`env_key` 要进放行名单），
  见 `docs/provider-setup.md` 与 `docs/provider-matrix.md`。

## 6. 复现与限制

* 复现：`scripts/probe_weak_model_planner.py`（判据与报告格式都是可反驳的）+
  `scripts/probe_model_cost.py`（用量/费用，**取不到记 null，不推算**）。
  端点还没确认通不通时，先用 `scripts/probe_responses_endpoint.py` 发一次最小请求 ——
  它失败时能分清"端点不通"和"CLI 配置错了"，比直接跑 planner 便宜得多。
* 这一节（§3–§5）刻意不写 `worker` 档的结论：那些数字只覆盖"会按 schema 输出 planner JSON"。
  **"小模型能不能当 worker"由 §7 单独回答**（2026-10-17 补测，一轮、单节点）。

* 限制（诚实记录）：
  * 上表是**一轮**结果，不是统计意义上的成功率。要看成功率得反复跑，本文没做。
  * 端点侧的成本元数据是厂商特有的字段路径，所以公开仓里只留**中性**的实现
    （`--cost-path` 由使用者指定）；
  * 没有做"同一模型不同 `reasoning_effort`"的对照 —— 那会成倍放大调用次数。

## 7. `worker` 档实测（2026-10-17，一轮、单节点）

§3 的矩阵只回答"会不会规划"。这一节补上"**能不能真改对文件**"——用 `scripts/real_codex_smoke.py`
跑**完整链路**（宪法草案 → 起树 → 真实 planner → 真实 worker 改文件 → 真实 verifier 验收）。

### 怎么跑的

```powershell
# 端点：本机 127.0.0.1:15721 上的 OpenAI 兼容代理（不用 key，wire_api=responses）
$env:CODEX_BATTLEFIELD_CODEX_HOME = "<隔离的 codex home，model = deepseek-flash>"
$env:CODEX_BATTLEFIELD_STATE_HOME = "<隔离状态根>"
$env:CODEX_BATTLEFIELD_RUNNER = "real"
api\.venv\Scripts\python.exe scripts\real_codex_smoke.py `
    --codex-bin "<npm 的 codex.cmd>" --timeout 300 --model deepseek-flash
```

隔离的 `codex home` / `state home` 是刻意的：不碰开发机的主配置，也不把 smoke 的树写进真实工程。

### 结果：`ok = true`，`root_status = done`

| 运行 | 状态 | exit | input / cached / output tokens | 时间（UTC+8） |
|---|---|---|---|---|
| planner | succeeded | 0 | 25 544 / 15 360 / 1 260 | 20:51:16 |
| worker | succeeded | 0 | 68 141 / 44 672 / 1 382 | 20:51:33 |
| verifier | succeeded | 0 | 47 408 / 20 992 / 2 735 | 20:51:56 |

worker 自己报的证据（`result_json`）：`changed_files=["BATTLEFIELD_SMOKE.txt"]`、
`length=31`、`expected_length=31`、`exact_match=True`、`has_trailing_newline=False`。
verifier 独立复核（`verdict=pass`）：`Format-Hex` 逐字节比对 31 字节、末字节 `0x6B`（不是 CR/LF）、
`leaf_misclassified=false`、`constitution_verdict=within_bounds`。
宿主侧另有一道：`smoke_files` 与 `matching_files` 都命中同一个 worktree 里的文件，
且 `debug_paths_ok=true`（prompt / last-message / logs / workdir / constitution_hash 五个路径都在）。

### 这一轮**证明了**什么、**没证明**什么

证明：一个便宜档（`deepseek-flash`）**能当 worker 用** —— 它按 schema 交回了"改了哪个文件 + 证据"，
文件内容逐字节正确，verifier 也独立复核通过；三层（planner / worker / verifier）都跑在同一个便宜档上，
整条链路约 **40 秒**。

**没证明**（不要外推）：

* **只有一轮、单节点**：`run_kinds = [planner, worker, verifier]`，**acceptance agent 这次没有跑**
  （单节点树由 verifier 收口），所以"便宜档能不能当 acceptance"仍无答案（**§8 已补**：多节点一轮里真实 acceptance 跑过并通过）；
* **没有成功率**：要成功率必须反复跑，本文没做；
* **没有失败模式样本**：这次一次过，没观察到它怎么写错（对比 §3：`qwen3.8-flash` 的失败形态是"结构漂移"）；
* **金额未取到**：端点只在 `usage_json` 里回传 token，不回传费用 —— 按"取不到就记 null"的规矩，这里不推算；
* **没测长跑/多节点/并发**：`scheduler`、`max_concurrency` 这些在单节点里根本没被压到。

复现要花额度（本机这次约 141k input + 5.4k output tokens，价格由端点自己定），所以没进 `verify_all`
的默认步骤 —— 它是 `--real-smoke` 那一档，需要凭据/额度时手工跑。

## 8. 递归链路（多节点 + acceptance）实测（2026-10-18）

§7 只有单节点，`acceptance` 一次都没跑。这一节把**整条递归链路**跑到底：root planner 拆出
一个叶子 → 叶子 worker 真写文件 → verifier 独立复核 → **root 用 acceptance agent 验收组合结果**。
三轮都跑在同一个便宜档（`deepseek-flash`），端点仍是本机 `127.0.0.1:15721` 上的
OpenAI 兼容代理（`wire_api=responses`，不需要 key）。

判据不看"我觉得跑通了"，看 `scripts/real_recursive_probe.py` 自己写出的 `summary.json`：

| 字段 | 要求 |
|---|---|
| `ok` | `true` |
| `root_status` | `done` |
| `max_depth` | `>= 1`（真的拆过一层） |
| `run_kinds` | 包含 `planner` / `worker` / `verifier` / **`acceptance`** |
| `acceptance_run_count` | `>= 1`，且**必须是真实运行**（见 §8.5） |
| `probe_files` | 非空（叶子真在目标仓库工作树里写出了文件） |
| `debug_paths_ok` | `true`（每条真实运行都有 prompt / last-message / logs / workdir / 宪法哈希） |

### 8.1 怎么跑的

```powershell
$env:CODEX_BATTLEFIELD_CODEX_HOME = "<隔离 codex home，model = deepseek-flash>"
api\.venv\Scripts\python.exe scripts\real_recursive_probe.py `
    --codex-bin "<npm 的 codex.cmd>" --model deepseek-flash --timeout 300
```

目标仓库是探针现建的临时 git 仓（`recursive-probe-targets/…`），数据库也是隔离的
（`probe-dbs/…`）——**不碰开发机的 Codex 主配置，也不把树写进真实工程**。
宪法生成用 `mock`（探针要验的是 planner / worker / verifier / acceptance 四层真实调用）。

### 8.2 第 1 轮（00:42）：树一步都没走 —— 被我自己并发的另一个探针占满了槽位

`summary.json`：`ok=false`、`root_status=queued`、`run_count=0`。
事件里写得很清楚（不是静默失败）：

```text
scheduler.external_codex_slots_full:
  {"external_codex_processes":1,"effective_codex_slots":1,"capacity":1, …}
```

原因不是程序缺陷，是**我同时跑了 §9 那个 provider 探针**：那个 codex 进程的命令行里带着
本项目路径，于是被槽位记账当成"已占用的 battlefield agent"，而自动并发当时只给了 1 个槽位。
这说明两件事：① 槽位记账是**准的**（它连我自己起的进程都数进去了）；② **同一台机器上不要
并发跑两个真实探针**，否则第二个会抢不到槽位。本条按"失败要指名"的规矩记在这里。

### 8.3 第 2 轮（00:43）：小模型把 JSON 写坏 → 被判 `blocked` → **树形状走偏**

这一轮真的跑起来了，但子节点的 planner 交回的 `--output-last-message` 不是合法 JSON：

```json
{"decision":"execute", …, "verification_plan":"…证据。"},"block_report":null}
```

`json.loads` 在 **char 1042** 抛 `Extra data: line 1 column 1043`：对象已经闭合，后面又跟了
一段。旧代码把这条失败当成 `decision=blocked`，于是：

```text
planner.decision: Codex planner output parse failed: Extra data: line 1 column 1043 (char 1042)
planner.blocked_converted: 规划器返回 blocked，但节点目标非空 → 转成"递归指导"子任务
```

结果：`max_depth=2`、多出两个"递归指导 / skill-note"子节点、`probe_files` 为空、
`root_status=waiting_children`、`ok=false`。

**这条是本轮最有价值的失败样本**：小模型的问题不是"不会规划"，而是**结构漂移**
（多写一个 `}`），而系统把它当成了"任务受阻"这种**语义结论**，于是一次解析失败污染了
整棵树的形状。§3 的 `qwen3.8-flash` 也是同一类失败（顶层键写错），只是那次直接校验抛错、
没有继续传播。

### 8.4 修法一：结构漂移触发**一次自纠重试**（`api/app/runner.py` + `api/app/retry.py`）

`_run_codex_io` 里，解析失败不再是"终局"，而是带着**可行动的反馈**重发一次：
把解析错误原文 + "只输出一个 JSON 对象、不要代码块围栏、不要第二个对象"追加到提示词末尾
（`retry.parse_failure_feedback`）。两条实现细节是有意的：

* **反馈只出现在重发上**：第一次就写"上次输出不合法"是凭空指责（P2 钉住这条）；
* **`*-prompt.txt` 记的是真正发出去的那一版**：否则事后复盘的依据是假的。

判据 `api/tests/test_parse_failure_retry.py`（P1–P4）：

| 用例 | 盯什么 |
|---|---|
| P1 | 第一次坏、第二次好 → `exit_code=0`，且提示词/日志里留下"重试反馈"与 `Extra data` |
| P2 | 第一次的提示词里**没有**反馈块 |
| P3 | `codex_retry_attempts=0` 时**不许**偷偷多跑一次，且报错原文留在 payload |
| P4 | 一直坏下去有终点：跑满预算后返回失败，理由仍是解析错误 |

变异（把重试分支改成 `if False and …`）：**P1/P2/P4 三条红**，P3 仍绿（它本来就是
"配额被尊重"的对照）。恢复后 4 条全绿。

### 8.5 修法二：探针只认**真实运行**（`scripts/real_recursive_probe.py`）

第 2 轮的 `summary.json` 曾经报 `acceptance_run_count=2`、`run_kinds` 含 `acceptance`
——**这是假证据**。编排器在"子节点没全 done"时会记一条 `_record_synthetic_run`
（`kind=acceptance`、`status=succeeded`，但 `logs/prompt/last_message` 全空）。
探针原来把 synthetic 也算成"acceptance 跑过"，于是**差点用一条合成记录去证明真跑过**。
现在：`run_count` / `run_kinds` / `acceptance_run_count` / `debug_paths_ok` 只统计真实运行
（三个路径都在），synthetic 单独计数（`synthetic_run_count`），且 `debug_paths_ok` 要求
**至少有一条**真实运行。

### 8.6 第 3 轮（00:47）：`ok = true`，8 次真实运行，含**真 acceptance**

| 运行 | 节点 | 角色 | exit | input / cached / output tokens |
|---|---|---|---|---|
| 1 | root | planner | 0 | 19 995 / 15 360 / 2 043 |
| 2 | 叶子 | planner | 0 | 20 218 / 15 360 / 714 |
| 3 | 叶子 | worker | 0 | 187 956 / 169 088 / 8 792 |
| 4 | 叶子 | verifier | 0 | 222 720 / 204 672 / 7 989 |
| 5 | skill-note 子节点 | planner | 0 | 719 031 / 682 368 / 21 138 |
| 6 | skill-note 子节点 | worker | 0 | 2 116 465 / 2 036 864 / 49 414 |
| 7 | skill-note 子节点 | verifier | 0 | 358 163 / 331 776 / 6 254 |
| 8 | **root** | **acceptance** | 0 | 270 724 / 232 192 / 11 021 |

`summary.json`：`ok=true`、`root_status=done`、`max_depth=1`、3 个节点全 `done`、
`run_count=8`、`synthetic_run_count=0`、`acceptance_run_count=1`、
`debug_paths_ok=true`、`probe_files` 命中两个 worktree 里的
`GAME_VERTICAL_SLICE_PROBE.md`。整链 **9 分 43 秒**（16:48:08 → 16:57:51 UTC），
合计 **3 915 272 input（其中 3 687 680 命中缓存）/ 107 365 output** tokens。

### 8.6.1 八轮复跑：修完之后 **8 / 8 全绿**，最深跑到**深度 6**

同一命令连跑八轮（每轮自建目标仓与数据库，互不影响；2026-10-18 16:48–19:40 UTC）：

| 轮 | 开始（UTC） | 墙钟 | 深度 | 真实运行（合成） | 失败 | input / output tokens | `ok` |
|---|---|---|---|---|---|---|---|
| 1 | 16:48:08 | 9 分 42 秒 | 1 | 8 | 0 | 3 915 272 / 107 365 | `true` |
| 2 | 16:58:36 | 15 分 35 秒 | 1 | 8 | 0 | 7 506 775 / 108 070 | `true` |
| 3 | 17:14:41 | 11 分 02 秒 | 1 | 8 | 0 | 6 208 427 / 130 937 | `true` |
| 4 | 18:17:11 | 12 分 55 秒 | 1 | 8 | 0 | 7 762 370 / 129 635 | `true` |
| 5 | 18:30:32 | 23 分 20 秒 | 1 | 8 | 0 | 8 562 654 / 154 180 | `true` |
| 6 | 18:54:21 | 14 分 19 秒 | 2 | 9（1 条合成） | 0 | 4 473 152 / 108 090 | `true` |
| 7 | 19:18:47 | 21 分 27 秒 | 1 | 8 | 0 | 4 668 896 / 116 704 | `true` |
| **8** | 19:40:52 | **29 分 46 秒** | **6** | **31（9 条合成）** | 0 | **24 096 468 / 622 870** | `true` |

八轮全是**真实** acceptance（`acceptance_run_count >= 1`）、`probe_files` 非空、`debug_paths_ok=true`；
树的形状**不是固定的**：

* 五轮停在深度 1（根 + 1 个叶子 + 1 个 skill-note 兄弟，共 3 个节点、8 次运行）；
* 第 6 轮深度 2（6 个节点、9 次运行）；
* **第 8 轮深度 6**（26 个节点、31 次运行、9 条合成 acceptance 记录）—— 同一句目标、同一档模型，
  它把树一路拆到了 6 层。**成本随之跳一个量级**（24.1M input vs 4–8M），墙钟也最长。

第 6/8 轮里出现的**合成 acceptance 记录**（子节点未全 done 时的占位）都被 `summary` 单独计数
（`synthetic_run_count`）、**没有**算成 acceptance 证据 —— 这正是 §8.5 那次修正要防的错。

修之前是 **0 / 2 可用**（§8.2 并发抢占、§8.3 解析失败）；修之后 **8 / 8**。
样本仍然小（n=8、同一档模型、同一端点），所以本文只说"这八轮全绿"，不说"成功率达 100%"；
而且"树有多深"目前是**模型当场决定的**，不是我们能承诺的 —— 想控成本要配并发/深度上限，
不是靠"它应该只拆一层"。

顺带两条工程事实：

* 墙钟 9–30 分钟、input token 3.9M–24.1M 的波动，主因是**拆得有多深**（第 8 轮 31 次运行），
  其次是 skill-note 子节点里那次 worker 读上下文（单次可吃 2.1M input）；
* 深度每加一层，运行次数按"planner + worker + verifier"三倍往上走，**所以"允许多深"是成本决策**。

### 8.7 这一节**证明了**什么、**没证明**什么

证明：

* 便宜档（`deepseek-flash`）能把**四层角色**都跑过一遍：planner 在 root 与子节点上都给出
  符合 schema 的决策，worker 真写出文件，verifier 独立复核，**acceptance agent 真实运行并
  给出通过结论**；
* 系统对"结构漂移"这类弱模型典型失败有了**自纠**路径（§8.4），而不是把它当语义结论传播；
* 探针**不再用合成记录充当证据**（§8.5）。

没证明（不要外推）：

* **成功率只能说“修之后 8 / 8”**：修之前 0 / 2（§8.2 并发抢占、§8.3 解析失败），修之后 8 / 8（§8.6.1）；
  n=8 仍然不足以给百分比，而且八轮都是同一档模型、同一端点、同一句目标 ——
  **变的是树拆多深**（1–6 层），不是任务形状被控制住了。
* **深层树只碰到两次**：八轮里六轮 `max_depth<=1`、一次 2 层、一次 **6 层**（26 个节点、31 次运行、24.1M input）
  `max_concurrency` 在真实链路上只到 2；
* **金额仍未取到**：端点只在 `usage_json` 里回传 token，不回传费用（按规矩记 `null`，
  不按单价推算）；
* **验收质量没有独立判据**：`acceptance` 的 `verdict=pass` 是模型自己的结论，本文只证明
  "它跑了、格式对、结论是 pass"，不证明"它的验收标准比 verifier 更严"。
