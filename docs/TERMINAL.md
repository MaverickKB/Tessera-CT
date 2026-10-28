# Terminal client

The terminal attaches to Tessera's Python daemon. The daemon owns saved session
logs, the summary tree and the fading working view. The client uses JSON-RPC
pipes and has no HTTP listener or native UI dependency. Mac and Linux use the
same Python 3.11+ standard-library client.

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/tessera
```

The module entrypoint is equivalent:

```sh
.venv/bin/python -m tessera
.venv/bin/python -m tessera chat --session subject
.venv/bin/python -m tessera resume
.venv/bin/python -m tessera resume --last
```

`TESSERA_HOME` or `--home` selects the private configuration and saved sessions.
The v1 client spawns an `app-server` attachment, initializes it, and sends commands
over its pipes. Closing the client closes stdin; the daemon drains accepted work
and saves state. A bounded shutdown fallback can terminate an unresponsive child.
A later client resumes that same durable history. No local log
or tree copy is maintained by the terminal. One daemon owns a home at a time.

Type a message to start a new session. `chat --session` resumes a matching ID or
title, or creates a named session. `resume` offers a numbered picker; `--last`
selects the most recently updated saved session. Explicit IDs work in scripts.
Resume prints a bounded recent history window (`--history-limit` or
`TESSERA_HISTORY_LIMIT`), fetching daemon pages without changing saved history.
Zoom prints the daemon's continuation offset when more source remains.
No model activity is claimed until the daemon reports an actual result. Without
a configured backend, status/help still work and failed chat remains a clear
configuration error.

Commands:

| Command | Behavior |
| --- | --- |
| `/new [title]` | Start a separate perpetual session |
| `/resume [id\|--last]` | Resume saved history or open the picker |
| `/agents` | List saved agent/session IDs and reported parent links |
| `/agents plan backlog.json` | Ask the daemon's parent planner to propose and admit a bounded plan |
| `/agents spawn spec.json` | Submit operator-chosen candidates to admission |
| `/agents run id` | Execute one bounded daemon-session wave |
| `/agents show id` | Inspect run state, child IDs and reported gates |
| `/agents merge id` | Request configured validation and gated local landing |
| `/send target-id message` | Record a durable direct message from the selected session |
| `/events [--after N] [--limit N] [--watch]` | Read or watch coordination journal pages |
| `/status` | Read passive daemon status |
| `/view` | Read supplied OptChat working-view lines |
| `/zoom start[+count] [count] [--offset N] [--limit N]` | Expand the selected session's source range |
| `/swarm create spec.json` | Create a job from explicit objective/workers JSON |
| `/swarm run id` | Run a configured bounded wave |
| `/swarm show id` | Read the durable job outcome |
| `/help`, `/quit` | List commands or detach |

`/agents` reads daemon session metadata and saved agent runs without starting
workers. Parent links use explicit run/worker session IDs; status comes from the
daemon. Missing topology stays unreported. An older daemon can
still list saved sessions. Use `/resume <child-id>` to attach a child's existing
history and working view, exactly as for a parent. The `*` marks the selected
session. Listing or resuming does not request fan-out; admission, configured
budgets and merge gates belong to the engine.

For a self-build run, select its parent with `/resume <parent-id>` and use
`/agents plan backlog.json`. The private JSON contains `objective` and `backlog`,
plus an optional stable `requestId` (32 lowercase hexadecimal characters). The
`backlog` is nonempty text. The terminal passes it to `agent/plan`; the parent planner and daemon own
candidate selection and admission. `/agents spawn spec.json` is the explicit
operator alternative with `objective` and `workers`, plus optional `requestId`.
An operator-submitted plan is not evidence of autonomous parent planning.

Use `/agents run <id>` for a wave, `/agents show <id>` for passive inspection and
`/agents merge <id>` to request the engine's configured test/Jev landing flow.
These commands require the corresponding daemon methods; missing support is
reported explicitly. The CLI does not create worktrees, select write permissions,
run validation commands or perform git merges itself. It prints known run states,
child session IDs, gate booleans/sources and token accounting, without dumping
private backend/worktree settings or raw judge decisions. A run result is not
itself evidence that commits landed. Child and integration commits display only
from a complete per-child landing receipt with passing child/integration checks
and an allowed Jev gate. Missing/inconsistent receipts stay unreported/incomplete.
Parent planning metadata and result-bundle gates stay separate from git landing.

Plan/run/merge waits use `--turn-timeout`; show uses the ordinary request wait.
A timeout does not confirm cancellation. The terminal never retries an action
automatically and prints its plan request ID before submission. Inspect that ID
before retrying; retain `requestId` in the private JSON for a deliberate replay.
While attached to the parent, finite coordination notices can arrive during a
wait. Use `/agents`, `/events` and `/resume <child-id>` to inspect the same saved
run. One daemon owns the home: another terminal cannot attach concurrently, and
watching does not make a second client or a background service.

Coordination uses session/run IDs across configured worker types, including mixed
CLI and Grok workers. The terminal does not select a provider, resolve its login
store or infer OAuth/API-key use from a worker label. Provider authentication and
auth-path evidence belong to the daemon/provider configuration and live report.

`/send child-id "A bounded task update"` sends from the selected session through
the daemon's guarded durable messaging path. A returned message receipt confirms
recording only. The terminal prints a fresh request ID before sending and checks
the matching receipt. Delivery, acknowledgement and a worker reply are separate events.
The client never automatically retries a send; inspect `/events` after a missing
receipt before sending it again. Use shell-style quotes for messages containing
quotes or spaces that should stay together.

`/events` reads one bounded coordination page. The next call in this terminal
attachment continues from the last returned journal cursor; `--after N`
explicitly selects another position, including after restarting the terminal.
Direct, channel, handoff and acknowledgement records keep their distinct labels.
`/events --watch` polls until Ctrl-C, then returns to the prompt without cancelling
queued work. An unchanged empty page is printed once while watching. Scripts
should use snapshot pages unless they intend a continuous
watch. Disabled channels, empty pages and missing daemon methods remain explicit;
none implies a running worker or successful delivery. No journal files are opened
by the client. These commands use `thread/list`, `agent/list`, `agent/send` and
`channel/events`; unavailable methods are reported without assuming success.

Assistant and tool notifications display as they arrive. Current daemon agent
messages arrive whole; the client does not simulate token deltas. A tool or judge
approval request asks for one explicit allow/deny decision. Enter defaults to
deny. `--ask-for-approval never`, EOF and noninteractive stdin deny requests;
script input is never consumed as an accidental approval. Ctrl-C during a turn
requests interruption only if the engine provides it. Unsupported interruption
is reported honestly; quitting is not a claim that accepted work was cancelled.

Settings are CLI options or environment variables. Place global options before
`chat`/`resume`. `--color auto|always|never` uses small warm headings and retains
terminal scrollback; `NO_COLOR` disables automatic color. JSON-RPC frame bounds,
request/turn/shutdown waits, and engine argv are configurable with
`TESSERA_RPC_MAX_FRAME_BYTES`, `TESSERA_RPC_TIMEOUT`, `TESSERA_TURN_TIMEOUT`,
`TESSERA_RPC_SHUTDOWN_TIMEOUT` and `TESSERA_ENGINE_COMMAND`. Engine command values
are JSON arrays of argv strings, executed without a shell. No endpoint, provider,
model, secret or external service is selected by the terminal. AetherMind remains
optional and its configured client behavior belongs to the daemon.

Coordination page size (`--event-limit`, `TESSERA_EVENT_LIMIT`) defaults to 100;
watch interval (`--event-poll-seconds`, `TESSERA_EVENT_POLL_SECONDS`) defaults to
one second. Both must be positive and the interval must be finite. The daemon
retains its own page limits and all channel/hop/rate/delivery guards.

Protocol and ownership: [daemon contract](DAEMON.md). Offline client
checks: `.venv/bin/pytest tests/test_terminal_client.py -q`. They use real stdio
subprocesses and synthetic data; they do not establish a live provider result.
The additive swarm/A2A consumer checks are
`.venv/bin/pytest tests/test_terminal_agents.py -q`.
