@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "SCRIPT=%SCRIPT_DIR%reload_susi4_driver.ps1"
set "LOG=%SCRIPT_DIR%susi4_driver_reload.log"

if not exist "%SCRIPT%" (
  echo [ERROR] Missing script: %SCRIPT%
  exit /b 2
)

fltmc >nul 2>&1
if errorlevel 1 (
  echo [INFO] Requesting administrator permission...
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%SCRIPT_DIR%'"
  exit /b 0
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" ^
  -DevicePattern "*SUSI4*" ^
  -LogPath "%LOG%"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Log: %LOG%
exit /b %EXIT_CODE%