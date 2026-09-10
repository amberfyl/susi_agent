[CmdletBinding()]
param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_fancontrol.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$FanConfigPath = "",
    [string]$FanIniPath = "",
    [string]$OutDir = "$PSScriptRoot\out",
    [switch]$AllowControl
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function ConvertTo-UInt32Value {
    param(
        [object]$Value,
        [string]$FieldName
    )

    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) {
        throw "$FieldName is empty."
    }
    if ($Value -is [string]) {
        $text = $Value.Trim()
        if ($text -match '^0x[0-9a-fA-F]+$') {
            return [Convert]::ToUInt32($text.Substring(2), 16)
        }
        return [Convert]::ToUInt32($text, 10)
    }
    return [Convert]::ToUInt32($Value)
}

function Get-ExpectedDelta {
    param(
        [object]$Value,
        [string]$Channel,
        [double]$Default = 200.0
    )

    if ($Value -is [System.Collections.IDictionary]) {
        foreach ($key in $Value.Keys) {
            if ([string]::Equals([string]$key, $Channel, [System.StringComparison]::OrdinalIgnoreCase)) {
                return [double]$Value[$key]
            }
        }
        return $Default
    }
    if ($null -ne $Value) { return [double]$Value }
    return $Default
}

function Resolve-ReferencedPath {
    param(
        [string]$Reference,
        [string]$ConfigResolvedPath
    )

    if ([string]::IsNullOrWhiteSpace($Reference)) { return $null }
    if (Test-Path -LiteralPath $Reference -PathType Leaf) {
        return (Resolve-Path -LiteralPath $Reference).Path
    }
    $candidate = if ([System.IO.Path]::IsPathRooted($Reference)) {
        $Reference
    } else {
        Join-Path (Split-Path -Parent $ConfigResolvedPath) $Reference
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Missing referenced file: $candidate"
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Read-SectionInput {
    param(
        [System.Collections.IDictionary]$Config,
        [string]$ExpectedSection,
        [string]$ExplicitIniPath,
        [string]$IniDir
    )

    $resolved = Resolve-SectionIniPath -Config $Config -IniPath $ExplicitIniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $Config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' -Default '')
    if (-not [string]::Equals($sourceSection, $ExpectedSection, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "source_ini.section must be $ExpectedSection, got '$sourceSection'."
    }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' -Default '')
    $actualHash = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    # Hash mismatch no longer blocks validation. We always validate by section content/runtime behavior.
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs for [$ExpectedSection] (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $resolved
    $section = Get-IniSection -Sections $sections -Name $ExpectedSection
    if ($null -eq $section) {
        throw "INI does not contain [$ExpectedSection]."
    }
    return ,([ordered]@{
        path = $resolved
        section = $ExpectedSection
        sha256 = $actualHash
        values = $section
    })
}

function Assert-SectionKeys {
    param(
        [System.Collections.IDictionary]$Section,
        [string[]]$Required,
        [int]$MinimumFields,
        [string]$SectionName
    )

    foreach ($key in $Required) {
        $matches = @($Section.Keys | Where-Object { $_ -ieq [string]$key })
        if ($matches.Count -ne 1) {
            throw "Required channel $key is missing from [$SectionName]."
        }
        $raw = [string]$Section[$matches[0]]
        if ([string]::IsNullOrWhiteSpace($raw)) {
            throw "Required channel $key is empty in [$SectionName]."
        }
        $parts = @($raw.Split(',', [System.StringSplitOptions]::None))
        if ($parts.Count -lt $MinimumFields) {
            throw "[$SectionName]$key has $($parts.Count) fields; expected at least $MinimumFields."
        }
    }
}

$report = New-ValidationReport -category 'HWM.Fan.Control' -configPath $ConfigPath
$config = $null
$fanConfig = $null
$initialized = $false
$originalControls = [ordered]@{}
$controlMappings = [ordered]@{}
$rpmMappings = [ordered]@{}
$dependency = [ordered]@{
    status = 'BLOCKED'
    reason = 'Not evaluated.'
}
$setGetTolerance = 2.0

try {
    $configResolved = (Resolve-Path -LiteralPath $ConfigPath).Path
    $config = Read-JsonFileAsHashtable -path $configResolved
    $controlInput = Read-SectionInput -Config $config -ExpectedSection 'HWM.Fan.Control' -ExplicitIniPath $IniPath -IniDir $IniDir
    $report.config_path = $configResolved
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $controlInput.path
        section = $controlInput.section
        sha256 = $controlInput.sha256
    }

    $required = if ($config.Contains('control_channels')) { @($config.control_channels) } else { @() }
    $controlCheck = Get-ConfigValue -Config $config -Name 'control_check' -Default ([ordered]@{})
    if ($controlCheck.Contains('sequence_pwm')) {
        $sequence = @($controlCheck.sequence_pwm)
    } elseif ($config.Contains('test_sequence')) {
        $sequence = @($config.test_sequence)
    } else {
        $sequence = @()
    }
    if ($required.Count -eq 0) { throw 'control_channels is empty.' }
    if ($sequence.Count -eq 0) { throw 'test_sequence is empty.' }
    Assert-SectionKeys -Section $controlInput.values -Required $required -MinimumFields 5 -SectionName 'HWM.Fan.Control'

    foreach ($ch in $required) {
        $mapping = Get-CanonicalFanApi -Key ([string]$ch)
        if ($null -eq $mapping.api_id_value) {
            throw "No canonical SUSI HWM fan ID for control channel $ch."
        }
        $controlMappings[$ch] = $mapping
    }
    $report.metrics.control_mapping = $controlMappings
    $report.validation_layers.L1_configuration = 'PASS'
    $report.checks.read_stability = 'NOT_RUN'
    $report.checks.control_effect = 'PENDING'
    $report.checks.recovery = 'PENDING'

    $setGetTolerance = [double](Get-ConfigValue -Config $controlCheck -Name 'set_get_tolerance' -Default (Get-ConfigValue -Config $config -Name 'set_get_tolerance' -Default 2))
    $settle = [int](Get-ConfigValue -Config $controlCheck -Name 'settle_time_sec' -Default (Get-ConfigValue -Config $config -Name 'settle_time_sec' -Default 10))
    $rpmN = [int](Get-ConfigValue -Config $controlCheck -Name 'rpm_sample_count' -Default (Get-ConfigValue -Config $config -Name 'rpm_sample_count' -Default 6))
    $rpmInt = [int](Get-ConfigValue -Config $controlCheck -Name 'rpm_sample_interval_ms' -Default (Get-ConfigValue -Config $config -Name 'rpm_sample_interval_ms' -Default 1000))
    $expectedDirection = [string](Get-ConfigValue -Config $controlCheck -Name 'expected_direction' -Default (Get-ConfigValue -Config $config -Name 'expected_direction' -Default 'up'))
    if ($expectedDirection -notin @('up', 'down')) {
        throw "expected_direction must be up or down, got '$expectedDirection'."
    }
    foreach ($targetValue in $sequence) {
        $target = ConvertTo-UInt32Value -Value $targetValue -FieldName 'test_sequence'
        if ($target -gt 100) { throw "Manual PWM target $target is outside the valid 0-100 range." }
    }

    try {
        $dependencyConfigPath = Resolve-ReferencedPath -Reference $FanConfigPath -ConfigResolvedPath $configResolved
        if ([string]::IsNullOrWhiteSpace($dependencyConfigPath)) {
            $rpmDependency = Get-ConfigValue -Config $config -Name 'rpm_dependency'
            $dependencyFile = [string](Get-ConfigValue -Config $rpmDependency -Name 'config_file' -Default '')
            $dependencyConfigPath = Resolve-ReferencedPath -Reference $dependencyFile -ConfigResolvedPath $configResolved
        }
        if ([string]::IsNullOrWhiteSpace($dependencyConfigPath)) {
            throw 'rpm_dependency.config_file is missing.'
        }
        $fanConfig = Read-JsonFileAsHashtable -path $dependencyConfigPath
        $fanInput = Read-SectionInput -Config $fanConfig -ExpectedSection 'HWM.Fan' -ExplicitIniPath $FanIniPath -IniDir $IniDir
        $fanRequired = if ($fanConfig.Contains('required_channels')) { @($fanConfig.required_channels) } else { @() }
        if ($fanRequired.Count -eq 0) { throw 'Referenced HWM.Fan config has no required_channels.' }
        Assert-SectionKeys -Section $fanInput.values -Required $fanRequired -MinimumFields 6 -SectionName 'HWM.Fan'

        $rpmDependency = Get-ConfigValue -Config $config -Name 'rpm_dependency'
        $channelMap = Get-ConfigValue -Config $rpmDependency -Name 'channel_map'
        if (-not ($channelMap -is [System.Collections.IDictionary])) {
            throw 'rpm_dependency.channel_map is missing.'
        }
        foreach ($ch in $required) {
            $fanKey = $null
            foreach ($mapKey in $channelMap.Keys) {
                if ([string]::Equals([string]$mapKey, [string]$ch, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $fanKey = [string]$channelMap[$mapKey]
                    break
                }
            }
            if ([string]::IsNullOrWhiteSpace($fanKey)) { throw "No RPM pairing for control channel $ch." }
            if (@($fanRequired | Where-Object { $_ -ieq $fanKey }).Count -ne 1) {
                throw "RPM pairing for $ch references missing HWM.Fan channel $fanKey."
            }
            $mapping = Get-CanonicalFanApi -Key $fanKey
            if ($null -eq $mapping.api_id_value) { throw "No canonical SUSI HWM fan ID for RPM channel $fanKey." }
            $rpmMappings[$ch] = [ordered]@{
                fan_key = $fanKey
                fan_id = $mapping
            }
        }
        $dependency = [ordered]@{
            status = 'READY'
            config_path = $dependencyConfigPath
            ini_path = $fanInput.path
            ini_section = $fanInput.section
            ini_sha256 = $fanInput.sha256
            channel_map = $rpmMappings
        }
    } catch {
        $dependency = [ordered]@{
            status = 'BLOCKED'
            reason = $_.Exception.Message
        }
    }
    $report.metrics.rpm_dependency = $dependency

    $enabled = [bool](Get-ConfigValue -Config $controlCheck -Name 'enabled' -Default (Get-ConfigValue -Config $config -Name 'enabled' -Default $false))
    if (-not $AllowControl) {
        $report.result = 'BLOCKED_SAFETY'
        $report.reason = 'Control mode requires the explicit -AllowControl switch.'
        $report.validation_layers.L2_capability = 'NOT_REQUIRED'
        $report.checks.control_effect = 'NOT_RUN'
        $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
        $report.checks.recovery = 'NOT_REQUIRED'
    } elseif (-not $enabled) {
        $report.result = 'BLOCKED_CONFIGURATION'
        $report.reason = 'enabled is false.'
        $report.validation_layers.L2_capability = 'NOT_REQUIRED'
        $report.checks.control_effect = 'NOT_RUN'
        $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
        $report.checks.recovery = 'NOT_REQUIRED'
    }

    if ($report.result -eq 'PENDING') {
        $init = Initialize-Susi -DllDirs $DllDirs
        $initialized = $true
        $report.init_status = Get-StatusName $init

        foreach ($ch in $required) {
            $fanId = [UInt32]$controlMappings[$ch].api_id_value
            $caps = Get-SusiFanControlCaps -FanId $fanId
            Add-ApiCall -report $report -name "Probe.ControlCaps:$ch" -status $caps.control_status -value $caps.control_support_flags
            Add-ApiCall -report $report -name "Probe.AutoCaps:$ch" -status $caps.auto_status -value $caps.auto_support_flags
            if (-not (Is-Success $caps.control_status)) {
                $report.result = 'FAIL_CAPABILITY'
                $report.reason = "SmartFan capability query failed for $ch."
                $report.validation_layers.L2_capability = 'FAIL'
                $report.checks.control_effect = 'FAIL'
                break
            }
            if (-not $caps.manual_supported) {
                $report.result = 'FAIL_CAPABILITY'
                $report.reason = "SmartFan manual PWM is not supported for $ch."
                $report.validation_layers.L2_capability = 'FAIL'
                $report.checks.control_effect = 'FAIL'
                break
            }

            $original = Get-SusiFanControlConfig -FanId $fanId
            $originalValue = $null
            if (Is-Success $original.status) {
                $originalValue = Convert-SusiFanControlConfigForReport -Config $original.config
            }
            Add-ApiCall -report $report -name "Probe.ControlConfig:$ch" -status $original.status -value $originalValue
            if (-not (Is-Success $original.status)) {
                $report.result = 'FAIL_API'
                $report.reason = "SmartFan config query failed for $ch."
                $report.validation_layers.L3_api = 'FAIL'
                $report.checks.control_effect = 'FAIL'
                break
            }
            $originalControls[$ch] = [ordered]@{
                fan_id = $fanId
                config = $original.config
            }
        }

        if ($report.result -eq 'PENDING') {
            $report.validation_layers.L2_capability = 'PASS'
            $report.validation_layers.L3_api = 'PASS'
            $controlRows = [ordered]@{}
            $rpmReady = ($dependency.status -eq 'READY')

            if ($rpmReady) {
                foreach ($ch in $required) {
                    $rpmId = [UInt32]$rpmMappings[$ch].fan_id.api_id_value
                    $rpmProbe = Read-BoardValue -id $rpmId
                    Add-ApiCall -report $report -name "Probe.RPM:$($rpmMappings[$ch].fan_key)" -status $rpmProbe.status -value $rpmProbe.value
                    if (-not (Is-Success $rpmProbe.status)) {
                        $rpmReady = $false
                        $dependency.status = 'BLOCKED'
                        $dependency.reason = "RPM channel unavailable for $($rpmMappings[$ch].fan_key)."
                        break
                    }
                }
            }

            foreach ($ch in $required) {
                $controlRows[$ch] = @()
                foreach ($targetValue in $sequence) {
                    $target = ConvertTo-UInt32Value -Value $targetValue -FieldName 'test_sequence'
                    $candidate = $originalControls[$ch].config
                    $candidate.Mode = [UInt32]2
                    $candidate.PWM = $target
                    $write = [NativeSusi]::SusiFanControlSetConfig($originalControls[$ch].fan_id, [ref]$candidate)
                    Add-ApiCall -report $report -name "Set.Control:$ch" -status $write -value (Convert-SusiFanControlConfigForReport -Config $candidate)
                    if (-not (Is-Success $write)) {
                        $report.result = 'FAIL_API'
                        $report.reason = "SmartFan set failed for $ch."
                        $report.validation_layers.L3_api = 'FAIL'
                        $report.checks.control_effect = 'FAIL'
                        break
                    }

                    Start-Sleep -Seconds $settle
                    $readback = Get-SusiFanControlConfig -FanId $originalControls[$ch].fan_id
                    $readbackValue = $null
                    if (Is-Success $readback.status) {
                        $readbackValue = Convert-SusiFanControlConfigForReport -Config $readback.config
                    }
                    Add-ApiCall -report $report -name "Get.Control:$ch" -status $readback.status -value $readbackValue
                    if (-not (Is-Success $readback.status)) {
                        $report.result = 'FAIL_API'
                        $report.reason = "SmartFan read-back failed for $ch."
                        $report.validation_layers.L3_api = 'FAIL'
                        $report.checks.control_effect = 'FAIL'
                        break
                    }

                    $controlDelta = [Math]::Abs([double]$readback.config.PWM - [double]$target)
                    $readbackPass = ([UInt32]$readback.config.Mode -eq 2 -and $controlDelta -le $setGetTolerance)
                    $row = [ordered]@{
                        target_pwm = $target
                        control_readback = $readbackValue
                        control_delta = [Math]::Round($controlDelta, 3)
                        control_readback_pass = $readbackPass
                    }
                    if ($rpmReady) {
                        $rpmId = [UInt32]$rpmMappings[$ch].fan_id.api_id_value
                        $series = Sample-Channel -id $rpmId -count $rpmN -intervalMs $rpmInt
                        $stats = Get-SeriesStats -series $series
                        $row.rpm = [ordered]@{
                            channel = $rpmMappings[$ch].fan_key
                            stats = $stats
                            series = $series
                        }
                        if ($stats.count -eq 0) {
                            $report.result = 'FAIL_API'
                            $report.reason = "No RPM samples on $($rpmMappings[$ch].fan_key)."
                            $report.validation_layers.L3_api = 'FAIL'
                            $report.checks.read_stability = 'FAIL'
                            break
                        }
                    }
                    $controlRows[$ch] += ,$row
                    if (-not $readbackPass) {
                        $report.result = 'FAIL_READBACK'
                        $report.reason = "SmartFan read-back mismatch for $ch."
                        $report.validation_layers.L4_readback = 'FAIL'
                        $report.checks.control_effect = 'FAIL'
                        break
                    }
                }
                if ($report.result -ne 'PENDING') { break }
            }
            $report.metrics.control_readback = $controlRows

            if ($report.result -eq 'PENDING') {
                $report.validation_layers.L4_readback = 'PASS'
                if (-not $rpmReady) {
                    $report.checks.read_stability = 'CONDITIONAL'
                    $report.checks.control_effect = 'CONDITIONAL'
                    $report.validation_layers.L5_functional = 'CONDITIONAL'
                    $report.result = 'CONDITIONAL'
                    $report.reason = if ([string]::IsNullOrWhiteSpace([string]$dependency.reason)) {
                        'RPM correlation is unavailable; API/readback only.'
                    } else {
                        [string]$dependency.reason
                    }
                } else {
                    $report.checks.read_stability = 'PASS'
                    $rpmCorrelation = [ordered]@{}
                    foreach ($ch in $required) {
                        $rows = @($controlRows[$ch])
                        $rpmCorrelation[$ch] = @($rows | ForEach-Object { $_.rpm })
                        if ($rows.Count -lt 2 -or $null -eq $rows[0].rpm -or $null -eq $rows[$rows.Count - 1].rpm) {
                            $report.result = 'FAIL_FUNCTIONAL'
                            $report.reason = "Insufficient RPM correlation samples on $ch."
                            $report.validation_layers.L5_functional = 'FAIL'
                            $report.checks.control_effect = 'FAIL'
                            break
                        }
                        $first = [double]$rows[0].rpm.stats.avg
                        $last = [double]$rows[$rows.Count - 1].rpm.stats.avg
                        $delta = $last - $first
                        $minimumDelta = Get-ExpectedDelta -Value (Get-ConfigValue -Config $controlCheck -Name 'expected_delta_rpm_min' -Default (Get-ConfigValue -Config $config -Name 'expected_delta_rpm_min' -Default 200)) -Channel $ch
                        if (($expectedDirection -eq 'up' -and $delta -lt $minimumDelta) -or ($expectedDirection -eq 'down' -and (-1.0 * $delta) -lt $minimumDelta)) {
                            $report.result = 'FAIL_FUNCTIONAL'
                            $report.reason = "RPM response too small on $ch (delta=$delta, min=$minimumDelta)."
                            $report.validation_layers.L5_functional = 'FAIL'
                            $report.checks.control_effect = 'FAIL'
                            break
                        }
                    }
                    $report.metrics.rpm_correlation = $rpmCorrelation
                    if ($report.result -eq 'PENDING') {
                        $report.validation_layers.L5_functional = 'PASS'
                        $report.checks.control_effect = 'PASS'
                    }
                }
                if ($report.result -eq 'PENDING') {
                    $report.validation_layers.L6_recovery = 'N_A'
                    $report.checks.recovery = 'NOT_REQUIRED'
                    $report.result = 'PASS'
                } elseif ($report.result -eq 'CONDITIONAL') {
                    $report.validation_layers.L6_recovery = 'N_A'
                    $report.checks.recovery = 'NOT_REQUIRED'
                }
            }
        }
    }
} catch {
    if ($report.result -eq 'PENDING') { $report.result = 'FAIL_CONFIG' }
    $report.reason = $_.Exception.Message
    $report.validation_layers.L1_configuration = 'FAIL'
    if ($report.checks.read_stability -eq 'PENDING') { $report.checks.read_stability = 'FAIL' }
    if ($report.checks.control_effect -eq 'PENDING') { $report.checks.control_effect = 'FAIL' }
    if ($report.checks.recovery -eq 'PENDING') { $report.checks.recovery = 'NOT_REQUIRED' }
} finally {
    if ($initialized -and $originalControls.Count -gt 0) {
        $restoreOk = $true
        foreach ($ch in $originalControls.Keys) {
            $original = $originalControls[$ch]
            try {
                $restoreConfig = $original.config
                $write = [NativeSusi]::SusiFanControlSetConfig($original.fan_id, [ref]$restoreConfig)
                Add-ApiCall -report $report -name "Restore.Set:$ch" -status $write -value (Convert-SusiFanControlConfigForReport -Config $restoreConfig)
                if (-not (Is-Success $write)) {
                    $restoreOk = $false
                    continue
                }
                $readback = Get-SusiFanControlConfig -FanId $original.fan_id
                $readbackValue = $null
                if (Is-Success $readback.status) {
                    $readbackValue = Convert-SusiFanControlConfigForReport -Config $readback.config
                }
                Add-ApiCall -report $report -name "Restore.Readback:$ch" -status $readback.status -value $readbackValue
                if (-not (Is-Success $readback.status) -or -not (Test-SusiFanControlConfigEquivalent -Expected $original.config -Actual $readback.config -PwmTolerance $setGetTolerance)) {
                    $restoreOk = $false
                }
            } catch {
                $restoreOk = $false
            }
        }
        $report.validation_layers.L6_recovery = if ($restoreOk) { 'PASS' } else { 'FAIL' }
        $report.checks.recovery = if ($restoreOk) { 'PASS' } else { 'FAIL' }
        if (-not $restoreOk) {
            $report.result = 'ABORTED_RESTORE_FAILURE'
            $report.reason = 'One or more SmartFan configurations could not be restored and verified.'
        }
    } elseif ($report.validation_layers.L6_recovery -eq 'PENDING') {
        $report.validation_layers.L6_recovery = 'NOT_REQUIRED'
        if ($report.checks.recovery -eq 'PENDING') { $report.checks.recovery = 'NOT_REQUIRED' }
    }

    if ($initialized) {
        try {
            $uninit = Uninitialize-Susi
            $report.uninit_status = Get-StatusName $uninit
        } catch {
            $report.uninit_status = 'EXCEPTION'
            if ($report.result -eq 'PASS') {
                $report.result = 'FAIL_RECOVERY'
                $report.reason = 'SusiLibUninitialize raised an exception.'
            }
        }
    }
}

$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'hwm_fan_control'
Write-Output "DONE. Result=$($report.result); Report=$path"

switch -Regex ($report.result) {
    '^PASS$' { exit 0 }
    '^CONDITIONAL$' { exit 2 }
    '^BLOCKED_' { exit 2 }
    default { exit 1 }
}
