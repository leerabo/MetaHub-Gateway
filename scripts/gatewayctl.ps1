$ErrorActionPreference = "Stop"

# 包装脚本只按自身位置解析仓库源码根，调用方从任意工作目录启动都不会扫描当前目录 (兼容 Windows PowerShell 5.1 无 BOM 解码).
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }

# 仅把共享 SDK 源码目录加入本进程模块路径；Token 仍只由 CLI 从环境变量或标准输入读取 (兼容 Windows PowerShell 5.1 无 BOM 解码).
$SourceRoot = Join-Path $Root "shared-sdk\python"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$SourceRoot;$env:PYTHONPATH" } else { $SourceRoot }

& $Python -m metahub_shared.gatewayctl.cli @args
exit $LASTEXITCODE
