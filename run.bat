@echo off
setlocal enabledelayedexpansion
title JARVIS - Cloud Assistant (Backend + Frontend)
color 0B

echo.
echo ========================================
echo        JARVIS AI ASSISTANT
echo    Just A Rather Very Intelligent System
echo ========================================
echo.

REM Get script directory
set SCRIPT_DIR=%~dp0

REM ===== Load .env file into environment =====
if exist "%SCRIPT_DIR%.env" (
  echo [*] Loading environment from .env
  REM Load .env variables (simplified approach)
  for /f "delims=" %%A in ('type "%SCRIPT_DIR%.env"') do (
    set "line=%%A"
    if not "!line:~0,1!"=="#" (
      if "!line!"=="" (
        REM skip empty lines
      ) else (
        for /f "tokens=1* delims==" %%B in ("!line!") do (
          if not "%%B"=="" (
            REM Skip problematic variables
            if not "%%B"=="SSH_KEY" (
              set "%%B=%%C"
            )
          )
        )
      )
    )
  )
  echo [OK] .env loaded
) else (
  echo [!] No .env file found. Using system environment.
)
echo.

REM ===== Check Python =====
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+ from https://www.python.org
    pause
    exit /b 1
)
echo [OK] Python found

REM ===== Check Node.js =====
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Please install from https://nodejs.org
    pause
    exit /b 1
)
echo [OK] Node.js found
echo.

REM ===== Set defaults =====
if not defined PORT set PORT=8000
if not defined BACKEND_PORT set BACKEND_PORT=%PORT%
if not defined FRONTEND_PORT set FRONTEND_PORT=3000
if not defined MODE set MODE=development
if not defined MONGODB_URI set MONGODB_URI=mongodb://localhost:27017/jarvis
if not defined MONGODB_DB_NAME set MONGODB_DB_NAME=jarvis_db

echo [*] Backend Port: %BACKEND_PORT%
echo [*] Mode: %MODE%
echo [*] MongoDB: %MONGODB_URI%
echo.

REM ===== Create venv if missing =====
if not exist "%SCRIPT_DIR%venv" (
    echo [*] Creating Python virtual environment...
    python -m venv "%SCRIPT_DIR%venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
)
echo [OK] Virtual environment ready
echo.

REM ===== Determine Python executable =====
set PYTHON_EXEC="%SCRIPT_DIR%venv\Scripts\python.exe"
if not exist %PYTHON_EXEC% (
    echo [!] venv python not found, using system python
    set PYTHON_EXEC=python
)
echo [*] Using Python: %PYTHON_EXEC%
echo.

REM ===== Force venv paths to the front of PATH =====
set PATH=%SCRIPT_DIR%venv\Scripts;%PATH%

REM ===== Install/check dependencies =====
echo [*] Checking Python dependencies...
timeout /t 1 /nobreak >nul
%PYTHON_EXEC% -m pip install -q --upgrade pip 2>nul
echo [*] Installing/updating all requirements...
%PYTHON_EXEC% -m pip install -r requirements.txt --no-warn-script-location 2>nul
if errorlevel 1 (
    echo [WARNING] Some packages failed to install, but continuing...
)
%PYTHON_EXEC% -m pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo [ERROR] FastAPI not installed - installation failed
    pause
    exit /b 1
)
echo [OK] Python dependencies ready
echo.

REM ===== Install frontend dependencies if missing =====
if not exist "%SCRIPT_DIR%jarvis-frontend\node_modules" (
    echo [*] Installing frontend dependencies...
    cd /d "%SCRIPT_DIR%jarvis-frontend"
    call npm install --silent
    if errorlevel 1 (
        cd /d "%SCRIPT_DIR%"
        echo [ERROR] Failed to install frontend dependencies
        pause
        exit /b 1
    )
    cd /d "%SCRIPT_DIR%"
)
echo [OK] Frontend dependencies ready
echo.

REM ===== Check ports =====
netstat -ano | findstr ":%BACKEND_PORT%" >nul 2>&1
if not errorlevel 1 (
  echo [!] Port %BACKEND_PORT% is already in use. Another instance may be running.
)
netstat -ano | findstr ":%FRONTEND_PORT%" >nul 2>&1
if not errorlevel 1 (
  echo [!] Port %FRONTEND_PORT% is already in use. Another frontend may be running.
)
echo.

REM ===== Start backend =====
echo [*] Starting backend on port %BACKEND_PORT%...
if /I "%MODE%"=="production" (
  echo [*] Production mode: using uvicorn (no reload)
  start "JARVIS Backend" cmd /k "set PORT=%BACKEND_PORT% && %PYTHON_EXEC% -m uvicorn app:app --host 0.0.0.0 --port %BACKEND_PORT%"
) else (
  echo [*] Development mode: uvicorn with --reload
  start "JARVIS Backend" cmd /k "set PORT=%BACKEND_PORT% && %PYTHON_EXEC% -m uvicorn app:app --reload --host 0.0.0.0 --port %BACKEND_PORT%"
)

REM ===== Start frontend =====
echo [*] Starting frontend (React on port %FRONTEND_PORT%)...
start "JARVIS Frontend" cmd /k "set PORT=%FRONTEND_PORT% && cd /d %SCRIPT_DIR%jarvis-frontend && npm start"

echo.
echo ========================================
echo        JARVIS STARTED
echo ========================================
echo.
echo [OK] Backend:  http://localhost:%BACKEND_PORT%
echo [OK] Frontend: http://localhost:3000
echo.
echo Two windows will open: one for backend, one for frontend.
echo Press Ctrl+C in each window to stop them.
echo.
echo This launcher window will close in 5 seconds...
echo ========================================
echo.

timeout /t 5 /nobreak

endlocal
