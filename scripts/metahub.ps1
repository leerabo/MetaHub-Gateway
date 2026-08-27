$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BundledPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if ($env:METAHUB_PYTHON) {
    $env:METAHUB_PYTHON
} elseif (Test-Path -LiteralPath $BundledPython -PathType Leaf) {
    $BundledPython
} else {
    "python"
}

$SourceRoot = Join-Path $Root "shared-sdk\python"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$SourceRoot;$env:PYTHONPATH" } else { $SourceRoot }

& $Python -m metahub_shared.installer.cli @args
exit $LASTEXITCODE
