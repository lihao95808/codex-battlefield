# Real-runner environment for Codex Battlefield.
#
# Three things are required, and each one was learned the hard way:
#
# 1. codex 必须在 PATH 上。战场用 `shutil.which("codex")` 解析 codex_bin，
#    解析不到时 _is_git_repo 那类探针会抛异常（已修）。
#
# 2. codex 必须是**较新**的版本。npm 上的 0.153.0（2026-09-03）不认识 gpt-6.1-sol，
#    会返回 400 "not supported when using Codex with a ChatGPT account" —— 这条消息
#    **是误导**：真实原因是客户端不认识该模型。`npm i -g @openai/codex@latest` 修复。
#
# 3. `[windows] sandbox` 必须是 `unelevated`。全局 config.toml 里的 `elevated`
#    与 worker 的 `--sandbox workspace-write` 组合会让 codex 进程创建 helper 失败：
#      Failed to create unified exec process: helper_unknown_error: setup refresh had errors
#    后果是 worker **既不能执行命令也不能写文件**，只会上报环境阻塞。
#    这里用一层 --config 覆盖，不改用户的全局配置。

$env:CODEX_BATTLEFIELD_RUNNER = 'real'
$env:CODEX_BATTLEFIELD_CODEX_MODEL = 'gpt-6.1-sol'
$env:CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT = 'max'

# codex 自带的 git 探测需要它；MinGit 是免安装副本
$mingit = 'D:\DOC\2026\9\3D-AI工具\tools\mingit\cmd'
if (Test-Path $mingit) { $env:PATH = "$mingit;$env:PATH" }

Write-Host "codex  : $((Get-Command codex -ErrorAction SilentlyContinue).Source)"
Write-Host "版本   : $(codex --version 2>&1 | Select-Object -First 1)"
Write-Host "model  : $env:CODEX_BATTLEFIELD_CODEX_MODEL  effort: $env:CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT"
Write-Host "git    : $((Get-Command git -ErrorAction SilentlyContinue).Source)"
