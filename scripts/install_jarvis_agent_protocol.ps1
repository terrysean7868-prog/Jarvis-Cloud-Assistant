Param(
  [string]$RepoPath = "",
  [string]$Protocol = "jarvis-agent"
)

# Registers a custom URL protocol so the browser UI can start the agent on-demand.
# After install, opening `jarvis-agent://start` will run run_pc_agent.bat.

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepoPath)) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot ".."))
} else {
  $RepoPath = (Resolve-Path $RepoPath)
}

$bat = Join-Path $RepoPath "run_pc_agent.bat"
if (-not (Test-Path $bat)) {
  throw "run_pc_agent.bat not found at $bat"
}

$baseKey = "HKCU:\Software\Classes\$Protocol"
New-Item -Path $baseKey -Force | Out-Null
New-ItemProperty -Path $baseKey -Name "(Default)" -Value "URL:Jarvis PC Agent" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $baseKey -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null

$iconKey = Join-Path $baseKey "DefaultIcon"
New-Item -Path $iconKey -Force | Out-Null
New-ItemProperty -Path $iconKey -Name "(Default)" -Value "$bat,0" -PropertyType String -Force | Out-Null

$cmdKey = Join-Path $baseKey "shell\open\command"
New-Item -Path $cmdKey -Force | Out-Null
# %1 is the URL; we ignore it for now and just start the agent.
New-ItemProperty -Path $cmdKey -Name "(Default)" -Value "\"$bat\"" -PropertyType String -Force | Out-Null

Write-Host "Installed protocol '$Protocol'. Test by running: start ${Protocol}://start" -ForegroundColor Green
