$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pidFile = Join-Path $Root ".logs\dev-processes.pid"
if (-not (Test-Path -LiteralPath $pidFile)) {
  Write-Host "No MetaHub development process record was found."
  exit 0
}

$recordedPids = @(
  Get-Content -LiteralPath $pidFile |
    Where-Object { $_ -match '^\d+$' } |
    ForEach-Object { [int]$_ }
)
$processRows = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$processById = @{}
$childrenByParent = @{}
foreach ($row in $processRows) {
  $processById[[int]$row.ProcessId] = $row
  $parentProcessId = [int]$row.ParentProcessId
  if (-not $childrenByParent.ContainsKey($parentProcessId)) {
    $childrenByParent[$parentProcessId] = [System.Collections.Generic.List[int]]::new()
  }
  [void]$childrenByParent[$parentProcessId].Add([int]$row.ProcessId)
}

$visited = [System.Collections.Generic.HashSet[int]]::new()
$stopOrder = [System.Collections.Generic.List[int]]::new()
function Add-MetaHubProcessTree {
  param([int]$ProcessId)

  if (-not $visited.Add($ProcessId)) { return }
  if ($childrenByParent.ContainsKey($ProcessId)) {
    foreach ($childProcessId in $childrenByParent[$ProcessId]) {
      Add-MetaHubProcessTree -ProcessId $childProcessId
    }
  }
  [void]$stopOrder.Add($ProcessId)
}

foreach ($recordedPid in $recordedPids) {
  if (-not $processById.ContainsKey($recordedPid)) { continue }
  $rootProcess = $processById[$recordedPid]
  $commandLine = [string]$rootProcess.CommandLine
  $isMetaHubLauncher = (
    $commandLine -match [regex]::Escape($Root) -and
    $commandLine -match 'scripts[\\/]dev[\\/]start-(gateway|control-api|console)\.ps1'
  )
  if (-not $isMetaHubLauncher) {
    Write-Warning "Skipped stale process record $recordedPid because it is not a MetaHub launcher."
    continue
  }
  Add-MetaHubProcessTree -ProcessId $recordedPid
}

foreach ($processId in $stopOrder) {
  if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
    Stop-Process -Id $processId -Force
  }
}
Remove-Item -LiteralPath $pidFile
Write-Host "MetaHub development services stopped."
