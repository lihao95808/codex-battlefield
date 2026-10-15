param(
    # 便捷包装：常见场景是"本机跑着一个 OpenAI 兼容的代理"（网关、Hub、自建转发都算），
    # 它在某个本地端口上暴露 `/v1`，且**不校验 key**。
    # 通用逻辑全在 `use_provider.ps1`（那个不认识任何具体厂商）。
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$EnvKey = "OPENAI_API_KEY",
    [string]$ApiKey = "local-placeholder",
    [string]$Model = "gpt-5-codex",
    [string]$ProviderId = "localproxy",
    [string]$ProviderName = "Local OpenAI-compatible proxy",
    [ValidateSet("responses", "chat")][string]$WireApi = "chat",
    [string]$ReasoningEffort = "medium",
    [int]$ContextWindow = 128000,
    # 某些主机上 codex 的 Windows 受限令牌建不起来（报 CreateProcessAsUserW failed: 5），
    # 需要显式降级。留空 = 不写 [windows] 段。
    [string]$WindowsSandbox = "",
    # 可选：代理把实际监听端口写在这里（可能因端口占用而换号）。
    [string]$PortFile = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

# 端口来源：显式 -PortFile > -Port。代理常会把实际端口写进一个文件，
# 因为端口占用时它会换号 —— 用错端口的表现是"节点全部连不上"。
if ($PortFile -and (Test-Path $PortFile)) {
    $raw = (Get-Content $PortFile -Raw).Trim()
    if ($raw -match '^\d+$') { $Port = [int]$raw }
}

& (Join-Path $PSScriptRoot 'use_provider.ps1') `
    -BaseUrl "http://127.0.0.1:$Port/v1" `
    -EnvKey $EnvKey `
    -ApiKey $ApiKey `
    -Model $Model `
    -ProviderId $ProviderId `
    -ProviderName $ProviderName `
    -WireApi $WireApi `
    -ReasoningEffort $ReasoningEffort `
    -ContextWindow $ContextWindow `
    -WindowsSandbox $WindowsSandbox `
    -Force:$Force

# 尽力探一下健康检查，**不拦流程**：这个脚本的职责是写配置，
# 探不到只说明代理没起，不影响"配置已经就绪"这个事实。
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    Write-Host "proxy  OK  http://127.0.0.1:$Port ($($health.status))"
} catch {
    Write-Warning "探测不到本地代理（http://127.0.0.1:$Port/health）。先把代理起起来再用真实 runner。"
}
