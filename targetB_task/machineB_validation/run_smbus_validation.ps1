param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_smbus.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableFixtureTest
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

function New-SmbusFixtureStep {
    param(
        [string]$Name,
        [UInt32]$Status,
        [object]$Expected = $null,
        [object]$Actual = $null,
        [bool]$Compare = $true,
        [bool]$AllowUnsupported = $false
    )

    $statusAccepted = (Is-Success $Status) -or ($AllowUnsupported -and $Status -eq [UInt32]0xFFFFFCFF)
    $passed = $statusAccepted -and $Compare
    return [ordered]@{
        name = $Name
        status = Get-StatusName $Status
        status_code = ('0x{0:X8}' -f $Status)
        expected = $Expected
        actual = $Actual
        result = if ($passed) { 'PASS' } else { 'FAIL' }
    }
}

function Invoke-LegacySmbusFixtureTest {
    param(
        [UInt32]$BusId,
        [byte]$Address,
        [bool]$BasicOnly,
        [hashtable]$Report
    )

    $steps = [System.Collections.ArrayList]::new()
    $cmd = [byte]0x01

    $status = [NativeSusi]::SusiSMBWriteByte($BusId, $Address, $cmd, [byte]0x11)
    Add-ApiCall -report $Report -name 'SusiSMBWriteByte:fixture' -status $status
    [void]$steps.Add((New-SmbusFixtureStep -Name 'WriteByte(Cmd=0x01,Data=0x11)' -Status $status))
    Start-Sleep -Milliseconds 100

    [byte]$readByte = 0
    $status = [NativeSusi]::SusiSMBReadByte($BusId, $Address, $cmd, [ref]$readByte)
    Add-ApiCall -report $Report -name 'SusiSMBReadByte:fixture' -status $status -value $readByte
    $expectedByte = if ($BasicOnly) { [byte]0x01 } else { [byte]0x11 }
    [void]$steps.Add((New-SmbusFixtureStep -Name 'ReadByte/Compare' -Status $status -Expected ('0x{0:X2}' -f $expectedByte) -Actual ('0x{0:X2}' -f $readByte) -Compare ($readByte -eq $expectedByte)))

    if ($BasicOnly) {
        Start-Sleep -Milliseconds 100
        $status = [NativeSusi]::SusiSMBSendByte($BusId, $Address, [byte]0x05)
        Add-ApiCall -report $Report -name 'SusiSMBSendByte:fixture' -status $status
        [void]$steps.Add((New-SmbusFixtureStep -Name 'SendByte(Data=0x05)' -Status $status))

        Start-Sleep -Milliseconds 100
        [byte]$received = 0
        $status = [NativeSusi]::SusiSMBReceiveByte($BusId, $Address, [ref]$received)
        Add-ApiCall -report $Report -name 'SusiSMBReceiveByte:fixture' -status $status -value $received
        [void]$steps.Add((New-SmbusFixtureStep -Name 'ReceiveByte/Compare' -Status $status -Expected '0x05' -Actual ('0x{0:X2}' -f $received) -Compare ($received -eq 0x05)))
    } else {
        Start-Sleep -Milliseconds 100
        $status = [NativeSusi]::SusiSMBWriteWord($BusId, $Address, $cmd, [UInt16]0x1234)
        Add-ApiCall -report $Report -name 'SusiSMBWriteWord:fixture' -status $status
        [void]$steps.Add((New-SmbusFixtureStep -Name 'WriteWord(Data=0x1234)' -Status $status))

        Start-Sleep -Milliseconds 100
        [UInt16]$readWord = 0
        $status = [NativeSusi]::SusiSMBReadWord($BusId, $Address, $cmd, [ref]$readWord)
        Add-ApiCall -report $Report -name 'SusiSMBReadWord:fixture' -status $status -value $readWord
        [void]$steps.Add((New-SmbusFixtureStep -Name 'ReadWord/Compare' -Status $status -Expected '0x1234' -Actual ('0x{0:X4}' -f $readWord) -Compare ($readWord -eq 0x1234)))

        Start-Sleep -Milliseconds 100
        $status = [NativeSusi]::SusiSMBSendByte($BusId, $Address, [byte]0x01)
        Add-ApiCall -report $Report -name 'SusiSMBSendByte:fixture' -status $status
        [void]$steps.Add((New-SmbusFixtureStep -Name 'SendByte(Data=0x01)' -Status $status))

        foreach ($expectedReceive in @([byte]0x34, [byte]0x12)) {
            Start-Sleep -Milliseconds 100
            [byte]$received = 0
            $status = [NativeSusi]::SusiSMBReceiveByte($BusId, $Address, [ref]$received)
            Add-ApiCall -report $Report -name 'SusiSMBReceiveByte:fixture' -status $status -value $received
            [void]$steps.Add((New-SmbusFixtureStep -Name 'ReceiveByte/Compare' -Status $status -Expected ('0x{0:X2}' -f $expectedReceive) -Actual ('0x{0:X2}' -f $received) -Compare ($received -eq $expectedReceive)))
        }

        [byte[]]$writeBlock = 0..9
        $status = [NativeSusi]::SusiSMBWriteBlock($BusId, $Address, $cmd, $writeBlock, [UInt32]$writeBlock.Length)
        Add-ApiCall -report $Report -name 'SusiSMBWriteBlock:fixture' -status $status
        [void]$steps.Add((New-SmbusFixtureStep -Name 'WriteBlock(00..09)' -Status $status))

        Start-Sleep -Milliseconds 100
        [byte[]]$readBlock = New-Object byte[] 32
        [UInt32]$readLength = 10
        $status = [NativeSusi]::SusiSMBReadBlock($BusId, $Address, $cmd, $readBlock, [ref]$readLength)
        Add-ApiCall -report $Report -name 'SusiSMBReadBlock:fixture' -status $status -value $readLength
        $blockMatch = ($readLength -ge 10)
        for ($i = 0; $i -lt 10 -and $blockMatch; $i++) { if ($readBlock[$i] -ne $writeBlock[$i]) { $blockMatch = $false } }
        [void]$steps.Add((New-SmbusFixtureStep -Name 'ReadBlock/Compare' -Status $status -Expected '00..09' -Actual (($readBlock[0..9] | ForEach-Object { '{0:X2}' -f $_ }) -join ' ') -Compare $blockMatch))

        $status = [NativeSusi]::SusiSMBI2CWriteBlock($BusId, $Address, $cmd, $writeBlock, [UInt32]$writeBlock.Length)
        Add-ApiCall -report $Report -name 'SusiSMBI2CWriteBlock:fixture' -status $status
        $i2cUnsupported = ($status -eq [UInt32]0xFFFFFCFF)
        [void]$steps.Add((New-SmbusFixtureStep -Name 'I2CWriteBlock(00..09)' -Status $status -Expected 'SUCCESS or UNSUPPORTED' -Actual (Get-StatusName $status) -Compare $true -AllowUnsupported $true))

        if (-not $i2cUnsupported) {
            Start-Sleep -Milliseconds 100
            [byte[]]$i2cRead = New-Object byte[] 10
            $status = [NativeSusi]::SusiSMBI2CReadBlock($BusId, $Address, $cmd, $i2cRead, [UInt32]10)
            Add-ApiCall -report $Report -name 'SusiSMBI2CReadBlock:fixture' -status $status
            $legacyAlt = [byte[]](10,0,1,2,3,4,5,6,7,8)
            $normalMatch = $true
            $altMatch = $true
            for ($i = 0; $i -lt 10; $i++) {
                if ($i2cRead[$i] -ne $writeBlock[$i]) { $normalMatch = $false }
                if ($i2cRead[$i] -ne $legacyAlt[$i]) { $altMatch = $false }
            }
            [void]$steps.Add((New-SmbusFixtureStep -Name 'I2CReadBlock/Compare' -Status $status -Expected '00..09 or legacy 0A,00..08' -Actual (($i2cRead | ForEach-Object { '{0:X2}' -f $_ }) -join ' ') -Compare ($normalMatch -or $altMatch)))
        }
    }

    $failed = @($steps | Where-Object { $_.result -eq 'FAIL' })
    return [ordered]@{
        bus_id = $BusId
        encoded_address = ('0x{0:X2}' -f $Address)
        seven_bit_address = ('0x{0:X2}' -f ($Address -shr 1))
        profile = if ($BasicOnly) { 'LEGACY_0x4A_BASIC' } else { 'LEGACY_0xAC_0xAE_FULL' }
        result = if ($failed.Count -eq 0) { 'PASS' } else { 'FAIL' }
        steps = @($steps)
    }
}

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
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.recovery = 'NOT_REQUIRED'

                if ($EnableFixtureTest) {
                    [Console]::WriteLine('')
                    [Console]::WriteLine('========== SMBus FIXTURE VALIDATION ==========')
                    [Console]::WriteLine('WARNING: This test follows legacy QA smb.c and writes data to the fixture.')
                    [Console]::WriteLine('Fixture addresses: 0xAC (7-bit 0x56), 0xAE (7-bit 0x57), 0x4A (7-bit 0x25).')
                    [Console]::WriteLine('FAIL may mean the fixture is not connected, address mismatch, wiring fault, or transaction/API failure.')

                    $fixtureTests = [System.Collections.ArrayList]::new()
                    for ($busId = 0; $busId -le 4; $busId++) {
                        if ((([UInt32]$first) -band ([UInt32](1 -shl $busId))) -eq 0) { continue }

                        $addresses = @([byte]0xAC, [byte]0xAE)
                        if ($busId -eq 0) { $addresses += [byte]0x4A }

                        foreach ($address in $addresses) {
                            $quickStatus = [NativeSusi]::SusiSMBWriteQuick([UInt32]$busId, $address)
                            Add-ApiCall -report $report -name 'SusiSMBWriteQuick:fixture_probe' -status $quickStatus -value ('Bus={0},Addr=0x{1:X2}' -f $busId, $address)

                            if (-not (Is-Success $quickStatus)) {
                                $fixtureResult = [ordered]@{
                                    bus_id = $busId
                                    encoded_address = ('0x{0:X2}' -f $address)
                                    seven_bit_address = ('0x{0:X2}' -f ($address -shr 1))
                                    profile = if ($address -eq 0x4A) { 'LEGACY_0x4A_BASIC' } else { 'LEGACY_0xAC_0xAE_FULL' }
                                    probe_status = Get-StatusName $quickStatus
                                    probe_status_code = ('0x{0:X8}' -f $quickStatus)
                                    result = 'FAIL'
                                    reason = 'FIXTURE_NOT_DETECTED_OR_PROBE_FAILED'
                                    steps = @()
                                }
                            } else {
                                $fixtureResult = Invoke-LegacySmbusFixtureTest -BusId ([UInt32]$busId) -Address $address -BasicOnly ($address -eq 0x4A) -Report $report
                                $fixtureResult.probe_status = Get-StatusName $quickStatus
                                $fixtureResult.probe_status_code = ('0x{0:X8}' -f $quickStatus)
                            }

                            [void]$fixtureTests.Add($fixtureResult)
                            [Console]::WriteLine(('[FIXTURE] Bus ID {0}, Addr {1} (7-bit {2}) => {3}' -f $fixtureResult.bus_id, $fixtureResult.encoded_address, $fixtureResult.seven_bit_address, $fixtureResult.result))
                        }
                    }

                    $fixturePassed = @($fixtureTests | Where-Object { $_.result -eq 'PASS' }).Count -gt 0
                    $report.fixture_validation = [ordered]@{
                        enabled = $true
                        source = 'legacy QA smb.c'
                        addresses = @(
                            [ordered]@{ encoded = '0xAC'; seven_bit = '0x56'; profile = 'FULL' },
                            [ordered]@{ encoded = '0xAE'; seven_bit = '0x57'; profile = 'FULL' },
                            [ordered]@{ encoded = '0x4A'; seven_bit = '0x25'; profile = 'BASIC_EXTERNAL_ONLY' }
                        )
                        result = if ($fixturePassed) { 'PASS_FIXTURE' } else { 'FAIL_FIXTURE' }
                        failure_meaning = 'fixture is not connected, address mismatch, wiring fault, or transaction/API failure'
                        tests = @($fixtureTests)
                    }

                    if ($fixturePassed) {
                        [Console]::WriteLine('FIXTURE PASS: At least one supported SMBus host completed the legacy QA transaction/readback sequence.')
                        $report.result = 'PASS_FIXTURE'
                        $report.reason = 'Legacy QA SMBus fixture transaction/readback passed'
                        $report.validation_layers.L5_functional = 'PASS'
                        $report.checks.control_effect = 'PASS'
                    } else {
                        [Console]::WriteLine('FIXTURE FAIL: No fixture completed the sequence. Check connection, address, wiring, and transaction/API status.')
                        $report.result = 'FAIL_FIXTURE'
                        $report.reason = 'Fixture validation failed; this does not change the independently calculated SW verdict'
                        $report.validation_layers.L5_functional = 'FAIL_FIXTURE'
                        $report.checks.control_effect = 'FAIL_FIXTURE'
                    }
                    [Console]::WriteLine('=============================================')
                } else {
                    $report.fixture_validation = [ordered]@{
                        enabled = $false
                        result = 'NOT_RUN'
                        reason = 'Use -EnableFixtureTest only after connecting the approved legacy QA fixture'
                        addresses = @('0xAC (7-bit 0x56)', '0xAE (7-bit 0x57)', '0x4A (7-bit 0x25)')
                    }
                    $report.validation_layers.L5_functional = 'CONDITIONAL'
                    $report.result = 'CONDITIONAL'
                    if ($unsupported.Count -gt 0) {
                        $report.reason = ('BLOCKED_REFERENCE: capability mask does not expose channel(s): ' + ($unsupported -join ', '))
                    } else {
                        $report.reason = 'BLOCKED_FIXTURE: rerun with -EnableFixtureTest after connecting the approved legacy QA fixture'
                    }
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

$verdict = Apply-VerdictPolicy -Report $report
$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'smbus'
Write-Output "DONE. Result=$($report.result); sw_verdict=$($verdict.sw_verdict); dqa_verdict=$($verdict.dqa_verdict); Report=$path"

exit ([int]$verdict.exit_code)
