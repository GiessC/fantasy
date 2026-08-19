@echo off
rem One-shot Windows setup. Double-click this file, or type: setup
rem
rem -ExecutionPolicy Bypass is the whole point: a default Windows install
rem refuses to run .ps1 files, and that refusal is the single most common
rem reason Python setup on Windows appears to be broken.
chcp 65001 >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup did not complete. The error is above.
)
echo.
pause
