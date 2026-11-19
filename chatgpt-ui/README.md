# GPT-PC Activity Console

The Tauri desktop console presents sanitized GPT-PC activity and task history. It supports task-scoped user corrections, attachments, and visual references.

It is a presentation and control surface for the existing MCP runtime. It does not host a model, create an agent loop, execute MCP tools directly, or capture private model reasoning.

## Development

    npm ci
    npm run dev
    npm test
    npm run build

For a native Windows build, install the Rust toolchain, Microsoft C++ Build Tools, and WebView2 Runtime, then run:

    npm run desktop:build

The presentation layer includes code derived from DeepSeek Harness; see THIRD_PARTY_NOTICES.md for attribution.
