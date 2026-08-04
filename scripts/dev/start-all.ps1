$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
New-Item -ItemType Directory -Force -Path "$Root\.logs" | Out-Null

# Start all PRD-001 shells in background windows and write separate logs.
Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-gateway.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\gateway.log" -RedirectStandardError "$Root\.logs\gateway.err.log"
Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-control-api.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\control-api.log" -RedirectStandardError "$Root\.logs\control-api.err.log"
Start-Process -WindowStyle Hidden -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$Root\scripts\dev\start-console.ps1") -WorkingDirectory $Root -RedirectStandardOutput "$Root\.logs\console.log" -RedirectStandardError "$Root\.logs\console.err.log"

Write-Host "Gateway:     http://localhost:8000/health"
Write-Host "Control API: http://localhost:8001/health"
Write-Host "Console:     http://localhost:5173"
Write-Host "Logs:        $Root\.logs"
