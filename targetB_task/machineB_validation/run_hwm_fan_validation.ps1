param(
    [string[]]$DllDirs = @(),
    [string]$ConfigPath = "$PSScriptRoot\AIMB-289_fan.json",
    [string]$IniPath = "",
    [string]$IniDir = "$env:WINDIR\SUSI",
    [string]$OutDir = ".\out",
    [switch]$EnableStimulus,
    [string]$StimulusStartCmd = "",
    [string]$StimulusStopCmd = "",
    [string]$Profile = ""
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\common_susi.ps1"

$report = New-ValidationReport -category "HWM.Fan" -configPath $ConfigPath
$initialized = $false
$config = $null

try {
    $config = Read-JsonFileAsHashtable -path $ConfigPath
    $iniResolved = Resolve-SectionIniPath -Config $config -IniPath $IniPath -IniDir $IniDir
    $source = Get-ConfigValue -Config $config -Name 'source_ini'
    $sourceSection = [string](Get-ConfigValue -Config $source -Name 'section' '')
    if ($sourceSection -ne 'HWM.Fan') {
        throw "source_ini.section must be HWM.Fan, got '$sourceSection'."
    }

    $expectedHash = [string](Get-ConfigValue -Config $source -Name 'sha256' '')
    $actualHash = (Get-FileHash -LiteralPath $iniResolved -Algorithm SHA256).Hash.ToLowerInvariant()
    # Hash mismatch no longer blocks validation. We always validate by section content/runtime behavior.
    if (-not [string]::IsNullOrWhiteSpace($expectedHash) -and $expectedHash.ToLowerInvariant() -ne $actualHash) {
        Write-Warning "Section INI SHA-256 differs from config (expected=$expectedHash, actual=$actualHash). Continue validation."
    }

    $sections = Read-IniFile -Path $iniResolved
    $fanSection = Get-IniSection -Sections $sections -Name 'HWM.Fan'
    if ($null -eq $fanSection) {
        throw "INI does not contain [HWM.Fan]."
    }

    $required = @()
    if ($config.Contains('required_channels')) {
        $required = @($config.required_channels)
    }
    if ($required.Count -eq 0) {
        throw 'required_channels is empty.'
    }
    foreach ($ch in $required) {
        $iniKey = @($fanSection.Keys | Where-Object { $_ -ieq [string]$ch })
        if ($iniKey.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$fanSection[$iniKey[0]])) {
            throw "Required channel $ch is missing from [HWM.Fan]."
        }
    }

    $profiles = Get-ConfigValue -Config $config -Name 'profiles' -Default ([ordered]@{})
    $defaultProfile = [string](Get-ConfigValue -Config $config -Name 'default_profile' 'bringup')
    $effectiveProfile = if ([string]::IsNullOrWhiteSpace($Profile)) { $defaultProfile } else { $Profile }
    $profileConfig = Get-ConfigValue -Config $profiles -Name $effectiveProfile -Default $null
    if ($null -eq $profileConfig) {
        throw "Profile '$effectiveProfile' not found in config.profiles."
    }

    $fixtureRequiredChannels = @()
    if ($profileConfig.Contains('fixture_required_channels')) {
        $fixtureRequiredChannels = @($profileConfig.fixture_required_channels | ForEach-Object { [string]$_ })
    }
    $optionalChannels = @()
    if ($profileConfig.Contains('optional_channels')) {
        $optionalChannels = @($profileConfig.optional_channels | ForEach-Object { [string]$_ })
    }

    $missingFixtureDefaultResult = [string](Get-ConfigValue -Config $profileConfig -Name 'missing_fixture_result' 'CONDITIONAL_NO_FIXTURE')
    $channelPolicy = Get-ConfigValue -Config $config -Name 'channel_policy' -Default ([ordered]@{})

    $report.config_path = (Resolve-Path $ConfigPath).Path
    $report.source_ini = [ordered]@{
        configured_path = $IniPath
        resolved_path = $iniResolved
        section = $sourceSection
        sha256 = $actualHash
    }
    $report.profile = [ordered]@{
        requested = if ([string]::IsNullOrWhiteSpace($Profile)) { '' } else { $Profile }
        effective = $effectiveProfile
        default_from_config = $defaultProfile
    }
    $report.metrics.mapping = [ordered]@{}
    foreach ($ch in $required) {
        $runtime = Get-CanonicalFanApi -Key ([string]$ch)
        if ($null -eq $runtime.api_id_value) {
            throw "No canonical SUSI HWM.Fan ID for channel $ch."
        }
        $report.metrics.mapping[$ch] = $runtime
    }

    $report.validation_layers.L1_configuration = 'PASS'
    $report.checks.control_effect = 'NOT_RUN'
    $report.checks.recovery = 'NOT_REQUIRED'
    $init = Initialize-Susi -DllDirs $DllDirs
    $initialized = $true
    $report.init_status = Get-StatusName $init

    foreach ($ch in $required) {
        $id = [UInt32]$report.metrics.mapping[$ch].api_id_value
        $probe = Read-BoardValue -id $id
        Add-ApiCall -report $report -name ("Probe:{0}" -f $ch) -status $probe.status -value $probe.value
        if (-not (Is-Success $probe.status)) {
            $report.result = 'FAIL_CAPABILITY'
            $report.reason = "Unsupported required channel $ch"
            $report.validation_layers.L2_capability = 'FAIL'
            $report.checks.read_stability = 'FAIL'
            break
        }
    }

    if ($report.result -eq 'PENDING') {
        $report.validation_layers.L2_capability = 'PASS'
        $report.validation_layers.L3_api = 'PASS'
        $baseline = [ordered]@{}
        $fixture_analysis = [ordered]@{}
        $channel_outcome = [ordered]@{}
        $hasConditionalNoFixture = $false

        $readCheck = Get-ConfigValue -Config $config -Name 'read_check' -Default ([ordered]@{})
        $stimulusCheck = Get-ConfigValue -Config $config -Name 'stimulus_check' -Default ([ordered]@{})
        $sampleCount = [int](Get-ConfigValue -Config $readCheck -Name 'sample_count' (Get-ConfigValue -Config $config -Name 'sample_count' 30))
        $sampleInterval = [int](Get-ConfigValue -Config $readCheck -Name 'sample_interval_ms' (Get-ConfigValue -Config $config -Name 'sample_interval_ms' 1000))
        $minimumSuccessRate = [double](Get-ConfigValue -Config $readCheck -Name 'min_success_rate' (Get-ConfigValue -Config $config -Name 'minimum_success_rate' 1.0))
        $maximumRpm = [double](Get-ConfigValue -Config $readCheck -Name 'max_rpm' (Get-ConfigValue -Config $config -Name 'maximum_plausible_rpm' 10000))
        $minimumRpm = [double](Get-ConfigValue -Config $readCheck -Name 'min_rpm' 1)

        foreach ($ch in $required) {
            $channel_outcome[$ch] = [ordered]@{
                status = 'PENDING'
                reason = 'Channel not evaluated yet'
            }

            $id = [UInt32]$report.metrics.mapping[$ch].api_id_value
            $series = Sample-Channel -id $id -count $sampleCount -intervalMs $sampleInterval
            $stats = Get-SeriesStats -series $series
            $baseline[$ch] = [ordered]@{ stats = $stats; series = $series }
            $successRate = if ($sampleCount -gt 0) { $stats.count / [double]$sampleCount } else { 0.0 }

            $validValues = @($series | Where-Object { $null -ne $_.value } | ForEach-Object { [double]$_.value })
            $zeroCount = @($validValues | Where-Object { $_ -eq 0 }).Count
            $aboveMax = @($validValues | Where-Object { $_ -gt $maximumRpm }).Count
            $belowMinNonZero = @($validValues | Where-Object { $_ -lt $minimumRpm -and $_ -ne 0 }).Count
            $allSuccessfulSamplesZero = ($stats.count -gt 0 -and $zeroCount -eq $stats.count)

            if ($stats.count -eq 0 -or $successRate -lt $minimumSuccessRate) {
                $channel_outcome[$ch] = [ordered]@{
                    status = 'FAIL'
                    reason = "Insufficient successful samples (success_rate=$successRate)"
                }
                $report.result = 'FAIL_API'
                $report.reason = "Insufficient successful samples on channel $ch"
                $report.validation_layers.L3_api = 'FAIL'
                $report.checks.read_stability = 'FAIL'
                break
            }

            if ($aboveMax -gt 0 -or $belowMinNonZero -gt 0) {
                $channel_outcome[$ch] = [ordered]@{
                    status = 'FAIL'
                    reason = "RPM out of configured range (min=$minimumRpm,max=$maximumRpm)"
                }
                $report.result = 'FAIL_FUNCTIONAL'
                $report.reason = "RPM value outside configured min/max range on channel $ch"
                $report.validation_layers.L5_functional = 'FAIL'
                $report.checks.read_stability = 'FAIL'
                break
            }

            $channelPolicyCfg = Get-ConfigValue -Config $channelPolicy -Name ([string]$ch) -Default $null
            $rpmZeroPolicyMap = if ($null -eq $channelPolicyCfg) { $null } else { Get-ConfigValue -Config $channelPolicyCfg -Name 'rpm_zero_policy' -Default $null }
            $policyResult = if ($null -eq $rpmZeroPolicyMap) { $missingFixtureDefaultResult } else { [string](Get-ConfigValue -Config $rpmZeroPolicyMap -Name $effectiveProfile -Default $missingFixtureDefaultResult) }

            $isFixtureRequired = @($fixtureRequiredChannels | Where-Object { $_ -ieq [string]$ch }).Count -gt 0
            $isOptionalChannel = @($optionalChannels | Where-Object { $_ -ieq [string]$ch }).Count -gt 0

            $fixture_analysis[$ch] = [ordered]@{
                fixture_required = $isFixtureRequired
                optional_channel = $isOptionalChannel
                zero_count = $zeroCount
                successful_sample_count = [int]$stats.count
                all_successful_samples_zero = $allSuccessfulSamplesZero
                zero_rpm_policy_result = $policyResult
            }

            if ($allSuccessfulSamplesZero) {
                if ($policyResult -like 'FAIL*') {
                    $channel_outcome[$ch] = [ordered]@{
                        status = 'FAIL'
                        reason = "All successful samples are 0 RPM under profile '$effectiveProfile'"
                    }
                    $report.result = 'FAIL_FUNCTIONAL'
                    $report.reason = "Tach mapping/API is readable but all samples are 0 RPM on channel $ch under profile '$effectiveProfile'"
                    $report.validation_layers.L5_functional = 'FAIL'
                    $report.checks.read_stability = 'FAIL'
                    break
                }

                if ($policyResult -like 'CONDITIONAL*') {
                    $channel_outcome[$ch] = [ordered]@{
                        status = 'PENDING'
                        reason = "Tach/API PASS, waiting fan fixture (policy=$policyResult, profile=$effectiveProfile)"
                    }
                    $hasConditionalNoFixture = $true
                }
            } else {
                $channel_outcome[$ch] = [ordered]@{
                    status = 'PASS'
                    reason = 'RPM samples are within range and non-zero behavior is acceptable'
                }
            }
        }
        $report.metrics.baseline = $baseline
        $report.metrics.fixture_analysis = $fixture_analysis
        $report.metrics.channel_outcome = $channel_outcome

        if ($report.result -eq 'PENDING') {
            $report.validation_layers.L4_readback = 'PASS'
            $report.checks.read_stability = 'PASS'

            if ($hasConditionalNoFixture) {
                $report.validation_layers.L5_functional = 'CONDITIONAL'
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.control_effect = 'NOT_RUN'
                $report.checks.recovery = 'NOT_REQUIRED'
                $report.reason = "Tach mapping/API is valid; missing fan fixture semantics applied under profile '$effectiveProfile'."
                $report.result = 'CONDITIONAL_NO_FIXTURE'
            } elseif ($EnableStimulus -and -not [string]::IsNullOrWhiteSpace($StimulusStartCmd)) {
                try {
                    Invoke-Expression $StimulusStartCmd
                    Start-Sleep -Seconds ([int](Get-ConfigValue -Config $stimulusCheck -Name 'settle_time_sec' (Get-ConfigValue -Config $config -Name 'settle_time_sec' 10)))

                    $response = [ordered]@{}
                    foreach ($ch in $required) {
                        $id = [UInt32]$report.metrics.mapping[$ch].api_id_value
                        $series = Sample-Channel -id $id -count $sampleCount -intervalMs $sampleInterval
                        $stats = Get-SeriesStats -series $series
                        $response[$ch] = [ordered]@{ stats = $stats; series = $series }
                        $delta = [double]$stats.avg - [double]$baseline[$ch].stats.avg
                        $minimumDelta = [double](Get-ConfigValue -Config $stimulusCheck -Name 'expected_delta_rpm_min' (Get-ConfigValue -Config $config -Name 'expected_delta_rpm_min' 200))
                        if ([Math]::Abs($delta) -lt $minimumDelta) {
                            $report.result = 'FAIL_FUNCTIONAL'
                            $report.reason = "RPM delta too small on $ch ($delta)"
                            $report.validation_layers.L5_functional = 'FAIL'
                            $report.checks.control_effect = 'FAIL'
                            break
                        }
                    }
                    $report.metrics.response = $response
                    if ($report.result -eq 'PENDING') {
                        $report.validation_layers.L5_functional = 'PASS'
                        $report.checks.control_effect = 'PASS'
                    }
                }
                finally {
                    if (-not [string]::IsNullOrWhiteSpace($StimulusStopCmd)) {
                        try { Invoke-Expression $StimulusStopCmd } catch {}
                    }
                }
            } else {
                $report.validation_layers.L5_functional = 'CONDITIONAL'
                $report.reason = 'No stimulus provided; API/readback only'
                $report.checks.control_effect = 'NOT_RUN'
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.recovery = 'NOT_REQUIRED'
                $report.result = 'CONDITIONAL'
            }

            if ($report.result -eq 'PENDING') {
                $report.validation_layers.L6_recovery = 'N_A'
                $report.checks.recovery = 'NOT_REQUIRED'
                $report.result = 'PASS'
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

if ($report.metrics.Contains('channel_outcome')) {
    foreach ($ch in @($report.metrics.channel_outcome.Keys)) {
        $st = [string]$report.metrics.channel_outcome[$ch].status
        if ($st -eq 'PASS') { $passItems += "channel:$ch"; continue }
        if ($st -eq 'FAIL') { $failItems += "channel:$ch"; continue }
        if ($st -eq 'N_A' -or $st -eq 'NOT_REQUIRED') { $naItems += "channel:$ch"; continue }
        $pendingItems += "channel:$ch"
    }
}

$report.result_breakdown = [ordered]@{
    pass = $passItems
    pending = $pendingItems
    fail = $failItems
    na = $naItems
}

$path = Save-ValidationReport -report $report -outDir $OutDir -prefix 'hwm_fan'
Write-Output "DONE. Report: $path"

switch ($report.result) {
    'PASS' { exit 0 }
    'CONDITIONAL' { exit 2 }
    'CONDITIONAL_NO_FIXTURE' { exit 2 }
    default { exit 1 }
}
