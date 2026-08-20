param(
  [ValidateSet("All", "Backend", "Performance", "Web")]
  [string]$Group = "All",
  [switch]$RunRealBrowser,
  [switch]$RunBrowserMatrix,
  [ValidateSet("chromium", "edge", "firefox")]
  [string]$Browser = "chromium",
  [ValidateSet("1", "1.25", "1.5")]
  [string]$Scale = "1",
  [switch]$RunLivePerformance,
  [switch]$RunPressure,
  [int]$PressureRequests = 600,
  [int]$PressureConcurrency = 12,
  [int]$PressureDurationSeconds = 60
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = if ($env:METAHUB_PYTHON) { $env:METAHUB_PYTHON } else { Join-Path $Root ".venv\Scripts\python.exe" }
$Node = if (Test-Path "C:\Program Files\nodejs\node.exe") { "C:\Program Files\nodejs\node.exe" } else { "node" }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$artifactRoot = Join-Path $Root ".artifacts\product-qa\$stamp"
$tempRoot = Join-Path $Root ".tmp\product-qa\$stamp"
New-Item -ItemType Directory -Force -Path $artifactRoot, $tempRoot | Out-Null
$previousQaArtifactRoot = $env:METAHUB_QA_ARTIFACT_ROOT
$env:METAHUB_QA_ARTIFACT_ROOT = $artifactRoot

function Add-Task {
  param([string]$Name, [string]$FilePath, [string[]]$ArgumentList, [string]$OutputPath)
  $errorPath = "$OutputPath.err"
  $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $Root `
    -RedirectStandardOutput $OutputPath -RedirectStandardError $errorPath -PassThru -WindowStyle Hidden
  [pscustomobject]@{ Name = $Name; Process = $process; OutputPath = $OutputPath; ErrorPath = $errorPath }
}

$tasks = @()
if ($Group -in @("All", "Backend")) {
  $tasks += Add-Task "WB-API-BB-GR-SEC" $Python @(
    "-m", "pytest", "-q", "--junitxml", (Join-Path $artifactRoot "backend-junit.xml"),
    "--basetemp", (Join-Path $tempRoot "backend"),
    (Join-Path $Root "tests\product_qa\backend\test_backend_product_qa.py"),
    (Join-Path $Root "tests\product_qa\test_plan_catalog.py")
  ) (Join-Path $artifactRoot "backend.out")
}
if ($Group -in @("All", "Performance")) {
  $tasks += Add-Task "PERF-isolated" $Python @(
    "-m", "pytest", "-q", "--junitxml", (Join-Path $artifactRoot "performance-junit.xml"),
    "--basetemp", (Join-Path $tempRoot "performance"),
    (Join-Path $Root "tests\product_qa\performance\test_product_qa_performance.py"),
    (Join-Path $Root "tests\product_qa\performance\test_product_qa_stability_gate.py")
  ) (Join-Path $artifactRoot "performance.out")
}
if ($Group -in @("All", "Web")) {
  $tasks += Add-Task "WEB-UI-UCD-COMP-FLOW-contracts" $Node @(
    "--test",
    (Join-Path $Root "tests\product_qa\web\compatibilityMatrix.test.mjs"),
    (Join-Path $Root "tests\product_qa\web\csvExportEvidence.test.mjs"),
    (Join-Path $Root "tests\product_qa\web\humanMouseJourneys.test.mjs"),
    (Join-Path $Root "tests\product_qa\web\productQaContracts.test.mjs"),
    (Join-Path $Root "tests\product_qa\web\serviceRecoveryFlow.test.mjs")
  ) (Join-Path $artifactRoot "web.out")
  $tasks += Add-Task "Console-lint" $Node @((Join-Path $Root "console-web\scripts\lint.mjs")) (Join-Path $artifactRoot "console-lint.out")
  if ($RunRealBrowser) {
    $env:METAHUB_QA_BROWSER = $Browser
    $env:METAHUB_QA_SCALE = $Scale
    $tasks += Add-Task ("FLOW-real-{0}-scale-{1}" -f $Browser, $Scale.Replace('.', '_')) $Python @(
      (Join-Path $Root "scripts\quality\product_qa_browser_runtime.py")
    ) (Join-Path $artifactRoot ("browser-real-{0}-scale-{1}.out" -f $Browser, $Scale.Replace('.', '_')))
  }
  if ($RunBrowserMatrix) {
    foreach ($matrixBrowser in @("chromium", "edge", "firefox")) {
      foreach ($matrixScale in @("1", "1.25", "1.5")) {
        $env:METAHUB_QA_BROWSER = $matrixBrowser
        $env:METAHUB_QA_SCALE = $matrixScale
        $matrixSuffix = "{0}-scale-{1}" -f $matrixBrowser, $matrixScale.Replace('.', '_')
        $matrixTask = Add-Task ("FLOW-real-{0}" -f $matrixSuffix) $Python @(
          (Join-Path $Root "scripts\quality\product_qa_browser_runtime.py")
        ) (Join-Path $artifactRoot ("browser-real-{0}.out" -f $matrixSuffix))
        $matrixTask.Process.WaitForExit()
        $matrixTask.Process.Refresh()
        $tasks += $matrixTask
      }
    }
  }
}

if ($RunLivePerformance -and $Group -in @("All", "Performance")) {
  $liveOutput = Join-Path $artifactRoot "performance-live.json"
  $tasks += Add-Task "PERF-loopback-smoke" $Python @(
    (Join-Path $Root "scripts\quality\product_qa_performance.py"),
    "--requests", "30", "--concurrency", "3", "--output", $liveOutput
  ) (Join-Path $artifactRoot "performance-live.out")
}
if ($RunPressure -and $Group -in @("All", "Performance")) {
  $pressureOutput = Join-Path $artifactRoot "performance-pressure.json"
  $tasks += Add-Task "PERF-loopback-pressure" $Python @(
    (Join-Path $Root "scripts\quality\product_qa_performance.py"),
    "--requests", "$PressureRequests", "--concurrency", "$PressureConcurrency",
    "--duration-seconds", "$PressureDurationSeconds", "--output", $pressureOutput
  ) (Join-Path $artifactRoot "performance-pressure.out")
}

foreach ($task in $tasks) {
  $task.Process.WaitForExit()
  $task.Process.Refresh()
}

$results = @()
foreach ($task in $tasks) {
  $task.Process.Refresh()
  $errorBytes = if (Test-Path -LiteralPath $task.ErrorPath) { (Get-Item -LiteralPath $task.ErrorPath).Length } else { 0 }
  $results += [pscustomobject]@{
    group = [string]$task.Name
    # A child traceback or runner error is a failed task even when Start-Process
    # exposes a stale zero ExitCode after a nested process timeout.
    exit_code = if ($errorBytes -gt 0) { -1 } else { [int]$task.Process.ExitCode }
    error_bytes = [int64]$errorBytes
  }
}
$summary = [ordered]@{
  schema = "metahub.product-qa.run.v1"
  group = $Group
  artifact_dir = $artifactRoot
  all_108_prds_complete = $false
  results = $results
}
$summaryPath = Join-Path $artifactRoot "summary.json"
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding utf8
$env:METAHUB_QA_ARTIFACT_ROOT = $previousQaArtifactRoot

$failed = @($results | Where-Object { $_.exit_code -ne 0 })
Write-Output ("Product QA group {0}: {1}/{2} tasks passed" -f $Group, ($results.Count - $failed.Count), $results.Count)
Write-Output ("Redacted summary: {0}" -f $summaryPath)
if ($failed.Count -gt 0) {
  Write-Output ("Failed task count: {0}; inspect the task-specific artifact files." -f $failed.Count)
  exit 1
}
exit 0
