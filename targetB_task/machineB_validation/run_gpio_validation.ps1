param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\MIO-2375_gpio.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableFunctionalTest
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function Convert-ToUInt32Safe {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { throw 'Missing UInt32 value.' }
    $t = $Text.Trim()
    if ($t.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
        return [UInt32]([Convert]::ToUInt32($t.Substring(2), 16))
    }
    return [UInt32]([Convert]::ToUInt32($t, 10))
}

function Restore-GpioBanks {
    param(
        [System.Collections.IDictionary]$States,
        [System.Collections.IDictionary]$Report
    )
    $failures = @()
    foreach ($name in @($States.Keys)) {
        $state = $States[$name]
        if ([bool]$state.restored) { continue }
        $bankId = [UInt32]$state.bank_id
        $mask = [UInt32]$state.expected_mask
        $originalLevel = [UInt32]$state.original_level
        $originalDirection = [UInt32]$state.original_direction

        $levelStatus = [NativeSusi]::SusiGPIOSetLevel($bankId, $mask, $originalLevel)
        Add-ApiCall -report $Report -name ("GPIO restore level:{0}" -f $name) -status $levelStatus -value ('0x{0:X8}' -f $originalLevel)
        $levelRead = [UInt32]0
        $levelReadStatus = [NativeSusi]::SusiGPIOGetLevel($bankId, $mask, [ref]$levelRead)
        Add-ApiCall -report $Report -name ("GPIO restore level read:{0}" -f $name) -status $levelReadStatus -value ('0x{0:X8}' -f $levelRead)

        $directionStatus = [NativeSusi]::SusiGPIOSetDirection($bankId, $mask, $originalDirection)
        Add-ApiCall -report $Report -name ("GPIO restore direction:{0}" -f $name) -status $directionStatus -value ('0x{0:X8}' -f $originalDirection)
        $directionRead = [UInt32]0
        $directionReadStatus = [NativeSusi]::SusiGPIOGetDirection($bankId, $mask, [ref]$directionRead)
        Add-ApiCall -report $Report -name ("GPIO restore direction read:{0}" -f $name) -status $directionReadStatus -value ('0x{0:X8}' -f $directionRead)

        $levelOk = ($levelStatus -eq 0 -and $levelReadStatus -eq 0 -and (($levelRead -band $mask) -eq ($originalLevel -band $mask)))
        $directionOk = ($directionStatus -eq 0 -and $directionReadStatus -eq 0 -and (($directionRead -band $mask) -eq ($originalDirection -band $mask)))
        $state.restored = ($levelOk -and $directionOk)
        $state.restore = [ordered]@{
            level_status = Get-StatusName $levelStatus
            level_read_status = Get-StatusName $levelReadStatus
            level_match = $levelOk
            direction_status = Get-StatusName $directionStatus
            direction_read_status = Get-StatusName $directionReadStatus
            direction_match = $directionOk
        }
        if (-not $state.restored) { $failures += $name }
    }
    return @($failures)
}

$report = New-ValidationReport -category 'GPIO' -configPath $ConfigPath
$initialized = $false
$states = [ordered]@{}
$functionalAttempted = $false
try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    if ([string](Get-ConfigValue -Config $config -Name 'category' '') -ne 'GPIO') {
        throw 'Config category must be GPIO.'
    }
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    if ([string](Get-ConfigValue -Config $source -Name 'section' '') -ne 'GPIO') {
        throw 'source_ini.section must be GPIO.'
    }
    $sections = Read-IniFile -Path $iniResolved
    $gpioSection = Get-IniSection -Sections $sections -Name 'GPIO'
    if ($null -eq $gpioSection) { throw 'INI does not contain [GPIO].' }
    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    foreach ($channel in $required) {
        if (@($gpioSection.Keys | Where-Object { $_ -ieq [string]$channel }).Count -ne 1) {
            throw "Required GPIO key $channel is missing from the existing INI."
        }
    }

    $banks = Get-ConfigValue -Config $config -Name 'banks' -Default ([ordered]@{})
    if ($banks.Count -eq 0) { throw 'banks is empty.' }
    $functional = Get-ConfigValue -Config $config -Name 'functional_check' -Default ([ordered]@{})
    $configFunctionalEnabled = [bool](Get-ConfigValue -Config $functional -Name 'enabled' $false)
    $functionalEnabled = ([bool]$EnableFunctionalTest -and $configFunctionalEnabled)
    $settleMs = [int](Get-ConfigValue -Config $functional -Name 'settle_time_ms' 100)
    if ($settleMs -lt 0) { throw 'settle_time_ms cannot be negative.' }

    $report.source_ini = [ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section='GPIO' }
    $report.metrics.policy = [ordered]@{
        enabled_by_config = $configFunctionalEnabled
        enabled_by_switch = [bool]$EnableFunctionalTest
        functional_enabled = $functionalEnabled
        settle_time_ms = $settleMs
    }
    $report.validation_layers.L1_configuration = 'PASS'

    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init
    $report.metrics.banks = [ordered]@{}
    $capabilityFailures = @()
    $readFailures = @()

    foreach ($bankName in @($banks.Keys)) {
        $bank = $banks[$bankName]
        $bankId = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $bank -Name 'bank_id' ''))
        $expected_mask = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $bank -Name 'expected_mask' ''))
        if ($expected_mask -eq 0) { throw "$bankName expected_mask cannot be zero." }

        $inputSupport = [UInt32]0
        $inputStatus = [NativeSusi]::SusiGPIOGetCaps($bankId, [UInt32]0, [ref]$inputSupport)
        Add-ApiCall -report $report -name ("GPIO GetCaps input:{0}" -f $bankName) -status $inputStatus -value ('0x{0:X8}' -f $inputSupport)
        $outputSupport = [UInt32]0
        $outputStatus = [NativeSusi]::SusiGPIOGetCaps($bankId, [UInt32]1, [ref]$outputSupport)
        Add-ApiCall -report $report -name ("GPIO GetCaps output:{0}" -f $bankName) -status $outputStatus -value ('0x{0:X8}' -f $outputSupport)

        $direction = [UInt32]0
        $directionStatus = [NativeSusi]::SusiGPIOGetDirection($bankId, $expected_mask, [ref]$direction)
        Add-ApiCall -report $report -name ("GPIO GetDirection:{0}" -f $bankName) -status $directionStatus -value ('0x{0:X8}' -f $direction)
        $level = [UInt32]0
        $levelStatus = [NativeSusi]::SusiGPIOGetLevel($bankId, $expected_mask, [ref]$level)
        Add-ApiCall -report $report -name ("GPIO GetLevel:{0}" -f $bankName) -status $levelStatus -value ('0x{0:X8}' -f $level)

        $capsOk = ($inputStatus -eq 0 -and $outputStatus -eq 0 -and (($inputSupport -band $expected_mask) -eq $expected_mask) -and (($outputSupport -band $expected_mask) -eq $expected_mask))
        $readsOk = ($directionStatus -eq 0 -and $levelStatus -eq 0)
        if (-not $capsOk) { $capabilityFailures += $bankName }
        if (-not $readsOk) { $readFailures += $bankName }
        if ($readsOk) {
            $states[$bankName] = [ordered]@{
                bank_id = $bankId
                expected_mask = $expected_mask
                original_direction = $direction
                original_level = $level
                restored = $false
                restore = $null
            }
        }
        $report.metrics.banks[$bankName] = [ordered]@{
            bank_id = ('0x{0:X8}' -f $bankId)
            expected_mask = ('0x{0:X8}' -f $expected_mask)
            input_support = ('0x{0:X8}' -f $inputSupport)
            output_support = ('0x{0:X8}' -f $outputSupport)
            original_direction = ('0x{0:X8}' -f $direction)
            original_level = ('0x{0:X8}' -f $level)
            caps_ok = $capsOk
            reads_ok = $readsOk
            patterns = @()
        }
    }

    if ($capabilityFailures.Count -gt 0 -or $readFailures.Count -gt 0) {
        $report.result = 'FAIL_API'
        $report.reason = 'GPIO capability or initial read failed.'
        $report.validation_layers.L2_capability = if ($capabilityFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = if ($readFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $report.validation_layers.L5_functional = 'NOT_RUN'
        $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
    } else {
        $report.result = 'PASS'
        $report.reason = 'GPIO capability and read validation passed.'
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'PASS'

        if (-not $functionalEnabled) {
            $report.validation_layers.L5_functional = 'CONDITIONAL'
            $report.validation_layers.L6_recovery = 'N_A'
            $report.reason = 'GPIO read validation passed; functional test was not requested.'
        } else {
            $functionalAttempted = $true
            $functionalFailures = @()
            foreach ($bankName in @($states.Keys)) {
                $state = $states[$bankName]
                $bankId = [UInt32]$state.bank_id
                $mask = [UInt32]$state.expected_mask
                $setDirectionStatus = [NativeSusi]::SusiGPIOSetDirection($bankId, $mask, [UInt32]0)
                Add-ApiCall -report $report -name ("GPIO set output:{0}" -f $bankName) -status $setDirectionStatus -value '0x00000000'
                if ($setDirectionStatus -ne 0) { $functionalFailures += $bankName; continue }

                foreach ($pattern in @([UInt32]0, $mask)) {
                    $setStatus = [NativeSusi]::SusiGPIOSetLevel($bankId, $mask, $pattern)
                    Add-ApiCall -report $report -name ("GPIO set level:{0}" -f $bankName) -status $setStatus -value ('0x{0:X8}' -f $pattern)
                    if ($settleMs -gt 0) { Start-Sleep -Milliseconds $settleMs }
                    $readLevel = [UInt32]0
                    $readStatus = [NativeSusi]::SusiGPIOGetLevel($bankId, $mask, [ref]$readLevel)
                    Add-ApiCall -report $report -name ("GPIO read level:{0}" -f $bankName) -status $readStatus -value ('0x{0:X8}' -f $readLevel)
                    $match = ($setStatus -eq 0 -and $readStatus -eq 0 -and (($readLevel -band $mask) -eq $pattern))
                    $report.metrics.banks[$bankName].patterns += [ordered]@{
                        set = ('0x{0:X8}' -f $pattern)
                        read = ('0x{0:X8}' -f $readLevel)
                        set_status = Get-StatusName $setStatus
                        read_status = Get-StatusName $readStatus
                        match = $match
                    }
                    if (-not $match) { $functionalFailures += $bankName }
                }
            }

            $restoreFailures = @(Restore-GpioBanks -States $states -Report $report)
            foreach ($bankName in @($states.Keys)) {
                $report.metrics.banks[$bankName].restore = $states[$bankName].restore
            }
            $report.validation_layers.L5_functional = if ($functionalFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
            $report.validation_layers.L6_recovery = if ($restoreFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
            if ($functionalFailures.Count -gt 0 -or $restoreFailures.Count -gt 0) {
                $report.result = 'FAIL_FUNCTIONAL'
                $report.reason = 'GPIO set/readback or restore failed.'
            } else {
                $report.reason = 'GPIO low/high readback and restore passed.'
            }
        }
    }
} catch {
    $report.result = 'FAIL_CONFIG'
    $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
} finally {
    if ($initialized -and $functionalAttempted) {
        try {
            $lateRestoreFailures = @(Restore-GpioBanks -States $states -Report $report)
            if ($lateRestoreFailures.Count -gt 0) {
                $report.result = 'FAIL_FUNCTIONAL'
                $report.reason = 'GPIO emergency restore failed.'
                $report.validation_layers.L6_recovery = 'FAIL'
            }
        } catch {
            $report.result = 'FAIL_FUNCTIONAL'
            $report.reason = "GPIO emergency restore raised: $($_.Exception.Message)"
            $report.validation_layers.L6_recovery = 'FAIL'
        }
    }
    if ($initialized) {
        $u = Uninitialize-Susi
        $report.uninit_status = Get-StatusName $u
    }
}

$report.checks.read_stability = if ($report.validation_layers.L4_readback -eq 'PASS') { 'PASS' } else { 'FAIL' }
$report.checks.control_effect = [string]$report.validation_layers.L5_functional
$report.checks.recovery = [string]$report.validation_layers.L6_recovery
$reportPath = Save-ValidationReport -report $report -outDir $OutDir -prefix 'gpio'
$report.report_path = $reportPath
($report | ConvertTo-Json -Depth 12) | Set-Content -Encoding UTF8 -Path $reportPath
if ($report.result -like 'FAIL*') { exit 1 } else { exit 0 }
