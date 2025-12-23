param(
	[switch]$CloudMode,
	[int]$Port = 18001
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
	$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
}
if (-not (Test-Path $python)) {
	throw "python not found in .venv or venv under: $repoRoot"
}

# Local API
# - Default: local/dev mode (allows registering an admin user)
# - Use: scripts/start_local_cloud_api.ps1 -CloudMode
if ($CloudMode) {
	$env:JARVIS_CLOUD_MODE = 'true'
} elseif (-not $env:JARVIS_CLOUD_MODE) {
	$env:JARVIS_CLOUD_MODE = 'false'
}
if (-not $env:JARVIS_JWT_SECRET) { $env:JARVIS_JWT_SECRET = 'devsecret' }
if (-not $env:JARVIS_AGENT_SHARED_SECRET) { $env:JARVIS_AGENT_SHARED_SECRET = 'localsecret' }
if (-not $env:JARVIS_DEFAULT_DEVICE_ID) { $env:JARVIS_DEFAULT_DEVICE_ID = 'safe' }

Write-Host "Starting API on http://127.0.0.1:$Port (cloud_mode=$($env:JARVIS_CLOUD_MODE))" -ForegroundColor Cyan
Push-Location $repoRoot
& $python -m uvicorn app:app --host 127.0.0.1 --port $Port --log-level info
Pop-Location
