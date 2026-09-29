@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1" %*
set "RESULT=%errorlevel%"
pause
exit /b %RESULT%
