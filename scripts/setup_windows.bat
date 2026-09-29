@echo off
setlocal

call "%~dp0..\install.bat" %*
exit /b %errorlevel%
