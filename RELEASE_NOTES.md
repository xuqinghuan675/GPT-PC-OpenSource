# GPT-PC 0.1.0 — initial public source release

## Included

- Windows-first local MCP runtime with workspace-scoped file, code, Git, process, job, visual, and optional computer-use tools.
- Optional desktop activity console built with Tauri, React, and Vite.
- Portable `GPT_PC_HOME` and per-user Windows configuration.
- Safe configuration templates and installation instructions.
- MIT license and third-party notices.

## Remote computer bridge

The README and architecture guide now describe the intended web-AI-to-Windows flow: remote control plane, authenticated outbound tunnel, loopback MCP endpoint, and GPT-PC execution controls. A generic launcher is provided for a separately supplied compatible tunnel client.

## Optional components

BrowserSkill, VirtualDesktopAccessor, PowerShell 7, and Gmail OAuth are configured per installation and are not bundled. See INSTALL.md.

## Validation snapshot

- Python backend and desktop helpers: 574 tests passed, plus 17 subtests.
- Frontend: 36 tests passed.
- TypeScript type-check: passed.
- Vite production build: passed.
- `npm audit`: 0 vulnerabilities reported for the resolved frontend dependency tree.

These results describe the release-preparation environment on Windows. Optional integrations still require their own installation and host-specific configuration.
