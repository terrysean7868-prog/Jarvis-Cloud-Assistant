@echo off
setlocal enabledelayedexpansion

REM One-click launcher for the Windows PC Agent.





















endlocal"%PYTHON%" pc_agent.pyecho.echo   JARVIS_AGENT_SHARED_SECRET=... (must match Render)echo   JARVIS_DEVICE_ID=primaryecho   JARVIS_SERVER_URL=https://jarvis-cloud-assistant.onrender.comecho Tip: Put agent vars in .env.agent (recommended) or .env:echo.echo Using python: %PYTHON%echo Starting PC Agent...if not exist "%PYTHON%" set "PYTHON=python"if not exist "%PYTHON%" set "PYTHON=%CD%\venv\Scripts\python.exe"set "PYTHON=%CD%\.venv\Scripts\python.exe"cd /d "%REPO%"set "REPO=%~dp0"REM The agent auto-loads .env or .env.agent now.