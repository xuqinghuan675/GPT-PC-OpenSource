# GPT-PC 0.1.0 — initial public source release

## Included

- Windows-first local MCP runtime with workspace-scoped file, code, Git, process, job, visual, and optional computer-use tools.
- Optional desktop activity console built with Tauri, React, and Vite.
- Portable `GPT_PC_HOME` and per-user Windows configuration.
- Safe configuration templates and installation instructions.
- MIT license and third-party notices.

## Optional components

BrowserSkill, VirtualDesktopAccessor, PowerShell 7, and Gmail OAuth are configured per installation and are not bundled. See INSTALL.md.

## Validation snapshot

- Python backend: 549 tests passed.
- Frontend: 36 tests passed.
- TypeScript type-check: passed.
- Vite production build: passed.
- `npm audit`: 0 vulnerabilities reported for the resolved frontend dependency tree.

These results describe the release-preparation environment on Windows. Optional integrations still require their own installation and host-specific configuration.
