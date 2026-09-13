@echo off
cd /d %~dp0
:loop
python -u main.py >> log.txt 2>&1
echo [%date% %time%] 程序退出，10秒后自动重启... >> log.txt
timeout /t 10 /nobreak >nul
goto loop
