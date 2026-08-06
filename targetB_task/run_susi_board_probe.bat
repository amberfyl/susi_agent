@echo off
setlocal

set SCRIPT_DIR=%~dp0
set REPORT=%SCRIPT_DIR%susi_board_probe_report.txt

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%susi_board_probe.ps1" ^
  -DllDirs "C:\Windows\System32","C:\Program Files\YourProject" ^
  -OutFile "%REPORT%"

echo.
echo Report: %REPORT%
endlocal
