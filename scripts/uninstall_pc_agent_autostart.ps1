param(
  [string]$TaskName = "JarvisPCAgent"
)

$ErrorActionPreference = 'Stop'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
  Write-Host "Removed Scheduled Task '$TaskName'." -ForegroundColor Yellow
} else {
  Write-Host "Scheduled Task '$TaskName' not found." -ForegroundColor Yellow
}
