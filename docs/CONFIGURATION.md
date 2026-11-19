# Configuration

## Runtime root

GPT_PC_HOME selects the local runtime root. If unset, GPT-PC uses the repository root.

PowerShell example:

    $env:GPT_PC_HOME = "C:\GPT-PC"

Runtime directories (runtime/, logs/, media/, chatgpt-agent-data/, and chatgpt-ui-data/) are local data and are ignored by Git.

## Machine settings

Copy config/machine.example.toml to config/machine.local.toml. Edit identity, paths, browser, and other supported tables for the local computer. The local file is ignored by Git. The parser rejects unknown fields and credential-like field names. Browser profiles must be dedicated GPT-PC profiles; ordinary Chrome/Edge user profiles are rejected.

## Environment variables

| Variable | Purpose |
| --- | --- |
| GPT_PC_HOME | Runtime root |
| GPT_PC_GMAIL_OAUTH_CLIENT | Path to the local Google OAuth Desktop client JSON |
| GPT_PC_GMAIL_OAUTH_TOKEN | Path to the local Gmail token JSON |
| GPT_PC_GMAIL_EXPECTED_ACCOUNT | Optional Gmail account pin |
| GPT_PC_ULTRA_COMPLETION_EMAIL | Optional Ultra completion notification recipient |
| GPT_PC_DISCONNECT_ALERT_EMAIL | Optional confirmed disconnect notification recipient |
| GPT_PC_DESKTOP_BROKER_OWNER | Optional Windows account override; defaults to the current user |
| GPT_PC_VDA_DLL | Optional path to VirtualDesktopAccessor.dll |
| GPT_PC_NATIVE_ACCELERATION | Enable/disable optional native acceleration |
| GPT_PC_CODEBASE_MEMORY_EXE | Optional code-navigation helper executable |

See config/example.env for a copyable list. The file is a template; GPT-PC does not automatically load it.
