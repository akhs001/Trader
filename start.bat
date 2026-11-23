@echo off
rem Double-click this file on Windows to start the bot.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m trader.main --loop) else (python -m trader.main --loop)
pause
