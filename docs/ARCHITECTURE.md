# Architecture

    MCP-compatible host
           |
           | stdio (default) or loopback Streamable HTTP
           v
    pc-mcp/server.py
           |
           +-- Runtime: workspace leases, jobs, control inbox, task state
           +-- Local tools: files, code, Git, processes, images
           +-- Optional integrations: BrowserSkill, Windows UI Automation, Gmail
           |
           +-- sanitized activity events --> chatgpt-ui-data/
                                               |
                                               v
                                      Tauri activity console

## Components

- pc-mcp/ owns the MCP surface and local runtime. Persistent state is rooted at GPT_PC_HOME (defaults to the repository root) and written to ignored runtime directories.
- desktop-workspace/ contains the optional aiwork CLI and Windows desktop broker.
- scripts/ contains supporting local helpers, including Gmail OAuth and per-agent background input.
- chatgpt-ui/ is a Tauri/React activity console. It reads sanitized activity and task metadata; it is not a model runtime or a second agent loop.

## Boundaries

- Core MCP file/code/runtime features do not require BrowserSkill, the virtual-desktop DLL, or Gmail.
- BrowserSkill and VirtualDesktopAccessor are optional external dependencies and are not bundled.
- Gmail OAuth credentials and tokens belong to the local installation.
- The activity console displays explicit public progress summaries and sanitized tool activity, not private model reasoning.
