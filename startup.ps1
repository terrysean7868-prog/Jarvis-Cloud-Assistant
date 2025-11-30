# Jarvis Cloud Assistant - Simple Startup Script
# This script starts the backend (FastAPI) and frontend (React) for local development

param(
    [switch]$SkipFrontend,
    [switch]$SkipBackend
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "       JARVIS AI ASSISTANT" -ForegroundColor Cyan
Write-Host "   Just A Rather Very Intelligent System" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Load .env
if (Test-Path ".env") {
    Write-Host "[*] Loading environment from .env" -ForegroundColor Yellow
    Get-Content ".env" | ForEach-Object {
        if ($_ -and -not $_.StartsWith("#")) {
            $parts = $_ -split "=", 2
            if ($parts.Count -eq 2) {
                [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim())
            }
        }
    }
    Write-Host "[OK] .env loaded" -ForegroundColor Green
} else {
    Write-Host "[!] No .env file found. Using system environment." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[*] Python version:" -ForegroundColor Yellow
& "$ScriptDir\venv\Scripts\python.exe" --version

Write-Host ""
Write-Host "[*] Backend on port 8000" -ForegroundColor Yellow
Write-Host "[*] Frontend on port 3000" -ForegroundColor Yellow
Write-Host ""

# Start Backend
if (-not $SkipBackend) {
    Write-Host "[*] Starting Backend (FastAPI)..." -ForegroundColor Yellow
    $backendProcess = Start-Process `
        -FilePath "$ScriptDir\venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000" `
        -WorkingDirectory $ScriptDir `
        -PassThru `
        -NoNewWindow
    Write-Host "[OK] Backend process started (PID: $($backendProcess.Id))" -ForegroundColor Green
    Start-Sleep -Seconds 2
}

# Start Frontend
if (-not $SkipFrontend) {
    Write-Host "[*] Starting Frontend (React)..." -ForegroundColor Yellow
    $frontendProcess = Start-Process `
        -FilePath "cmd" `
        -ArgumentList "/c", "cd /d `"$ScriptDir\jarvis-frontend`" && npm start" `
        -PassThru `
        -NoNewWindow
    Write-Host "[OK] Frontend process started (PID: $($frontendProcess.Id))" -ForegroundColor Green
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "       JARVIS READY" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "[OK] Backend:  http://localhost:8000" -ForegroundColor Green
Write-Host "[OK] Frontend: http://localhost:3000" -ForegroundColor Green
Write-Host "[OK] Health:   http://localhost:8000/health" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop all services" -ForegroundColor Yellow
Write-Host ""

# Keep script running
while ($true) { Start-Sleep -Seconds 1 }
