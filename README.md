# GPT-PC

**GPT-PC** is a Windows-first local AI-agent runtime. It exposes local computer capabilities through the Model Context Protocol (MCP), coordinates bounded tasks, and provides an optional desktop activity console.

## What it includes

- **Local MCP server (pc-mcp/)** — file and code operations, Git, process and job management, visual context, browser/computer-use integrations, and optional Gmail workflows.
- **Desktop automation (desktop-workspace/, scripts/)** — a shared BrowserSkill CLI entry point, Windows virtual-desktop coordination, and per-agent background UI Automation.
- **Activity console (chatgpt-ui/)** — a Tauri desktop app for task activity, history, visual references, and task-scoped user corrections.
- **Portable configuration (config/)** — safe templates. Runtime state and credentials are created locally on each installation.

GPT-PC connects to an MCP-compatible host. It does not bundle a language model or require an OpenAI API key.

## Quick start

Requirements: Windows 10/11, Python 3.12+, and Git. The desktop console additionally needs Node.js 20.19+ and Rust tooling.

    git clone https://github.com/xuqinghuan675/GPT-PC-OpenSource.git
    cd GPT-PC-OpenSource
    uv sync --project pc-mcp --extra dev
    .\pc-mcp\.venv\Scripts\python.exe -m pytest -q .\pc-mcp\tests
    .\pc-mcp\.venv\Scripts\python.exe .\pc-mcp\server.py

See INSTALL.md for MCP host configuration, optional browser/desktop components, Gmail setup, and the activity console.

## Configuration

By default, runtime files are stored under the repository directory. Set GPT_PC_HOME to use another local data root. Each computer should create its own config/machine.local.toml, OAuth files, browser profile, and runtime state. Never commit credentials or runtime data.

See docs/CONFIGURATION.md.

## Project status

GPT-PC is an actively developed project. Some integrations are optional and require separately installed third-party tools; they are not bundled in this repository. See the installation guide for exact prerequisites.

## License

GPT-PC is released under the MIT License. See LICENSE and THIRD_PARTY_NOTICES.md for third-party components and attribution.
