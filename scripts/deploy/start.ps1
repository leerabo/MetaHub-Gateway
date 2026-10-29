[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [ValidateSet("local-sqlite", "compose-full")]
  [string]$Profile = "local-sqlite",
  [string]$EnvFile = "infra/compose/.env.compose",
  [string]$ProjectName = "metahub",
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/MetaHub.Deploy.psm1") -Force

$RequestId = New-MetaHubRequestId
$ResolvedEnvFile = Resolve-MetaHubPath $EnvFile

if (-not (Test-Path -LiteralPath $ResolvedEnvFile)) {
  Write-MetaHubCliError -Code "deployment.profile_invalid" -Message "Compose env file does not exist." -RequestId $RequestId -Suggestion "Run scripts/deploy/install.ps1 first."
  exit 1
}

$envValues = Assert-MetaHubEnvFile -EnvFile $ResolvedEnvFile -RequestId $RequestId
if ($DryRun -or $WhatIfPreference) {
  # start dry-run 只说明将要启动的 profile，不触碰现有 volume。
  Write-MetaHubCliOk -RequestId $RequestId -Payload @{ mode = "dry_run"; profile_id = $Profile; project_name = $ProjectName; env_file = $ResolvedEnvFile }
  exit 0
}

Assert-MetaHubDockerAvailable -RequestId $RequestId
$composeArgs = Get-MetaHubComposeArgs -EnvFile $ResolvedEnvFile -ProjectName $ProjectName -Profile $Profile
Invoke-MetaHubDockerCompose -ComposeArgs $composeArgs -CommandArgs @("up", "-d") -RequestId $RequestId -FailureCode "deployment.compose_failed"
$services = Invoke-MetaHubSmoke -EnvValues $envValues -RequestId $RequestId
Write-MetaHubCliOk -RequestId $RequestId -Payload @{ profile_id = $Profile; project_name = $ProjectName; services = $services }
