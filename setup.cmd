@echo off
rem Installs or repairs LectureLab for this Windows user. Double-click it, or run it again to repair.
rem The full path, so a powershell.exe sitting next to this file can never run instead.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set code=%errorlevel%
echo.
if not "%code%"=="0" echo Setup stopped. The full log is in %TEMP%\lecturelab-setup.log
pause
exit /b %code%
