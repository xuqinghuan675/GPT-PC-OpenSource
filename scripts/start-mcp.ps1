$ErrorActionPreference = 'Stop'
$ServiceScript = Join-Path $PSScriptRoot 'start-mcp-service.ps1'
if (-not (Test-Path -LiteralPath $ServiceScript)) { throw "MCP service start script not found: $ServiceScript" }
& $ServiceScript
exit $LASTEXITCODE
