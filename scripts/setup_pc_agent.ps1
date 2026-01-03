param(
  [string]$RepoPath = "",
  [switch]$AllUsers,
  [switch]$StartNow
)

# One-command setup for a new Windows PC.
# - Installs jarvisagent:// protocol handler (current user)
# - Does NOT install any Windows-login autostart (user requested manual start)
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_pc_agent.ps1 -StartNow
#
# Non-interactive install (no autostart):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_pc_agent.ps1

$ErrorActionPreference = 'Stop'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  $RepoPath = (Resolve-Path $RepoPath).Path
}

$protocolInstaller = Join-Path $RepoPath 'scripts\install_jarvisagent_protocol.ps1'

$starter = Join-Path $RepoPath 'scripts\start_pc_agent_hidden.ps1'

if (-not (Test-Path $protocolInstaller)) {
  throw "Protocol installer not found: $protocolInstaller"
}

Write-Host "[1/2] Installing jarvisagent:// protocol handler (current user)…" -ForegroundColor Cyan
if ($AllUsers) {
  Write-Host "NOTE: -AllUsers requires admin privileges." -ForegroundColor Yellow
  powershell -NoProfile -ExecutionPolicy Bypass -File $protocolInstaller -RepoPath $RepoPath -AllUsers | Out-Host
} else {
  powershell -NoProfile -ExecutionPolicy Bypass -File $protocolInstaller -RepoPath $RepoPath | Out-Host
}

if ($StartNow) {
  if (-not (Test-Path $starter)) {
    throw "Agent starter not found: $starter"
  }

  Write-Host "[2/2] Starting PC agent now (no login autostart)…" -ForegroundColor Cyan
  Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File', $starter,
    '-RepoPath', $RepoPath, '-Loop'
  ) -WorkingDirectory $RepoPath -WindowStyle Hidden | Out-Null

  Write-Host "Done. The agent is running (started on demand)." -ForegroundColor Green
} else {
  Write-Host "Done. No Windows-login autostart is installed." -ForegroundColor Green
  Write-Host "Start the agent manually with run_pc_agent.bat when needed." -ForegroundColor Green
}
