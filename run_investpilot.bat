@echo off
cd /d "%~dp0"
echo [%date% %time%] InvestPilot startet... >> investpilot_scheduler.log
python investpilot.py >> investpilot_scheduler.log 2>&1
echo [%date% %time%] InvestPilot beendet. >> investpilot_scheduler.log
