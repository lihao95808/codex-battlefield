param(
    # 任何 **OpenAI 兼容**端点都能用：官方、自建网关、本地代理都行。
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    # codex 用这个环境变量名去取 key。**只要求变量存在**（本地代理通常不校验内容）。
    [string]$EnvKey = "OPENAI_API_KEY",
    [string]$ApiKey = "local-placeholder",
    [string]$Model = "gpt-5-codex",
    [string]$ProviderId = "custom",
    [string]$ProviderName = "Custom OpenAI-compatible provider",
    [ValidateSet("responses", "chat")][string]$WireApi = "responses",
    [string]$ReasoningEffort = "medium",
    [int]$ContextWindow = 128000,
    # codex 的 Windows 沙箱档。留空 = 不写 [windows] 段，用 codex 自己的默认。
    # 有些主机上 codex 的受限令牌建不起来（实测报 CreateProcessAsUserW failed: 5），
    # 那种情况下要显式给 danger-full-access。
    [string]$WindowsSandbox = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# 隔离 CODEX_HOME 的落点必须与 `Settings.layout.project_dir` 一致，否则 runner
# 会去一个没有 provider 配置的目录里找 config.toml —— 表现是"节点全部 401"。
# 所以这里问 `state_paths.py` 要，而不是自己拼 "workspace"。
$statePaths = Join-Path $PSScriptRoot 'state_paths.py'
$venvPython = Join-Path $root 'api\.venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }

$projectDir = ''
if (Test-Path $statePaths) {
    try {
        # 只认**输出是否为空**，不看 $LASTEXITCODE。
        # 实测：`$x = (& native ... | Select-Object -First 1)` 之后 $LASTEXITCODE 会是
        # 陈旧的 -1，于是"校验退出码"把**成功**的结果当成失败丢掉。
        $projectDir = (& $python $statePaths --get project_dir 2>$null | Select-Object -First 1)
    } catch { $projectDir = '' }
}
if ($projectDir) { $projectDir = "$projectDir".Trim() }

if ($projectDir) {
    $codexHome = Join-Path $projectDir 'codex-home'
    Write-Host "隔离 CODEX_HOME（按当前绑定的工程）：$codexHome"
} else {
    # 未绑定工程时回退旧位置，与未绑定语义保持一致。
    $codexHome = Join-Path $root 'workspace\codex-home'
    Write-Host "未绑定工程，回退旧位置：$codexHome"
    Write-Host "  提示：先用 battlefield-project use <工程目录> 绑定，状态才会落到状态根。"
}

if (-not (Test-Path $codexHome)) {
    New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
}

$configPath = Join-Path $codexHome 'config.toml'
if ((Test-Path $configPath) -and -not $Force) {
    Write-Host ""
    Write-Host "已存在配置，未覆盖：$configPath"
    Write-Host "  （要覆盖请加 -Force）"
} else {
    $lines = @(
        "# 由 scripts/use_provider.ps1 生成。**不要手改**：改完再跑一次这个脚本会被覆盖。",
        "# 该文件放在隔离 CODEX_HOME 里，只影响本工具的节点进程，不影响你自己的 codex 配置。",
        "",
        "model = `"$Model`"",
        "model_provider = `"$ProviderId`"",
        "model_reasoning_effort = `"$ReasoningEffort`"",
        "model_context_window = $ContextWindow",
        "",
        "[model_providers.$ProviderId]",
        "name = `"$ProviderName`"",
        "base_url = `"$BaseUrl`"",
        "wire_api = `"$WireApi`"",
        "env_key = `"$EnvKey`""
    )
    if ($WindowsSandbox) {
        $lines += @("", "[windows]", "sandbox = `"$WindowsSandbox`"")
    }
    # 用 .NET 写，避免 PS 5.1 的 Out-File 加 BOM / 改行尾
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($configPath, (($lines -join "`n") + "`n"), $utf8NoBom)
    Write-Host "已写入 provider 配置：$configPath"
}

$env:CODEX_BATTLEFIELD_RUNNER = 'real'
$env:CODEX_BATTLEFIELD_CODEX_HOME = $codexHome
$env:CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG = '0'
# codex 要求 env_key 指名的变量**存在**（即使端点不校验它的值）。
# 这里导出，同时也让 runner 的 `_ensure_provider_env_key` 在无此变量时补占位值。
Set-Item -Path "Env:$EnvKey" -Value $ApiKey
Remove-Item Env:CODEX_BATTLEFIELD_CODEX_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT -ErrorAction SilentlyContinue

Write-Host "runner $env:CODEX_BATTLEFIELD_RUNNER"
Write-Host "home   $codexHome"
Write-Host "model  $Model  ($ReasoningEffort)"
Write-Host "provider $ProviderId -> $BaseUrl"
Write-Host ""
Write-Host "这些环境变量只在**当前 shell** 有效。要让 API/GUI 也用上，" -ForegroundColor Yellow
Write-Host "把 CODEX_BATTLEFIELD_CODEX_HOME 写进用户配置（battlefield-project set-state-home 那类），" -ForegroundColor Yellow
Write-Host "或在启动 API 的那个环境里设置它。" -ForegroundColor Yellow
