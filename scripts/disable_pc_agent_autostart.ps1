param(
  [string]$TaskName = "JarvisPCAgent"
)

$ErrorActionPreference = 'Stop'

# Convenience wrapper: disable/remove the Windows login autostart task.
$uninstall = Join-Path $PSScriptRoot 'uninstall_pc_agent_autostart.ps1'
if (-not (Test-Path $uninstall)) {
  throw "Uninstall script not found: $uninstall"
}

powershell -NoProfile -ExecutionPolicy Bypass -File $uninstall -TaskName $TaskName | Out-Host
Write-Host "PC agent will NOT auto-start at Windows login." -ForegroundColor Green
Write-Host "Start it manually with dist\JarvisPCAgent.exe (or python pc_agent.py) when needed." -ForegroundColor Green
