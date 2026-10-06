$ErrorActionPreference = 'Stop'

function Get-StatusName([UInt32]$st) {
    switch ($st) {
        0 { 'SUSI_STATUS_SUCCESS' }
        0xFFFFFFFF { 'SUSI_STATUS_NOT_INITIALIZED' }
        0xFFFFFFFE { 'SUSI_STATUS_INITIALIZED' }
        0xFFFFFFFD { 'SUSI_STATUS_ALLOC_ERROR' }
        0xFFFFFFFC { 'SUSI_STATUS_DRIVER_TIMEOUT' }
        0xFFFFFEFF { 'SUSI_STATUS_INVALID_PARAMETER' }
        0xFFFFFCFF { 'SUSI_STATUS_UNSUPPORTED' }
        0xFFFFFBFF { 'SUSI_STATUS_NOT_FOUND' }
        0xFFFFFBFE { 'SUSI_STATUS_TIMEOUT' }
        0xFFFFF9FF { 'SUSI_STATUS_MORE_DATA' }
        default { ('0x{0:X8}' -f $st) }
    }
}

function Is-Success([UInt32]$st) {
    return ($st -eq 0 -or $st -eq 0xFFFFFFFE)
}

function Initialize-Susi([string[]]$DllDirs) {
    foreach ($d in $DllDirs) {
        if ([string]::IsNullOrWhiteSpace($d)) { continue }
        if (Test-Path $d) { $env:PATH = "$d;$env:PATH" }
    }

    $cs = @"
using System;
using System.Runtime.InteropServices;

[StructLayout(LayoutKind.Sequential)]
public struct SusiAutoFan
{
    public UInt32 TmlSource;
    public UInt32 OpMode;
    public UInt32 LowStopLimit;
    public UInt32 LowLimit;
    public UInt32 HighLimit;
    public UInt32 MinPWM;
    public UInt32 MaxPWM;
    public UInt32 MinRPM;
    public UInt32 MaxRPM;
}

[StructLayout(LayoutKind.Sequential)]
public struct SusiFanControl
{
    public UInt32 Mode;
    public UInt32 PWM;
    public SusiAutoFan AutoControl;
}

[StructLayout(LayoutKind.Sequential)]
public struct SusiThermalProtect
{
    public UInt32 SourceId;
    public UInt32 EventType;
    public UInt32 SendEventTemperature;
    public UInt32 ClearEventTemperature;
}

public static class NativeSusi
{
    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibInitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibUninitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiBoardGetValue(UInt32 Id, ref UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiBoardSetValue(UInt32 Id, ref UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBWriteQuick(UInt32 Id, byte Addr);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBWriteByte(UInt32 Id, byte Addr, byte Cmd, byte Data);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReadByte(UInt32 Id, byte Addr, byte Cmd, out byte pBuffer);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBWriteWord(UInt32 Id, byte Addr, byte Cmd, UInt16 Data);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReadWord(UInt32 Id, byte Addr, byte Cmd, out UInt16 pBuffer);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBSendByte(UInt32 Id, byte Addr, byte Data);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReceiveByte(UInt32 Id, byte Addr, out byte pData);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBWriteBlock(UInt32 Id, byte Addr, byte Cmd, byte[] pBuffer, UInt32 Length);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReadBlock(UInt32 Id, byte Addr, byte Cmd, [In, Out] byte[] pBuffer, ref UInt32 pLength);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBI2CWriteBlock(UInt32 Id, byte Addr, byte Cmd, byte[] pBuffer, UInt32 Length);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBI2CReadBlock(UInt32 Id, byte Addr, byte Cmd, [In, Out] byte[] pBuffer, UInt32 Length);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CGetFrequency(UInt32 Id, out UInt32 pFreq);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CSetFrequency(UInt32 Id, UInt32 Freq);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CProbeDevice(UInt32 Id, UInt32 Addr);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiWDogGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiWDogStart(UInt32 Id, UInt32 DelayTime, UInt32 EventTime, UInt32 ResetTime, UInt32 EventType);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiWDogStop(UInt32 Id);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiWDogTrigger(UInt32 Id);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiFanControlGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiFanControlGetConfig(UInt32 Id, out SusiFanControl pConfig);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiFanControlSetConfig(UInt32 Id, ref SusiFanControl pConfig);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiThermalProtectionGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiThermalProtectionGetConfig(UInt32 Id, out SusiThermalProtect pConfig);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiThermalProtectionSetConfig(UInt32 Id, ref SusiThermalProtect pConfig);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiStorageGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiStorageAreaRead(UInt32 Id, UInt32 Offset, [In, Out] byte[] pBuffer, UInt32 BufLen);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiStorageAreaWrite(UInt32 Id, UInt32 Offset, byte[] pBuffer, UInt32 BufLen);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetDirection(UInt32 Id, UInt32 Bitmask, out UInt32 pDirection);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOSetDirection(UInt32 Id, UInt32 Bitmask, UInt32 Direction);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetLevel(UInt32 Id, UInt32 Bitmask, out UInt32 pLevel);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOSetLevel(UInt32 Id, UInt32 Bitmask, UInt32 Level);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetBacklightEnable(UInt32 Id, out UInt32 pEnable);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaSetBacklightEnable(UInt32 Id, UInt32 Enable);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetBacklightBrightness(UInt32 Id, out UInt32 pBright);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaSetBacklightBrightness(UInt32 Id, UInt32 Bright);

    // Storage lock/unlock intentionally disabled for initial validation.
    // SusiStorageAreaSetUnlock / SusiStorageAreaSetLock are not declared or called.
}
"@
    if (-not ('NativeSusi' -as [type])) {
        Add-Type -TypeDefinition $cs -Language CSharp
    }

    $st = [NativeSusi]::SusiLibInitialize()
    if (-not (Is-Success $st)) {
        throw "SusiLibInitialize failed: $(Get-StatusName $st)"
    }
    return $st
}

function Uninitialize-Susi() {
    $st = [NativeSusi]::SusiLibUninitialize()
    return $st
}

function Read-BoardValue([UInt32]$id) {
    $v = [UInt32]0
    $st = [NativeSusi]::SusiBoardGetValue($id, [ref]$v)
    return [ordered]@{ status = $st; value = $v }
}

function Decode-TempC([UInt32]$raw) {
    return [Math]::Round((($raw - 2731) / 10.0), 2)
}

function Sample-Channel([UInt32]$id, [int]$count, [int]$intervalMs, [scriptblock]$decode = $null) {
    $series = @()
    for ($i = 0; $i -lt $count; $i++) {
        $r = Read-BoardValue -id $id
        if ($r.status -eq 0) {
            $decoded = if ($decode) { & $decode $r.value } else { [double]$r.value }
            $series += [ordered]@{ ts = (Get-Date).ToString('o'); raw = $r.value; value = $decoded }
        } else {
            $series += [ordered]@{
                ts = (Get-Date).ToString('o')
                raw = $null
                value = $null
                status = Get-StatusName $r.status
                status_code = ('0x{0:X8}' -f [UInt32]$r.status)
            }
        }
        if ($i -lt ($count - 1)) { Start-Sleep -Milliseconds $intervalMs }
    }
    return $series
}

function Get-SeriesStats([object[]]$series) {
    $vals = @($series | Where-Object { $_.value -ne $null } | ForEach-Object { [double]$_.value })
    if ($vals.Count -eq 0) {
        return [ordered]@{ count = 0; min = $null; max = $null; avg = $null; span = $null }
    }

    $min = ($vals | Measure-Object -Minimum).Minimum
    $max = ($vals | Measure-Object -Maximum).Maximum
    $avg = ($vals | Measure-Object -Average).Average
    return [ordered]@{
        count = $vals.Count
        min = [Math]::Round($min, 3)
        max = [Math]::Round($max, 3)
        avg = [Math]::Round($avg, 3)
        span = [Math]::Round(($max - $min), 3)
    }
}


function ConvertTo-Hashtable($obj) {
    if ($null -eq $obj) { return $null }

    if ($obj -is [System.Collections.IDictionary]) {
        $h = [ordered]@{}
        foreach ($k in $obj.Keys) {
            $h[$k] = ConvertTo-Hashtable $obj[$k]
        }
        return $h
    }

    if ($obj -is [System.Collections.IEnumerable] -and -not ($obj -is [string])) {
        $arr = @()
        foreach ($item in $obj) { $arr += ,(ConvertTo-Hashtable $item) }
        return $arr
    }

    if ($obj -is [pscustomobject]) {
        $h = [ordered]@{}
        foreach ($p in $obj.PSObject.Properties) {
            $h[$p.Name] = ConvertTo-Hashtable $p.Value
        }
        return $h
    }

    return $obj
}

function Read-JsonFileAsHashtable([string]$path) {
    $raw = Get-Content $path -Raw
    $obj = $raw | ConvertFrom-Json
    return ConvertTo-Hashtable $obj
}

function Get-ConfigValue {
    param(
        [object]$Config,
        [string]$Name,
        [object]$Default = $null
    )

    if ($null -eq $Config) { return $Default }
    if ($Config -is [System.Collections.IDictionary] -and $Config.Contains($Name)) {
        return $Config[$Name]
    }
    return $Default
}

function Get-IniSection {
    param(
        [System.Collections.IDictionary]$Sections,
        [string]$Name
    )

    foreach ($sectionName in $Sections.Keys) {
        if ([string]::Equals([string]$sectionName, $Name, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $Sections[$sectionName]
        }
    }
    return $null
}

function Read-IniFile {
    param([string]$Path)

    $sections = [ordered]@{}
    $currentSection = $null
    $lineNumber = 0

    foreach ($line in Get-Content -LiteralPath $Path) {
        $lineNumber++
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith(';') -or $trimmed.StartsWith('#')) {
            continue
        }

        if ($trimmed -match '^\[([^\]]+)\]$') {
            $sectionName = $matches[1].Trim()
            if (@($sections.Keys | Where-Object { $_ -ieq $sectionName }).Count -gt 0) {
                throw "Duplicate INI section [$sectionName] at line $($lineNumber)."
            }
            $sections[$sectionName] = [ordered]@{}
            $currentSection = $sectionName
            continue
        }

        if ($trimmed -match '^([^=]+)=(.*)$') {
            if ($null -eq $currentSection) {
                throw "INI key appears before a section at line $($lineNumber)."
            }

            $key = $matches[1].Trim()
            if ([string]::IsNullOrWhiteSpace($key)) {
                throw "INI key is empty at line $($lineNumber)."
            }
            if (@($sections[$currentSection].Keys | Where-Object { $_ -ieq $key }).Count -gt 0) {
                throw "Duplicate INI key [$currentSection]$key at line $($lineNumber)."
            }
            $sections[$currentSection][$key] = $matches[2].Trim()
            continue
        }

        throw "Unsupported INI syntax at line $($lineNumber): $trimmed"
    }

    return $sections
}

function Resolve-SectionIniPath {
    param(
        [System.Collections.IDictionary]$Config,
        [string]$IniPath = '',
        [string]$IniDir = "$env:WINDIR\SUSI"
    )

    $candidate = $IniPath
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $source = Get-ConfigValue -Config $Config -Name 'source_ini'
        $fileName = [string](Get-ConfigValue -Config $source -Name 'file' '')
        if ([string]::IsNullOrWhiteSpace($fileName)) {
            throw 'Config must contain source_ini.file or -IniPath must be supplied.'
        }
        $candidate = Join-Path $IniDir $fileName
    }

    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Missing section INI: $candidate"
    }
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Get-CanonicalFanApi {
    param([string]$Key)

    $offsets = @{
        FCPU = 0
        FSYS = 1
        FCPU2 = 2
        FOEM0 = 3
        FOEM1 = 4
        FOEM2 = 5
        FOEM3 = 6
        FOEM4 = 7
        FOEM5 = 8
        FOEM6 = 9
        FSYS2 = 10
        FSYS3 = 11
        FSYS4 = 12
    }

    if (-not $offsets.ContainsKey($Key)) {
        return [ordered]@{
            api_id = $null
            api_id_value = $null
            source = 'NOT_DEFINED_FOR_SUSI_HWM_FAN_NAMESPACE'
        }
    }

    $apiId = [UInt32](0x00022000 + [int]$offsets[$Key])
    return [ordered]@{
        api_id = ('0x{0:X8}' -f $apiId)
        api_id_value = $apiId
        source = 'SUSI_ID_HWM_FAN_CANONICAL_MAPPING'
    }
}

function Get-SusiFanControlCaps {
    param([UInt32]$FanId)

    $controlFlags = [UInt32]0
    $controlStatus = [NativeSusi]::SusiFanControlGetCaps(
        $FanId,
        [UInt32]0x00000000,
        [ref]$controlFlags
    )

    $autoFlags = [UInt32]0
    $autoStatus = [NativeSusi]::SusiFanControlGetCaps(
        $FanId,
        [UInt32]0x00000001,
        [ref]$autoFlags
    )

    return [ordered]@{
        control_status = $controlStatus
        control_support_flags = $controlFlags
        auto_status = $autoStatus
        auto_support_flags = $autoFlags
        manual_supported = (($controlStatus -eq 0) -and (($controlFlags -band 0x00000004) -ne 0))
    }
}

function Get-SusiFanControlConfig {
    param([UInt32]$FanId)

    $config = New-Object SusiFanControl
    $status = [NativeSusi]::SusiFanControlGetConfig($FanId, [ref]$config)
    return [ordered]@{
        status = $status
        config = $config
    }
}

function Get-SusiFanControlModeName {
    param([UInt32]$Mode)

    switch ($Mode) {
        0 { return 'OFF' }
        1 { return 'FULL' }
        2 { return 'MANUAL' }
        3 { return 'AUTO' }
        default { return ('UNKNOWN_{0}' -f $Mode) }
    }
}

function Convert-SusiFanControlConfigForReport {
    param([object]$Config)

    return [ordered]@{
        mode = [UInt32]$Config.Mode
        mode_name = Get-SusiFanControlModeName -Mode ([UInt32]$Config.Mode)
        pwm = [UInt32]$Config.PWM
        auto_control = [ordered]@{
            thermal_source = ('0x{0:X8}' -f [UInt32]$Config.AutoControl.TmlSource)
            operation_mode = [UInt32]$Config.AutoControl.OpMode
            low_stop_limit = [UInt32]$Config.AutoControl.LowStopLimit
            low_limit = [UInt32]$Config.AutoControl.LowLimit
            high_limit = [UInt32]$Config.AutoControl.HighLimit
            min_pwm = [UInt32]$Config.AutoControl.MinPWM
            max_pwm = [UInt32]$Config.AutoControl.MaxPWM
            min_rpm = [UInt32]$Config.AutoControl.MinRPM
            max_rpm = [UInt32]$Config.AutoControl.MaxRPM
        }
    }
}

function Test-SusiFanControlConfigEquivalent {
    param(
        [object]$Expected,
        [object]$Actual,
        [double]$PwmTolerance
    )

    if ($null -eq $Actual) { return $false }
    if ([UInt32]$Expected.Mode -ne [UInt32]$Actual.Mode) { return $false }
    if ([UInt32]$Expected.Mode -eq 2 -and [Math]::Abs([double]$Expected.PWM - [double]$Actual.PWM) -gt $PwmTolerance) {
        return $false
    }

    $expectedAuto = $Expected.AutoControl
    $actualAuto = $Actual.AutoControl
    foreach ($field in @('TmlSource', 'OpMode', 'LowStopLimit', 'LowLimit', 'HighLimit', 'MinPWM', 'MaxPWM', 'MinRPM', 'MaxRPM')) {
        if ([UInt32]$expectedAuto.$field -ne [UInt32]$actualAuto.$field) { return $false }
    }
    return $true
}

function Convert-JsonTextToHashtable([string]$text) {
    $obj = $text | ConvertFrom-Json
    return ConvertTo-Hashtable $obj
}

function New-ValidationReport([string]$category, [string]$configPath) {
    return [ordered]@{
        run_at = (Get-Date).ToString('o')
        host = $env:COMPUTERNAME
        category = $category
        config_path = $configPath
        result = 'PENDING'
        reason = ''
        validation_layers = [ordered]@{
            L1_configuration = 'PENDING'
            L2_capability = 'PENDING'
            L3_api = 'PENDING'
            L4_readback = 'PENDING'
            L5_functional = 'PENDING'
            L6_recovery = 'PENDING'
        }
        checks = [ordered]@{
            read_stability = 'PENDING'
            control_effect = 'PENDING'
            recovery = 'PENDING'
        }
        api_calls = @()
        metrics = [ordered]@{}
        evidence = @()
        init_status = ''
        uninit_status = ''
    }
}

function Add-ApiCall($report, [string]$name, [uint32]$status, [object]$value = $null) {
    if ($null -eq $report.api_calls) { $report.api_calls = @() }
    $entry = [ordered]@{
        name = $name
        status = Get-StatusName $status
        status_code = ('0x{0:X8}' -f $status)
        ts = (Get-Date).ToString('o')
    }
    if ($null -ne $value) { $entry.value = $value }
    $report.api_calls += $entry
}

function Save-ValidationReport([hashtable]$report, [string]$outDir, [string]$prefix) {
    if (-not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir | Out-Null
    }
    $path = Join-Path $outDir ("{0}_{1}.json" -f $prefix, (Get-Date -Format 'yyyyMMdd_HHmmss'))
    ($report | ConvertTo-Json -Depth 12) | Set-Content -Encoding UTF8 -Path $path
    return $path
}

function Resolve-DqaVerdictFromLayers {
    param(
        [System.Collections.IDictionary]$Layers
    )

    $l5 = [string](Get-ConfigValue -Config $Layers -Name 'L5_functional' -Default 'PENDING')
    $l6 = [string](Get-ConfigValue -Config $Layers -Name 'L6_recovery' -Default 'PENDING')
    $states = @($l5, $l6)

    if (@($states | Where-Object { $_ -like 'FAIL*' }).Count -gt 0) {
        return 'FAIL_DQA'
    }

    $naStates = @('N_A', 'NOT_REQUIRED')
    if (@($states | Where-Object { $_ -notin $naStates }).Count -eq 0) {
        return 'N_A_DQA'
    }

    if (@($states | Where-Object { $_ -eq 'PASS' }).Count -eq $states.Count) {
        return 'PASS_DQA'
    }

    return 'PENDING_DQA'
}

function Apply-VerdictPolicy {
    param(
        [System.Collections.IDictionary]$Report
    )

    $layers = Get-ConfigValue -Config $Report -Name 'validation_layers' -Default ([ordered]@{})
    $swLayers = @(
        [string](Get-ConfigValue -Config $layers -Name 'L1_configuration' -Default 'PENDING'),
        [string](Get-ConfigValue -Config $layers -Name 'L2_capability' -Default 'PENDING'),
        [string](Get-ConfigValue -Config $layers -Name 'L3_api' -Default 'PENDING'),
        [string](Get-ConfigValue -Config $layers -Name 'L4_readback' -Default 'PENDING')
    )

    # Phase 1 SW verdict: every SW layer must have actually passed. PENDING or
    # CONDITIONAL means the API path was not exercised, so it is not a pass.
    $swFail = @($swLayers | Where-Object { $_ -like 'FAIL*' }).Count -gt 0
    $swDone = @($swLayers | Where-Object { $_ -notin @('PASS', 'N_A', 'NOT_REQUIRED') }).Count -eq 0
    $swVerdict = if ($swFail) { 'FAIL_SW' } elseif ($swDone) { 'PASS_SW' } else { 'PENDING_SW' }
    $dqaVerdict = Resolve-DqaVerdictFromLayers -Layers $layers
    $exitCode = if ($swVerdict -eq 'FAIL_SW') { 1 } else { 0 }

    $Report.sw_verdict = $swVerdict
    $Report.dqa_verdict = $dqaVerdict
    $Report.ci_exit_code = $exitCode
    $Report.exit_code_policy = [ordered]@{
        fail_sw = 1
        pass_sw = 0
        pending_sw = 0
    }

    return [ordered]@{
        sw_verdict = $swVerdict
        dqa_verdict = $dqaVerdict
        exit_code = $exitCode
    }
}

function Invoke-ApiHarness([string]$apiHarness, [string]$section, [string]$configPath) {
    if ([string]::IsNullOrWhiteSpace($apiHarness)) { return $null }
    if (-not (Test-Path $apiHarness)) { return $null }
    $raw = & $apiHarness -Section $section -ConfigPath $configPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ApiHarness failed ($LASTEXITCODE): $($raw -join "`n")"
    }
    $text = ($raw -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "ApiHarness returned empty output"
    }
    return (Convert-JsonTextToHashtable -text $text)
}
