param(
  [string]$RepoPath = "",
  [switch]$Loop
)

# Starts the Jarvis PC agent with no visible terminal window.
# - Uses pythonw.exe when available
# - Can optionally restart the agent in a loop

$ErrorActionPreference = 'SilentlyContinue'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  try { $RepoPath = (Resolve-Path $RepoPath).Path } catch { }
}

$agent = Join-Path $RepoPath 'pc_agent.py'
if (-not (Test-Path $agent)) {
  Write-Host "pc_agent.py not found at $agent"
  exit 1
}

# Prefer pythonw so there is no console window.
$pythonw = Join-Path $RepoPath '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = Join-Path $RepoPath 'venv\Scripts\pythonw.exe' }
if (-not (Test-Path $pythonw)) { $pythonw = Join-Path $env:WINDIR 'py.exe' }

$python = Join-Path $RepoPath '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = Join-Path $RepoPath 'venv\Scripts\python.exe' }
if (-not (Test-Path $python)) { $python = 'python.exe' }

function Start-AgentOnce {
  if (Test-Path $pythonw) {
    return Start-Process -FilePath $pythonw -ArgumentList @($agent) -WorkingDirectory $RepoPath -WindowStyle Hidden -PassThru
  }

  # Fallback: start python.exe hidden (still no visible window in most cases)
  return Start-Process -FilePath $python -ArgumentList @($agent) -WorkingDirectory $RepoPath -WindowStyle Hidden -PassThru
}

if (-not $Loop) {
  $p = Start-AgentOnce
  exit 0
}

while ($true) {
  try {
    $p = Start-AgentOnce
    if ($p -and $p.Id) {
      Wait-Process -Id $p.Id
    } else {
      Start-Sleep -Seconds 10
    }
  } catch {
    Start-Sleep -Seconds 5
  }
}
