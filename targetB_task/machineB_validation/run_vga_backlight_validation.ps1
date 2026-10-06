param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\MIO-2375_backlight.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableFunctionalTest
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function Convert-ToUInt32Safe {
    param([string]$Text, [UInt32]$Default = 0)
    try {
        if ([string]::IsNullOrWhiteSpace($Text)) { return $Default }
        $t = $Text.Trim()
        if ($t.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
            return [UInt32]([Convert]::ToUInt32($t.Substring(2), 16))
        }
        return [UInt32]([Convert]::ToUInt32($t, 10))
    } catch { return $Default }
}

function Get-BacklightState([UInt32]$Id) {
    $value = [UInt32]0
    $status = [NativeSusi]::SusiVgaGetBacklightEnable($Id, [ref]$value)
    return [ordered]@{ status=$status; status_name=(Get-StatusName $status); value=$value }
}

$report = New-ValidationReport -category 'VGA.Backlight' -configPath $ConfigPath
$initialized = $false
try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'VGA.Backlight') { throw "source_ini.section must be VGA.Backlight, got '$sourceSection'." }

    $sections = Read-IniFile -Path $iniResolved
    $backlightSection = Get-IniSection -Sections $sections -Name 'VGA.Backlight'
    if ($null -eq $backlightSection) { throw 'INI does not contain [VGA.Backlight].' }
    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channels = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $functionalCheck = Get-ConfigValue -Config $config -Name 'functional_check' -Default ([ordered]@{})
    $configFunctionalEnabled = [bool](Get-ConfigValue -Config $functionalCheck -Name 'enabled' $false)
    $functionalEnabled = ($EnableFunctionalTest -and $configFunctionalEnabled)
    $toggleTo = [UInt32](Get-ConfigValue -Config $functionalCheck -Name 'toggle_to' 0)
    $verify = [bool](Get-ConfigValue -Config $functionalCheck -Name 'verify' $true)
    $restoreOriginal = [bool](Get-ConfigValue -Config $functionalCheck -Name 'restore_original' $true)

    $report.source_ini = [ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section=$sourceSection }
    $report.metrics.policy = [ordered]@{
        functional_enabled_by_config=$configFunctionalEnabled
        functional_enabled_by_switch=[bool]$EnableFunctionalTest
        functional_enabled=$functionalEnabled
        toggle_to=$toggleTo
        verify=$verify
        restore_original=$restoreOriginal
    }
    $report.validation_layers.L1_configuration = 'PASS'

    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init
    $report.metrics.channels = [ordered]@{}
    $apiFailures = @()
    $functionalFailures = @()
    $recoveryFailures = @()

    foreach ($ch in $required) {
        $iniKey = @($backlightSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [VGA.Backlight]." }
        $meta = if ($channels.Contains([string]$ch)) { $channels[[string]$ch] } else { $null }
        if ($null -eq $meta) { throw "Missing JSON channel metadata for $ch." }
        $id = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $meta -Name 'backlight_api_id' '')) 0
        if ($id -gt 3) { throw "Invalid Backlight API ID for $ch." }

        $initial = Get-BacklightState $id
        Add-ApiCall -report $report -name ("VgaGetBacklightEnable:{0}:initial" -f $ch) -status $initial.status -value $initial.value
        $entry = [ordered]@{
            backlight_api_id=('0x{0:X8}' -f $id)
            api_result=if ($initial.status -eq 0) { 'PASS' } else { 'FAIL' }
            initial=$initial
            functional=[ordered]@{ attempted=$false; result='NOT_RUN' }
        }
        if ($initial.status -ne 0) {
            $apiFailures += $ch
        } elseif ($functionalEnabled) {
            $entry.functional.attempted = $true
            $setStatus = [NativeSusi]::SusiVgaSetBacklightEnable($id, $toggleTo)
            Add-ApiCall -report $report -name ("VgaSetBacklightEnable:{0}:toggle" -f $ch) -status $setStatus -value $toggleTo
            $verifyState = if ($setStatus -eq 0 -and $verify) { Get-BacklightState $id } else { [ordered]@{ status=[Convert]::ToUInt32('FFFFFFFF', 16); status_name='NOT_RUN'; value=$null } }
            if ($setStatus -eq 0 -and $verify) {
                Add-ApiCall -report $report -name ("VgaGetBacklightEnable:{0}:verify" -f $ch) -status $verifyState.status -value $verifyState.value
            }
            $toggleOk = ($setStatus -eq 0 -and ((-not $verify) -or ($verifyState.status -eq 0 -and $verifyState.value -eq $toggleTo)))
            if (-not $toggleOk) { $functionalFailures += $ch }
            $restoreStatus = [UInt32]0
            $restoreState = [ordered]@{ status=[UInt32]0; status_name='NOT_RUN'; value=$null }
            if ($restoreOriginal) {
                $restoreStatus = [NativeSusi]::SusiVgaSetBacklightEnable($id, $initial.value)
                Add-ApiCall -report $report -name ("VgaSetBacklightEnable:{0}:restore" -f $ch) -status $restoreStatus -value $initial.value
                if ($restoreStatus -eq 0 -and $verify) {
                    $restoreState = Get-BacklightState $id
                    Add-ApiCall -report $report -name ("VgaGetBacklightEnable:{0}:restore_verify" -f $ch) -status $restoreState.status -value $restoreState.value
                }
                $restoreOk = ($restoreStatus -eq 0 -and ((-not $verify) -or ($restoreState.status -eq 0 -and $restoreState.value -eq $initial.value)))
                if (-not $restoreOk) { $recoveryFailures += $ch }
            }
            $entry.functional = [ordered]@{
                attempted=$true; result=if ($toggleOk -and $recoveryFailures -notcontains $ch) { 'PASS' } else { 'FAIL' }
                toggle_to=$toggleTo; set_status=Get-StatusName $setStatus; verify=$verify
                verify_status=Get-StatusName $verifyState.status; verify_value=$verifyState.value
                restore_original=$restoreOriginal; restore_status=Get-StatusName $restoreStatus
                restore_verify_status=Get-StatusName $restoreState.status; restore_verify_value=$restoreState.value
            }
        }
        $report.metrics.channels[$ch] = $entry
    }

    $passedChannels = @($required | Where-Object { $apiFailures -notcontains $_ })
    $failedChannelDetails = @(
        foreach ($failedChannel in $apiFailures) {
            $failedEntry = $report.metrics.channels[[string]$failedChannel]
            [ordered]@{
                channel = [string]$failedChannel
                status = [string]$failedEntry.initial.status_name
                status_code = ('0x{0:X8}' -f [UInt32]$failedEntry.initial.status)
            }
        }
    )
    $report.channel_summary = [ordered]@{
        overall = if ($apiFailures.Count -eq 0) { 'PASS' } elseif ($passedChannels.Count -gt 0) { 'PARTIAL_FAIL' } else { 'FAIL' }
        total = $required.Count
        passed = $passedChannels.Count
        failed = $apiFailures.Count
        passed_channels = @($passedChannels)
        failed_channels = @($failedChannelDetails)
    }

    $report.validation_layers.L2_capability = if ($apiFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
    $report.validation_layers.L3_api = if ($apiFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
    $report.validation_layers.L4_readback = if ($apiFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
    if (-not $functionalEnabled) {
        $report.metrics.functional_test = [ordered]@{ attempted=$false; result='NOT_RUN'; reason='Pass -EnableFunctionalTest and keep functional_check.enabled=true to run reversible toggle/restore.' }
        $report.validation_layers.L5_functional = 'CONDITIONAL'
        $report.validation_layers.L6_recovery = 'N_A'
    } else {
        $functionalResult = if ($apiFailures.Count -gt 0) { 'NOT_RUN' } elseif ($functionalFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $report.metrics.functional_test = [ordered]@{ attempted=($apiFailures.Count -eq 0); result=$functionalResult; failed_channels=@($functionalFailures | Select-Object -Unique) }
        $report.validation_layers.L5_functional = if ($apiFailures.Count -gt 0) { 'N_A' } elseif ($functionalFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $report.validation_layers.L6_recovery = if ($apiFailures.Count -gt 0) { 'N_A' } elseif ($recoveryFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
    }
    if ($apiFailures.Count -gt 0) {
        $passedDescription = if ($passedChannels.Count -gt 0) { $passedChannels -join ',' } else { 'none' }
        $failedDescription = @($failedChannelDetails | ForEach-Object { "$($_.channel)=$($_.status_code)" }) -join ','
        $report.result='FAIL_API'
        $report.reason=("VGA Backlight partial channel result: {0}/{1} passed; passed=[{2}]; failed=[{3}]." -f $passedChannels.Count, $required.Count, $passedDescription, $failedDescription)
    }
    elseif ($functionalFailures.Count -gt 0 -or $recoveryFailures.Count -gt 0) { $report.result='FAIL_FUNCTIONAL'; $report.reason='VGA Backlight toggle/restore failed.' }
    else { $report.result='PASS'; $report.reason='VGA Backlight API and validation passed.' }
    $report.checks.read_stability = if ($report.result -like 'FAIL*') { 'FAIL' } else { 'PASS' }
    $report.checks.control_effect = if ($functionalEnabled) { 'PASS' } else { 'CONDITIONAL' }
    $report.checks.recovery = if ($functionalEnabled) { if ($recoveryFailures.Count -eq 0) { 'PASS' } else { 'FAIL' } } else { 'N_A' }
} catch {
    $report.result='FAIL_CONFIG'; $report.reason=$_.Exception.Message; $report.validation_layers.L1_configuration='FAIL'
} finally {
    if ($initialized) { $u=Uninitialize-Susi; $report.uninit_status=Get-StatusName $u }
}

$report_path=Save-ValidationReport -report $report -outDir $OutDir -prefix 'vga_backlight'
$report.report_path=$report_path
($report | ConvertTo-Json -Depth 12) | Set-Content -Encoding UTF8 -Path $report_path
if ($report.result -like 'FAIL*') { exit 1 } else { exit 0 }
