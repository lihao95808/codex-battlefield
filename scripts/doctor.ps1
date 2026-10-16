param(
    [int]$ApiPort = 8765,
    [int]$WebPort = 5173,
    # 输出机器可读的 JSON（给 CI 或诊断包用）。
    [switch]$Json
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param(
        [string]$Name,
        [ValidateSet("ok", "warn", "fail", "skip")][string]$Status,
        [string]$Detail,
        [string]$Fix = ""
    )
    $checks.Add([pscustomobject]@{ name = $Name; status = $Status; detail = $Detail; fix = $Fix })
}

# ---------- 1. Python / venv ----------
$venvPython = Join-Path $root "api\.venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $ver = "$(& $venvPython -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)".Trim()
    Add-Check "api/.venv" "ok" "Python $ver"
    # 依赖真的可导入吗（venv 存在但包装坏了也常见）
    $importOk = & $venvPython -c "import fastapi, sqlalchemy, uvicorn; print('ok')" 2>&1
    if ("$importOk".Trim() -eq "ok") {
        Add-Check "Python 依赖" "ok" "fastapi / sqlalchemy / uvicorn 可导入"
    } else {
        Add-Check "Python 依赖" "fail" "venv 在但关键依赖导入失败：$importOk" ".\scripts\setup.ps1 -Force"
    }
} else {
    Add-Check "api/.venv" "fail" "没有虚拟环境" ".\scripts\setup.ps1"
}

# ---------- 2. Node / Web 依赖 ----------
if (Get-Command node -ErrorAction SilentlyContinue) {
    $nodeVer = "$(& node --version 2>$null)".Trim()
    Add-Check "Node" "ok" $nodeVer
} else {
    Add-Check "Node" "warn" "没找到 node（只影响 Web 界面）" ".\scripts\setup.ps1 -SkipWeb 可只跑后端"
}
if (Test-Path (Join-Path $root "web\node_modules")) {
    Add-Check "web/node_modules" "ok" "已安装"
} else {
    Add-Check "web/node_modules" "warn" "没装 Web 依赖（-ApiOnly 仍可跑）" ".\scripts\setup.ps1"
}

# ---------- 3. 端口 ----------
foreach ($item in @(@{ p = $ApiPort; n = "API" }, @{ p = $WebPort; n = "Web" })) {
    $conn = Get-NetTCPConnection -LocalPort $item.p -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        Add-Check "端口 $($item.p)（$($item.n)）" "ok" "已被 pid=$($conn[0].OwningProcess) 监听"
    } else {
        Add-Check "端口 $($item.p)（$($item.n)）" "warn" "空闲（服务没在跑）" ".\scripts\start.ps1"
    }
}

# ---------- 4. 状态根可写 ----------
$stateHome = ""
try {
    $stateHome = "$(& $venvPython (Join-Path $root "scripts\state_paths.py") --get state_home 2>$null)".Trim()
} catch { }
if ($stateHome) {
    try {
        if (-not (Test-Path $stateHome)) { New-Item -ItemType Directory -Force -Path $stateHome | Out-Null }
        $probe = Join-Path $stateHome ".doctor-write-probe"
        Set-Content -Path $probe -Value "probe" -Encoding ASCII -ErrorAction Stop
        Remove-Item $probe -Force -ErrorAction SilentlyContinue
        Add-Check "状态根可写" "ok" $stateHome
    } catch {
        Add-Check "状态根可写" "fail" "写不进去：$stateHome（$($_.Exception.Message)）" "检查权限，或用 battlefield-project set-state-home 换位置"
    }
} else {
    Add-Check "状态根" "warn" "解析不出来（可能没装依赖）" ".\scripts\setup.ps1"
}

# ---------- 5. 工程绑定 ----------
$projectRoot = ""
try {
    $projectRoot = "$(& $venvPython (Join-Path $root "scripts\state_paths.py") --get project_root 2>$null)".Trim()
} catch { }
if ($projectRoot) {
    Add-Check "工程绑定" "ok" $projectRoot
} else {
    Add-Check "工程绑定" "warn" "还没绑定工程（界面会显示空状态）" "battlefield-project use <工程目录>"
}

# ---------- 6. provider 可达性（只在配了隔离 CODEX_HOME 时） ----------
$codexHome = $env:CODEX_BATTLEFIELD_CODEX_HOME
if (-not $codexHome) {
    Add-Check "provider 配置" "skip" "未设 CODEX_BATTLEFIELD_CODEX_HOME（走 mock 或你自己 ~/.codex）" ".\scripts\use_provider.ps1 ..."
} else {
    $cfg = Join-Path $codexHome "config.toml"
    if (Test-Path $cfg) {
        $base = (Select-String -Path $cfg -Pattern '^base_url\s*=\s*"([^"]+)"' | Select-Object -First 1)
        $envKey = (Select-String -Path $cfg -Pattern '^env_key\s*=\s*"([^"]+)"' | Select-Object -First 1)
        $baseUrl = if ($base) { $base.Matches[0].Groups[1].Value } else { "" }
        $keyName = if ($envKey) { $envKey.Matches[0].Groups[1].Value } else { "" }
        Add-Check "provider 配置" "ok" "$cfg  base_url=$baseUrl env_key=$keyName"
        # codex 要求该变量**存在**（值不重要）。缺了就提示——这是实测踩过的断链点。
        if ($keyName) {
            $present = [bool](Get-Item "Env:$keyName" -ErrorAction SilentlyContinue)
            if ($present) {
                Add-Check "provider 凭据变量" "ok" "$keyName 存在"
            } else {
                Add-Check "provider 凭据变量" "warn" "$keyName 不存在（runner 会补占位值，本地代理通常没关系）" "或在启动 API 的环境里设置它"
            }
        }
        # 本地端点探一下
        if ($baseUrl -match '^https?://(127\.0\.0\.1|localhost)') {
            $health = ($baseUrl -replace '/v1/?$', '') + "/health"
            try {
                $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 5
                Add-Check "本地 model 端点" "ok" "$health -> $($r.StatusCode)"
            } catch {
                Add-Check "本地 model 端点" "warn" "探不到 $health" "先把本地代理/网关起起来，再用真实 runner"
            }
        }
    } else {
        Add-Check "provider 配置" "warn" "$codexHome 里没有 config.toml" ".\scripts\use_provider.ps1 ..."
    }
}

# ---------- 7. 看门狗计划任务 ----------
$task = schtasks /Query /TN "CodexBattlefieldWatchdog" /FO LIST 2>$null
if ($LASTEXITCODE -eq 0 -and $task) {
    $last = ($task | Select-String -Pattern "Last Result" | Select-Object -First 1)
    Add-Check "看门狗计划任务" "ok" "已注册 $(if ($last) { "($($last.Line.Trim()))" })"
} else {
    Add-Check "看门狗计划任务" "warn" "未注册（树不会自动续跑/兜底）" "powershell -File scripts\register_battlefield_watchdog_task.ps1 -StateHome <状态根>"
}

# ---------- 8. git（worktree 隔离要用） ----------
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    Add-Check "git" "ok" "$($gitCmd.Source)"
} else {
    Add-Check "git" "warn" "PATH 上没有 git（worktree 隔离与检查点账本会失效）" "装 Git 并确保它在 PATH 上"
}

# ---------- 输出 ----------
if ($Json) {
    $summary = [pscustomobject]@{
        ok      = -not ($checks | Where-Object { $_.status -eq "fail" })
        checks  = $checks
        root    = $root
        version = (Get-Date -Format "s")
    }
    $summary | ConvertTo-Json -Depth 5
} else {
    Write-Host ""
    Write-Host "===== doctor =====" -ForegroundColor Cyan
    Write-Host "仓库：$root"
    Write-Host ""
    foreach ($c in $checks) {
        $color = switch ($c.status) { "ok" { "Green" } "warn" { "Yellow" } "fail" { "Red" } default { "DarkGray" } }
        $tag = $c.status.ToUpper()
        Write-Host ("  [{0,-4}] {1,-18} {2}" -f $tag, $c.name, $c.detail) -ForegroundColor $color
        if ($c.fix -and $c.status -ne "ok") {
            Write-Host ("         修：{0}" -f $c.fix) -ForegroundColor DarkGray
        }
    }
    $fails = ($checks | Where-Object { $_.status -eq "fail" }).Count
    $warns = ($checks | Where-Object { $_.status -eq "warn" }).Count
    Write-Host ""
    if ($fails) {
        Write-Host "$fails 项失败，$warns 项提醒。" -ForegroundColor Red
    } elseif ($warns) {
        Write-Host "没有失败，$warns 项提醒（多数不影响 mock 模式跑起来）。" -ForegroundColor Yellow
    } else {
        Write-Host "全部通过。" -ForegroundColor Green
    }
}

# 只有 fail 才非 0：warn 是"可选/未启用"，不该让 CI 红。
if ($checks | Where-Object { $_.status -eq "fail" }) { exit 1 }
exit 0
