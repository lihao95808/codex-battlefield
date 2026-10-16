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
* 这一节刻意不写 `worker` 档的结论：本次没有测 worker（要真改文件、跑测试、花更多钱），
  所以"小模型能不能当 `worker`"这个问题**本文没有答案**，不要从 planner 的结果外推。

* 限制（诚实记录）：
  * 上表是**一轮**结果，不是统计意义上的成功率。要看成功率得反复跑，本文没做。
  * 端点侧的成本元数据是厂商特有的字段路径，所以公开仓里只留**中性**的实现
    （`--cost-path` 由使用者指定）；
  * 没有做"同一模型不同 `reasoning_effort`"的对照 —— 那会成倍放大调用次数。
