[CmdletBinding()]
param(
  [string]$Mode = "mock",
  [string]$OutputPath = "",
  [string]$TenantId = "",
  [string]$ProbeConfig = "",
  [string]$ControlUrl = "",
  [string]$ControlTokenEnv = "METAHUB_INTERNAL_CONFIG_TOKEN",
  [switch]$AllowLoopbackControlHttp
)

$ErrorActionPreference = "Stop"

# mock 是默认离线合并门禁；未知模式在启动 Python 前以参数错误关闭。gateway_probe 仍由
# CLI 负责返回显式配置缺失码，避免 wrapper 形成另一套探测配置解释器。

if ($Mode -notin @("mock", "gateway_probe")) {
  exit 2
}

# 以脚本位置而不是调用 cwd 定位仓库，使 CI 和研发终端从任意目录启动都执行同一制品。

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = "python"
if ($env:METAHUB_PYTHON) {
  $Python = $env:METAHUB_PYTHON
}
$DefaultOutput = Join-Path $Root ".artifacts\provider-adapter-contract\report.json"
$ReportPath = $DefaultOutput
if ($OutputPath) {
  # 调用方只能改变 `.artifacts` 内的报告位置，最终归属由 Python 侧 resolve 后再次校验。

  $ReportPath = $OutputPath
}

# 合同门禁不依赖安装动作；显式拼接源码目录，且保留调用环境已有 PYTHONPATH 供 CI 扩展。

$SourcePaths = @(
  (Join-Path $Root "shared-sdk\python"),
  (Join-Path $Root "gateway-data-plane\src"),
  (Join-Path $Root "control-plane-api\src"),
  (Join-Path $Root "examples\python\src")
)
if ($env:PYTHONPATH) {
  $SourcePaths += $env:PYTHONPATH
}
$env:PYTHONPATH = $SourcePaths -join ";"

# Python CLI 负责固定退出码、报告隐私和原子落盘；使用参数数组避免路径空格或调用 cwd
# 改变命令含义，wrapper 原样传播退出码，让 CI 真正阻断合并。

$CliArguments = @(
  "-m"
  "metahub_gateway.provider_contracts"
  "--mode"
  $Mode
  "--workspace-root"
  $Root
  "--output"
  $ReportPath
)

# mock 默认使用合成租户；probe 的 tenant 由严格配置持有，只有调用方显式传入时才交叉核对。

if ($TenantId) {
  $CliArguments += @("--tenant-id", $TenantId)
} elseif ($Mode -eq "mock") {
  $CliArguments += @("--tenant-id", "ci-contract")
}
if ($ProbeConfig) {
  $CliArguments += @("--probe-config", $ProbeConfig)
}
if ($ControlUrl) {
  # Token 只按环境变量名交给 Python，wrapper 参数和进程列表都不会出现 Token 明文。

  $CliArguments += @(
    "--control-url"
    $ControlUrl
    "--control-token-env"
    $ControlTokenEnv
  )
}
if ($AllowLoopbackControlHttp) {
  $CliArguments += "--allow-loopback-control-http"
}
& $Python @CliArguments
$ContractExitCode = $LASTEXITCODE
exit $ContractExitCode
