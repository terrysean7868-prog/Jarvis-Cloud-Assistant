@echo off
setlocal enabledelayedexpansion

REM Run Jarvis API locally (loads .env automatically via python-dotenv)
REM Usage:
REM   run_local.bat            (defaults to port 18001)
REM   run_local.bat 19001      (custom port)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=18001"

set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%CD%\venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo Starting Jarvis API on http://127.0.0.1:%PORT%
echo Using python: %PYTHON%

"%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port %PORT% --log-level info
