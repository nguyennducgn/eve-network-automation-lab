@echo off
setlocal
cd /d "%~dp0"
set "LAB_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%LAB_PYTHON%" set "LAB_PYTHON=python"
"%LAB_PYTHON%" diagnose_ssh.py
echo.
pause
