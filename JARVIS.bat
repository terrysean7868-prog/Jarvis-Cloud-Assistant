@echo off
title JARVIS - AI Assistant
color 0B
echo.
echo ========================================
echo        JARVIS AI ASSISTANT
echo    Just A Rather Very Intelligent System
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH
    echo Please install Python 3.8+ from https://www.python.org
    pause
    exit /b 1
)

REM Check if Node.js is installed
node --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js is not installed or not in PATH
    echo Please install Node.js from https://nodejs.org
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist "venv\" (
    echo [1/5] Creating Python virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created
    echo.
)

REM Activate virtual environment
echo [2/5] Activating virtual environment...
call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment
    pause
    exit /b 1
)
echo [OK] Virtual environment activated
echo.

REM Check and install Python dependencies
echo [3/5] Checking Python dependencies...
set DEPS_OK=1

pip show fastapi >nul 2>&1
if errorlevel 1 set DEPS_OK=0

pip show uvicorn >nul 2>&1
if errorlevel 1 set DEPS_OK=0

pip show openai >nul 2>&1
if errorlevel 1 set DEPS_OK=0

pip show pydantic >nul 2>&1
if errorlevel 1 set DEPS_OK=0

if %DEPS_OK%==0 (
    echo Installing Python dependencies...
    python -m pip install --upgrade pip --quiet
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install Python dependencies
        echo Please check your internet connection and try again
        pause
        exit /b 1
    )
    echo [OK] Python dependencies installed
) else (
    echo [OK] Python dependencies are up to date
)
echo.

REM Check .env file
if not exist ".env" (
    echo [WARNING] .env file not found!
    echo Creating .env file template...
    (
        echo OPENAI_API_KEY=your_openai_api_key_here
        echo AUTO_APPLY=true
        echo LLM_PROVIDER=auto
    ) > .env
    echo.
    echo [IMPORTANT] Please edit .env file and add your OPENAI_API_KEY
    echo Press any key to open .env file in notepad...
    pause >nul
    notepad .env
    echo.
)

REM Check and install frontend dependencies
echo [4/5] Checking frontend dependencies...
if not exist "jarvis-frontend\node_modules\" (
    echo Installing frontend dependencies...
    cd jarvis-frontend
    call npm install --silent
    if errorlevel 1 (
        echo [ERROR] Failed to install frontend dependencies
        cd ..
        pause
        exit /b 1
    )
    cd ..
    echo [OK] Frontend dependencies installed
) else (
    echo [OK] Frontend dependencies are up to date
)
echo.

REM Check ports
echo [5/5] Checking ports...
netstat -ano | findstr ":8000" >nul 2>&1
if not errorlevel 1 (
    echo [WARNING] Port 8000 is already in use
    echo Backend might already be running
)
netstat -ano | findstr ":3000" >nul 2>&1
if not errorlevel 1 (
    echo [WARNING] Port 3000 is already in use
    echo Frontend might already be running
)
echo.

REM Start JARVIS
echo ========================================
echo        STARTING JARVIS...
echo ========================================
echo.
echo Backend:  http://localhost:8000
echo Frontend: http://localhost:3000
echo.
echo Press Ctrl+C to stop JARVIS
echo.
echo ========================================
echo.

python run_jarvis.py

echo.
echo ========================================
echo        JARVIS STOPPED
echo ========================================
pause

