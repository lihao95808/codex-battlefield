param(
    # 任何 **OpenAI 兼容**端点都能用：官方、自建网关、本地代理都行。
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    # codex 用这个环境变量名去取 key。**只要求变量存在**（本地代理通常不校验内容）。
    [string]$EnvKey = "OPENAI_API_KEY",
    [string]$ApiKey = "local-placeholder",
    [string]$Model = "gpt-5-codex",
    [string]$ProviderId = "custom",
    [string]$ProviderName = "Custom OpenAI-compatible provider",
    # **只写 `responses`**：新版 codex 的 `wire_api` 已经没有 `chat` 这个取值，
    # 写了它 codex 会**整份配置加载失败**（实测 `codex doctor` 报 `error invalid data`），
    # 表现为所有节点起不来。真要用不支持 Responses 的端点，请装 0.80 那一档的旧 codex。
    [ValidateSet("responses", "chat")][string]$WireApi = "responses",
    [string]$ReasoningEffort = "medium",
    [int]$ContextWindow = 128000,
    # codex 的 Windows 沙箱档。留空 = 不写 [windows] 段，用 codex 自己的默认。
    # 有些主机上 codex 的受限令牌建不起来（实测报 CreateProcessAsUserW failed: 5），
    # 那种情况下要显式给 danger-full-access。
    [string]$WindowsSandbox = "",
    # 配置落点。留空 = 按 state_paths.py 解析（正常用法）。
    # 给值只用于**离线验证/多份配置对照**（`scripts/verify_provider_matrix.py`）：
    # 没有它就没法在一台机器上同时生成三种 provider 类别的配置而不互相覆盖。
    [string]$CodexHome = "",
    # 只写文件、**不碰环境变量、不打印启用提示**。给自动化用：
    # 普通交互用法需要那些变量，但自动化只想拿到配置内容。
    [switch]$NoEnv,
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

if ($CodexHome) {
    # 显式落点：只用于离线验证/多份配置对照，**跳过工程绑定解析**（也就不打印落点提示）。
    $codexHome = $CodexHome
} else {
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
}

if (-not (Test-Path $codexHome)) {
    New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
}

$configPath = Join-Path $codexHome 'config.toml'

if ($WireApi -eq 'chat') {
    # 不静默：这个取值在新版 codex 上会让整份配置加载失败（见 docs/provider-setup.md 陷阱 3.8）。
    Write-Warning "wire_api = `"chat`" 在新版 codex 上已不被接受：codex 会**整份配置加载失败**（实测报 error invalid data），所有节点都起不来。要用它请装 0.80 档的旧 codex。"
}

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

if ($NoEnv) {
    # 只写文件模式：环境变量一个都不动，也不打印"怎么让 API/GUI 用上"那几行提示。
    Write-Host "只写配置（-NoEnv）：未改动任何环境变量。"
    exit 0
}

$env:CODEX_BATTLEFIELD_RUNNER = 'real'
$env:CODEX_BATTLEFIELD_CODEX_HOME = $codexHome
$env:CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG = '0'
# codex 要求 env_key 指名的变量**存在**（即使端点不校验它的值）。
# 这里导出，同时也让 runner 的 `_ensure_provider_env_key` 在无此变量时补占位值。
# 已经存在的真实凭据**不覆盖**：覆盖用户凭据是静默的破坏。
if (-not (Test-Path "Env:$EnvKey")) {
    Set-Item -Path "Env:$EnvKey" -Value $ApiKey
}
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
