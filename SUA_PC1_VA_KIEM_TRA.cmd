@echo off
echo Step 1: Configure and save PC1 using the corrected console reader.
call "%~dp0run_lab.cmd" deploy --devices PC1
if errorlevel 1 goto failed
echo Step 2: Verify all six nodes.
call "%~dp0run_lab.cmd" verify
if errorlevel 1 goto failed
echo Step 3: Back up all six nodes.
call "%~dp0run_lab.cmd" backup
if errorlevel 1 goto failed
echo ALL STEPS PASSED.
pause
exit /b 0
:failed
echo A step failed. Keep this window open and send the result to the assistant.
pause
exit /b 1
