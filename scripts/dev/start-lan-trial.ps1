[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$ConsoleOrigin
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

try {
  $origin = [uri]$ConsoleOrigin
} catch {
  throw "ConsoleOrigin must be an absolute http or https origin, for example http://172.16.100.2:5173."
}

if (
  -not $origin.IsAbsoluteUri -or
  $origin.Scheme -notin @("http", "https") -or
  -not $origin.Host -or
  $origin.UserInfo -or
  $origin.AbsolutePath -ne "/" -or
  $origin.Query -or
  $origin.Fragment
) {
  throw "ConsoleOrigin must contain only scheme, host, and optional port."
}

# CORS is an exact browser-origin contract. This launcher adds one explicit
# trial origin and preserves the loopback origins used by local operators.
$canonicalOrigin = $origin.GetLeftPart([System.UriPartial]::Authority)
$env:METAHUB_ALLOWED_ORIGINS = @(
  "http://localhost:5173",
  "http://127.0.0.1:5173",
  $canonicalOrigin
) -join ","

Write-Host "Starting MetaHub LAN trial for Console origin: $canonicalOrigin"
& "$Root\scripts\dev\start-all.ps1"
