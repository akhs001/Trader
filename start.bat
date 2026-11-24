@echo off
rem Double-click this file on Windows to start Trader. It opens in your browser.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m trader.app) else (python -m trader.app)
if errorlevel 1 pause
