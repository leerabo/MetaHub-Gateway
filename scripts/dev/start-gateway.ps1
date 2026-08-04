$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }

# PRD-001 uses src layout. Add shared runtime and gateway source before starting.
$env:PYTHONPATH = "$Root\shared-sdk\python;$Root\gateway-data-plane\src;$env:PYTHONPATH"
if ($env:METAHUB_DEBUG_STARTUP -eq "1") {
  Write-Host "Root=$Root"
  Write-Host "PYTHONPATH=$env:PYTHONPATH"
  Write-Host "Python=$Python"
}

& $Python -m metahub_gateway.main
