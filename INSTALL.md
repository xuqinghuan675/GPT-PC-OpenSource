# Installation

## 1. Requirements

- Windows 10 or 11.
- Python 3.12 or newer (Python 3.12 is recommended).
- Git.
- uv (Python package and environment manager).
- Optional activity console: Node.js 20.19+, Rust toolchain, Microsoft C++ Build Tools, and WebView2 Runtime.
- Optional browser automation: Tencent BrowserSkill CLI.
- Optional virtual-desktop automation: Ciantic VirtualDesktopAccessor DLL (64-bit Windows).

## 2. Install the MCP server

Install uv if it is not already available:

    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
    uv --version


    git clone https://github.com/xuqinghuan675/GPT-PC-OpenSource.git
    cd GPT-PC-OpenSource
    uv sync --project pc-mcp --extra dev
    .\pc-mcp\.venv\Scripts\python.exe -m pytest -q .\pc-mcp\tests

Start the server directly:

    .\pc-mcp\.venv\Scripts\python.exe .\pc-mcp\server.py --transport stdio

The default transport is stdio. For local Streamable HTTP, use --transport streamable-http; keep it bound to 127.0.0.1 unless you have separately configured authentication and network protection.

## 3. Configure an MCP host

Add an MCP server entry to your MCP host configuration. Use absolute paths for the Python executable and repository location:

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

Restart or reload the MCP host after saving its configuration.

## 4. Optional browser automation

GPT-PC uses the external BrowserSkill CLI; its executable is not bundled here.

Install the official Windows CLI:

    irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex
    bsk --version

The aiwork entry point searches PATH for bsk.exe, then checks desktop-workspace/browser-skill/bsk.exe. Follow the BrowserSkill project documentation for browser pairing and daemon setup: https://github.com/Tencent/BrowserSkill

## 5. Optional Windows virtual desktops

The virtual-desktop helper uses the third-party VirtualDesktopAccessor.dll, which is not bundled here.

1. Download a compatible x64 Windows DLL from the upstream project: https://github.com/ciantic/VirtualDesktopAccessor
2. Place it at desktop-workspace/VirtualDesktopAccessor.dll, or set GPT_PC_VDA_DLL to its full path.
3. Verify the DLL SHA-256 against the expected build recorded in desktop-workspace/desktopctl.py.
4. Run desktop-workspace/desktopctl.cmd status.

The optional desktop automation entry point is desktop-workspace/aiwork.cmd. Browser sessions require BrowserSkill; virtual desktop operations require the DLL.

## 6. Optional activity console

    cd .\chatgpt-ui
    npm ci
    npm test
    npm run build

Use npm run dev for the Vite development server. A native Tauri build additionally requires Rust and the Windows desktop build prerequisites.

## 7. Optional Gmail integration

Gmail support is disabled until you configure your own Google OAuth Desktop client:

1. Create an OAuth Desktop client in your own Google Cloud project and enable the Gmail API.
2. Save the client JSON outside version control (the default path is config/gmail-oauth-client.json).
3. Run an interactive Gmail action once to complete consent. The token is stored locally at config/gmail-oauth-token.json.
4. Optionally set GPT_PC_GMAIL_EXPECTED_ACCOUNT to pin the account used by the bridge.

Optional email notifications require explicit configuration:
- GPT_PC_ULTRA_COMPLETION_EMAIL — destination for Ultra completion messages.
- GPT_PC_DISCONNECT_ALERT_EMAIL — destination for confirmed disconnect alerts.

No recipient is hard-coded. Do not commit OAuth client files, token files, or environment files.

## 8. Local configuration

Copy config/machine.example.toml to config/machine.local.toml and edit paths and host-specific settings. The local file is ignored by Git. See docs/CONFIGURATION.md.
