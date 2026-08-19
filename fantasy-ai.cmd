@echo off
rem Runs fantasy-ai out of the project's virtual environment, so you never
rem have to activate anything.
rem
rem   PowerShell:  .\fantasy-ai analyze board
rem   cmd.exe:     fantasy-ai analyze board
setlocal
set "FA=%~dp0.venv\Scripts\fantasy-ai.exe"
if not exist "%FA%" (
  echo Virtual environment not found. Run setup.cmd first.
  exit /b 1
)
"%FA%" %*
