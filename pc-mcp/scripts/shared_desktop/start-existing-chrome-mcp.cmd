@echo off
setlocal
REM Official Playwright MCP extension mode; operates the user's existing Chrome profile.
REM Requires the Playwright Extension 0.5.0+ to be installed in Chrome first.
cd /d "%~dp0"
call npx --yes @playwright/mcp@0.0.84 --extension --background --profile-dir-name Default --host 127.0.0.1 --port 8931
