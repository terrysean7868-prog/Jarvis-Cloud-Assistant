param(
  [string]$RepoPath = ""
)

# Stops the locally running Jarvis PC agent process on Windows.
# This is used by the jarvisagent://stop protocol handler and can be run manually.

$ErrorActionPreference = 'SilentlyContinue'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  try { $RepoPath = (Resolve-Path $RepoPath).Path } catch { }
}

$repoNorm = $RepoPath.TrimEnd('\\')

$matches = @()
try {
  $matches = Get-CimInstance Win32_Process |
    Where-Object {
      $_.CommandLine -and
      $_.CommandLine -like "*pc_agent.py*" -and
      $_.CommandLine -like "*$repoNorm*"
    }
} catch {
  $matches = @()
}

$killed = 0
foreach ($p in $matches) {
  try {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $killed++
  } catch {}
}

Write-Host "Stopped $killed agent process(es)." -ForegroundColor Yellow
exit 0
