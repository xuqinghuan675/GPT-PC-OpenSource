# Remote tunnel bridge

GPT-PC's primary use case is remote computer operation: a web-based AI session reaches a Windows computer through a control plane and an authenticated outbound tunnel.

## Components

1. **Web AI / remote MCP host** — sends MCP requests.
2. **Remote control plane** — authenticates and routes requests to the correct connected computer. This is an external service.
3. **Tunnel client** — runs on the Windows computer and maintains the outbound authenticated connection. Its executable and profile are supplied separately.
4. **GPT-PC MCP service** — listens locally at http://127.0.0.1:18765/mcp and executes the requested tools.
5. **GPT-PC Runtime** — enforces workspace/task boundaries, bounded concurrency, background-job tracking, and task control.

## Start the local MCP endpoint

Install the Python environment, then run:

~~~powershell
uv sync --project pc-mcp --extra dev
.\scripts\start-mcp-service.ps1
~~~

The service binds only to 127.0.0.1:18765 and serves Streamable HTTP at /mcp.

## Configure a compatible tunnel client

The public launcher expects a tunnel client with a run --profile-file ... --control-plane.api-key env:CONTROL_PLANE_API_KEY interface. It also passes bounded concurrency and connection-lifetime options. If your tunnel client uses a different CLI, adapt scripts/start-remote-tunnel.ps1 to its documented interface.

Set these environment variables in the tunnel process environment:

| Variable | Required | Purpose |
| --- | --- | --- |
| GPT_PC_TUNNEL_CLIENT_EXE | Yes | Absolute path to the external tunnel-client executable |
| GPT_PC_TUNNEL_PROFILE | Yes | Absolute path to the local profile configured for this GPT-PC instance |
| GPT_PC_TUNNEL_API_KEY | Yes | Control-plane API key; read from a secret manager or local process environment |
| GPT_PC_MCP_HOST | No | Local MCP host; defaults to 127.0.0.1 |
| GPT_PC_MCP_PORT | No | Local MCP port; defaults to 18765 |
| GPT_PC_PYTHON | No | Python executable; defaults to pc-mcp/.venv/Scripts/python.exe |

Then start the tunnel:

~~~powershell
.\scripts\start-remote-tunnel.ps1
~~~

The launcher:
- checks that the tunnel executable and profile exist;
- waits for the local MCP listener to become available;
- passes the API key to the child process through CONTROL_PLANE_API_KEY and the client's environment-variable reference, not as a command-line literal;
- runs the tunnel client through run-tunnel-client.py, which keeps bounded rotating logs and cleans up only its own child process.

## Profile requirements

The local profile must identify the correct control-plane tunnel and route MCP traffic to this computer's local GPT-PC endpoint. Keep one profile per computer. Do not commit profiles, tunnel IDs, API keys, health URLs, or runtime credentials.

The profile format and control-plane registration are owned by the external tunnel provider. GPT-PC does not create hosted control-plane accounts or provision tunnel credentials.

## Network boundary

The MCP service is intentionally bound to loopback. The tunnel client makes an outbound connection to the control plane; do not open port 18765 to the public internet or bind the local MCP service to 0.0.0.0 for convenience.

## Direct local MCP mode

For an MCP host running on the same PC, start pc-mcp/server.py with stdio transport instead. No tunnel or remote control plane is needed in that configuration.
