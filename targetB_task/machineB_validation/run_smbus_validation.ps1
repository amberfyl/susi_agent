param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_smbus.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out"
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

$report = New-ValidationReport -category "SMBus" -configPath $ConfigPath
$initialized = $false

try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir

    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'SMBus') {
        throw "source_ini.section must be SMBus, got '$sourceSection'."
    }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    # Hash mismatch no longer blocks validation. We always validate by section content/runtime behavior.
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $smbusSection = Get-IniSection -Sections $sections -Name 'SMBus'
    if ($null -eq $smbusSection) {
        throw "INI does not contain [SMBus]."
    }

    $required = @($config.required_channels)
    if ($required.Count -eq 0) {
        throw 'required_channels is empty.'
    }

    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $report.metrics.channels = [ordered]@{}

    foreach ($ch in $required) {
        $iniKey = @($smbusSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1) {
            throw "Required channel $ch is missing from [SMBus]."
        }
        $tuple = [string]$smbusSection[$iniKey[0]]
        if ([string]::IsNullOrWhiteSpace($tuple)) {
            throw "Required channel $ch has empty tuple in [SMBus]."
        }

        $meta = $null
        if ($channelDefs -is [System.Collections.IDictionary] -and $channelDefs.Contains([string]$ch)) {
            $meta = $channelDefs[[string]$ch]
        }
        if ($null -eq $meta) {
            throw "channels.$ch is missing in config."
        }

        $busIdx = [int](Get-ConfigValue -Config $meta -Name 'bus_index' -Default -1)
        $capBit = [int](Get-ConfigValue -Config $meta -Name 'capability_bit' -Default $busIdx)
        if ($busIdx -lt 0 -or $capBit -lt 0) {
            throw "Invalid bus_index/capability_bit for $ch"
        }

        $report.metrics.channels[$ch] = [ordered]@{
            tuple = $tuple
            bus_index = $busIdx
            capability_bit = $capBit
        }
    }

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $iniResolved
        section = $sourceSection
        sha256 = $actualHash
    }
    $report.validation_layers.L1_configuration = 'PASS'
    $report.checks.control_effect = 'NOT_APPLICABLE'

    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    $capabilityCfg = Get-ConfigValue -Config $config -Name 'capability_check' -Default ([ordered]@{})
    $sampleCount = [int](Get-ConfigValue -Config $capabilityCfg -Name 'sample_count' (Get-ConfigValue -Config $config -Name 'sample_count' 5))
    $sampleInterval = [int](Get-ConfigValue -Config $capabilityCfg -Name 'sample_interval_ms' (Get-ConfigValue -Config $config -Name 'sample_interval_ms' 200))
    $minSuccessRate = [double](Get-ConfigValue -Config $capabilityCfg -Name 'min_success_rate' (Get-ConfigValue -Config $config -Name 'minimum_success_rate' 1.0))
    $requireStableMask = [bool](Get-ConfigValue -Config $capabilityCfg -Name 'require_stable_mask' (Get-ConfigValue -Config $config -Name 'require_stable_mask' $true))
    $enforceMaskMatch = [bool](Get-ConfigValue -Config $capabilityCfg -Name 'enforce_mask_match' (Get-ConfigValue -Config $config -Name 'enforce_mask_match' $true))

    $supportedIdText = [string](Get-ConfigValue -Config $capabilityCfg -Name 'supported_id' (Get-ConfigValue -Config $config -Name 'supported_id' '0x00030000'))
    $supportedIdText = $supportedIdText.Trim()
    if ($supportedIdText.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
        $supportedIdText = $supportedIdText.Substring(2)
    }
    $supportedId = [UInt32]([Convert]::ToUInt32($supportedIdText, 16))

    $samples = @()
    for ($i = 0; $i -lt $sampleCount; $i++) {
        $r = Read-BoardValue -id $supportedId
        Add-ApiCall -report $report -name 'SusiBoardGetValue:SUSI_ID_SMBUS_SUPPORTED' -status $r.status -value $r.value
        if (Is-Success $r.status) {
            $samples += [ordered]@{ ts = (Get-Date).ToString('o'); value = [UInt32]$r.value }
        } else {
            $samples += [ordered]@{ ts = (Get-Date).ToString('o'); value = $null; status = Get-StatusName $r.status }
        }
        if ($i -lt ($sampleCount - 1)) { Start-Sleep -Milliseconds $sampleInterval }
    }

    $report.metrics.smbus_supported_id = ('0x{0:X8}' -f $supportedId)
    $report.metrics.smbus_supported_samples = $samples

    $okVals = @($samples | Where-Object { $null -ne $_.value } | ForEach-Object { [UInt32]$_.value })
    $successRate = if ($sampleCount -gt 0) { $okVals.Count / [double]$sampleCount } else { 0.0 }
    $report.metrics.smbus_supported_success_rate = [Math]::Round($successRate, 4)

    if ($okVals.Count -eq 0 -or $successRate -lt $minSuccessRate) {
        $report.result = 'FAIL_API'
        $report.reason = 'Cannot read stable SMBUS capability mask from SUSI_ID_SMBUS_SUPPORTED'
        $report.validation_layers.L2_capability = 'FAIL'
        $report.validation_layers.L3_api = 'FAIL'
        $report.checks.read_stability = 'FAIL'
    }

    if ($report.result -eq 'PENDING') {
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'

        $first = $okVals[0]
        $maskStable = $true
        foreach ($v in $okVals) {
            if ($v -ne $first) { $maskStable = $false; break }
        }
        $report.metrics.smbus_supported_mask = ('0x{0:X8}' -f [UInt32]$first)
        $report.metrics.smbus_supported_mask_stable = $maskStable

        if ($requireStableMask -and -not $maskStable) {
            $report.result = 'FAIL_API'
            $report.reason = 'SMBUS capability mask is not stable across samples'
            $report.validation_layers.L4_readback = 'FAIL'
            $report.checks.read_stability = 'FAIL'
        }

        if ($report.result -eq 'PENDING') {
            $unsupported = @()
            foreach ($ch in $required) {
                $meta = $report.metrics.channels[$ch]
                $bit = [int]$meta.capability_bit
                $isSupported = ((([UInt32]$first) -band ([UInt32](1 -shl $bit))) -ne 0)
                $meta.supported_by_mask = $isSupported
                if (-not $isSupported) { $unsupported += $ch }
            }
            $report.metrics.unsupported_channels = $unsupported

            if ($enforceMaskMatch -and $unsupported.Count -gt 0) {
                $report.result = 'FAIL_CAPABILITY'
                $report.reason = ('Unsupported SMBus channel(s) by capability mask: ' + ($unsupported -join ', '))
                $report.validation_layers.L4_readback = 'FAIL'
                $report.checks.read_stability = 'FAIL'
            } else {
                $report.validation_layers.L4_readback = 'PASS'
                $report.checks.read_stability = 'PASS'
                $report.validation_layers.L5_functional = 'CONDITIONAL'
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.recovery = 'NOT_REQUIRED'
                $report.result = 'CONDITIONAL'
                if ($unsupported.Count -gt 0) {
                    $report.reason = ('BLOCKED_REFERENCE: capability mask does not expose channel(s): ' + ($unsupported -join ', '))
                } else {
                    $report.reason = 'BLOCKED_FIXTURE: no approved SMBus slave/register contract for read-only transaction validation'
                }
            }
        }
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
        $supported = $report.metrics.channels[$ch].supported_by_mask
        if ($null -eq $supported) {
            $pendingItems += "channel:$ch"
        } elseif ([bool]$supported) {
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

$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'smbus'
Write-Output "DONE. Result=$($report.result); Report=$path"

switch ($report.result) {
    'PASS' { exit 0 }
    'CONDITIONAL' { exit 2 }
    default { exit 1 }
}
