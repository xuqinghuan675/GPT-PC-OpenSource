[CmdletBinding()]
param(
    [int]$RetentionDays = 10,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDirectory = Join-Path $ProjectRoot 'logs'
$JobDirectory = Join-Path $ProjectRoot 'runtime\jobs'

if ($RetentionDays -lt 1) { throw 'RetentionDays 必须至少为 1。' }

function Remove-ExpiredLog {
    param([System.IO.FileInfo]$File)

    if ($DryRun) {
        Write-Output ('将删除超过保留期的日志：' + $File.FullName)
        return
    }

    Remove-Item -LiteralPath $File.FullName -Force -ErrorAction Stop
    Write-Output ('已删除超过保留期的日志：' + $File.FullName)
}

$cutoff = (Get-Date).AddDays(-$RetentionDays)
$expiredLogs = @(if (Test-Path -LiteralPath $LogDirectory) {
    Get-ChildItem -LiteralPath $LogDirectory -File -Force -Recurse -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LastWriteTime -lt $cutoff -and
            $_.Name -match '\.log(?:\.\d+)?$'
        }
})
$expiredJobRecords = @(if (Test-Path -LiteralPath $JobDirectory) {
    Get-ChildItem -LiteralPath $JobDirectory -File -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.LastWriteTime -lt $cutoff -and
            $_.Name -match '^job-[0-9a-f]+\.json$'
        }
})

if ($expiredLogs.Count -eq 0 -and $expiredJobRecords.Count -eq 0) {
    Write-Output ("没有超过 $RetentionDays 天的日志或任务记录需要清理。")
    exit 0
}

foreach ($file in $expiredLogs) {
    Remove-ExpiredLog -File $file
}

foreach ($file in $expiredJobRecords) {
    Remove-ExpiredLog -File $file
}
