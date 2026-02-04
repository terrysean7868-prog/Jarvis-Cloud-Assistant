param(
  [string]$TaskName = "JarvisPCAgent"
)

# NOTE:
# Autostart-at-login has been removed by request.
# This script is kept for backward compatibility but now DISABLES the task if it exists.

$ErrorActionPreference = 'Stop'

$uninstall = Join-Path $PSScriptRoot 'uninstall_pc_agent_autostart.ps1'
if (Test-Path $uninstall) {
  powershell -NoProfile -ExecutionPolicy Bypass -File $uninstall -TaskName $TaskName | Out-Host
}

Write-Host "PC agent autostart at Windows login is disabled." -ForegroundColor Yellow
Write-Host "Start the agent manually when needed (dist\JarvisPCAgent.exe or python pc_agent.py)." -ForegroundColor Yellow
