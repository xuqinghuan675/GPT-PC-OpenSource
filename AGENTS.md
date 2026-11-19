# GPT-PC contributor and agent instructions

- Read README.md, INSTALL.md, and the relevant component README before changing architecture or setup.
- Keep runtime state, logs, media, browser profiles, OAuth files, local machine configuration, and credentials outside tracked source.
- Use GPT_PC_HOME for the runtime root. Do not add machine-specific absolute paths or usernames to source, tests, or docs.
- Optional integrations must fail clearly when their external prerequisite is missing; core MCP startup must remain usable without optional browser, desktop, or Gmail components.
- Preserve third-party notices. Do not copy or redistribute external binaries unless their upstream license and version are documented.
- Add or update tests for behavior changes. Run the focused tests first, then the relevant full test/build commands.
- Keep MCP stdio stdout reserved for protocol traffic; diagnostic output belongs on stderr or in bounded local logs.
- Do not add hidden model reasoning capture. The activity console may display only explicit public progress summaries and sanitized tool activity.
- Do not commit local configuration, runtime state, tokens, OAuth credentials, or user data.
