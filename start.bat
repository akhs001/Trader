@echo off
rem Double-click this file on Windows to start Trader. It opens in your browser.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -c "import yfinance" >nul 2>nul || (echo Installing the price library, this takes a minute the first time... & %PY% -m pip install --quiet -r requirements.txt)
%PY% -m trader.app
if errorlevel 1 pause
