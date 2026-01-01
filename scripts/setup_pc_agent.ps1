param(
  [string]$RepoPath = "",
  [string]$ServerUrl = "",
  [string]$SharedSecret = "",
  [string]$DeviceId = "",
  [switch]$AllUsers,
  [switch]$StartNow,
  [switch]$NonInteractive
)

# One-command setup for a new Windows PC.
# - Installs jarvisagent:// protocol handler (current user)
# - Installs Scheduled Task autostart at Windows login
# - Prompts for JARVIS_AGENT_SHARED_SECRET if missing
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_pc_agent.ps1 -StartNow
#
# Unattended (recommended for repeatable installs):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_pc_agent.ps1 \
#     -ServerUrl "https://<your-render-service>.onrender.com" \
#     -SharedSecret "<same-as-backend>" \
#     -DeviceId "<stable-device-id>" \
#     -StartNow -NonInteractive

$ErrorActionPreference = 'Stop'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  $RepoPath = (Resolve-Path $RepoPath).Path
}

$protocolInstaller = Join-Path $RepoPath 'scripts\install_jarvisagent_protocol.ps1'
$autostartInstaller = Join-Path $RepoPath 'scripts\install_pc_agent_autostart.ps1'

if (-not (Test-Path $protocolInstaller)) {
  throw "Protocol installer not found: $protocolInstaller"
}
if (-not (Test-Path $autostartInstaller)) {
  throw "Autostart installer not found: $autostartInstaller"
}

Write-Host "[1/2] Installing jarvisagent:// protocol handler (current user)…" -ForegroundColor Cyan
if ($AllUsers) {
  Write-Host "NOTE: -AllUsers requires admin privileges." -ForegroundColor Yellow
  powershell -NoProfile -ExecutionPolicy Bypass -File $protocolInstaller -RepoPath $RepoPath -AllUsers | Out-Host
} else {
  powershell -NoProfile -ExecutionPolicy Bypass -File $protocolInstaller -RepoPath $RepoPath | Out-Host
}

Write-Host "[2/2] Installing PC agent autostart Scheduled Task…" -ForegroundColor Cyan
if ($NonInteractive) {
  if (-not $SharedSecret) {
    throw "NonInteractive requires -SharedSecret"
  }
}

$commonArgs = @(
  '-NoProfile',
  '-ExecutionPolicy',
  'Bypass',
  '-File',
  $autostartInstaller,
  '-RepoPath',
  $RepoPath
)

if ($ServerUrl) { $commonArgs += @('-ServerUrl', $ServerUrl) }
if ($SharedSecret) { $commonArgs += @('-SharedSecret', $SharedSecret) }
if ($DeviceId) { $commonArgs += @('-DeviceId', $DeviceId) }

if ($StartNow) {
  if ($SharedSecret) {
    powershell @($commonArgs + @('-StartNow')) | Out-Host
  } else {
    powershell @($commonArgs + @('-PromptForSecret','-StartNow')) | Out-Host
  }
} else {
  if ($SharedSecret) {
    powershell @($commonArgs) | Out-Host
  } else {
    powershell @($commonArgs + @('-PromptForSecret')) | Out-Host
  }
}

Write-Host "Done. The agent will auto-start at Windows login." -ForegroundColor Green
