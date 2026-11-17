param(
    [string[]]$DllDirs = @(),
    [string]$OutDir = ".\out"
)

# Board information check (read-only): Boot Counter and Running Time Meter.
# Both come from the EC through SusiBoardGetValue and must be >= 1 on a unit that
# has been booted and used. Nothing is written to the target.
#   API status not SUCCESS -> fail (FAIL_API), original status code is recorded
#   API succeeded, value 0  -> fail (FAIL_READBACK), "value is 0 (expected >= 1)"
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

# Public SUSI IDs (Susi4.h). Running Time Meter is in hours: the SUSI4 manual says so and
# SusiDemo4 shows "Running time (hours)". (A comment in one Susi4.h copy says minutes; ignore it.)
$items = @(
    [ordered]@{ key = 'boot_counter';       name = 'SUSI_ID_BOARD_BOOT_COUNTER_VAL';        id = [UInt32]0x00000001; unit = 'boot' },
    [ordered]@{ key = 'running_time_meter'; name = 'SUSI_ID_BOARD_RUNNING_TIME_METER_VAL';  id = [UInt32]0x00000002; unit = 'hour' }
)

$report = New-ValidationReport -category 'BoardInfo' -configPath ''
$initialized = $false
try {
    $report.validation_layers.L1_configuration = 'PASS'
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init
    $report.validation_layers.L2_capability = 'PASS'

    $report.metrics.items = [ordered]@{}
    $apiFailures = @()
    $valueFailures = @()
    foreach ($item in $items) {
        $r = Read-BoardValue -id $item.id
        $status = [UInt32]$r.status
        Add-ApiCall -report $report -name ("SusiBoardGetValue:{0}" -f $item.name) -status $status -value ('0x{0:X8}' -f [UInt32]$r.value)
        $entry = [ordered]@{
            id = ('0x{0:X8}' -f $item.id)
            unit = $item.unit
            api_status = Get-StatusName $status
            api_status_code = ('0x{0:X8}' -f $status)
            value = $null
            status = 'fail'
            reason = $null
        }
        if ($status -ne 0) {
            $entry.reason = ('API status {0} (0x{1:X8})' -f (Get-StatusName $status), $status)
            $apiFailures += ('{0}: {1}' -f $item.name, $entry.reason)
        } else {
            $entry.value = [UInt32]$r.value
            if ([UInt32]$r.value -lt 1) {
                $entry.reason = 'value is 0 (expected >= 1)'
                $valueFailures += ('{0}: {1}' -f $item.name, $entry.reason)
            } else {
                $entry.status = 'success'
            }
        }
        $report.metrics.items[$item.key] = $entry
    }

    $report.validation_layers.L5_functional = 'N_A'
    $report.validation_layers.L6_recovery = 'N_A'
    if ($apiFailures.Count -gt 0) {
        $report.result = 'FAIL_API'
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = if ($valueFailures.Count -gt 0) { 'FAIL' } else { 'PASS' }
        $report.reason = 'Board info read failed: ' + (@($apiFailures) + @($valueFailures) -join '; ')
    } elseif ($valueFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
        $report.reason = 'Board info value problem: ' + ($valueFailures -join '; ')
    } else {
        $report.result = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'PASS'
        $report.reason = ('Boot Counter={0}, Running Time Meter={1}; both >= 1.' -f $report.metrics.items.boot_counter.value, $report.metrics.items.running_time_meter.value)
    }
    $report.checks.read_stability = if ($report.result -eq 'PASS') { 'PASS' } else { 'FAIL' }
    $report.checks.control_effect = 'NOT_APPLICABLE'
    $report.checks.recovery = 'NOT_REQUIRED'
} catch {
    $report.result = 'FAIL_CONFIG'
    $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
    $report.validation_layers.L5_functional = 'NOT_RUN'
    $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
} finally {
    if ($initialized) { $u = Uninitialize-Susi; $report.uninit_status = Get-StatusName $u }
}

$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'boardinfo'
$report.report_path = $path
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); Report=$path"
exit ([int]$verdict.exit_code)
