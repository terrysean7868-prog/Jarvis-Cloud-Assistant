@echo off
setlocal enabledelayedexpansion

REM Starts BOTH backend + frontend for local UI voice register/login.
REM Backend:  http://127.0.0.1:18001  (default)
REM Frontend: http://127.0.0.1:3000
REM Logs:     .\logs\api.log and .\logs\ui.log
REM
REM Usage:
REM   run_full_local.bat
REM   run_full_local.bat 19001

set "API_PORT=%~1"
if "%API_PORT%"=="" set "API_PORT=18001"

set "REPO=%~dp0"
cd /d "%REPO%"

if not exist "logs" mkdir "logs" >nul 2>&1

set "API_LOG=%CD%\logs\api.log"
set "API_ERR=%CD%\logs\api.err"
set "UI_LOG=%CD%\logs\ui.log"
set "UI_ERR=%CD%\logs\ui.err"

REM Pick python from .venv/venv if present
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=%CD%\venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo.
echo === Jarvis Local (UI + API) ===
echo API:      http://127.0.0.1:%API_PORT%
echo Frontend: http://127.0.0.1:3000
echo Logs:     %CD%\logs\api.log  and  %CD%\logs\ui.log
echo.

REM Ensure log files exist (helps troubleshooting)
if not exist "%API_LOG%" type nul > "%API_LOG%" 2>nul
if not exist "%API_ERR%" type nul > "%API_ERR%" 2>nul
if not exist "%UI_LOG%" type nul > "%UI_LOG%" 2>nul
if not exist "%UI_ERR%" type nul > "%UI_ERR%" 2>nul

REM Free backend port (best-effort)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop_local_api.ps1 -Port %API_PORT% >nul 2>&1

REM Free frontend port 3000 (best-effort)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-NetTCPConnection -LocalPort 3000 -ErrorAction SilentlyContinue | Where-Object { $_.State -eq 'Listen' } | Select-Object -First 1; if ($c) { try { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1

REM Start backend (background)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:AUTH_USE_DB='true'; $env:JARVIS_CLOUD_MODE='false'; Start-Process -FilePath '%PYTHON%' -WorkingDirectory '%CD%' -ArgumentList @('-m','uvicorn','app:app','--host','127.0.0.1','--port','%API_PORT%','--log-level','info') -RedirectStandardOutput '%API_LOG%' -RedirectStandardError '%API_ERR%' | Out-Null"

if not exist "jarvis-frontend\package.json" (
	echo ERROR: jarvis-frontend\package.json not found. Frontend cannot start.
) else (
	REM Start frontend (background) detached; capture logs
	powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'cmd.exe' -WorkingDirectory '%CD%\\jarvis-frontend' -ArgumentList @('/d','/s','/c','set REACT_APP_API_URL=http://127.0.0.1:%API_PORT%& set HOST=127.0.0.1& set PORT=3000& set BROWSER=none& set DANGEROUSLY_DISABLE_HOST_CHECK=true& set WDS_SOCKET_HOST=127.0.0.1& call npm.cmd start') -RedirectStandardOutput '%UI_LOG%' -RedirectStandardError '%UI_ERR%' | Out-Null"
)

REM Quick health checks
powershell -NoProfile -Command "for ($i=0; $i -lt 25; $i++) { try { $r = Invoke-RestMethod -Uri 'http://127.0.0.1:%API_PORT%/health' -TimeoutSec 2; Write-Host ('API OK: ' + ($r | ConvertTo-Json -Compress)); break } catch { Start-Sleep -Milliseconds 400 } }"
powershell -NoProfile -Command "$ok=$false; for ($i=0; $i -lt 60; $i++) { try { $w = Invoke-WebRequest -Uri 'http://127.0.0.1:3000' -UseBasicParsing -TimeoutSec 2; Write-Host ('UI OK: status=' + $w.StatusCode); $ok=$true; break } catch { Start-Sleep -Milliseconds 500 } }; if (-not $ok) { Write-Host ('UI not ready yet (check logs\\ui.log).') }"

echo.
echo Started.
echo - Open UI: http://127.0.0.1:3000
echo - API health: http://127.0.0.1:%API_PORT%/health
echo - Logs: %API_LOG% / %API_ERR%
echo - Logs: %UI_LOG% / %UI_ERR%
echo.
