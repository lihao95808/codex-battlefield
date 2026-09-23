# 让本工程的 Codex CLI 走 CodeMaker Hub

本工程真实 runner 调的 Codex CLI 可以通过隔离的 `CODEX_HOME` 指向 CodeMaker Hub 的本地代理，
从而使用便宜模型（默认 `deepseek-v4.1-flash`），并且**完全不影响桌面版 Codex 和 `~/.codex`**。

## 启用

```powershell
cd C:\projects\codex-battlefield
.\scripts\use_codemaker_hub.ps1
```

该脚本会：

1. 从 `~/.codemaker-hub/proxy.port` 读 Hub 实际端口（缺省 `15721`）；
2. 按 `scripts/codemaker-hub.codex.toml` 模板生成 `workspace/codex-home/config.toml`（已存在则保留，`-Force` 覆盖）；
3. 设置以下环境变量，仅对当前 shell 生效；
4. 请求 Hub `/health` 做可达性检查。

| 环境变量 | 值 | 说明 |
|---|---|---|
| `CODEX_BATTLEFIELD_RUNNER` | `real` | 启用真实 Codex CLI runner |
| `CODEX_BATTLEFIELD_CODEX_HOME` | `workspace\codex-home` | 隔离配置目录，隔离的关键 |
| `CMHUB_API_KEY` | `hub-local` | 占位值；Hub 本地代理不校验，但 codex 要求 `env_key` 对应的变量存在 |
| `CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG` | `0` | **必须**，见下方陷阱 2 |

脚本同时清空 `CODEX_BATTLEFIELD_CODEX_MODEL` 和 `CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT`，
让模型由隔离 `config.toml` 决定，而不是被环境变量覆盖。

启用后按 `README.md` 正常启动 API 即可，任务树里所有 Codex 子节点都会走 Hub。

## 隔离配置内容

`workspace/codex-home/config.toml`（由 `scripts/codemaker-hub.codex.toml` 生成）：

```toml
model = "deepseek-flash"
model_provider = "cmhub"
model_reasoning_effort = "medium"
model_context_window = 128000
model_max_output_tokens = 16384

[model_providers.cmhub]
name = "CodeMaker Hub"
base_url = "http://127.0.0.1:15721/v1"
wire_api = "responses"
env_key = "CMHUB_API_KEY"

[windows]
sandbox = "unelevated"
```

换模型只需改 `model`，或直接改 `workspace/codex-home/config.toml`。

## Hub 本地代理接口

Hub GUI 在 `127.0.0.1:<proxy.port>` 暴露一个本地转发代理（无需鉴权）：

| 路径 | 用途 |
|---|---|
| `/health` | 存活探测，返回 `{"status":"healthy",...}` |
| `/v1/responses` | **Codex 专用**，只接受 Hub 的 Codex 模型白名单 |
| `/v1/chat/completions` | 通用 OpenAI 兼容，模型名不带 provider 前缀（如 `qwen3.8-flash`） |

### ⚠️ 模型白名单会变，且**变更时静默失败**

2026-09-11 实测：`deepseek-v4.1-flash` **已从白名单移除**，被 `deepseek-flash` 取代。由于模型名写死在静态 `config.toml` 里，**所有走 Hub 的调用立即全部失败**，而错误只出现在 `--json` 事件流的 `turn.failed` 里——终端上看起来只是"没有输出"。

**每次长跑前、以及出现"调用无输出"时，先刷新白名单。** 最省事的刷新法：发一个不存在的模型名，Hub 会把完整可用清单回给你。

当前白名单（2026-09-11 快照）：

```
deepseek-flash, deepseek-v4-flash-inhouse-yd, deepseek-v4-flash-vision-exp,
qwen3.8-flash, qwen3.8-max, qwen3.7-plus, glm-5.3-flash, glm-5.3, glm-5.2,
gemini-3.8-flash, gemini-3.7-flash, kimi-k3, kimi-k2.7-code,
claude-opus-5, claude-sonnet-5, grok-4.6-zen, grok-4.5-zen,
gpt-5.6-terra, gpt-5.6-luna, MiniMax-M3
```

排查"调用无输出"时的三步：

1. 刷新上面的白名单，确认 `config.toml` 里的 `model` 仍在清单内，不在就改。
2. 看 `workspace/runs/<node-id>/<kind>.jsonl` 里的 `turn.failed`，那里有原始的 Hub 报错原文。
3. 只看终端输出会误判——失败时终端可能完全是空的。

注意：请求 `deepseek-v4.1-flash` 时 Hub 上游返回的 `model` 字段可能是 `deepseek-v4-flash`，属正常映射。
计费返回 `aigw.cost`，币种为 `CNY`。

## 陷阱与根因记录

### 1. 缺 `[windows] sandbox = "unelevated"` 会导致 worker 静默变只读

**现象**：worker 明明由 runner 以 `--sandbox workspace-write` 启动，却报告 `sandbox_mode=read-only`，
所有命令在 `CreateProcess` 阶段被 `blocked by policy` 拒绝，无法写文件，节点变 `blocked`。

**根因**：codex 0.154.0 在 Windows 上需要 `[windows] sandbox = "unelevated"` 才能正常建立可写沙箱。
桌面版 Codex 的 `~/.codex/config.toml` 里本来就有这段，所以一直正常；新建的隔离 `CODEX_HOME`
如果漏掉这段，`workspace-write` 会退化成只读。

**修复**：在隔离 `config.toml` 加：

```toml
[windows]
sandbox = "unelevated"
```

### 2. 只读沙箱会忽略 provider 配置

runner 在 `read-only` 沙箱下会追加 `--ignore-user-config`（见 `api/app/runner.py:988`），
这会**忽略 `$CODEX_HOME/config.toml`**，于是自定义 provider 失效、verifier 会回落到默认 OpenAI 端点而失败。

因此必须设 `CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG=0`。

### 3. `codemaker run` 在非 TTY 下会挂住

`codemaker run` 起本地 server 并等待 stdin，在脚本/管道中直接调用会长时间不返回。
需要 `--format json`、把 stdin 重定向到空文件，并加硬超时。CLI 输出的 JSON 事件流形态为
`step_start` → `text` → `step_finish`（含 `tokens` 与 `cost`）。

### 4. PowerShell 下 `codex` 会解析成 `codex.ps1`

`Get-Command codex` 返回 `codex.ps1`，用 `Start-Process` 启动会报
`%1 不是有效的 Win32 应用程序`。脚本里应显式用 `codex.cmd`。

### 5. `Model metadata ... not found` 是无害警告

```
Model metadata for `deepseek-v4.1-flash` not found. Defaulting to fallback metadata
```

codex 的内置模型注册表里没有这些第三方模型名，属装饰性警告，不影响功能。
`model_context_window` / `model_max_output_tokens` 无法消除它。

### 6. 依赖 Hub GUI 常开

端口由 Hub 进程提供，Hub 关闭后该通道即不可用。`use_codemaker_hub.ps1` 的健康检查会提前告警。

## 验证

单次真实 planner 调用（便宜、快速）：

```powershell
. .\scripts\use_codemaker_hub.ps1
python .\scripts\verify_codemaker_hub_planner.py --timeout 180
```

检查项：`exit_code == 0`，`decision` / `leaf_certainty` 通过 `planner-decision.schema.json` 严格校验。

完整链路（planner + worker + verifier + acceptance）：

```powershell
. .\scripts\use_codemaker_hub.ps1
python .\scripts\real_codex_smoke.py --timeout 300 --constitution-runner real
```

通过标准：`ok: true`，`root_status: "done"`，`matching_files` 非空，`debug_paths_ok: true`。

本轮在 `deepseek-v4.1-flash` 下的实测结果：真实 planner 判定 `execute` / `leaf_certainty=300%`，
严格 JSON schema 通过；完整烟测 `ok: true`，root `done`，产出文件字节匹配。

## 回退

不启用脚本即可回到原状：新 shell 不设置这四个环境变量，工程就走 `~/.codex` 的默认配置。
`~/.codex` 全程未被修改。`workspace/codex-home/` 已加入 `.gitignore`。

## 为什么不会影响桌面版 Codex（实测）

两者是**两套独立安装，二进制不同**：

| | 路径 |
|---|---|
| 本工程调用的 CLI | `%APPDATA%\npm\node_modules\@openai\codex\...\vendor\x86_64-pc-windows-msvc\bin\codex.exe` |
| 桌面版 Codex | `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe app-server ...` |

隔离靠 `CODEX_HOME` 环境变量只注入给本工程派生的子进程，桌面版的 `app-server` 进程读的是它自己的配置，两者互不影响。

**清理进程时的注意点**：桌面版进程名同样是 `codex.exe`。要终止本工程残留时，**必须先看命令行**，
只杀带 `exec` 的 CLI 进程，**不要**动带 `app-server` 的桌面版进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name='codex.exe'" |
  Select-Object ProcessId,@{n='Kind';e={ if ($_.CommandLine -match 'app-server') { 'DESKTOP (keep)' } else { 'CLI (safe to kill)' } }}
```
