# Desktop workspace usage

Use aiwork.cmd as the common entry point for optional browser and Windows desktop tasks.

## Browser

    aiwork.cmd browser start --agent task_001 --url https://example.com
    aiwork.cmd browser observe --agent task_001
    aiwork.cmd browser stop --agent task_001

Agent IDs must be unique among concurrent tasks. BrowserSkill is installed separately; see INSTALL.md.

## Virtual desktops

    desktopctl.cmd status
    desktopctl.cmd allocate --agent task_001 --mode visual
    desktopctl.cmd release --agent task_001

Virtual desktop commands require the optional VirtualDesktopAccessor DLL. Set GPT_PC_VDA_DLL if it is stored outside this directory.

## Background input

The scripts/aiwork_input helpers use the current Windows account and per-user LocalAppData. They do not ship authentication keys; the local broker creates its own key on installation.
