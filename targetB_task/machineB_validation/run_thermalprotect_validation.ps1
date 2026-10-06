param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\MIO-2375_thermalprotect.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableSetConfigTest
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

function Get-ThermalEventName([UInt32]$eventType) {
    switch ($eventType) {
        0 { 'SHUTDOWN' }
        1 { 'THROTTLE' }
        2 { 'POWEROFF' }
        255 { 'NONE' }
        default { "UNKNOWN_$eventType" }
    }
}

$report = New-ValidationReport -category 'ThermalProtect' -configPath $ConfigPath
$initialized = $false
try {
    # SetConfig testing requires -EnableSetConfigTest (enabled by default in full validation).
    # Only the trigger temperature is changed and always restored; armed events are only raised.
    $allowThermalSetConfig = [bool]$EnableSetConfigTest
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'ThermalProtect') { throw "source_ini.section must be ThermalProtect, got '$sourceSection'." }
    $sections = Read-IniFile -Path $iniResolved
    $thermalSection = Get-IniSection -Sections $sections -Name 'ThermalProtect'
    if ($null -eq $thermalSection) { throw 'INI does not contain [ThermalProtect].' }

    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $capability = Get-ConfigValue -Config $config -Name 'capability_check' -Default ([ordered]@{})
    $itemIds = Get-ConfigValue -Config $capability -Name 'item_ids' -Default ([ordered]@{})
    $capItems = [ordered]@{
        support_flags = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name 'support_flags' '0x00000000')) 0
        trigger_maximum = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name 'trigger_maximum' '0x00000001')) 1
        trigger_minimum = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name 'trigger_minimum' '0x00000002')) 2
        clear_maximum = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name 'clear_maximum' '0x00000003')) 3
        clear_minimum = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name 'clear_minimum' '0x00000004')) 4
    }
    $report.source_ini = [ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section=$sourceSection }
    $report.metrics.requested_item_ids = $capItems
    $report.metrics.safety = [ordered]@{
        set_config_test_enabled = $allowThermalSetConfig
        event_trigger_enabled = $false
        policy = 'SetConfig test changes only the trigger temperature within caps (raised only for armed events), then restores the original config'
    }
    $report.validation_layers.L1_configuration = 'PASS'

    # [RUNNER SKELETON] 2) initialize
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    # [RUNNER SKELETON] 3) GetCaps for TPCH0~TPCH3
    $channelMetrics = [ordered]@{}
    $capFailures = @()
    $configFailures = @()
    $setFailures = @()
    $restoreFailures = @()
    $setSkipped = @()
    $setVerified = @()
    $notApplied = @()
    $setErrors = @()
    foreach ($ch in $required) {
        $iniKey = @($thermalSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [ThermalProtect]." }
        $tuple = [string]$thermalSection[$iniKey[0]]
        $meta = $null
        if ($channelDefs -is [System.Collections.IDictionary] -and $channelDefs.Contains([string]$ch)) { $meta = $channelDefs[[string]$ch] }
        $apiText = if ($null -ne $meta) { [string](Get-ConfigValue -Config $meta -Name 'thermal_api_id' -Default '') } else { '' }
        $thermalId = Convert-ToUInt32Safe $apiText 0
        if ($null -eq $meta -or $apiText -eq '' -or $thermalId -gt 3) { throw "Missing/invalid ThermalProtect API ID for $ch." }

        $caps = [ordered]@{}
        foreach ($item in $capItems.Keys) {
            $value = [UInt32]0
            $status = [NativeSusi]::SusiThermalProtectionGetCaps($thermalId, [UInt32]$capItems[$item], [ref]$value)
            Add-ApiCall -report $report -name ("ThermalGetCaps:{0}:{1}" -f $ch, $item) -status $status -value ([ordered]@{ value=$value; item_id=('0x{0:X8}' -f [UInt32]$capItems[$item]) })
            $caps[$item] = [ordered]@{ status=('0x{0:X8}' -f [UInt32]$status); status_name=(Get-StatusName $status); value=$value }
            if ($status -ne 0) { $capFailures += $ch }
        }

        # [RUNNER SKELETON] 4) read-only GetConfig
        $thermalConfig = New-Object SusiThermalProtect
        $configStatus = [NativeSusi]::SusiThermalProtectionGetConfig($thermalId, [ref]$thermalConfig)
        Add-ApiCall -report $report -name ("ThermalGetConfig:{0}" -f $ch) -status $configStatus
        $sourceId = [UInt32]$thermalConfig.SourceId
        $eventType = [UInt32]$thermalConfig.EventType
        $sendTemp = [UInt32]$thermalConfig.SendEventTemperature
        $clearTemp = [UInt32]$thermalConfig.ClearEventTemperature
        $setTest = [ordered]@{
            attempted=$false; original_send_0_1K=$sendTemp; target_send_0_1K=$null; set_status=$null
            readback=$null; readback_verified=$false; restore_attempted=$false; restore_status=$null
            restore_verified=$null; final_state='UNCHANGED'; skip_reason=$null
        }

        if ($allowThermalSetConfig -and $configStatus -eq 0) {
            # Change only the trigger temperature to another in-range value; SourceId,
            # EventType and clear temperature keep their original values. When the
            # original event is armed (SHUTDOWN/THROTTLE/POWEROFF) the trigger is only
            # ever raised, so the test can never make protection fire earlier.
            $trigMax = [UInt32]$caps.trigger_maximum.value
            $trigMin = [UInt32]$caps.trigger_minimum.value
            $target = $null
            if ($eventType -eq 0xFF) {
                foreach ($candidate in @($trigMin, $trigMax)) {
                    if ($candidate -ne $sendTemp -and $candidate -gt $clearTemp) { $target = $candidate; break }
                }
            } elseif ($sendTemp -lt $trigMax) {
                $target = $trigMax
            }

            if ($null -eq $target) {
                $setTest.skip_reason = 'No safe alternate trigger temperature within caps; config not modified'
                $setTest.final_state = 'NOT_MODIFIED'
                $setSkipped += $ch
            } else {
                $setTest.attempted = $true
                $setTest.target_send_0_1K = $target
                $candidateConfig = New-Object SusiThermalProtect
                $candidateConfig.SourceId = $sourceId
                $candidateConfig.EventType = $eventType
                $candidateConfig.SendEventTemperature = $target
                $candidateConfig.ClearEventTemperature = $clearTemp
                $setStatus = [NativeSusi]::SusiThermalProtectionSetConfig($thermalId, [ref]$candidateConfig)
                $setTest.set_status = ('0x{0:X8}' -f [UInt32]$setStatus)
                Add-ApiCall -report $report -name ("ThermalSetConfigTest:{0}" -f $ch) -status $setStatus -value ([ordered]@{ send_event_temperature_0_1K=$target })

                if ($setStatus -eq 0) {
                    $read = New-Object SusiThermalProtect
                    $readStatus = [NativeSusi]::SusiThermalProtectionGetConfig($thermalId, [ref]$read)
                    $setTest.readback = [ordered]@{
                        status = ('0x{0:X8}' -f [UInt32]$readStatus)
                        source_id = ('0x{0:X8}' -f [UInt32]$read.SourceId)
                        event_type = ('0x{0:X8}' -f [UInt32]$read.EventType)
                        send_event_temperature_0_1K = [UInt32]$read.SendEventTemperature
                        clear_event_temperature_0_1K = [UInt32]$read.ClearEventTemperature
                    }
                    $setTest.readback_verified = ($readStatus -eq 0 -and [UInt32]$read.SourceId -eq $sourceId -and [UInt32]$read.EventType -eq $eventType -and [UInt32]$read.SendEventTemperature -eq $target -and [UInt32]$read.ClearEventTemperature -eq $clearTemp)
                    Add-ApiCall -report $report -name ("ThermalVerifySetConfig:{0}" -f $ch) -status $readStatus -value $setTest.readback

                    # Always restore once Set succeeded, even if the readback did not match.
                    $original = New-Object SusiThermalProtect
                    $original.SourceId = $sourceId
                    $original.EventType = $eventType
                    $original.SendEventTemperature = $sendTemp
                    $original.ClearEventTemperature = $clearTemp
                    $setTest.restore_attempted = $true
                    $restoreStatus = [NativeSusi]::SusiThermalProtectionSetConfig($thermalId, [ref]$original)
                    $setTest.restore_status = ('0x{0:X8}' -f [UInt32]$restoreStatus)
                    Add-ApiCall -report $report -name ("ThermalRestoreOriginal:{0}" -f $ch) -status $restoreStatus
                    $restoreRead = New-Object SusiThermalProtect
                    $restoreReadStatus = [NativeSusi]::SusiThermalProtectionGetConfig($thermalId, [ref]$restoreRead)
                    Add-ApiCall -report $report -name ("ThermalVerifyRestore:{0}" -f $ch) -status $restoreReadStatus -value ([ordered]@{ send_event_temperature_0_1K=[UInt32]$restoreRead.SendEventTemperature })
                    $setTest.restore_verified = ($restoreStatus -eq 0 -and $restoreReadStatus -eq 0 -and [UInt32]$restoreRead.SourceId -eq $sourceId -and [UInt32]$restoreRead.EventType -eq $eventType -and [UInt32]$restoreRead.SendEventTemperature -eq $sendTemp -and [UInt32]$restoreRead.ClearEventTemperature -eq $clearTemp)

                    if (-not $setTest.restore_verified) {
                        $setTest.final_state = 'RESTORE_FAILED'
                        $restoreFailures += $ch
                    } elseif (-not $setTest.readback_verified) {
                        $setTest.final_state = 'SET_NOT_APPLIED_ORIGINAL_RESTORED'
                        $setFailures += $ch
                        $notApplied += ('{0}(wrote {1}, read back {2})' -f $ch, $target, $setTest.readback.send_event_temperature_0_1K)
                    } else {
                        $setTest.final_state = 'ORIGINAL_RESTORED'
                        $setVerified += ('{0}({1}->{2}->{3} 0.1K)' -f $ch, $sendTemp, $target, [UInt32]$restoreRead.SendEventTemperature)
                    }
                } else {
                    $setTest.final_state = 'SET_FAILED'
                    $setFailures += $ch
                    $setErrors += ('{0}({1})' -f $ch, $setTest.set_status)
                }
            }
        }

        $sourceConfigured = ($sourceId -ge 0x00020000 -and $sourceId -lt 0x0002000C)
        $disabledConfig = ($eventType -eq 0xFF -and $sourceId -eq 0)
        $sourceValid = ($sourceConfigured -or $disabledConfig)
        $eventValid = ($eventType -eq 0 -or $eventType -eq 1 -or $eventType -eq 2 -or $eventType -eq 0xFF)
        $configValid = ($configStatus -eq 0 -and $sourceValid -and $eventValid)
        if (-not $configValid) { $configFailures += $ch }
        $channelMetrics[$ch] = [ordered]@{
            tuple = $tuple
            thermal_api_id = ('0x{0:X8}' -f [UInt32]$thermalId)
            caps = $caps
            set_config_test = $setTest
            get_config = [ordered]@{
                status = ('0x{0:X8}' -f [UInt32]$configStatus)
                status_name = Get-StatusName $configStatus
                source_id = ('0x{0:X8}' -f $sourceId)
                event_type = ('0x{0:X8}' -f $eventType)
                event_name = Get-ThermalEventName $eventType
                send_event_temperature_0_1K = $sendTemp
                clear_event_temperature_0_1K = $clearTemp
                source_id_valid = $sourceValid
                source_id_configured = $sourceConfigured
                disabled_config = $disabledConfig
                event_type_valid = $eventValid
                config_valid = $configValid
            }
        }
    }
    $report.metrics.channels = $channelMetrics
    $capFailures = @($capFailures | Select-Object -Unique | Sort-Object)
    $configFailures = @($configFailures | Select-Object -Unique | Sort-Object)
    if ($capFailures.Count -gt 0) {
        $report.result = 'FAIL_API'
        $report.reason = 'ThermalProtect GetCaps failed for channel(s): ' + ($capFailures -join ', ')
        $report.validation_layers.L2_capability = 'FAIL'
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($configFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $report.reason = 'ThermalProtect GetConfig returned unsupported/invalid config for channel(s): ' + ($configFailures -join ', ')
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($restoreFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $report.reason = 'ThermalProtect original config could not be restored on: ' + ($restoreFailures -join ', ')
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($setFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'
        $parts = @()
        if ($notApplied.Count -gt 0) { $parts += 'SetConfig returned SUCCESS but value not applied: ' + ($notApplied -join ', ') }
        if ($setErrors.Count -gt 0) { $parts += 'SetConfig failed: ' + ($setErrors -join ', ') }
        $report.reason = ($parts -join '; ') + '; original restored'
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($allowThermalSetConfig -and $setSkipped.Count -gt 0) {
        $report.result = 'CONDITIONAL'
        $report.reason = 'No safe alternate trigger temperature; SetConfig not exercised on: ' + ($setSkipped -join ', ')
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'CONDITIONAL'
    } else {
        # [RUNNER SKELETON] 5) verdict mapping; event trigger APIs are never called
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'PASS'
        if ($allowThermalSetConfig) {
            $report.result = 'PASS'
            $report.reason = 'ThermalProtect SetConfig/readback/restore passed: ' + ($setVerified -join '; ')
        } else {
            # SetConfig exists but was not exercised: phase 1 write path not proven.
            $report.result = 'CONDITIONAL'
            $report.reason = 'ThermalProtect GetCaps/GetConfig passed; SetConfig test was not enabled.'
            $report.validation_layers.L4_readback = 'CONDITIONAL'
        }
    }
    $report.checks.read_stability = 'NOT_APPLICABLE'
    $report.checks.control_effect = 'NOT_RUN'
    $report.validation_layers.L5_functional = 'CONDITIONAL'
    if ($restoreFailures.Count -gt 0) {
        $report.validation_layers.L6_recovery = 'FAIL'
        $report.checks.recovery = 'FAIL'
    } elseif ($setVerified.Count -gt 0 -or $setFailures.Count -gt 0) {
        $report.validation_layers.L6_recovery = 'PASS'
        $report.checks.recovery = 'PASS'
    } else {
        $report.validation_layers.L6_recovery = 'N_A'
        $report.checks.recovery = 'NOT_REQUIRED'
    }
} catch {
    $report.result = 'FAIL_CONFIG'
    $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
    $report.validation_layers.L5_functional = 'NOT_RUN'
    $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
} finally {
    if ($initialized) { $u = Uninitialize-Susi; $report.uninit_status = Get-StatusName $u }
}

$pass=@(); $pending=@(); $fail=@(); $na=@()
foreach ($n in @($report.validation_layers.Keys)) {
    $s=[string]$report.validation_layers[$n]
    if ($s -eq 'PASS') { $pass += "layer:$n"; continue }
    if ($s -like 'FAIL*') { $fail += "layer:$n"; continue }
    if ($s -eq 'N_A' -or $s -eq 'NOT_REQUIRED') { $na += "layer:$n"; continue }
    if ($s -eq 'PENDING' -or $s -like 'CONDITIONAL*') { $pending += "layer:$n" }
}
if ($report.metrics.Contains('channels')) {
    foreach ($ch in @($report.metrics.channels.Keys)) {
        $m = $report.metrics.channels[$ch]
        if ($m.get_config.config_valid) { $pass += "channel:$ch" } else { $fail += "channel:$ch" }
    }
}
$report.result_breakdown = [ordered]@{ pass=$pass; pending=$pending; fail=$fail; na=$na }
$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'thermalprotect'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"
exit ([int]$verdict.exit_code)
