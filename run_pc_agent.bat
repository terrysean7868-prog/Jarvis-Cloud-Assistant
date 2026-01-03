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
echo Tip: You can run WITHOUT .env.agent by using an agent token from the web UI.
echo.

:run_agent
set "AGENT_TOKEN=%JARVIS_AGENT_TOKEN%"
if "%AGENT_TOKEN%"=="" (
	echo Paste Agent Token (from web UI) and press Enter.
	echo (Or just press Enter to use .env.agent / shared-secret mode.)
	set /p "AGENT_TOKEN=> "
)

if not "%AGENT_TOKEN%"=="" (
	"%PYTHON%" "%REPO%pc_agent.py" --token "%AGENT_TOKEN%"
) else (
	"%PYTHON%" "%REPO%pc_agent.py"
)
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