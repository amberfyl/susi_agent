[CmdletBinding()]
param(
    [string]$DevicePattern = '*SUSI4*',
    [string]$InstanceId = '',
    [string]$LogPath = '',
    [int]$TimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    $LogPath = Join-Path $PSScriptRoot 'susi4_driver_reload.log'
}

function Write-Log {
    param([string]$Message)

    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $Message
    Write-Host $line
    Add-Content -LiteralPath $LogPath -Value $line
}

function Get-SusiDevice {
    if (-not [string]::IsNullOrWhiteSpace($InstanceId)) {
        return @(Get-PnpDevice -InstanceId $InstanceId -ErrorAction Stop)
    }

    return @(Get-PnpDevice -Class System -PresentOnly | Where-Object {
        $_.FriendlyName -like $DevicePattern -or $_.Name -like $DevicePattern
    })
}

function Wait-ForEnabledDevice {
    param(
        [string]$TargetInstanceId,
        [int]$Seconds
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $device = Get-PnpDevice -InstanceId $TargetInstanceId -ErrorAction SilentlyContinue
        if ($null -ne $device -and $device.Status -eq 'OK' -and $device.Problem -eq 0) {
            return $device
        }

        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    return Get-PnpDevice -InstanceId $TargetInstanceId -ErrorAction SilentlyContinue
}

function Wait-ForDisabledDevice {
    param(
        [string]$TargetInstanceId,
        [int]$Seconds
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $device = Get-PnpDevice -InstanceId $TargetInstanceId -ErrorAction SilentlyContinue
        if ($null -eq $device -or $device.Status -ne 'OK' -or $device.Problem -eq 22) {
            return $device
        }

        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)

    return Get-PnpDevice -InstanceId $TargetInstanceId -ErrorAction SilentlyContinue
}

try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'This script must run as Administrator.'
    }

    if (-not (Get-Command Get-PnpDevice -ErrorAction SilentlyContinue)) {
        throw 'Get-PnpDevice is unavailable on this Windows installation.'
    }

    $logDirectory = Split-Path -Parent $LogPath
    if (-not [string]::IsNullOrWhiteSpace($logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    }

    Write-Log "Search pattern: $DevicePattern"
    if (-not [string]::IsNullOrWhiteSpace($InstanceId)) {
        Write-Log "Requested instance ID: $InstanceId"
    }

    $devices = @(Get-SusiDevice)
    if ($devices.Count -eq 0) {
        throw 'No matching SUSI4 device was found under the System device class.'
    }

    if ($devices.Count -gt 1) {
        Write-Log 'More than one matching device was found; refusing to choose automatically.'
        $devices | Select-Object Status, Problem, FriendlyName, InstanceId | Format-Table -AutoSize | Out-String | Write-Host
        throw 'Specify -InstanceId for the intended SUSI4 device.'
    }

    $device = $devices[0]
    Write-Log "Target: $($device.FriendlyName)"
    Write-Log "InstanceId: $($device.InstanceId)"
    Write-Log "Before: Status=$($device.Status); Problem=$($device.Problem)"

    Disable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
    $disabled = Wait-ForDisabledDevice -TargetInstanceId $device.InstanceId -Seconds $TimeoutSeconds
    if ($null -eq $disabled) {
        Write-Log 'Disable command completed; device is not currently returned by Get-PnpDevice.'
    } else {
        Write-Log "After disable: Status=$($disabled.Status); Problem=$($disabled.Problem)"
    }

    Enable-PnpDevice -InstanceId $device.InstanceId -Confirm:$false -ErrorAction Stop
    $enabled = Wait-ForEnabledDevice -TargetInstanceId $device.InstanceId -Seconds $TimeoutSeconds
    if ($null -eq $enabled -or $enabled.Status -ne 'OK' -or $enabled.Problem -ne 0) {
        $status = if ($null -eq $enabled) { '<missing>' } else { $enabled.Status }
        $problem = if ($null -eq $enabled) { '<missing>' } else { $enabled.Problem }
        throw "SUSI4 device did not return to OK. Status=$status; Problem=$problem"
    }

    Write-Log "After enable: Status=$($enabled.Status); Problem=$($enabled.Problem)"
    Write-Log 'SUSI4 driver disable/enable completed successfully.'
    exit 0
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}