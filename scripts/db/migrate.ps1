$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }
$env:PYTHONPATH = "$Root\shared-sdk\python;$env:PYTHONPATH"

& $Python -m metahub_shared.db.cli migrate
