param(
    [int]$ApiPort = 8765,
    [int]$WebPort = 5173,
    # 只起 API（界面用静态托管，或只想用接口时）。
    [switch]$ApiOnly,
    # 不起浏览器。
    [switch]$NoBrowser,
    # 跳过依赖自检（确定装好了、想快一点时用）。
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$stateRoot = Join-Path $root "workspace\logs"
if (-not (Test-Path $stateRoot)) { New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null }
$apiLog = Join-Path $stateRoot "api.out.log"
$apiErr = Join-Path $stateRoot "api.err.log"
$webLog = Join-Path $stateRoot "web.out.log"
$webErr = Join-Path $stateRoot "web.err.log"

function Test-Port {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Wait-Http {
    param([string]$Url, [int]$TimeoutSeconds = 60, [string]$Name)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 400) { return $true }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    Write-Host "  等不到 $Name（$Url）——看日志：$apiErr" -ForegroundColor Red
    return $false
}

# ---------- 自检 ----------
$venvPython = Join-Path $root "api\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "还没装好：找不到 $venvPython" -ForegroundColor Red
    Write-Host "先跑：.\scripts\setup.ps1" -ForegroundColor Yellow
    exit 1
}

if (-not $SkipChecks -and -not $ApiOnly) {
    $webModules = Join-Path $root "web\node_modules"
    if (-not (Test-Path $webModules)) {
        Write-Host "还没装好：找不到 web\node_modules" -ForegroundColor Red
        Write-Host "先跑：.\scripts\setup.ps1    （或加 -ApiOnly 只起后端）" -ForegroundColor Yellow
        exit 1
    }
    $pm = if (Get-Command pnpm -ErrorAction SilentlyContinue) { "pnpm" } elseif (Get-Command npm -ErrorAction SilentlyContinue) { "npm" } else { "" }
    if (-not $pm) {
        Write-Host "既没有 pnpm 也没有 npm；加 -ApiOnly 可只起后端" -ForegroundColor Red
        exit 1
    }
}

# ---------- 幂等：已经在跑就别再起一遍 ----------
$apiAlready = Test-Port $ApiPort
$webAlready = Test-Port $WebPort

if ($apiAlready -and ($ApiOnly -or $webAlready)) {
    Write-Host "服务似乎已经在跑（API=$ApiPort Web=$WebPort）；直接打开界面。" -ForegroundColor Yellow
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$WebPort/" }
    exit 0
}

$procs = New-Object System.Collections.Generic.List[object]

function Stop-All {
    foreach ($p in $procs) {
        try {
            if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
        } catch { }
    }
    $procs.Clear()
}

try {
    # ---------- 起 API ----------
    if ($apiAlready) {
        Write-Host "API 已在 $ApiPort 上跑着，复用。" -ForegroundColor DarkGray
    } else {
        Write-Host "起 API（$ApiPort）…" -ForegroundColor Cyan
        $apiProc = Start-Process -FilePath $venvPython `
            -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
            -WorkingDirectory (Join-Path $root "api") `
            -RedirectStandardOutput $apiLog -RedirectStandardError $apiErr `
            -PassThru -NoNewWindow
        $procs.Add($apiProc)
    }

    if (-not (Wait-Http -Url "http://127.0.0.1:$ApiPort/api/health" -Name "API" -TimeoutSeconds 60)) {
        Write-Host "API 没起来。最后几行错误：" -ForegroundColor Red
        if (Test-Path $apiErr) { Get-Content $apiErr -Tail 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkRed } }
        Stop-All
        exit 1
    }
    Write-Host "  API OK  http://127.0.0.1:$ApiPort" -ForegroundColor Green

    # ---------- 起 Web ----------
    if ($ApiOnly) {
        Write-Host "按要求只起 API（-ApiOnly）。" -ForegroundColor DarkGray
        Write-Host "`n界面地址（若有静态托管）：http://127.0.0.1:$ApiPort"
    } else {
        if ($webAlready) {
            Write-Host "Web 已在 $WebPort 上跑着，复用。" -ForegroundColor DarkGray
        } else {
            Write-Host "起 Web（$WebPort）…" -ForegroundColor Cyan
            $pmExe = if (Get-Command pnpm -ErrorAction SilentlyContinue) { "pnpm" } else { "npm" }
            $webProc = Start-Process -FilePath $pmExe `
                -ArgumentList @("dev", "--host", "127.0.0.1", "--port", "$WebPort") `
                -WorkingDirectory (Join-Path $root "web") `
                -RedirectStandardOutput $webLog -RedirectStandardError $webErr `
                -PassThru -NoNewWindow
            $procs.Add($webProc)
        }

        if (-not (Wait-Http -Url "http://127.0.0.1:$WebPort/" -Name "Web" -TimeoutSeconds 90)) {
            Write-Host "Web 没起来。最后几行错误：" -ForegroundColor Red
            if (Test-Path $webErr) { Get-Content $webErr -Tail 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkRed } }
            Stop-All
            exit 1
        }
        Write-Host "  Web OK  http://127.0.0.1:$WebPort" -ForegroundColor Green

        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:$WebPort/"
            Write-Host "已打开浏览器。" -ForegroundColor Green
        } else {
            Write-Host "`n界面地址：http://127.0.0.1:$WebPort"
        }
    }

    Write-Host ""
    Write-Host "按 Ctrl+C 停止本脚本启动的进程。" -ForegroundColor DarkGray
    Write-Host "（已经在跑、被本脚本复用的服务不会被停掉。）" -ForegroundColor DarkGray

    # 保持前台：Ctrl+C 时 finally 会收尾。不等子进程退出——
    # 目标就是让用户能用 Ctrl+C 一次收干净。
    while ($true) {
        Start-Sleep -Seconds 2
        foreach ($p in @($procs)) {
            if ($p -and $p.HasExited) {
                Write-Host "有服务退出了（pid=$($p.Id)）。看日志：$apiErr / $webErr" -ForegroundColor Yellow
                Stop-All
                exit 1
            }
        }
    }
} finally {
    Stop-All
}
