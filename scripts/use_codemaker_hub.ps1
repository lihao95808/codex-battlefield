param([switch]$Force)

$root = Split-Path -Parent $PSScriptRoot
$codexHome = Join-Path $root 'workspace\codex-home'

if (-not (Test-Path $codexHome)) {
  New-Item -ItemType Directory -Force -Path $codexHome | Out-Null
}

$portFile = Join-Path $env:USERPROFILE '.codemaker-hub\proxy.port'
$port = '15721'
if (Test-Path $portFile) {
  $raw = (Get-Content $portFile -Raw).Trim()
  if ($raw -match '^\d+$') { $port = $raw }
}

$configPath = Join-Path $codexHome 'config.toml'
if ($Force -or -not (Test-Path $configPath)) {
  $template = Get-Content (Join-Path $PSScriptRoot 'codemaker-hub.codex.toml') -Raw
  Set-Content -Path $configPath -Value $template.Replace('__PORT__', $port) -Encoding utf8
}

$env:CODEX_BATTLEFIELD_RUNNER = 'real'
$env:CODEX_BATTLEFIELD_CODEX_HOME = $codexHome
$env:CMHUB_API_KEY = 'hub-local'
$env:CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG = '0'
Remove-Item Env:CODEX_BATTLEFIELD_CODEX_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT -ErrorAction SilentlyContinue

try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 5
  Write-Host "hub    OK  http://127.0.0.1:$port ($($health.status))"
} catch {
  Write-Warning "CodeMaker Hub unreachable on port $port. Start the Hub GUI first."
}

Write-Host "runner $env:CODEX_BATTLEFIELD_RUNNER"
Write-Host "home   $codexHome"
Write-Host "model  $((Select-String -Path $configPath -Pattern '^model = ').Line)"
