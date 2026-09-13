@echo off
rem Starts the Telegram bot. Used by Windows Task Scheduler and for a double-click launch.
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -e .
  exit /b 1
)
if not exist "data" mkdir data
".venv\Scripts\python.exe" -m checkvalidator.cli bot >> "data\bot.log" 2>&1
