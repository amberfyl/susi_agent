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

$implementation = Join-Path $PSScriptRoot 'run_hwm_fan_control_validation_section.ps1'
if (-not (Test-Path -LiteralPath $implementation -PathType Leaf)) {
    throw "Missing control implementation: $implementation"
}

& $implementation @PSBoundParameters
exit $LASTEXITCODE
