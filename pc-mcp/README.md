# GPT-PC MCP server

The MCP server exposes local Windows capabilities to an MCP-compatible host. The default transport is stdio; Streamable HTTP is available for local deployments.

## Features

- Workspace-scoped file, code, and Git operations.
- Process and background-job management.
- Runtime coordination, task-scoped control messages, and bounded activity history.
- Visual context and image handling.
- Optional browser and Windows computer-use integrations.
- Optional Gmail operations through a per-install Google OAuth client.

## Development

From the repository root:

    uv sync --project pc-mcp --extra dev
    .\pc-mcp\.venv\Scripts\python.exe -m pytest -q .\pc-mcp\tests

Start the server:

    .\pc-mcp\.venv\Scripts\python.exe .\pc-mcp\server.py --transport stdio

For configuration and optional integrations, see ../INSTALL.md and ../docs/CONFIGURATION.md.
