# dsh example — registering this server via `mcp-registry`

The dsh desktop shell exposes registered MCP connectors through a bridge that
(as of dsh-mcp-registry 0.3.17) issues `tools/call` **without** an initialize
handshake; strict stdio servers answer `-32602: Invalid request parameters`.

`mcp_relay.js` (this folder) is a generic fix: wrap any stdio command with it.

1. Edit `~/.dsh/mcp-registry/registry.json` and add (or replace) a connector:

   ```json
   {
     "id": "anycubic",
     "name": "anycubic-mcp",
     "transport": "stdio",
     "command": "node",
     "args": [
       "C:\\path\\to\\anycubic-mcp\\examples\\dsh\\mcp_relay.js",
       "--", "anycubic-mcp"
     ],
     "env": { "PYTHONIOENCODING": "utf-8" },
     "permissionMode": "allow-all",
     "enabled": true,
     "pendingConfirmation": false
   }
   ```

   (`"command": "anycubic-mcp"` assumes the package was installed with
   `pipx install anycubic-mcp` and is on PATH; use the full path to the
   console script otherwise, or `uvx anycubic-mcp`.)

2. Restart dsh — the registry is only read at startup.

3. Verify from the agent: `mcp_registry_status` should list `anycubic`;
   `mcp_bridge_list_tools("anycubic")` should list the tools.

Set `RELAY_LOG=<file>` in the connector env for a byte-level debug log.
