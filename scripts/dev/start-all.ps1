$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
New-Item -ItemType Directory -Force -Path "$Root\.logs" | Out-Null

$occupied = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in @(8000, 8001, 8002, 5173) }
if ($occupied) {
  $ports = ($occupied.LocalPort | Sort-Object -Unique) -join ", "
  throw "MetaHub ports are already in use: $ports. Stop the existing MetaHub processes first."
}

# Start local data, control, public-status, and Console services in background windows.
$processes = @(
  Start-Process -WindowStyle Hidden -PassThru -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-gateway.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\gateway.log" -RedirectStandardError "$Root\.logs\gateway.err.log"
  Start-Process -WindowStyle Hidden -PassThru -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-control-api.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\control-api.log" -RedirectStandardError "$Root\.logs\control-api.err.log"
  Start-Process -WindowStyle Hidden -PassThru -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-status.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\status.log" -RedirectStandardError "$Root\.logs\status.err.log"
  Start-Process -WindowStyle Hidden -PassThru -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-console.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\console.log" -RedirectStandardError "$Root\.logs\console.err.log"
)
$processes.Id | Set-Content -LiteralPath "$Root\.logs\dev-processes.pid" -Encoding ascii

Write-Host "Gateway:     http://localhost:8000/health"
Write-Host "Control API: http://localhost:8001/health"
Write-Host "Status API:  http://localhost:8002/public/status"
Write-Host "Console:     http://localhost:5173"
Write-Host "Logs:        $Root\.logs"
