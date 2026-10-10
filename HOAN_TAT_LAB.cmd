@echo off
echo Deploying and verifying Network_Automation_6_Nodes on EVE 192.168.126.130.
echo Keep all six nodes running and close their interactive consoles.
call "%~dp0run_lab.cmd" all
echo.
echo Send the final PASS/FAIL lines or the runs folder report back to the assistant.
pause
