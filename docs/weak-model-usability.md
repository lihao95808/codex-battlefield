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
  （单节点树由 verifier 收口），所以"便宜档能不能当 acceptance"仍无答案；
* **没有成功率**：要成功率必须反复跑，本文没做；
* **没有失败模式样本**：这次一次过，没观察到它怎么写错（对比 §3：`qwen3.8-flash` 的失败形态是"结构漂移"）；
* **金额未取到**：端点只在 `usage_json` 里回传 token，不回传费用 —— 按"取不到就记 null"的规矩，这里不推算；
* **没测长跑/多节点/并发**：`scheduler`、`max_concurrency` 这些在单节点里根本没被压到。

复现要花额度（本机这次约 141k input + 5.4k output tokens，价格由端点自己定），所以没进 `verify_all`
的默认步骤 —— 它是 `--real-smoke` 那一档，需要凭据/额度时手工跑。
