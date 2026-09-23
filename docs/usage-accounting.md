# 用量与成本记账（供应商无关）

## 为什么单独有这一层

Codex CLI 只在 `--json` 的 `turn.completed` 事件里回报 **token**；自建网关（例如 CodeMaker Hub 的
`aigw.cost`）会在**它自己的响应**里额外回报**金额**。不同供应商的字段名、币种、计价口径都不一样。

如果把这些形状写进业务代码，换供应商时就要改多处。所以本工程把换算收在**一个模块**里：

```
CodexExecRunner  --捕获-->  RunRecord.usage_json  --规范化-->  app/usage.py  -->  API / 看板
   （原样存储）                    （原始负载）                  （唯一换算边界）
```

## 三层职责

| 层 | 位置 | 职责 |
|---|---|---|
| 捕获 | `api/app/runner.py` `usage_from_codex_logs()` | 从日志尾部取最后一次 `turn.completed.usage`，**原样**放进 `RunnerEnvelope.usage` |
| 存储 | `RunRecord.usage_json` | 存**供应商原始负载**，不做任何解释 |
| 解释 | `api/app/usage.py` | 规范化 + 汇总，唯一知道各供应商字段差异的地方 |

**为什么存原始负载而不是规范化结果**：规范化逻辑以后会改（新供应商、新字段、修 bug）。存原始负载的话，
改进后**对全部历史数据一并生效**；存规范化结果就只能影响新数据。

## 规范化后的形状

```json
{
  "input_tokens": 209900,
  "cached_input_tokens": 195584,
  "cache_write_tokens": 0,
  "output_tokens": 6758,
  "reasoning_tokens": 3653,
  "total_tokens": 216658,
  "cost": { "amount": 0.010157, "currency": "CNY", "source": "cost_object", "container": "aigw" },
  "raw": { "...供应商原始负载，原样保留..." }
}
```

- 缺什么就少什么（只有 token 没有金额是正常的，`cost` 直接不出现）。
- 无法识别时 `normalize_usage()` 返回 `None`，不会污染汇总。

## 金额按币种分桶

`summarize_usages()` 产出 `cost_by_currency`，**不做跨币种相加**——把 CNY 与 USD 加起来是错的。
币种缺失的金额单独放进 `cost_unlabeled`。前端同样分币种展示。

## 换供应商时要动的地方

只动 `api/app/usage.py`：

| 需求 | 改动点 |
|---|---|
| 供应商用了别的 token 字段名 | 在 `_FIELD_CANDIDATES` 的对应元组里加候选键 |
| token 放在嵌套容器里 | 在 `_TOKEN_CONTAINER_KEYS` 加容器键（现已支持 `usage` / `token_usage`） |
| 金额放在别的容器下 | 在 `_COST_CONTAINER_KEYS` 加容器键（现已支持 `aigw` / `gateway` / `billing` / `provider` / `metadata`） |
| 金额是全新形状 | 在 `_cost_of_flat()` 里加一个形态分支 |
| 币种字段名不同 | 在 `CURRENCY_FIELD_CANDIDATES` 加候选键 |

调用方（runner / orchestrator / 前端）**都不需要改**。

## 验证

```powershell
cd C:\projects\codex-battlefield\api
.\.venv\Scripts\python.exe -m pytest -q tests/test_usage.py
```

单测覆盖：codex 形状、OpenAI 形状（`prompt_tokens`/`completion_tokens` + 嵌套 details）、
嵌套 token 容器 + 网关 `cost` 对象、`cost.details` 多币种、无法识别返回 `None`、跨币种不相加。

对已有真实数据的端到端核对方式：把 `CODEX_BATTLEFIELD_DATABASE_URL` 指向某个已跑完的
`workspace/smoke-dbs/*.sqlite3`，调用 `Orchestrator.get_tree()` 读 `usage` 字段。
