$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location "$Root\console-web"
try {
  # The PRD-001 console shell has no npm dependencies.
  npm run dev
}
finally {
  Pop-Location
}
