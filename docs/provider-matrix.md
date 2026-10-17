# provider 矩阵：官方 / 自建网关 / 本地推理

本文回答一个问题：**同一个战场，怎么接到三类不同的模型端点上去，以及怎么在没有凭据的机器上
验证这些配置是对的。**

`docs/provider-setup.md` 讲的是"为什么用隔离 `CODEX_HOME`、踩过哪些坑"；本文只讲**矩阵本身**
—— 每一类端点的可复制配置、以及每一条判据**验过什么、没验什么**。

## 1. 三类端点

| 类别 | 端点 | 谁在跑 | 凭据在哪 | 参考模型档位 |
|---|---|---|---|---|
| 官方 | `https://api.openai.com/v1` | OpenAI 自己的 Responses API | `OPENAI_API_KEY` | `gpt-5-codex` |
| 自建网关 | `http://127.0.0.1:8080/v1`（端口按实际改） | 公司网关 / 本机转发 | 网关侧，本机只给占位变量 | 网关自己的模型名 |
| 本地推理 | `http://127.0.0.1:11434/v1` | Ollama 一类本机进程 | 通常不校验，变量存在即可 | `qwen2.5-coder:7b` 这类小模型 |

三类的**配置形状完全一样**，差别只在 `base_url` / `env_key` / `model` / 推理档位四件事——
这正是把通用逻辑收在 `scripts/use_provider.ps1`、把"三类怎么填"收在
`scripts/verify_provider_matrix.py` 的原因。

## 2. 可复制的配置

三类都由同一条命令生成（脚本会写到**隔离 `CODEX_HOME`**，不碰你自己的 `~/.codex`）：

```powershell
# 官方
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "https://api.openai.com/v1" -EnvKey "OPENAI_API_KEY" `
    -Model "gpt-5-codex" -ProviderId "official" -ProviderName "OpenAI official"

# 自建网关 / 本机转发
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "http://127.0.0.1:8080/v1" -EnvKey "MY_GATEWAY_KEY" `
    -Model "team-default" -ProviderId "gateway" -ProviderName "Self-hosted gateway" `
    -WindowsSandbox unelevated

# 本地推理（Ollama 类，OpenAI 兼容端点）
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "http://127.0.0.1:11434/v1" -EnvKey "OLLAMA_API_KEY" `
    -Model "qwen2.5-coder:7b" -ProviderId "ollama-local" `
    -ProviderName "Ollama local (OpenAI compatible)" -ReasoningEffort low -ContextWindow 32768
```

生成出来的 `config.toml` 长这样（本地推理那一份，实测输出）：

```toml
model = "qwen2.5-coder:7b"
model_provider = "ollama-local"
model_reasoning_effort = "low"
model_context_window = 32768

[model_providers.ollama-local]
name = "Ollama local (OpenAI compatible)"
base_url = "http://127.0.0.1:11434/v1"
wire_api = "responses"
env_key = "OLLAMA_API_KEY"
```

> `-WireApi` 的参数校验里还留着 `chat` 这个取值，但**默认值只有 `responses`**：
> 显式传 `chat` 会打印警告。原因见下一节。

### 2.1 网关/本地端点的 `env_key` 还要进放行名单

子进程环境**默认收窄**（`docs/spec-task-isolation.md` §2.4），默认放行的凭据前缀是：

```
CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_
```

矩阵里自建网关用 `MY_GATEWAY_KEY`、本地推理用 `OLLAMA_API_KEY` —— **这两个前缀都不在名单里**。
不放行的后果很隐蔽：真实凭据被丢在宿主进程里，runner 又按 `config.toml` 的 `env_key`
补了一个占位值 `local-placeholder`（`_ensure_provider_env_key`），于是 codex 拿到了一个
**存在但是错的** key，症状是端点 401，而配置看上去完全正确。

所以用了非默认前缀的 `env_key` 时，必须在启动 API 的环境里加上它：

```powershell
$env:CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES = "CODEX_,OPENAI_,MY_GATEWAY_,OLLAMA_"
```

`api/tests/test_provider_matrix.py` 的 M10 用真实的 `CodexExecRunner._codex_env` 钉住两个方向：
名单外的凭据**会被丢**（fail-closed 是对的），配了前缀**就留得住**（放行通道真的有效）。

## 3. 判据：`scripts/verify_provider_matrix.py`

```powershell
# 完整报告（默认落临时目录，跑完删掉）
python scripts\verify_provider_matrix.py

# 门禁用法：一行摘要 + 每类端点一行结果
python scripts\verify_provider_matrix.py --quiet --work-dir .tmp\provider-matrix
```

它对三类端点各跑一次**真实生成**（调 `use_provider.ps1 -CodexHome <临时目录> -NoEnv`，
走的是用户真正会走的路径，而不是脚本自己拼一份"看起来一样"的 TOML），然后逐项判：

| 判据 | 失败形态 |
|---|---|
| TOML 可解析 | 配置语法错 |
| 无 UTF-8 BOM | 第一行的键名带隐形前缀，codex 认不出来 |
| 顶层 `model` / `model_provider` 齐全 | 缺了会回落到默认 provider，症状是"改了配置没生效" |
| provider 段键名都在 codex 认识的字段表里 | 编造的键让**整份配置加载失败** |
| `wire_api` 取值在已核实可用的集合内 | 见 4.1 |
| `[windows].sandbox` 只在显式要求时出现 | 没要求却写了，节点沙箱档位就与预期不符 |
| 配置里没有 `sk-` / `Bearer ` 字样 | 隔离配置只该写**变量名**，写进凭据就等于把凭据复制进状态目录 |
| `codex doctor` 读得进这份配置 | 本机没有 codex 时记 `null`（**没查**），不记通过 |

**字段表是怎么来的**（可复核）：codex 的 `config.toml` 里 `[model_providers.*]` 收的是
`ModelProviderInfo`，字段表直接从二进制里抽：

```powershell
# 二进制路径（npm 安装）
$exe = "$env:APPDATA\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
# 在 $exe 里搜 "struct ModelProviderInfo with" 附近的那串字段名
```

实测 codex 0.162.1 抽出来是 21 个字段：`name` / `base_url` / `model_catalog_url` /
`env_key` / `env_key_instructions` / `experimental_bearer_token` / `gateway_oauth` / `aws` /
`wire_api` / `capabilities` / `query_params` / `request_max_retries` / `stream_max_retries` /
`stream_idle_timeout_ms` / `websocket_connect_timeout_ms` / `requires_openai_auth` /
`supports_websockets` / `supports_standalone_web_search`。
`api/tests/test_provider_matrix.py` 里那份白名单就是它。

## 4. 实测踩到的两件事

### 4.0 端点的契约：**必须流式，而且事件序列有硬要求**（离线实测，2026-10-17）

这一条是**不需要凭据**就能验出来的，而且是本文档原来没写的。做法：起一个本机假网关
（标准库 `http.server`），让**真实的 Codex CLI** 通过它跑一次 planner，
把收到的原始请求与"什么应答才能让这一侧成功"逐项试出来 ——
判据在 `api/tests/test_provider_wire_format.py`（F1–F4）。

试出来的两件事，**每一条都是先失败再改对的**：

| 现象（codex 的原话） | 真实原因 | 端点必须做到 |
|---|---|---|
| `stream disconnected before completion: stream closed before response.completed` | 这一侧默认发 `"stream": true`，而假网关回了普通 JSON body | 必须支持 **SSE 流式**，并以 `response.completed` 收尾 |
| `no last agent message; wrote empty content` | 只发了 `response.output_text.delta` / `.done`，**没有** `response.output_item.done` | 事件序列里必须有 **`response.output_item.done`**，且带上完整的 message item |

另外两条（形状层面）：

* 请求打到 **`POST <base_url>/responses`**（`base_url` 以 `/v1` 结尾 → `/v1/responses`）；
* body 里是 **`input`**（Responses 形状），**不是 `messages`**（Chat Completions 形状）——
  这也从另一面印证了 4.1 那条"`wire_api = chat` 已经不存在"。

`docs/provider-setup.md` 里那句"多数第三方网关只支持 chat"因此要加一层解释：
**只实现 Chat Completions 的网关接不上**（不是配置写法问题，是协议不同）。

### 4.1 新版 codex 不再接受 `wire_api = "chat"`，而且是**整份配置加载失败**

`scripts/use_provider.ps1` 原来的默认值是 `chat`，`use_local_proxy.ps1` 也是。
在本机 codex `0.162.1` 上实测：只要配置里有 `wire_api = "chat"`，**不管这个 provider 有没有被
用到**，配置就加载不了：

```
Codex Doctor v0.162.1 · windows-x86_64
  ✗ config       config could not be loaded
        error                    invalid data
```

`invalid data` 指不到是哪一行——所以这个坑的形态是"所有节点一起起不来，日志里没有线索"。
同一个 `config.toml` 只把 `chat` 改成 `responses`，`codex doctor` 立刻变绿（两边都实测过）。

上游记录：[openai/codex#41816](https://github.com/openai/codex/issues/41816)（"CLI fails at
config load when an unused `model_providers` entry still sets `wire_api = chat`"）。

**处置**：两个脚本的默认值都改成 `responses`；显式传 `chat` 时打印警告并说明后果。
真要用只支持 Chat Completions 的端点，只能装 0.80 那一档的旧 codex
（[阿里云文档](https://help.aliyun.com/zh/model-studio/codex#4) 也是这个口径）。

> 这条是**写文档时顺手验出来的**：本文档原来会写"多数第三方网关只支持 chat，所以回落到 chat"
> —— 那句话在本机 codex 上会直接把配置写坏。判据的价值就在这里。

### 4.2 `-CodexHome` / `-NoEnv` 为什么必须存在

没有它们，三类端点没法在同一台机器上验证：脚本只会写"当前绑定工程"的那一个 `codex-home`，
三份配置互相覆盖；而且它会**修改当前 shell 的环境变量**（`runner=real`、
`CODEX_BATTLEFIELD_CODEX_HOME`、`env_key` 指名的变量）。

`-CodexHome` 只跳过落点解析，`-NoEnv` 只写文件、一个环境变量都不碰。
两者都是**给自动化用的**，正常交互用法不需要传。

顺带修掉一个静默风险：脚本原来无条件
`Set-Item -Path "Env:$EnvKey" -Value $ApiKey`，会把用户 shell 里**已有的真实凭据覆盖成占位值**。
现在只在变量不存在时才写。


### 4.4 鉴权那一半：用**带鉴权的假网关**把"错的 key 会被拒"验掉（2026-10-18）

真凭据能验的只有"厂商端点接不接受这把钥匙"；但在此之前有一半**不需要凭据**：
**错的 key 会不会被拒、而且这一侧会不会把它报出来**。这条以前一直没验，现在由
`api/tests/test_provider_wire_format.py` 的 **F5/F6** 钉住（假网关新增 `require_token`，
不匹配就回 401）：

| 用例 | 配置 | 判据 |
|---|---|---|
| **F5** | 网关要求 `good-token`，环境里放 `wrong-token` | 真实 Codex 调用 **`exit_code != 0`**，且日志里能看到 `401 / unauthorized / invalid api key`，并且网关记录里真的出现 `Bearer wrong-token` |
| **F6** | 同一份配置，只把 key 换成 `good-token` | 调用 **`exit_code == 0`** |

F5 与 F6 必须成对：单看 F5 会有盲区（"是不是只要网关要鉴权这一侧就一律失败"）。
变异（把网关的校验改成 `return False`）→ **F5 立刻红**（`assert 0 != 0`：错 key 也成功了）。

这条证明的是**失败方向**：401 不会被静默吞成"模型没有输出"。它**不**证明某家厂商云端端点
的鉴权细节（签名、组织/项目头、IP 白名单等），那仍然只能由使用者在自己的端点上跑
`scripts/verify_provider_planner.py`。

### 4.3 真实端点实测：**可达性跑通了，鉴权这条只验了一半**（2026-10-18）

§4.0 用的是本机假网关（证明"端点契约"），这一节换**真实端点**：本机 `127.0.0.1:15721` 上的
OpenAI 兼容代理（`wire_api = "responses"`，配置里 `requires_openai_auth = false`）。
脚本仍是 `scripts/verify_provider_planner.py`（只发**一次** planner 调用），隔离 `CODEX_HOME`
由 `-CodexHome` 指定，不碰 `~/.codex`。

| 组 | 端点 | 结果 | 证据 |
|---|---|---|---|
| 正样本 | `127.0.0.1:15721/v1` | `ok=true`，`exit_code=0` | `decision=execute`、`leaf_certainty=300%`、`PlannerDecision` 校验通过，约 15 秒 |
| 负样本 | `127.0.0.1:15999/v1`（无监听） | `ok=false`，`exit_code=124` | `reason=资源/流量类阻塞，可等待后重试：Codex planner hard timeout after 60s.` |

负样本那条值得单独说：**端点不通时看到的是"硬超时"，不是"连接被拒绝"** —— codex 自己会退避
重试，把连接失败吞成一次慢失败。所以"这个端点到底通不通"最快的判据是：**给它一个明确的
`--timeout`，看它是"很快回来"还是"耗满超时"**。本机这一对的差别是 15 秒 vs 60 秒，够分辨。

**仍然没验证的**（与 §5 一致，不要外推）：

* 需要凭据的云端端点：本机没有那种凭据，也不打算为演示买一份；
* **"发错的 key 会被拒"**：本机这个端点 `requires_openai_auth = false`，**没有 key 校验** ——
  所以"鉴权"这一条在这里只证明了"不带 key 也能连通"，证不了"错的 key 会被 401"。
  这一半要靠使用者在自己有鉴权的端点上跑同一条命令。

## 5. 验证边界（这段和判据一样重要）

**本仓库在无凭据机器上能验的**：

* 三类端点的配置由真实脚本生成、结构正确、`codex doctor` 读得进去（本机有 codex 时）；
* `api/tests/test_provider_matrix.py` 的 M1–M9 判据，以及它们的变异版本（M5 系列专门证明
  坏配置会被抓住：`chat`、编造的键、BOM、`model_provider` 与段名不一致、配置里带凭据）。

**本仓库验不了的**（不要在别处当成"已验证"）：

* **某家厂商的云端端点**：本机没有那种凭据，也不打算为演示买一份 ——
  用 `scripts/verify_provider_planner.py`（一次 planner 调用）或
  `scripts/real_codex_smoke.py`（完整链路）在你自己的环境里跑；
* **厂商特有的鉴权细节**（签名、组织/项目头、IP 白名单、按 key 计费的限额）：
  §4.4 的 F5/F6 只证明了"错的 key 会被拒、而且会被看见"这条**通用**行为；
* **真实端点的读法差异**（不同厂商在 SSE 细节/usage 字段上的偏差）：F1–F4 只钉住
  我们这一侧发什么，以及一个**最小** Responses 子集够不够用。

**已经验过的**（免得被上面三条误读成"什么都没验"）：

* 端点**契约**（F1–F4，假网关 + 真实 CLI，离线）；
* **可达性**正/负样本（§4.3：真实端点 15 秒成功、死端点撞硬超时）；
* **鉴权的失败方向**（§4.4：错 key → 401 可见；对 key → 成功）。

报告里的 `verified_here` / `not_verified_here` 两张清单就是这条边界的机器可读版本。

## 6. 回退

删掉隔离 `CODEX_HOME`（或新开一个不设那些环境变量的 shell）即回到原状；
`~/.codex` 全程未被修改（脚本只写 `-CodexHome` 指向的目录）。
