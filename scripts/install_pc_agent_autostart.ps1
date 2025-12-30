param(
  [string]$RepoPath = "",
  [string]$TaskName = "JarvisPCAgent"
)

# Installs a per-user Scheduled Task that starts the PC agent automatically at login.
# This is the only reliable way to "auto-start" the agent when the backend is on Render.

$ErrorActionPreference = 'Stop'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  $RepoPath = (Resolve-Path $RepoPath).Path
}

$bat = Join-Path $RepoPath 'run_pc_agent.bat'
if (-not (Test-Path $bat)) {
  throw "Agent launcher not found: $bat"
}

$userId = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }

# Run the .bat via cmd.exe for consistent quoting.
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ("/c `"`"$bat`" --loop --no-pause`"")
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType InteractiveToken -RunLevel LeastPrivilege
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

# Replace existing task if present.
try {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
  }
} catch {
  # ignore
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host "Installed Scheduled Task '$TaskName' for user '$userId'." -ForegroundColor Green
Write-Host "It will start the agent at login. You can run it now from Task Scheduler -> Run." -ForegroundColor Green
