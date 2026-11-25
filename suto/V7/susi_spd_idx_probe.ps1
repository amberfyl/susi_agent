param(
    [string[]]$DllDirs = @(),
    [int]$IdxMin = 0,
    [int]$IdxMax = 4,
    [string]$OutFile = ".\susi_spd_idx_probe_report.txt"
)

$ErrorActionPreference = 'Stop'

foreach ($d in $DllDirs) {
    if ([string]::IsNullOrWhiteSpace($d)) { continue }
    if (Test-Path $d) {
        $env:PATH = "$d;$env:PATH"
    }
}

$cs = @"
using System;
using System.Runtime.InteropServices;

public static class NativeSusi
{
    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibInitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibUninitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiBoardGetValue(UInt32 Id, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReadByte(UInt32 Id, byte Addr, byte Cmd, out byte pBuffer);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBWriteByte(UInt32 Id, byte Addr, byte Cmd, byte Data);
}
"@

Add-Type -TypeDefinition $cs -Language CSharp

function Get-StatusName([UInt32]$st) {
    $hex = '0x{0:X8}' -f $st
    switch ($hex) {
        '0x00000000' { 'SUSI_STATUS_SUCCESS' }
        '0xFFFFFFFF' { 'SUSI_STATUS_NOT_INITIALIZED' }
        '0xFFFFFCFF' { 'SUSI_STATUS_UNSUPPORTED' }
        '0xFFFFFBFF' { 'SUSI_STATUS_NOT_FOUND' }
        '0xFFFFFBFE' { 'SUSI_STATUS_TIMEOUT' }
        default { $hex }
    }
}

function Encode7Bit([byte]$addr) {
    return [byte](($addr -band 0x7F) -shl 1)
}

# Mirrors CSPD::GetDRAMDeviceType() in SusiDevice\Library\SPD.cpp:
# read SPD byte offset 2 ("DRAM Device Type") - 0x0B=DDR3, 0x0C=DDR4.
# If neither, probe the DDR5 signature (write page 0, read byte 0 == 0x51).
function Test-SpdSlot([UInt32]$busId, [byte]$addr) {
    $data = [byte]0
    $st = [NativeSusi]::SusiSMBReadByte($busId, $addr, 2, [ref]$data)
    if ($st -eq 0 -and $data -eq 0x0B) { return @{ Found = $true; Type = 'DDR3' } }
    if ($st -eq 0 -and $data -eq 0x0C) { return @{ Found = $true; Type = 'DDR4' } }

    # DDR5 path
    [NativeSusi]::SusiSMBWriteByte($busId, $addr, 0x0B, 0) | Out-Null
    $data5 = [byte]0
    $st5 = [NativeSusi]::SusiSMBReadByte($busId, $addr, 0, [ref]$data5)
    if ($st5 -eq 0 -and $data5 -eq 0x51) { return @{ Found = $true; Type = 'DDR5' } }

    if ($st -eq 0) { return @{ Found = $false; Type = ('responded, unrecognized type=0x{0:X2}' -f $data) } }
    return @{ Found = $false; Type = (Get-StatusName $st) }
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("SUSI SMBus idx / SPD Verification Report")
$lines.Add(("Time: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
$lines.Add(("Host: {0}" -f $env:COMPUTERNAME))
$lines.Add("Purpose: for each candidate idx (SMBus BusId), probe the 8 standard DIMM SPD")
$lines.Add("addresses (0x50-0x57) reading SPD byte offset 2 (DRAM Device Type), same check")
$lines.Add("SusiDevice.dll's CSPD constructor uses. An idx that finds a valid DDR3/4/5")
$lines.Add("signature is the one wired to the DIMM socket's SPD bus - put that in the ini.")
$lines.Add("")

try {
    $cpu = Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1
    $lines.Add(("[OK]  CPU_MANUFACTURER                           Value=`"{0}`"" -f $cpu.Manufacturer))
    if ($cpu.Manufacturer -like '*Intel*') {
        $lines.Add("  Note: on Intel platforms idx is conventionally always 0. If a non-zero")
        $lines.Add("  idx is found below on an Intel board, that's unusual - double-check.")
    } elseif ($cpu.Manufacturer -like '*AMD*') {
        $lines.Add("  Note: on AMD platforms idx varies by board/SKU (0-4) - no fixed default,")
        $lines.Add("  this per-board probe is required.")
    }
} catch {
    $lines.Add(("[ERR] CPU_MANUFACTURER                           Status=WMI query failed: {0}" -f $_.Exception.Message))
}
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

    # This board's SUSI_ID_SMBUS_SUPPORTED bitmask varies per board/SKU - never
    # assume a fixed candidate set. Read it fresh every run and only probe the
    # idx values this specific board actually reports as present.
    $smbusSupportedMask = [UInt32]0
    $stSmbCaps = [NativeSusi]::SusiBoardGetValue(0x00030000, [ref]$smbusSupportedMask)
    $lines.Add(("SMBUS_SUPPORTED mask = 0b{0} ({1})" -f [Convert]::ToString($smbusSupportedMask,2).PadLeft(8,'0'), (Get-StatusName $stSmbCaps)))
    $lines.Add("")

    $bestIdx = New-Object System.Collections.Generic.List[string]

    for ($idx = $IdxMin; $idx -le $IdxMax; $idx++) {
        $bit = [UInt32](1 -shl $idx)
        if ($stSmbCaps -ne 0 -or ($bit -band $smbusSupportedMask) -eq 0) {
            $lines.Add(("idx={0}: not supported per SUSI_ID_SMBUS_SUPPORTED on this board, skip" -f $idx))
            $lines.Add("")
            continue
        }

        $busId = [UInt32]$idx
        Write-Host ("--- idx={0} (BusId={0}): probing DIMM SPD addresses 0x50-0x57 ---" -f $idx)
        $lines.Add(("=== idx={0} (BusId={0}) ===" -f $idx))

        $anyFound = $false
        for ($slot = 0; $slot -lt 8; $slot++) {
            $addr7 = [byte](0x50 + $slot)
            $addr = Encode7Bit $addr7
            $r = Test-SpdSlot $busId $addr
            if ($r.Found) {
                $anyFound = $true
                $line = ('[OK]  idx{0}_DIMM{1}_0x{2:X2}                        Addr=0x{2:X2} Value={3}' -f $idx, $slot, $addr7, $r.Type)
            } else {
                $line = ('[ERR] idx{0}_DIMM{1}_0x{2:X2}                        Addr=0x{2:X2} Status={3}' -f $idx, $slot, $addr7, $r.Type)
            }
            $lines.Add($line)
            Write-Host $line
        }

        if ($anyFound) {
            $lines.Add(("=> idx={0}: at least one valid SPD signature found - THIS LOOKS LIKE THE CORRECT idx" -f $idx))
            $bestIdx.Add($idx.ToString())
        } else {
            $lines.Add(("=> idx={0}: no valid SPD signature on any of the 8 addresses" -f $idx))
        }
        $lines.Add("")
    }

    if ($bestIdx.Count -gt 0) {
        $lines.Add(("SUMMARY: idx value(s) with a detected DIMM: {0}" -f ($bestIdx -join ', ')))
    } else {
        $lines.Add("SUMMARY: no idx in the tested range found a valid SPD device.")
        $lines.Add("  - If RAM is installed, the correct idx may be outside $IdxMin..$IdxMax, or")
        $lines.Add("    the physical SMBus routing/driver mapping itself may be wrong.")
    }

    $uninit = [NativeSusi]::SusiLibUninitialize()
    $lines.Add("")
    $lines.Add(("Uninitialize: {0}" -f (Get-StatusName $uninit)))
}
catch {
    $lines.Add("")
    $lines.Add("EXCEPTION: $($_.Exception.Message)")
}

$lines | Set-Content -Path $OutFile -Encoding UTF8
Write-Output "DONE. Report: $OutFile"
