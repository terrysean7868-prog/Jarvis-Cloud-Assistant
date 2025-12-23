param(
	[int]$Port = 18001
)

$ErrorActionPreference = 'Stop'

$conns = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue)
if ($conns.Count -eq 0) {
	Write-Host "No process is listening on port $Port" -ForegroundColor Yellow
	exit 0
}

$pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique | Where-Object { $_ -and $_ -ne 0 }
foreach ($p in $pids) {
	try {
		$proc = Get-Process -Id $p -ErrorAction Stop
		Write-Host "Stopping PID $($proc.Id) ($($proc.ProcessName)) on port $Port" -ForegroundColor Cyan
		Stop-Process -Id $proc.Id -Force
	} catch {
		Write-Host "Could not stop PID ${p}: $($_.Exception.Message)" -ForegroundColor Red
	}
}

Start-Sleep -Milliseconds 300
if (@(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue).Count -eq 0) {
	Write-Host "Stopped. Port $Port is now free." -ForegroundColor Green
} else {
	Write-Host "Some listeners remain on port $Port." -ForegroundColor Yellow
}
