@echo off
cd /d "C:\Users\CarlosBaumann\OneDrive - Mattka GmbH\Desktop\Claude\investpilot"
echo [%date% %time%] InvestPilot gestartet >> trader_scheduler.log
python demo_trader.py >> trader_scheduler.log 2>&1
echo [%date% %time%] InvestPilot beendet >> trader_scheduler.log
echo. >> trader_scheduler.log
