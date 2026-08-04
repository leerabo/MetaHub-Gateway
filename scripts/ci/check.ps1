$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }

function Invoke-CheckedNative {
  param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
  )

  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code $LASTEXITCODE`: $FilePath $($Arguments -join ' ')"
  }
}

# CI checks must pass without database, Redis, or Provider endpoints.
$env:PYTHONPATH = "$Root\shared-sdk\python;$Root\gateway-data-plane\src;$Root\control-plane-api\src;$env:PYTHONPATH"
Invoke-CheckedNative $Python "$Root\scripts\ci\check-comments.py"

# Provider Adapter 合同是独立合并门禁，必须在全量 pytest 前单独执行；仅依赖 pytest
# 间接覆盖会让新增 spec 在收集或选择变化时被静默放行。
$CurrentPowerShell = (Get-Process -Id $PID).Path
Invoke-CheckedNative $CurrentPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File `
  "$Root\scripts\ci\provider-adapter-contract-test.ps1" -Mode mock
Invoke-CheckedNative $Python -m pytest

# JavaScript/Node 示例是独立可下载制品，必须按 lockfile 安装并执行自身测试，
# 不能只依赖控制台测试间接证明示例入口、流式处理和错误脱敏仍可运行。
# 使用 --prefix 显式指定绝对目录，避免调用方或前序测试改变 PowerShell 当前目录后找错 lockfile。
Invoke-CheckedNative "npm" --prefix "$Root\examples\js" ci --ignore-scripts
Invoke-CheckedNative "npm" --prefix "$Root\examples\js" test

# 控制台同样使用固定目录执行，确保前后端测试之间不存在隐式工作目录耦合。
Invoke-CheckedNative "npm" --prefix "$Root\console-web" test
Invoke-CheckedNative "npm" --prefix "$Root\console-web" run lint
