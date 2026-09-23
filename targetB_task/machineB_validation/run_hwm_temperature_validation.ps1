param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_temperature.json",
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

$report = New-ValidationReport -category "HWM.Temperature" -configPath $ConfigPath
$initialized = $false

try {
    # [RUNNER SKELETON] 1) config + section gate
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir

    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'HWM.Temperature') {
        throw "source_ini.section must be HWM.Temperature, got '$sourceSection'."
    }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $tempSection = Get-IniSection -Sections $sections -Name 'HWM.Temperature'
    if ($null -eq $tempSection) {
        throw "INI does not contain [HWM.Temperature]."
    }

    $required = @($config.required_channels)
    if ($required.Count -eq 0) {
        throw 'required_channels is empty.'
    }

    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})

    $sampleCfg = Get-ConfigValue -Config $config -Name 'read_check' -Default ([ordered]@{})
    $sampleCount = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_count' (Get-ConfigValue -Config $config -Name 'sample_count' 10))
    $sampleIntervalMs = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_interval_ms' (Get-ConfigValue -Config $config -Name 'sample_interval_ms' 1000))
    $minSuccessRate = [double](Get-ConfigValue -Config $sampleCfg -Name 'min_success_rate' (Get-ConfigValue -Config $config -Name 'minimum_success_rate' 0.9))
    $minCelsius = [double](Get-ConfigValue -Config $sampleCfg -Name 'min_celsius' (Get-ConfigValue -Config $config -Name 'min_celsius' -40.0))
    $maxCelsius = [double](Get-ConfigValue -Config $sampleCfg -Name 'max_celsius' (Get-ConfigValue -Config $config -Name 'max_celsius' 125.0))
    $maxSpan = [double](Get-ConfigValue -Config $sampleCfg -Name 'max_span_celsius' (Get-ConfigValue -Config $config -Name 'max_span_celsius' 30.0))

    if ($sampleCount -le 0 -or $sampleIntervalMs -le 0) {
        throw "Invalid sampling setup: sample_count=$sampleCount, sample_interval_ms=$sampleIntervalMs"
    }

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $iniResolved
        section = $sourceSection
        sha256 = $actualHash
    }
    $report.metrics.thresholds = [ordered]@{
        sample_count = $sampleCount
        sample_interval_ms = $sampleIntervalMs
        min_success_rate = $minSuccessRate
        min_celsius = $minCelsius
        max_celsius = $maxCelsius
        max_span_celsius = $maxSpan
    }
    $report.validation_layers.L1_configuration = 'PASS'

    # [RUNNER SKELETON] 2) initialize + capability gate
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    $report.validation_layers.L2_capability = 'PASS'

    # [RUNNER SKELETON] 3) per-channel sampling loop
    $channelMetrics = [ordered]@{}
    $apiFailures = @()
    $readbackFailures = @()

    foreach ($ch in $required) {
        $iniKey = @($tempSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) {
            throw "Required channel $ch is missing from [HWM.Temperature]."
        }

        $tuple = [string]$tempSection[$iniKey[0]]
        if ([string]::IsNullOrWhiteSpace($tuple)) {
            throw "Required channel $ch has empty tuple in [HWM.Temperature]."
        }
        $parts = @($tuple.Split(',') | ForEach-Object { $_.Trim() })
        if ($parts.Count -lt 4) {
            throw "Required channel $ch tuple has $($parts.Count) fields; expected at least 4."
        }

        $chMeta = $null
        if ($channelDefs -is [System.Collections.IDictionary] -and $channelDefs.Contains([string]$ch)) {
            $chMeta = $channelDefs[[string]$ch]
        }

        $apiIdText = if ($null -ne $chMeta) { [string](Get-ConfigValue -Config $chMeta -Name 'api_id' -Default $parts[1]) } else { $parts[1] }
        $apiId = Convert-ToUInt32Safe -Text $apiIdText -Default 0
        if ($apiId -eq 0) {
            $apiFailures += $ch
            $channelMetrics[$ch] = [ordered]@{
                tuple = $tuple
                api_id = $apiIdText
                status = 'INVALID_CHANNEL_ID'
            }
            continue
        }

        $series = Sample-Channel -id $apiId -count $sampleCount -intervalMs $sampleIntervalMs -decode { param($raw) Decode-TempC $raw }
        foreach ($s in $series) {
            $statusCode = if ($null -eq $s.status) {
                [UInt32]0
            } else {
                Convert-ToUInt32Safe -Text ([string]$s.status_code) -Default ([UInt32]::MaxValue)
            }
            Add-ApiCall -report $report -name ("TempGet:{0}" -f $ch) -status $statusCode -value $s.raw
        }

        $successCount = @($series | Where-Object { $_.value -ne $null }).Count
        $successRate = $successCount / [double]$sampleCount
        $stats = Get-SeriesStats -series $series
        $inRange = ($stats.count -gt 0 -and $stats.min -ge $minCelsius -and $stats.max -le $maxCelsius)
        $spanOk = ($stats.count -gt 0 -and $stats.span -le $maxSpan)

        if ($successRate -lt $minSuccessRate) {
            $apiFailures += $ch
        }
        if (-not $inRange -or -not $spanOk) {
            $readbackFailures += $ch
        }

        $channelMetrics[$ch] = [ordered]@{
            tuple = $tuple
            api_id = ('0x{0:X8}' -f [UInt32]$apiId)
            success_count = $successCount
            success_rate = [Math]::Round($successRate, 4)
            stats_celsius = $stats
            in_range = $inRange
            span_ok = $spanOk
            samples = $series
        }
    }

    $report.metrics.channels = $channelMetrics

    if ($apiFailures.Count -gt 0) {
        $report.result = 'FAIL_API'
        $failedApiChannels = @($apiFailures | Select-Object -Unique | Sort-Object)
        $report.reason = ('Temperature API/read mapping failed for channel(s): ' + ($failedApiChannels -join ', '))
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = 'FAIL'
        $report.validation_layers.L5_functional = 'FAIL'
        $report.checks.read_stability = 'FAIL'
        $report.checks.control_effect = 'NOT_RUN'
    } elseif ($readbackFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $failedReadbackChannels = @($readbackFailures | Select-Object -Unique | Sort-Object)
        $report.reason = ('Temperature out-of-range/unstable span for channel(s): ' + ($failedReadbackChannels -join ', '))
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
        $report.validation_layers.L5_functional = 'FAIL'
        $report.checks.read_stability = 'FAIL'
        $report.checks.control_effect = 'NOT_RUN'
    } else {
        # [RUNNER SKELETON] 4) verdict mapping + result breakdown
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'PASS'
        $report.validation_layers.L5_functional = 'CONDITIONAL'
        $report.validation_layers.L6_recovery = 'N_A'
        $report.checks.read_stability = 'PASS'
        $report.checks.control_effect = 'NOT_APPLICABLE'
        $report.checks.recovery = 'NOT_REQUIRED'
        $report.result = 'CONDITIONAL'
        $report.reason = 'Read-only temperature validation passed; no thermal stimulus profile applied.'
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
        $meta = $report.metrics.channels[$ch]
        if ($null -eq $meta.success_rate) {
            $failItems += "channel:$ch"
            continue
        }
        if ([double]$meta.success_rate -le 0) {
            $failItems += "channel:$ch"
            continue
        }
        if ($meta.in_range -and $meta.span_ok) {
            $passItems += "channel:$ch"
        } else {
            $failItems += "channel:$ch"
        }
    }
}

$report.result_breakdown = [ordered]@{
    pass = $passItems
    pending = $pendingItems
    fail = $failItems
    na = $naItems
}

$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'hwm_temperature'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"

exit ([int]$verdict.exit_code)
