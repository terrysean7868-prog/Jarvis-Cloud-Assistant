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

  # If the agent isn't already running, ensure autostart task exists and start it.
  # This keeps the UI flow simple: Jarvis calls jarvisagent://start and the PC self-heals.
  $taskName = "JarvisPCAgent"
  $hasTask = $false
  try {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t) { $hasTask = $true }
  } catch {
    $hasTask = $false
  }

  if (-not $hasTask) {
    $installer = Join-Path $repo 'scripts\install_pc_agent_autostart.ps1'
    if (Test-Path $installer) {
      try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList @(
          '-NoProfile','-ExecutionPolicy','Bypass','-File', $installer,
          '-RepoPath', $repo,
          '-TaskName', $taskName,
          '-StartNow'
        ) -WorkingDirectory $repo -WindowStyle Hidden -Wait | Out-Null
      } catch {
        # ignore
      }
    }
  }

  # Try starting the Scheduled Task (preferred). If it doesn't exist/failed, fall back
  # to starting the hidden loop directly.
  try {
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
    return
  } catch {
    # ignore
  }

  # Start hidden, detached
  Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', $starter, '-RepoPath', $repo, '-Loop') -WorkingDirectory $repo -WindowStyle Hidden | Out-Null
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
