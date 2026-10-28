# Phone access over the tailnet (HTTPS)

The core binds loopback only. `tailscale serve` (tailnet-only, using a tailnet
certificate) terminates HTTPS and proxies to it. Never use `tailscale funnel`.

1. List the proxy name in the private config or environment (empty by default):
   `TESSERA_SERVER_ALLOWED_HOSTS='["<mac>.<tailnet>.ts.net"]'`. Requests whose
   Host is that name pass; a present Origin must be `https://` that same name.
   Every other Host/Origin still gets 403.
   Prefer `[server].allowed_hosts` in the private `config.toml` over the env
   var: a core started by the desktop app (desktop.toml `keep_core`) inherits no
   shell or launchd environment, so an env-only allow-list silently breaks the
   tailnet front door the first time the app, not launchd, starts the core.
2. Run the core on a fixed loopback port (`TESSERA_SERVER_PORT=<port> python -m tessera serve`).
3. `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`; check
   `tailscale serve status` first and do not replace an existing handler.

The UI uses relative URLs and same-origin SSE, so it works unchanged behind the
proxy in a secure (HTTPS) context; the mobile layout applies on phones.

## What the front door exposes

`tailnet-only` is not a security boundary for the board API, and it must not be
described as one. Every board write route runs as the operator: the acting
session is dropped before the ownership guard, so `actor` arrives as `None` and
`board.py` treats that as the operator. A tailnet device that curls the front
door can therefore strip another session's claim, revise its note, move its
status, answer a pending human question, or file and remove a card. The
`_check_local_request` Host/Origin check stops a browser page from another origin;
it does not identify anyone, and it does not stop a direct request carrying the
allowed Host name.

The core still binds loopback only, so the listener itself is local. The proxy is
what widens reach. Per-session ownership is enforced on the tool path
(`board_tools.py` passes `runtime.slug`), and it is a coordination rule there,
not authentication. The HTTP board routes do not carry the acting session.
Do not add a hostname to `allowed_hosts` on the assumption that the tailnet
makes the API safe.
