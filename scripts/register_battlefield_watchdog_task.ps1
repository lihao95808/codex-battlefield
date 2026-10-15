param(
    [string]$TaskName = "CodexBattlefieldWatchdog",
    [string]$LogonTaskName = "",
    [string]$RootId = "",
    [int]$IntervalMinutes = 1,
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
    [string]$CodexHome = "",
    [string]$CodexProfile = "",
    [string]$StateHome = "",
    [switch]$GoalMode,
    [switch]$SkipCodexPreflight,
    [switch]$WithLogonTrigger,
    [switch]$FixedConcurrency
)

$ErrorActionPreference = "Stop"
$Watchdog = (Resolve-Path (Join-Path $PSScriptRoot "battlefield_watchdog.ps1")).Path
if (-not $LogonTaskName) {
    $LogonTaskName = "$TaskName-Logon"
}

if ($GoalMode) {
    $AgentBurstLimit = 0
    $AgentBurstReserve = 0
}

$taskArgs = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$Watchdog`"",
    "-MaxConcurrency", "$MaxConcurrency",
    "-AgentBurstLimit", "$AgentBurstLimit",
    "-AgentBurstReserve", "$AgentBurstReserve",
    "-ResumeAfterSeconds", "$ResumeAfterSeconds",
    "-CodexPreflightTimeoutSeconds", "$CodexPreflightTimeoutSeconds",
    "-SafeMinAvailableMemoryMB", "$SafeMinAvailableMemoryMB",
    "-MaxCodexProcessTrees", "$MaxCodexProcessTrees",
    "-MaxCodexResidentMB", "$MaxCodexResidentMB",
    "-CompletedCodexGraceSeconds", "$CompletedCodexGraceSeconds",
    "-MachinePressureResumeAfterSeconds", "$MachinePressureResumeAfterSeconds",
    "-AutoConcurrencyQueueThreshold", "$AutoConcurrencyQueueThreshold",
    "-FuelDeadlineHour", "$FuelDeadlineHour",
    "-FuelTimezoneOffsetHours", "$FuelTimezoneOffsetHours",
    "-FuelSprintWindowMinutes", "$FuelSprintWindowMinutes"
)

if ($GoalMode) {
    $taskArgs += @("-GoalMode")
}

if ($SkipCodexPreflight) {
    $taskArgs += @("-SkipCodexPreflight")
}

if ($FixedConcurrency) {
    $taskArgs += @("-FixedConcurrency")
}

if ($RootId) {
    $taskArgs += @("-RootId", $RootId)
}

if ($CodexHome) {
    $taskArgs += @("-CodexHome", $CodexHome)
}

if ($CodexProfile) {
    $taskArgs += @("-CodexProfile", $CodexProfile)
}

# 状态根：**必须作为参数传下去**。计划任务不会继承任何 CODEX_BATTLEFIELD_*
# 环境变量（已核对），所以环境变量在任务里没用 —— 只能走命令行。
# 不传时看门狗自己按"环境变量 -> 用户主目录默认状态根"解析，仍能工作。
if ($StateHome) {
    $taskArgs += @("-StateHome", $StateHome)
}

$taskRun = "powershell.exe $($taskArgs -join ' ')"

function Install-StartupFolderHook {
    param([string]$HookName, [string]$CommandLine)

    $startupDir = [Environment]::GetFolderPath("Startup")
    if (-not $startupDir) {
        Write-Warning "Cannot locate the current user's Startup folder; logon fallback was not installed."
        return
    }

    $startupPath = Join-Path $startupDir "$HookName.cmd"
    Set-Content -Path $startupPath -Value @("@echo off", $CommandLine) -Encoding ASCII
    Write-Output "Installed Startup folder hook '$startupPath'."
    Write-Output "It runs at user logon without requiring scheduled-task elevation."
}

function Enable-TaskBatteryRun {
    param([string]$Name)

    try {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $task.Settings.DisallowStartIfOnBatteries = $false
        $task.Settings.StopIfGoingOnBatteries = $false
        Set-ScheduledTask -TaskName $Name -Settings $task.Settings | Out-Null
        Write-Output "Updated task power settings for '$Name' so watchdog can run on battery."
    } catch {
        Write-Warning "Could not update power settings for scheduled task '$Name': $($_.Exception.Message)"
    }
}

function Register-WatchdogScheduledTask {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [string]$CommandLine,
        [int]$EveryMinutes
    )

    try {
        $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument ($Arguments -join " ")
        $trigger = New-ScheduledTaskTrigger `
            -Once `
            -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        Register-ScheduledTask `
            -TaskName $Name `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -Force | Out-Null
        return
    } catch {
        Write-Warning "ScheduledTasks API registration failed for '$Name'; falling back to schtasks.exe: $($_.Exception.Message)"
    }

    & schtasks.exe /Create `
        /TN $Name `
        /SC MINUTE `
        /MO $EveryMinutes `
        /TR $CommandLine `
        /F | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks.exe failed to register '$Name' with exit code $LASTEXITCODE."
    }
}

Register-WatchdogScheduledTask `
    -Name $TaskName `
    -Arguments $taskArgs `
    -CommandLine $taskRun `
    -EveryMinutes $IntervalMinutes

Write-Output "Registered Windows scheduled task '$TaskName'."
Write-Output "It runs every $IntervalMinutes minutes and starts Codex Battlefield if needed."
Write-Output "If the configured root is finished, the watchdog can auto-discover the latest active root."
if ($GoalMode) {
    Write-Output "Goal mode is enabled: no agent burst ceiling will be applied by the watchdog-started API."
    Write-Output "L4 fuel control is enabled in goal mode unless FixedConcurrency is set; it can run up to $MaxConcurrency Codex CLI slots."
}
if ($FixedConcurrency) {
    Write-Output "Fixed concurrency is enabled: watchdog-started API will always use $MaxConcurrency Codex CLI slots."
} else {
    Write-Output "Auto throttle is enabled: watchdog-started API will use 1-$MaxConcurrency Codex CLI slots based on queue and resource pressure."
}
Write-Output "Machine safety guard is enabled: min available memory ${SafeMinAvailableMemoryMB}MB, max Codex process trees $MaxCodexProcessTrees, max Codex RSS ${MaxCodexResidentMB}MB."
Write-Output "Remove it later with: schtasks /Delete /TN $TaskName /F"
Enable-TaskBatteryRun -Name $TaskName

if ($WithLogonTrigger) {
    $oldErrorActionPreference = $ErrorActionPreference
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    if ($nativePreference) {
        $oldNativePreference = $PSNativeCommandUseErrorActionPreference
        $PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = "Continue"
        $logonOutput = & schtasks.exe /Create /TN $LogonTaskName /SC ONLOGON /TR $taskRun /F 2>&1
        $logonExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
        if ($nativePreference) {
            $PSNativeCommandUseErrorActionPreference = $oldNativePreference
        }
    }

    if ($logonExitCode -eq 0) {
        $logonOutput | Out-Host
        Write-Output "Registered Windows logon hook '$LogonTaskName'."
        Write-Output "It runs once at user logon, starts services if needed, then asks the watchdog state endpoint whether to resume."
        Write-Output "Remove it later with: schtasks /Delete /TN $LogonTaskName /F"
        Enable-TaskBatteryRun -Name $LogonTaskName
    } else {
        Write-Warning "Windows refused the ONLOGON scheduled task; installing a Startup folder fallback instead."
        Install-StartupFolderHook -HookName $LogonTaskName -CommandLine $taskRun
    }
}
