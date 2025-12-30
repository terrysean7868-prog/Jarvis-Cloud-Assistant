@echo off
setlocal

REM One-click launcher for the Windows PC Agent.
REM Usage:
REM   run_pc_agent.bat --loop --no-pause

set "REPO=%~dp0"
cd /d "%REPO%" || exit /b 1

set "LOOP=0"
set "PAUSE_ON_EXIT=1"

:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--loop" set "LOOP=1"
if /i "%~1"=="--no-pause" set "PAUSE_ON_EXIT=0"
shift
goto parse_args

:args_done

REM Prefer workspace venvs if present.
set "PYTHON=%REPO%\.venv\Scripts\python.exe"
if exist "%PYTHON%" goto have_python
set "PYTHON=%REPO%\venv\Scripts\python.exe"
if exist "%PYTHON%" goto have_python
set "PYTHON=python"

:have_python

echo.
echo Starting Jarvis PC Agent...
echo Repo: %REPO%
echo Using python: %PYTHON%
echo Tip: Put agent vars in .env.agent (recommended) or .env
echo.

:run_agent
"%PYTHON%" "%REPO%pc_agent.py"
set "EXITCODE=%ERRORLEVEL%"

if "%LOOP%"=="1" (
	REM Keep the agent alive if it exits unexpectedly.
	timeout /t 5 /nobreak >nul
	goto run_agent
)

echo.
echo PC Agent exited with code %EXITCODE%.
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b %EXITCODE%