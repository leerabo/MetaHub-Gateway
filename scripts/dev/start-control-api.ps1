$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$WorkspacePython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if ($env:METAHUB_PYTHON) {
  $env:METAHUB_PYTHON
} elseif (Test-Path -LiteralPath $WorkspacePython) {
  $WorkspacePython
} else {
  "python"
}

# Console 本地开发既可能从 localhost 打开，也可能从 127.0.0.1 打开。
# 两者都是同一台机器，但对浏览器 CORS 来说是不同 Origin。仅在调用者未显式
# 配置时提供这两个 loopback 来源，避免把开发默认带到实际部署配置中。
if (-not $env:METAHUB_ALLOWED_ORIGINS) {
  $env:METAHUB_ALLOWED_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
}

# Control API 仍是独立进程，但调试控制台复用 Gateway Provider Adapter，因此需加载三个源码根目录。
if ($env:METAHUB_DEBUG_STARTUP -eq "1") {
  Write-Host "Root=$Root"
  Write-Host "SourceRoots=${Root}\shared-sdk\python;${Root}\gateway-data-plane\src;${Root}\control-plane-api\src"
  Write-Host "Python=$Python"
}

# 由 Python 显式注入 sys.path，避免部分 Windows PowerShell 宿主静默清空 PYTHONPATH。
& $Python -c "import runpy, sys; sys.path[:0] = [r'${Root}\shared-sdk\python', r'${Root}\gateway-data-plane\src', r'${Root}\control-plane-api\src']; runpy.run_module('metahub_control.main', run_name='__main__')"
