param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_current.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out"
)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"
function Convert-ToUInt32Safe {
    param([string]$Text, [UInt32]$Default = 0)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $Default }
    try { $t=$Text.Trim(); if ($t.StartsWith('0x',[System.StringComparison]::OrdinalIgnoreCase)) { return [UInt32]([Convert]::ToUInt32($t.Substring(2),16)) }; return [UInt32]([Convert]::ToUInt32($t,10)) } catch { return $Default }
}
$report = New-ValidationReport -category 'HWM.Current' -configPath $ConfigPath
$initialized = $false
try {
    # [RUNNER SKELETON] 1) config + section gate
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'HWM.Current') { throw "source_ini.section must be HWM.Current, got '$sourceSection'." }
    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expectedHash -and $expectedHash.ToLowerInvariant() -ne $actualHash) { Write-Warning "Section INI SHA-256 differs from config. Continue validation." }
    $sections = Read-IniFile -Path $iniResolved
    $currentSection = Get-IniSection -Sections $sections -Name 'HWM.Current'
    if ($null -eq $currentSection) { throw 'INI does not contain [HWM.Current].' }
    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $sampleCfg = Get-ConfigValue -Config $config -Name 'read_check' -Default ([ordered]@{})
    $sampleCount = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_count' 10)
    $sampleIntervalMs = [int](Get-ConfigValue -Config $sampleCfg -Name 'sample_interval_ms' 500)
    $minSuccessRate = [double](Get-ConfigValue -Config $sampleCfg -Name 'min_success_rate' 0.9)
    $minMa = [double](Get-ConfigValue -Config $sampleCfg -Name 'min_milliamps' 0.0)
    $maxMa = [double](Get-ConfigValue -Config $sampleCfg -Name 'max_milliamps' 100000.0)
    $maxSpan = [double](Get-ConfigValue -Config $sampleCfg -Name 'max_span_milliamps' 10000.0)
    if ($sampleCount -le 0 -or $sampleIntervalMs -le 0) { throw "Invalid sampling setup: sample_count=$sampleCount, sample_interval_ms=$sampleIntervalMs" }
    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section=$sourceSection; sha256=$actualHash }
    $report.metrics.thresholds = [ordered]@{ sample_count=$sampleCount; sample_interval_ms=$sampleIntervalMs; min_success_rate=$minSuccessRate; min_milliamps=$minMa; max_milliamps=$maxMa; max_span_milliamps=$maxSpan }
    $report.validation_layers.L1_configuration = 'PASS'

    # [RUNNER SKELETON] 2) initialize + capability gate
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init
    $report.validation_layers.L2_capability = 'PASS'

    # [RUNNER SKELETON] 3) per-channel sampling loop
    $channelMetrics=[ordered]@{}; $apiFailures=@(); $readbackFailures=@()
    foreach ($ch in $required) {
        $iniKey=@($currentSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [HWM.Current]." }
        $tuple=[string]$currentSection[$iniKey[0]]
        $meta=$null
        if ($channelDefs -is [System.Collections.IDictionary] -and $channelDefs.Contains([string]$ch)) { $meta=$channelDefs[[string]$ch] }
        $apiText=if ($null -ne $meta) { [string](Get-ConfigValue -Config $meta -Name 'api_id' -Default '') } else { '' }
        $apiId=Convert-ToUInt32Safe -Text $apiText -Default 0
        if ($apiId -eq 0) { throw "Missing/invalid canonical API ID for Current channel $ch." }
        $series=Sample-Channel -id $apiId -count $sampleCount -intervalMs $sampleIntervalMs
        foreach ($s in $series) { $st=if ($null -eq $s.status) {[UInt32]0} else {Convert-ToUInt32Safe ([string]$s.status_code) ([UInt32]::MaxValue)}; Add-ApiCall -report $report -name ("CurrentGet:{0}" -f $ch) -status $st -value $s.raw }
        $success=@($series | Where-Object {$_.value -ne $null}).Count
        $rate=$success/[double]$sampleCount; $stats=Get-SeriesStats -series $series
        $inRange=($stats.count -gt 0 -and $stats.min -ge $minMa -and $stats.max -le $maxMa)
        $spanOk=($stats.count -gt 0 -and $stats.span -le $maxSpan)
        if ($rate -lt $minSuccessRate) {$apiFailures+=$ch}; if (-not $inRange -or -not $spanOk) {$readbackFailures+=$ch}
        $channelMetrics[$ch]=[ordered]@{ tuple=$tuple; api_id=('0x{0:X8}' -f [UInt32]$apiId); success_count=$success; success_rate=[Math]::Round($rate,4); stats_milliamps=$stats; in_range=$inRange; span_ok=$spanOk; samples=$series }
    }
    $report.metrics.channels=$channelMetrics
    if ($apiFailures.Count -gt 0) {
        $report.result='FAIL_API'; $report.reason='Current API/read mapping failed for channel(s): '+(@($apiFailures|Select-Object -Unique|Sort-Object)-join ', '); $report.validation_layers.L3_api='FAIL';$report.validation_layers.L4_readback='FAIL';$report.validation_layers.L5_functional='FAIL';$report.checks.read_stability='FAIL';$report.checks.control_effect='NOT_APPLICABLE'
    } elseif ($readbackFailures.Count -gt 0) {
        $report.result='FAIL_READBACK'; $report.reason='Current out-of-range/unstable span for channel(s): '+(@($readbackFailures|Select-Object -Unique|Sort-Object)-join ', '); $report.validation_layers.L3_api='PASS';$report.validation_layers.L4_readback='FAIL';$report.validation_layers.L5_functional='FAIL';$report.checks.read_stability='FAIL';$report.checks.control_effect='NOT_APPLICABLE'
    } else {
        # [RUNNER SKELETON] 4) verdict mapping + result breakdown
        $report.validation_layers.L3_api='PASS';$report.validation_layers.L4_readback='PASS';$report.validation_layers.L5_functional='CONDITIONAL';$report.validation_layers.L6_recovery='N_A';$report.checks.read_stability='PASS';$report.checks.control_effect='NOT_APPLICABLE';$report.checks.recovery='NOT_REQUIRED';$report.result='CONDITIONAL';$report.reason='Read-only current validation passed; no load/current stimulus profile applied.'
    }
} catch { $report.result='FAIL_CONFIG';$report.reason=$_.Exception.Message;$report.validation_layers.L1_configuration='FAIL';if($report.checks.read_stability -eq 'PENDING'){$report.checks.read_stability='FAIL'};if($report.checks.control_effect -eq 'PENDING'){$report.checks.control_effect='NOT_RUN'};if($report.checks.recovery -eq 'PENDING'){$report.checks.recovery='NOT_REQUIRED'} } finally { if($initialized){$u=Uninitialize-Susi;$report.uninit_status=Get-StatusName $u} }
$pass=@();$pending=@();$fail=@();$na=@()
foreach($n in @($report.validation_layers.Keys)){ $s=[string]$report.validation_layers[$n];if($s -eq 'PASS'){$pass+="layer:$n";continue};if($s -like 'FAIL*'){$fail+="layer:$n";continue};if($s -eq 'N_A' -or $s -eq 'NOT_REQUIRED'){$na+="layer:$n";continue};if($s -eq 'PENDING' -or $s -like 'CONDITIONAL*'){$pending+="layer:$n"} }
if($report.metrics.Contains('channels')){foreach($ch in @($report.metrics.channels.Keys)){$m=$report.metrics.channels[$ch];if($null -eq $m.success_rate -or [double]$m.success_rate -le 0){$fail+="channel:$ch";continue};if($m.in_range -and $m.span_ok){$pass+="channel:$ch"}else{$fail+="channel:$ch"}}}
$report.result_breakdown=[ordered]@{pass=$pass;pending=$pending;fail=$fail;na=$na}
$verdict=Apply-VerdictPolicy -Report $report
$path=Save-ValidationReport -report $report -outDir $OutDir -prefix 'hwm_current'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"
exit ([int]$verdict.exit_code)
