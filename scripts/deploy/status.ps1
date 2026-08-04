[CmdletBinding()]
param(
  [ValidateSet("local-sqlite", "compose-full")]
  [string]$Profile = "local-sqlite",
  [string]$EnvFile = "infra/compose/.env.compose",
  [string]$ProjectName = "metahub"
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/MetaHub.Deploy.psm1") -Force

$RequestId = New-MetaHubRequestId
$ResolvedEnvFile = Resolve-MetaHubPath $EnvFile
if (-not (Test-Path -LiteralPath $ResolvedEnvFile)) {
  Write-MetaHubCliError -Code "deployment.profile_invalid" -Message "Compose env file does not exist." -RequestId $RequestId -Suggestion "Run scripts/deploy/install.ps1 first."
  exit 1
}

$envValues = Read-MetaHubEnvFile -EnvFile $ResolvedEnvFile
Assert-MetaHubDockerAvailable -RequestId $RequestId
$composeArgs = Get-MetaHubComposeArgs -EnvFile $ResolvedEnvFile -ProjectName $ProjectName -Profile $Profile
$psOutput = (& docker @composeArgs ps --format json) -join "`n"
$services = @{
  gateway = "http://localhost:$(Get-MetaHubEnvValue $envValues "METAHUB_GATEWAY_PORT" "8000")/health/live"
  control_api = "http://localhost:$(Get-MetaHubEnvValue $envValues "METAHUB_CONTROL_PORT" "8001")/health/live"
  console = "http://localhost:$(Get-MetaHubEnvValue $envValues "METAHUB_CONSOLE_PORT" "5173")/"
}
Write-MetaHubCliOk -RequestId $RequestId -Payload @{ profile_id = $Profile; project_name = $ProjectName; services = $services; compose_ps = $psOutput }
