# AetherMind MCP server

Optional. The library and CLI work without it.

```bash
pip install -e '.[mcp]'
python mcp/aethermind_mcp.py
```

Defaults:

- host `127.0.0.1` (`MCP_HOST` overrides it)
- port `8452` (pass another port as the first argument)
- `MCP_ALLOWED_HOSTS` is empty unless you set it

Run the script from this repository, or pass the script path explicitly.
The server does not start on install.

Example client config, with paths relative to this repository:

```json
{
  "mcpServers": {
    "aethermind": {
      "command": "python",
      "args": ["mcp/aethermind_mcp.py"],
      "env": {
        "MCP_HOST": "127.0.0.1"
      }
    }
  }
}
```

The writer must run on the host that owns the project files. Pass that
project's directory as `project_root`. Do not send a path from another machine.
