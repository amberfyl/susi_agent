param(
    [string[]]$DllDirs = @(),
    [string]$OutFile = ".\susi_i2c_smbus_probe_report.txt",
    [switch]$Append
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
using System.Text;

public static class NativeSusi
{
    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibInitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiLibUninitialize();

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiBoardGetValue(UInt32 Id, out UInt32 pValue);

    [DllImport("Susi4.dll", CharSet = CharSet.Ansi)]
    public static extern UInt32 SusiBoardGetStringA(UInt32 Id, StringBuilder pBuffer, ref UInt32 pBufLen);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CGetFrequency(UInt32 Id, out UInt32 pFreq);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiI2CProbeDevice(UInt32 Id, UInt32 Addr);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiSMBReadQuick(UInt32 Id, byte Addr);
}
"@

Add-Type -TypeDefinition $cs -Language CSharp

# I2C bus ids + their bit in SUSI_ID_I2C_SUPPORTED
$I2C_BUSES = @(
    @{N='I2C_EXTERNAL'; ID=0; Bit=(1 -shl 0)},
    @{N='I2C_OEM0'; ID=1; Bit=(1 -shl 1)},
    @{N='I2C_OEM1'; ID=2; Bit=(1 -shl 2)},
    @{N='I2C_OEM2'; ID=3; Bit=(1 -shl 3)},
    @{N='I2C_OEM3'; ID=4; Bit=(1 -shl 4)},
    @{N='I2C_OEM4'; ID=5; Bit=(1 -shl 5)},
    @{N='I2C_OEM5'; ID=6; Bit=(1 -shl 6)}
)

# SMBus bus ids + their bit in SUSI_ID_SMBUS_SUPPORTED
$SMBUS_BUSES = @(
    @{N='SMBUS_EXTERNAL'; ID=0; Bit=(1 -shl 0)},
    @{N='SMBUS_OEM0'; ID=1; Bit=(1 -shl 1)},
    @{N='SMBUS_OEM1'; ID=2; Bit=(1 -shl 2)},
    @{N='SMBUS_OEM2'; ID=3; Bit=(1 -shl 3)},
    @{N='SMBUS_OEM3'; ID=4; Bit=(1 -shl 4)}
)

$SUSI_ID_SMBUS_SUPPORTED = 0x00030000
$SUSI_ID_I2C_SUPPORTED   = 0x00030100

$NAME_BASE_I2C  = 0x20000000   # SUSI_ID_BASE_GET_NAME_I2C
$NAME_BASE_SMB  = 0x30000000   # SUSI_ID_BASE_GET_NAME_SMB

function Get-ChannelName([UInt32]$busId, [UInt32]$nameBase) {
    $len = [UInt32]32
    $sb = New-Object System.Text.StringBuilder 32
    $id = [UInt32]($busId -bor $nameBase)
    $st = [NativeSusi]::SusiBoardGetStringA($id, $sb, [ref]$len)
    if ($st -eq 0) { return $sb.ToString() }
    return ''
}

function Get-StatusName([UInt32]$st) {
    # NOTE: switching on the UInt32 value directly is broken in PowerShell --
    # hex literals above 0x7FFFFFFF parse as signed Int32/Int64, so they never
    # compare equal to the actual UInt32 return value. Switch on the formatted
    # hex string instead to sidestep the sign mismatch entirely.
    $hex = '0x{0:X8}' -f $st
    switch ($hex) {
        '0x00000000' { 'SUSI_STATUS_SUCCESS' }
        '0xFFFFFFFF' { 'SUSI_STATUS_NOT_INITIALIZED' }
        '0xFFFFFFFE' { 'SUSI_STATUS_INITIALIZED' }
        '0xFFFFFFFD' { 'SUSI_STATUS_ALLOC_ERROR' }
        '0xFFFFFFFC' { 'SUSI_STATUS_DRIVER_TIMEOUT' }
        '0xFFFFFEFF' { 'SUSI_STATUS_INVALID_PARAMETER' }
        '0xFFFFFEFE' { 'SUSI_STATUS_INVALID_BLOCK_ALIGNMENT' }
        '0xFFFFFEFD' { 'SUSI_STATUS_INVALID_BLOCK_LENGTH' }
        '0xFFFFFEFC' { 'SUSI_STATUS_INVALID_DIRECTION' }
        '0xFFFFFEFB' { 'SUSI_STATUS_INVALID_BITMASK' }
        '0xFFFFFEFA' { 'SUSI_STATUS_RUNNING' }
        '0xFFFFFCFF' { 'SUSI_STATUS_UNSUPPORTED' }
        '0xFFFFFBFF' { 'SUSI_STATUS_NOT_FOUND' }
        '0xFFFFFBFE' { 'SUSI_STATUS_TIMEOUT' }
        '0xFFFFFBFD' { 'SUSI_STATUS_BUSY_COLLISION' }
        '0xFFFFFBFC' { 'SUSI_STATUS_NORESPONSE' }
        '0xFFFFFBFB' { 'SUSI_STATUS_NOACK' }
        '0xFFFFFBFA' { 'SUSI_STATUS_LOCKFAIL' }
        '0xFFFFFBF9' { 'SUSI_STATUS_DEVICE_ERROR' }
        '0xFFFFFAFF' { 'SUSI_STATUS_READ_ERROR' }
        '0xFFFFFAFE' { 'SUSI_STATUS_WRITE_ERROR' }
        '0xFFFFFAFD' { 'SUSI_STATUS_ACCESS_ERROR' }
        '0xFFFFF9FF' { 'SUSI_STATUS_MORE_DATA' }
        '0xFFFFF0FF' { 'SUSI_STATUS_ERROR' }
        default { $hex }
    }
}

function Encode7Bit([byte]$addr) {
    return [byte](($addr -band 0x7F) -shl 1)
}

# Address range 0x03-0x77 (reserved addresses excluded), one [OK]/[ERR] line
# per address, same style as susi_board_probe.ps1's Read-BoardValue output.
# Each line is written to the console as it's produced, so progress is visible
# live even though a single blocked probe call cannot itself be interrupted.
function Get-AddrLines([string]$namePrefix, [scriptblock]$probeFn) {
    $outLines = New-Object System.Collections.Generic.List[string]
    $found = New-Object System.Collections.Generic.List[string]

    for ($addr = 0x03; $addr -le 0x77; $addr++) {
        $a = [byte]$addr
        $st = & $probeFn $a
        $stName = Get-StatusName $st
        $name = ('{0}_0x{1:X2}' -f $namePrefix, $a)
        if ($st -eq 0) {
            $line = ('[OK]  {0,-42} Addr=0x{1:X2} Status={2}' -f $name, $a, $stName)
            $found.Add(('0x{0:X2}' -f $a))
        } else {
            $line = ('[ERR] {0,-42} Addr=0x{1:X2} Status={2}' -f $name, $a, $stName)
        }
        $outLines.Add($line)
        Write-Host $line
    }
    return @{ Lines = $outLines; Found = $found }
}

$script:HasWrittenOnce = $false
function Write-Report([System.Collections.Generic.List[string]]$reportLines) {
    if (($Append -or $script:HasWrittenOnce) -and (Test-Path $OutFile)) {
        $reportLines | Add-Content -Path $OutFile -Encoding UTF8
    } else {
        $reportLines | Set-Content -Path $OutFile -Encoding UTF8
    }
    $script:HasWrittenOnce = $true
}

$lines = New-Object System.Collections.Generic.List[string]

function Flush-Lines {
    if ($lines.Count -gt 0) {
        Write-Report $lines
        $lines.Clear()
    }
}

$lines.Add("SUSI I2C / SMBus Probe Report")
$lines.Add(("Time: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
$lines.Add(("Host: {0}" -f $env:COMPUTERNAME))
$lines.Add("")

try {
    $init = [NativeSusi]::SusiLibInitialize()
    if ($init -ne 0 -and $init -ne 0xFFFFFFFE) {
        $lines.Add(("Initialize failed: {0}" -f (Get-StatusName $init)))
        Flush-Lines
        Write-Output "FAILED. Report: $OutFile"
        exit 1
    }
    $lines.Add(("Initialize: {0}" -f (Get-StatusName $init)))
    $lines.Add("")

    # Ask the board which channels actually exist BEFORE touching any real bus.
    # This is a metadata read (SusiBoardGetValue), not a bus transaction.
    $i2cSupportedMask = [UInt32]0
    $stI2CCaps = [NativeSusi]::SusiBoardGetValue([UInt32]$SUSI_ID_I2C_SUPPORTED, [ref]$i2cSupportedMask)
    $smbusSupportedMask = [UInt32]0
    $stSmbCaps = [NativeSusi]::SusiBoardGetValue([UInt32]$SUSI_ID_SMBUS_SUPPORTED, [ref]$smbusSupportedMask)

    $lines.Add(("I2C_SUPPORTED mask   = 0b{0} ({1})" -f [Convert]::ToString($i2cSupportedMask,2).PadLeft(8,'0'), (Get-StatusName $stI2CCaps)))
    $lines.Add(("SMBUS_SUPPORTED mask = 0b{0} ({1})" -f [Convert]::ToString($smbusSupportedMask,2).PadLeft(8,'0'), (Get-StatusName $stSmbCaps)))
    $lines.Add("")
    Flush-Lines

    $lines.Add("========== I2C Buses ==========")
    Flush-Lines
    foreach ($bus in $I2C_BUSES) {
        if ($stI2CCaps -ne 0 -or (([UInt32]$bus.Bit) -band $i2cSupportedMask) -eq 0) {
            $lines.Add(("{0} (Id={1}): not supported per SUSI_ID_I2C_SUPPORTED, skip" -f $bus.N, $bus.ID))
            $lines.Add("")
            Flush-Lines
            continue
        }

        Write-Host ("--- {0} (Id={1}): scanning addresses ---" -f $bus.N, $bus.ID)
        $freq = [UInt32]0
        [NativeSusi]::SusiI2CGetFrequency([UInt32]$bus.ID, [ref]$freq) | Out-Null
        $chName = Get-ChannelName ([UInt32]$bus.ID) $NAME_BASE_I2C
        $lines.Add(("{0} (Id={1}), Name=`"{2}`", Frequency={3}KHz" -f $bus.N, $bus.ID, $chName, $freq))
        $busId = [UInt32]$bus.ID
        # Address-by-address [OK]/[ERR] lines still print live to the console
        # (see Get-AddrLines) for progress visibility, but only the summary
        # goes into the report - the per-address detail isn't needed here.
        $result = Get-AddrLines "I2C_$($bus.N)" { param($addr) [NativeSusi]::SusiI2CProbeDevice($busId, [UInt32](Encode7Bit $addr)) }
        if ($result.Found.Count -gt 0) {
            $lines.Add(("Devices found: {0}" -f ($result.Found -join ', ')))
        } else {
            $lines.Add("Devices found: (none)")
        }
        $lines.Add("")
        Flush-Lines
    }

    $lines.Add("========== SMBus Buses ==========")
    Flush-Lines
    foreach ($bus in $SMBUS_BUSES) {
        if ($stSmbCaps -ne 0 -or (([UInt32]$bus.Bit) -band $smbusSupportedMask) -eq 0) {
            $lines.Add(("{0} (Id={1}): not supported per SUSI_ID_SMBUS_SUPPORTED, skip" -f $bus.N, $bus.ID))
            $lines.Add("")
            Flush-Lines
            continue
        }

        # SMBus quick-command probing can block for a real clock-stretch
        # timeout per address on actual hardware (seen as long strings of
        # SUSI_STATUS_TIMEOUT) - scanning all 117 addresses is what made
        # this section unresponsive on the target machine. Report existence
        # only (from the capability bitmask, already read above - no bus
        # transaction), skip the address sweep entirely.
        $chName = Get-ChannelName ([UInt32]$bus.ID) $NAME_BASE_SMB
        $lines.Add(("{0} (Id={1}), Name=`"{2}`": EXISTS (address scan skipped, see notes)" -f $bus.N, $bus.ID, $chName))
        $lines.Add("")
        Flush-Lines
    }

    $uninit = [NativeSusi]::SusiLibUninitialize()
    $lines.Add(("Uninitialize: {0}" -f (Get-StatusName $uninit)))
}
catch {
    $lines.Add("")
    $lines.Add("EXCEPTION: $($_.Exception.Message)")
    if ($_.Exception.InnerException) {
        $lines.Add("INNER: $($_.Exception.InnerException.Message)")
    }
}

Flush-Lines
Write-Output "DONE. Report: $OutFile"
