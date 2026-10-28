# Security

AetherMind stores are project files. They can contain whatever a writer appends.
Do not commit a real `.aethermind/` directory, and do not paste private layer
bodies into a public issue.

Installing this package does not start a server and does not open a network
listener. The optional MCP script binds to `127.0.0.1` unless you set
`MCP_HOST` yourself.

Summary compaction has no built-in endpoint. It runs only when you pass a chat
URL or set `AETHERMIND_COMPACTOR_URL` and `AETHERMIND_COMPACTOR_MODEL`.

The optional project-key registry defaults to `~/.aethermind/project_registry.json`.
Set `AETHERMIND_REGISTRY` to another file if that path is already in use.
