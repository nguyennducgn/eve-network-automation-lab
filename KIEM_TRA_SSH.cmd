@echo off
setlocal
cd /d "%~dp0"
call run_lab.cmd verify --transport ssh
echo.
pause
