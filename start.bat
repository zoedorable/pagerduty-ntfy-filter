@echo off
REM Start the PagerDuty filter (Windows). Runs in a minimized window "PD-Filter".
cd /d "%~dp0"
tasklist /V /FI "WINDOWTITLE eq PD-Filter*" | find /I "PD-Filter" >nul
if not errorlevel 1 (
  echo Already running.
  timeout /t 3 >nul
  exit /b
)
REM Own Python environment in this folder (one-time), so requests is always available
if not exist ".venv\Scripts\python.exe" (
  echo Setting up Python environment ^(one-time^)...
  python -m venv .venv 2>nul || py -3 -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Could not create venv. Is Python installed?
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import requests" 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install --quiet requests
  if errorlevel 1 (
    echo Installing requests failed.
    pause
    exit /b 1
  )
)
start "PD-Filter" /min cmd /c ".venv\Scripts\python.exe pd_filter.py >> pd_filter.log 2>&1"
echo ON. Log: pd_filter.log  ^|  Dashboard: view.bat
timeout /t 3 >nul
