param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\MIO-2375_brightness.json",
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
        $t=$Text.Trim()
        if ($t.StartsWith('0x',[System.StringComparison]::OrdinalIgnoreCase)) { return [UInt32]([Convert]::ToUInt32($t.Substring(2),16)) }
        return [UInt32]([Convert]::ToUInt32($t,10))
    } catch { return $Default }
}
function Get-BrightnessState([UInt32]$Id) {
    $value=[UInt32]0
    $status=[NativeSusi]::SusiVgaGetBacklightBrightness($Id,[ref]$value)
    return [ordered]@{ status=$status; status_name=(Get-StatusName $status); value=$value }
}

$report=New-ValidationReport -category 'VGA.Brightness' -configPath $ConfigPath
$initialized=$false
try {
    $config=Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved=Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source=Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection=[string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'VGA.Brightness') { throw "source_ini.section must be VGA.Brightness, got '$sourceSection'." }
    $sections=Read-IniFile -Path $iniResolved
    $brightnessSection=Get-IniSection -Sections $sections -Name 'VGA.Brightness'
    if ($null -eq $brightnessSection) { throw 'INI does not contain [VGA.Brightness].' }
    $required=@($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channels=Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $functionalCheck=Get-ConfigValue -Config $config -Name 'functional_check' -Default ([ordered]@{})
    $configEnabled=[bool](Get-ConfigValue -Config $functionalCheck -Name 'enabled' $false)
    $functionalEnabled=($EnableFunctionalTest -and $configEnabled)
    $testValue=[UInt32](Get-ConfigValue -Config $functionalCheck -Name 'test_value' 0)
    $verify=[bool](Get-ConfigValue -Config $functionalCheck -Name 'verify' $true)
    $restoreOriginal=[bool](Get-ConfigValue -Config $functionalCheck -Name 'restore_original' $true)
    if ($testValue -gt 100) { throw 'functional_check.test_value must be in [0,100].' }

    $report.source_ini=[ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section=$sourceSection }
    $report.metrics.policy=[ordered]@{ functional_enabled_by_config=$configEnabled; functional_enabled_by_switch=[bool]$EnableFunctionalTest; functional_enabled=$functionalEnabled; test_value=$testValue; verify=$verify; restore_original=$restoreOriginal }
    $report.validation_layers.L1_configuration='PASS'
    $init=Initialize-Susi -DllDirs $DllDirs; $initialized=$true; $report.init_status=Get-StatusName $init
    $report.metrics.channels=[ordered]@{}
    $apiFailures=@(); $functionalFailures=@(); $recoveryFailures=@()

    foreach ($ch in $required) {
        $iniKey=@($brightnessSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [VGA.Brightness]." }
        $meta=if ($channels.Contains([string]$ch)) { $channels[[string]$ch] } else { $null }
        if ($null -eq $meta) { throw "Missing JSON channel metadata for $ch." }
        $id=Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $meta -Name 'brightness_api_id' '')) 0
        if ($id -gt 3) { throw "Invalid Brightness API ID for $ch." }
        $initial=Get-BrightnessState $id
        Add-ApiCall -report $report -name ("VgaGetBacklightBrightness:{0}:initial" -f $ch) -status $initial.status -value $initial.value
        $entry=[ordered]@{ brightness_api_id=('0x{0:X8}' -f $id); initial=$initial; functional=[ordered]@{ attempted=$false; result='NOT_RUN' } }
        if ($initial.status -ne 0) { $apiFailures += $ch }
        elseif ($functionalEnabled) {
            $entry.functional.attempted=$true
            $setStatus=[NativeSusi]::SusiVgaSetBacklightBrightness($id,$testValue)
            Add-ApiCall -report $report -name ("VgaSetBacklightBrightness:{0}:test" -f $ch) -status $setStatus -value $testValue
            $verifyState=if ($setStatus -eq 0 -and $verify) { Get-BrightnessState $id } else { [ordered]@{ status=[Convert]::ToUInt32('FFFFFFFF', 16); status_name='NOT_RUN'; value=$null } }
            if ($setStatus -eq 0 -and $verify) { Add-ApiCall -report $report -name ("VgaGetBacklightBrightness:{0}:verify" -f $ch) -status $verifyState.status -value $verifyState.value }
            $setOk=($setStatus -eq 0 -and ((-not $verify) -or ($verifyState.status -eq 0 -and $verifyState.value -eq $testValue)))
            if (-not $setOk) { $functionalFailures += $ch }
            $restoreStatus=[UInt32]0; $restoreState=[ordered]@{ status=[UInt32]0; status_name='NOT_RUN'; value=$null }
            if ($restoreOriginal) {
                $restoreStatus=[NativeSusi]::SusiVgaSetBacklightBrightness($id,$initial.value)
                Add-ApiCall -report $report -name ("VgaSetBacklightBrightness:{0}:restore" -f $ch) -status $restoreStatus -value $initial.value
                if ($restoreStatus -eq 0 -and $verify) { $restoreState=Get-BrightnessState $id; Add-ApiCall -report $report -name ("VgaGetBacklightBrightness:{0}:restore_verify" -f $ch) -status $restoreState.status -value $restoreState.value }
                $restoreOk=($restoreStatus -eq 0 -and ((-not $verify) -or ($restoreState.status -eq 0 -and $restoreState.value -eq $initial.value)))
                if (-not $restoreOk) { $recoveryFailures += $ch }
            }
            $entry.functional=[ordered]@{ attempted=$true; result=if ($setOk -and $recoveryFailures -notcontains $ch) { 'PASS' } else { 'FAIL' }; test_value=$testValue; set_status=Get-StatusName $setStatus; verify_status=Get-StatusName $verifyState.status; verify_value=$verifyState.value; restore_status=Get-StatusName $restoreStatus; restore_verify_status=Get-StatusName $restoreState.status; restore_verify_value=$restoreState.value }
        }
        $report.metrics.channels[$ch]=$entry
    }
    $report.validation_layers.L2_capability=if($apiFailures.Count -eq 0){'PASS'}else{'FAIL'}
    $report.validation_layers.L3_api=if($apiFailures.Count -eq 0){'PASS'}else{'FAIL'}
    $report.validation_layers.L4_readback=if($apiFailures.Count -eq 0){'PASS'}else{'FAIL'}
    if (-not $functionalEnabled) { $report.metrics.functional_test=[ordered]@{attempted=$false;result='NOT_RUN';reason='Pass -EnableFunctionalTest and keep functional_check.enabled=true.'};$report.validation_layers.L5_functional='CONDITIONAL';$report.validation_layers.L6_recovery='N_A' }
    else { $fr=if($apiFailures.Count -gt 0){'NOT_RUN'}elseif($functionalFailures.Count -eq 0){'PASS'}else{'FAIL'};$report.metrics.functional_test=[ordered]@{attempted=($apiFailures.Count -eq 0);result=$fr;failed_channels=@($functionalFailures|Select-Object -Unique)};$report.validation_layers.L5_functional=if($apiFailures.Count -gt 0){'N_A'}elseif($functionalFailures.Count -eq 0){'PASS'}else{'FAIL'};$report.validation_layers.L6_recovery=if($apiFailures.Count -gt 0){'N_A'}elseif($recoveryFailures.Count -eq 0){'PASS'}else{'FAIL'} }
    if($apiFailures.Count -gt 0){$report.result='FAIL_API';$report.reason='VGA Brightness Get failed.'}elseif($functionalFailures.Count -gt 0 -or $recoveryFailures.Count -gt 0){$report.result='FAIL_FUNCTIONAL';$report.reason='VGA Brightness set/verify/restore failed.'}else{$report.result='PASS';$report.reason='VGA Brightness API and validation passed.'}
    $report.checks.read_stability=if($report.result -like 'FAIL*'){'FAIL'}else{'PASS'};$report.checks.control_effect=if($functionalEnabled){'PASS'}else{'CONDITIONAL'};$report.checks.recovery=if($functionalEnabled){if($recoveryFailures.Count -eq 0){'PASS'}else{'FAIL'}}else{'N_A'}
} catch { $report.result='FAIL_CONFIG';$report.reason=$_.Exception.Message;$report.validation_layers.L1_configuration='FAIL' }
finally { if($initialized){$u=Uninitialize-Susi;$report.uninit_status=Get-StatusName $u} }
$report_path=Save-ValidationReport -report $report -outDir $OutDir -prefix 'vga_brightness';$report.report_path=$report_path;($report|ConvertTo-Json -Depth 12)|Set-Content -Encoding UTF8 -Path $report_path;if($report.result -like 'FAIL*'){exit 1}else{exit 0}
