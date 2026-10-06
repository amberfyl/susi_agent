param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\i2c.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableSetTest
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function Convert-ToUInt32Strict {
    param([object]$Value, [string]$Name)
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text)) { throw "$Name is empty." }
    $text = $text.Trim()
    try {
        if ($text.StartsWith('0x', [System.StringComparison]::OrdinalIgnoreCase)) {
            return [UInt32]([Convert]::ToUInt32($text.Substring(2), 16))
        }
        return [UInt32]([Convert]::ToUInt32($text, 10))
    } catch {
        throw "$Name is not a valid UInt32: '$Value'."
    }
}

$report = New-ValidationReport -category 'I2C' -configPath $ConfigPath
$initialized = $false
$stage = 'configuration'

try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    if ([string](Get-ConfigValue -Config $config -Name 'category' '') -ne 'I2C') {
        throw "config.category must be I2C."
    }

    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'I2C') { throw "source_ini.section must be I2C, got '$sourceSection'." }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expectedHash -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config. Continue with tuple-level validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $i2cSection = Get-IniSection -Sections $sections -Name 'I2C'
    if ($null -eq $i2cSection) { throw 'INI does not contain [I2C].' }

    $required = @($config.required_channels)
    if ($required.Count -eq 0) { throw 'required_channels is empty.' }
    $channelDefs = Get-ConfigValue -Config $config -Name 'channels' -Default ([ordered]@{})
    $channelMetrics = [ordered]@{}
    foreach ($channel in $required) {
        $iniKeys = @($i2cSection.Keys | Where-Object { $_ -ieq [string]$channel })
        if ($iniKeys.Count -ne 1) { throw "Required channel $channel is missing from [I2C]." }
        $actualTuple = [string]$i2cSection[$iniKeys[0]]
        if ([string]::IsNullOrWhiteSpace($actualTuple)) { throw "Required channel $channel has an empty tuple." }
        if (-not ($channelDefs -is [System.Collections.IDictionary]) -or -not $channelDefs.Contains([string]$channel)) {
            throw "channels.$channel is missing in config."
        }
        $meta = $channelDefs[[string]$channel]
        $expectedTuple = [string](Get-ConfigValue -Config $meta -Name 'raw_tuple' '')
        if ($actualTuple -ne $expectedTuple) {
            throw "Tuple mismatch for [I2C]$channel. Expected '$expectedTuple', got '$actualTuple'."
        }
        $apiId = Convert-ToUInt32Strict (Get-ConfigValue -Config $meta -Name 'i2c_api_id' '') "channels.$channel.i2c_api_id"
        $capabilityBit = [int](Get-ConfigValue -Config $meta -Name 'capability_bit' -1)
        if ($capabilityBit -lt 0 -or $capabilityBit -gt 31) { throw "Invalid capability_bit for $channel." }
        $channelMetrics[$channel] = [ordered]@{
            tuple = $actualTuple
            encoded_channel = [string](Get-ConfigValue -Config $meta -Name 'encoded_channel' '')
            i2c_api_id = ('0x{0:X8}' -f $apiId)
            i2c_api_id_value = $apiId
            capability_bit = $capabilityBit
            supported_by_mask = $false
            caps = $null
            frequency = $null
        }
    }

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $iniResolved
        section = $sourceSection
        sha256 = $actualHash
    }
    $report.metrics.channels = $channelMetrics
    $report.validation_layers.L1_configuration = 'PASS'
    $report.checks.control_effect = 'NOT_APPLICABLE'

    $stage = 'runtime'
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    $capCfg = Get-ConfigValue -Config $config -Name 'capability_check' -Default ([ordered]@{})
    $supportedId = Convert-ToUInt32Strict (Get-ConfigValue -Config $capCfg -Name 'supported_id' '0x00030100') 'capability_check.supported_id'
    $sampleCount = [int](Get-ConfigValue -Config $capCfg -Name 'sample_count' 5)
    $sampleIntervalMs = [int](Get-ConfigValue -Config $capCfg -Name 'sample_interval_ms' 200)
    $minSuccessRate = [double](Get-ConfigValue -Config $capCfg -Name 'min_success_rate' 1.0)
    $requireStableMask = [bool](Get-ConfigValue -Config $capCfg -Name 'require_stable_mask' $true)
    $enforceMaskMatch = [bool](Get-ConfigValue -Config $capCfg -Name 'enforce_mask_match' $true)
    if ($sampleCount -le 0 -or $sampleIntervalMs -lt 0) { throw 'Invalid capability sampling policy.' }

    $samples = @()
    for ($i = 0; $i -lt $sampleCount; $i++) {
        $sample = Read-BoardValue -id $supportedId
        Add-ApiCall -report $report -name 'SusiBoardGetValue:SUSI_ID_I2C_SUPPORTED' -status $sample.status -value $sample.value
        $samples += [ordered]@{
            ts = (Get-Date).ToString('o')
            status = Get-StatusName $sample.status
            status_code = ('0x{0:X8}' -f [UInt32]$sample.status)
            value = if (Is-Success $sample.status) { [UInt32]$sample.value } else { $null }
        }
        if ($i -lt ($sampleCount - 1)) { Start-Sleep -Milliseconds $sampleIntervalMs }
    }

    $okValues = @($samples | Where-Object { $null -ne $_.value } | ForEach-Object { [UInt32]$_.value })
    $successRate = if ($sampleCount -gt 0) { $okValues.Count / [double]$sampleCount } else { 0.0 }
    $maskStable = $false
    if ($okValues.Count -gt 0) {
        $maskStable = @($okValues | Where-Object { $_ -ne $okValues[0] }).Count -eq 0
    }
    $mask = if ($okValues.Count -gt 0) { [UInt32]$okValues[0] } else { [UInt32]0 }
    $report.metrics.i2c_supported_id = ('0x{0:X8}' -f $supportedId)
    $report.metrics.i2c_supported_samples = $samples
    $report.metrics.i2c_supported_success_rate = [Math]::Round($successRate, 4)
    $report.metrics.i2c_supported_mask = ('0x{0:X8}' -f $mask)
    $report.metrics.i2c_supported_mask_stable = $maskStable

    $unsupportedChannels = @()
    foreach ($channel in $required) {
        $meta = $channelMetrics[$channel]
        $supported = (($mask -band ([UInt32](1 -shl [int]$meta.capability_bit))) -ne 0)
        $meta.supported_by_mask = $supported
        if (-not $supported) { $unsupportedChannels += $channel }
    }
    $report.metrics.unsupported_channels = $unsupportedChannels

    if ($okValues.Count -eq 0 -or $successRate -lt $minSuccessRate) {
        $report.result = 'FAIL_API'
        $report.reason = 'Cannot read SUSI_ID_I2C_SUPPORTED at the required success rate.'
        $report.validation_layers.L2_capability = 'FAIL'
        $report.validation_layers.L3_api = 'FAIL'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($requireStableMask -and -not $maskStable) {
        $report.result = 'FAIL_READBACK'
        $report.reason = 'SUSI_ID_I2C_SUPPORTED mask is not stable across samples.'
        $report.validation_layers.L2_capability = 'FAIL'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
    } elseif ($enforceMaskMatch -and $unsupportedChannels.Count -gt 0) {
        $report.result = 'FAIL_CAPABILITY'
        $report.reason = 'INI declares I2C channel(s) absent from SUSI_ID_I2C_SUPPORTED: ' + ($unsupportedChannels -join ', ')
        $report.validation_layers.L2_capability = 'FAIL'
        $report.validation_layers.L3_api = 'PASS'
        $report.validation_layers.L4_readback = 'FAIL'
    } else {
        $report.validation_layers.L2_capability = 'PASS'

        $frequencyCfg = Get-ConfigValue -Config $config -Name 'frequency_check' -Default ([ordered]@{})
        $minKHz = [UInt32](Get-ConfigValue -Config $frequencyCfg -Name 'min_khz' 1)
        $maxKHz = [UInt32](Get-ConfigValue -Config $frequencyCfg -Name 'max_khz' 1000)
        $requireFrequency = [bool](Get-ConfigValue -Config $frequencyCfg -Name 'require_api_success' $false)
        $capsCfg = Get-ConfigValue -Config $config -Name 'caps_check' -Default ([ordered]@{})
        $capsItemId = Convert-ToUInt32Strict (Get-ConfigValue -Config $capsCfg -Name 'maximum_block_length_item_id' '0x00000000') 'caps_check.maximum_block_length_item_id'
        $requireCaps = [bool](Get-ConfigValue -Config $capsCfg -Name 'require_api_success' $false)

        $apiFailures = @()
        $readbackFailures = @()
        foreach ($channel in $required) {
            $meta = $channelMetrics[$channel]
            $apiId = [UInt32]$meta.i2c_api_id_value

            [UInt32]$maximumBlockLength = 0
            $capsStatus = [NativeSusi]::SusiI2CGetCaps($apiId, $capsItemId, [ref]$maximumBlockLength)
            Add-ApiCall -report $report -name "SusiI2CGetCaps:$channel" -status $capsStatus -value $maximumBlockLength
            $capsAccepted = (Is-Success $capsStatus) -or ((-not $requireCaps) -and $capsStatus -eq [Convert]::ToUInt32('FFFFFCFF', 16))
            $meta.caps = [ordered]@{
                item_id = ('0x{0:X8}' -f $capsItemId)
                status = Get-StatusName $capsStatus
                maximum_block_length = if (Is-Success $capsStatus) { $maximumBlockLength } else { $null }
                required = $requireCaps
            }
            if (-not $capsAccepted) { $apiFailures += "${channel}:GetCaps" }
            if ((Is-Success $capsStatus) -and $maximumBlockLength -eq 0) { $readbackFailures += "${channel}:MaximumBlockLength" }

            [UInt32]$frequencyKHz = 0
            $frequencyStatus = [NativeSusi]::SusiI2CGetFrequency($apiId, [ref]$frequencyKHz)
            Add-ApiCall -report $report -name "SusiI2CGetFrequency:$channel" -status $frequencyStatus -value $frequencyKHz
            $frequencyAccepted = (Is-Success $frequencyStatus) -or ((-not $requireFrequency) -and $frequencyStatus -eq [Convert]::ToUInt32('FFFFFCFF', 16))
            $frequencyInRange = (-not (Is-Success $frequencyStatus)) -or ($frequencyKHz -ge $minKHz -and $frequencyKHz -le $maxKHz)
            $meta.frequency = [ordered]@{
                status = Get-StatusName $frequencyStatus
                khz = if (Is-Success $frequencyStatus) { $frequencyKHz } else { $null }
                required = $requireFrequency
                in_range = $frequencyInRange
            }
            if (-not $frequencyAccepted) { $apiFailures += "${channel}:GetFrequency" }
            if (-not $frequencyInRange) { $readbackFailures += "${channel}:Frequency" }
        }

        if ($apiFailures.Count -gt 0) {
            $report.result = 'FAIL_API'
            $report.reason = 'I2C host API failure(s): ' + ($apiFailures -join ', ')
            $report.validation_layers.L3_api = 'FAIL'
            $report.validation_layers.L4_readback = 'FAIL'
        } elseif ($readbackFailures.Count -gt 0) {
            $report.result = 'FAIL_READBACK'
            $report.reason = 'I2C host API returned invalid value(s): ' + ($readbackFailures -join ', ')
            $report.validation_layers.L3_api = 'PASS'
            $report.validation_layers.L4_readback = 'FAIL'
        } else {
            # Phase 1 write path: change the bus frequency, read it back, restore it.
            # Falls back to a device probe scan when SetFrequency is unsupported.
            $setCfg = Get-ConfigValue -Config $config -Name 'frequency_set_check' -Default ([ordered]@{})
            $alternateKHz = [UInt32](Get-ConfigValue -Config $setCfg -Name 'alternate_khz' 100)
            $fallbackKHz = [UInt32](Get-ConfigValue -Config $setCfg -Name 'fallback_khz' 400)
            $unsupportedStatus = [Convert]::ToUInt32('FFFFFCFF', 16)
            $setTests = [ordered]@{}
            $setFailures = @()
            $restoreFailures = @()
            $verified = @()
            $unverified = @()
            foreach ($channel in $required) {
                $meta = $channelMetrics[$channel]
                $apiId = [UInt32]$meta.i2c_api_id_value
                $test = [ordered]@{
                    original_khz = $null; target_khz = $null; set_status = $null; readback_khz = $null
                    restore_status = $null; restored_khz = $null; probe_devices = @(); result = 'NOT_RUN'
                }
                $setTests[$channel] = $test

                if ($EnableSetTest -and $null -ne $meta.frequency.khz) {
                    $originalKHz = [UInt32]$meta.frequency.khz
                    $targetKHz = if ($originalKHz -ne $alternateKHz) { $alternateKHz } else { $fallbackKHz }
                    $test.original_khz = $originalKHz
                    $test.target_khz = $targetKHz
                    $setStatus = [NativeSusi]::SusiI2CSetFrequency($apiId, $targetKHz)
                    Add-ApiCall -report $report -name "SusiI2CSetFrequency:$channel" -status $setStatus -value $targetKHz
                    $test.set_status = Get-StatusName $setStatus
                    if (Is-Success $setStatus) {
                        [UInt32]$readKHz = 0
                        $readStatus = [NativeSusi]::SusiI2CGetFrequency($apiId, [ref]$readKHz)
                        Add-ApiCall -report $report -name "SusiI2CGetFrequency:${channel}:readback" -status $readStatus -value $readKHz
                        if (Is-Success $readStatus) { $test.readback_khz = $readKHz }

                        $restoreStatus = [NativeSusi]::SusiI2CSetFrequency($apiId, $originalKHz)
                        Add-ApiCall -report $report -name "SusiI2CSetFrequency:${channel}:restore" -status $restoreStatus -value $originalKHz
                        [UInt32]$restoredKHz = 0
                        $restoredStatus = [NativeSusi]::SusiI2CGetFrequency($apiId, [ref]$restoredKHz)
                        Add-ApiCall -report $report -name "SusiI2CGetFrequency:${channel}:restored" -status $restoredStatus -value $restoredKHz
                        $test.restore_status = Get-StatusName $restoreStatus
                        if (Is-Success $restoredStatus) { $test.restored_khz = $restoredKHz }

                        $restored = (Is-Success $restoreStatus) -and (Is-Success $restoredStatus) -and ($restoredKHz -eq $originalKHz)
                        $changed = (Is-Success $readStatus) -and ($readKHz -ne $originalKHz)
                        if (-not $restored) {
                            $test.result = 'FAIL_RESTORE'
                            $restoreFailures += $channel
                        } elseif (-not $changed) {
                            $test.result = 'FAIL_READBACK'
                            $setFailures += "${channel}:FrequencyReadback"
                        } else {
                            $test.result = 'PASS'
                            $verified += ('{0}({1}->{2}->{3} kHz)' -f $channel, $originalKHz, $readKHz, $restoredKHz)
                        }
                        continue
                    }
                    if ($setStatus -ne $unsupportedStatus) {
                        $test.result = 'FAIL_SET'
                        $setFailures += "${channel}:SetFrequency"
                        continue
                    }
                }

                # Set test disabled, SetFrequency unsupported, or no frequency:
                # read-only probe of 7-bit addresses 0x08..0x77.
                $found = @()
                for ($address = 0x08; $address -le 0x77; $address++) {
                    $probeStatus = [NativeSusi]::SusiI2CProbeDevice($apiId, [UInt32]($address -shl 1))
                    if (Is-Success $probeStatus) { $found += ('0x{0:X2}' -f $address) }
                }
                Add-ApiCall -report $report -name "SusiI2CProbeDevice:${channel}:scan" -status ([UInt32]0) -value ($found -join ',')
                $test.probe_devices = $found
                if ($found.Count -gt 0) {
                    $test.result = 'PASS_PROBE'
                    $verified += ('{0}(device ACK at {1})' -f $channel, ($found -join ','))
                } else {
                    $test.result = 'NO_DEVICE_RESPONDED'
                    $unverified += $channel
                }
            }
            $report.metrics.frequency_set_test = $setTests

            $report.validation_layers.L3_api = 'PASS'
            $report.validation_layers.L5_functional = 'PENDING_FIXTURE'
            if ($restoreFailures.Count -gt 0) {
                $report.result = 'FAIL_READBACK'
                $report.reason = 'I2C frequency could not be restored on: ' + ($restoreFailures -join ', ')
                $report.validation_layers.L4_readback = 'FAIL'
                $report.validation_layers.L6_recovery = 'FAIL'
                $report.checks.recovery = 'FAIL'
            } elseif ($setFailures.Count -gt 0) {
                $report.result = 'FAIL_READBACK'
                $report.reason = 'I2C frequency set/readback failed: ' + ($setFailures -join ', ')
                $report.validation_layers.L4_readback = 'FAIL'
                $report.validation_layers.L6_recovery = 'PASS'
                $report.checks.recovery = 'PASS'
            } elseif ($unverified.Count -gt 0) {
                $report.result = 'CONDITIONAL'
                $prefix = if ($EnableSetTest) { 'SetFrequency unsupported' } else { 'Set test disabled' }
                $report.reason = $prefix + ' and no device responded on: ' + ($unverified -join ', ') + '; bus path not exercised.'
                $report.validation_layers.L4_readback = 'CONDITIONAL'
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.recovery = 'NOT_REQUIRED'
            } else {
                $report.result = 'PASS'
                $report.reason = 'I2C API path verified: ' + ($verified -join '; ')
                $report.validation_layers.L4_readback = 'PASS'
                # Recovery applies only when a frequency was actually changed.
                $changedAny = @($setTests.Values | Where-Object { $_.result -eq 'PASS' }).Count -gt 0
                $report.validation_layers.L6_recovery = if ($changedAny) { 'PASS' } else { 'N_A' }
                $report.checks.recovery = if ($changedAny) { 'PASS' } else { 'NOT_REQUIRED' }
            }
            $report.checks.read_stability = 'PASS'
        }
    }

    if ($report.validation_layers.L5_functional -eq 'PENDING') { $report.validation_layers.L5_functional = 'PENDING_FIXTURE' }
    if ($report.validation_layers.L6_recovery -eq 'PENDING') { $report.validation_layers.L6_recovery = 'N_A' }
    if ($report.checks.read_stability -eq 'PENDING') { $report.checks.read_stability = if ($report.validation_layers.L4_readback -eq 'PASS') { 'PASS' } else { 'FAIL' } }
    if ($report.checks.recovery -eq 'PENDING') { $report.checks.recovery = 'NOT_REQUIRED' }
} catch {
    if ($stage -eq 'configuration') {
        $report.result = 'FAIL_CONFIG'
        $report.validation_layers.L1_configuration = 'FAIL'
    } else {
        $report.result = 'FAIL_API'
        if ($report.validation_layers.L2_capability -eq 'PENDING') { $report.validation_layers.L2_capability = 'FAIL' }
        if ($report.validation_layers.L3_api -eq 'PENDING') { $report.validation_layers.L3_api = 'FAIL' }
        if ($report.validation_layers.L4_readback -eq 'PENDING') { $report.validation_layers.L4_readback = 'FAIL' }
    }
    $report.reason = $_.Exception.Message
    if ($report.validation_layers.L5_functional -eq 'PENDING') { $report.validation_layers.L5_functional = 'PENDING_FIXTURE' }
    if ($report.validation_layers.L6_recovery -eq 'PENDING') { $report.validation_layers.L6_recovery = 'N_A' }
    $report.checks.read_stability = 'FAIL'
    $report.checks.control_effect = 'NOT_APPLICABLE'
    $report.checks.recovery = 'NOT_REQUIRED'
} finally {
    if ($initialized) {
        $uninit = Uninitialize-Susi
        $report.uninit_status = Get-StatusName $uninit
    }
}

$pass = @(); $pending = @(); $fail = @(); $na = @()
foreach ($name in @($report.validation_layers.Keys)) {
    $state = [string]$report.validation_layers[$name]
    if ($state -eq 'PASS') { $pass += "layer:$name" }
    elseif ($state -like 'FAIL*') { $fail += "layer:$name" }
    elseif ($state -eq 'N_A' -or $state -eq 'NOT_REQUIRED') { $na += "layer:$name" }
    else { $pending += "layer:$name" }
}
$report.result_breakdown = [ordered]@{ pass = $pass; pending = $pending; fail = $fail; na = $na }
$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'i2c'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"
exit ([int]$verdict.exit_code)
