param(
  [switch]$AllUsers
)

$ErrorActionPreference = 'Stop'

$root = if ($AllUsers) { 'HKLM:\Software\Classes' } else { 'HKCU:\Software\Classes' }
$protoKey = Join-Path $root 'jarvisagent'

if (Test-Path $protoKey) {
  Remove-Item -Path $protoKey -Recurse -Force
  Write-Host "Removed jarvisagent:// protocol handler from $root"
} else {
  Write-Host "Protocol handler not found in $root"
}
