@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\tray_app.py
if errorlevel 1 (
  echo Startup failed. Run install.bat again or check logs.
  pause
  exit /b 1
)
