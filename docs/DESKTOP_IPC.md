# Desktop core launch contract

The Tauri desktop shell (`desktop/`) owns one Python core child and its loopback HTTP transport. The
core serves the packaged static UI, JSON API and SSE; it is not a public server.
No bearer token or login dialog exists. Requests must carry the bound loopback
Host (and, when present, the same Origin). Every resolved
bind address must be loopback, including when a legacy server token is present.

## Launch and readiness (version 1)

Use the installed Python interpreter selected by the desktop app. Arguments are
separate argv entries, without a shell. Global `--home` comes before `desktop`:

```text
<PYTHON> -m tessera --home <PRIVATE_HOME> desktop --host <LOOPBACK_ADDRESS> --port 0
```

`TESSERA_HOME` is an alternative to `--home`. `[server].host` and `.port` or
`TESSERA_SERVER_HOST`/`TESSERA_SERVER_PORT` supply defaults; command arguments
override them. The shipped defaults are IPv4 loopback and port zero, asking the
OS to assign an available port. IPv6 loopback is also supported. The app should
explicitly request port zero when it wants an independent child port.

Connect stdin, stdout and stderr as separate pipes. After binding the listener
and starting request handling, stdout emits one UTF-8 JSON line and flushes it:

```json
{"type":"tessera.ready","version":1,"transport":"loopback-http","url":"http://127.0.0.1:<ASSIGNED_PORT>","pid":1234}
```

The PID above is illustrative. `url` is the actual bound origin, with IPv6
brackets when appropriate. There is no token, config path, provider selector or
credential in the record. Diagnostics go to stderr; stdout has no further
protocol messages. Startup failure exits nonzero without a ready record. Main
must bound its readiness wait and handle early exit, malformed JSON or an
unsupported contract version as launch failure. Validate the returned numeric
loopback origin and owned child PID before using it; do not accept a remote URL.

Keep stdin open for the lifetime of the app. Closing it requests graceful
shutdown, including on parent exit. SIGTERM or an interrupt also requests
shutdown with exit status zero after readiness. The core stops serving, closes
SSE streams, drains accepted work and releases its home lock. Work uses configured
provider/tool timeouts; the app may apply a separate bounded termination fallback.
Stdin is a lifetime pipe, not a general command or secret channel. Each child
needs its own home; another core cannot take the same home lock.

For manual diagnostics, `serve` has the same loopback confinement and no bearer
requirement. It prints `Serving Tessera at <ACTUAL_ORIGIN>` instead of the JSON
launch protocol, runs until interrupted, and does not use stdin ownership.

## Local requests and events

Load the static page from the returned origin. The window can issue same-origin JSON
requests to that origin and bridge replies to the isolated renderer through its
preload API. A `file://` renderer should use that main-process bridge. The core
does not supply a browser CORS/preflight or remote-authentication flow.

- `GET /` and `/static/<asset>` serve the bundled UI with traversal/symlink fences.
- `GET /api/status` is passive; it neither resolves provider credentials nor mints
  voice access. Existing session, board and integration status projections remain
  available.
- `POST /api/sessions` with `{"title":"..."}` creates a session. Existing JSON
  routes retain their schemas and body limits; include `Content-Type: application/json`.
- `POST /api/sessions/<slug>/messages` accepts `{"text":"..."}` and returns a
  `turn_id`. `GET .../messages`, `.../view` and `.../zoom` expose saved results.
- `GET /api/sessions/<slug>/events` streams SSE. Replay with `Last-Event-ID`; records
  contain integer IDs, event types and JSON data. Close streams when leaving a
  session or shutting down. No Authorization header is needed.

There is no HTTP authentication. A legacy `server.token_env` in an old config is
ignored; a stray `TESSERA_SERVER_TOKEN` value is only redacted from output. The
core accepts requests whose Host is the bound loopback address and whose Origin
is absent or that same origin (the desktop window loads the core origin), and
answers 403 otherwise. This is a rebinding/cross-site fence, not a credential. Provider
credentials still use explicit env or `credential_ref` settings in the private
home; never put credential values in TOML, argv, readiness records or evidence.

## Settings

`GET /api/settings` returns the typed settings projection: sections, and for each
field an id, section, type (`enum`, `bool`, `int`, `float`, `string`, `secret`,
`list-of-records`), current value, allowed values or constraints, the write
action, and whether the change applies live or needs a restart. Secret fields
are empty. Credential values are never included.

`POST /api/settings` with `{"id","value"}` or `{"id","action","name","record"}`
validates the change, writes `config.toml` by temp file and rename after a
timestamped backup, and reloads it into the running core when it can. The
response has `restart_required` and `applied` (`live` or `restart`). A secret
in a credential record is stored through the keychain helper and is not written
to config, returned, or logged. Each provider row names the real mechanism,
such as `via grok CLI` or `API key`. Tessera does not start a vendor login and
does not present one as a native sign-in. The shell does not add a second
command channel for this: the window is the core origin, so these requests are
the desktop IPC.

`config-changes.jsonl` and `config-observed.json` sit beside `config.toml`.
The change log records each ConfigEditor save and each later write that did not
go through ConfigEditor. The first observation is a baseline and is not listed.
A secret value is recorded as `changed (not shown)`. A removed key is `removed`.
A value longer than 400 characters is cut and marked `… (truncated)`.
`ui.config_journal_entries` (default 40) is how many newest records the Settings
page shows. The page says `Showing N of M`, and the file is then kept to that
many lines. `config-observed.json` holds the hash and redacted tree of the last
observation so a later outside write can be diffed. It is not a second config.

`POST /api/config/restore` with `{"name":"config.before-...toml"}` validates that
backup, replaces `config.toml`, and keeps a new backup of the file it replaced.
The response is the settings envelope: `changed`, `restart_required`, `applied`,
`backup`, `restored`, `detail`, and `settings`.

`GET /api/aethermind/engine-log` returns the tail of the packaged engine log,
bounded by `aethermind.engine_log_max_bytes`. The response includes `cap` (that
byte limit), `available`, and `text`. Secret values are replaced with
`[redacted]` before the text leaves the core, whatever their length. A missing
log is `available: false` with `missing: true` and `path`. An install with no
bundled engine is `available: false` with `reason`, because no log was expected
there. A path that is not a regular file, such as a directory, returns `error`
naming what it is. An unreadable log, or a missing `build-receipt.json` under an
explicit bundled path, returns `error` in the form
`could not read engine log at <path>: <error>`.

`POST /api/trials` accepts an optional `margin` greater than 0 and at most 1.
When it is omitted, a `turn_seconds_median` trial stores the duration margin and
every other instrument stores the rate margin. Both defaults are 0.8. The page
shows the margin the decision will use. An unusable stored margin is flagged,
and the decision uses that instrument's default.

## Voice ownership

Voice remains disabled until configured and explicitly started. On
`POST /api/sessions/<slug>/voice/start` with `{}`, Python resolves the configured
credential, performs the bounded upstream mint and returns an expiring lease
with `voice_id`, configured transport/audio settings, `session_update` and
ephemeral connection protocols. The long-lived provider key stays in Python;
it is never returned, pasted by the user or fetched from the provider by page JS.

The page receives this response through the owned local API and can keep
the ephemeral protocol and provider socket in main, exposing only required
audio/events to the renderer. Do not persist or log the ephemeral access either.
Forward supported completed transcript/function events through
`POST .../voice/events` with `{"voice_id":"...","events":[...]}`. Stop with
`POST .../voice/stop` and `{"voice_id":"..."}`; local tracks/playback/socket must
also close in the desktop app. Lease expiry, session scoping, replay checks,
tool policy and provider timeout behavior remain enforced by Python.

See [SETUP.md](SETUP.md) for optional provider/voice configuration and
[README](../README.md#persistent-sessions-and-desktop-core) for session storage.
Offline subprocess/HTTP/SSE/mint fixtures validate this contract; they do not
claim live provider transport, microphone or speaker acceptance.
