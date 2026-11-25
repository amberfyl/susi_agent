@echo off
setlocal

set SCRIPT_DIR=%~dp0
set REPORT=%SCRIPT_DIR%susi_full_probe_report.txt

if exist "%REPORT%" del "%REPORT%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%susi_board_probe.ps1" ^
  -DllDirs "C:\Windows\System32","C:\Program Files\YourProject" ^
  -OutFile "%REPORT%"

echo. >> "%REPORT%"
echo ============================================================ >> "%REPORT%"
echo. >> "%REPORT%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%susi_gpio_probe.ps1" ^
  -DllDirs "C:\Windows\System32","C:\Program Files\YourProject" ^
  -OutFile "%REPORT%" -Append

echo. >> "%REPORT%"
echo ============================================================ >> "%REPORT%"
echo. >> "%REPORT%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%susi_i2c_smbus_probe.ps1" ^
  -DllDirs "C:\Windows\System32","C:\Program Files\YourProject" ^
  -OutFile "%REPORT%" -Append

echo. >> "%REPORT%"
echo ============================================================ >> "%REPORT%"
echo. >> "%REPORT%"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%susi_vga_backlight_probe.ps1" ^
  -DllDirs "C:\Windows\System32","C:\Program Files\YourProject" ^
  -OutFile "%REPORT%" -Append

echo.
echo Report: %REPORT%
endlocal
