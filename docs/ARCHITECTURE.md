# Architecture

GPT-PC connects a web-based AI session to a Windows computer through an authenticated remote tunnel. The tunnel/control plane carries MCP requests; GPT-PC provides the local execution and control boundary.

~~~text
+-----------------------------+
| Web-based AI / remote host  |
+--------------+--------------+
               | MCP requests
               v
+-----------------------------+
| Remote control plane        |  External service
+--------------+--------------+
               | Authenticated outbound tunnel
               v
+-----------------------------+
| Tunnel client               |  External client/profile
| Runs on the Windows PC      |
+--------------+--------------+
               | HTTP over loopback
               | 127.0.0.1:18765/mcp
               v
+-----------------------------+
| GPT-PC local MCP service    |
| pc-mcp/server.py            |
+-----------------------------+
| Runtime execution controls  |
| - workspace leases          |
| - task state/control inbox  |
| - bounded jobs/concurrency  |
+-----------------------------+
| Windows capabilities        |
| - files, code, Git, process |
| - images and visual context |
| - optional browser/desktop  |
+-----------------------------+
               |
               +-- sanitized activity --> Tauri activity console
~~~

## Request path

1. A web AI or remote MCP host sends an MCP request to the remote control plane.
2. The control plane routes the request over an authenticated outbound tunnel established by the tunnel client on the Windows PC.
3. The tunnel client reaches the local MCP HTTP endpoint at 127.0.0.1:18765/mcp.
4. pc-mcp/server.py validates the request and dispatches the relevant local tool.
5. runtime.py applies workspace/task controls, bounded concurrency, job tracking, and control-message rules.
6. The Windows capability layer performs the requested file, process, browser, or desktop operation.
7. Sanitized task activity can be displayed by the optional Tauri activity console.

## Components

- pc-mcp/ owns the local MCP service and runtime controls. Persistent state is rooted at GPT_PC_HOME (defaults to the repository root) and written to ignored runtime directories.
- scripts/start-mcp-service.ps1 starts the local Streamable HTTP MCP endpoint on loopback.
- scripts/start-remote-tunnel.ps1 launches a compatible external tunnel client through the bounded process/log wrapper.
- scripts/run-tunnel-client.py owns the child process, bounds log rotation, and terminates only that child if log handling fails.
- desktop-workspace/ contains optional browser and Windows desktop helpers.
- chatgpt-ui/ is a Tauri/React activity console. It displays sanitized activity and task metadata; it is not a model runtime or a second agent loop.

## Network and security boundary

- The local MCP listener is bound to 127.0.0.1; do not expose port 18765 directly to the public internet.
- The tunnel client establishes the outbound connection. The remote control plane and tunnel client are external components supplied by the operator.
- Tunnel API keys and profile files are local secrets. Keep them outside Git and provide the key through the process environment or a secret manager.
- The public repository does not include the hosted control plane, tunnel-client executable, tunnel profiles, or credentials.
- BrowserSkill, VirtualDesktopAccessor, and Gmail OAuth are optional and configured independently.

## Direct local mode

The MCP server also supports stdio for an MCP host running on the same computer. That mode bypasses the remote tunnel and is useful for local development and direct desktop use.
