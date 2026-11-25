param(
    [string[]]$DllDirs = @(),
    [string]$OutFile = ".\susi_vga_backlight_probe_report.txt",
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

    [DllImport("Susi4.dll", CharSet = CharSet.Ansi)]
    public static extern UInt32 SusiBoardGetStringA(UInt32 Id, StringBuilder pBuffer, ref UInt32 pBufLen);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetCaps(UInt32 Id, UInt32 ItemId, out UInt32 pValue);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetBacklightEnable(UInt32 Id, out UInt32 pEnable);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetBacklightBrightness(UInt32 Id, out UInt32 pBright);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetBacklightLevel(UInt32 Id, out UInt32 pLevel);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetPolarity(UInt32 Id, out UInt32 pPolarity);

    [DllImport("Susi4.dll")]
    public static extern UInt32 SusiVgaGetFrequency(UInt32 Id, out UInt32 pFrequency);
}
"@

Add-Type -TypeDefinition $cs -Language CSharp

# ---- Constants (from Susi4.h VGA/Backlight section) ----
# SUSI_ID_BACKLIGHT_MAX also bounds the [VGA.Brightness] channel count - both
# sections in the ini (Backlight1..4 / Brightness1..4) share the same 0-3 Id
# range, they're just two independent config tables keyed by the same Id.
$SUSI_ID_BACKLIGHT_MAX = 4
$NAME_BASE_VGA_BACKLIGHT = 0x60000000   # SUSI_ID_BASE_GET_NAME_VGA_BACKLIGHT
$SUSI_ID_VGA_BRIGHTNESS_MAXIMUM = 0x00010000
$SUSI_ID_VGA_BRIGHTNESS_MINIMUM = 0x00010001

function Get-StatusName([UInt32]$st) {
    $hex = '0x{0:X8}' -f $st
    switch ($hex) {
        '0x00000000' { 'SUSI_STATUS_SUCCESS' }
        '0xFFFFFFFF' { 'SUSI_STATUS_NOT_INITIALIZED' }
        '0xFFFFFFFE' { 'SUSI_STATUS_INITIALIZED' }
        '0xFFFFFEFF' { 'SUSI_STATUS_INVALID_PARAMETER' }
        '0xFFFFFCFF' { 'SUSI_STATUS_UNSUPPORTED' }
        '0xFFFFFBFF' { 'SUSI_STATUS_NOT_FOUND' }
        default { $hex }
    }
}

function Get-PanelName([UInt32]$panelId) {
    $len = [UInt32]32
    $sb = New-Object System.Text.StringBuilder 32
    $id = [UInt32]($panelId -bor $NAME_BASE_VGA_BACKLIGHT)
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
$lines.Add("SUSI VGA Backlight/Brightness Probe Report")
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

    $foundBrightness = 0
    $foundBacklight = 0

    # ========== [VGA.Brightness] Channel1..4 ==========
    # Maps 1:1 to SusiVgaGetCaps(Id, SUSI_ID_VGA_BRIGHTNESS_MAXIMUM/MINIMUM) -
    # this is what ini's LC_Brightness() populates (Max/Min/Frequency/Name).
    $lines.Add("========== VGA.Brightness Channels ==========")
    for ($i = 0; $i -lt $SUSI_ID_BACKLIGHT_MAX; $i++) {
        $panelId = [UInt32]$i
        $chNum = $i + 1   # 1-based, matches ini key "Brightness1".."Brightness4"
        $name = Get-PanelName $panelId
        $label = if ($name) { "Brightness${chNum}_$name" } else { "Brightness${chNum}" }

        $maxB = [UInt32]0
        $stMax = [NativeSusi]::SusiVgaGetCaps($panelId, $SUSI_ID_VGA_BRIGHTNESS_MAXIMUM, [ref]$maxB)
        $minB = [UInt32]0
        $stMin = [NativeSusi]::SusiVgaGetCaps($panelId, $SUSI_ID_VGA_BRIGHTNESS_MINIMUM, [ref]$minB)

        if ($stMax -ne 0 -and $stMin -ne 0) {
            $lines.Add(('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f $label, $panelId, (Get-StatusName $stMax)))
            continue
        }

        $foundBrightness++
        $lines.Add(('[OK]  {0,-42} Id=0x{1:X8} Status=FOUND' -f $label, $panelId))
        Write-ValueLine $lines "${label}_Max" $panelId $stMax $maxB.ToString()
        Write-ValueLine $lines "${label}_Min" $panelId $stMin $minB.ToString()
    }
    $lines.Add("")

    # ========== [VGA.Backlight] Channel1..4 ==========
    # Maps 1:1 to SusiVgaGetBacklightEnable/Brightness/Level/Polarity/Frequency -
    # this is what ini's LC_Backlight() populates (HWID/ChannelID/IOPort/Option).
    $lines.Add("========== VGA.Backlight Channels ==========")
    for ($i = 0; $i -lt $SUSI_ID_BACKLIGHT_MAX; $i++) {
        $panelId = [UInt32]$i
        $chNum = $i + 1   # 1-based, matches ini key "Backlight1".."Backlight4"
        $name = Get-PanelName $panelId
        $label = if ($name) { "Backlight${chNum}_$name" } else { "Backlight${chNum}" }

        $enable = [UInt32]0
        $stEnable = [NativeSusi]::SusiVgaGetBacklightEnable($panelId, [ref]$enable)
        $bright = [UInt32]0
        $stBright = [NativeSusi]::SusiVgaGetBacklightBrightness($panelId, [ref]$bright)
        $level = [UInt32]0
        $stLevel = [NativeSusi]::SusiVgaGetBacklightLevel($panelId, [ref]$level)
        $polarity = [UInt32]0
        $stPolarity = [NativeSusi]::SusiVgaGetPolarity($panelId, [ref]$polarity)
        $freq = [UInt32]0
        $stFreq = [NativeSusi]::SusiVgaGetFrequency($panelId, [ref]$freq)

        $anyOk = ($stEnable -eq 0 -or $stBright -eq 0 -or $stLevel -eq 0 -or $stPolarity -eq 0 -or $stFreq -eq 0)
        if (-not $anyOk) {
            $lines.Add(('[ERR] {0,-42} Id=0x{1:X8} Status={2}' -f $label, $panelId, (Get-StatusName $stEnable)))
            continue
        }

        $foundBacklight++
        $lines.Add(('[OK]  {0,-42} Id=0x{1:X8} Status=FOUND' -f $label, $panelId))
        Write-ValueLine $lines "${label}_Enable" $panelId $stEnable $(if ($enable -ne 0) { 'ON' } else { 'OFF' })
        Write-ValueLine $lines "${label}_Brightness" $panelId $stBright $bright.ToString()
        Write-ValueLine $lines "${label}_Level" $panelId $stLevel $level.ToString()
        Write-ValueLine $lines "${label}_Polarity" $panelId $stPolarity $(if ($polarity -ne 0) { 'INVERT' } else { 'NON-INVERT' })
        Write-ValueLine $lines "${label}_Frequency" $panelId $stFreq ('{0} Hz' -f $freq)
    }
    $lines.Add("")

    $lines.Add(("SUMMARY: Brightness channels found: {0}, Backlight channels found: {1}" -f $foundBrightness, $foundBacklight))

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
