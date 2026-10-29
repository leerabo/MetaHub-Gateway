$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }
$env:PYTHONPATH = "$Root\shared-sdk\python;$env:PYTHONPATH"

if ($env:METAHUB_CONFIRM_PROD_ROLLBACK -eq "1") {
  & $Python -m metahub_shared.db.cli rollback --confirm-prod
} else {
  & $Python -m metahub_shared.db.cli rollback
}
