[CmdletBinding()]
param(
  [string]$EnvFile = "infra/compose/.env.compose",
  [int]$Retries = 30,
  [int]$DelaySeconds = 2
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
$services = Invoke-MetaHubSmoke -EnvValues $envValues -RequestId $RequestId -Retries $Retries -DelaySeconds $DelaySeconds

# 登录 smoke 只发送默认管理员凭据，不把密码写入输出；失败则统一归类为健康失败。
$controlBase = "http://localhost:$(Get-MetaHubEnvValue $envValues "METAHUB_CONTROL_PORT" "8001")"
$adminEmail = Get-MetaHubEnvValue $envValues "METAHUB_SEED_ADMIN_EMAIL" "admin@metahub.local"
$adminPassword = Get-MetaHubEnvValue $envValues "METAHUB_SEED_ADMIN_PASSWORD" ""
if ($adminPassword) {
  try {
    $body = @{ email = $adminEmail; password = $adminPassword; tenant_slug = "default" } | ConvertTo-Json -Compress
    $login = Invoke-WebRequest -Uri "$controlBase/auth/login" -Method Post -ContentType "application/json" -Body $body -UseBasicParsing -TimeoutSec 5
    if ($login.StatusCode -lt 200 -or $login.StatusCode -ge 300) {
      throw "login failed"
    }
  }
  catch {
    Write-MetaHubCliError -Code "deployment.health_failed" -Message "Default admin login smoke failed." -RequestId $RequestId -Suggestion "Run db seed again and verify METAHUB_SEED_ADMIN_PASSWORD in the local env file."
    exit 1
  }
}

Write-MetaHubCliOk -RequestId $RequestId -Payload @{ services = $services; admin_login_checked = [bool]$adminPassword }
