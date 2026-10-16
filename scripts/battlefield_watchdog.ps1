param(
    [int]$ApiPort = 8765,
    [int]$WebPort = 5173,
    [string]$RootId = "",
    [int]$MaxConcurrency = 13,
    [int]$AgentBurstLimit = 8,
    [int]$AgentBurstReserve = 1,
    [int]$ResumeAfterSeconds = 18000,
    [int]$CodexPreflightTimeoutSeconds = 25,
    [int]$SafeMinAvailableMemoryMB = 4096,
    [int]$MaxCodexProcessTrees = 0,
    [int]$MaxCodexResidentMB = 32768,
    [int]$CompletedCodexGraceSeconds = 120,
    [int]$MachinePressureResumeAfterSeconds = 1800,
    [int]$AutoConcurrencyQueueThreshold = 6,
    [int]$FuelDeadlineHour = 20,
    [double]$FuelTimezoneOffsetHours = 8.0,
    [int]$FuelSprintWindowMinutes = 120,
    [int]$WatchdogStaleAfterSeconds = 60,
    # 本次 pass 的调度间隔。看门狗自己**不**用它做任何调度（重复由计划任务的
    # 触发器负责），它只把这个数字写进心跳，好让观测方算出"多久没心跳算停摆"。
    # 默认 1 分钟 = 注册脚本的默认间隔。
    [int]$IntervalMinutes = 1,
    [string]$CodexHome = "",
    [string]$CodexProfile = "",
    [string]$StateHome = "",
    [switch]$GoalMode,
    [switch]$SkipWeb,
    [switch]$SkipCodexPreflight,
    [switch]$DisableAutoDiscover,
    [switch]$FixedConcurrency
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ApiUrl = "http://127.0.0.1:$ApiPort"
$WebUrl = "http://127.0.0.1:$WebPort"
$WorkspaceDir = Join-Path $RepoRoot "workspace"

# 状态根解析（`docs/spec-state-project-decoupling.md` 11-H4）
# 本脚本是独立的计划任务进程：调度注册不传 CODEX_BATTLEFIELD_* 环境变量，
# 只传命令行参数。所以状态根必须能靠 -StateHome 传进来，环境变量只是次选。
#
# **优先级必须与 Python 侧一致**，否则两边读写不同的目录，而表现是静默错位：
# 看门狗写了心跳、API 在另一个目录找，于是"看门狗好像没在跑"。
# Python 的顺序是 环境变量 > 用户配置文件(`~/.codex-battlefield.toml`) > 内置默认；
# 这里**不再自己实现一遍**（自己实现就是第二份真相，实测已经漂移过一次：
# 配置把状态根指到 C:\work\.codex-battlefield，而 ps1 落到了
# %USERPROFILE%\.codex-battlefield）。改为：把三个候选交给解析器，
# 解析器算完再问它"最终用的是哪个"。
$DefaultStateHome = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".codex-battlefield"
if ($StateHome) {
    $StateHomeResolved = $StateHome
} elseif ($env:CODEX_BATTLEFIELD_STATE_HOME) {
    $StateHomeResolved = $env:CODEX_BATTLEFIELD_STATE_HOME
} else {
    $StateHomeResolved = $DefaultStateHome
}

# 机器级路径的解析器（提前定义，因为下面要先用它纠偏状态根）。
$StatePathsScript = Join-Path $PSScriptRoot "state_paths.py"

function Resolve-RepoPython {
    $candidates = @(
        (Join-Path $RepoRoot "api\.venv\Scripts\python.exe"),
        (Join-Path $RepoRoot ".venv\Scripts\python.exe"),
        (Join-Path $RepoRoot "api\.venv\bin\python"),
        (Join-Path $RepoRoot ".venv\bin\python")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return "python"
}

# 纠偏：**只有**在调用方没显式给 -StateHome 时才问解析器。
# 显式给的值永远优先 —— 计划任务注册时会传，那是运维的明确意图，不该被推翻。
if (-not $StateHome -and (Test-Path -LiteralPath $StatePathsScript)) {
    try {
        # 注意这里**不传** --state-home：传了就等于把上面的猜测当成答案，
        # 解析器就没机会去看用户配置文件了（那正是要修的 bug）。
        $canonical = & (Resolve-RepoPython) $StatePathsScript --get state_home 2>$null
        if ($LASTEXITCODE -eq 0 -and "$canonical".Trim()) {
            $StateHomeResolved = "$canonical".Trim()
        }
    } catch {
        # 拿不到就用上面的猜测，并在日志里留痕（日志路径依赖状态根，所以只能延后写）
        $script:StateHomeResolveWarning = "state_home resolution failed: $($_.Exception.Message)"
    }
}

$WatchdogLockPath = Join-Path $WorkspaceDir "watchdog.lock.json"

# 状态路径解析器：路径的唯一真相来源。ps1 不自己拼工程 key 与目录名，
# 否则 Python 与 ps1 会各自漂移，而漂移的表现是
# "看门狗读写的地方 API 已经不看了" —— 静默错位，只在出问题时暴露。
# （`$StatePathsScript` 与 `Resolve-RepoPython` 在上面解析状态根时已定义。）

# 问解析器要一个路径。拿不到返回空串（不抛）。
# 解析器约定：机器级 key 恒可用；每工程 key 在未绑定工程时以退出码 3 失败。
function Get-StatePath {
    param([string]$Key)
    if (-not (Test-Path -LiteralPath $StatePathsScript)) { return "" }
    $python = Resolve-RepoPython
    try {
        $value = & $python $StatePathsScript --get $Key --state-home $StateHomeResolved 2>$null
        if ($LASTEXITCODE -ne 0) { return "" }
        return ("$value").Trim()
    } catch {
        return ""
    }
}

# 机器级状态文件的落点：优先新状态根，回退旧工作区。
# 为什么两边都看而不是直接切：迁移是分步做的。看门狗先切而 API 还在写旧位置，
# 就会出现"看门狗读的地方 API 没在写"。RequireExists 为真时只在文件真存在时
# 才采用新路径，避免文件还没迁过来时读到一个空的新路径、把旧的真实数据丢掉。
function Resolve-MachineStatePath {
    param([string]$FileName, [string]$Key, [switch]$RequireExists)
    $newPath = Get-StatePath -Key $Key
    if ($newPath) {
        if (-not $RequireExists -or (Test-Path -LiteralPath $newPath)) {
            return $newPath
        }
    }
    return (Join-Path $WorkspaceDir $FileName)
}

# 每工程状态的落点（库 / runs 等）。未绑定工程时解析器失败，这里回退旧工作区，
# 保住"什么都没绑也能跑"。
function Resolve-ProjectStatePath {
    param([string]$Key, [string]$LegacyName)
    $resolved = Get-StatePath -Key $Key
    if ($resolved) { return $resolved }
    return (Join-Path $WorkspaceDir $LegacyName)
}

$LogPath = Resolve-MachineStatePath -FileName "watchdog.log" -Key "watchdog_log"
$CodexHealthPath = Resolve-MachineStatePath -FileName "codex_health.json" -Key "codex_health"
$MachineSafetyPath = Resolve-MachineStatePath -FileName "machine_safety.json" -Key "machine_safety"
# 心跳落在**状态根**（机器级），与 codex_health / machine_safety 同处 ——
# 这些是"跨工程共享的机器事实"，不属于某个工程。
$WatchdogStatusPath = Resolve-MachineStatePath -FileName "watchdog_status.json" -Key "watchdog_status"
$script:WatchdogStartedAt = (Get-Date).ToUniversalTime()

$ApiRestartFlagPath = Join-Path $WorkspaceDir "restart_api_when_idle.flag"
$ApiRestartDrainFlagPath = Join-Path $WorkspaceDir "restart_api_drain_when_safe.flag"
$ApiRestartDeferredFlagPath = Join-Path $WorkspaceDir "restart_api_deferred_by_fuel.flag"
$ApiRestartLocalDrainPath = Join-Path $WorkspaceDir "restart_api_local_drain.json"
$LocalKickDrainPath = Join-Path $WorkspaceDir "local_kick_drain.json"
$script:WatchdogLockOwned = $false

if ($CodexHome) {
    $env:CODEX_HOME = "$CodexHome"
    $env:CODEX_BATTLEFIELD_CODEX_HOME = "$CodexHome"
} else {
    Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_HOME -ErrorAction SilentlyContinue
}

if ($GoalMode) {
    $AgentBurstLimit = 0
    $AgentBurstReserve = 0
}

if ($MaxCodexProcessTrees -le 0) {
    $MaxCodexProcessTrees = [Math]::Max(1, $MaxConcurrency)
}

if ($MachinePressureResumeAfterSeconds -le 0) {
    $MachinePressureResumeAfterSeconds = [Math]::Min($ResumeAfterSeconds, 1800)
}

function Write-WatchdogLog {
    param([string]$Message)
    if (-not (Test-Path $WorkspaceDir)) {
        New-Item -ItemType Directory -Path $WorkspaceDir -Force | Out-Null
    }
    # 轮转：原先 Add-Content 没有任何上限，24/7 长跑几个月就积成几十上百 MB。
    # 只保留一份上一代（.1），够事后查；失败也不拦日志写入——日志写不下去比日志被截断更糟。
    try {
        if (Test-Path $LogPath) {
            $existing = Get-Item -LiteralPath $LogPath -ErrorAction Stop
            if ($existing.Length -gt 8MB) {
                $previous = "$LogPath.1"
                if (Test-Path $previous) { Remove-Item -LiteralPath $previous -Force -ErrorAction SilentlyContinue }
                Move-Item -LiteralPath $LogPath -Destination $previous -Force
            }
        }
    } catch {
        # 轮转失败不影响本次写入
    }
    $line = "{0} {1}" -f (Get-Date).ToString("s"), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
    # Keep log messages out of the success pipeline; several watchdog helpers
    # return booleans, and Write-Output would make `$false` look truthy to `if`.
    Write-Host $Message
}

# 状态根纠偏失败要留痕。放在这里是因为它只能用 `Write-WatchdogLog`，
# 而那个函数必须先定义 —— 在上面直接写会"命令未找到"。
if ($script:StateHomeResolveWarning) {
    Write-WatchdogLog $script:StateHomeResolveWarning
    $script:StateHomeResolveWarning = ""
}

function Write-JsonNoBom {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Depth = 6
    )
    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $json = $Value | ConvertTo-Json -Depth $Depth
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $json, $utf8NoBom)
}

function Enter-WatchdogSingleInstance {
    if (-not (Test-Path $WorkspaceDir)) {
        New-Item -ItemType Directory -Path $WorkspaceDir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $WatchdogLockPath) {
        try {
            $existing = Get-Content -LiteralPath $WatchdogLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
            $existingPid = [int]$existing.pid
            if ($existingPid -gt 0 -and (Test-WatchdogProcessActive -ProcessId $existingPid)) {
                if (Test-StaleWatchdogShell -ProcessId $existingPid -StartedAt $existing.started_at) {
                    Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
                    Remove-Item -LiteralPath $WatchdogLockPath -Force -ErrorAction SilentlyContinue
                    Write-WatchdogLog "stale watchdog shell reclaimed pid=$existingPid stale_after_seconds=$WatchdogStaleAfterSeconds"
                } else {
                    # **持锁过久也当失效**（`docs/spec-watchdog-steward.md` §2.5）。
                    #
                    # 只看"进程还活着"有个致命缺口：一个**卡住**的 powershell
                    # 进程是活的、也持有锁，于是之后每一次调度都走这条分支、
                    # 直接 exit 0 —— 连心跳都不会写。而观测方靠心跳判断停摆，
                    # 心跳不写就永远判不出来：**看门狗已经死了，而告警是哑的。**
                    #
                    # 宽限比 `Test-StaleWatchdogShell` 的 60 秒大得多：那个是
                    # "一次正常 pass 该多久"，这个是"卡死"该多久才算数。
                    $lockAgeSeconds = $null
                    if ($existing.started_at) {
                        try {
                            $lockStarted = [datetime]::Parse("$($existing.started_at)").ToUniversalTime()
                            $lockAgeSeconds = ((Get-Date).ToUniversalTime() - $lockStarted).TotalSeconds
                        } catch {
                            $lockAgeSeconds = $null
                        }
                    }
                    $hardLimit = [Math]::Max(600, $WatchdogStaleAfterSeconds * 10)
                    if ($null -ne $lockAgeSeconds -and $lockAgeSeconds -gt $hardLimit) {
                        Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
                        Remove-Item -LiteralPath $WatchdogLockPath -Force -ErrorAction SilentlyContinue
                        Write-WatchdogLog "watchdog lock held too long pid=$existingPid age=$([int]$lockAgeSeconds)s limit=${hardLimit}s; reclaimed and continuing"
                    } else {
                        # 正常重叠：另一个 pass 正在跑。**也要写心跳**，
                        # 否则"频繁重叠"在观测方看来和"停摆"一模一样。
                        Write-WatchdogLog "watchdog already running pid=$existingPid; skipping overlapping run"
                        Write-WatchdogHeartbeat -Status "overlapped" -Reason "another watchdog pass holds the lock"
                        exit 0
                    }
                }
            }
        } catch {
        }
    }
    $payload = [ordered]@{
        pid = $PID
        started_at = (Get-Date).ToUniversalTime().ToString("o")
        root_id = $RootId
    }
    Write-JsonNoBom -Value $payload -Path $WatchdogLockPath -Depth 4
    $script:WatchdogLockOwned = $true
}

function Test-WatchdogProcessActive {
    param([int]$ProcessId)

    if ($ProcessId -le 0) {
        return $false
    }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
    } catch {
        return $false
    }
    if ($null -eq $process) {
        return $false
    }
    return (
        $process.Name -eq "powershell.exe" -and
        [string]$process.CommandLine -match "battlefield_watchdog\.ps1"
    )
}

function Test-StaleWatchdogShell {
    param(
        [int]$ProcessId,
        [object]$StartedAt
    )

    if ($ProcessId -le 0 -or $WatchdogStaleAfterSeconds -le 0) {
        return $false
    }
    try {
        $started = [datetimeoffset]::Parse([string]$StartedAt)
        $ageSeconds = ((Get-Date).ToUniversalTime() - $started.UtcDateTime).TotalSeconds
        if ($ageSeconds -lt $WatchdogStaleAfterSeconds) {
            return $false
        }
    } catch {
        return $false
    }
    try {
        $children = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.ParentProcessId -eq $ProcessId })
    } catch {
        return $false
    }
    $meaningfulChildren = @(
        $children | Where-Object {
            $commandLine = [string]$_.CommandLine
            $isApiChild = (
                $_.Name -eq "python.exe" -and
                $commandLine -match "uvicorn app\.main:app" -and
                $commandLine -match "--port\s+$ApiPort"
            )
            $_.Name -ne "conhost.exe" -and
            $_.Name -ne "OpenConsole.exe" -and
            -not $isApiChild
        }
    )
    return ($meaningfulChildren.Count -eq 0)
}

function Write-WatchdogHeartbeat {
    <#
    写心跳 / 健康（`docs/spec-watchdog-steward.md`）。

    为什么需要它：看门狗的失败模式**不是报错，而是静默停摆** ——
    计划任务被禁用、shell 崩了、脚本卡住。那时树在无人看管地跑，而没有任何
    地方会报错。原先唯一的痕迹是 `watchdog.lock.json`（只在一次 pass 期间存在）
    和 `watchdog.log` 的 mtime（API 不读），所以"看门狗还活着吗"根本无法回答。

    由 `Exit-WatchdogSingleInstance` 调用 —— 那条路径**每次退出都会走**
    （正常、异常、提前 return），所以心跳不会因为某条分支忘了写而缺失。
    这一点是刻意的：把"必写"挂在唯一的退出点上，而不是散在每条 return 前。
    #>
    param(
        [string]$Status = "completed",
        [string]$Reason = "",
        [string]$EffectiveRootId = ""
    )

    $startedAt = $script:WatchdogStartedAt
    $nowUtc = (Get-Date).ToUniversalTime()
    $durationSeconds = $null
    if ($startedAt) {
        $durationSeconds = [Math]::Round(($nowUtc - $startedAt).TotalSeconds, 1)
    }

    $payload = [ordered]@{
        # 心跳的**判据**：`finished_at` 是"上一次完整跑完"的时间。
        # 观测方（API / 管家）用它对比 pass 间隔来判断是否停摆。
        started_at        = if ($startedAt) { $startedAt.ToString("o") } else { $null }
        finished_at       = $nowUtc.ToString("o")
        status            = $Status
        reason            = $Reason
        duration_seconds  = $durationSeconds
        pid               = $PID
        root_id           = $EffectiveRootId
        api_url           = $ApiUrl
        state_home        = $StateHomeResolved
        # 观测方拿它算"多久算停摆"，避免把阈值硬编码在两边（两边不一致的话，
        # 一边认为正常、另一边报警，是最难查的一类不一致）。
        stale_after_seconds = $WatchdogStaleAfterSeconds
        interval_seconds  = ([Math]::Max(1, $IntervalMinutes) * 60)
        repo_root         = $RepoRoot
    }

    try {
        Write-JsonNoBom -Value $payload -Path $WatchdogStatusPath -Depth 5
    } catch {
        # 心跳写失败**不能**让看门狗本身崩掉：它还有正事要干。
        # 但要在日志里留痕 —— 否则"心跳没了"和"看门狗没跑"就分不清了。
        Write-WatchdogLog "heartbeat write failed: $($_.Exception.Message)"
    }
}

function Exit-WatchdogSingleInstance {
    if (-not $script:WatchdogLockOwned) {
        return
    }
    Write-WatchdogHeartbeat
    try {
        if (Test-Path -LiteralPath $WatchdogLockPath) {
            $existing = Get-Content -LiteralPath $WatchdogLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$existing.pid -eq $PID) {
                Remove-Item -LiteralPath $WatchdogLockPath -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
    } finally {
        $script:WatchdogLockOwned = $false
    }
}

Enter-WatchdogSingleInstance

function Invoke-ExternalCommandWithTimeout {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [int]$TimeoutSeconds
    )

    $timeout = [Math]::Max(1, $TimeoutSeconds)
    $prefix = "codex-battlefield-{0}" -f ([guid]::NewGuid().ToString("N"))
    $stdoutPath = Join-Path ([IO.Path]::GetTempPath()) "$prefix.out"
    $stderrPath = Join-Path ([IO.Path]::GetTempPath()) "$prefix.err"
    $timedOut = $false
    $exitCode = $null
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $Arguments `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -WindowStyle Hidden
        # Windows PowerShell may leave ExitCode unset for Start-Process
        # -PassThru unless the process handle is opened before waiting.
        $null = $process.Handle
        if (-not $process.WaitForExit($timeout * 1000)) {
            $timedOut = $true
            try {
                Stop-Process -Id $process.Id -Force
            } catch {
            }
            $exitCode = 124
        } else {
            $process.Refresh()
            $process.WaitForExit()
            $process.Refresh()
            $exitCode = $process.ExitCode
            if ($null -eq $exitCode -and $process.HasExited) {
                $exitCode = 1
            }
        }
        $stdout = if (Test-Path $stdoutPath) { [string](Get-Content -Path $stdoutPath -Raw -Encoding UTF8) } else { "" }
        $stderr = if (Test-Path $stderrPath) { [string](Get-Content -Path $stderrPath -Raw -Encoding UTF8) } else { "" }
        return [pscustomobject]@{
            exit_code = $exitCode
            timed_out = $timedOut
            stdout = $stdout
            stderr = $stderr
        }
    } catch {
        return [pscustomobject]@{
            exit_code = $null
            timed_out = $false
            stdout = ""
            stderr = $_.Exception.Message
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -ErrorAction SilentlyContinue
    }
}

function Test-CodexResourceBackoffText {
    param([string]$Text)
    if (-not $Text) {
        return $false
    }
    $pattern = '(?i)((?<![\d.])429(?![\d.]|ms\b|milliseconds?\b)|quota|rate limit|too many requests|retry-after|retry after|try again (?:in|at)|available (?:in|at)|reset (?:in|at)|resets (?:in|at)|limit reached|usage limit|resource exhausted|insufficient_quota|限流|额度|余额|流量|稍后重试|等待.*重试)'
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line) {
            continue
        }
        if ((Test-CodexShellToolExitOutputLine $line) -and -not (Test-CodexStrongResourceBackoffText $line)) {
            continue
        }
        if ($line -match $pattern) {
            return $true
        }
    }
    return $false
}

function Test-CodexShellToolExitOutputLine {
    param([string]$Text)
    return ($Text -match 'codex_core::tools::router' -and $Text -match 'error=Exit code:')
}

function Test-CodexStrongResourceBackoffText {
    param([string]$Text)
    if (-not $Text) {
        return $false
    }
    return ($Text -match '(?i)(you(?:''| have|ve) hit your usage limit|too many requests|http status 429|"status"\s*:\s*429|insufficient_quota|resource exhausted|retry-after|retry after|try again (?:in|at)|available (?:in|at)|reset (?:in|at)|resets (?:in|at))')
}

function Get-CodexObservedLocalTimeFromText {
    param([string]$Text)
    if (-not $Text) {
        return $null
    }
    $match = [regex]::Match($Text, '(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)')
    if ($match.Success) {
        try {
            return ([datetimeoffset]::Parse($match.Groups[1].Value)).LocalDateTime
        } catch {
        }
    }
    return $null
}

function Get-CodexQuotaClockResetFromText {
    param([string]$Text)
    if (-not $Text) {
        return $null
    }
    $pattern = '(?i)(?:try again at|available at|reset at|resets at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?'
    $matches = [regex]::Matches($Text, $pattern)
    if ($matches.Count -eq 0) {
        return $null
    }
    $now = Get-Date
    $observed = Get-CodexObservedLocalTimeFromText -Text $Text
    $baseTime = if ($null -ne $observed) { $observed } else { $now }
    $bestFuture = $null
    $bestElapsed = $null
    foreach ($match in $matches) {
        $hour = [int]$match.Groups[1].Value
        $minute = 0
        if ($match.Groups[2].Success -and $match.Groups[2].Value) {
            $minute = [int]$match.Groups[2].Value
        }
        if ($hour -lt 0 -or $hour -gt 23 -or $minute -lt 0 -or $minute -gt 59) {
            continue
        }
        $suffix = ""
        if ($match.Groups[3].Success) {
            $suffix = $match.Groups[3].Value.ToLowerInvariant()
        }
        if ($suffix -eq "pm" -and $hour -lt 12) {
            $hour += 12
        } elseif ($suffix -eq "am" -and $hour -eq 12) {
            $hour = 0
        }
        try {
            $candidate = Get-Date -Year $baseTime.Year -Month $baseTime.Month -Day $baseTime.Day -Hour $hour -Minute $minute -Second 0
        } catch {
            continue
        }
        if ($null -ne $observed -and $candidate -lt $observed.AddMinutes(-2)) {
            $candidate = $candidate.AddDays(1)
        } elseif ($null -eq $observed -and $candidate -lt $now.AddHours(-12)) {
            $candidate = $candidate.AddDays(1)
        }
        if ($candidate -gt $now) {
            if ($null -eq $bestFuture -or $candidate -lt $bestFuture) {
                $bestFuture = $candidate
            }
        } else {
            if ($null -eq $bestElapsed -or $candidate -gt $bestElapsed) {
                $bestElapsed = $candidate
            }
        }
    }
    if ($null -ne $bestFuture) {
        return [pscustomobject]@{
            elapsed = $false
            retry_after_seconds = [Math]::Max(0, [int][Math]::Ceiling(($bestFuture - $now).TotalSeconds))
            reset_at = $bestFuture.ToUniversalTime().ToString("o")
        }
    }
    if ($null -ne $bestElapsed) {
        return [pscustomobject]@{
            elapsed = $true
            retry_after_seconds = 0
            reset_at = $bestElapsed.ToUniversalTime().ToString("o")
        }
    }
    return $null
}

function ConvertTo-TimeUnitSeconds {
    param([string]$Unit)
    $normalized = ""
    if ($Unit) {
        $normalized = $Unit.ToLowerInvariant()
    }
    if ($normalized -match '^(?:s|sec|secs|second|seconds|秒)$') {
        return 1
    }
    if ($normalized -match '^(?:m|min|mins|minute|minutes|分钟)$') {
        return 60
    }
    if ($normalized -match '^(?:h|hr|hrs|hour|hours|小时)$') {
        return 3600
    }
    return 0
}

function Get-CodexQuotaSignalFromText {
    param(
        [string]$Text,
        [string]$Source
    )

    $result = [ordered]@{
        quota_observability = "codex_cli_no_direct_quota_endpoint"
        quota_retry_after_seconds = $null
        quota_reset_at = $null
        quota_retry_after_source = ""
        quota_signal = ""
        quota_source = $Source
    }
    if (-not $Text) {
        return [pscustomobject]$result
    }

    $candidates = New-Object System.Collections.Generic.List[int]
    $patterns = @(
        '(?i)(?:retry-after|retry after|try again in|available in|reset in|resets in|wait(?: for)?|等待)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|秒|分钟|小时)',
        '(?i)(\d+(?:\.\d+)?)\s*(seconds?|secs?|sec|s|minutes?|mins?|min|m|hours?|hrs?|hr|h|秒|分钟|小时)\s*(?:later|后|之后|以后|until reset|to reset)'
    )
    foreach ($pattern in $patterns) {
        foreach ($match in [regex]::Matches($Text, $pattern)) {
            $amount = [double]$match.Groups[1].Value
            $unitSeconds = ConvertTo-TimeUnitSeconds -Unit $match.Groups[2].Value
            if ($unitSeconds -gt 0) {
                $seconds = [int][Math]::Round($amount * $unitSeconds)
                if ($seconds -gt 0) {
                    $candidates.Add($seconds)
                }
            }
        }
    }

    foreach ($match in [regex]::Matches($Text, '(?i)retry-after\s*[:=]\s*(\d+)')) {
        $seconds = [int]$match.Groups[1].Value
        if ($seconds -gt 0) {
            $candidates.Add($seconds)
        }
    }

    $clockReset = Get-CodexQuotaClockResetFromText -Text $Text
    if ($candidates.Count -gt 0) {
        $retryAfter = ($candidates | Measure-Object -Maximum).Maximum
        $retryAfter = [Math]::Max(60, [Math]::Min([int]$retryAfter, 604800))
        $result.quota_observability = "retry_after_observed_from_codex_output"
        $result.quota_retry_after_seconds = $retryAfter
        $result.quota_reset_at = (Get-Date).ToUniversalTime().AddSeconds($retryAfter).ToString("o")
        $result.quota_retry_after_source = "codex_output"
        $result.quota_signal = ConvertTo-ShortText $Text 400
    } elseif ($null -ne $clockReset) {
        if ([bool]$clockReset.elapsed) {
            $result.quota_observability = "retry_after_elapsed_from_codex_output"
            $result.quota_retry_after_seconds = 0
        } else {
            $result.quota_observability = "retry_after_observed_from_codex_output"
            $result.quota_retry_after_seconds = [Math]::Max(1, [int]$clockReset.retry_after_seconds)
        }
        $result.quota_reset_at = $clockReset.reset_at
        $result.quota_retry_after_source = "codex_output_clock_time"
        $result.quota_signal = ConvertTo-ShortText $Text 400
    } elseif (Test-CodexResourceBackoffText $Text) {
        $result.quota_observability = "backoff_observed_without_reset"
        $result.quota_retry_after_source = "codex_output_without_reset"
        $result.quota_signal = ConvertTo-ShortText $Text 400
    }
    return [pscustomobject]$result
}

function Merge-CodexQuotaSignal {
    param(
        [Parameter(Mandatory = $true)]$Health,
        [Parameter(Mandatory = $true)]$Signal
    )

    if (-not $Signal) {
        return
    }
    if ($Signal.quota_observability -and $Signal.quota_observability -ne "codex_cli_no_direct_quota_endpoint") {
        $Health["quota_observability"] = $Signal.quota_observability
        $Health["quota_retry_after_seconds"] = $Signal.quota_retry_after_seconds
        $Health["quota_reset_at"] = $Signal.quota_reset_at
        $Health["quota_retry_after_source"] = $Signal.quota_retry_after_source
        $Health["quota_signal"] = $Signal.quota_signal
        $Health["quota_source"] = $Signal.quota_source
    }
}

function ConvertTo-ShortText {
    param([string]$Text, [int]$MaxLength = 1200)
    if (-not $Text) {
        return ""
    }
    $normalized = ($Text -replace "\s+", " ").Trim()
    if ($normalized.Length -le $MaxLength) {
        return $normalized
    }
    return $normalized.Substring(0, $MaxLength)
}

function ConvertTo-WorkspaceRelativePath {
    param([string]$Path)
    if (-not $Path) {
        return ""
    }
    try {
        $fullPath = [System.IO.Path]::GetFullPath($Path)
        $workspaceFullPath = [System.IO.Path]::GetFullPath($WorkspaceDir)
        if ($fullPath.StartsWith($workspaceFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $fullPath.Substring($workspaceFullPath.Length).TrimStart("\", "/")
        }
    } catch {
    }
    return $Path
}

function Read-FileTailText {
    param(
        [string]$Path,
        [int]$MaxBytes = 65536
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    $stream = $null
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($item.Length -le 0) {
            return ""
        }
        $bytesToRead = [int][Math]::Min([int64]$MaxBytes, [int64]$item.Length)
        $buffer = New-Object byte[] $bytesToRead
        $stream = [System.IO.File]::Open($item.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        $stream.Seek(-1 * $bytesToRead, [System.IO.SeekOrigin]::End) | Out-Null
        $read = $stream.Read($buffer, 0, $bytesToRead)
        if ($read -le 0) {
            return ""
        }
        $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        if ($item.Length -gt $bytesToRead) {
            $newlineIndex = $text.IndexOf("`n")
            if ($newlineIndex -lt 0) {
                return ""
            }
            $text = $text.Substring($newlineIndex + 1)
        }
        return $text
    } catch {
        return ""
    } finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function New-CodexHealthRecord {
    param([string]$Reason = "runtime_log_observation")
    return [ordered]@{
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
        reason = $Reason
        status = "unknown"
        can_resume = $true
        codex_command = ""
        login_status = "unknown"
        doctor_status = "not_run"
        quota_observability = "codex_cli_has_no_direct_quota_or_5h_reset_api"
        quota_retry_after_seconds = $null
        quota_reset_at = $null
        quota_retry_after_source = ""
        quota_signal = ""
        quota_source = ""
        quota_observed_at = $null
        quota_root_id = ""
        health_parse_failed = $false
        summary = ""
        login_stdout = ""
        login_stderr = ""
        doctor_stdout = ""
        doctor_stderr = ""
    }
}

function Read-CodexHealthRecord {
    $health = New-CodexHealthRecord
    if (-not (Test-Path -LiteralPath $CodexHealthPath)) {
        return $health
    }
    try {
        $existing = Get-Content -LiteralPath $CodexHealthPath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($property in $existing.PSObject.Properties) {
            $health[$property.Name] = $property.Value
        }
    } catch {
        $health["health_parse_failed"] = $true
        $health["summary"] = "codex_health_parse_failed"
    }
    return $health
}

function Update-ExpiredCodexQuotaHealth {
    if (-not (Test-Path -LiteralPath $CodexHealthPath)) {
        return $false
    }
    $health = Read-CodexHealthRecord
    if ([string]$health["status"] -ne "resource_block") {
        return $false
    }
    $resetAtText = [string]$health["quota_reset_at"]
    if (-not $resetAtText) {
        return $false
    }
    try {
        $resetAt = [datetimeoffset]::Parse($resetAtText)
    } catch {
        return $false
    }
    if ($resetAt.UtcDateTime -gt (Get-Date).ToUniversalTime()) {
        return $false
    }
    $health["checked_at"] = (Get-Date).ToUniversalTime().ToString("o")
    $health["reason"] = "quota_reset_elapsed"
    $health["status"] = "warning"
    $health["can_resume"] = $true
    $health["quota_retry_after_seconds"] = 0
    $health["quota_observability"] = "retry_after_elapsed_from_codex_output"
    $health["summary"] = "codex_quota_reset_window_elapsed"
    Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 8
    Write-WatchdogLog "codex quota reset window elapsed; health downgraded to warning reset_at=$resetAtText"
    return $true
}

function Clear-CodexRuntimeBackoffHealthIfNoSignal {
    if (-not (Test-Path -LiteralPath $CodexHealthPath)) {
        return $false
    }
    $health = Read-CodexHealthRecord
    if ([bool]$health["health_parse_failed"]) {
        $health["checked_at"] = (Get-Date).ToUniversalTime().ToString("o")
        $health["reason"] = "runtime_log_observation_clear"
        $health["status"] = "ok"
        $health["can_resume"] = $true
        $health["quota_observability"] = "codex_cli_has_no_direct_quota_or_5h_reset_api"
        $health["quota_retry_after_seconds"] = $null
        $health["quota_reset_at"] = $null
        $health["quota_retry_after_source"] = ""
        $health["quota_signal"] = ""
        $health["quota_source"] = ""
        $health["quota_observed_at"] = $null
        $health["health_parse_failed"] = $false
        $health["summary"] = "codex_runtime_no_fresh_quota_signal"
        Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 8
        Write-WatchdogLog "codex health repaired; previous runtime quota health JSON was unreadable and no fresh quota signal was found"
        return $true
    }
    if ([string]$health["reason"] -eq "runtime_log_observation_clear") {
        $health["checked_at"] = (Get-Date).ToUniversalTime().ToString("o")
        $health["status"] = "ok"
        $health["can_resume"] = $true
        $health["summary"] = "codex_runtime_no_fresh_quota_signal"
        Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 8
        return $true
    }
    if ([string]$health["reason"] -ne "runtime_log_observation") {
        return $false
    }
    if ([string]$health["quota_observability"] -ne "backoff_observed_without_reset") {
        return $false
    }
    if ($health["quota_reset_at"]) {
        return $false
    }
    $health["checked_at"] = (Get-Date).ToUniversalTime().ToString("o")
    $health["reason"] = "runtime_log_observation_clear"
    $health["status"] = "ok"
    $health["can_resume"] = $true
    $health["quota_observability"] = "codex_cli_has_no_direct_quota_or_5h_reset_api"
    $health["quota_retry_after_seconds"] = $null
    $health["quota_reset_at"] = $null
    $health["quota_retry_after_source"] = ""
    $health["quota_signal"] = ""
    $health["quota_source"] = ""
    $health["quota_observed_at"] = $null
    $health["summary"] = "codex_runtime_no_fresh_quota_signal"
    Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 8
    Write-WatchdogLog "codex runtime quota warning cleared; no fresh quota signal found in recent run logs"
    return $true
}

function Select-CodexQuotaCandidateTextFromLines {
    param([string]$Text)
    if (-not $Text) {
        return ""
    }
    $selected = New-Object System.Collections.Generic.List[string]
    $lastTimestampLine = ""
    foreach ($line in ($Text -split "`r?`n")) {
        if (-not $line) {
            continue
        }
        $trimmedLine = $line.TrimStart()
        if (
            $trimmedLine.StartsWith("{") -and
            $line -notmatch '"type"\s*:\s*"(?:error|turn\.failed|response\.failed|run\.failed|session\.error|thread\.error)"' -and
            $line -notmatch '"error"\s*:' -and
            $line -notmatch '"message"\s*:' -and
            $line -notmatch '"severity"\s*:\s*"(?:error|fatal|warning|warn)"' -and
            $line -notmatch '"level"\s*:\s*"(?:error|fatal|warning|warn)"'
        ) {
            continue
        }
        if ($line -match '\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z') {
            $lastTimestampLine = $line
        }
        if (
            $line -match '"aggregated_output"\s*:' -or
            $line -match '"type"\s*:\s*"command_execution"' -or
            $line -match '"type"\s*:\s*"agent_message"' -or
            $line -match '"item"\s*:\s*\{' -or
            $line -match '"type"\s*:\s*"item\.completed"'
        ) {
            continue
        }
        if (
            $line -match '(?i)((?<![\d.])429(?![\d.]|ms\b|milliseconds?\b)|quota|rate limit|too many requests|retry-after|retry after|try again (?:in|at)|available (?:in|at)|reset (?:in|at)|resets (?:in|at)|limit reached|usage limit|resource exhausted|insufficient_quota|限流|额度|余额|流量|稍后重试|等待.*重试)' -or
            $line -match '"type"\s*:\s*"(?:error|turn\.failed|response\.failed|run\.failed|session\.error|thread\.error)"'
        ) {
            if ((Test-CodexShellToolExitOutputLine $line) -and -not (Test-CodexStrongResourceBackoffText $line)) {
                continue
            }
            if ($lastTimestampLine -and $lastTimestampLine -ne $line) {
                $selected.Add($lastTimestampLine)
                $lastTimestampLine = ""
            }
            $selected.Add($line)
        }
    }
    return ($selected -join "`n")
}

function Read-CodexQuotaCandidateTextFromFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    try {
        $lines = Get-Content -LiteralPath $Path -Tail 200 -Encoding UTF8 -ErrorAction Stop
        return Select-CodexQuotaCandidateTextFromLines -Text (@($lines) -join "`n")
    } catch {
        return Select-CodexQuotaCandidateTextFromLines -Text (Read-FileTailText -Path $Path)
    }
}

function Find-CodexQuotaSignalInRunLogs {
    param(
        [int]$LookbackHours = 12,
        [int]$MaxRunDirs = 80,
        [int]$MaxFiles = 120
    )
    $runsDir = Resolve-ProjectStatePath -Key "runs" -LegacyName "runs"
    if (-not (Test-Path -LiteralPath $runsDir)) {
        return $null
    }
    $cutoff = (Get-Date).AddHours(-1 * [Math]::Max(1, $LookbackHours))
    $runDirs = Get-ChildItem -LiteralPath $runsDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $cutoff } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First ([Math]::Max(1, $MaxRunDirs))
    $candidateFiles = New-Object System.Collections.Generic.List[object]
    foreach ($dir in @($runDirs)) {
        $dirFiles = Get-ChildItem -LiteralPath $dir.FullName -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Length -gt 0 -and
                $_.Extension -in @(".jsonl", ".json", ".txt") -and
                $_.Name -notlike "*.schema.json" -and
                $_.Name -notlike "*-prompt.txt"
            }
        foreach ($file in @($dirFiles)) {
            $candidateFiles.Add($file)
        }
    }
    $files = $candidateFiles |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First ([Math]::Max(1, $MaxFiles))

    foreach ($file in @($files)) {
        $candidateText = Read-CodexQuotaCandidateTextFromFile -Path $file.FullName
        if (-not (Test-CodexResourceBackoffText $candidateText)) {
            continue
        }
        $relativePath = ConvertTo-WorkspaceRelativePath -Path $file.FullName
        $signal = Get-CodexQuotaSignalFromText -Text $candidateText -Source "run_log:$relativePath"
        if ($signal.quota_observability -and $signal.quota_observability -ne "codex_cli_no_direct_quota_endpoint") {
            return $signal
        }
    }
    return $null
}

function Update-CodexHealthFromRunLogs {
    param([string]$EffectiveRootId)

    Update-ExpiredCodexQuotaHealth | Out-Null
    try {
        $health = Read-CodexHealthRecord
        $checkedAtText = [string]$health["checked_at"]
        if ($checkedAtText) {
            $checkedAt = [datetimeoffset]::Parse($checkedAtText)
            if (((Get-Date).ToUniversalTime() - $checkedAt.UtcDateTime).TotalSeconds -lt 180) {
                return $false
            }
        }
    } catch {
    }
    $signal = Find-CodexQuotaSignalInRunLogs -LookbackHours 4 -MaxRunDirs 40 -MaxFiles 60
    if ($null -eq $signal) {
        Clear-CodexRuntimeBackoffHealthIfNoSignal | Out-Null
        return $false
    }
    if ([string]$signal.quota_observability -eq "retry_after_elapsed_from_codex_output") {
        Clear-CodexRuntimeBackoffHealthIfNoSignal | Out-Null
        Write-WatchdogLog "codex quota reset from run log already elapsed root=$EffectiveRootId reset_at=$($signal.quota_reset_at); runtime backoff ignored"
        return $false
    }

    $health = Read-CodexHealthRecord
    $health["checked_at"] = (Get-Date).ToUniversalTime().ToString("o")
    $health["reason"] = "runtime_log_observation"
    $health["can_resume"] = $true
    $health["quota_observed_at"] = (Get-Date).ToUniversalTime().ToString("o")
    $health["quota_root_id"] = $EffectiveRootId
    Merge-CodexQuotaSignal -Health $health -Signal $signal
    if ($null -ne $signal.quota_retry_after_seconds -and [int]$signal.quota_retry_after_seconds -gt 0) {
        $health["status"] = "resource_block"
        $health["can_resume"] = $false
        $health["summary"] = "codex_runtime_retry_after_observed"
    } else {
        $health["status"] = "warning"
        $health["summary"] = "codex_runtime_backoff_observed_without_reset"
    }
    Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 8
    Write-WatchdogLog "codex quota signal observed root=$EffectiveRootId observability=$($signal.quota_observability) retry_after=$($signal.quota_retry_after_seconds) reset_at=$($signal.quota_reset_at) source=$($signal.quota_source)"
    return $true
}

function Invoke-CodexPreflight {
    param([string]$Reason)

    if (-not (Test-Path $WorkspaceDir)) {
        New-Item -ItemType Directory -Path $WorkspaceDir -Force | Out-Null
    }

    $health = New-CodexHealthRecord -Reason $Reason
    $health.can_resume = $false

    $codex = Get-Command codex.cmd -ErrorAction SilentlyContinue
    if (-not $codex) {
        $codex = Get-Command codex -ErrorAction SilentlyContinue
    }
    if (-not $codex) {
        $health.status = "blocked"
        $health.login_status = "missing_codex"
        $health.summary = "codex_command_missing"
        Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 6
        return [pscustomobject]$health
    }

    $health.codex_command = $codex.Source
    $loginArgs = @("login", "status")
    $login = Invoke-ExternalCommandWithTimeout -FilePath $codex.Source -Arguments $loginArgs -TimeoutSeconds ([Math]::Min($CodexPreflightTimeoutSeconds, 10))
    $health.login_stdout = ConvertTo-ShortText $login.stdout
    $health.login_stderr = ConvertTo-ShortText $login.stderr
    $loginText = "$($login.stdout)`n$($login.stderr)"
    Merge-CodexQuotaSignal -Health $health -Signal (Get-CodexQuotaSignalFromText -Text $loginText -Source "login_status")
    if (-not $login.timed_out -and $loginText -match "(?i)logged in") {
        $health.login_status = "ok"
        $health.can_resume = $true
        $health.status = "ok"
        $health.summary = "codex_login_ok_no_direct_quota_api"
    } elseif (Test-CodexResourceBackoffText $loginText) {
        $health.login_status = "resource_backoff"
        $health.status = "resource_block"
        $health.can_resume = $false
        $health.summary = "codex_login_resource_backoff"
    } else {
        $health.login_status = "failed"
        $health.status = "blocked"
        $health.can_resume = $false
        $health.summary = "codex_login_status_failed"
    }

    if ($health.login_status -eq "ok" -and $CodexPreflightTimeoutSeconds -gt 10) {
        $doctorTimeout = [Math]::Max(5, $CodexPreflightTimeoutSeconds - 10)
        $doctorArgs = @("doctor", "--summary", "--ascii", "--no-color")
        $doctor = Invoke-ExternalCommandWithTimeout -FilePath $codex.Source -Arguments $doctorArgs -TimeoutSeconds $doctorTimeout
        $health.doctor_stdout = ConvertTo-ShortText $doctor.stdout
        $health.doctor_stderr = ConvertTo-ShortText $doctor.stderr
        $doctorText = "$($doctor.stdout)`n$($doctor.stderr)"
        Merge-CodexQuotaSignal -Health $health -Signal (Get-CodexQuotaSignalFromText -Text $doctorText -Source "doctor")
        if (Test-CodexResourceBackoffText $doctorText) {
            $health.doctor_status = "resource_backoff"
            $health.status = "resource_block"
            $health.can_resume = $false
            $health.summary = "codex_doctor_resource_backoff"
        } elseif ($doctor.timed_out) {
            $health.doctor_status = "timeout_warning"
            if ($health.status -eq "ok") {
                $health.status = "warning"
                $health.summary = "codex_login_ok_doctor_timeout_warning"
            }
        } elseif ($doctor.exit_code -ne 0) {
            $health.doctor_status = "warning"
            if ($health.status -eq "ok") {
                $health.status = "warning"
                $health.summary = "codex_login_ok_doctor_warning"
            }
        } else {
            $health.doctor_status = "ok"
        }
    }

    Write-JsonNoBom -Value $health -Path $CodexHealthPath -Depth 6
    return [pscustomobject]$health
}

function Resume-TreeIfCodexReady {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        return $false
    }

    if (-not $SkipCodexPreflight) {
        $health = Invoke-CodexPreflight -Reason "before_resume"
        Write-WatchdogLog "codex preflight status=$($health.status) can_resume=$($health.can_resume) login=$($health.login_status) doctor=$($health.doctor_status) summary=$($health.summary)"
        if (-not [bool]$health.can_resume) {
            Write-WatchdogLog "resume held by codex preflight for root=$EffectiveRootId"
            return $false
        }
    }

    Invoke-RestMethod -Uri "$ApiUrl/api/tasks/$EffectiveRootId/resume" -Method Post -TimeoutSec 10 | Out-Null
    return $true
}

function Start-LocalSlotTopOffFallback {
    param(
        [string]$EffectiveRootId,
        [int]$Capacity,
        [int]$ActiveSlots,
        [int]$Frontier,
        [string]$ApiError,
        [int]$SettleSeconds = 8
    )

    if (-not $EffectiveRootId) {
        return $false
    }
    if ($SettleSeconds -gt 0) {
        Start-Sleep -Seconds $SettleSeconds
    }
    $latestState = Get-LocalWatchdogState -CandidateRootId $EffectiveRootId
    if ($null -ne $latestState -and $null -ne $latestState.codex_slots_active -and "$($latestState.codex_slots_active)" -ne "") {
        $latestEffectiveSlots = [int]$latestState.codex_slots_active
        if ($latestEffectiveSlots -ge $Capacity) {
            Write-WatchdogLog "slot top-off local launch skipped root=$EffectiveRootId because effective slots settled at $latestEffectiveSlots/$Capacity reason=$ApiError"
            return $true
        }
        if ($latestEffectiveSlots -gt $ActiveSlots) {
            $ActiveSlots = $latestEffectiveSlots
        }
    }
    $latestSlots = Get-ObservedCodexSlotCount
    if ($null -ne $latestSlots) {
        if ($latestSlots -ge $Capacity) {
            Write-WatchdogLog "slot top-off local launch skipped root=$EffectiveRootId because slots settled at $latestSlots/$Capacity reason=$ApiError"
            return $true
        }
        if ($latestSlots -gt $ActiveSlots) {
            $ActiveSlots = [int]$latestSlots
        }
    }
    if (Test-Path $ApiRestartDrainFlagPath) {
        Write-WatchdogLog "slot top-off local fallback skipped root=$EffectiveRootId because api restart drain is pending"
        return $false
    }
    $script = Join-Path $RepoRoot "scripts\local_kick_tree.py"
    if (-not (Test-Path -LiteralPath $script)) {
        Write-WatchdogLog "slot top-off local fallback unavailable root=$EffectiveRootId script=$script"
        return $false
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path -LiteralPath $python)) {
        $python = "python"
    }

    $env:CODEX_BATTLEFIELD_RUNNER = "real"
    $env:CODEX_BATTLEFIELD_MAX_CONCURRENCY = "$Capacity"
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED = if ($FixedConcurrency) { "0" } else { "1" }
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN = "1"
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX = "$Capacity"
    $env:CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS = "0"
    $env:CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS = "1800"
    $env:CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS = "45"
    $env:CODEX_BATTLEFIELD_AUTO_HIBERNATE_ON_RESOURCE_BLOCK = "1"
    $env:CODEX_BATTLEFIELD_GOAL_MODE = if ($GoalMode) { "1" } else { "0" }
    $env:CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS = "$ResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_LIMIT = "$AgentBurstLimit"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_RESERVE = "$AgentBurstReserve"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS = "$ResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB = "$SafeMinAvailableMemoryMB"
    $env:CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS = "$MachinePressureResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START = "0"
    $env:CODEX_BATTLEFIELD_PREVIEW_URL = "$WebUrl/"
    if ($CodexHome) {
        $env:CODEX_BATTLEFIELD_CODEX_HOME = "$CodexHome"
    } else {
        Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_HOME -ErrorAction SilentlyContinue
    }
    if ($CodexProfile) {
        $env:CODEX_BATTLEFIELD_CODEX_PROFILE = "$CodexProfile"
    } else {
        Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_PROFILE -ErrorAction SilentlyContinue
    }
    $currentToolHead = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref "HEAD"
    if ($currentToolHead) {
        $env:CODEX_BATTLEFIELD_LOCAL_KICK_TOOL_HEAD = $currentToolHead
    } else {
        Remove-Item Env:\CODEX_BATTLEFIELD_LOCAL_KICK_TOOL_HEAD -ErrorAction SilentlyContinue
    }
    Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT -ErrorAction SilentlyContinue

    $safeRoot = $EffectiveRootId -replace '[^a-zA-Z0-9_.-]', '_'
    $stamp = Get-Date -Format "yyyyMMddTHHmmssfff"
    $outPath = Join-Path $WorkspaceDir "local-kick-$safeRoot-$stamp.out.log"
    $errPath = Join-Path $WorkspaceDir "local-kick-$safeRoot-$stamp.err.log"
    $arguments = @(
        $script,
        "--root-id", $EffectiveRootId,
        "--runner-mode", "real",
        "--max-concurrency", "$Capacity",
        "--resume-after-seconds", "$ResumeAfterSeconds",
        "--machine-pressure-resume-after-seconds", "$MachinePressureResumeAfterSeconds",
        "--safe-min-available-memory-mb", "$SafeMinAvailableMemoryMB",
        "--topoff-only"
    )
    if ($Capacity -le 1) {
        $arguments += "--disable-secondary-lock"
    } else {
        $arguments += "--allow-primary-with-secondary"
    }
    if ($FixedConcurrency) {
        $arguments += "--fixed-concurrency"
    }
    try {
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList $arguments `
            -WorkingDirectory $RepoRoot `
            -RedirectStandardOutput $outPath `
            -RedirectStandardError $errPath `
            -WindowStyle Hidden `
            -PassThru
        Write-WatchdogLog "slot top-off local launch started root=$EffectiveRootId pid=$($process.Id) slots=$ActiveSlots/$Capacity frontier=$Frontier reason=$ApiError"
        return $true
    } catch {
        Write-WatchdogLog "slot top-off local fallback failed root=$EffectiveRootId slots=$ActiveSlots/$Capacity frontier=$Frontier error=$($_.Exception.Message)"
        return $false
    }
}

function Test-GoalModeLocalTopOffFirst {
    if (-not $GoalMode) {
        return $false
    }
    $mode = [string]$env:CODEX_BATTLEFIELD_GOAL_MODE_LOCAL_TOP_OFF_FIRST
    if (-not $mode) {
        return $true
    }
    return -not ($mode -match '^(?i:0|false|no|off|disabled)$')
}

function Invoke-SlotTopOffIfNeeded {
    param(
        $State,
        [string]$EffectiveRootId
    )

    if (-not $EffectiveRootId -or $null -eq $State) {
        return $false
    }
    if (Test-Path $ApiRestartDrainFlagPath) {
        Write-WatchdogLog "slot top-off skipped root=$EffectiveRootId because api restart drain is pending"
        return $false
    }
    $rootStatus = ""
    if ($null -ne $State.root_status) {
        $rootStatus = [string]$State.root_status
    }
    if ($rootStatus -in @("done", "failed", "canceled", "paused")) {
        return $false
    }
    if ($null -ne $State.active_resource_pressure -and [bool]$State.active_resource_pressure) {
        return $false
    }
    if ($null -ne $State.budget_guard_active -and [bool]$State.budget_guard_active) {
        return $false
    }
    $hibernating = 0
    if ($null -ne $State.hibernating) {
        $hibernating = [int]$State.hibernating
    }
    if ($hibernating -gt 0) {
        return $false
    }

    $capacity = [Math]::Max(1, [int]$MaxConcurrency)
    if ($null -ne $State.codex_slots_capacity -and "$($State.codex_slots_capacity)" -ne "") {
        $capacity = [int]$State.codex_slots_capacity
    }
    $activeSlots = 0
    if ($null -ne $State.codex_slots_active -and "$($State.codex_slots_active)" -ne "") {
        $activeSlots = [int]$State.codex_slots_active
    }
    $observedSlots = Get-ObservedCodexSlotCount
    if ($null -ne $observedSlots -and $observedSlots -gt $activeSlots) {
        $activeSlots = $observedSlots
    }
    $frontier = 0
    if ($null -ne $State.active_frontier -and "$($State.active_frontier)" -ne "") {
        $frontier = [int]$State.active_frontier
    }

    if ($activeSlots -ge $capacity -or $frontier -le $activeSlots) {
        return $false
    }

    if (Test-GoalModeLocalTopOffFirst) {
        Write-WatchdogLog "slot top-off using local-first scheduler root=$EffectiveRootId slots=$activeSlots/$capacity frontier=$frontier"
        return Start-LocalSlotTopOffFallback -EffectiveRootId $EffectiveRootId -Capacity $capacity -ActiveSlots $activeSlots -Frontier $frontier -ApiError "goal_mode_local_first" -SettleSeconds 0
    }

    try {
        Invoke-RestMethod -Uri "$ApiUrl/api/tasks/$EffectiveRootId/kick" -Method Post -TimeoutSec 20 | Out-Null
        Start-Sleep -Seconds 3
        $afterSlots = Get-ObservedCodexSlotCount
        if ($null -ne $afterSlots -and $afterSlots -gt $activeSlots -and $afterSlots -ge $capacity) {
            Write-WatchdogLog "slot top-off requested root=$EffectiveRootId slots=$activeSlots->$afterSlots/$capacity frontier=$frontier"
            return $true
        }
        if ($null -ne $afterSlots -and $afterSlots -gt $activeSlots) {
            Start-Sleep -Seconds 8
            $settledSlots = Get-ObservedCodexSlotCount
            if ($null -ne $settledSlots -and $settledSlots -ge $capacity) {
                Write-WatchdogLog "slot top-off API filled after settle root=$EffectiveRootId slots=$activeSlots->$settledSlots/$capacity frontier=$frontier"
                return $true
            }
            $fallbackSlots = if ($null -ne $settledSlots) { $settledSlots } else { $afterSlots }
            Write-WatchdogLog "slot top-off API partially filled root=$EffectiveRootId slots=$activeSlots->$fallbackSlots/$capacity frontier=$frontier; starting local fallback"
            return Start-LocalSlotTopOffFallback -EffectiveRootId $EffectiveRootId -Capacity $capacity -ActiveSlots $fallbackSlots -Frontier $frontier -ApiError "api_kick_partial_topoff"
        }
        Write-WatchdogLog "slot top-off API accepted but did not fill root=$EffectiveRootId slots=$activeSlots/$capacity frontier=$frontier; starting local fallback"
        return Start-LocalSlotTopOffFallback -EffectiveRootId $EffectiveRootId -Capacity $capacity -ActiveSlots $activeSlots -Frontier $frontier -ApiError "api_kick_no_topoff"
    } catch {
        Start-Sleep -Seconds 3
        $afterSlots = Get-ObservedCodexSlotCount
        if ($null -ne $afterSlots -and $afterSlots -gt $activeSlots -and $afterSlots -ge $capacity) {
            Write-WatchdogLog "slot top-off request likely accepted despite API timeout root=$EffectiveRootId slots=$activeSlots->$afterSlots/$capacity frontier=$frontier"
            return $true
        }
        if ($null -ne $afterSlots -and $afterSlots -gt $activeSlots) {
            Start-Sleep -Seconds 8
            $settledSlots = Get-ObservedCodexSlotCount
            if ($null -ne $settledSlots -and $settledSlots -ge $capacity) {
                Write-WatchdogLog "slot top-off API timeout filled after settle root=$EffectiveRootId slots=$activeSlots->$settledSlots/$capacity frontier=$frontier"
                return $true
            }
            $fallbackSlots = if ($null -ne $settledSlots) { $settledSlots } else { $afterSlots }
            Write-WatchdogLog "slot top-off API timeout partially filled root=$EffectiveRootId slots=$activeSlots->$fallbackSlots/$capacity frontier=$frontier; starting local fallback"
            return Start-LocalSlotTopOffFallback -EffectiveRootId $EffectiveRootId -Capacity $capacity -ActiveSlots $fallbackSlots -Frontier $frontier -ApiError $_.Exception.Message
        }
        $apiError = $_.Exception.Message
        Write-WatchdogLog "slot top-off API failed root=$EffectiveRootId slots=$activeSlots/$capacity frontier=$frontier error=$apiError"
        return Start-LocalSlotTopOffFallback -EffectiveRootId $EffectiveRootId -Capacity $capacity -ActiveSlots $activeSlots -Frontier $frontier -ApiError $apiError
    }
}

function Get-StateIntValue {
    param(
        $State,
        [string]$Name,
        [int]$Default = 0
    )

    if ($null -eq $State) {
        return $Default
    }
    $value = $State.PSObject.Properties[$Name]
    if ($null -eq $value -or $null -eq $value.Value -or "$($value.Value)" -eq "") {
        return $Default
    }
    try {
        return [int]$value.Value
    } catch {
        return $Default
    }
}

function Get-ObservedCodexSlotCount {
    try {
        return [int](Get-CodexBattlefieldProcessGroups).Count
    } catch {
        return $null
    }
}

function Test-GoalFuelTopOffOpportunity {
    param($State)

    if (-not $GoalMode -or $null -eq $State) {
        return $false
    }
    $rootStatus = ""
    if ($null -ne $State.root_status) {
        $rootStatus = [string]$State.root_status
    }
    if ($rootStatus -in @("done", "failed", "canceled", "paused")) {
        return $false
    }
    if ($null -ne $State.active_resource_pressure -and [bool]$State.active_resource_pressure) {
        return $false
    }
    if ($null -ne $State.budget_guard_active -and [bool]$State.budget_guard_active) {
        return $false
    }
    if ((Get-StateIntValue -State $State -Name "hibernating" -Default 0) -gt 0) {
        return $false
    }

    $capacity = Get-StateIntValue -State $State -Name "codex_slots_capacity" -Default ([Math]::Max(1, [int]$MaxConcurrency))
    $capacity = [Math]::Max(1, $capacity)
    $activeSlots = Get-StateIntValue -State $State -Name "codex_slots_active" -Default 0
    $observedSlots = Get-ObservedCodexSlotCount
    if ($null -ne $observedSlots -and $observedSlots -gt $activeSlots) {
        $activeSlots = $observedSlots
    }
    $frontier = Get-StateIntValue -State $State -Name "active_frontier" -Default 0
    return ($activeSlots -lt $capacity -and $frontier -gt $activeSlots)
}

function Suspend-ApiRestartForFuelTopOff {
    param($State)

    if (-not (Test-Path $ApiRestartFlagPath)) {
        Remove-Item -LiteralPath $ApiRestartLocalDrainPath -ErrorAction SilentlyContinue
        return $false
    }
    return $false
}

function Restore-DeferredApiRestartWhenIdle {
    param($State)

    if ((Test-Path $ApiRestartFlagPath) -or -not (Test-Path $ApiRestartDeferredFlagPath)) {
        return $false
    }
    try {
        $note = Get-Content -Path $ApiRestartDeferredFlagPath -Raw -ErrorAction SilentlyContinue
        if (-not $note) {
            $note = "restored deferred API restart; normal restart flags no longer block L4 fuel top-off"
        }
        Set-Content -Path $ApiRestartFlagPath -Value $note -Encoding UTF8
        Remove-Item -LiteralPath $ApiRestartDeferredFlagPath -ErrorAction SilentlyContinue
        Write-WatchdogLog "api restart defer removed; restored restart flag without blocking L4 fuel top-off"
        return $true
    } catch {
        Write-WatchdogLog "api restart deferred restore failed: $($_.Exception.Message)"
        return $false
    }
}

function Invoke-CheckpointLedgerAnchorIfNeeded {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        Write-WatchdogLog "checkpoint git anchor skipped; missing effective root id"
        return $false
    }
    $anchorMode = [string]$env:CODEX_BATTLEFIELD_CHECKPOINT_GIT_ANCHOR
    if (-not $anchorMode) {
        $anchorMode = if ($GoalMode) { "commit_tag" } else { "ledger" }
    }
    if ($anchorMode -match '^(?i:0|false|no|off|disabled)$') {
        Write-WatchdogLog "checkpoint git anchor skipped root=$EffectiveRootId mode=$anchorMode"
        return $false
    }
    # Do not gate checkpoint anchoring on a momentary process-count sample.
    # Slot sampling is intentionally conservative for top-off decisions, but
    # anchoring an already-written full checkpoint should not be skipped just
    # because a healthy run briefly reports 12/13 while agents are rotating.
    $checkpointRoot = Join-Path $WorkspaceDir "full_checkpoints\$EffectiveRootId"
    if (-not (Test-Path $checkpointRoot)) {
        Write-WatchdogLog "checkpoint git anchor skipped root=$EffectiveRootId mode=$anchorMode reason=missing_full_checkpoint_root"
        return $false
    }
    $latestCheckpoint = Get-ChildItem -Path $checkpointRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName "manifest.json") } |
        Sort-Object Name -Descending |
        Select-Object -First 1
    if ($null -eq $latestCheckpoint) {
        Write-WatchdogLog "checkpoint git anchor skipped root=$EffectiveRootId mode=$anchorMode reason=missing_manifest"
        return $false
    }
    $ledgerPath = Join-Path $WorkspaceDir "checkpoint_ledger\$EffectiveRootId\$($latestCheckpoint.Name).json"
    $tagBase = "battlefield-$EffectiveRootId-$($latestCheckpoint.Name)"
    $battlefieldTag = "$tagBase-battlefield"
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCommand) {
        $tagCheck = Invoke-ExternalCommandWithTimeout `
            -FilePath $gitCommand.Source `
            -Arguments @("-C", $RepoRoot, "rev-parse", "--verify", "refs/tags/$battlefieldTag") `
            -TimeoutSeconds 15
        if ($tagCheck.exit_code -eq 0) {
            Write-WatchdogLog "checkpoint git anchor skipped root=$EffectiveRootId checkpoint=$($latestCheckpoint.Name) mode=$anchorMode reason=already_tagged tag=$battlefieldTag"
            return $false
        }
    }
    if ((Test-Path $ledgerPath) -and $anchorMode -eq "ledger") {
        Write-WatchdogLog "checkpoint git anchor skipped root=$EffectiveRootId checkpoint=$($latestCheckpoint.Name) mode=$anchorMode reason=ledger_exists"
        return $false
    }

    $anchorScript = Join-Path $RepoRoot "scripts\git_snapshot_anchor.py"
    if (-not (Test-Path $anchorScript)) {
        Write-WatchdogLog "checkpoint ledger anchor skipped; missing script=$anchorScript"
        return $false
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            Write-WatchdogLog "checkpoint ledger anchor skipped; python was not found"
            return $false
        }
        $python = $pythonCommand.Source
    }

    $anchorArguments = @($anchorScript, "--root-id", $EffectiveRootId)
    if ($anchorMode -match '^(?i:commit|commit_tag|tag|auto)$') {
        $anchorArguments += @("--init-git", "--commit", "--tag")
    }
    $result = Invoke-ExternalCommandWithTimeout `
        -FilePath $python `
        -Arguments $anchorArguments `
        -TimeoutSeconds 180
    if ($result.exit_code -eq 0) {
        Write-WatchdogLog "checkpoint git anchor completed root=$EffectiveRootId checkpoint=$($latestCheckpoint.Name) mode=$anchorMode tag=$battlefieldTag"
        return $true
    }
    $detail = ConvertTo-ShortText "$($result.stderr) $($result.stdout)" 500
    Write-WatchdogLog "checkpoint git anchor failed root=$EffectiveRootId checkpoint=$($latestCheckpoint.Name) mode=$anchorMode exit=$($result.exit_code) detail=$detail"
    return $false
}

function Invoke-GitRevParseCommit {
    param(
        [string]$RepoPath,
        [string]$Ref
    )

    if (-not $RepoPath -or -not $Ref) {
        return ""
    }
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if (-not $gitCommand) {
        return ""
    }
    $result = Invoke-ExternalCommandWithTimeout `
        -FilePath $gitCommand.Source `
        -Arguments @("-C", $RepoPath, "rev-parse", "--verify", "$Ref^{commit}") `
        -TimeoutSeconds 15
    if ($result.exit_code -ne 0) {
        return ""
    }
    return ([string]$result.stdout).Trim()
}

function Test-LatestCheckpointCoversBattlefieldHead {
    param([string]$EffectiveRootId)

    $currentHead = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref "HEAD"
    if (-not $currentHead) {
        return $true
    }
    $ledgerPath = Join-Path $WorkspaceDir "checkpoint_ledger\$EffectiveRootId\latest.json"
    if (-not (Test-Path $ledgerPath)) {
        return $false
    }
    try {
        $ledger = Get-Content -LiteralPath $ledgerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $false
    }
    $refs = @()
    foreach ($name in @("battlefield_restore_ref", "battlefield_git_tag", "battlefield_git_commit_after", "battlefield_git_commit_before")) {
        if ($null -ne $ledger.$name -and "$($ledger.$name)") {
            $refs += [string]$ledger.$name
        }
    }
    if ($null -ne $ledger.battlefield_git) {
        foreach ($name in @("commit", "current_commit", "head", "head_commit")) {
            if ($null -ne $ledger.battlefield_git.$name -and "$($ledger.battlefield_git.$name)") {
                $refs += [string]$ledger.battlefield_git.$name
            }
        }
    }
    foreach ($ref in $refs) {
        $resolved = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref $ref
        if ($resolved -and $resolved -eq $currentHead) {
            return $true
        }
    }
    return $false
}

function Test-LocalKickDrainCoversToolHead {
    param(
        [string]$EffectiveRootId,
        [string]$CurrentHead
    )

    if (-not $EffectiveRootId -or -not $CurrentHead -or -not (Test-Path $LocalKickDrainPath)) {
        return $false
    }
    try {
        $payload = Get-Content -LiteralPath $LocalKickDrainPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $false
    }
    if ($null -ne $payload.drain -and -not [bool]$payload.drain) {
        return $false
    }
    $rootMatches = $false
    if ($null -ne $payload.root_ids) {
        foreach ($root in @($payload.root_ids)) {
            if ([string]$root -eq $EffectiveRootId) {
                $rootMatches = $true
                break
            }
        }
    } elseif ($null -ne $payload.root_id) {
        $rootMatches = ([string]$payload.root_id -eq $EffectiveRootId)
    } else {
        $rootMatches = $true
    }
    if (-not $rootMatches) {
        return $false
    }
    $replacementHead = ""
    foreach ($name in @("replacement_tool_head", "current_tool_head", "retire_before_tool_head")) {
        if ($null -ne $payload.$name -and "$($payload.$name)") {
            $replacementHead = [string]$payload.$name
            break
        }
    }
    if (-not $replacementHead) {
        return $false
    }
    $resolvedReplacement = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref $replacementHead
    if (-not $resolvedReplacement) {
        $resolvedReplacement = $replacementHead
    }
    $normalizedReplacement = $resolvedReplacement.Trim().ToLowerInvariant()
    $normalizedCurrent = $CurrentHead.Trim().ToLowerInvariant()
    if (-not $normalizedReplacement -or -not $normalizedCurrent) {
        return $false
    }
    return (
        $normalizedReplacement -eq $normalizedCurrent -or
        $normalizedReplacement.StartsWith($normalizedCurrent) -or
        $normalizedCurrent.StartsWith($normalizedReplacement)
    )
}

function Invoke-LocalKickDrainIfToolHeadChanged {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        return $false
    }
    $mode = [string]$env:CODEX_BATTLEFIELD_DRAIN_LOCAL_KICK_ON_TOOL_HEAD_CHANGE
    if (-not $mode) {
        $mode = if ($GoalMode) { "1" } else { "0" }
    }
    if ($mode -match '^(?i:0|false|no|off|disabled)$') {
        return $false
    }
    $currentHead = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref "HEAD"
    if (-not $currentHead) {
        return $false
    }
    $drainAlreadyCovers = Test-LocalKickDrainCoversToolHead -EffectiveRootId $EffectiveRootId -CurrentHead $currentHead
    if ($drainAlreadyCovers -and (Test-LatestCheckpointCoversBattlefieldHead -EffectiveRootId $EffectiveRootId)) {
        return $false
    }
    try {
        $payload = [ordered]@{
            drain = $true
            root_id = $EffectiveRootId
            root_ids = @($EffectiveRootId)
            reason = "tool_head_changed"
            requested_at = (Get-Date).ToUniversalTime().ToString("o")
            replacement_tool_head = $currentHead
            behavior = "Retire local_kick schedulers that started before this tool head; new schedulers launched from this head ignore this scoped drain."
        }
        Write-JsonNoBom -Value $payload -Path $LocalKickDrainPath -Depth 6
        Write-WatchdogLog "local kick drain requested for tool-head change root=$EffectiveRootId replacement_head=$($currentHead.Substring(0, [Math]::Min(12, $currentHead.Length)))"
        return $true
    } catch {
        Write-WatchdogLog "local kick drain request failed root=$EffectiveRootId error=$($_.Exception.Message)"
        return $false
    }
}

function Invoke-FullCheckpointIfToolHeadChanged {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        return $false
    }
    $mode = [string]$env:CODEX_BATTLEFIELD_CHECKPOINT_ON_TOOL_HEAD_CHANGE
    if (-not $mode) {
        $mode = if ($GoalMode) { "1" } else { "0" }
    }
    if ($mode -match '^(?i:0|false|no|off|disabled)$') {
        return $false
    }
    if (Test-LatestCheckpointCoversBattlefieldHead -EffectiveRootId $EffectiveRootId) {
        return $false
    }
    $scriptPath = Join-Path $RepoRoot "scripts\write_full_checkpoint.py"
    if (-not (Test-Path $scriptPath)) {
        Write-WatchdogLog "tool-head checkpoint skipped root=$EffectiveRootId; missing script=$scriptPath"
        return $false
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            Write-WatchdogLog "tool-head checkpoint skipped root=$EffectiveRootId; python was not found"
            return $false
        }
        $python = $pythonCommand.Source
    }
    $result = Invoke-ExternalCommandWithTimeout `
        -FilePath $python `
        -Arguments @($scriptPath, "--root-id", $EffectiveRootId, "--reason", "watchdog.tool_head_changed") `
        -TimeoutSeconds 240
    if ($result.exit_code -eq 0) {
        $detail = ConvertTo-ShortText $result.stdout 300
        Write-WatchdogLog "tool-head full checkpoint completed root=$EffectiveRootId detail=$detail"
        return $true
    }
    $detail = ConvertTo-ShortText "$($result.stderr) $($result.stdout)" 500
    Write-WatchdogLog "tool-head full checkpoint failed root=$EffectiveRootId exit=$($result.exit_code) detail=$detail"
    return $false
}

function Invoke-RetryableBlockRepairIfNeeded {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        return $false
    }
    $scriptPath = Join-Path $RepoRoot "scripts\repair_retryable_block_reports.py"
    $dbPath = Resolve-ProjectStatePath -Key "db" -LegacyName "battlefield.sqlite3"
    if (-not (Test-Path $scriptPath) -or -not (Test-Path $dbPath)) {
        return $false
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            return $false
        }
        $python = $pythonCommand.Source
    }
    $retryAfter = if ($GoalMode) { 300 } else { [Math]::Max(300, [int]$ResumeAfterSeconds) }
    $result = Invoke-ExternalCommandWithTimeout `
        -FilePath $python `
        -Arguments @($scriptPath, "--db", $dbPath, "--root-id", $EffectiveRootId, "--retry-after-seconds", "$retryAfter", "--limit", "$MaxConcurrency") `
        -TimeoutSeconds 60
    if ($result.stdout) {
        try {
            $payload = $result.stdout | ConvertFrom-Json
            $repaired = @($payload.repaired).Count
            $adjustedWait = @($payload.adjusted_wait).Count
            $requeued = @($payload.requeued).Count
            if ($repaired -gt 0 -or $adjustedWait -gt 0 -or $requeued -gt 0) {
                Write-WatchdogLog "retryable block repair root=$EffectiveRootId repaired=$repaired adjusted_wait=$adjustedWait requeued=$requeued"
                return $true
            }
            return $false
        } catch {
        }
    }
    if ($result.exit_code -ne 0) {
        $detail = ConvertTo-ShortText "$($result.stderr) $($result.stdout)" 500
        Write-WatchdogLog "retryable block repair failed root=$EffectiveRootId exit=$($result.exit_code) detail=$detail"
    }
    return $false
}

function Invoke-BlockerRecoveryDependencyRepairIfNeeded {
    param([string]$EffectiveRootId)

    if (-not $EffectiveRootId) {
        return $false
    }
    $scriptPath = Join-Path $RepoRoot "scripts\repair_blocker_recovery_dependencies.py"
    $dbPath = Resolve-ProjectStatePath -Key "db" -LegacyName "battlefield.sqlite3"
    if (-not (Test-Path $scriptPath) -or -not (Test-Path $dbPath)) {
        return $false
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            return $false
        }
        $python = $pythonCommand.Source
    }
    $result = Invoke-ExternalCommandWithTimeout `
        -FilePath $python `
        -Arguments @($scriptPath, "--db", $dbPath, "--root-id", $EffectiveRootId, "--limit", "$MaxConcurrency") `
        -TimeoutSeconds 60
    if ($result.stdout) {
        try {
            $payload = $result.stdout | ConvertFrom-Json
            $dependencyFixes = @($payload.dependency_fixes).Count
            $reclassified = @($payload.reclassified).Count
            $retargeted = @($payload.retargeted).Count
            $requeued = @($payload.requeued).Count
            if ($dependencyFixes -gt 0 -or $reclassified -gt 0 -or $retargeted -gt 0 -or $requeued -gt 0) {
                Write-WatchdogLog "blocker recovery dependency repair root=$EffectiveRootId dependency_fixes=$dependencyFixes reclassified=$reclassified retargeted=$retargeted requeued=$requeued"
                return $true
            }
            return $false
        } catch {
        }
    }
    if ($result.exit_code -ne 0) {
        $detail = ConvertTo-ShortText "$($result.stderr) $($result.stdout)" 500
        Write-WatchdogLog "blocker recovery dependency repair failed root=$EffectiveRootId exit=$($result.exit_code) detail=$detail"
    }
    return $false
}

function Invoke-CodexHealthScanIfNeeded {
    param([string]$EffectiveRootId)

    if ($SkipCodexPreflight) {
        return $false
    }
    Update-CodexHealthFromRunLogs -EffectiveRootId $EffectiveRootId | Out-Null
    return $true
}

function Refresh-WatchdogStateAfterAction {
    param(
        $PreviousState,
        [string]$EffectiveRootId
    )

    $nextState = Get-LocalWatchdogState -CandidateRootId $EffectiveRootId
    if ($null -ne $nextState) {
        return $nextState
    }
    $nextState = Get-WatchdogState -CandidateRootId $EffectiveRootId
    if ($null -ne $nextState) {
        return $nextState
    }
    try {
        $nextState = Get-LegacyTreeState -CandidateRootId $EffectiveRootId
        if ($null -ne $nextState) {
            return $nextState
        }
    } catch {
        Write-WatchdogLog "state refresh after action failed for root=$EffectiveRootId; keeping previous state: $($_.Exception.Message)"
    }
    $nextState = Get-LocalWatchdogState -CandidateRootId $EffectiveRootId
    if ($null -ne $nextState) {
        return $nextState
    }
    return $PreviousState
}

function Test-Endpoint {
    param([string]$Url)
    try {
        Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-BattlefieldApiProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "python.exe" -and
        $_.CommandLine -match "uvicorn app.main:app" -and
        $_.CommandLine -match "--port\s+$ApiPort"
    })
}

function Get-ApiListenerProcessId {
    try {
        $listener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($listener) {
            return [int]$listener.OwningProcess
        }
    } catch {
        return $null
    }
    return $null
}

function Stop-DuplicateBattlefieldApiProcesses {
    $ownerPid = Get-ApiListenerProcessId
    $apiProcesses = @(Get-BattlefieldApiProcesses)
    if ($apiProcesses.Count -le 1) {
        return
    }
    if (-not $ownerPid) {
        Write-WatchdogLog "duplicate api processes detected, but no listening owner was identified; cleanup skipped"
        return
    }

    $ownerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
    $allowedPids = @([int]$ownerPid)
    if ($ownerProcess -and $ownerProcess.ParentProcessId) {
        $allowedPids += [int]$ownerProcess.ParentProcessId
    }

    foreach ($process in $apiProcesses) {
        if ($allowedPids -contains [int]$process.ProcessId) {
            continue
        }
        if (Test-ApiProcessOwnsCodexChildren -ProcessId ([int]$process.ProcessId)) {
            Write-WatchdogLog "duplicate api process pid=$($process.ProcessId) still owns Codex child processes; cleanup skipped"
            continue
        }
        try {
            Stop-Process -Id $process.ProcessId -Force
            Write-WatchdogLog "stopped duplicate api process pid=$($process.ProcessId)"
        } catch {
            Write-WatchdogLog "failed to stop duplicate api process pid=$($process.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Stop-AllBattlefieldApiProcesses {
    $apiProcesses = @(Get-BattlefieldApiProcesses)
    foreach ($process in $apiProcesses) {
        try {
            Stop-Process -Id $process.ProcessId -Force
            Write-WatchdogLog "stopped api process for idle restart pid=$($process.ProcessId)"
        } catch {
            Write-WatchdogLog "failed to stop api process for idle restart pid=$($process.ProcessId): $($_.Exception.Message)"
        }
    }
}

function Get-LastMessagePathFromCommandLine {
    param([string]$CommandLine)
    if (-not $CommandLine) {
        return ""
    }
    $match = [regex]::Match($CommandLine, '--output-last-message\s+(?:"([^"]+)"|(\S+))')
    if (-not $match.Success) {
        return ""
    }
    if ($match.Groups[1].Success) {
        return $match.Groups[1].Value
    }
    return $match.Groups[2].Value
}

function Get-RunLogPathFromLastMessagePath {
    param([string]$LastMessagePath)
    if (-not $LastMessagePath) {
        return ""
    }
    $leaf = Split-Path -Path $LastMessagePath -Leaf
    if ($leaf -notmatch '^(.*)-last-message\.json$') {
        return ""
    }
    $kind = $Matches[1]
    $dir = Split-Path -Path $LastMessagePath -Parent
    if (-not $dir -or -not $kind) {
        return ""
    }
    return (Join-Path $dir "$kind.jsonl")
}

function Test-RunLogHasUnresolvedIdleWatchdog {
    param([string]$LogPath)
    if (-not $LogPath -or -not (Test-Path $LogPath)) {
        return $false
    }
    try {
        $item = Get-Item -LiteralPath $LogPath -ErrorAction Stop
        $stream = [System.IO.File]::Open($item.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        try {
            $tailBytes = [Math]::Min([int64]262144, $stream.Length)
            $stream.Seek(-$tailBytes, [System.IO.SeekOrigin]::End) | Out-Null
            $buffer = New-Object byte[] $tailBytes
            $read = $stream.Read($buffer, 0, $buffer.Length)
            $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        } finally {
            $stream.Dispose()
        }
        $comparison = [System.StringComparison]::OrdinalIgnoreCase
        $lastIdle = $text.LastIndexOf("idle watchdog fired", $comparison)
        if ($lastIdle -lt 0) {
            return $false
        }
        $lastAttempt = $text.LastIndexOf("--- codex ", $comparison)
        return ($lastIdle -gt $lastAttempt)
    } catch {
        Write-WatchdogLog "stale codex scan skipped unreadable log=$LogPath error=$($_.Exception.Message)"
        return $false
    }
}

function Test-LastMessageReadyForCleanup {
    param([string]$LastMessagePath)
    if (-not $LastMessagePath -or -not (Test-Path $LastMessagePath)) {
        return $false
    }
    try {
        $item = Get-Item -LiteralPath $LastMessagePath -ErrorAction Stop
        if ($item.Length -le 0) {
            return $false
        }
        $ageSeconds = ((Get-Date).ToUniversalTime() - $item.LastWriteTimeUtc).TotalSeconds
        return ($ageSeconds -ge [Math]::Max(30, $CompletedCodexGraceSeconds))
    } catch {
        Write-WatchdogLog "completed codex cleanup skipped unreadable last_message=$LastMessagePath error=$($_.Exception.Message)"
        return $false
    }
}

function Get-CodexBattlefieldProcessGroups {
    $processes = @(Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -like "*codex-battlefield*workspace*" -and
        $_.CommandLine -like "*--output-last-message*" -and
        ($_.Name -eq "cmd.exe" -or $_.Name -eq "node.exe" -or $_.Name -eq "codex.exe")
    })
    $groups = @{}
    foreach ($process in $processes) {
        $lastMessagePath = Get-LastMessagePathFromCommandLine -CommandLine $process.CommandLine
        if (-not $lastMessagePath) {
            continue
        }
        if (-not $groups.ContainsKey($lastMessagePath)) {
            $groups[$lastMessagePath] = @()
        }
        $groups[$lastMessagePath] += $process
    }
    return $groups
}

function Test-ApiProcessOwnsCodexChildren {
    param(
        [int]$ProcessId,
        [switch]$IgnoreDetachedLocalSchedulers
    )

    if ($ProcessId -le 0) {
        return $false
    }
    try {
        $processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine)
    } catch {
        return $false
    }
    $parents = @{}
    $commands = @{}
    foreach ($process in $processes) {
        $parents[[int]$process.ProcessId] = [int]$process.ParentProcessId
        $commands[[int]$process.ProcessId] = [string]$process.CommandLine
    }
    foreach ($process in $processes) {
        if (-not (
            ($process.Name -eq "cmd.exe" -or $process.Name -eq "node.exe" -or $process.Name -eq "codex.exe") -and
            $process.CommandLine -like "*codex-battlefield*workspace*" -and
            $process.CommandLine -like "*--output-last-message*"
        )) {
            continue
        }
        $cursor = [int]$process.ParentProcessId
        $seen = @{}
        $viaDetachedLocalScheduler = $false
        while ($cursor -gt 0 -and -not $seen.ContainsKey($cursor)) {
            if ($commands.ContainsKey($cursor) -and $commands[$cursor] -like "*local_kick_tree.py*") {
                $viaDetachedLocalScheduler = $true
            }
            if ($cursor -eq $ProcessId) {
                if ($IgnoreDetachedLocalSchedulers -and $viaDetachedLocalScheduler) {
                    break
                }
                return $true
            }
            $seen[$cursor] = $true
            if (-not $parents.ContainsKey($cursor)) {
                break
            }
            $cursor = [int]$parents[$cursor]
        }
    }
    return $false
}

function Test-AnyBattlefieldApiProcessOwnsCodexChildren {
    param([switch]$IgnoreDetachedLocalSchedulers)

    $apiProcesses = @(Get-BattlefieldApiProcesses)
    foreach ($process in $apiProcesses) {
        if (Test-ApiProcessOwnsCodexChildren -ProcessId ([int]$process.ProcessId) -IgnoreDetachedLocalSchedulers:$IgnoreDetachedLocalSchedulers) {
            return $true
        }
    }
    return $false
}

function Stop-DrainedLocalKickSchedulers {
    param(
        [string]$EffectiveRootId,
        [int]$GraceSeconds = 60
    )

    if (-not $EffectiveRootId) {
        return $false
    }
    $currentHead = Invoke-GitRevParseCommit -RepoPath $RepoRoot -Ref "HEAD"
    if (-not (Test-LocalKickDrainCoversToolHead -EffectiveRootId $EffectiveRootId -CurrentHead $currentHead)) {
        return $false
    }
    try {
        $processes = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine,CreationDate)
    } catch {
        Write-WatchdogLog "drained local_kick cleanup skipped root=$EffectiveRootId; process scan failed: $($_.Exception.Message)"
        return $false
    }
    $escapedRoot = [regex]::Escape($EffectiveRootId)
    $localKickProcesses = @($processes | Where-Object {
        $_.Name -eq "python.exe" -and
        [string]$_.CommandLine -match "local_kick_tree\.py" -and
        [string]$_.CommandLine -match "--root-id\s+$escapedRoot(\s|$)"
    })
    if ($localKickProcesses.Count -eq 0) {
        return $false
    }
    $localKickPids = @{}
    foreach ($process in $localKickProcesses) {
        $localKickPids[[int]$process.ProcessId] = $true
    }
    $roots = @($localKickProcesses | Where-Object { -not $localKickPids.ContainsKey([int]$_.ParentProcessId) })
    $stopped = 0
    foreach ($process in $roots) {
        $localKickProcessId = [int]$process.ProcessId
        $ageSeconds = [double]::PositiveInfinity
        if ($null -ne $process.CreationDate) {
            try {
                $createdAt = ([datetime]$process.CreationDate).ToUniversalTime()
                $ageSeconds = ((Get-Date).ToUniversalTime() - $createdAt).TotalSeconds
            } catch {
                $ageSeconds = [double]::PositiveInfinity
            }
        }
        if ($ageSeconds -lt [Math]::Max(30, $GraceSeconds)) {
            continue
        }
        if (Test-ApiProcessOwnsCodexChildren -ProcessId $localKickProcessId) {
            Write-WatchdogLog "drained local_kick scheduler kept root=$EffectiveRootId pid=$localKickProcessId because it still owns Codex children"
            continue
        }
        try {
            $output = & taskkill /PID $localKickProcessId /T /F 2>&1
            $stopped += 1
            Write-WatchdogLog "stopped drained local_kick scheduler root=$EffectiveRootId pid=$localKickProcessId age_seconds=$([Math]::Round($ageSeconds, 1)) output=$(ConvertTo-ShortText ($output -join ' ') 800)"
        } catch {
            Write-WatchdogLog "failed to stop drained local_kick scheduler root=$EffectiveRootId pid=$localKickProcessId error=$($_.Exception.Message)"
        }
    }
    return ($stopped -gt 0)
}

function Get-MachineSafetyState {
    $groups = Get-CodexBattlefieldProcessGroups
    $codexResidentMB = 0
    foreach ($group in $groups.Values) {
        foreach ($process in $group) {
            $codexResidentMB += [double]($process.WorkingSetSize / 1MB)
        }
    }

    $freeMB = 0
    $totalMB = 0
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $freeMB = [Math]::Round([double]$os.FreePhysicalMemory / 1024, 1)
        $totalMB = [Math]::Round([double]$os.TotalVisibleMemorySize / 1024, 1)
    } catch {
        Write-WatchdogLog "machine safety memory probe failed: $($_.Exception.Message)"
    }

    $reasons = @()
    if ($SafeMinAvailableMemoryMB -gt 0 -and $freeMB -gt 0 -and $freeMB -lt $SafeMinAvailableMemoryMB) {
        $reasons += "available_memory_mb=$freeMB below safe_min=$SafeMinAvailableMemoryMB"
    }
    if ($MaxCodexProcessTrees -gt 0 -and $groups.Count -gt $MaxCodexProcessTrees) {
        $reasons += "codex_process_trees=$($groups.Count) above max=$MaxCodexProcessTrees"
    }
    if ($MaxCodexResidentMB -gt 0 -and $codexResidentMB -gt $MaxCodexResidentMB) {
        $reasons += "codex_resident_mb=$([Math]::Round($codexResidentMB, 1)) above max=$MaxCodexResidentMB"
    }

    $state = [pscustomobject]@{
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
        pressure = ($reasons.Count -gt 0)
        reason = ($reasons -join "; ")
        available_memory_mb = $freeMB
        total_memory_mb = $totalMB
        codex_process_trees = $groups.Count
        codex_resident_mb = [Math]::Round($codexResidentMB, 1)
        safe_min_available_memory_mb = $SafeMinAvailableMemoryMB
        max_codex_process_trees = $MaxCodexProcessTrees
        max_codex_resident_mb = $MaxCodexResidentMB
    }
    try {
        Write-JsonNoBom -Value $state -Path $MachineSafetyPath -Depth 4
    } catch {
        Write-WatchdogLog "machine safety state write failed: $($_.Exception.Message)"
    }
    return $state
}

function Stop-StaleCodexProcessTrees {
    $groups = Get-CodexBattlefieldProcessGroups
    if ($groups.Count -eq 0) {
        return
    }

    foreach ($lastMessagePath in $groups.Keys) {
        $logPath = Get-RunLogPathFromLastMessagePath -LastMessagePath $lastMessagePath
        $hasIdleWatchdog = Test-RunLogHasUnresolvedIdleWatchdog -LogPath $logPath
        $hasCompletedMessage = Test-LastMessageReadyForCleanup -LastMessagePath $lastMessagePath
        if (-not ($hasIdleWatchdog -or $hasCompletedMessage)) {
            continue
        }
        $candidates = @($groups[$lastMessagePath] | Sort-Object `
            @{ Expression = { if ($_.Name -eq "cmd.exe") { 0 } elseif ($_.Name -eq "node.exe") { 1 } else { 2 } } }, `
            CreationDate)
        if ($candidates.Count -eq 0) {
            continue
        }
        $root = $candidates[0]
        try {
            $output = & taskkill /PID $root.ProcessId /T /F 2>&1
            $reason = if ($hasCompletedMessage) { "last_message_persisted" } else { "idle_watchdog" }
            Write-WatchdogLog "stopped stale codex process tree pid=$($root.ProcessId) reason=$reason log=$logPath output=$(ConvertTo-ShortText ($output -join ' ') 800)"
        } catch {
            Write-WatchdogLog "failed to stop stale codex process tree pid=$($root.ProcessId) log=$logPath error=$($_.Exception.Message)"
        }
    }
}

function Start-BattlefieldApi {
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $python = "python"
    }

    $env:CODEX_BATTLEFIELD_RUNNER = "real"
    $env:CODEX_BATTLEFIELD_MAX_CONCURRENCY = "$MaxConcurrency"
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED = if ($FixedConcurrency) { "0" } else { "1" }
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN = "1"
    $env:CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX = "$MaxConcurrency"
    $env:CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS = "0"
    $env:CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS = "1800"
    $env:CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS = "45"
    $env:CODEX_BATTLEFIELD_AUTO_HIBERNATE_ON_RESOURCE_BLOCK = "1"
    $env:CODEX_BATTLEFIELD_GOAL_MODE = if ($GoalMode) { "1" } else { "0" }
    $env:CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS = "$ResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_LIMIT = "$AgentBurstLimit"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_RESERVE = "$AgentBurstReserve"
    $env:CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS = "$ResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB = "$SafeMinAvailableMemoryMB"
    $env:CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS = "$MachinePressureResumeAfterSeconds"
    $env:CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START = "1"
    $env:CODEX_BATTLEFIELD_PREVIEW_URL = "$WebUrl/"
    if ($CodexHome) {
        $env:CODEX_BATTLEFIELD_CODEX_HOME = "$CodexHome"
    } else {
        Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_HOME -ErrorAction SilentlyContinue
    }
    if ($CodexProfile) {
        $env:CODEX_BATTLEFIELD_CODEX_PROFILE = "$CodexProfile"
    } else {
        Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_PROFILE -ErrorAction SilentlyContinue
    }

    Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_MODEL -ErrorAction SilentlyContinue
    Remove-Item Env:\CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT -ErrorAction SilentlyContinue

    Start-Process `
        -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -WorkingDirectory (Join-Path $RepoRoot "api") `
        -WindowStyle Hidden
}

function Start-BattlefieldWeb {
    $pnpm = Get-Command pnpm.cmd -ErrorAction SilentlyContinue
    if ($pnpm) {
        Start-Process `
            -FilePath $pnpm.Source `
            -ArgumentList @("dev", "--host", "127.0.0.1", "--port", "$WebPort") `
            -WorkingDirectory (Join-Path $RepoRoot "web") `
            -WindowStyle Hidden
        return
    }

    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($npm) {
        Start-Process `
            -FilePath $npm.Source `
            -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$WebPort") `
            -WorkingDirectory (Join-Path $RepoRoot "web") `
            -WindowStyle Hidden
    }
}

function Restart-ApiIfIdleRequested {
    param($State)

    Restore-DeferredApiRestartWhenIdle -State $State | Out-Null

    if (-not (Test-Path $ApiRestartFlagPath)) {
        return $false
    }

    if (Suspend-ApiRestartForFuelTopOff -State $State) {
        return $false
    }

    $activeAgents = 0
    if ($null -ne $State -and $null -ne $State.active_agents) {
        $activeAgents = [int]$State.active_agents
    }
    $codexSlotsActive = $activeAgents
    if ($null -ne $State -and $null -ne $State.codex_slots_active) {
        $codexSlotsActive = [int]$State.codex_slots_active
    }
    $observedSlots = Get-ObservedCodexSlotCount
    if ($null -ne $observedSlots -and $observedSlots -gt $codexSlotsActive) {
        $codexSlotsActive = [int]$observedSlots
    }
    $apiOwnsCodexChildren = Test-AnyBattlefieldApiProcessOwnsCodexChildren -IgnoreDetachedLocalSchedulers
    if (($activeAgents -gt 0 -or $codexSlotsActive -gt 0) -and $apiOwnsCodexChildren) {
        Write-WatchdogLog "api idle restart pending, active_agents=$activeAgents codex_slots_active=$codexSlotsActive"
        if (Test-Path $ApiRestartDrainFlagPath) {
            $effectiveRootId = ""
            if ($null -ne $State -and $State.root_id) {
                $effectiveRootId = [string]$State.root_id
            } elseif ($RootId) {
                $effectiveRootId = $RootId
            }
            if (-not $effectiveRootId) {
                Write-WatchdogLog "api restart drain armed, but no root_id is available"
                return $false
            }
            try {
                $drain = [ordered]@{
                    status = "waiting_for_codex_slots"
                    root_id = $effectiveRootId
                    observed_active_agents = $activeAgents
                    observed_codex_slots_active = $codexSlotsActive
                    reason = "API restart drain waits for real Codex slots to reach 0 before restarting API; it must not hibernate live agents."
                    updated_at = (Get-Date).ToUniversalTime().ToString("o")
                }
                Write-JsonNoBom -Value $drain -Path $ApiRestartLocalDrainPath -Depth 4
                Write-WatchdogLog "api restart drain armed; waiting for observed Codex slots to reach 0 root=$effectiveRootId active_agents=$activeAgents codex_slots_active=$codexSlotsActive"
            } catch {
                try {
                    $fallback = [ordered]@{
                        status = "active"
                        root_id = $effectiveRootId
                        observed_active_agents = $activeAgents
                        observed_codex_slots_active = $codexSlotsActive
                        reason = "API restart drain state write failed; restart flags remain armed so local schedulers drain naturally until observed Codex slots reach 0."
                        error = $_.Exception.Message
                        updated_at = (Get-Date).ToUniversalTime().ToString("o")
                    }
                    Write-JsonNoBom -Value $fallback -Path $ApiRestartLocalDrainPath -Depth 4
                } catch {
                    Write-WatchdogLog "api restart local drain state write failed: $($_.Exception.Message)"
                }
                Write-WatchdogLog "api restart drain state write failed: $($_.Exception.Message); waiting for observed Codex slots to reach 0"
            }
        }
        return $false
    }
    if ($activeAgents -gt 0 -or $codexSlotsActive -gt 0) {
        Write-WatchdogLog "api restart proceeding while detached Codex slots continue; API owns no Codex child processes active_agents=$activeAgents codex_slots_active=$codexSlotsActive"
    }

    Write-WatchdogLog "api idle restart flag detected; restarting API with latest code"
    Stop-AllBattlefieldApiProcesses
    Start-Sleep -Seconds 2
    Start-BattlefieldApi
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Endpoint "$ApiUrl/api/health") {
            Remove-Item -LiteralPath $ApiRestartFlagPath -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $ApiRestartDrainFlagPath -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $ApiRestartLocalDrainPath -ErrorAction SilentlyContinue
            Write-WatchdogLog "api idle restart completed"
            return $true
        }
    }
    Write-WatchdogLog "api idle restart attempted, but API health did not recover in time"
    return $false
}

function Get-WatchdogState {
    param([string]$CandidateRootId)
    $uri = "$ApiUrl/api/watchdog/state"
    if ($CandidateRootId) {
        $uri = "${uri}?root_id=$([uri]::EscapeDataString($CandidateRootId))"
    }
    try {
        return Invoke-RestMethod -Uri $uri -TimeoutSec 8
    } catch {
        return $null
    }
}

function Get-LocalWatchdogState {
    param([string]$CandidateRootId)

    $scriptPath = Join-Path $RepoRoot "scripts\local_watchdog_state.py"
    $dbPath = Resolve-ProjectStatePath -Key "db" -LegacyName "battlefield.sqlite3"
    if (-not (Test-Path $scriptPath) -or -not (Test-Path $dbPath)) {
        return $null
    }
    $python = Resolve-RepoPython
    if (-not (Test-Path $python)) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            return $null
        }
        $python = $pythonCommand.Source
    }
    $slotsActive = 0
    try {
        $slotsActive = (Get-CodexBattlefieldProcessGroups).Count
    } catch {
        $slotsActive = 0
    }
    $arguments = @(
        $scriptPath,
        "--db", $dbPath,
        "--root-id", "$CandidateRootId",
        "--slots-active", "$slotsActive",
        "--capacity", "$MaxConcurrency",
        "--queue-threshold", "$AutoConcurrencyQueueThreshold",
        "--fuel-deadline-hour", "$FuelDeadlineHour",
        "--fuel-timezone-offset-hours", "$FuelTimezoneOffsetHours",
        "--fuel-sprint-window-minutes", "$FuelSprintWindowMinutes"
    )
    if ($GoalMode) {
        $arguments += "--goal-mode"
    }
    $result = Invoke-ExternalCommandWithTimeout -FilePath $python -Arguments $arguments -TimeoutSeconds 10
    if ($result.stdout) {
        try {
            $state = $result.stdout | ConvertFrom-Json
            Write-WatchdogLog "local sqlite watchdog state used root=$($state.root_id) slots=$($state.codex_slots_active)/$($state.codex_slots_capacity) frontier=$($state.active_frontier)"
            return $state
        } catch {
        }
    }
    if ($result.exit_code -ne 0) {
        $detail = ConvertTo-ShortText "$($result.stderr) $($result.stdout)" 500
        Write-WatchdogLog "local sqlite watchdog state failed root=$CandidateRootId exit=$($result.exit_code) detail=$detail"
        return $null
    }
    try {
        $state = $result.stdout | ConvertFrom-Json
        Write-WatchdogLog "local sqlite watchdog state used root=$($state.root_id) slots=$($state.codex_slots_active)/$($state.codex_slots_capacity) frontier=$($state.active_frontier)"
        return $state
    } catch {
        Write-WatchdogLog "local sqlite watchdog state parse failed root=$CandidateRootId error=$($_.Exception.Message)"
        return $null
    }
}

function Invoke-MachineSafetyGuard {
    param($State)

    $safety = Get-MachineSafetyState
    Write-WatchdogLog "machine safety available_mb=$($safety.available_memory_mb)/$($safety.total_memory_mb) codex_trees=$($safety.codex_process_trees) codex_rss_mb=$($safety.codex_resident_mb) pressure=$($safety.pressure)"
    if (-not [bool]$safety.pressure) {
        return $false
    }

    $effectiveRootId = ""
    if ($null -ne $State -and $State.root_id) {
        $effectiveRootId = [string]$State.root_id
    } elseif ($RootId) {
        $effectiveRootId = $RootId
    }
    if (-not $effectiveRootId) {
        Write-WatchdogLog "machine safety pressure detected but no root_id is available; resume skipped reason=$($safety.reason)"
        return $true
    }

    try {
        $payload = [ordered]@{
            root_id = $effectiveRootId
            resume_after_seconds = [Math]::Max(300, $MachinePressureResumeAfterSeconds)
            reason = "Machine safety guard: $($safety.reason). Saved state and paused new Codex starts to protect the local machine."
            hibernate_tree = $true
        }
        $body = $payload | ConvertTo-Json -Depth 4
        Invoke-RestMethod -Uri "${ApiUrl}/api/budget-guard" -Method Post -ContentType "application/json; charset=utf-8" -Body $body -TimeoutSec 20 | Out-Null
        Write-WatchdogLog "machine safety guard hibernated root=$effectiveRootId reason=$($safety.reason)"
    } catch {
        Write-WatchdogLog "machine safety guard failed to hibernate root=$effectiveRootId error=$($_.Exception.Message)"
    }
    return $true
}

function Invoke-StalledAgentCleanupIfNeeded {
    param(
        $State,
        [string]$EffectiveRootId
    )

    if (-not $EffectiveRootId -or $null -eq $State -or $null -eq $State.active_agent_diagnostics) {
        return $false
    }
    $stalled = @($State.active_agent_diagnostics | Where-Object { [string]$_.severity -eq "stalled" })
    if ($stalled.Count -le 0) {
        return $false
    }
    try {
        $uri = "${ApiUrl}/api/watchdog/cleanup-stalled?root_id=$([uri]::EscapeDataString($EffectiveRootId))"
        $result = Invoke-RestMethod -Uri $uri -Method Post -TimeoutSec 30
        $requeued = 0
        if ($null -ne $result -and $null -ne $result.requeued) {
            $requeued = [int]$result.requeued
        }
        if ($requeued -gt 0) {
            Write-WatchdogLog "stalled agent cleanup requeued=$requeued stalled=$($result.stalled) root=$EffectiveRootId"
            return $true
        }
        Write-WatchdogLog "stalled agent cleanup checked root=$EffectiveRootId stalled=$($stalled.Count) requeued=0"
        return $false
    } catch {
        Write-WatchdogLog "stalled agent cleanup failed root=$EffectiveRootId stalled=$($stalled.Count) error=$($_.Exception.Message)"
        return $false
    }
}

function Get-LegacyTreeState {
    param([string]$CandidateRootId)
    $uri = "$ApiUrl/api/tree?compact_done=true"
    if ($CandidateRootId) {
        $uri = "${uri}&root_id=$([uri]::EscapeDataString($CandidateRootId))"
    }
    $tree = Invoke-RestMethod -Uri $uri -TimeoutSec 8
    $root = @($tree.tasks | Where-Object { $null -eq $_.parent_id } | Select-Object -First 1)
    $nowUtc = (Get-Date).ToUniversalTime()
    $resumeDue = $false
    $resumeAt = $null
    $reason = ""
    foreach ($task in @($tree.tasks | Where-Object { $_.status -eq "hibernating" })) {
        $hibernation = $task.node_meta.hibernation
        if ($hibernation -and $hibernation.resume_at) {
            try {
                $candidateResumeAt = ([datetime]::Parse($hibernation.resume_at)).ToUniversalTime()
                if ($null -eq $resumeAt -or $candidateResumeAt -lt $resumeAt) {
                    $resumeAt = $candidateResumeAt
                }
                if ($candidateResumeAt -le $nowUtc) {
                    $resumeDue = $true
                }
            } catch {
                $resumeDue = $true
            }
            if (-not $reason -and $hibernation.reason) {
                $reason = $hibernation.reason
            }
        }
    }
    $activeFrontierStatuses = @(
        "planned",
        "planning",
        "decomposing",
        "queued",
        "waiting_children",
        "running",
        "working",
        "verifying",
        "accepting",
        "repair"
    )
    $parentIds = @{}
    foreach ($task in @($tree.tasks)) {
        if ($task.parent_id) {
            $parentIds[[string]$task.parent_id] = $true
        }
    }
    $activeFrontier = @($tree.tasks | Where-Object {
        -not $parentIds.ContainsKey([string]$_.id) -and ([string]$_.status) -in $activeFrontierStatuses
    }).Count
    if ($null -ne $tree.stats.active_frontier -and "$($tree.stats.active_frontier)" -ne "") {
        $activeFrontier = [int]$tree.stats.active_frontier
    }
    $codexSlotsActive = [int]$tree.stats.active_agents
    if ($null -ne $tree.stats.codex_slots_active -and "$($tree.stats.codex_slots_active)" -ne "") {
        $codexSlotsActive = [int]$tree.stats.codex_slots_active
    } else {
        try {
            $codexSlotsActive = [Math]::Max($codexSlotsActive, (Get-CodexBattlefieldProcessGroups).Count)
        } catch {
        }
    }
    $codexSlotsCapacity = [Math]::Max(1, [int]$MaxConcurrency)
    if ($null -ne $tree.stats.codex_slots_capacity -and "$($tree.stats.codex_slots_capacity)" -ne "") {
        $codexSlotsCapacity = [int]$tree.stats.codex_slots_capacity
    }
    return [pscustomobject]@{
        root_id = if ($root) { $root.id } else { $CandidateRootId }
        requested_root_id = $CandidateRootId
        selection = "legacy_tree"
        root_status = if ($root) { $root.status } else { "" }
        active_agents = [int]$tree.stats.active_agents
        codex_slots_active = $codexSlotsActive
        codex_slots_capacity = $codexSlotsCapacity
        active_frontier = $activeFrontier
        hibernating = [int]$tree.stats.hibernating
        resume_due = $resumeDue
        should_resume = ($resumeDue -and [int]$tree.stats.active_agents -eq 0)
        resume_at = $resumeAt
        reason = $reason
        agent_burst_used = [int]$tree.stats.agent_burst_used
        agent_burst_limit = [int]$tree.stats.agent_burst_limit
        agent_burst_reserve = [int]$tree.stats.agent_burst_reserve
        agent_burst_work_limit = [int]$tree.stats.agent_burst_work_limit
        agent_burst_remaining = [int]$tree.stats.agent_burst_remaining
        goal_mode = [bool]$tree.stats.goal_mode
        recommended_action = if ($resumeDue) { "resume" } else { "observe" }
    }
}

if (-not (Test-Endpoint "$ApiUrl/api/health")) {
    Stop-DuplicateBattlefieldApiProcesses
    if (Test-Path $ApiRestartFlagPath) {
        Write-WatchdogLog "api health failed but restart drain is pending; skipping eager API start"
    } else {
        Start-BattlefieldApi
        Start-Sleep -Seconds 5
    }
}

Stop-DuplicateBattlefieldApiProcesses
Stop-StaleCodexProcessTrees

if (-not $SkipWeb -and -not (Test-Endpoint $WebUrl)) {
    Start-BattlefieldWeb
    Start-Sleep -Seconds 3
}

if ($RootId) {
    try {
        $stateRootId = $RootId
        $state = Get-WatchdogState -CandidateRootId $stateRootId
        if ($null -eq $state -and -not $DisableAutoDiscover) {
            $state = Get-WatchdogState -CandidateRootId ""
        }
        if ($null -eq $state) {
            $state = Get-LocalWatchdogState -CandidateRootId $stateRootId
        }
        if ($null -eq $state) {
            try {
                $state = Get-LegacyTreeState -CandidateRootId $stateRootId
            } catch {
                Write-WatchdogLog "legacy tree state failed root=$stateRootId error=$($_.Exception.Message)"
                $state = $null
            }
        }
        if ($null -eq $state) {
            throw "watchdog state unavailable for root=$stateRootId"
        }
        $effectiveRootId = $state.root_id
        if (-not $effectiveRootId) {
            $effectiveRootId = $RootId
        }

        if (Restart-ApiIfIdleRequested -State $state) {
            Start-Sleep -Seconds 2
            $state = Get-WatchdogState -CandidateRootId $effectiveRootId
            if ($null -eq $state) {
                $state = Get-LocalWatchdogState -CandidateRootId $effectiveRootId
            }
            if ($null -eq $state) {
                try {
                    $state = Get-LegacyTreeState -CandidateRootId $effectiveRootId
                } catch {
                    Write-WatchdogLog "legacy tree state after API restart failed root=$effectiveRootId error=$($_.Exception.Message)"
                    $state = $null
                }
            }
        }

        Invoke-CodexHealthScanIfNeeded -EffectiveRootId $effectiveRootId | Out-Null

        if (Invoke-MachineSafetyGuard -State $state) {
            Write-WatchdogLog "machine safety guard held root=$effectiveRootId; skipping resume this cycle"
            Exit-WatchdogSingleInstance
            return
        }

        if (Invoke-StalledAgentCleanupIfNeeded -State $state -EffectiveRootId $effectiveRootId) {
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
        }

        if ([bool]$state.should_resume -and $effectiveRootId) {
            $resumed = Resume-TreeIfCodexReady -EffectiveRootId $effectiveRootId
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
            if ($resumed) {
                Write-WatchdogLog "resume hook fired for root=$effectiveRootId"
            }
        }

        if (Invoke-RetryableBlockRepairIfNeeded -EffectiveRootId $effectiveRootId) {
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
        }
        if (Invoke-BlockerRecoveryDependencyRepairIfNeeded -EffectiveRootId $effectiveRootId) {
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
        }

        Invoke-LocalKickDrainIfToolHeadChanged -EffectiveRootId $effectiveRootId | Out-Null
        if (Stop-DrainedLocalKickSchedulers -EffectiveRootId $effectiveRootId) {
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
        }
        if (Invoke-SlotTopOffIfNeeded -State $state -EffectiveRootId $effectiveRootId) {
            Start-Sleep -Seconds 2
            $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $effectiveRootId
        }
        Invoke-FullCheckpointIfToolHeadChanged -EffectiveRootId $effectiveRootId | Out-Null
        Invoke-CheckpointLedgerAnchorIfNeeded -EffectiveRootId $effectiveRootId | Out-Null

        $workLimit = $state.agent_burst_work_limit
        if ($null -eq $workLimit -or "$workLimit" -eq "") {
            $workLimit = $state.agent_burst_limit
        }
        $reserve = $state.agent_burst_reserve
        if ($null -eq $reserve -or "$reserve" -eq "") {
            $reserve = $AgentBurstReserve
        }
        Write-WatchdogLog "root=$effectiveRootId selection=$($state.selection) goal_mode=$($state.goal_mode) status=$($state.root_status) action=$($state.recommended_action) active_agents=$($state.active_agents) hibernating=$($state.hibernating) resume_due=$($state.resume_due) burst=$($state.agent_burst_used)/$workLimit reserve=$reserve limit=$($state.agent_burst_limit) remaining=$($state.agent_burst_remaining)"
    } catch {
        Write-WatchdogLog "watchdog started services, but tree read failed: $($_.Exception.Message)"
    }
} else {
    try {
        $state = $null
        if (-not $DisableAutoDiscover) {
            $state = Get-WatchdogState -CandidateRootId ""
        }
        if ($null -eq $state) {
            $state = Get-LocalWatchdogState -CandidateRootId ""
        }
        if ($null -ne $state -and $state.root_id) {
            if (Restart-ApiIfIdleRequested -State $state) {
                Start-Sleep -Seconds 2
                $state = Get-WatchdogState -CandidateRootId $state.root_id
                if ($null -eq $state) {
                    $state = Get-LocalWatchdogState -CandidateRootId ""
                }
            }
            Invoke-CodexHealthScanIfNeeded -EffectiveRootId $state.root_id | Out-Null
            if (Invoke-MachineSafetyGuard -State $state) {
                Write-WatchdogLog "machine safety guard held root=$($state.root_id); skipping resume this cycle"
                Exit-WatchdogSingleInstance
                return
            }
            if (Invoke-StalledAgentCleanupIfNeeded -State $state -EffectiveRootId $state.root_id) {
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            if ([bool]$state.should_resume) {
                $resumed = Resume-TreeIfCodexReady -EffectiveRootId $state.root_id
                if ($resumed) {
                    Write-WatchdogLog "resume hook fired for auto-discovered root=$($state.root_id)"
                }
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            if (Invoke-RetryableBlockRepairIfNeeded -EffectiveRootId $state.root_id) {
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            if (Invoke-BlockerRecoveryDependencyRepairIfNeeded -EffectiveRootId $state.root_id) {
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            Invoke-LocalKickDrainIfToolHeadChanged -EffectiveRootId $state.root_id | Out-Null
            if (Stop-DrainedLocalKickSchedulers -EffectiveRootId $state.root_id) {
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            if (Invoke-SlotTopOffIfNeeded -State $state -EffectiveRootId $state.root_id) {
                Start-Sleep -Seconds 2
                $state = Refresh-WatchdogStateAfterAction -PreviousState $state -EffectiveRootId $state.root_id
            }
            Invoke-FullCheckpointIfToolHeadChanged -EffectiveRootId $state.root_id | Out-Null
            Invoke-CheckpointLedgerAnchorIfNeeded -EffectiveRootId $state.root_id | Out-Null
            if ($null -ne $state) {
                Write-WatchdogLog "root=$($state.root_id) selection=$($state.selection) goal_mode=$($state.goal_mode) status=$($state.root_status) action=$($state.recommended_action) active_agents=$($state.active_agents) hibernating=$($state.hibernating) resume_due=$($state.resume_due) burst=$($state.agent_burst_used)/$($state.agent_burst_work_limit) reserve=$($state.agent_burst_reserve) limit=$($state.agent_burst_limit) remaining=$($state.agent_burst_remaining)"
            } else {
                Write-WatchdogLog "Codex Battlefield watchdog checked API=$ApiUrl Web=$WebUrl; auto-discovered root state was unavailable after action"
            }
        } else {
            Write-WatchdogLog "Codex Battlefield watchdog checked API=$ApiUrl Web=$WebUrl; no active root discovered"
        }
    } catch {
        Write-WatchdogLog "Codex Battlefield watchdog checked API=$ApiUrl Web=$WebUrl; auto-discovery failed: $($_.Exception.Message)"
    }
}

Exit-WatchdogSingleInstance
