@echo off
setlocal

REM One-click launcher for the Windows PC Agent.
REM Usage:
REM   run_pc_agent.bat --loop --no-pause

set "REPO=%~dp0"
cd /d "%REPO%" || (
	echo.
	echo ERROR: Failed to change directory to: %REPO%
	echo Please run this script from a local folder, not inside a zip, and ensure you have access.
	set "EXITCODE=1"
	goto done
)

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
echo Tip: Use an agent token from the web UI (recommended).
echo.

:run_agent
set "AGENT_TOKEN=%JARVIS_AGENT_TOKEN%"
if "%AGENT_TOKEN%"=="" (
	echo Paste Agent Token from the web UI and press Enter.
	echo Or just press Enter to use shared-secret mode from .env or environment variables.
	set /p "AGENT_TOKEN=> "
)

if not "%AGENT_TOKEN%"=="" (
	"%PYTHON%" "%REPO%apps\pc_agent\pc_agent.py" --token "%AGENT_TOKEN%"
) else (
	"%PYTHON%" "%REPO%apps\pc_agent\pc_agent.py"
)
set "EXITCODE=%ERRORLEVEL%"

if "%LOOP%"=="1" (
	REM Keep the agent alive if it exits unexpectedly.
	timeout /t 5 /nobreak >nul
	goto run_agent
)

echo.
echo PC Agent exited with code %EXITCODE%.
goto done

:done
if "%PAUSE_ON_EXIT%"=="1" pause
exit /b %EXITCODE%