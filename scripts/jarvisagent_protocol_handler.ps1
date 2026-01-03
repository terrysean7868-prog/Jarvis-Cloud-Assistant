param(
  [Parameter(Mandatory=$true)][string]$Url
)

$ErrorActionPreference = 'SilentlyContinue'

function Start-Agent {
  $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
  $starter = Join-Path $repo 'scripts\start_pc_agent_hidden.ps1'
  if (-not (Test-Path $starter)) {
    Write-Host "Agent starter not found: $starter"
    exit 1
  }

  # Autostart-at-login has been removed by request. Do NOT create or start Scheduled Tasks.
  # Start hidden, detached on demand.
  Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoProfile','-ExecutionPolicy','Bypass','-File', $starter,
    '-RepoPath', $repo, '-Loop'
  ) -WorkingDirectory $repo -WindowStyle Hidden | Out-Null
}

function Stop-Agent {
  $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
  $stopper = Join-Path $repo 'scripts\stop_pc_agent.ps1'
  if (-not (Test-Path $stopper)) {
    Write-Host "Stop script not found: $stopper"
    exit 1
  }

  # Run stopper in a separate PowerShell instance (detached)
  Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', $stopper, '-RepoPath', $repo) -WindowStyle Hidden | Out-Null
}

# Very simple routing: jarvisagent://start
if ($Url -match '^jarvisagent://start') {
  Start-Agent
  exit 0
}

if ($Url -match '^jarvisagent://stop') {
  Stop-Agent
  exit 0
}

Start-Agent
exit 0
