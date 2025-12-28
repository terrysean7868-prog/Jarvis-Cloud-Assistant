Param(
  [string]$Protocol = "jarvis-agent"
)

$ErrorActionPreference = "Stop"

$baseKey = "HKCU:\Software\Classes\$Protocol"
if (Test-Path $baseKey) {
  Remove-Item -Path $baseKey -Recurse -Force
  Write-Host "Removed protocol '$Protocol'." -ForegroundColor Yellow
} else {
  Write-Host "Protocol '$Protocol' is not installed." -ForegroundColor Yellow
}
