param(
    [string[]]$DllDirs = @(),
    [int]$MaxBankNum = 4,
    [string]$OutFile = ".\susi_gpio_probe_report.txt",
    [switch]$Append
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

    [DllImport("Susi4.dll", CharSet = CharSet.Ansi)]
    public static extern UInt32 SusiBoardGetStringA(UInt32 Id, StringBuilder pBuffer, ref UInt32 pBufLen);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetDirection(UInt32 Id, UInt32 Bitmask, out UInt32 pDirection);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiGPIOGetLevel(UInt32 Id, UInt32 Bitmask, out UInt32 pLevel);
}
"@

Add-Type -TypeDefinition $cs -Language CSharp

# ---- Constants (from Susi4.h GPIO section) ----
$GPIO_BANK_BASE       = 0x00010000
$ITEM_INPUT_SUPPORT   = 0
$ITEM_OUTPUT_SUPPORT  = 1
$ITEM_INT_SUPPORT     = 2
$NAME_BASE_GPIO       = 0x40000000   # SUSI_ID_BASE_GET_NAME_GPIO
$GPIO_INPUT           = 1
$GPIO_HIGH            = 1

function Get-StatusName([UInt32]$st) {
    # Switch on the formatted hex string, not the raw value: hex literals
    # above 0x7FFFFFFF parse as signed Int32/Int64 in PowerShell and never
    # compare equal to the actual UInt32 return value otherwise.
    $hex = '0x{0:X8}' -f $st
    switch ($hex) {
        '0x00000000' { 'SUSI_STATUS_SUCCESS' }
        '0xFFFFFFFF' { 'SUSI_STATUS_NOT_INITIALIZED' }
        '0xFFFFFFFE' { 'SUSI_STATUS_INITIALIZED' }
        '0xFFFFFFFD' { 'SUSI_STATUS_ALLOC_ERROR' }
        '0xFFFFFFFC' { 'SUSI_STATUS_DRIVER_TIMEOUT' }
        '0xFFFFFEFF' { 'SUSI_STATUS_INVALID_PARAMETER' }
        '0xFFFFFCFF' { 'SUSI_STATUS_UNSUPPORTED' }
        '0xFFFFFBFF' { 'SUSI_STATUS_NOT_FOUND' }
        '0xFFFFFBFE' { 'SUSI_STATUS_TIMEOUT' }
        '0xFFFFF9FF' { 'SUSI_STATUS_MORE_DATA' }
        default { $hex }
    }
}

function Get-GpioName([UInt32]$pinId) {
    $len = [UInt32]32
    $sb = New-Object System.Text.StringBuilder 32
    $id = [UInt32]($pinId -bor $NAME_BASE_GPIO)
    $st = [NativeSusi]::SusiBoardGetStringA($id, $sb, [ref]$len)
    if ($st -eq 0) { return $sb.ToString().Trim() }
    return ''
}

# Same [OK]/[ERR] NAME  Id=0x... Value=.../Status=... style as susi_board_probe.ps1.
function Write-ValueLine([System.Collections.Generic.List[string]]$target, [string]$name, [UInt32]$id, [UInt32]$status, [string]$value) {
    if ($status -eq 0) {
        $target.Add(('[OK]  {0,-42} Id=0x{1:X8} Value={2}' -f $name, $id, $value))
    } else {
        $target.Add(('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f $name, $id, (Get-StatusName $status)))
    }
}

function Write-Report([System.Collections.Generic.List[string]]$reportLines) {
    if ($Append -and (Test-Path $OutFile)) {
        $reportLines | Add-Content -Path $OutFile -Encoding UTF8
    } else {
        $reportLines | Set-Content -Path $OutFile -Encoding UTF8
    }
}

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("SUSI GPIO Probe Report")
$lines.Add(("Time: {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
$lines.Add(("Host: {0}" -f $env:COMPUTERNAME))
$lines.Add("")

try {
    $init = [NativeSusi]::SusiLibInitialize()
    if ($init -ne 0 -and $init -ne 0xFFFFFFFE) {
        $lines.Add(("Initialize failed: {0}" -f (Get-StatusName $init)))
        Write-Report $lines
        Write-Output "FAILED. Report: $OutFile"
        exit 1
    }
    $lines.Add(("Initialize: {0}" -f (Get-StatusName $init)))
    $lines.Add("")

    $totalPins = 0

    for ($bank = 0; $bank -lt $MaxBankNum; $bank++) {
        $bankId = [UInt32]($GPIO_BANK_BASE + $bank)

        $inSup  = [UInt32]0
        $outSup = [UInt32]0
        $intSup = [UInt32]0

        $stIn  = [NativeSusi]::SusiGPIOGetCaps($bankId, $ITEM_INPUT_SUPPORT, [ref]$inSup)
        if ($stIn -ne 0) {
            $lines.Add(('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f "GPIO_Bank$bank", $bankId, (Get-StatusName $stIn)))
            $lines.Add("")
            continue
        }
        $stOut = [NativeSusi]::SusiGPIOGetCaps($bankId, $ITEM_OUTPUT_SUPPORT, [ref]$outSup)
        $stInt = [NativeSusi]::SusiGPIOGetCaps($bankId, $ITEM_INT_SUPPORT, [ref]$intSup)

        Write-ValueLine $lines "GPIO_Bank${bank}_InputSupport"  $bankId $stIn  ('0b{0}' -f [Convert]::ToString($inSup, 2).PadLeft(32,'0'))
        Write-ValueLine $lines "GPIO_Bank${bank}_OutputSupport" $bankId $stOut ('0b{0}' -f [Convert]::ToString($outSup,2).PadLeft(32,'0'))
        Write-ValueLine $lines "GPIO_Bank${bank}_IntSupport"    $bankId $stInt ('0b{0}' -f [Convert]::ToString($intSup,2).PadLeft(32,'0'))

        for ($bit = 0; $bit -lt 32; $bit++) {
            $mask = [UInt32]((1L -shl $bit) -band 0xFFFFFFFFL)
            $canIn  = ($inSup  -band $mask) -ne 0
            $canOut = ($outSup -band $mask) -ne 0
            if (-not $canIn -and -not $canOut) { continue }

            $pinId = [UInt32](($bank -shl 5) + $bit)
            $totalPins++

            $name = Get-GpioName $pinId
            $label = if ($name) { "GPIO_B${bank}P${bit}_$name" } else { "GPIO_B${bank}P${bit}" }

            $dir = [UInt32]0
            $stDir = [NativeSusi]::SusiGPIOGetDirection($pinId, 1, [ref]$dir)
            $dirStr = if ($dir -eq $GPIO_INPUT) { 'INPUT' } else { 'OUTPUT' }
            Write-ValueLine $lines "${label}_Direction" $pinId $stDir $dirStr

            $lvl = [UInt32]0
            $stLvl = [NativeSusi]::SusiGPIOGetLevel($pinId, 1, [ref]$lvl)
            $lvlStr = if ($lvl -eq $GPIO_HIGH) { 'HIGH' } else { 'LOW' }
            Write-ValueLine $lines "${label}_Level" $pinId $stLvl $lvlStr
        }
        $lines.Add("")
    }

    $lines.Add(("Total probed pins: {0}" -f $totalPins))

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

Write-Report $lines
Write-Output "DONE. Report: $OutFile"
