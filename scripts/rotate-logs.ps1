param(
    [int]$MaxSizeMB = 10,
    [int]$Keep = 5
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $ProjectRoot 'logs'
if (-not (Test-Path -LiteralPath $LogDir)) { exit 0 }

$MaxBytes = [int64]$MaxSizeMB * 1MB
$files = Get-ChildItem -LiteralPath $LogDir -Filter '*.log' -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notin @('mcp-server.log') }
foreach ($file in $files) {
    if ($file.Length -le $MaxBytes) { continue }
    for ($index = $Keep; $index -ge 1; $index--) {
        $source = $file.FullName + '.' + $index
        $destination = $file.FullName + '.' + ($index + 1)
        if (Test-Path -LiteralPath $source) {
            Move-Item -LiteralPath $source -Destination $destination -Force -ErrorAction SilentlyContinue
        }
    }
    Move-Item -LiteralPath $file.FullName -Destination ($file.FullName + '.1') -Force -ErrorAction SilentlyContinue
}
