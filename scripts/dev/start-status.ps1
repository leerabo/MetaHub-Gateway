$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WorkspacePython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if ($env:METAHUB_PYTHON) {
  $env:METAHUB_PYTHON
} elseif (Test-Path -LiteralPath $WorkspacePython) {
  $WorkspacePython
} else {
  "python"
}

# The public status reader signs only local status projections. It does not
# change Gateway KMS configuration or carry Console credentials.
& $Python "$Root\scripts\dev\run-status.py"
