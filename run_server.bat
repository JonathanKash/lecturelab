@echo off
rem LectureLab server watchdog — starts the app and restarts it if it crashes.
rem Logs go to server.log next to this file.
cd /d %~dp0
:loop
rem If something is already listening on port 8000, don't start a second copy.
netstat -ano | findstr /r /c:":8000 .*LISTENING" >nul
if %errorlevel%==0 exit /b
.venv\Scripts\python.exe app.py >> server.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
