@echo off
rem Installs or repairs LectureLab for this Windows user. Double-click it, or run it again to repair.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set code=%errorlevel%
echo.
if not "%code%"=="0" echo Setup stopped. The full log is in %TEMP%\lecturelab-setup.log
pause
exit /b %code%
