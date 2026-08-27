param(
    [string]$Version = "1.0.0",
    [string]$OutputRoot = ""
)

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
$ReleaseRoot = Join-Path $Root "infra\installers\v$Version"
$TrustStore = Join-Path $Root "infra\installers\trusted-keys.json"
$MediaRoot = if ($OutputRoot) { $OutputRoot } else { Join-Path $Root "artifacts\installers\v$Version" }

& $Python -m metahub_shared.installer.release_builder `
    --release-root $ReleaseRoot `
    --trust-store $TrustStore `
    --output-root $MediaRoot
exit $LASTEXITCODE
