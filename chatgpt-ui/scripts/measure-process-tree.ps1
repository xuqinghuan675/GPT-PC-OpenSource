param(
  [Parameter(Mandatory = $true)]
  [int]$RootPid
)

$ErrorActionPreference = 'Stop'
$queue = [System.Collections.Generic.Queue[int]]::new()
$seen = [System.Collections.Generic.HashSet[int]]::new()
$metadata = @{}
$queue.Enqueue($RootPid)

while ($queue.Count -gt 0) {
  $pidValue = $queue.Dequeue()
  if (-not $seen.Add($pidValue)) { continue }
  $meta = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
  if ($meta) { $metadata[$pidValue] = $meta }
  foreach ($child in Get-CimInstance Win32_Process -Filter "ParentProcessId=$pidValue" -ErrorAction SilentlyContinue) {
    $queue.Enqueue([int]$child.ProcessId)
  }
}

$rows = foreach ($pidValue in $seen) {
  $meta = $metadata[$pidValue]
  $proc = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
  if (-not $proc) { continue }
  [pscustomobject]@{
    pid = $pidValue
    parentPid = if ($meta) { [int]$meta.ParentProcessId } else { $null }
    name = $proc.ProcessName
    executablePath = if ($meta) { $meta.ExecutablePath } else { $null }
    workingSetMb = [math]::Round($proc.WorkingSet64 / 1MB, 1)
    privateMemoryMb = [math]::Round($proc.PrivateMemorySize64 / 1MB, 1)
    cpuSeconds = [math]::Round($proc.CPU, 3)
    mainWindowTitle = $proc.MainWindowTitle
  }
}

$main = $rows | Where-Object { $_.pid -eq $RootPid } | Select-Object -First 1
$webviews = @($rows | Where-Object { $_.name -like 'msedgewebview2*' })
$total = ($rows | Measure-Object workingSetMb -Sum).Sum
$webviewTotal = ($webviews | Measure-Object workingSetMb -Sum).Sum

[pscustomobject]@{
  timestamp = (Get-Date).ToString('o')
  rootPid = $RootPid
  mainWorkingSetMb = if ($main) { $main.workingSetMb } else { 0 }
  webView2WorkingSetMb = [math]::Round([double]$webviewTotal, 1)
  totalWorkingSetMb = [math]::Round([double]$total, 1)
  processCount = @($rows).Count
  processes = @($rows | Sort-Object pid)
} | ConvertTo-Json -Depth 5
