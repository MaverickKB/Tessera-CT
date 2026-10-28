# Tessera

A working starting point for a generic, single-owner agent harness. Python
3.11+ and the standard library power the runtime. The test extra installs
pytest, Pygments, and setuptools. No service is installed. Which operating systems have a recorded
run is in [Platforms with a recorded run](#platforms-with-a-recorded-run).

For an isolated configuration, optional gateway/CLI providers, explicit swarm
jobs and ephemeral-authenticated realtime voice, follow [the setup guide](docs/SETUP.md).
All example targets and models are placeholders; supply deployment settings
outside the checkout. Tests and the standard demo need no live providers.

## Install and run

The short path: one command creates what is missing (config, engine bundle,
seed pack), starts the core with AetherMind, and opens the app in a browser.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m tessera start
# Or add the app menu / start menu launcher that runs the same command:
.venv/bin/python -m tessera install-launcher
```

The first launch opens on the settings page, which asks for the two required
things in order: connect a provider, pick a chat model. Everything else is
optional. `start` reruns cleanly; it only creates what is missing.

The explicit path, for a configuration you drive by hand:

```sh
# Optional, only for running the test suite (pytest, Pygments, and setuptools):
.venv/bin/python -m pip install -e ".[test]"
# Optional: export TESSERA_HOME="<PRIVATE_CONFIG_DIRECTORY>" before init.
.venv/bin/python -m tessera init
# Edit $TESSERA_HOME/config.toml (default ~/.tessera/config.toml).
# Configure only the providers you choose, using env/credential_ref selectors.
# The example selects MODEL_API_KEY; change that selector for your provider.
# Never put credential values in TOML; optional integrations default to off.
.venv/bin/python -m tessera run "Explain the files in this directory"
.venv/bin/python -m tessera chat
.venv/bin/python -m tessera chat --session "Project notes"
.venv/bin/python -m tessera serve
.venv/bin/python -m tessera status
.venv/bin/python -m tessera zoom 0 1
.venv/bin/python -m tessera zoom 0+8
```

Run the install command from the cloned repository root. The editable install
uses this checkout; the installed `tessera` command or `python -m tessera` can
then run from another working directory. Wheels also include the initialization
template and static UI. Installation uses setuptools as a build dependency;
there are no third-party runtime dependencies. To install offline, supply the
build dependency from a trusted local wheelhouse with pip's `--no-index` and
`--find-links` options. Keep private configuration outside the checkout.

## iOS example

[`examples/ios/TesseraVoice`](examples/ios/TesseraVoice) is an example iPhone
client. It displays as Tessera. Signing uses your team and bundle-id prefix from
`Signing.local.xcconfig` (start from `Signing.example.xcconfig`). Physical
device setup is [docs/ios/DEVELOPER-MODE.md](docs/ios/DEVELOPER-MODE.md).
Deploy notes are in [examples/ios/DEPLOY.md](examples/ios/DEPLOY.md).

## Seeds and the AetherMind engine

A Tessera install is a harness until it has judgment. The seed pack is what
gives one its rules, textures, and skills: 31 rule
layers, 6 textures, and 5 skills, written into an AetherMind store by

```sh
tessera seed
```

A neutral companion template is [examples/companion](examples/companion/README.md).
It is not loaded unless `identity_file` points at copies in the home. Adopters
name their own companion.

`tessera seed --dry-run` reports what would be written without writing it.
`--store PATH` names the store and `--skills-dir PATH` names the shelf. Seeding
is deliberate, not automatic on first launch. A rerun never rewrites a body
already in the store and never replaces a skill you have customized: the pack is
a floor, and your version wins.

Writing a store needs the AetherMind engine. A release wheel carries the
vendored copy.

- **Release wheel.** The commit recorded in
  `docs/packaging/aethermind-engine-manifest.json` is already in the repository
  at `tessera/_assets/aethermind-engine`. The build hash-checks every file
  against that manifest and refuses to build if anything is missing or
  different. The wheel carries those files plus `build-receipt.json`, and
  `tessera seed` uses the engine with no extra configuration. The first
  session that uses embedded memory installs the MCP server's dependencies
  from `docs/packaging/aethermind-mcp-requirements.lock` into a venv under
  the Tessera home. That lock is Tessera's. The vendored tree does not
  include `mcp/requirements-mcp.lock`.
- **Optional override.** `TESSERA_BUNDLE_SOURCE` may name another directory
  that holds the same bytes. It is not required. A directory that does not
  match the manifest fails the build, and the error names
  `TESSERA_BUNDLE_SOURCE` and the files that differ.
- **A checkout pointed at some other engine tree.** Set `TESSERA_SEED_ENGINE`
  to a directory holding `lib/aethermind/cli.py` when you need to seed from a
  tree other than the vendored one.

The vendored copy keeps its own license, the AetherMind Noncommercial Public
License. The engine is stdlib-only on Python 3.11 and newer, so seeding needs
no venv and no running MCP service.

`init` leaves `url` and `endpoints` empty, so the embedded engine is the
memory service until an external AetherMind is configured. There is no off
state.

`init` copies [config.example.toml](config.example.toml) with placeholders and
refuses to overwrite an existing file. Replace endpoint URLs, model names,
and memory project IDs. Set `TESSERA_HOME` or use `--home PATH` before the
subcommand to isolate independent chats. The example includes empty credential
selectors and disabled swarm, board and voice sections. Edit those existing
tables following the [setup guide](docs/SETUP.md); do not append duplicate TOML
tables. The working directory defaults to the launch directory and can be
configured with `cwd`.

See the generated [configuration reference](docs/reference/CONFIG.md) for the
complete configuration surface, declared defaults and environment override names.
The example ships three backend roles -- `main`, `fast` and `compactor` -- each
naming a configured backend. `main` carries replies, `fast` carries
direct-routed turns and stands as a failover for judging, and `compactor` carries
background summaries.
Environment overrides include `TESSERA_TOOLS_APPROVAL=deny_risky`,
`TESSERA_JEV_ENABLED=false`,
`TESSERA_ROLES_MAIN=primary`, and
`TESSERA_BACKENDS__PRIMARY__MODEL=your-model`. Values use TOML types when
possible; URLs and unquoted strings remain strings. `TESSERA_AETHERMIND_ENABLED`
is accepted and ignored: memory uses the packaged engine unless an external
service validates. Backend extra request
parameters can be supplied as TOML fields or under `extra.parameters`.

Approval policies: `ask` prompts on an interactive terminal for every shell
command and file mutation, `deny_risky` rejects those operations, and `never`
allows them. Noninteractive `ask` denies them. The web and desktop UI call the
`yolo` policy Unleashed (config value `yolo`). Python callers may pass an
explicit approval callback. These controls are not a sandbox. File reads
can access paths available to the process, and approved shell commands have
the process's permissions. Review model access and hook commands accordingly.

## Architecture

Each turn sends a constant system prompt, the persisted summary view,
a turn-state block (date, cwd, inherited memory, routing, write-debt), and
the current user message. Tool calls continue within a bounded loop.
Daily JSONL records are fsynced; incomplete crash tails are preserved in
`.incomplete-*` files before recovery, and complete corrupt records fail
explicitly. An advisory lock grants one session ownership of a chat.
Background agents have separate logs and a tool allowlist, and append their
reports to the parent chat. Limits apply across nested agents.

The backend refuses HTTP redirects to prevent credential forwarding.

Default agent tools are read_file, zoom, date, memory_recall. Inherited parent
view IDs use `source="parent"` with zoom/date; default IDs refer to the
agent's own chat. Assigned agents execute their task without automatically
routing the same delegation request again.

The summary tree and view retain a bounded representation of the log, and
`zoom` exposes original messages. A background compactor builds leaves and
parents; without a configured compactor backend it uses an extractive
fallback. Injected test backends also use this fallback by default.

Capture hooks record turns and tool outcomes independently of the model.
AetherMind writes go through MCP, with local durable outbox entries and
stable request IDs. When the packaged engine is missing, or an external
service is not available, writes remain queued and visible as write-debt.
A later session retries them when the service is available. Jev is optional
and disabled by default. With Jev off, empty endpoints cause no remote judge
calls, and capture remains visible as local write-debt. Set its endpoint,
model, and credential reference through TOML or environment variables before
enabling it. Jev selects direct/tools/delegate routes with
heuristic fallback, participates in shell risk checks, recalls memory when
its decision requests it, and batches intake, tool risk, output verification and memory classification decisions. Each session records question-level probabilities and latency in `decisions.jsonl` without raw judge prompts or credentials. Optional compaction faithfulness checking retries a weak summary once and stores score metadata; enable it through `[compactor].judge_enabled` and configure its threshold/request budgets. These scores
can flag capture; they are not task acceptance evidence.

Shell hooks receive JSON stdin. Exit 2 blocks a pre_tool operation. JSON
`context` or plain stdout becomes turn/tool context. Python hook exceptions
fail closed for pre_tool and do not prevent later capture hooks from running.

## Platforms with a recorded run

An Ubuntu 24.04.5 cloud VM installed a wheel built from a clean
checkout, saved one HTTP provider through the browser, completed one chat
turn against a loopback fake backend, and ran one CLI worker. The same VM
built the desktop `.deb` with `cargo +stable tauri build --bundles deb`
and opened that shell under Xvfb.

Windows is supported by running Tessera under WSL. Inside WSL the process is
Linux, and embedded memory follows that path. Native Windows is not yet
supported for embedded memory: the engine reports no file locking backend,
and a turn is refused before any write. No WSL install and no native Windows
install are recorded here.

These have no recorded run from this work: a macOS VM, a macOS or Windows
desktop bundle, a live systemd user service, a Windows service, Windows
Credential Manager, an AppImage, and a bundled AetherMind turn on the Ubuntu
wheel above. The Debian 13 journey is a different machine and is unchanged.
GitHub Actions requires `ubuntu-latest` and `macos-latest` for pytest plus
`scripts/smoke.py`, including an acknowledged embedded memory write.
`windows-latest` runs an import, a named portable subset, and checks that
the smoke turn refuses with the labeled native-Windows sentence. That job
passing is the refusal, not a native Windows memory write.

## Verification and status

```sh
.venv/bin/python -m pytest -q
.venv/bin/python demo/run_demo.py
# Optional, uses configured services and can create memory captures:
.venv/bin/python demo/run_demo.py --live
```

The offline demo uses scripted model replies and temporary files, exercises
tools and a subagent, compaction and zoom, routing, and queued capture. It
writes `demo/transcript.md`. No live service is needed for tests or the
standard demo. The live demo is opt-in and requires configured endpoints.

This is a working starting point, not a finished product. It has no chat
gateway, multi-user server, provider-native cache markers, or security sandbox. Summary quality depends on the configured model; raw log zoom is
the recovery path. Outbox debt is evidence of queued writes, not proof of
remote durability. Local HTTP fixtures prove protocol behavior without
claiming a particular live deployment works. See [the design](docs/DESIGN.md)
for scope and [platforms with a recorded run](#platforms-with-a-recorded-run)
for what has been exercised.

All implementation and prompt text here is original. The design references
other projects for ideas only; no external source code is incorporated.

## Persistent sessions and desktop core

The Tauri desktop app (`desktop/`) owns a Python core child, which serves the static UI,
JSON API and SSE on loopback with an OS-assigned port. No bearer token or login
prompt is required; non-loopback binds are refused. The vanilla assets live in
`web/` with no frontend build step and are included in the Python package.
See the [desktop launch and IPC contract](docs/DESKTOP_IPC.md) for the `desktop`
command, ready record and parent-pipe shutdown. `serve` is a manual foreground
diagnostic entrypoint that prints its actual loopback URL. No background service
or host deployment is installed.

`chat --session NAME` resumes a matching title or slug, creating it if absent.
It uses the same persistent storage format as the web server. A manager owns
an exclusive lock while open, so stop the server before opening that same
TESSERA_HOME through the standalone named-chat CLI. Legacy `chat`, `run`,
`status`, and `zoom` without a session continue using the original `chat/`
folder; existing history is preserved.

Each persistent subject has `meta.json`, its own log/tree/view/outbox, a durable
turn queue, and replayable events under `$TESSERA_HOME/sessions/<slug>/`.
Accepted turns execute in order within a subject; separate subjects can run
concurrently. Idle subjects load lazily. Unfinished queue work is inspected
at startup: queued turns resume automatically, while interrupted running
turns are marked failed and await explicit resubmission to avoid replaying
shell side effects. Graceful shutdown drains accepted work. Logical session
IDs and capture turn numbering survive restarts.

With `[channels].enabled` and `.auto_reply` explicitly configured,
`send_to_session(slug, text)` records a work message in the target and starts
a turn there. The target must already exist and be unarchived. Sender/hop
provenance is harness-owned and defaults to at most four hops. A backend
explicitly selected at creation handles both main and direct replies. Default
subjects retain the configured fast role. A separately configured compactor
role remains available for background summaries.

The optional `[server]` section configures loopback `host`, `port` (default zero),
and `max_hops`. `--host` and `--port` on `desktop` or `serve` override the address.
The core rejects requests whose Host is not the bound loopback address or whose
Origin is foreign, so a web page cannot drive the local API through DNS
rebinding or cross-site requests.

There is also an optional shared key: `[server] auth_token` with `auth_mode`
set to `loopback` (the default) or `always`. Under `loopback` the key is
required only for requests arriving through an `allowed_hosts` proxy, so local
clients are never stranded; under `always` it is required on every `/api/`
request. The key is one shared secret with no per-user identity — it is not
multi-user isolation. Read [the threat model](docs/security/THREAT-MODEL.md)
before exposing a core beyond your own account.
Voice credentials are resolved and ephemeral access minted inside Python, then
handed to the desktop shell through the local API. No provider key is pasted into
the UI. See [SETUP.md](docs/SETUP.md#optional-realtime-voice) for configuration.

The API supports session CRUD, paginated messages, structured view/zoom,
background turn submission, status, and SSE. SSE sends two kinds of events.
Complete log records, plus route, capture, lifecycle, and error events, carry
durable integer ids. A client resumes those with `Last-Event-ID`. While a turn
is running, the core also sends live `delta` events (`turn_id`, `kind`,
`text`). Those events have no id, so they are not replayed. A client that
attaches mid-turn receives one `resume` event with the text accumulated so
far. Status reports cached inheritance availability and write-debt without
probing live services.

```sh
.venv/bin/pytest -q
.venv/bin/python demo/stage1_smoke.py
```

The stage 1 smoke starts the actual CLI server against a temporary loopback
FakeBackend HTTP fixture, exercises token-free loopback access, tools, SSE replay, session
messaging, view/zoom, archive/rename, and stop/restart, then shuts everything
down. `demo/fake_config.toml` is its placeholder configuration;
`demo/stage1_smoke.txt` contains the assertion results. No live model or memory
endpoint is used. Browser-free JavaScript/HTTP checks cover the UI behavior;
rendered desktop/mobile light/dark captures are in `demo/ui-*.png`. They use a synthetic offline session with tool cards and work messages. Assistive-technology acceptance remains unverified.

## Stage 1b evidence and research

The reusable smoke reads endpoint,
model and home from env, disables automatic retries and refuses to reset an
existing durable budget. Private live values belong only in env/private config.

Advisory model scores do not establish
task correctness or remote durability. See the [design](docs/DESIGN.md)
for scope, decisions and limits.

Run `bash scripts/check_no_hardwires.sh` before a push. Tests and standard demos
run offline; no optional service is bundled or installed.

## ROADMAP

These items are planned for beta. They are not settings or artwork in this release.
The same list is in the [design notes](docs/DESIGN.md#roadmap).

### Strict verification gates (beta)

An opt-in setting that turns the workflow's review rules from guidance into hard gates. When it's on, a job can't finish or exit early until the standard tests and the user-defined tests pass. It adds stricter review passes (pstack-style principle and adversarial review) on everything produced. Because it's a user switch, it holds in every mode, Unleashed included.

Licensed under [Apache-2.0](LICENSE).
Copyright 2026 Kenneth Bandoly. See [NOTICE](NOTICE). The vendored
AetherMind engine under `tessera/_assets/aethermind-engine` keeps its own
license file. The [public brand assets](docs/brand/README.md)
include the locked board, marks, wordmarks and menu glyphs.
