param(
  [Parameter(Mandatory=$true)][string]$Url
)

$ErrorActionPreference = 'SilentlyContinue'

function Start-Agent {
  $repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
  $bat = Join-Path $repo 'run_pc_agent.bat'
  if (-not (Test-Path $bat)) {
    Write-Host "Agent launcher not found: $bat"
    exit 1
  }

  # Start minimized, detached
  Start-Process -FilePath $bat -WorkingDirectory $repo -WindowStyle Minimized | Out-Null
}

# Very simple routing: jarvisagent://start
if ($Url -match '^jarvisagent://start') {
  Start-Agent
  exit 0
}

Start-Agent
exit 0
