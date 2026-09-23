param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\MIO-2375_storage.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableWriteTest
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

function Convert-BytesToHex([byte[]]$Bytes) {
    if ($null -eq $Bytes) { return '' }
    return (($Bytes | ForEach-Object { '{0:X2}' -f $_ }) -join '')
}

$report = New-ValidationReport -category 'StorageArea' -configPath $ConfigPath
$initialized = $false
try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'StorageArea') { throw "source_ini.section must be StorageArea, got '$sourceSection'." }

    $sections = Read-IniFile -Path $iniResolved
    $storageSection = Get-IniSection -Sections $sections -Name 'StorageArea'
    if ($null -eq $storageSection) { throw 'INI does not contain [StorageArea].' }
    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channels = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $readCheck = Get-ConfigValue -Config $config -Name 'read_check' -Default ([ordered]@{})
    $offset = [UInt32](Get-ConfigValue -Config $readCheck -Name 'offset' 0)
    $length = [UInt32](Get-ConfigValue -Config $readCheck -Name 'length' 16)
    $sampleCount = [int](Get-ConfigValue -Config $readCheck -Name 'sample_count' 1)
    $writeCheck = Get-ConfigValue -Config $config -Name 'write_check' -Default ([ordered]@{})
    $configWriteEnabled = [bool](Get-ConfigValue -Config $writeCheck -Name 'enabled' $false)
    $writeEnabled = ($EnableWriteTest -and $configWriteEnabled)

    if ($length -eq 0 -or $sampleCount -le 0) { throw 'StorageArea read_check length/sample_count must be positive.' }
    $report.source_ini = [ordered]@{ configured_path=$IniPath; resolved_path=$iniResolved; section=$sourceSection }
    $report.metrics.policy = [ordered]@{
        offset = $offset
        length = $length
        sample_count = $sampleCount
        write_enabled_by_config = $configWriteEnabled
        write_enabled_by_switch = [bool]$EnableWriteTest
        write_enabled = $writeEnabled
    }
    $report.validation_layers.L1_configuration = 'PASS'

    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init
    $report.metrics.channels = [ordered]@{}
    $capFailures = @()
    $readFailures = @()
    $originalBuffers = [ordered]@{}

    foreach ($ch in $required) {
        $iniKey = @($storageSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) { throw "Required channel $ch is missing from [StorageArea]." }
        $meta = if ($channels.Contains([string]$ch)) { $channels[[string]$ch] } else { $null }
        if ($null -eq $meta) { throw "Missing JSON channel metadata for $ch." }
        $id = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $meta -Name 'storage_api_id' '')) 0
        if ($id -gt 11) { throw "Invalid Storage API ID for $ch." }

        $caps = [ordered]@{}
        foreach ($item in @('total_size','block_size','lock_status','password_max_length')) {
            $itemIds = Get-ConfigValue -Config (Get-ConfigValue -Config $config -Name 'capability_check' -Default ([ordered]@{})) -Name 'item_ids' -Default ([ordered]@{})
            $itemId = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $itemIds -Name $item '0')) 0
            $value = [UInt32]0
            $status = [NativeSusi]::SusiStorageGetCaps($id, $itemId, [ref]$value)
            Add-ApiCall -report $report -name ("StorageGetCaps:{0}:{1}" -f $ch,$item) -status $status -value $value
            $caps[$item] = [ordered]@{ status=('0x{0:X8}' -f $status); status_name=Get-StatusName $status; value=$value; item_id=('0x{0:X8}' -f $itemId) }
            if ($status -ne 0) { $capFailures += $ch }
        }

        $samples = @()
        for ($i=0; $i -lt $sampleCount; $i++) {
            $buffer = New-Object byte[] ([int]$length)
            $status = [NativeSusi]::SusiStorageAreaRead($id, $offset, $buffer, $length)
            Add-ApiCall -report $report -name ("StorageAreaRead:{0}:{1}" -f $ch,$i) -status $status -value ([ordered]@{ offset=$offset; length=$length })
            if ($status -eq 0) {
                if (-not $originalBuffers.Contains([string]$ch)) { $originalBuffers[[string]$ch] = $buffer.Clone() }
                $samples += [ordered]@{ status='SUSI_STATUS_SUCCESS'; status_code='0x00000000'; offset=$offset; length=$length; data_hex=(Convert-BytesToHex $buffer) }
            } else {
                $readFailures += $ch
                $samples += [ordered]@{ status=Get-StatusName $status; status_code=('0x{0:X8}' -f $status); offset=$offset; length=$length }
            }
        }
        $report.metrics.channels[$ch] = [ordered]@{ storage_api_id=('0x{0:X8}' -f $id); caps=$caps; read_samples=$samples }
    }

    if ($capFailures.Count -gt 0) {
        $report.result = 'FAIL_API'; $report.reason = 'StorageArea GetCaps failed.'
        $report.validation_layers.L2_capability = 'FAIL'; $report.validation_layers.L3_api = 'FAIL'
    } elseif ($readFailures.Count -gt 0) {
        $report.result = 'FAIL_READBACK'; $report.reason = 'StorageArea read failed.'
        $report.validation_layers.L2_capability = 'PASS'; $report.validation_layers.L3_api = 'FAIL'; $report.validation_layers.L4_readback = 'FAIL'
    } else {
        $report.result = 'PASS'; $report.reason = 'StorageArea capability and read validation passed.'
        $report.validation_layers.L2_capability = 'PASS'; $report.validation_layers.L3_api = 'PASS'; $report.validation_layers.L4_readback = 'PASS'
    }

    if (-not $writeEnabled) {
        $report.metrics.write_test = [ordered]@{ attempted=$false; result='NOT_RUN'; reason='Pass -EnableWriteTest and keep JSON write_check.enabled=true to run persistence write/restore.' }
        $report.validation_layers.L5_functional = 'CONDITIONAL'
        $report.validation_layers.L6_recovery = 'N_A'
    } else {
        $patternText = [string](Get-ConfigValue -Config $writeCheck -Name 'pattern_hex' 'A5')
        $pattern = [byte]([Convert]::ToByte($patternText.Trim().TrimStart('0','x','X'), 16))
        $writeResults = @()
        $writeFailures = @()
        foreach ($ch in $required) {
            $meta = $channels[[string]$ch]
            $id = Convert-ToUInt32Safe ([string](Get-ConfigValue -Config $meta -Name 'storage_api_id' '')) 0
            if (-not $originalBuffers.Contains([string]$ch)) { $writeFailures += $ch; continue }
            $candidate = [byte[]]$originalBuffers[[string]$ch].Clone()
            $candidate[0] = $pattern
            $writeStatus = [NativeSusi]::SusiStorageAreaWrite($id, $offset, $candidate, $length)
            Add-ApiCall -report $report -name ("StorageAreaWrite:{0}" -f $ch) -status $writeStatus -value ([ordered]@{ offset=$offset; length=$length; pattern_hex=('0x{0:X2}' -f $pattern) })
            $verifyStatus = [UInt32]0
            $verifyBuffer = New-Object byte[] ([int]$length)
            if ($writeStatus -eq 0) { $verifyStatus = [NativeSusi]::SusiStorageAreaRead($id, $offset, $verifyBuffer, $length) }
            $changed = ($verifyStatus -eq 0 -and $verifyBuffer[0] -eq $pattern)
            if (-not $changed) { $writeFailures += $ch }
            $restoreStatus = [UInt32]0
            $restoreVerifyStatus = [UInt32]0
            $restoreVerifyBuffer = New-Object byte[] ([int]$length)
            if ([bool](Get-ConfigValue -Config $writeCheck -Name 'restore_original' $true)) {
                $restoreStatus = [NativeSusi]::SusiStorageAreaWrite($id, $offset, [byte[]]$originalBuffers[[string]$ch], $length)
                Add-ApiCall -report $report -name ("StorageAreaRestore:{0}" -f $ch) -status $restoreStatus -value ([ordered]@{ offset=$offset; length=$length })
                if ($restoreStatus -eq 0) {
                    $restoreVerifyStatus = [NativeSusi]::SusiStorageAreaRead($id, $offset, $restoreVerifyBuffer, $length)
                    Add-ApiCall -report $report -name ("StorageAreaRestoreRead:{0}" -f $ch) -status $restoreVerifyStatus -value ([ordered]@{ offset=$offset; length=$length })
                }
                if ($restoreStatus -ne 0 -or $restoreVerifyStatus -ne 0 -or (Convert-BytesToHex $restoreVerifyBuffer) -ne (Convert-BytesToHex ([byte[]]$originalBuffers[[string]$ch])) ) { $writeFailures += $ch }
            }
            $writeResults += [ordered]@{ channel=$ch; write_status=Get-StatusName $writeStatus; verify_status=Get-StatusName $verifyStatus; changed=$changed; restore_status=Get-StatusName $restoreStatus; restore_verify_status=Get-StatusName $restoreVerifyStatus }
        }
        $report.metrics.write_test = [ordered]@{ attempted=$true; result=if ($writeFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }; samples=$writeResults; failed_channels=@($writeFailures | Select-Object -Unique) }
        $report.validation_layers.L5_functional = if ($writeFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        $report.validation_layers.L6_recovery = if ($writeFailures.Count -eq 0) { 'PASS' } else { 'FAIL' }
        if ($writeFailures.Count -gt 0) { $report.result = 'FAIL_FUNCTIONAL'; $report.reason = 'StorageArea write/verify/restore failed.' }
    }
    $report.checks.read_stability = if ($report.result -like 'FAIL*') { 'FAIL' } else { 'PASS' }
    $report.checks.control_effect = 'NOT_RUN'
    $report.checks.recovery = 'NOT_REQUIRED'
} catch {
    $report.result = 'FAIL_CONFIG'; $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
} finally {
    if ($initialized) { $u = Uninitialize-Susi; $report.uninit_status = Get-StatusName $u }
}

$report_path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'storage'
$report.report_path = $report_path
($report | ConvertTo-Json -Depth 12) | Set-Content -Encoding UTF8 -Path $report_path
if ($report.result -like 'FAIL*') { exit 1 } else { exit 0 }
