[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TunnelClient = $env:GPT_PC_TUNNEL_CLIENT_EXE
$TunnelProfile = $env:GPT_PC_TUNNEL_PROFILE
$TunnelApiKey = $env:GPT_PC_TUNNEL_API_KEY
$McpHost = if ($env:GPT_PC_MCP_HOST) { $env:GPT_PC_MCP_HOST } else { '127.0.0.1' }
$McpPort = if ($env:GPT_PC_MCP_PORT) { [int]$env:GPT_PC_MCP_PORT } else { 18765 }
$Python = if ($env:GPT_PC_PYTHON) { $env:GPT_PC_PYTHON } else { Join-Path $ProjectRoot 'pc-mcp\.venv\Scripts\python.exe' }
$TunnelRunner = Join-Path $ProjectRoot 'scripts\run-tunnel-client.py'
$LogDir = Join-Path $ProjectRoot 'logs'
$LogFile = Join-Path $LogDir 'tunnel-client.log'

if ([string]::IsNullOrWhiteSpace($TunnelClient) -or -not (Test-Path -LiteralPath $TunnelClient -PathType Leaf)) {
    throw 'Set GPT_PC_TUNNEL_CLIENT_EXE to the compatible tunnel-client executable.'
}
if ([string]::IsNullOrWhiteSpace($TunnelProfile) -or -not (Test-Path -LiteralPath $TunnelProfile -PathType Leaf)) {
    throw 'Set GPT_PC_TUNNEL_PROFILE to this computer''s local tunnel profile.'
}
if ([string]::IsNullOrWhiteSpace($TunnelApiKey)) {
    throw 'Set GPT_PC_TUNNEL_API_KEY in the tunnel process environment or secret manager.'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python executable not found: $Python" }
if (-not (Test-Path -LiteralPath $TunnelRunner -PathType Leaf)) { throw "Tunnel runner not found: $TunnelRunner" }

$deadline = (Get-Date).AddSeconds(45)
$mcpReady = $false
while ((Get-Date) -lt $deadline) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connect = $client.ConnectAsync($McpHost, $McpPort)
        if ($connect.Wait(500) -and $client.Connected) {
            $mcpReady = $true
            break
        }
    } catch {
        # The MCP service may still be starting.
    } finally {
        $client.Dispose()
    }
    Start-Sleep -Milliseconds 500
}
if (-not $mcpReady) { throw ("GPT-PC MCP endpoint is unavailable at {0}:{1}. Start scripts\start-mcp-service.ps1 first." -f $McpHost, $McpPort) }

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$env:CONTROL_PLANE_API_KEY = $TunnelApiKey
try {
    Set-Location -LiteralPath $ProjectRoot
    $arguments = @(
        '--log-file', $LogFile,
        '--',
        $TunnelClient, 'run',
        '--profile-file', $TunnelProfile,
        '--control-plane.api-key', 'env:CONTROL_PLANE_API_KEY',
        '--control-plane.max-inflight', '16',
        '--mcp.max-concurrent-requests', '8',
        '--mcp.connection-max-ttl', '12h',
        '--log.format', 'json',
        '--log.level', 'info'
    )
    & $Python $TunnelRunner @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
}
exit $exitCode
