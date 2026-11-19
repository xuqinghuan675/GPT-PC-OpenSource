# GPT-PC local MCP service

pc-mcp/server.py is the local execution endpoint used by GPT-PC's remote tunnel bridge. It exposes Windows computer capabilities through MCP and applies the local Runtime's workspace/task controls.

- For remote web-AI operation, start the loopback Streamable HTTP endpoint with scripts/start-mcp-service.ps1, then connect a compatible external tunnel client using scripts/start-remote-tunnel.ps1.
- For direct local MCP use, run pc-mcp/server.py --transport stdio.

Features include workspace-scoped file/code/Git operations, process and background-job management, task state/control messages, visual context, and optional browser/Windows computer-use/Gmail integrations.

See the root README, INSTALL.md, and docs/REMOTE_TUNNEL.md for the complete bridge architecture and setup.
