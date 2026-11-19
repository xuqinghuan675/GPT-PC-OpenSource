# GPT-PC

**GPT-PC is a remote bridge and execution-control framework that lets web-based AI agents operate a Windows computer through an authenticated tunnel.** A local MCP service exposes computer capabilities; a separately supplied tunnel client connects that service to a remote control plane. GPT-PC manages the local execution boundary, task isolation, and computer operations.

## How it works

~~~text
Web-based AI / remote MCP host
          |
          | MCP requests
          v
Remote control plane (external)
          |
          | authenticated outbound tunnel
          v
Tunnel client on the Windows PC (external)
          |
          | localhost HTTP: 127.0.0.1:18765/mcp
          v
GPT-PC local MCP service
          |
          +-- execution controls: workspace leases, task state, job limits
          +-- local capabilities: files, code, Git, processes, images
          +-- optional capabilities: browser, Windows desktop, Gmail
          |
          v
Windows computer

Sanitized task activity --------------------> Tauri activity console
~~~

The tunnel connection is outbound from the PC. The local MCP listener is bound to loopback; the computer does not need a publicly exposed MCP port.

## Main components

- **Remote bridge (scripts/start-remote-tunnel.ps1, scripts/run-tunnel-client.py)** — launches a compatible external tunnel client, keeps its output in bounded rotating logs, and connects it to the local MCP endpoint. Tunnel client binaries, profiles, credentials, and the hosted control plane are supplied separately.
- **Local execution service (pc-mcp/)** — MCP tools for workspace-scoped file/code operations, Git, process and job management, visual context, and task control.
- **Execution controls (pc-mcp/runtime.py)** — workspace leases, task state, bounded concurrency, background jobs, and control messages.
- **Windows automation (desktop-workspace/, scripts/)** — optional browser sessions, virtual-desktop coordination, and background UI Automation.
- **Activity console (chatgpt-ui/)** — a Tauri desktop app for task activity, history, visual references, and task-scoped user corrections.
- **Portable configuration (config/)** — safe templates. Runtime state, tunnel profiles, OAuth files, and credentials stay local to each installation.

GPT-PC connects web AI to a real computer; it does not bundle a language model or require an OpenAI API key. MCP is the local tool interface, while the tunnel/control plane carries remote requests to the computer.

## Quick start

Requirements: Windows 10/11, Python 3.12+, uv, and Git. The desktop console additionally needs Node.js 20.19+ and Rust tooling.

~~~powershell
git clone https://github.com/xuqinghuan675/GPT-PC-OpenSource.git
cd GPT-PC-OpenSource
uv sync --project pc-mcp --extra dev

# Start the local MCP HTTP endpoint on 127.0.0.1:18765/mcp
.\scripts\start-mcp-service.ps1

# In a second PowerShell session, configure a compatible tunnel client and run:
# .\scripts\start-remote-tunnel.ps1
~~~

See INSTALL.md and docs/REMOTE_TUNNEL.md for the remote bridge setup. Direct local MCP hosts can still use stdio mode.

## Configuration

Set GPT_PC_HOME to choose the local data root. Each computer keeps its own config/machine.local.toml, tunnel profile, OAuth files, browser profile, and runtime state. Never commit credentials or runtime data.

See docs/CONFIGURATION.md.

## Project status

The local MCP runtime and bridge launcher are open source. The hosted control plane and compatible tunnel client are external components and are not bundled in this repository. BrowserSkill, VirtualDesktopAccessor, and Gmail OAuth are also optional integrations.

## License

GPT-PC is released under the MIT License. See LICENSE and THIRD_PARTY_NOTICES.md for third-party components and attribution.
