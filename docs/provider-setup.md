# 让节点的 Codex CLI 走你自己的 provider

本工具的真实 runner 会为每个节点派生一个 Codex CLI 进程。通过**隔离的 `CODEX_HOME`**，
可以让这些进程用你指定的任意 **OpenAI 兼容**端点（官方 API、公司网关、本地代理都行），
并且**完全不碰你自己的 `~/.codex/config.toml`**。

不配也能跑：`runner_mode` 默认是 `mock`，不需要任何模型和凭据（见 `README.md`）。

## 1. 启用

```powershell
# 任何 OpenAI 兼容端点
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "https://api.openai.com/v1" `
    -EnvKey  "OPENAI_API_KEY" `
    -Model   "gpt-5-codex"

# 自建网关 / 本地代理（端口按实际改）
powershell -File scripts\use_provider.ps1 `
    -BaseUrl "http://127.0.0.1:8080/v1" `
    -EnvKey  "MY_GATEWAY_KEY" `
    -Model   "my-model" `
    -WireApi "chat"
```

脚本会：

1. 生成隔离 `config.toml`（已存在则保留，`-Force` 覆盖）。**落点由 `state_paths.py` 决定**：
   绑定了工程就是 `<状态根>/projects/<key>/codex-home/`，未绑定才回退 `workspace/codex-home/`；
2. 导出下面这几个环境变量（**仅当前 shell 生效**）；
3. 打印最终用的模型与端点，便于核对。

| 环境变量 | 值 | 说明 |
|---|---|---|
| `CODEX_BATTLEFIELD_RUNNER` | `real` | 启用真实 Codex CLI runner |
| `CODEX_BATTLEFIELD_CODEX_HOME` | 见上 | 隔离配置目录，**隔离的关键** |
| `<EnvKey>` | 你给的占位值 | codex 要求该变量**存在**；本地代理通常不校验内容 |
| `CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG` | `0` | **必须**，见陷阱 2 |

> **为什么落点不能让脚本自己拼**（`docs/spec-state-project-decoupling.md`）：runner 会去
> `Settings.layout.project_dir/codex-home` 找 `config.toml`。两边一旦不一致，
> 表现就是"节点全部 401"——正是陷阱 2.1 记录的那个形态。

脚本同时清空 `CODEX_BATTLEFIELD_CODEX_MODEL` / `_REASONING_EFFORT`，让模型由隔离
`config.toml` 决定，而不是被环境变量覆盖。

启用后按 `README.md` 正常启动 API 即可，树里所有 Codex 子节点都会走这个端点。

## 2. 隔离配置长什么样

`<隔离 CODEX_HOME>/config.toml`（模板见 `scripts/provider.codex.toml.example`）：

```toml
model = "your-model-id"
model_provider = "custom"
model_reasoning_effort = "medium"
model_context_window = 128000

[model_providers.custom]
name = "Custom OpenAI-compatible provider"
base_url = "https://api.example.com/v1"
wire_api = "chat"          # "responses" 或 "chat"；多数第三方网关只支持 chat
env_key = "OPENAI_API_KEY" # codex 只要求这个变量**存在**
```

换模型只需改 `model`，或直接改隔离 `CODEX_HOME` 里的 `config.toml`
（路径用 `python scripts/state_paths.py` 查）。

## 3. 陷阱与根因记录

这一节是本文档最有价值的部分——每一条都是实测踩出来的，而且**失效形态都很隐蔽**。

### 3.1 缺 `[windows] sandbox` 会让 worker 静默变只读

**现象**：worker 明明由 runner 以 `--sandbox workspace-write` 启动，却报告
`sandbox_mode=read-only`，所有命令在 `CreateProcess` 阶段被 `blocked by policy` 拒绝，
无法写文件，节点变 `blocked`。

**根因**：某些 codex 版本在 Windows 上需要 `[windows] sandbox = "unelevated"` 才能
正常建立可写沙箱。你原来的 `~/.codex/config.toml` 里本来就有这段（所以一直正常）；
新建的隔离 `CODEX_HOME` 如果漏掉，`workspace-write` 会退化成只读。

**修复**：`use_provider.ps1 -WindowsSandbox unelevated`。

### 3.2 只读沙箱会忽略 provider 配置

runner 在 `read-only` 沙箱下会追加 `--ignore-user-config`，这会**忽略
`$CODEX_HOME/config.toml`** → 自定义 provider 失效 → verifier 回落到默认 OpenAI 端点而失败。

**已内建修复**：`CodexExecRunner._should_ignore_user_config_for_sandbox` 现在
**配了 `codex_home` 就绝不加该 flag**，不再依赖手工设
`CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG=0`。没配 `codex_home` 时保留原行为。

### 3.3 每节点隔离 `CODEX_HOME` 会丢掉整段 provider 配置

`docs/spec-task-isolation.md` §2.1 把节点的 `CODEX_HOME` 指到 `<base>/nodes/<node-id>`。
**codex 不从父目录继承配置**——它只读自己 `CODEX_HOME/config.toml`。节点 home 里那个文件
当时只有 codex 自己写的 `[projects...] trust_level`，于是 `model_provider` 与
`[model_providers.*]` 整段丢失，请求回落到默认端点：

```
ERROR codex_api::endpoint::responses_websocket:
failed to connect ... 401 Unauthorized, url: wss://api.openai.com/v1/responses
```

**修复**：`_codex_env` 在建节点 home 后调用 `_seed_node_home_config`，把基座 `config.toml`
**每次覆盖**写入节点 home。取"覆盖"而不是"只在缺失时写一次"的理由：基座是 provider 的
唯一真相，留下过期副本会让"改基座只对新节点生效"，而那正是本 bug 的形态。

### 3.4 `env_key` 指名的变量必须留在环境放行名单里

子进程环境默认收窄（`spec-task-isolation.md` §2.4）按前缀白名单放行。如果你的
`env_key` 用了名单外的前缀，**凭据会被自己的收窄过滤掉**，codex 随即以
`Missing environment variable: \`XXX\`` 失败，整条通道断掉。

**修复（两层）**：

* 前缀可配：`CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES`（逗号分隔）；
* runner 的 `_ensure_provider_env_key` 会读隔离 `config.toml` 里声明的 `env_key`，
  在变量缺失时补一个**占位值**——它不是凭据，只是满足 codex 的存在性要求。

> **实测过的一个后果**：`env_key` 若只在开发机的 shell 里存在，那么**由 GUI / 计划任务
> 拉起的 API 会丢掉它**，整条通道断掉。所以"认证在网关里做"并不够，变量本身必须存在。

### 3.5 PowerShell 下 `codex` 会解析成 `codex.ps1`

`Get-Command codex` 返回 `codex.ps1`，用 `Start-Process` 启动会报
`%1 不是有效的 Win32 应用程序`。脚本里应显式用 `codex.cmd`。

### 3.6 `Model metadata ... not found` 是无害警告

```
Model metadata for `your-model-id` not found. Defaulting to fallback metadata
```

codex 内置模型注册表里没有第三方模型名，属装饰性警告，不影响功能。
`model_context_window` / `model_max_output_tokens` 消不掉它。

### 3.7 依赖端点常开

本地代理类端点在进程关闭后即不可用。写作 `use_provider.ps1` 之外的东西做健康检查
时，记得**告警但不拦流程**：配置已就绪是事实，端点没起是另一件事。

## 4. 验证

单次真实 planner 调用（便宜、快速，用来把"通道不通"与"planner 写错"分开）：

```powershell
. .\scripts\use_provider.ps1 -BaseUrl ... -EnvKey ... -Model ...
python .\scripts\verify_provider_planner.py --timeout 180
```

检查项：`exit_code == 0`，`decision` / `leaf_certainty` 通过
`planner-decision.schema.json` 严格校验。

完整链路（planner + worker + verifier + acceptance）：

```powershell
python .\scripts\real_codex_smoke.py --timeout 300 --constitution-runner real
```

通过标准：`ok: true`，`root_status: "done"`，`matching_files` 非空，
`debug_paths_ok: true`。

## 5. 回退

不启用脚本即可回到原状：新 shell 不设这些环境变量，工程就走 `~/.codex` 的默认配置。
`~/.codex` 全程未被修改。隔离 `CODEX_HOME` 目录已在 `.gitignore` 里。

## 6. 为什么不会影响桌面版 Codex

两者是**两套独立安装，二进制不同**：

| | 路径 |
|---|---|
| 本工程调用的 CLI | `%APPDATA%\npm\node_modules\@openai\codex\...\bin\codex.exe` |
| 桌面版 Codex | `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe app-server ...` |

隔离靠 `CODEX_HOME` 只注入给本工程派生的子进程；桌面版的 `app-server` 读它自己的配置。

**清理进程时的注意点**：桌面版进程名同样是 `codex.exe`。终止本工程残留时**必须先看命令行**，
只杀带 `exec` 的 CLI 进程，**不要**动带 `app-server` 的桌面版进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name='codex.exe'" |
  Select-Object ProcessId,@{n='Kind';e={ if ($_.CommandLine -match 'app-server') { 'DESKTOP (keep)' } else { 'CLI (safe to kill)' } }}
```
