# Installation

## 1. Requirements

- Windows 10 or 11.
- Python 3.12 or newer.
- Git and uv (Python package and environment manager).
- Optional remote operation: a compatible tunnel-client executable, a control-plane account, a local tunnel profile, and an API key.
- Optional activity console: Node.js 20.19+, Rust toolchain, Microsoft C++ Build Tools, and WebView2 Runtime.
- Optional browser automation: Tencent BrowserSkill CLI.
- Optional virtual-desktop automation: Ciantic VirtualDesktopAccessor DLL (64-bit Windows).

## 2. Install the local MCP runtime

Install uv if it is not already available:

~~~powershell
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
~~~

Clone and install:

~~~powershell
git clone https://github.com/xuqinghuan675/GPT-PC-OpenSource.git
cd GPT-PC-OpenSource
uv sync --project pc-mcp --extra dev
.\pc-mcp\.venv\Scripts\python.exe -m pytest -q .\pc-mcp\tests
~~~

## 3. Run GPT-PC locally

To use the local Streamable HTTP endpoint (required by the remote tunnel integration):

~~~powershell
.\scripts\start-mcp-service.ps1
~~~

The endpoint listens on 127.0.0.1:18765/mcp. It is bound to loopback and should not be exposed directly to the public internet.

For an MCP host running on the same computer, use stdio mode instead:

~~~powershell
.\pc-mcp\.venv\Scripts\python.exe .\pc-mcp\server.py --transport stdio
~~~

## 4. Connect a remote web AI through a tunnel

GPT-PC does not bundle a hosted control plane or tunnel-client executable. Obtain a compatible tunnel client and profile from your chosen provider/operator.

Set the following variables in the tunnel process environment:

- GPT_PC_TUNNEL_CLIENT_EXE — full path to the tunnel-client executable.
- GPT_PC_TUNNEL_PROFILE — full path to the local tunnel profile for this PC.
- GPT_PC_TUNNEL_API_KEY — API key supplied by the control plane. Keep it in a secret manager or local process environment; never commit it.
- Optional: GPT_PC_PYTHON, GPT_PC_MCP_HOST, and GPT_PC_MCP_PORT.

Start the authenticated tunnel:

~~~powershell
.\scripts\start-remote-tunnel.ps1
~~~

The launcher checks the local MCP endpoint and starts the tunnel client through the bounded log/process wrapper. Its CLI contract is documented in docs/REMOTE_TUNNEL.md. If your provider uses different CLI arguments, adapt the launcher to the provider's documented interface.

See docs/REMOTE_TUNNEL.md for the request path, profile requirements, and network boundary.

## 5. Configure another MCP host

For a local MCP host, add a server entry to its configuration. Use absolute paths for the Python executable and repository location:

~~~json
{
  "mcpServers": {
    "gpt-pc": {
      "command": "C:\\path\\to\\GPT-PC-OpenSource\\pc-mcp\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\path\\to\\GPT-PC-OpenSource\\pc-mcp\\server.py"
      ],
      "env": {
        "GPT_PC_HOME": "C:\\path\\to\\GPT-PC-OpenSource"
      }
    }
  }
}
~~~

Restart or reload the MCP host after saving the configuration.

## 6. Optional browser automation

GPT-PC uses the external BrowserSkill CLI; its executable is not bundled here.

Install the official Windows CLI:

~~~powershell
irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex
bsk --version
~~~

The aiwork entry point searches PATH for bsk.exe, then checks desktop-workspace/browser-skill/bsk.exe. Follow the BrowserSkill project documentation for browser pairing and daemon setup: https://github.com/Tencent/BrowserSkill

## 7. Optional Windows virtual desktops

The virtual-desktop helper uses the third-party VirtualDesktopAccessor.dll, which is not bundled here.

1. Download a compatible x64 Windows DLL from the upstream project: https://github.com/ciantic/VirtualDesktopAccessor
2. Place it at desktop-workspace/VirtualDesktopAccessor.dll, or set GPT_PC_VDA_DLL to its full path.
3. Verify the DLL SHA-256 against the expected build recorded in desktop-workspace/desktopctl.py.
4. Run desktop-workspace/desktopctl.cmd status.

## 8. Optional activity console

~~~powershell
cd .\chatgpt-ui
npm ci
npm test
npm run build
~~~

Use npm run dev for the Vite development server. A native Tauri build additionally requires Rust and the Windows desktop build prerequisites.

## 9. Optional Gmail integration

Gmail support is disabled until you configure your own Google OAuth Desktop client:

1. Create an OAuth Desktop client in your own Google Cloud project and enable the Gmail API.
2. Save the client JSON outside version control (the default path is config/gmail-oauth-client.json).
3. Run an interactive Gmail action once to complete consent. The token is stored locally at config/gmail-oauth-token.json.
4. Optionally set GPT_PC_GMAIL_EXPECTED_ACCOUNT to pin the account used by the bridge.

Optional email notifications require explicit configuration:
- GPT_PC_ULTRA_COMPLETION_EMAIL — destination for Ultra completion messages.
- GPT_PC_DISCONNECT_ALERT_EMAIL — destination for confirmed disconnect alerts.

No recipient is hard-coded. Do not commit OAuth client files, token files, or environment files.

## 10. Local configuration

Copy config/machine.example.toml to config/machine.local.toml and edit paths and host-specific settings. The local file is ignored by Git. See docs/CONFIGURATION.md.
