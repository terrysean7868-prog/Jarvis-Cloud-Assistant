param(
  [string]$RepoPath = "",
  [switch]$AllUsers
)

$ErrorActionPreference = 'Stop'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

$handler = Join-Path $RepoPath 'scripts\jarvisagent_protocol_handler.ps1'
if (-not (Test-Path $handler)) {
  throw "Handler not found: $handler"
}

$root = if ($AllUsers) { 'HKLM:\Software\Classes' } else { 'HKCU:\Software\Classes' }
$protoKey = Join-Path $root 'jarvisagent'

New-Item -Path $protoKey -Force | Out-Null
New-ItemProperty -Path $protoKey -Name '(Default)' -Value 'URL:Jarvis PC Agent' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $protoKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null

$iconKey = Join-Path $protoKey 'DefaultIcon'
New-Item -Path $iconKey -Force | Out-Null
New-ItemProperty -Path $iconKey -Name '(Default)' -Value "$RepoPath\jarvis-frontend\public\favicon.ico" -PropertyType String -Force | Out-Null

$cmdKey = Join-Path $protoKey 'shell\open\command'
New-Item -Path $cmdKey -Force | Out-Null

# Use powershell to run handler with the URL argument (%1) (hidden window)
$cmd = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$handler`" `"%1`""
New-ItemProperty -Path $cmdKey -Name '(Default)' -Value $cmd -PropertyType String -Force | Out-Null

Write-Host "Installed jarvisagent:// protocol handler in $root"
Write-Host "Test by running: Start-Process 'jarvisagent://start'"
