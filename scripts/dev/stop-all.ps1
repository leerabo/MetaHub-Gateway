$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$pidFile = Join-Path $Root ".logs\dev-processes.pid"
$recordedPids = @()
if (Test-Path -LiteralPath $pidFile) {
  $recordedPids = @(
    Get-Content -LiteralPath $pidFile |
      Where-Object { $_ -match '^\d+$' } |
      ForEach-Object { [int]$_ }
  )
}
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
    $commandLine -match 'scripts[\\/]dev[\\/]start-(gateway|control-api|status|console)\.ps1'
  )
  if (-not $isMetaHubLauncher) {
    Write-Warning "Skipped stale process record $recordedPid because it is not a MetaHub launcher."
    continue
  }
  Add-MetaHubProcessTree -ProcessId $recordedPid
}

# Manually started services have no launcher PID record. Match both port and
# command before stopping a listener; leave unknown processes running.
$listenerCommands = @{
  5173 = '(^|[\\/\s])scripts[\\/]dev-server\.mjs(\s|$)'
  8000 = '(^|\s)metahub_gateway\.main(\s|$)'
  8001 = '(^|\s)metahub_control\.main(\s|$)'
  8002 = [regex]::Escape((Join-Path $Root 'scripts\dev\run-status.py'))
}
$listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $listenerCommands.ContainsKey([int]$_.LocalPort) })
foreach ($listener in $listeners) {
  $listenerPid = [int]$listener.OwningProcess
  if ($visited.Contains($listenerPid)) { continue }
  $process = $processById[$listenerPid]
  if ($null -eq $process -or [string]$process.CommandLine -notmatch $listenerCommands[[int]$listener.LocalPort]) {
    Write-Warning "Port $($listener.LocalPort) is used by PID $listenerPid, which is not a recognized MetaHub service. It was left running."
    continue
  }
  Add-MetaHubProcessTree -ProcessId $listenerPid
}

foreach ($processId in $stopOrder) {
  if (Get-Process -Id $processId -ErrorAction SilentlyContinue) {
    Stop-Process -Id $processId -Force
  }
}
if (Test-Path -LiteralPath $pidFile) {
  Remove-Item -LiteralPath $pidFile
}
if ($stopOrder.Count -eq 0) {
  Write-Host "No managed MetaHub development services were found."
} else {
  Write-Host "MetaHub development services stopped."
}
