# Configure a private Tessera home

For optional judge readiness and evidence-bound advisory reviews, see
[Jev readiness and advisory evidence review](JEV.md).

Tessera is a single-owner Python harness with its own loop. It does not install
providers, remote services or a background daemon. Python 3.11+ and the standard
library run the harness; pytest is needed only for tests. Install from the cloned
repository's root, where `pyproject.toml` and `config.example.toml` are located.
No global package or service installation is required.

On Debian and Ubuntu, `python3` does not include `venv` or `pip` — `python3 -m venv`
creates an interpreter with no `pip` and no `ensurepip` module, so the install below
fails at the second line. Measured on a clean Debian 13 machine. Install the
packaging support first:

```sh
sudo apt-get install -y python3-venv python3-pip
```

```sh
python3 -m venv .venv
.venv/bin/python -m pip install "git+https://github.com/MaverickKB/Tessera-CT.git"
export TESSERA_HOME="<PRIVATE_CONFIG_DIRECTORY>"
.venv/bin/python -m tessera start
```

No clone is needed; pip fetches and builds the package from the repository.
`start` creates the config when missing, materializes the verified engine
bundle, seeds an empty store, starts the core, and opens the app in a browser.
`init` remains available when you want to create the config alone and edit it
before the first run; run `start` afterward to launch.

This editable install keeps source changes visible and registers the `tessera`
command inside the venv. `python -m tessera` also works outside the checkout.
To put Tessera in the app menu (Linux), Applications (macOS) or the Start Menu
(Windows), run `tessera install-launcher`; the launcher runs `start`.
Wheels bundle the same configuration template and static UI. Setuptools is a
build dependency, not a runtime dependency; an offline install needs a trusted
local copy of the build requirement declared in `pyproject.toml` (use pip
`--no-index --find-links "<WHEELHOUSE_DIRECTORY>"`). Install pytest separately
only to run the tests. See the [README installation steps](../README.md#install-and-run).

After cloning, run `scripts/install-git-hooks.sh` once. It sets
`core.hooksPath=scripts/git-hooks`, which turns on the tracked pre-commit guard:
a commit made on a detached HEAD is refused, because such a commit is reachable
only through the reflog and a `gc` would silently discard it. Pass
`TESSERA_ALLOW_DETACHED=1` to commit on purpose while detached. The hooks live in
the repository rather than in `.git/hooks` so a fresh clone keeps them.

Replace every angle-bracket placeholder before use. Keep this home outside the
checkout with owner-only permissions: it stores config, transcripts, artifacts
and optional integration journals. `init` copies [config.example.toml](../config.example.toml)
and refuses to overwrite existing config. The example includes empty
`credentials.gateway`/`credentials.voice_api` selectors plus disabled `[swarm]`,
`[board]` and `[voice]` sections. Never put credential values in TOML.
Provider placeholders must be replaced before running a turn. An empty provider
inventory starts no network requests and cannot execute a model turn. Swarm,
voice and other optional integrations default to disabled.

## Who can reach the core, and what the shared key defends

The core binds loopback only, and that is enforced in code, not merely documented:
`tessera/server.py` refuses any non-loopback bind at startup. So on a desktop install
nothing on the network can connect at all, and there is no second credential boundary
inside the app — anything running as your OS user (a browser tab, another app, a script)
reaches every route with full operator authority. That is the same trust you already
extend to your own shell, and for a single-user desktop tool it is the intended posture.

The shared key exists for the one case where that changes: a phone or another app
reaching the core **through a proxy** named in `server.allowed_hosts` (for example
`tailscale serve`). A key on a loopback-only core would defend nothing, because a local
process can read `config.toml` and would simply use it. A key on a proxied request is
exactly what protects the tailnet path.

Two settings, in `[server]`:

- `auth_token` — the shared key. Empty (default) means no auth at all, which is the
  historical desktop posture and is unchanged by this option.
- `auth_mode` — `"loopback"` (default) requires the key **only** on requests that arrive
  through an allowed proxy. Direct loopback requests stay open, so the local web page,
  the desktop window and the restart scripts cannot be locked out by a wrong or missing
  key. `"always"` requires it on every `/api/` request, for a genuinely shared machine;
  every local client then needs the key, and scripts read `TESSERA_KEY`.

Clients can present the key three ways, because no single form covers them all:

| Form | Used by |
| --- | --- |
| `Authorization: Bearer <key>` | the iOS app (it already sends this) |
| `X-Tessera-Key: <key>` | the web UI, scripts, the desktop readiness probe |
| `?key=<key>` on the URL | the live transcript stream — `EventSource` cannot set headers |

The web UI stores the key when you open the core once as `/?key=<key>` and strips it from
the address bar afterwards. Static UI files are served without the key so a missing or
wrong key shows an explanatory error rather than a blank window.

What the key does **not** do: it does not protect against another process running as your
OS user, and it is not a substitute for a per-user auth layer. If Tessera is ever run as a
shared service or a second OS user is expected to use one instance, the honest fix is a
per-user identity layer, not a shared bearer key.


[docs/reference/CONFIG.md](reference/CONFIG.md) is the authoritative list of
every configuration table, with each key's declared default and its `TESSERA_*`
environment override name (or an explicit indication when none exists).
This walkthrough covers the sections a first install is most likely to change:
credentials, accounts, backends/roles, swarm, board, voice, and the other sections
already explained below.

The remaining sections are listed here for discovery; use the generated reference
for their keys. “Leave default” means no first-install configuration is required
for basic chat; it does not mean the integration is active.

| Section | Purpose and first-install choice |
| --- | --- |
| `aethermind` | Persistent memory and recall. The packaged engine is on by default. Set a service URL only to use an external store, and only after that store is reachable and accepts its auth; an invalid external service leaves the packaged engine running and reports the error. |
| `agents` | Sub-agent delegation: how many children may run, how deep they may nest, their token and wall-clock budgets, and the backend policy that picks a worker per capability. Leave default (disabled) until you want delegated work. |
| `autonomy` | Event-driven wakes and background tasks. Leave default (disabled); configure to enable autonomous work. |
| `channels` | Inter-session message delivery and replies. Leave default (disabled); configure to enable messaging. |
| `codex` | Where ChatGPT or Codex sign-in lists models: the catalog URL and the client version sent with that request. Leave default; change them on the Models page under Advanced. |
| `companions` | Orientation and continuity companion connections. Leave default (both disabled); configure endpoints to use them. |
| `compactor` | Background summarisation of long sessions: worker count, retries, and whether a judge gates the result. Leave default (judge disabled); raise `workers` only if compaction is slow. |
| `computer_use` | How the companion drives a desktop: the provider (`native`, `jev` or an MCP server), the tool prefix and the timeout. Leave default for a text-only install; set `provider` to use screen and pointer tools. |
| `cua` | The macOS native helper the `computer_use` lane calls: helper app, bundle id, screenshot and input commands, image size cap. Leave default (disabled); this is platform wiring, not a preference. |
| `daemon` | Frame and queue limits for the desktop bridge that carries tool calls between the app and the core. Leave default unless you are diagnosing a large-payload or backlog problem. |
| `grafana` | Grafana dashboards and datasource access. Leave default (disabled); configure your service to use it. |
| `hooks` | Shell commands run around engine events, each entry naming an `event` and a `command`. Leave default (empty); add hooks only for automation you want on every matching event. |
| `hosts` | Named machines for infrastructure probes and metrics. Leave default (empty inventory); add hosts when monitoring machines. |
| `identity` | The companion's identity records and whether the operator is recorded as an identity. Leave default (disabled); enable it to keep a named identity journal. |
| `infrastructure` | Infrastructure snapshots and SparkDash/Prometheus collection. Leave default (disabled); configure sources to collect status. |
| `inspector` | Local inspection socket and controlled inspection requests. Leave default (disabled); configure to enable inspection. |
| `intake` | Gathering outside artifacts (pull requests) into intake cards: how many to read per gather and how long to wait for the provider. An intake card is not work — it is not claimable and wakes no worker until someone triages and accepts it. The collector names one repository and needs the GitHub CLI (`gh`). Leave default until you want PRs on the board. |
| `jev` | The companion's judge for risk, goals and action gates. Leave default for basic chat (disabled, no service/model); configure and enable it when judgment is required. |
| `mcp_servers` | External MCP connections that supply the companion's tools. Leave default for basic chat (empty inventory); add a URL or command for each server whose tools you need. |
| `prompt` | The standing instructions the companion receives: memory guidance, skill guidance, the turn reminder, delegation wording, and whether a body map is included. Leave default; edit only to change how the companion talks to itself. |
| `remotes` | Named SSH machines and optional remote desktop access. Leave default (empty inventory); configure targets before remote operations. |
| `skills` | Agent Skills shelf and trusted project skill discovery. Leave default (enabled, using the home skills shelf); configure for a different shelf or trusted projects. |
| `status` | Caps on what the status view reads: task limit, per-file and total byte ceilings, log count. Leave default unless a status read is being truncated. |
| `systems` | Purpose-bearing registry: what each system is for and the thresholds the autonomy floor uses for it. Leave default (empty); add a record when you want to declare a host's purpose or change a floor threshold instead of editing the application. |
| `tools` | Tool approval posture (`ask`, `deny_risky`, `never`, or `yolo`). The UI calls `yolo` Unleashed. Also which tool classes are gated, and the output and read limits for every tool. Leave default (`ask`); change this deliberately, because it decides what runs without asking. |
| `ui` | Presentation defaults: theme, skin, accent, page sizes, banner timings, the send shortcut, and the optional F-it button label. Leave default; these change how the page looks, not what it does. |
| `view` | Byte budgets for each view tier (node, high, low, prompt) that decide how much context a reader is shown. Leave default unless you are tuning context size. |

The snippets below show operator-enabled configurations. **Edit or replace the
matching tables** in the copied file rather than appending duplicate tables;
TOML rejects duplicate headers. Add only tables that do not already exist.
For a basic chat, leave all optional features off. See [README](../README.md)
for architecture and install/run commands.

### First setup in Settings

A fresh landing with no configured chat model opens **Getting started**. Existing
configured installs retain their normal landing, and explicit session/page links
are respected. You can reopen Getting started from Settings at any time or choose
**Use settings without the guide** to use the complete editor.

Choose the connection you already have:

1. **Sign in** uses the existing supported provider sign-in flow. Continue to the
   account’s model list and choose its **Chat model** option.
2. **Use an API key** saves the key in the core computer’s OS credential store,
   then carries that saved reference into the model form. Internal credential and
   backend names are generated without replacing existing entries. If the key is
   already in the core’s environment, use **Use an environment variable instead**
   and enter its variable name. On Linux, pasting a key requires a working
   `secret-tool`/Secret Service store; the environment alternative does not.
3. **Use a local model** asks for the server address and model ID. The server must
   already be running and reachable from the computer running Tessera. This does
   not download or start a model. A phone’s localhost is the phone itself; the
   model address entered in workspace Settings is resolved by the core computer.

**Find models** reads the chosen server’s catalog on request. If it does not offer
one, enter the model ID from that provider’s documentation. The HTTP protocol is
under Advanced. **Save and use for chat** saves the model and explicitly selects
it as the main chat model; worker roles and other settings are unchanged. If you
already saved a key, the API-key connection step offers **Use saved key**.
The guide carries that connection into the model form; models do not ask you to
choose credentials.

**Send test message** creates a saved “Getting started” conversation using that
model and submits one short greeting through the normal session/turn API. Nothing
runs merely because you opened the guide. The test uses the same harness,
permissions, memory, and event stream as a normal chat. Read the actual reply in
Chat; saved configuration and an accepted submission are not proof of a reply.
The conversation remains available to continue with a useful task. Within the
same open page, a failed or uncertain submission can be retried without
duplicating an accepted request.
Correct a provider or model in Settings and send a new test after changing it.

Scalar settings save when changed and show saving, saved, restart-required, or
error feedback beside the control. Record editors use Save and Cancel. Moving
between settings sections keeps unfinished record drafts in this page’s memory;
return to that section to finish or cancel. Reloading or closing the app does not
persist drafts. Removing a saved credential requires an explicit confirmation.

The browser and desktop shell use the same settings page. Platform-specific
lifecycle controls are available only when their host supports them; a desktop
bundle still needs the Python checkout described in `desktop/README.md`. Native
Windows embedded memory is unsupported; use WSL for that runtime. The iPhone
Connection sheet configures access to an already running core, while workspace
Settings configures that core. Use a reachable private server address and the
server’s optional access token, then Test connection or Save and connect.
Configure private networking on the host before connecting from the phone.

### What the Settings page edits, and what it does not

The Settings page is catalog-driven, and the catalog does not cover the whole
file. Of the 46 top-level configuration attributes,
**19 are reachable from the page** and **27 are not**.
That split is not a bug report; it is the shape of the product today, and this
paragraph exists so nobody has to infer it from a missing control.

Reachable from the page (`ui`, `aethermind`, `jev`, `identity`, `voice`,
`autonomy`, `computer_use`, `hosts`, `systems`, `roles`, `credentials`, `mcp_servers`,
`backends`, `accounts`, `swarm`, `agent_name`, `operator_name`, `codex`, `tools`).
`hosts`, `systems`, `credentials` and `mcp_servers` are record editors: you can add,
edit and remove rows. `backends` is reached by the Models editor and `accounts` by
Service accounts, so those two have named-entry controls rather than a single field.
`swarm` is reached only partially: the page edits the two local-engine admission
fields, and the rest of the table still means editing `config.toml`.
`tools` is reached only partially too: the page edits the Safe policy, and the
rest of the table is tool limits.

Not reachable from the page: `home`, `cwd`, `config_missing`, `workspace`,
`user_instructions`, `compactor`, `view`, `hooks`, `agents`, `server`,
`daemon`, `status`, `channels`, `board`, `intake`, `remotes`,
`infrastructure`, `grafana`, `companions`, `inspector`, `cua`, `prompt`, `skills`,
`inspector_error`, `identity_file`, `identity_max_bytes`, `retired_settings`.

Editing those means editing `config.toml` and restarting the core. Five of them
are not settings at all and will never have a control: `home`, `cwd` and
`config_missing` are derived at load time, and `identity_file`/`identity_max_bytes`
are paths and caps the identity layer reads directly. The rest are configuration
a person can reasonably want to change, and the absence of a control is real.

The Settings page now states this itself: a "Set in config.toml" section lists every
one of these names with the reason it has no control. The reasons come from
`FILE_ONLY_REASONS` in `tessera/settings_model.py`, the same table the page serves, so
a table added to Config without a reason makes the projection refuse rather than
silently going undisclosed.

[The generated reference](reference/CONFIG.md) is the authority for any single
key: its Availability column says `Settings row` or `file-only` per key, and that
column is generated from the same catalog the page renders. `tests/test_settings_disclosure.py`
fails if this list and the catalog disagree.

Two files sit beside `config.toml`. `config-changes.jsonl` is the config change
log: ConfigEditor saves, restores, and writes that bypassed ConfigEditor. The
Settings page lists the newest `ui.config_journal_entries` records and says
`Showing N of M`. `config-observed.json` is the redacted snapshot the page
compares against the next outside write. `POST /api/config/restore` puts a named backup back.
`GET /api/aethermind/engine-log` returns the packaged engine log tail, bounded by
the Engine log cap, and names whether the log is missing, unreadable, or not
expected in this install.
`POST /api/trials` stores the win margin on the trial. Omit it and a duration
trial keeps the duration margin; any other instrument keeps the rate margin.
Both defaults are 0.8. An unusable stored margin is shown as that default and
flagged.


The desktop app spawns the Python core on loopback with an assigned port and
no bearer-token prompt. The optional `[server]` table in the example selects
loopback and port zero. See [DESKTOP_IPC.md](DESKTOP_IPC.md) for the launch argv,
ready JSON, local JSON/SSE bridge and child shutdown contract. `serve` remains
a foreground diagnostic command; non-loopback listeners are refused.

Every CLI command accepts `--home "<PRIVATE_CONFIG_DIRECTORY>"` **before** the
subcommand as an alternative to `TESSERA_HOME`. Configuration strings do not
expand shell variables. Named-section overrides use double underscores, such as
`TESSERA_BACKENDS__GATEWAY__MODEL="<MODEL_ID>"`; ordinary section fields use
`TESSERA_VOICE_ENABLED=false` or `TESSERA_SWARM_ENABLED=false`. Environment values
use TOML types when possible. Credential environment variables contain values;
the config names those variables, never their contents.

## HTTP providers and credentials

Replace the example backend/roles tables and edit `credentials.gateway` for the
pools and backends you actually use. This example fits an existing Spark-style
OpenAI-compatible inference gateway; it does not manage GPUs or start training
or model services.

```toml
[credentials.gateway]
env = "YOUR_GATEWAY_API_KEY"
# Optional OS-store fallback; use both selectors together:
# keychain_service = "<KEYCHAIN_SERVICE>"
# keychain_account = "<KEYCHAIN_ACCOUNT>"

[accounts.gateway_pool]
provider_kind = "openai_chat"
credential_ref = "gateway"
limits = { max_concurrent = 1 }
budget = { tokens = 32768 }

[backends.gateway]
account = "gateway_pool"
base_url = "https://models.example.com/v1"
model = "<MODEL_ID>"
timeout = 30
retries = 0
parameters = { max_tokens = 256 }

[roles]
main = "gateway"
fast = "gateway"
compactor = "gateway"
```

Credential lookup is explicit env selector, then the named reference's env,
OS Keychain (macOS `security`, or Linux `secret-tool` when available), then its
`config_ref` pointing to another named credential reference. `config_ref` is not
a plaintext-secret field or a file to read. Missing sources fail clearly. For a
gateway that explicitly needs no auth, omit the credential reference rather
than inventing a key. Credential resolution happens on execution, not status.

`openai_responses` and `anthropic_messages` are also selectable account provider
kinds. Set each backend's base URL, model, timeout and request parameters for
that provider. Anthropic also requires `api_version` and an output-token bound
such as `parameters = { max_tokens = 256 }`. Endpoints are not discovered or
probed automatically. HTTP redirects are refused to prevent credential forwarding.

## Official CLI workers

Install and sign into an official supported vendor CLI yourself. Tessera starts
it under your existing login and never extracts or replays its OAuth tokens.
Add this pool/backend to the same private config for a separate worker lane:

```toml
[accounts.cli_pool]
provider_kind = "cli_worker"
limits = { max_concurrent = 1 }
budget = { tokens = 32768 }

[backends.cli]
account = "cli_pool"
[backends.cli.cli]
vendor = "codex"
command = "codex"
cwd = "<WORKER_DIRECTORY>"
timeout = 60
max_output_bytes = 1048576
args = ["exec", "--json", "--sandbox", "read-only", "-"]
```

For Claude select `vendor = "claude"`, its installed command, and
`args = ["--print", "--output-format", "json"]`. Only fresh noninteractive
JSON/stdin invocations are supported; resume/continue flags are rejected.
Configure the vendor's execution permissions deliberately. The prompt's scope
is not a sandbox. API keys belong on HTTP adapters; CLI accounts reject API
credential references and configured key environment selectors are stripped
from child processes. Timeouts kill the subprocess group; output is bounded.

For Codex, set `model` on the backend itself. Tessera passes it to the official
CLI. With `cli.engine_tools = true`, Codex returns a structured dispatcher reply;
Tessera executes the requested tools and supplies their results on the next
call. Native Codex execution and fan-out follow the configured CLI options.
The integration does not force feature overrides. A native read-only sandbox
describes Codex's own process; advertised engine tools execute in Tessera's host
process under its current policy. Children inherit that policy and the parent's
approval-request channel. Workers report actual tool results rather than infer
an engine permission denial from native sandbox metadata.

Worker selection is configured with `agents.backend_policy`. Each key names an
existing backend and carries `capability` (`routine`, `standard` or `complex`),
`cost_rank` (lower is preferred) and an optional description. These are operator
rankings, not dollar prices or measured quality scores. `agent_backends` shows
the live choices; `spawn_agent` accepts a task capability and selects the
cheapest eligible backend. An explicit `backend` overrides automatic selection.
Use routine for narrow inspection or simple edits, standard for implementation
and debugging, and complex for difficult architecture or reasoning. Escalate
when the task needs it or a cheaper attempt fails. Saved sessions share one
subagent capacity pool, and stopping a parent also cancels its descendants.

## Per-session thinking budget

The chat header's Thinking menu offers an optional token-budget slider. Its
default uses the engine's own setting. A saved budget applies to each subsequent
model response; turning reasoning Off suspends it without losing the saved value.
The control leaves autonomous session duration and tool-loop continuation alone.

Supporting engines receive the native `thinking_budget` parameter. For an explicit
budget, Tessera adds that many tokens to a configured `max_tokens` or
`max_completion_tokens` allowance so thinking does not take that allocation away
from the answer. The engine's context window and total-response limits still apply.
The current Mia TensorFold deployment closes the thinking block at the budget and
continues the answer; verify that behavior before using this control with another
engine. `PATCH /api/sessions/<slug>` accepts `thinking_budget` as a positive integer
or `0` to return to the engine default.

## Explicit swarm jobs and the board

```toml
[swarm]
enabled = true
max_attempts = 1
lease_seconds = 120

[board]
enabled = true
```

Create an existing saved session through the UI, then save a private JSON spec:

```json
{
  "objective": "Join two independent arithmetic checks",
  "workers": [
    {"backend": "gateway", "objective": "Return 8 times 7", "scope": ["check/alpha"], "budget": {"tokens": 4096}},
    {"backend": "cli", "objective": "Return 81 minus 17", "scope": ["check/beta"], "budget": {"tokens": 16384}}
  ]
}
```

```sh
.venv/bin/python -m tessera swarm-create "<PRIVATE_SPEC_FILE>" --session "<EXISTING_SESSION_SLUG>"
.venv/bin/python -m tessera swarm-run "<RETURNED_JOB_ID>"
.venv/bin/python -m tessera swarm-show "<RETURNED_JOB_ID>"
```

Stop any server holding that home's manager lock before these standalone CLI
commands. Creation persists the job; `run` launches one bounded wave explicitly.
Inside a live session, use the `swarm_job` engine tool with actions `create`,
`run`, `status` and `artifact`. It uses the existing owner instead of opening a
second manager on the same home. Jobs collect response-only evidence; use
`spawn_agent` for workers that must execute tools or implement code. Job worker
specs may omit `backend` and declare `capability` to use the same cost policy.
Inspection never invokes providers. Worker backends and accounts must be distinct,
scopes must be disjoint, and gates verify the ordered artifact merge. The board
links artifacts and source log ranges. No background retry loop is started.

Token caps admit reservations against cumulative account usage; reserve enough
for the entire next task, including CLI prompt overhead. They are not guaranteed
generation limits or cost estimates. Set provider output limits separately.
Missing usage is conservatively charged against the reservation, not reported
as measured zero. Preserve failed runs and their honest usage. Passive UI/status
fields contain sanitized summaries; raw transcripts and artifacts stay private.

## Optional realtime voice

Tessera has two voice pipes and runs one at a time. Full duplex uses one model
that hears and speaks, and keeps the session history and tools. Local speech
uses a speech-to-text service and a text-to-speech service; an utterance becomes
a normal turn in the session and the reply is spoken back.

Settings, under Voice, sets up both. Duplex needs an account: a sign-in under
Providers, or a provider row pointed at the provider's endpoint. Choosing the
account fills in the endpoint, the token service and the protocol prefix from the
provider profile, so nobody types them. The model is a dropdown of the models
that account offers. Two duplex providers are built in: xAI Grok and OpenAI
Codex. The OpenAI path binds to the Codex sign-in the app already holds and uses
the GA Realtime shape: the browser pairs the `realtime` subprotocol with the
`openai-insecure-api-key.` token, voice sits under `audio.output`, and turn
detection and transcription sit under `audio.input`. OpenAI publishes its voice
roster rather than serving a voice-list endpoint; xAI's roster is probed from
`GET /tts/voices`. Local speech services are endpoints that speak the OpenAI
audio API; saving an endpoint lists the models it serves, and the model field is
a dropdown of those names. A pipe choice appears only when both pipes are
configured, and duplex is the default.

The example below deliberately uses placeholders. Set `turn_detection`
explicitly if your provider requires it; the copied template leaves it empty.

Local speech is push-to-talk on the Voice page: hold Hold to talk, speak, and
release. The page sends the recording to Tessera, which asks the speech-to-text
service for a transcript, runs it as a normal session turn, and asks the
text-to-speech service for the reply, then plays it back. The speech services
must answer the OpenAI audio API: `POST /audio/transcriptions` with a WAV file
and `POST /audio/speech` with JSON. A service that returns anything other than
PCM16 WAV from `/audio/speech` is refused with a clear error rather than played
as noise. `[voice.tts].voice` is passed through when the service needs a voice
name; `local_reply_timeout` bounds how long one push-to-talk request waits for
the turn, and `local_max_audio_bytes` bounds the recording size.

```toml
[credentials.voice_api]
env = "YOUR_VOICE_API_KEY"

[voice]
enabled = true
pipe = ""
websocket_url = "wss://voice.example.com/realtime"
model = "<REALTIME_MODEL_ID>"
api_base_url = "https://voice.example.com"
client_secret_path = "/<CLIENT_SECRET_MINT_PATH>"
client_protocol_prefix = "<EPHEMERAL_PROTOCOL_PREFIX>"
client_protocol_extra = []
session_shape = ""
transcribe_model = ""
credential_ref = "voice_api"
max_session_seconds = 60
secret_ttl_seconds = 60
timeout = 5
max_sessions = 1
audio_format = "audio/pcm"
audio_sample_rate = 24000
turn_detection = { type = "<TURN_DETECTION_TYPE>" }
tools = []
local_reply_timeout = 120
local_max_audio_bytes = 786432

[voice.stt]
base_url = "http://localhost:8001/v1"
model = "<SPEECH_TO_TEXT_MODEL>"

[voice.tts]
base_url = "http://localhost:8880/v1"
model = "<TEXT_TO_SPEECH_MODEL>"
voice = ""
```

The protocol prefix must be a valid WebSocket subprotocol token; replace its
placeholder before loading config. `client_protocol_extra` lists any companion
protocols the browser must pair with that token; OpenAI needs `["realtime"]`.
`session_shape` selects the provider's session layout: empty or `xai` keeps voice
and turn detection at the session top level, `openai` moves them under
`audio.input` and `audio.output`. `transcribe_model` names the input
transcription model; empty falls back to `grok-transcribe`. Choosing an account
under Settings writes all three, so they are provider detail rather than fields
to type. Optional `session_options` (including a vendor voice selector) and
`turn_detection` are config tables. Input/output formats must match the
configured mono PCM16 sample rate.

Supply the provider credential through your existing secure injector or OS-store
reference, without placing values in shell history, TOML or command arguments.
The desktop app owns the loopback core and its voice start/events/stop calls;
there is no HTTP bearer setup or provider-key paste step. Opening the desktop
window or polling status never mints credentials or starts capture; voice needs
an explicit Start action. Microphone permission is separate from minting.

On explicit Start, Tessera resolves the long-lived key server-side, mints a
short-lived client secret and returns only ephemeral connection access to
the desktop page over the owned local API. Main can keep the provider socket and
ephemeral access out of the renderer. The desktop sends final transcript events
to the same session log. Transcript
provenance is `client_reported`, not provider attestation. Stop closes local
capture/playback and the socket, then stops the server lease; the lease also
expires at its configured deadline. Tools are absent unless explicitly allowed
in `voice.tools` and still use normal server hooks/approval. Provider request
timeouts and session/byte/event limits are configurable.

See the official xAI [ephemeral authentication guide](https://docs.x.ai/developers/model-capabilities/audio/ephemeral-tokens)
and [realtime protocol guide](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech),
and OpenAI's [Realtime API reference](https://developers.openai.com/api/reference/resources/realtime/subresources/client_secrets/methods/create)
for the Codex-bound path.
A configured audio device name alone proves
neither a working loopback route nor permission to record.

## Verification and evidence hygiene

```sh
.venv/bin/pytest -q
.venv/bin/python demo/run_demo.py
bash scripts/check_no_hardwires.sh
git diff --check
```

Tests and the standard demo use fakes/local fixtures. Live acceptance is a
separate operator action with tiny explicit budgets. Never publish raw config,
provider errors, headers, request bodies, credential refs, personal paths or
artifacts. Publish only redacted counts, fixed error categories and verified
outcomes. Keep long-lived keys and ephemeral access out of both private evidence
and committed demos. AetherMind is required and independent of swarm/voice.
The packaged engine is the memory service. An external service is used only
after it is reachable and accepts its auth. A normal no-tree
state is not an instruction to build or write server memory.

### Known-red baseline, so new red means something

Read the suite as "N failed" plus *which* N. A fixed set of failures that nobody
reads is how a real regression hides. The suite baseline on the development machine
was green when this note was written:

```
.venv/bin/pytest -q   # 1529 passed, 3 skipped in 234s
```

Re-measure rather than trusting this line. The number above replaced 1518/3
after eleven more tests landed (the voice utterance-identity regression plus the
board, credential and config-serialization tests from the review findings).
A baseline that is never re-read is the same problem as the failure list it
replaced.

**The trap that made this number lie for one run.** `load_config()` lets any
`TESSERA_*` env var override the config file, and the live core's launchd plist
sets `TESSERA_SERVER_HOST`, `TESSERA_SERVER_PORT` and
`TESSERA_SERVER_ALLOWED_HOSTS`. A suite run from a session that inherited that
environment read the deployment's port 8740 and its tailnet hostname into tests
that write `port=0` into a tmp dir, so two tests failed on values that had
nothing to do with the code. `tests/conftest.py` now clears `TESSERA_*` per test
(commit 67f8941). If you ever see `assert 8740 == 0` or a tailnet name where
`[]` was expected, check that isolation is still there before chasing the code.

What was in the pile before, and where each one went — the point of this list is
that none of it was "pre-existing and unknowable":

- **Four Linux-host tests with no platform guard** (`test_native_cua.py`,
  `test_computer_use_mcp.py`, `test_harness_tools.py`). They asserted Linux
  backend behaviour on a Mac. Fixed in ca4f7e0 by pinning `sys.platform` for the
  assertion each one makes, so they still check the Linux branch from a Mac
  instead of being skipped into silence.
- **Credential stub signature** (`test_provider_credential_contracts.py`).
  `Backend` gained `oauth_home`; the test's stub took one positional arg. Fixed
  in ca4f7e0 — this restored a security assertion that had been covering nothing.
- **Self-build worker tools** (`test_daemon_selfbuild.py`). The test asserted
  children get no tools; commit 54188dc deliberately gave them the session's
  registry. Fixed in 43050d9 by asserting what workers actually receive (parent
  0 tools, each worker 43).
- **Hardwire check fixture** (`desktop/src/core.rs` test using a placeholder
  home path). Fixed in ca4f7e0 in `scripts/check_no_hardwires.sh` by allowing
  exactly the two conventional placeholder names, verified in both directions:
  real names still fail the check.
- **Shakedown direct-route tool list** (`test_shakedown.py`). It named
  `computer_screenshot`, which the macOS native helper did not register at the
  time — that backend captured one app's window inside `get_app_state` (787b8b5
  made native the macOS default precisely to leave the cursor alone). The test now
  asserts that every `computer_*` name the host's backend *does* register reaches
  the direct route with the same schema, so it cannot drift again when a backend
  gains or loses a tool. Both capture shapes are wanted, and 6ae7214 added
  `computer_screenshot` to the native helper: the whole display, while
  `get_app_state` keeps the one-app capture.

Rule that follows from this: when a test and the code disagree, check git history
for a deliberate decision before asking a human which one is intended. Twice in
this pass the commit message already answered the question.

Host-dependent tool names are the recurring shape of this bug. Assert the
behaviour ("the direct route offers the registry's own schemas"), not a fixed
list of names that happens to be true on the host you wrote it on.

## Steering and interrupting a running turn

- `POST /api/sessions/<slug>/steer` with `{"text": "..."}` adds user guidance to the
  turn that is running. The guidance goes into the log as a `user` message with
  `meta.steer = true` and reaches the model at the next iteration boundary. If guidance
  arrives while the model is writing its final answer, the turn continues with it. When
  the session is idle, the text is queued as a normal turn. The response is
  `{"mode": "steer"|"queued", "turn_id": ...}` and the SSE stream gets a `steer` event.
- `POST /api/sessions/<slug>/interrupt` with `{}` stops the running turn at the next
  iteration boundary, before any pending tool calls run. A model call already in flight
  finishes first. The turn ends with `success=false` and the reply "Interrupted by the
  user."; the SSE stream gets an `interrupt` event.

## Restarting the core

For development sessions that need to oversee their own core reload, run the
external controller from a shell outside Tessera, passing the active session slug:

```sh
scripts/dev-mode.sh <session-slug>
```

The controller stays outside the backend, uses the supervised restart path, waits
for the new core generation, fetches the same session by slug, and prints its
rejoin URL. To verify or reconnect after a restart started elsewhere, use
`scripts/dev-mode.sh <session-slug> --rejoin`. The desktop window also reconnects
its existing `#view=chat&session=<slug>` route when the core returns.

Use `scripts/restart-core.sh`, never `launchctl kickstart -k`. It asks the core to
restart through `/api/core/restart`: new turns are refused, running turns get
`[server] drain_seconds` (180) to finish while the UI keeps streaming, then any
still running are interrupted as "Interrupted by a core restart" and given
`interrupt_grace_seconds` (20). Queued turns stay queued and resume after the
restart. The script waits for the new process (`TESSERA_RESTART_TIMEOUT`, 300s).
A supervisor SIGTERM drains the same way, but launchd kills after its own exit
timeout (20s by default), so a kickstart can still cut a long turn short.
An existing install whose LaunchAgent uses a different label sets `TESSERA_CORE_LABEL` to that label before running `scripts/restart-core.sh`.

### When the desktop app is the supervisor

If `~/.config/tessera/desktop.toml` has `keep_core = true`, the app — not launchd
— starts and restarts the core, and `scripts/restart-core.sh` will refuse because
it cannot see the app. Restart from the app instead: Settings → Restart core, or
quit and reopen the app (the app respawns a dead core at its configured address,
and reopening also adopts a core that drained into nothing). The launchd job can
be left unloaded in that arrangement; the two supervisors must not both hold the
same home, because the session manager lock allows one core per home.
