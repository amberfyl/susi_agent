param(
    [string[]]$DllDirs = @(),
    [string]$OutFile = ".\susi_board_probe_report.txt"
)

$ErrorActionPreference = 'Stop'

# ---- Load DLL search paths (System32 / Program Files / custom project dir) ----
foreach ($d in $DllDirs) {
    if ([string]::IsNullOrWhiteSpace($d)) { continue }
    if (Test-Path $d) {
        $env:PATH = "$d;$env:PATH"
    }
}

$cs = @"
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class NativeSusi
{
    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibInitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibUninitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiBoardGetValue(UInt32 Id, ref UInt32 pValue);

    [DllImport("Susi4.dll", CharSet = CharSet.Ansi)]
    public static extern UInt32 SusiBoardGetStringA(UInt32 Id, StringBuilder pBuffer, ref UInt32 pBufLen);
}
"@

Add-Type -TypeDefinition $cs -Language CSharp

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

function Read-BoardValue([string]$name, [UInt32]$id, [string]$unit = '', [scriptblock]$decode = $null) {
    $v = [UInt32]0
    $st = [NativeSusi]::SusiBoardGetValue($id, [ref]$v)
    $stName = Get-StatusName $st
    if ($st -eq 0) {
        $display = if ($decode) { & $decode $v } else { "$v" }
        if ($unit) { $display = "$display $unit" }
        return ('[OK]  {0,-42} Id=0x{1:X8} Value={2}' -f $name, $id, $display)
    }
    return ('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f $name, $id, $stName)
}

function Read-BoardString([string]$name, [UInt32]$id) {
    $len = [UInt32]512
    $sb = New-Object System.Text.StringBuilder 512
    $st = [NativeSusi]::SusiBoardGetStringA($id, $sb, [ref]$len)
    $stName = Get-StatusName $st
    if ($st -eq 0) {
        return ('[OK]  {0,-42} Id=0x{1:X8} Value="{2}"' -f $name, $id, $sb.ToString())
    }
    return ('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f $name, $id, $stName)
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("SUSI Board Probe Report")
$lines.Add(("Time: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
$lines.Add(("Host: {0}" -f $env:COMPUTERNAME))
$lines.Add("")

try {
    $init = [NativeSusi]::SusiLibInitialize()
    if ($init -ne 0 -and $init -ne 0xFFFFFFFE) {
        $lines.Add(("Initialize failed: {0}" -f (Get-StatusName $init)))
        $lines | Set-Content -Path $OutFile -Encoding UTF8
        Write-Output "FAILED. Report: $OutFile"
        exit 1
    }

    $lines.Add(("Initialize: {0}" -f (Get-StatusName $init)))
    $lines.Add("")

    $lines.Add("=== Board String Info ===")
    $stringIds = @(
        @{N='BOARD_MANUFACTURER_STR'; ID=0x00000000},
        @{N='BOARD_NAME_STR'; ID=0x00000001},
        @{N='BOARD_REVISION_STR'; ID=0x00000002},
        @{N='BOARD_SERIAL_STR'; ID=0x00000003},
        @{N='BOARD_BIOS_REVISION_STR'; ID=0x00000004},
        @{N='BOARD_HW_REVISION_STR'; ID=0x00000005},
        @{N='BOARD_PLATFORM_TYPE_STR'; ID=0x00000006},
        @{N='BOARD_EC_FW_STR'; ID=0x00000007},
        @{N='BOARD_BIOS_FW_STR'; ID=0x00000008}
    )
    foreach ($x in $stringIds) { $lines.Add((Read-BoardString $x.N $x.ID)) }
    $lines.Add("")

    $lines.Add("=== Board Numeric Info ===")
    $boardIds = @(
        @{N='GET_SPEC_VERSION'; ID=0x00000000},
        @{N='BOARD_BOOT_COUNTER_VAL'; ID=0x00000001},
        @{N='BOARD_RUNNING_TIME_METER_VAL'; ID=0x00000002; U='min'},
        @{N='BOARD_PNPID_VAL'; ID=0x00000003},
        @{N='BOARD_PLATFORM_REV_VAL'; ID=0x00000004},
        @{N='BOARD_LAST_SHUTDOWN_STATUS_VAL'; ID=0x00000005},
        @{N='BOARD_LAST_SHUTDOWN_EVENT_VAL'; ID=0x00000006},
        @{N='BOARD_DRIVER_VERSION_VAL'; ID=0x00010000},
        @{N='BOARD_LIB_VERSION_VAL'; ID=0x00010001},
        @{N='BOARD_FIRMWARE_VERSION_VAL'; ID=0x00010002},
        @{N='BOARD_DOCUMENT_VERSION_VAL'; ID=0x00010005},
        @{N='SMBUS_SUPPORTED'; ID=0x00030000},
        @{N='I2C_SUPPORTED'; ID=0x00030100}
    )
    foreach ($x in $boardIds) {
        $u = if ($x.ContainsKey('U')) { $x.U } else { '' }
        $lines.Add((Read-BoardValue $x.N $x.ID $u))
    }
    $lines.Add("")

    $lines.Add("=== HWM Temperature (0.1K / decoded C) ===")
    $tempNames = @('CPU','CHIPSET','SYSTEM','CPU2','OEM0','OEM1','OEM2','OEM3','OEM4','OEM5','SYSTEM2','GRAPHIC')
    for ($i=0; $i -lt $tempNames.Count; $i++) {
        $id = [uint32](0x00020000 + $i)
        $nm = "HWM_TEMP_$($tempNames[$i])"
        $lines.Add((Read-BoardValue $nm $id '' { param($v) ('{0} (={1:N1} C)' -f $v, (($v - 2731) / 10.0)) }))
    }
    $lines.Add("")

    $lines.Add("=== HWM Voltage (mV) ===")
    $voltNames = @('VCORE','VCORE2','2V5','3V3','5V','12V','5VSB','3VSB','VBAT','5NV','12NV','VTT','24V','DC','DCSTBY','VBATLI','OEM0','OEM1','OEM2','OEM3','1V05','1V5','1V8','12VS5','5VS5','3V3S5')
    for ($i=0; $i -lt $voltNames.Count; $i++) {
        $id = [uint32](0x00021000 + $i)
        $nm = "HWM_VOLTAGE_$($voltNames[$i])"
        $lines.Add((Read-BoardValue $nm $id 'mV'))
    }
    $lines.Add("")

    $lines.Add("=== HWM Fan (RPM) ===")
    $fanNames = @('CPU','SYSTEM','CPU2','OEM0','OEM1','OEM2','OEM3','OEM4','OEM5','OEM6','SYSTEM2','SYSTEM3','SYSTEM4')
    for ($i=0; $i -lt $fanNames.Count; $i++) {
        $id = [uint32](0x00022000 + $i)
        $nm = "HWM_FAN_$($fanNames[$i])"
        $lines.Add((Read-BoardValue $nm $id 'RPM'))
    }
    $lines.Add("")

    $lines.Add("=== HWM Current (mA) ===")
    for ($i=0; $i -lt 3; $i++) {
        $id = [uint32](0x00023000 + $i)
        $nm = "HWM_CURRENT_OEM$($i)"
        $lines.Add((Read-BoardValue $nm $id 'mA'))
    }
    $lines.Add("")

    $lines.Add("=== HWM CaseOpen (Demo HWM page has this) ===")
    for ($i=0; $i -lt 3; $i++) {
        $id = [uint32](0x00024000 + $i)
        $nm = "HWM_CASEOPEN_OEM$($i)"
        $lines.Add((Read-BoardValue $nm $id))
    }

    $uninit = [NativeSusi]::SusiLibUninitialize()
    $lines.Add("")
    $lines.Add(("Uninitialize: {0}" -f (Get-StatusName $uninit)))
}
catch {
    $lines.Add("")
    $lines.Add("EXCEPTION: $($_.Exception.Message)")
    if ($_.Exception.InnerException) {
        $lines.Add("INNER: $($_.Exception.InnerException.Message)")
    }
}

$lines | Set-Content -Path $OutFile -Encoding UTF8
Write-Output "DONE. Report: $OutFile"