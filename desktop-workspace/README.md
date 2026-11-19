# Desktop workspace tools

This directory contains optional Windows helpers for browser sessions and virtual-desktop coordination.

- aiwork.cmd is the common command entry point.
- aiwork.py uses the BrowserSkill CLI installed separately and reuses the current user's configured BrowserSkill profile.
- desktopctl.py coordinates Windows virtual desktops through the optional VirtualDesktopAccessor DLL.
- The DLL and BrowserSkill executable are not bundled. See INSTALL.md for upstream installation and verification.

The broker stores its per-user state under LocalAppData/GPTPCDesktopWorkspace. It defaults to the current Windows user; GPT_PC_DESKTOP_BROKER_OWNER can override that identity when required.
