@echo off
setlocal

cd /d "%~dp0.."

set "PYTHON_EXE="
call :try_python ".venv\Scripts\python.exe"
call :try_python "venv\Scripts\python.exe"
call :try_command py
call :try_command python
call :try_existing "%LocalAppData%\Programs\Python\Python313\python.exe"
call :try_existing "%LocalAppData%\Programs\Python\Python312\python.exe"

if not defined PYTHON_EXE (
  echo Python not found.
  pause
  exit /b 1
)

%PYTHON_EXE% scripts\tray_app.py
exit /b %errorlevel%

:try_python
if defined PYTHON_EXE exit /b 0
if not exist "%~1" exit /b 0
"%~1" --version >nul 2>nul && set "PYTHON_EXE=%~1"
exit /b 0

:try_command
if defined PYTHON_EXE exit /b 0
where %~1 >nul 2>nul || exit /b 0
%~1 --version >nul 2>nul && set "PYTHON_EXE=%~1"
exit /b 0

:try_existing
if defined PYTHON_EXE exit /b 0
if exist "%~1" set "PYTHON_EXE=%~1"
exit /b 0
