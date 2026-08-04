[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [ValidateSet("local-sqlite", "compose-full")]
  [string]$Profile = "local-sqlite",
  [string]$EnvFile = "infra/compose/.env.compose",
  [string]$ProjectName = "metahub",
  [switch]$SkipBuild,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ModulePath = Join-Path $PSScriptRoot "lib/MetaHub.Deploy.psm1"
Import-Module $ModulePath -Force

$RequestId = New-MetaHubRequestId
$ResolvedEnvFile = Resolve-MetaHubPath $EnvFile
$Root = Get-MetaHubRoot

try {
  if ($DryRun -or $WhatIfPreference) {
    # dry-run 只输出计划步骤和端口，不访问 Docker，也不会生成或打印任何 secret。
    $dryRunEnvFile = if (Test-Path -LiteralPath $ResolvedEnvFile) {
      $ResolvedEnvFile
    } else {
      Join-Path $Root "infra/compose/.env.compose.example"
    }
    $envValues = Read-MetaHubEnvFile -EnvFile $dryRunEnvFile
    Write-MetaHubCliOk -RequestId $RequestId -Payload @{
      mode = "dry_run"
      profile_id = $Profile
      env_file = $ResolvedEnvFile
      project_name = $ProjectName
      steps = @("check docker", "check ports", "build images", "compose up", "migrate", "seed", "smoke")
      ports = @{
        gateway = Get-MetaHubEnvValue $envValues "METAHUB_GATEWAY_PORT" "8000"
        control_api = Get-MetaHubEnvValue $envValues "METAHUB_CONTROL_PORT" "8001"
        console = Get-MetaHubEnvValue $envValues "METAHUB_CONSOLE_PORT" "5173"
      }
    }
    exit 0
  }

  $envResult = Initialize-MetaHubEnvFile -EnvFile $ResolvedEnvFile -RequestId $RequestId
  $envValues = Read-MetaHubEnvFile -EnvFile $ResolvedEnvFile

  Assert-MetaHubDockerAvailable -RequestId $RequestId
  Assert-MetaHubPortsAvailable -EnvValues $envValues -Profile $Profile -RequestId $RequestId
  New-Item -ItemType Directory -Force -Path (Join-Path $Root ".data/compose") | Out-Null
  New-Item -ItemType Directory -Force -Path (Join-Path $Root ".logs/deploy") | Out-Null

  $composeArgs = Get-MetaHubComposeArgs -EnvFile $ResolvedEnvFile -ProjectName $ProjectName -Profile $Profile
  $upArgs = @("up", "-d")
  if (-not $SkipBuild) {
    $upArgs += "--build"
  }
  Invoke-MetaHubDockerCompose -ComposeArgs $composeArgs -CommandArgs $upArgs -RequestId $RequestId -FailureCode "deployment.compose_failed"

  Invoke-MetaHubDockerCompose -ComposeArgs $composeArgs -CommandArgs @("exec", "-T", "control-api", "python", "-m", "metahub_shared.db.cli", "migrate") -RequestId $RequestId -FailureCode "deployment.init_failed"
  Invoke-MetaHubDockerCompose -ComposeArgs $composeArgs -CommandArgs @("exec", "-T", "control-api", "python", "-m", "metahub_shared.db.cli", "seed") -RequestId $RequestId -FailureCode "deployment.init_failed"

  $services = Invoke-MetaHubSmoke -EnvValues $envValues -RequestId $RequestId
  Write-MetaHubDeploymentEvent -Profile $Profile -RequestId $RequestId -Services $services

  $payload = @{
    profile_id = $Profile
    env_file = $ResolvedEnvFile
    project_name = $ProjectName
    services = $services
    env_created = [bool]$envResult.created
  }
  if ($envResult.created) {
    $payload["admin_email"] = Get-MetaHubEnvValue $envValues "METAHUB_SEED_ADMIN_EMAIL" "admin@metahub.local"
    $payload["admin_password"] = $envResult.admin_password
    $payload["notice"] = "Admin password is printed once; rotate it after first login."
  }
  Write-MetaHubCliOk -RequestId $RequestId -Payload $payload
}
catch {
  Write-MetaHubCliError `
    -Code "deployment.compose_failed" `
    -Message "Install failed before deployment completed." `
    -RequestId $RequestId `
    -Suggestion "Check Docker status, env file syntax and local filesystem permissions."
  exit 1
}
