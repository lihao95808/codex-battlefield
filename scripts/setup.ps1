param(
    # 跳过 Web 依赖安装。只想起 API（配 mock runner 时界面用不到）时用。
    [switch]$SkipWeb,
    # 已经装过也重装一遍（venv 会复用，只是重跑 pip install）。
    [switch]$Force,
    # 只检查不安装（CI / 排障用）——有缺就打清单并以非 0 退出。
    [switch]$Check
)

$ErrorActionPreference = "Stop"

# 仓库根 = 本脚本的上一级。**不**用 "当前目录"，因为这个脚本经常被人
# 从别处调用（甚至双击运行），而双击时 $PWD 是脚本所在目录、命令行调用时
# 又是调用者的目录 —— 两者都能对上的只有 $PSScriptRoot。
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$results = New-Object System.Collections.Generic.List[object]
$problems = New-Object System.Collections.Generic.List[string]

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Fix = "")
    $results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail; Fix = $Fix })
    if ($Status -eq "缺") { $problems.Add("$Name`: $Detail$(if ($Fix) { "`n    修：$Fix" })") }
}

# ---------- 1. 找 Python ----------
# 判据来自 api/pyproject.toml 的 `requires-python = ">=3.12,<3.14"`。
# 为什么要**先读它**而不是把版本号抄在这一行：抄一份就是第二份真相，
# 改了 pyproject 而忘了改这里，脚本会兴致勃勃地装一个装不上的版本。
$requires = ""
$pyproject = Join-Path $root "api\pyproject.toml"
if (Test-Path $pyproject) {
    $m = Select-String -Path $pyproject -Pattern 'requires-python\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($m -and $m.Matches.Count) { $requires = $m.Matches[0].Groups[1].Value }
}
Write-Host "api/pyproject.toml requires-python: $requires" -ForegroundColor Cyan

$pythonExe = ""
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    # 优先 3.12；退而求其次 3.13。刻意**不**取 3.14（当前上限是 <3.14）。
    foreach ($candidate in @("3.12", "3.13")) {
        $found = & py -$candidate -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { $pythonExe = "$found".Trim(); break }
    }
}
if (-not $pythonExe) {
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if ($sys) { $pythonExe = $sys.Source }
}

if ($pythonExe) {
    $ver = (& $pythonExe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null)
    $ver = "$ver".Trim()
    $vparts = $ver.Split(".")
    $major = [int]$vparts[0]; $minor = [int]$vparts[1]
    # 与 pyproject 的区间一致：>=3.12,<3.14
    if ($major -eq 3 -and $minor -ge 12 -and $minor -lt 14) {
        Add-Result "Python" "OK" "$ver  ($pythonExe)"
    } else {
        Add-Result "Python" "缺" "找到 $ver，但本项目需要 3.12–3.13" `
            "装 Python 3.12（winget install Python.Python.3.12），或直接用 py -3.12 建 venv"
    }
} else {
    Add-Result "Python" "缺" "没找到 python / py 启动器" `
        "winget install Python.Python.3.12  然后重开一个终端"
}

# ---------- 2. Node 与包管理器 ----------
$nodeExe = Get-Command node -ErrorAction SilentlyContinue
if ($nodeExe) {
    $nodeVer = "$(& node --version 2>$null)".Trim().TrimStart("v")
    $nodeMajor = [int]($nodeVer.Split(".")[0])
    if ($nodeMajor -ge 20) {
        Add-Result "Node" "OK" "v$nodeVer"
    } else {
        Add-Result "Node" "缺" "v$nodeVer，需要 20+" "winget install OpenJS.NodeJS.LTS"
    }
} else {
    Add-Result "Node" "缺" "没找到 node（只影响 Web 界面；API 与 mock runner 不需要它）" `
        "winget install OpenJS.NodeJS.LTS，或用 -SkipWeb 只装后端"
}

$pm = ""
$pmArgs = @()
if (Get-Command pnpm -ErrorAction SilentlyContinue) {
    $pm = "pnpm"; $pmArgs = @("install")
    Add-Result "包管理器" "OK" "pnpm $(& pnpm --version 2>$null)"
} elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    $pm = "npm"; $pmArgs = @("install")
    Add-Result "包管理器" "OK" "npm $(& npm --version 2>$null)（仓库带 pnpm-lock.yaml，用 pnpm 更一致）"
} else {
    Add-Result "包管理器" "缺" "既没有 pnpm 也没有 npm" `
        "npm i -g pnpm  （需要先装 Node）"
}

# ---------- 3. 可选：Codex CLI ----------
$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($codex) {
    Add-Result "Codex CLI" "OK" "$($codex.Source)  （只有真实 runner 模式需要）"
} else {
    Add-Result "Codex CLI" "可选" "未安装 —— 不影响 mock 模式（默认），只是不能用真实 agent"
}

# ---------- 4. 汇总 ----------
Write-Host ""
Write-Host "===== 环境检查 =====" -ForegroundColor Cyan
foreach ($r in $results) {
    $color = switch ($r.Status) { "OK" { "Green" } "可选" { "DarkGray" } default { "Red" } }
    Write-Host ("  [{0,-4}] {1,-12} {2}" -f $r.Status, $r.Name, $r.Detail) -ForegroundColor $color
}

if ($problems.Count) {
    Write-Host ""
    Write-Host "需要先解决：" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "  - $p" -ForegroundColor Yellow }
}

if ($Check) {
    if ($problems.Count) { Write-Host "`n检查未通过。" -ForegroundColor Red; exit 1 }
    Write-Host "`n检查通过。" -ForegroundColor Green
    exit 0
}

if ($problems.Count) {
    Write-Host "`n先修上面的问题再重跑本脚本。" -ForegroundColor Red
    exit 1
}

# ---------- 5. 建 venv 并装依赖 ----------
$apiDir = Join-Path $root "api"
$venv = Join-Path $apiDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

if ($Force -or -not (Test-Path $venvPython)) {
    Write-Host "`n[1/3] 建 venv：$venv" -ForegroundColor Cyan
    # venv 必须建在 api/ 下 —— 这是**约定位置**：goal 模式的分离式 scheduler
    # 会按这个路径找解释器，换地方会让后台任务起不来。
    & $pythonExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { Write-Host "建 venv 失败" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "`n[1/3] venv 已存在，复用：$venv" -ForegroundColor DarkGray
}

Write-Host "[2/3] 安装 Python 依赖（含 dev）" -ForegroundColor Cyan
# **必须在 api/ 下跑 pip**：`pyproject.toml` 在那儿。从仓库根跑会报
# "does not appear to be a Python project" —— 而且那句话不会告诉你路径错了，
# 只说"这里不是 Python 项目"。实测踩到，这正是"一条命令装好"最容易断的地方。
Push-Location $apiDir
try {
    & $venvPython -m pip install -q -U pip
    if ($LASTEXITCODE -ne 0) { Write-Host "升级 pip 失败" -ForegroundColor Red; exit 1 }
    & $venvPython -m pip install -q -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { Write-Host "安装 Python 依赖失败" -ForegroundColor Red; exit 1 }
} finally {
    Pop-Location
}

if ($SkipWeb) {
    Write-Host "[3/3] 按要求跳过 Web 依赖（-SkipWeb）" -ForegroundColor DarkGray
} else {
    Write-Host "[3/3] 安装 Web 依赖（$pm）" -ForegroundColor Cyan
    Push-Location (Join-Path $root "web")
    try {
        & $pm @pmArgs
        if ($LASTEXITCODE -ne 0) { Write-Host "安装 Web 依赖失败" -ForegroundColor Red; exit 1 }
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "安装完成。" -ForegroundColor Green
Write-Host ""
Write-Host "下一步："
Write-Host "  .\scripts\start.ps1          # 起 API + 界面"
Write-Host "  .\scripts\doctor.ps1         # 排障自检"
Write-Host ""
Write-Host "想连真实模型（可选）："
Write-Host "  .\scripts\use_provider.ps1 -BaseUrl <你的端点>/v1 -EnvKey <KEY 名> -Model <模型>"
