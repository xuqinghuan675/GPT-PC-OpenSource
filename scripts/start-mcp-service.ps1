[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot 'pc-mcp\.venv\Scripts\python.exe'
$Server = Join-Path $ProjectRoot 'pc-mcp\server.py'
$LogDir = Join-Path $ProjectRoot 'logs'
$ServiceLog = Join-Path $LogDir 'mcp-http-service.log'
$HostAddress = '127.0.0.1'
$Port = 18765
$Path = '/mcp'

if (-not (Test-Path -LiteralPath $Python)) { throw "MCP Python not found: $Python" }
if (-not (Test-Path -LiteralPath $Server)) { throw "MCP server not found: $Server" }
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$client = [System.Net.Sockets.TcpClient]::new()
try {
    $connect = $client.ConnectAsync($HostAddress, $Port)
    if ($connect.Wait(500) -and $client.Connected) {
        Add-Content -LiteralPath $ServiceLog -Value "$(Get-Date -Format o) MCP HTTP listener already available on $HostAddress`:$Port" -Encoding UTF8
        exit 0
    }
} catch {
    # A refused connection faults Task.Wait. No listener is normal on a cold
    # start, so continue to launch the server instead of failing the task.
    Add-Content -LiteralPath $ServiceLog -Value "$(Get-Date -Format o) MCP listener unavailable; starting service" -Encoding UTF8
} finally {
    $client.Dispose()
}

Set-Location -LiteralPath (Join-Path $ProjectRoot 'pc-mcp')
& $Python $Server --transport streamable-http --host $HostAddress --port $Port --path $Path *>> $ServiceLog
exit $LASTEXITCODE
