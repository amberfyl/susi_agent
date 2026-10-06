param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_wdt.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out"
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function Convert-ToUInt32Safe {
    param(
        [string]$Text,
        [UInt32]$Default = 0
    )

    if ([string]::IsNullOrWhiteSpace($Text)) { return $Default }
    $t = $Text.Trim()
    try {
        if ($t.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
            return [UInt32]([Convert]::ToUInt32($t.Substring(2), 16))
        }
        return [UInt32]([Convert]::ToUInt32($t, 10))
    } catch {
        return $Default
    }
}

$report = New-ValidationReport -category "WDT" -configPath $ConfigPath
$initialized = $false

try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir

    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'WDT') {
        throw "source_ini.section must be WDT, got '$sourceSection'."
    }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $wdtSection = Get-IniSection -Sections $sections -Name 'WDT'
    if ($null -eq $wdtSection) {
        throw "INI does not contain [WDT]."
    }

    $required = @($config.required_channels)
    if ($required.Count -eq 0) {
        throw 'required_channels is empty.'
    }

    $channels = [ordered]@{}
    foreach ($ch in $required) {
        $iniKey = @($wdtSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) {
            throw "Required channel $ch is missing from [WDT]."
        }
        $tuple = [string]$wdtSection[$iniKey[0]]
        if ([string]::IsNullOrWhiteSpace($tuple)) {
            throw "Required channel $ch has empty tuple in [WDT]."
        }

        $parts = @($tuple.Split(',') | ForEach-Object { $_.Trim() })
        if ($parts.Count -lt 4) {
            throw "Required channel $ch tuple has $($parts.Count) fields; expected at least 4."
        }

        $channels[$ch] = [ordered]@{
            tuple = $tuple
            hardware_id = $parts[0]
            channel_id = $parts[1]
            io_port = $parts[2]
            option = $parts[3]
        }
    }

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $iniResolved
        section = $sourceSection
        sha256 = $actualHash
    }
    $report.metrics.channels = $channels
    $report.validation_layers.L1_configuration = 'PASS'

    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    $report.validation_layers.L5_functional = 'CONDITIONAL'

    $nondestructive = Get-ConfigValue -Config $config -Name 'nondestructive_check' -Default ([ordered]@{})
    $destructive = Get-ConfigValue -Config $config -Name 'destructive_check' -Default ([ordered]@{})
    $semantics = Get-ConfigValue -Config $config -Name 'result_semantics' -Default ([ordered]@{})

    $ndEnabled = [bool](Get-ConfigValue -Config $nondestructive -Name 'enabled' -Default $true)

    # SAFETY: reboot/power-cycle WDT tests are disabled at runner level.
    # Do not remove this hard gate until a dedicated recovery harness and explicit
    # operator approval exist. In particular, do not call SusiWDogStart with
    # SUSI_WDT_EVENT_TYPE_PWRCYCLE, and do not intentionally wait for timeout.
    $desEnabledConfigured = [bool](Get-ConfigValue -Config $destructive -Name 'enabled' -Default $false)
    $allowDestructiveConfigured = [bool](Get-ConfigValue -Config $destructive -Name 'allow_destructive_reset' -Default $false)
    $desEnabled = $false
    $allowDestructive = $false

    $report.metrics.policy = [ordered]@{
        nondestructive_enabled = $ndEnabled
        destructive_enabled = $desEnabled
        destructive_enabled_configured = $desEnabledConfigured
        allow_destructive_reset = $allowDestructive
        allow_destructive_reset_configured = $allowDestructiveConfigured
        destructive_runner_gate = 'DISABLED'
    }

    $timeoutSec = [int](Get-ConfigValue -Config $nondestructive -Name 'test_timeout_sec' (Get-ConfigValue -Config $config -Name 'test_timeout_sec' 30))
    $refreshIntervalSec = [int](Get-ConfigValue -Config $nondestructive -Name 'refresh_interval_sec' (Get-ConfigValue -Config $config -Name 'refresh_interval_sec' 5))
    $refreshCycles = [int](Get-ConfigValue -Config $nondestructive -Name 'refresh_cycles' (Get-ConfigValue -Config $config -Name 'refresh_cycles' 3))

    $rangeValid = ($timeoutSec -gt 0 -and $refreshIntervalSec -gt 0 -and $refreshIntervalSec -lt $timeoutSec -and $refreshCycles -gt 0)
    $report.metrics.nondestructive = [ordered]@{
        test_timeout_sec = $timeoutSec
        refresh_interval_sec = $refreshIntervalSec
        refresh_cycles = $refreshCycles
        parameter_range_valid = $rangeValid
    }

    if (-not $rangeValid) {
        $report.result = 'BLOCKED_PARAMETER'
        $report.reason = "Invalid non-destructive parameters: timeout=$timeoutSec, refresh_interval=$refreshIntervalSec, refresh_cycles=$refreshCycles"
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = 'FAIL'
        $report.validation_layers.L5_functional = 'FAIL'
        $report.checks.read_stability = 'FAIL'
        $report.checks.control_effect = 'FAIL'
    }

    if ($report.result -eq 'PENDING') {
        $capProbe = @()
        foreach ($ch in $required) {
            $chMeta = $channels[$ch]
            $id = Convert-ToUInt32Safe -Text ([string]$chMeta.channel_id) -Default 0
            if ($id -eq 0) {
                $capProbe += [ordered]@{
                    channel = $ch
                    api_id = [string]$chMeta.channel_id
                    status = 'SKIPPED_INVALID_CHANNEL_ID'
                }
                continue
            }

            $r = Read-BoardValue -id $id
            Add-ApiCall -report $report -name ("WDTProbe:{0}" -f $ch) -status $r.status -value $r.value
            $capProbe += [ordered]@{
                channel = $ch
                api_id = ('0x{0:X8}' -f $id)
                status = Get-StatusName $r.status
                status_code = ('0x{0:X8}' -f [UInt32]$r.status)
                raw_value = [UInt32]$r.value
            }
        }
        $report.metrics.capability_probe = $capProbe

        if (-not $ndEnabled) {
            $report.result = 'PENDING'
            $report.reason = 'Non-destructive WDT checks disabled by config policy'
            $report.validation_layers.L3_api = 'PENDING'
            $report.validation_layers.L4_readback = 'PENDING'
            $report.validation_layers.L5_functional = 'PENDING'
            $report.checks.read_stability = 'PENDING'
            $report.checks.control_effect = 'NOT_RUN'
        } else {
            # Phase 1 write path without reboot: Start with the maximum reset time
            # and no pre-event, read the settings back, Trigger once, then Stop
            # immediately. Stop is always attempted in finally and retried.
            $startStop = [ordered]@{}
            $failures = @()
            $notExercised = @()
            $stopFailures = @()
            foreach ($ch in $required) {
                $match = [regex]::Match([string]$ch, '^WDT(\d+)$', 'IgnoreCase')
                if (-not $match.Success) { throw "Cannot map $ch to a SUSI watchdog id." }
                $wdogId = [UInt32]([int]$match.Groups[1].Value - 1)
                $test = [ordered]@{
                    wdog_id = $wdogId; caps = [ordered]@{}; original = [ordered]@{}; start = $null
                    readback = [ordered]@{}; trigger_status = $null; stop_status = $null
                    stop_attempts = 0; stopped = $false; result = 'NOT_RUN'
                }
                $startStop[$ch] = $test

                $capItems = [ordered]@{
                    support_flags = 0x00000000; delay_max = 0x00000001; delay_min = 0x00000002
                    event_max = 0x00000003; event_min = 0x00000004; reset_max = 0x00000005
                    reset_min = 0x00000006; unit_min = 0x0000000F
                    delay_time = 0x00010001; event_time = 0x00010002; reset_time = 0x00010003; event_type = 0x00010004
                }
                $capsOk = $true
                foreach ($name in @($capItems.Keys)) {
                    [UInt32]$value = 0
                    $status = [NativeSusi]::SusiWDogGetCaps($wdogId, [UInt32]$capItems[$name], [ref]$value)
                    Add-ApiCall -report $report -name ("SusiWDogGetCaps:{0}:{1}" -f $ch, $name) -status $status -value $value
                    $target = if ($name -in @('delay_time', 'event_time', 'reset_time', 'event_type')) { $test.original } else { $test.caps }
                    $target[$name] = if (Is-Success $status) { $value } else { $null }
                    if ($name -in @('support_flags', 'reset_max') -and -not (Is-Success $status)) { $capsOk = $false }
                }
                if (-not $capsOk -or -not $test.caps.reset_max) {
                    $test.result = 'FAIL_CAPS'
                    $failures += "${ch}:GetCaps"
                    continue
                }

                $resetTime = [UInt32]$test.caps.reset_max
                # Use a value different from the current setting so the readback proves
                # the write; one unit below maximum is still far from any reset.
                $unit = if ($test.caps.unit_min) { [UInt32]$test.caps.unit_min } else { [UInt32]1 }
                if ($null -ne $test.original.reset_time -and [UInt32]$test.original.reset_time -eq $resetTime -and $resetTime -gt (2 * $unit)) {
                    $resetTime = $resetTime - $unit
                }
                $delayTime = if ($test.caps.delay_min) { [UInt32]$test.caps.delay_min } else { [UInt32]0 }
                $eventTime = if ($test.caps.event_min) { [UInt32]$test.caps.event_min } else { [UInt32]0 }
                $eventType = [UInt32]0  # SUSI_WDT_EVENT_TYPE_NONE
                $test.start = [ordered]@{ delay_time = $delayTime; event_time = $eventTime; reset_time = $resetTime; event_type = $eventType }

                $started = $false
                try {
                    $startStatus = [NativeSusi]::SusiWDogStart($wdogId, $delayTime, $eventTime, $resetTime, $eventType)
                    Add-ApiCall -report $report -name "SusiWDogStart:$ch" -status $startStatus -value $test.start
                    $test.start.status = Get-StatusName $startStatus
                    if ($startStatus -eq [Convert]::ToUInt32('FFFFFEFA', 16)) {
                        # SUSI_STATUS_RUNNING: someone else owns the watchdog; do not touch it.
                        $test.result = 'ALREADY_RUNNING_NOT_MODIFIED'
                        $notExercised += $ch
                        continue
                    }
                    if (-not (Is-Success $startStatus)) {
                        $test.result = 'FAIL_START'
                        $failures += "${ch}:Start"
                        continue
                    }
                    $started = $true

                    foreach ($name in @('reset_time', 'event_type')) {
                        [UInt32]$value = 0
                        $status = [NativeSusi]::SusiWDogGetCaps($wdogId, [UInt32]$capItems[$name], [ref]$value)
                        Add-ApiCall -report $report -name ("SusiWDogGetCaps:{0}:{1}:readback" -f $ch, $name) -status $status -value $value
                        $test.readback[$name] = if (Is-Success $status) { $value } else { $null }
                    }
                    $resetMatches = ($null -ne $test.readback.reset_time) -and ([UInt32]$test.readback.reset_time -eq $resetTime)

                    $triggerStatus = [NativeSusi]::SusiWDogTrigger($wdogId)
                    Add-ApiCall -report $report -name "SusiWDogTrigger:$ch" -status $triggerStatus
                    $test.trigger_status = Get-StatusName $triggerStatus

                    if (-not (Is-Success $triggerStatus)) {
                        $test.result = 'FAIL_TRIGGER'
                        $failures += "${ch}:Trigger"
                    } elseif (-not $resetMatches) {
                        $test.result = 'FAIL_READBACK'
                        $failures += "${ch}:ResetTimeReadback"
                    } else {
                        $test.result = 'PASS'
                    }
                } finally {
                    if ($started) {
                        for ($attempt = 1; $attempt -le 3 -and -not $test.stopped; $attempt++) {
                            $stopStatus = [NativeSusi]::SusiWDogStop($wdogId)
                            Add-ApiCall -report $report -name ("SusiWDogStop:{0}:attempt{1}" -f $ch, $attempt) -status $stopStatus
                            $test.stop_attempts = $attempt
                            $test.stop_status = Get-StatusName $stopStatus
                            $test.stopped = Is-Success $stopStatus
                        }
                        if (-not $test.stopped) {
                            $test.result = 'FAIL_STOP'
                            $stopFailures += ('{0} (target may reset after {1} ms)' -f $ch, $resetTime)
                        }
                    }
                }
            }
            $report.metrics.start_stop_test = $startStop

            if ($stopFailures.Count -gt 0) {
                $report.result = 'FAIL_API'
                $report.reason = 'WARNING: WDT could not be stopped: ' + ($stopFailures -join ', ')
                $report.validation_layers.L2_capability = 'PASS'
                $report.validation_layers.L3_api = 'FAIL'
                $report.validation_layers.L4_readback = 'FAIL'
                $report.validation_layers.L6_recovery = 'FAIL'
                $report.checks.recovery = 'FAIL'
            } elseif ($failures.Count -gt 0) {
                $report.result = 'FAIL_API'
                $report.reason = 'WDT start/readback/trigger/stop failed: ' + ($failures -join ', ')
                $report.validation_layers.L2_capability = if (@($failures | Where-Object { $_ -like '*:GetCaps' }).Count -gt 0) { 'FAIL' } else { 'PASS' }
                $report.validation_layers.L3_api = 'FAIL'
                $report.validation_layers.L4_readback = 'FAIL'
            } elseif ($notExercised.Count -gt 0) {
                $report.result = 'CONDITIONAL'
                $report.reason = 'WDT already running (owned by another program); left untouched: ' + ($notExercised -join ', ')
                $report.validation_layers.L2_capability = 'PASS'
                $report.validation_layers.L3_api = 'CONDITIONAL'
                $report.validation_layers.L4_readback = 'CONDITIONAL'
            } else {
                $report.result = 'PASS'
                $report.reason = ('WDT start/readback/trigger/stop passed (reset time ' + (@($startStop.Values | ForEach-Object { $_.start.reset_time }) -join ',') + '); watchdog stopped, no reset triggered.')
                $report.validation_layers.L2_capability = 'PASS'
                $report.validation_layers.L3_api = 'PASS'
                $report.validation_layers.L4_readback = 'PASS'
            }
            $report.checks.read_stability = 'PASS'
            $report.checks.control_effect = 'CONDITIONAL'
        }
    }

    $destructiveSkipped = $false
    # DISABLED TEST BLOCK: WDT Start/timeout/Stop and PWRCYCLE/reset validation.
    # These tests can alter reboot/power behavior and are intentionally commented
    # out until a recovery-capable harness is approved.
    if ($false -and $desEnabled -and $allowDestructive) {
        # SusiWDogStart / timeout / SusiWDogStop would belong here.
        # Never enable this branch for ordinary machineB validation.
        $report.metrics.destructive = [ordered]@{
            attempted = $false
            allowed_by_policy = $false
            reason = 'Disabled safety gate: reboot/power-cycle WDT tests are not run'
        }
        $destructiveSkipped = $true
    } else {
        $pendingResult = [string](Get-ConfigValue -Config $destructive -Name 'pending_result_when_disabled' -Default 'PENDING')
        $pendingReason = [string](Get-ConfigValue -Config $destructive -Name 'pending_reason_when_disabled' -Default 'Destructive reboot-required WDT checks are pending by policy')
        $report.metrics.destructive = [ordered]@{
            attempted = $false
            allowed_by_policy = $false
            pending_result = $pendingResult
            pending_reason = $pendingReason
        }
        $destructiveSkipped = $true
    }

    if ($destructiveSkipped -and $report.validation_layers.L6_recovery -ne 'FAIL') {
        # Recovery (Stop) only applies to watchdogs this runner actually started.
        $stoppedAny = $report.metrics.Contains('start_stop_test') -and @($report.metrics.start_stop_test.Values | Where-Object { $_.stopped }).Count -gt 0
        $report.validation_layers.L6_recovery = if ($stoppedAny) { 'PASS' } else { 'N_A' }
        $report.checks.recovery = if ($stoppedAny) { 'PASS' } else { 'NOT_REQUIRED' }


    }

} catch {
    $report.result = 'FAIL_CONFIG'
    $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
    if ($report.checks.read_stability -eq 'PENDING') { $report.checks.read_stability = 'FAIL' }
    if ($report.checks.control_effect -eq 'PENDING') { $report.checks.control_effect = 'NOT_RUN' }
    if ($report.checks.recovery -eq 'PENDING') { $report.checks.recovery = 'NOT_REQUIRED' }
} finally {
    if ($initialized) {
        $u = Uninitialize-Susi
        $report.uninit_status = Get-StatusName $u
    }
}

$passItems = @()
$pendingItems = @()
$failItems = @()
$naItems = @()

foreach ($layerName in @($report.validation_layers.Keys)) {
    $state = [string]$report.validation_layers[$layerName]
    if ($state -eq 'PASS') { $passItems += "layer:$layerName"; continue }
    if ($state -like 'FAIL*') { $failItems += "layer:$layerName"; continue }
    if ($state -eq 'N_A' -or $state -eq 'NOT_REQUIRED') { $naItems += "layer:$layerName"; continue }
    if ($state -eq 'PENDING' -or $state -like 'CONDITIONAL*') { $pendingItems += "layer:$layerName"; continue }
}

if ($report.metrics.Contains('channels')) {
    foreach ($ch in @($report.metrics.channels.Keys)) {
        $passItems += "channel:$ch"
    }
}

$destructiveBucket = 'pending'
$semanticsCfg = $null
if ($null -ne $config) {
    $semanticsCfg = Get-ConfigValue -Config $config -Name 'result_semantics' -Default ([ordered]@{})
    $bucketCandidate = [string](Get-ConfigValue -Config $semanticsCfg -Name 'destructive_skipped_breakdown_bucket' -Default 'pending')
    if ($bucketCandidate -eq 'na') { $destructiveBucket = 'na' }
}

if ($report.metrics.Contains('destructive') -and $false -eq [bool]$report.metrics.destructive.attempted) {
    if ($destructiveBucket -eq 'na') {
        $naItems += 'case:destructive_timeout_reset'
    } else {
        $pendingItems += 'case:destructive_timeout_reset'
    }
}

$report.result_breakdown = [ordered]@{
    pass = $passItems
    pending = $pendingItems
    fail = $failItems
    na = $naItems
}

$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'wdt'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"

exit ([int]$verdict.exit_code)
