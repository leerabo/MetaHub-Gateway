$ErrorActionPreference = "Stop"

# 生成部署链路 request_id，所有 CLI 输出和事件都使用同一个值串联。
function New-MetaHubRequestId {
  return "deploy_$([Guid]::NewGuid().ToString("N"))"
}

# 解析仓库根目录，保证脚本从任意目录执行时路径一致。
function Get-MetaHubRoot {
  return (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}

# 将用户传入路径归一化为绝对路径，避免写错 env 或日志目录。
function Resolve-MetaHubPath {
  param([Parameter(Mandatory = $true)][string]$Path)

  if ([System.IO.Path]::IsPathRooted($Path)) {
    return $Path
  }
  return (Join-Path (Get-MetaHubRoot) $Path)
}

# 只解析 KEY=VALUE，不执行 env 文件里的任何表达式。
function Read-MetaHubEnvFile {
  param([Parameter(Mandatory = $true)][string]$EnvFile)

  $values = @{}
  if (-not (Test-Path -LiteralPath $EnvFile)) {
    return $values
  }
  foreach ($rawLine in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    $line = $rawLine.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
      continue
    }
    $parts = $line.Split("=", 2)
    $values[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'")
  }
  return $values
}

# 从 env 读取配置值，缺失时使用部署模板默认值。
function Get-MetaHubEnvValue {
  param(
    [Parameter(Mandatory = $true)][hashtable]$Values,
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)][string]$Default
  )

  if ($Values.ContainsKey($Key) -and $Values[$Key]) {
    return [string]$Values[$Key]
  }
  return $Default
}

# 已生成的部署 env 必须完整替换公开占位符，并保持各密钥域互不复用。
function Assert-MetaHubEnvFile {
  param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$RequestId
  )

  $values = Read-MetaHubEnvFile -EnvFile $EnvFile
  $requiredSecrets = @(
    "METAHUB_INTERNAL_CONFIG_TOKEN",
    "METAHUB_INTERNAL_CHAT_TOKEN",
    "METAHUB_INTERNAL_BATCH_TOKEN",
    "METAHUB_AUTH_TOKEN_SECRET",
    "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
    "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
    "METAHUB_FILE_ENCRYPTION_SECRET",
    "METAHUB_BATCH_ENCRYPTION_SECRET",
    "METAHUB_MCP_ENCRYPTION_SECRET",
    "METAHUB_MCP_HMAC_SECRET",
    "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN",
    "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET",
    "METAHUB_MCP_SANDBOX_PERMIT_SECRET",
    "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT",
    "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
    "METAHUB_SESSION_RISK_HMAC_SECRET",
    "METAHUB_APPROVAL_HMAC_SECRET",
    "METAHUB_SEED_ADMIN_PASSWORD",
    "METAHUB_SEED_PROVIDER_API_KEY",
    "METAHUB_SEED_VIRTUAL_KEY",
    "METAHUB_POSTGRES_PASSWORD",
    "METAHUB_MINIO_ACCESS_KEY",
    "METAHUB_MINIO_SECRET_KEY"
  )
  $invalid = $false
  foreach ($entry in $values.GetEnumerator()) {
    if ([string]$entry.Value -like "*replace-with-generated-*") {
      $invalid = $true
    }
  }
  $seenSecrets = @{}
  foreach ($key in $requiredSecrets) {
    if (-not $values.ContainsKey($key)) {
      $invalid = $true
      continue
    }
    $value = [string]$values[$key]
    if ([string]::IsNullOrWhiteSpace($value) -or $value.Length -lt 16) {
      $invalid = $true
      continue
    }
    if ($seenSecrets.ContainsKey($value)) {
      $invalid = $true
      continue
    }
    $seenSecrets[$value] = $true
  }
  foreach ($key in @(
    "METAHUB_INTERNAL_CONFIG_TOKEN",
    "METAHUB_INTERNAL_CHAT_TOKEN",
    "METAHUB_INTERNAL_BATCH_TOKEN",
    "METAHUB_AUTH_TOKEN_SECRET",
    "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
    "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
    "METAHUB_FILE_ENCRYPTION_SECRET",
    "METAHUB_BATCH_ENCRYPTION_SECRET",
    "METAHUB_MCP_ENCRYPTION_SECRET",
    "METAHUB_MCP_HMAC_SECRET",
    "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN",
    "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET",
    "METAHUB_MCP_SANDBOX_PERMIT_SECRET",
    "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT",
    "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
    "METAHUB_SESSION_RISK_HMAC_SECRET",
    "METAHUB_APPROVAL_HMAC_SECRET"
  )) {
    if ($values.ContainsKey($key) -and ([string]$values[$key]).Length -lt 32) {
      $invalid = $true
    }
  }
  if ($invalid) {
    Write-MetaHubCliError `
      -Code "deployment.profile_invalid" `
      -Message "Compose env secrets are missing, public placeholders, weak, or reused." `
      -RequestId $RequestId `
      -Suggestion "Remove the invalid env file and rerun scripts/deploy/install.ps1."
    exit 1
  }
  return $values
}

# 输出统一 CLI 错误结构，并始终包含 request_id。
function Write-MetaHubCliError {
  param(
    [Parameter(Mandatory = $true)][string]$Code,
    [Parameter(Mandatory = $true)][string]$Message,
    [Parameter(Mandatory = $true)][string]$RequestId,
    [Parameter(Mandatory = $true)][string]$Suggestion
  )

  $payload = [ordered]@{
    status = "error"
    code = $Code
    message = $Message
    request_id = $RequestId
    suggestion = $Suggestion
  }
  [Console]::Error.WriteLine(($payload | ConvertTo-Json -Compress))
}

# 输出统一 CLI 成功结构，供部署流水线解析。
function Write-MetaHubCliOk {
  param(
    [Parameter(Mandatory = $true)][string]$RequestId,
    [Parameter(Mandatory = $true)][hashtable]$Payload
  )

  $Payload["status"] = "ok"
  $Payload["request_id"] = $RequestId
  Write-Output ($Payload | ConvertTo-Json -Depth 8)
}

# 检查 Docker 和 Compose 插件是否可用。
function Assert-MetaHubDockerAvailable {
  param([Parameter(Mandatory = $true)][string]$RequestId)

  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-MetaHubCliError `
      -Code "deployment.docker_missing" `
      -Message "Docker CLI is not installed or not available in PATH." `
      -RequestId $RequestId `
      -Suggestion "Install Docker Desktop or Docker Engine, then retry."
    exit 1
  }
  & docker compose version *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-MetaHubCliError `
      -Code "deployment.docker_missing" `
      -Message "Docker Compose plugin is not available." `
      -RequestId $RequestId `
      -Suggestion "Install a recent Docker Compose v2 plugin, then retry."
    exit 1
  }
}

# 通过本地绑定检测端口是否可用，不输出占用进程详情。
function Test-MetaHubPortAvailable {
  param([Parameter(Mandatory = $true)][int]$Port)

  $listener = $null
  try {
    $address = [System.Net.IPAddress]::Parse("127.0.0.1")
    $listener = [System.Net.Sockets.TcpListener]::new($address, $Port)
    $listener.Start()
    return $true
  }
  catch {
    return $false
  }
  finally {
    if ($listener) {
      $listener.Stop()
    }
  }
}

# 在 compose up 前检查端口冲突，失败时给出服务名和端口。
function Assert-MetaHubPortsAvailable {
  param(
    [Parameter(Mandatory = $true)][hashtable]$EnvValues,
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][string]$RequestId
  )

  $ports = @(
    @{ service = "gateway"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_GATEWAY_PORT" "8000") },
    @{ service = "control-api"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_CONTROL_PORT" "8001") },
    @{ service = "console"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_CONSOLE_PORT" "5173") }
  )
  if ($Profile -eq "compose-full") {
    $ports += @(
      @{ service = "redis"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_REDIS_PORT" "6379") },
      @{ service = "postgres"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_POSTGRES_PORT" "5432") },
      @{ service = "minio"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_MINIO_PORT" "9000") },
      @{ service = "minio-console"; port = [int](Get-MetaHubEnvValue $EnvValues "METAHUB_MINIO_CONSOLE_PORT" "9001") }
    )
  }
  foreach ($item in $ports) {
    if (-not (Test-MetaHubPortAvailable -Port $item.port)) {
      Write-MetaHubCliError `
        -Code "deployment.port_conflict" `
        -Message "Port $($item.port) for $($item.service) is already in use." `
        -RequestId $RequestId `
        -Suggestion "Change infra/compose/.env.compose or release the occupied port."
      exit 1
    }
  }
}

# 生成本地部署 secret；返回值只写入 .env.compose。
function New-MetaHubLocalSecret {
  param([int]$Length = 32)

  $bytes = New-Object byte[] $Length
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  }
  finally {
    $rng.Dispose()
  }
  return [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
}

# 生成中的 env 只允许当前服务账号读取；权限设置失败时不得发布目标文件。
function Protect-MetaHubEnvFile {
  param([Parameter(Mandatory = $true)][string]$EnvFile)

  if ($env:OS -eq "Windows_NT") {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetOwner($identity)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
      $identity,
      [System.Security.AccessControl.FileSystemRights]::FullControl,
      [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    [System.IO.File]::SetAccessControl($EnvFile, $acl)
    return
  }
  & chmod 600 -- $EnvFile
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to protect compose env file."
  }
}

# 先收紧空的临时目录，使 secret 文件从创建起就不暴露在继承权限下。
function Protect-MetaHubPrivateDirectory {
  param([Parameter(Mandatory = $true)][string]$Directory)

  if ($env:OS -eq "Windows_NT") {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetOwner($identity)
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
      $identity,
      [System.Security.AccessControl.FileSystemRights]::FullControl,
      $inheritance,
      [System.Security.AccessControl.PropagationFlags]::None,
      [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
    [System.IO.Directory]::SetAccessControl($Directory, $acl)
    return
  }
  & chmod 700 -- $Directory
  if ($LASTEXITCODE -ne 0) {
    throw "Unable to protect compose env staging directory."
  }
}

# 首次安装时从 example 生成本地 env，并替换所有 secret 占位符。
function Initialize-MetaHubEnvFile {
  param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$RequestId
  )

  if (Test-Path -LiteralPath $EnvFile) {
    $null = Assert-MetaHubEnvFile -EnvFile $EnvFile -RequestId $RequestId
    return @{ created = $false; admin_password = $null }
  }
  $root = Get-MetaHubRoot
  $example = Join-Path $root "infra/compose/.env.compose.example"
  if (-not (Test-Path -LiteralPath $example)) {
    Write-MetaHubCliError `
      -Code "deployment.profile_invalid" `
      -Message "Missing infra/compose/.env.compose.example." `
      -RequestId $RequestId `
      -Suggestion "Restore the compose env template and retry install."
    exit 1
  }

  $targetDir = Split-Path -Parent $EnvFile
  New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
  $content = Get-Content -LiteralPath $example -Raw -Encoding UTF8
  $adminPassword = "MetaHub-" + (New-MetaHubLocalSecret -Length 18) + "!1"
  $replacements = @{
    "replace-with-generated-internal-config-token" = (New-MetaHubLocalSecret)
    "replace-with-generated-internal-chat-token" = (New-MetaHubLocalSecret)
    "replace-with-generated-internal-batch-token" = (New-MetaHubLocalSecret)
    "replace-with-generated-auth-token-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-provider-encryption-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-prompt-template-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-file-encryption-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-batch-encryption-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-encryption-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-hmac-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-sandbox-internal-token" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-sandbox-envelope-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-sandbox-permit-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-mcp-sandbox-response-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-cache-affinity-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-session-hmac-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-approval-hmac-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-response-cache-hmac-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-response-cache-encryption-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-response-cache-control-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-response-cache-spool-secret" = (New-MetaHubLocalSecret)
    "replace-with-generated-admin-password" = $adminPassword
    "replace-with-generated-provider-placeholder" = "placeholder-" + (New-MetaHubLocalSecret -Length 12)
    "replace-with-generated-virtual-key" = "mhv_dev_" + (New-MetaHubLocalSecret -Length 24)
    "replace-with-generated-postgres-password" = (New-MetaHubLocalSecret)
    "replace-with-generated-minio-access-key" = "metahub" + (New-MetaHubLocalSecret -Length 8)
    "replace-with-generated-minio-secret-key" = (New-MetaHubLocalSecret)
  }
  foreach ($key in $replacements.Keys) {
    $content = $content.Replace($key, [string]$replacements[$key])
  }
  $temporaryDirectory = Join-Path $targetDir ".metahub-env-$([Guid]::NewGuid().ToString("N")).private"
  $temporaryEnvFile = Join-Path $temporaryDirectory "compose.env"
  try {
    New-Item -ItemType Directory -Path $temporaryDirectory -ErrorAction Stop | Out-Null
    Protect-MetaHubPrivateDirectory -Directory $temporaryDirectory
    [System.IO.File]::WriteAllText(
      $temporaryEnvFile,
      $content,
      [System.Text.UTF8Encoding]::new($false)
    )
    Protect-MetaHubEnvFile -EnvFile $temporaryEnvFile
    Move-Item -LiteralPath $temporaryEnvFile -Destination $EnvFile -ErrorAction Stop
  }
  catch {
    $failure = $_
    if (Test-Path -LiteralPath $temporaryEnvFile) {
      Remove-Item -LiteralPath $temporaryEnvFile -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporaryDirectory) {
      Remove-Item -LiteralPath $temporaryDirectory -Force -ErrorAction SilentlyContinue
    }
    # 并发安装只允许一个创建者；后来者接受并验证赢家的完整文件，绝不覆盖。
    if (Test-Path -LiteralPath $EnvFile) {
      $null = Assert-MetaHubEnvFile -EnvFile $EnvFile -RequestId $RequestId
      return @{ created = $false; admin_password = $null }
    }
    throw $failure
  }
  if (Test-Path -LiteralPath $temporaryDirectory) {
    Remove-Item -LiteralPath $temporaryDirectory -Force
  }
  $null = Assert-MetaHubEnvFile -EnvFile $EnvFile -RequestId $RequestId
  return @{ created = $true; admin_password = $adminPassword }
}

# 统一生成 docker compose 参数，保证 profile 和 project name 一致。
function Get-MetaHubComposeArgs {
  param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [Parameter(Mandatory = $true)][string]$ProjectName,
    [Parameter(Mandatory = $true)][string]$Profile
  )

  $root = Get-MetaHubRoot
  $composeFile = Join-Path $root "infra/compose/docker-compose.yml"
  # --env-file 只负责 Compose 插值；显式路径变量保证服务 env_file 使用同一份文件。
  $env:METAHUB_COMPOSE_ENV_FILE = $EnvFile.Replace("\", "/")
  $args = @("compose", "--env-file", $EnvFile, "-f", $composeFile, "-p", $ProjectName)
  if ($Profile -eq "compose-full") {
    $args += @("--profile", "compose-full")
  }
  return $args
}

# 调用 docker compose，并把失败收敛为部署错误码。
function Invoke-MetaHubDockerCompose {
  param(
    [Parameter(Mandatory = $true)][string[]]$ComposeArgs,
    [Parameter(Mandatory = $true)][string[]]$CommandArgs,
    [Parameter(Mandatory = $true)][string]$RequestId,
    [Parameter(Mandatory = $true)][string]$FailureCode
  )

  & docker @ComposeArgs @CommandArgs
  if ($LASTEXITCODE -ne 0) {
    Write-MetaHubCliError `
      -Code $FailureCode `
      -Message "docker compose command failed." `
      -RequestId $RequestId `
      -Suggestion "Run docker compose logs for gateway, control-api or console."
    exit 1
  }
}

# 对一个 HTTP 入口执行轻量健康检查。
function Test-MetaHubHttpOk {
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$TimeoutSeconds = 5
  )

  try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSeconds
    return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
  }
  catch {
    return $false
  }
}

# 轮询 gateway、control-api、console，直到三者都可访问。
function Invoke-MetaHubSmoke {
  param(
    [Parameter(Mandatory = $true)][hashtable]$EnvValues,
    [Parameter(Mandatory = $true)][string]$RequestId,
    [int]$Retries = 30,
    [int]$DelaySeconds = 2
  )

  $gatewayUrl = "http://localhost:$(Get-MetaHubEnvValue $EnvValues "METAHUB_GATEWAY_PORT" "8000")/health/live"
  $controlUrl = "http://localhost:$(Get-MetaHubEnvValue $EnvValues "METAHUB_CONTROL_PORT" "8001")/health/live"
  $consoleUrl = "http://localhost:$(Get-MetaHubEnvValue $EnvValues "METAHUB_CONSOLE_PORT" "5173")/"

  for ($attempt = 1; $attempt -le $Retries; $attempt++) {
    $gatewayOk = Test-MetaHubHttpOk -Url $gatewayUrl
    $controlOk = Test-MetaHubHttpOk -Url $controlUrl
    $consoleOk = Test-MetaHubHttpOk -Url $consoleUrl
    if ($gatewayOk -and $controlOk -and $consoleOk) {
      return @{ gateway = $gatewayUrl; control_api = $controlUrl; console = $consoleUrl }
    }
    Start-Sleep -Seconds $DelaySeconds
  }

  Write-MetaHubCliError `
    -Code "deployment.health_failed" `
    -Message "Gateway, control API or console health check did not become ready." `
    -RequestId $RequestId `
    -Suggestion "Run scripts/deploy/status.ps1 and docker compose logs."
  exit 1
}

# 写入本地 deployment.completed 事件，不包含任何 secret。
function Write-MetaHubDeploymentEvent {
  param(
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][string]$RequestId,
    [Parameter(Mandatory = $true)][hashtable]$Services
  )

  $root = Get-MetaHubRoot
  $logDir = Join-Path $root ".logs/deploy"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  $payload = [ordered]@{
    event_type = "deployment.completed"
    deployment_id = "dep_$([Guid]::NewGuid().ToString("N"))"
    profile_id = $Profile
    status = "ok"
    completed_at = (Get-Date).ToUniversalTime().ToString("o")
    services = $Services
    request_id = $RequestId
  }
  Add-Content -LiteralPath (Join-Path $logDir "deployment-events.jsonl") -Value ($payload | ConvertTo-Json -Compress) -Encoding UTF8
}

Export-ModuleMember -Function *
