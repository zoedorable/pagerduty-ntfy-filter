@echo off
REM Stop the PagerDuty filter (Windows)
taskkill /FI "WINDOWTITLE eq PD-Filter*" /T /F >nul 2>&1
if errorlevel 1 (
  echo Was not running.
) else (
  echo OFF.
)
timeout /t 3 >nul
