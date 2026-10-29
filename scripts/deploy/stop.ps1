[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [ValidateSet("local-sqlite", "compose-full")]
  [string]$Profile = "local-sqlite",
  [string]$EnvFile = "infra/compose/.env.compose",
  [string]$ProjectName = "metahub",
  [switch]$RemoveVolumes,
  [switch]$ConfirmRemoveVolumes,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/MetaHub.Deploy.psm1") -Force

$RequestId = New-MetaHubRequestId
$ResolvedEnvFile = Resolve-MetaHubPath $EnvFile

if ($RemoveVolumes -and -not $ConfirmRemoveVolumes) {
  Write-MetaHubCliError -Code "deployment.profile_invalid" -Message "Removing volumes requires -ConfirmRemoveVolumes." -RequestId $RequestId -Suggestion "Run without -RemoveVolumes to keep data, or pass both flags intentionally."
  exit 1
}

if ($DryRun -or $WhatIfPreference) {
  # stop dry-run 明确是否保留数据卷，防止误以为默认会删除数据。
  Write-MetaHubCliOk -RequestId $RequestId -Payload @{ mode = "dry_run"; profile_id = $Profile; remove_volumes = [bool]$RemoveVolumes; env_file = $ResolvedEnvFile }
  exit 0
}

Assert-MetaHubDockerAvailable -RequestId $RequestId
$composeArgs = Get-MetaHubComposeArgs -EnvFile $ResolvedEnvFile -ProjectName $ProjectName -Profile $Profile
$downArgs = @("down")
if ($RemoveVolumes) {
  $downArgs += "-v"
}
Invoke-MetaHubDockerCompose -ComposeArgs $composeArgs -CommandArgs $downArgs -RequestId $RequestId -FailureCode "deployment.compose_failed"
Write-MetaHubCliOk -RequestId $RequestId -Payload @{ profile_id = $Profile; project_name = $ProjectName; volumes_preserved = -not [bool]$RemoveVolumes }
