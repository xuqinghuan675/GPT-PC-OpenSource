param(
    [switch]$Force
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$McpPidFile = Join-Path $ProjectRoot 'runtime\mcp-server.pid'
$TaskNames = @('GPT-PC-MCP-Server', 'GPT-PC-MCP')
$stopped = @()
$stoppedTasks = @()

if (-not $Force) {
    $leaseDirectory = Join-Path $ProjectRoot 'runtime\leases'
    $activeLease = $false
    if (Test-Path -LiteralPath $leaseDirectory) {
        foreach ($recordPath in Get-ChildItem -LiteralPath $leaseDirectory -Filter 'lease-*.json' -File -ErrorAction SilentlyContinue) {
            try {
                $record = Get-Content -LiteralPath $recordPath.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $expiry = [DateTimeOffset]::Parse([string]$record.expires_at)
                $isInternalService = ([string]$record.thread_id).StartsWith('gptagent-supervisor-')
                if ($record.mode -eq 'execute_exclusive' -and -not $isInternalService -and $expiry -gt [DateTimeOffset]::UtcNow) {
                    $activeLease = $true
                    break
                }
            } catch {}
        }
    }
    if ($activeLease) {
        throw '存在活跃执行工作区租约；为避免中断开发任务，停止运行底座必须明确添加 -Force。'
    }
}

foreach ($taskName in $TaskNames) {
    try {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq 'Running') {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $stoppedTasks += $taskName
        }
    } catch {}
}
Start-Sleep -Milliseconds 500

if (Test-Path -LiteralPath $McpPidFile) {
    $pidText = (Get-Content -LiteralPath $McpPidFile -Raw -Encoding UTF8).Trim()
    $pidValue = 0
    if ([int]::TryParse($pidText, [ref]$pidValue) -and $pidValue -gt 0) {
        $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
        if ($process) {
            & taskkill.exe /PID $pidValue /T /F | Out-Null
            $stopped += $pidValue
        }
    }
    Remove-Item -LiteralPath $McpPidFile -Force -ErrorAction SilentlyContinue
}

Write-Output ("Background tasks stopped: " + ($stoppedTasks -join ', '))
Write-Output ("Stop requested for process IDs: " + (($stopped | Select-Object -Unique) -join ', '))
