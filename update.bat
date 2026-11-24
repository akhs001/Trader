@echo off
rem Double-click this file on Windows to update Trader to the newest version.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m trader.update & pause) else (python -m trader.update & pause)
