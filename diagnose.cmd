@echo off
rem Prints everything needed to work out why Python is not being found.
rem Plain batch on purpose: no PowerShell, so it cannot fail the same way
rem setup.cmd might. Run it and send the whole output.
echo ===== where python =====
where python 2>&1
echo.
echo ===== where python3 =====
where python3 2>&1
echo.
echo ===== where py =====
where py 2>&1
echo.
echo ===== py -0p  (installed interpreters, per the launcher) =====
py -0p 2>&1
echo.
echo ===== python --version =====
python --version 2>&1
echo.
echo ===== py --version =====
py --version 2>&1
echo.
echo ===== per-user install folder =====
if exist "%LOCALAPPDATA%\Programs\Python" (
  dir /b "%LOCALAPPDATA%\Programs\Python" 2>&1
) else (
  echo not present: %LOCALAPPDATA%\Programs\Python
)
echo.
echo ===== all-users install folder =====
dir /b "%ProgramFiles%" 2>nul | findstr /i python
if errorlevel 1 echo no Python folder under %ProgramFiles%
echo.
echo ===== PATH =====
echo %PATH%
echo.
pause
