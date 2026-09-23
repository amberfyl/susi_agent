param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_caseopen.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out"
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function Convert-ToUInt32Safe {
    param([string]$Text, [UInt32]$Default = 0)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Default }
    try {
        $t = $Text.Trim()
        if ($t.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
            return [UInt32]([Convert]::ToUInt32($t.Substring(2), 16))
        }
        return [UInt32]([Convert]::ToUInt32($t, 10))
    } catch { return $Default }
}

$report = New-ValidationReport -category "HWM.CaseOpen" -configPath $ConfigPath
$initialized = $false

try {
    # [RUNNER SKELETON] 1) config + section gate
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'HWM.CaseOpen') { throw "source_ini.section must be HWM.CaseOpen, got '$sourceSection'." }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $caseSection = Get-IniSection -Sections $sections -Name 'HWM.CaseOpen'
    if ($null -eq $caseSection) { throw "INI does not contain [HWM.CaseOpen]." }
    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }

    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $sampleCfg = Get-ConfigValue -Config $config -Name 'read_check' -Default ([ordered]@{})
    $sampleCount = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_count' (Get-ConfigValue -Config $config -Name 'sample_count' 10))
    $sampleIntervalMs = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_interval_ms' (Get-ConfigValue -Config $config -Name 'sample_interval_ms' 500))
    $minSuccessRate = [double](Get-ConfigValue -Config $sampleCfg -Name 'min_success_rate' (Get-ConfigValue -Config $config -Name 'minimum_success_rate' 0.9))
    $allowedValues = @(Get-ConfigValue -Config $sampleCfg -Name 'allowed_values' (Get-ConfigValue -Config $config -Name 'allowed_values' @(0, 1)))
    if ($sampleCount -le 0 -or $sampleIntervalMs -le 0) { throw "Invalid sampling setup: sample_count=$sampleCount, sample_interval_ms=$sampleIntervalMs" }

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{ configured_path = $IniPath; resolved_path = $iniResolved; section = $sourceSection; sha256 = $actualHash }
    $report.metrics.thresholds = [ordered]@{ sample_count = $sampleCount; sample_interval_ms = $sampleIntervalMs; min_success_rate = $minSuccessRate; allowed_values = $allowedValues }
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
        $iniKey = @($caseSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [HWM.CaseOpen]." }
        $tuple = [string]$caseSection[$iniKey[0]]
        $chMeta = $null
        if ($channelDefs -is [System.Collections.IDictionary] -and $channelDefs.Contains([string]$ch)) { $chMeta = $channelDefs[[string]$ch] }
        $apiIdText = if ($null -ne $chMeta) { [string](Get-ConfigValue -Config $chMeta -Name 'api_id' -Default '') } else { '' }
        $apiId = Convert-ToUInt32Safe -Text $apiIdText -Default 0
        if ($apiId -eq 0) { throw "Missing/invalid canonical API ID for CaseOpen channel $ch." }

        $series = Sample-Channel -id $apiId -count $sampleCount -intervalMs $sampleIntervalMs -decode { param($raw) [UInt32]$raw }
        foreach ($s in $series) {
            $statusCode = if ($null -eq $s.status) { [UInt32]0 } else { Convert-ToUInt32Safe -Text ([string]$s.status_code) -Default ([UInt32]::MaxValue) }
            Add-ApiCall -report $report -name ("CaseOpenGet:{0}" -f $ch) -status $statusCode -value $s.raw
        }
        $successCount = @($series | Where-Object { $_.value -ne $null }).Count
        $successRate = $successCount / [double]$sampleCount
        $validCount = @($series | Where-Object { $_.value -ne $null -and $allowedValues -contains [int]$_.value }).Count
        $stats = Get-SeriesStats -series $series
        $validValues = ($validCount -eq $successCount)
        if ($successRate -lt $minSuccessRate) { $apiFailures += $ch }
        if (-not $validValues) { $readbackFailures += $ch }
        $channelMetrics[$ch] = [ordered]@{
            tuple = $tuple
            api_id = ('0x{0:X8}' -f [UInt32]$apiId)
            success_count = $successCount
            success_rate = [Math]::Round($successRate, 4)
            valid_value_count = $validCount
            stats = $stats
            valid_values = $validValues
            samples = $series
        }
    }
    $report.metrics.channels = $channelMetrics

    if ($apiFailures.Count -gt 0) {
        $report.result = 'FAIL_API'
        $failed = @($apiFailures | Select-Object -Unique | Sort-Object)
        $report.reason = ('CaseOpen API/read mapping failed for channel(s): ' + ($failed -join ', '))
        $report.validation_layers.L3_api = 'FAIL'; $report.validation_layers.L4_readback = 'FAIL'; $report.validation_layers.L5_functional = 'FAIL'; $report.checks.read_stability = 'FAIL'; $report.checks.control_effect = 'NOT_APPLICABLE'
    } elseif ($readbackFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $failed = @($readbackFailures | Select-Object -Unique | Sort-Object)
        $report.reason = ('CaseOpen returned non-boolean value(s) for channel(s): ' + ($failed -join ', '))
        $report.validation_layers.L3_api = 'PASS'; $report.validation_layers.L4_readback = 'FAIL'; $report.validation_layers.L5_functional = 'FAIL'; $report.checks.read_stability = 'FAIL'; $report.checks.control_effect = 'NOT_APPLICABLE'
    } else {
        # [RUNNER SKELETON] 4) verdict mapping + result breakdown
        $report.validation_layers.L3_api = 'PASS'; $report.validation_layers.L4_readback = 'PASS'; $report.validation_layers.L5_functional = 'CONDITIONAL'; $report.validation_layers.L6_recovery = 'N_A'
        $report.checks.read_stability = 'PASS'; $report.checks.control_effect = 'NOT_APPLICABLE'; $report.checks.recovery = 'NOT_REQUIRED'
        $report.result = 'CONDITIONAL'; $report.reason = 'Read-only CaseOpen validation passed; physical chassis-open/close stimulus was not applied.'
    }
} catch {
    $report.result = 'FAIL_CONFIG'; $report.reason = $_.Exception.Message; $report.validation_layers.L1_configuration = 'FAIL'
    if ($report.checks.read_stability -eq 'PENDING') { $report.checks.read_stability = 'FAIL' }
    if ($report.checks.control_effect -eq 'PENDING') { $report.checks.control_effect = 'NOT_RUN' }
    if ($report.checks.recovery -eq 'PENDING') { $report.checks.recovery = 'NOT_REQUIRED' }
} finally {
    if ($initialized) { $u = Uninitialize-Susi; $report.uninit_status = Get-StatusName $u }
}

$passItems = @(); $pendingItems = @(); $failItems = @(); $naItems = @()
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
        if ($null -eq $meta.success_rate -or [double]$meta.success_rate -le 0) { $failItems += "channel:$ch"; continue }
        if ($meta.valid_values) { $passItems += "channel:$ch" } else { $failItems += "channel:$ch" }
    }
}
$report.result_breakdown = [ordered]@{ pass = $passItems; pending = $pendingItems; fail = $failItems; na = $naItems }
$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'hwm_caseopen'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"
exit ([int]$verdict.exit_code)
