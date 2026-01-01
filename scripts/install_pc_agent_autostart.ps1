param(
  [string]$RepoPath = "",
  [string]$TaskName = "JarvisPCAgent",
  [string]$ServerUrl = "",
  [string]$SharedSecret = "",
  [string]$DeviceId = "",
  [switch]$StartNow,
  [switch]$PromptForSecret
)

# Installs a per-user Scheduled Task that starts the PC agent automatically at login.
# This is the only reliable way to "auto-start" the agent when the backend is on Render.

$ErrorActionPreference = 'Stop'

if (-not $RepoPath) {
  $RepoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
  $RepoPath = (Resolve-Path $RepoPath).Path
}

$starter = Join-Path $RepoPath 'scripts\start_pc_agent_hidden.ps1'
if (-not (Test-Path $starter)) {
  throw "Agent starter not found: $starter"
}

$envFile = Join-Path $RepoPath '.env.agent'
if (-not (Test-Path $envFile)) {
  # Create a starter .env.agent so zero-click autostart is easy to finish.
  $defaultUrl = if ($ServerUrl) { $ServerUrl } else { "https://jarvis-cloud-assistant.onrender.com" }
  $defaultDevice = if ($DeviceId) { $DeviceId } else { $env:COMPUTERNAME }
  $secret = $SharedSecret

  @(
    "# Jarvis PC Agent environment",
    "# IMPORTANT: Keep this file private (contains a shared secret).",
    "JARVIS_SERVER_URL=$defaultUrl",
    "JARVIS_AGENT_SHARED_SECRET=$secret",
    "JARVIS_DEVICE_ID=$defaultDevice"
  ) | Set-Content -Path $envFile -Encoding UTF8
  Write-Host "Created $envFile" -ForegroundColor Yellow
} else {
  # Update values if user provided overrides.
  $lines = Get-Content -Path $envFile -ErrorAction SilentlyContinue
  if (-not $lines) { $lines = @() }

  function Upsert-Line([string]$key, [string]$value, [string[]]$content) {
    if (-not $value) { return $content }
    $pattern = "^\s*" + [Regex]::Escape($key) + "\s*="
    $newLine = "$key=$value"
    $found = $false
    $out = @()
    foreach ($l in $content) {
      if ($l -match $pattern) {
        $out += $newLine
        $found = $true
      } else {
        $out += $l
      }
    }
    if (-not $found) { $out += $newLine }
    return $out
  }

  $lines = Upsert-Line -key "JARVIS_SERVER_URL" -value $ServerUrl -content $lines
  $lines = Upsert-Line -key "JARVIS_AGENT_SHARED_SECRET" -value $SharedSecret -content $lines
  $lines = Upsert-Line -key "JARVIS_DEVICE_ID" -value $DeviceId -content $lines
  $lines | Set-Content -Path $envFile -Encoding UTF8
}

$hasSecret = $false
try {
  $envLines = Get-Content -Path $envFile -ErrorAction SilentlyContinue
  foreach ($l in ($envLines | Where-Object { $_ -ne $null })) {
    if ($l -match "^\s*JARVIS_AGENT_SHARED_SECRET\s*=\s*(.*)\s*$") {
      $val = ($Matches[1] | ForEach-Object { $_.Trim() })
      if ($val) { $hasSecret = $true }
      break
    }
  }
} catch {
  $hasSecret = $false
}

if (-not $hasSecret -and $PromptForSecret) {
  $entered = Read-Host "Enter JARVIS_AGENT_SHARED_SECRET (must match your backend env)"
  if ($entered) {
    $lines = Get-Content -Path $envFile -ErrorAction SilentlyContinue
    if (-not $lines) { $lines = @() }

    function Upsert-Line([string]$key, [string]$value, [string[]]$content) {
      if (-not $value) { return $content }
      $pattern = "^\s*" + [Regex]::Escape($key) + "\s*="
      $newLine = "$key=$value"
      $found = $false
      $out = @()
      foreach ($l in $content) {
        if ($l -match $pattern) {
          $out += $newLine
          $found = $true
        } else {
          $out += $l
        }
      }
      if (-not $found) { $out += $newLine }
      return $out
    }

    $lines = Upsert-Line -key "JARVIS_AGENT_SHARED_SECRET" -value $entered -content $lines
    $lines | Set-Content -Path $envFile -Encoding UTF8
    $hasSecret = $true
  }
}

$userId = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }

# Run hidden via PowerShell so no console window appears.
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$starter`" -RepoPath `"$RepoPath`" -Loop")
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
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

if (-not $hasSecret) {
  Write-Host "WARNING: JARVIS_AGENT_SHARED_SECRET is not set in $envFile. The agent will exit until you set it." -ForegroundColor Yellow
  Write-Host "Tip: Copy .env.agent.example to .env.agent and fill in the secret." -ForegroundColor Yellow
} elseif ($StartNow) {
  try {
    Start-ScheduledTask -TaskName $TaskName | Out-Null
    Write-Host "Started Scheduled Task '$TaskName'." -ForegroundColor Green
  } catch {
    Write-Host "Installed task, but could not start it automatically: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}
