$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { "python" }

# Control API 仍是独立进程，但调试控制台复用 Gateway Provider Adapter，因此需加载三个源码根目录。
if ($env:METAHUB_DEBUG_STARTUP -eq "1") {
  Write-Host "Root=$Root"
  Write-Host "SourceRoots=${Root}\shared-sdk\python;${Root}\gateway-data-plane\src;${Root}\control-plane-api\src"
  Write-Host "Python=$Python"
}

# 由 Python 显式注入 sys.path，避免部分 Windows PowerShell 宿主静默清空 PYTHONPATH。
& $Python -c "import runpy, sys; sys.path[:0] = [r'${Root}\shared-sdk\python', r'${Root}\gateway-data-plane\src', r'${Root}\control-plane-api\src']; runpy.run_module('metahub_control.main', run_name='__main__')"
