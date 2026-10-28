# Tessera: design notes

Tessera is a small, generic agent harness: one long-running chat with tools,
subagents, a summary-tree memory that keeps context at a fixed size, and
harness-owned capture into an external memory service (AetherMind) that does
not depend on the model choosing to write. It is a strong starting point, not a
finished product.

## 1. What we learned (research, 2026-10)

An open-source Python agent harness (Nous Research, MIT; studied as prior art only, not a Tessera dependency). Tool-calling loop,
pluggable toolsets, skills as Markdown, a memory plugin interface, context
compression when nearing the window, subagent delegation with restricted
toolsets, and gateways (CLI, chat apps). Reusable ideas: toolset registry with
per-agent allowlists, skill files loaded on demand, compression that keeps the
head (system + first turns) and tail and summarizes the middle, iteration
budget per turn. MIT allows code reuse with attribution.

OpenAI Codex CLI (Apache-2.0). Rust core with a turn loop over the Responses
API, approval policies (untrusted, on-request, never) and sandbox modes, an
exec policy for shell, instruction-file layering, MCP client and server, hooks, and
auto-compaction: when token use passes a threshold it asks the model for a
handoff summary and restarts the history as [initial context, recent user
messages, summary]. Reusable ideas: approval/sandbox split, compaction as a
handoff summary that preserves the user's own messages verbatim, rollout JSONL
of every event for replay.

Summary-tree chat (public notes by V. Taelin, no license, ideas only, no text
or code copied). Log every message and tool call append-only. Build a binary
tree of fixed-size one-line summaries (leaf = one message, parent = merge of
two siblings). Each turn is a fresh call that sees a "view": a list of tree
nodes covering the whole chat, fine near now and coarse in the past, with a
byte budget. Merge the pair that is most overdue relative to its own size;
merge in batches (grow to a high-water mark, then shrink to a low-water mark)
so the view prefix stays byte-stable and prompt caching keeps working. The
model can zoom(id, n) to open any line down to the raw message. Subagents get
the view plus a task and report back as one message. Persist the view; never
rebuild it from the log.

Current practice on context and caching (Anthropic, OpenAI, others, 2025-26):
- Keep the prompt prefix stable: tools and system prompt first and constant,
  no timestamps in them; per-turn state goes after the history. Cached input
  costs about 10% of fresh input on major providers.
- Context engineering beats bigger windows: long contexts degrade quality, so
  compact, clear stale tool results, and keep notes outside the window.
- Clip tool output (head plus tail) and tell the model to restate what it
  learned, since summaries lose tool output.
- Subagents work best with isolated context, a narrow task, a tool allowlist,
  and a single condensed report back.
- Hooks at lifecycle points (session start, before/after tool, turn end,
  session end) are how harnesses enforce policy and capture without relying
  on the model.
- Evals: scripted fake-model tests for the loop, plus small scenario evals
  judged by a cheap classifier; track pass rate over time.

## 2. Architecture

```
cli (repl / one-shot)
  -> Session (lock, config, hooks, capture)
     -> Turn loop: [tools][system][view][turn state][message]
        -> Backend (OpenAI-compatible /chat/completions, configurable)
        -> Tools: shell, read_file, write_file, edit_file, zoom, date,
                  spawn_agent, memory_recall (AetherMind read)
     -> Log (append-only JSONL, flushed, one writer)
     -> Tree + View (summary nodes, due-order merges, sawtooth budget)
     -> Compactor (background workers, cheap model, ruler and retry)
     -> Router (Jev System One: route + risk + judge; heuristic fallback)
     -> Capture (AetherMind outbox: turn/session writes, write-debt)
```

Prompt layout per call. Tools and system prompt are constant bytes. The view
follows, growing only at its end between merge batches. Per-turn state (date,
cwd, inherited memory digest, write-debt notice) follows the view, then the
user message. Nothing else carries across turns; within a turn the tool loop
appends assistant and tool messages normally.

### Log
Message kinds: user, agent, tool, echo (tool result), work (subagent report),
note (imported memory). One JSONL file per day, fsync on append, an advisory
file lock so one process owns a chat. Tool output clipped to head and tail
(configurable, default 30,000 chars). Model reasoning is not logged.

### Tree and view
Leaf node = message in at most N bytes (default 512); a short message is its
own leaf with no model call. Parent = merge of two siblings. Node id is
(level, index) and is shown as `start+count`. View = ordered list of node ids.
Merge priority for sibling pair at level l ending at message `last` with T
messages: (T - last) / 2^l, larger first, ties to the older pair, only when the
parent is built. Batch: when the view exceeds the high-water bytes, merge until
under the low-water bytes. The view is saved to disk after every change.
Unsummarized recent messages render as a placeholder that tells the model to
zoom. Sizes are UTF-8 bytes.

### Compactor
Bounded worker pool (default 4). Ready-node queues, never a full scan. The
compaction call reuses the same system prompt and tools (not callable) so it
shares the prompt cache, and shows a smaller context view before the task.
Length is enforced with a visible ruler and up to N retries that show the cut
point; keep the shortest. Without a model, a deterministic extractive
fallback is used (tests and offline mode).

### Subagents
spawn_agent(name, task, tools?) starts a background run with the current view
and the task, its own log under agents/<name>/, and a restricted toolset.
Its final reply is appended to the main log as a work message "[name] ...".
The parent never polls; reports arrive between tool calls or as the next turn.
Concurrency and depth limits are configurable.

### Hooks
Events: session_start, turn_start, pre_tool, post_tool, turn_end,
session_end, compaction. Hooks are Python callables registered in code or
shell commands from config (JSON on stdin, exit 2 blocks a pre_tool call,
stdout may add context). Capture and routing are built-in hooks, so they run
whatever the model does.

### AetherMind integration (first class)
Transport: MCP streamable HTTP client (JSON-RPC over POST, SSE responses,
session header), endpoint and optional bearer token from config/env. The
harness only talks to the service through its tools; it never touches tree
files.
- Session start: prepare_inheritance(project, anchor) and tree_view for the
  personal project; the digest goes into turn state, not the system prompt.
- Capture: every turn and tool call is already in the local log. At turn end
  the capture hook writes one layer summarizing the turn (user words close to
  verbatim, decisions, outcomes, failures) with a stable request_id
  (session id + turn number) so retries are idempotent. At session end it
  writes a session layer.
- Outbox: each pending write goes to a local outbox file first, then is sent.
  Failures stay queued with attempt counts. Unsent items are write-debt.
- Write-debt surfacing: at the next session start the harness retries the
  outbox, and any remaining debt is listed in the turn state next to the
  inheritance block, and reported by `tessera status`.
- The model also gets memory_recall (read_layers, tree_zoom) as a tool, and a
  memory_note tool for explicit notes, but capture never depends on them.

### Jev / System One routing
Optional. The host and model come only from settings or the environment and
default unset, so the feature stays off unless configured. Enabling the
computer-use picker without both is a configuration error. The key comes from
the configured environment variable. Used for cheap typed decisions:
- route (Choice): answer directly with the fast backend, use the main backend
  with tools, or delegate to a subagent.
- needs_memory (Noul): should the turn start by recalling AetherMind.
- tool risk (Noul): is this shell command destructive or externally visible;
  above threshold it requires approval per policy.
- judge (Score): did the turn accomplish the request; used by evals and to
  flag turns for capture as failures.
Every decision has a heuristic fallback when the key is missing or the call
fails, and decisions are logged with probabilities.

### Config
TOML at $TESSERA_HOME/config.toml (default ~/.tessera), env overrides with the
TESSERA_ prefix. Backends are named OpenAI-compatible endpoints (base_url,
model, api_key_env). Roles map to backends: main, fast, compactor. No paths,
hosts, or users are hardcoded; personal setups live only in local config.

## 3. Non-goals for v0
Chat gateways, a multi-user server, image input, and provider-native cache
markers beyond OpenAI-style automatic prefix caching. Anthropic cache markers
are not implemented.

## 4. Risks
- Summary quality depends on the compactor model; bad lines lose detail
  (zoom mitigates).
- Shell tool runs real commands; default approval policy asks before risky
  ones.
- The packaged AetherMind engine is the memory service. An external service
  replaces it only after that service is reachable and accepts its auth. An
  invalid external service leaves the packaged engine running, and the failure
  is reported. When the packaged bytes are missing, capture queues as write-debt.

## 5. Product scope and staged roadmap

The engine above stays first. The product grows around it in stages. Each
stage ships working, tested, and documented before the next starts.

### Stage 1: engine plus chat UI with persistent sessions
- Sessions: each subject is a named, persistent session (one ChatLog, tree,
  view, and capture state per session under $TESSERA_HOME/sessions/<slug>/).
  A session never ends; it continues through the summary tree and zoom.
- Local web server (stdlib http.server, threaded) bound to 127.0.0.1 by default,
  host and port from config. JSON API: list/create/rename/archive sessions,
  post a message, stream turn events over Server-Sent Events (agent text, tool
  calls, tool results, subagent reports, routing decisions, capture status),
  zoom, status. Optional bearer token from env for non-local binds.
- Web UI: single page, no build step, vanilla JS and CSS. Chat-app layout:
  session list on the left, conversation in the middle, a side panel with the
  view (fading lines, click to zoom), tool calls, and memory state (inheritance,
  write-debt). Visual style follows the Tessera theme (serif body,
  monospace for data, its palette) through CSS variables in one theme file.
  No invented logos or icons: placeholders only until design assets are
  supplied by the design owner.
- Session messaging (groundwork): a session can post a message to another
  session (tool send_to_session), which lands as a work message there and
  starts a turn. Full coordination comes in stage 3.

### Stage 2: providers and credentials
- Tessera backends for OpenAI-compatible chat, OpenAI Responses, Anthropic
  Messages, and CLI workers for subscription accounts the operator chooses to
  connect. Credentials resolve from env, then OS keychain, then a config
  reference. Accounts are capacity pools for the scheduler. No provider is
  required. Offline FakeBackend and local endpoints work without any cloud
  account.

### Stage 3: agent-to-agent coordination
- Shared channels (many sessions subscribe), direct messages, and handoffs
  (transfer a task with the sender's view and a brief). Messages are logged in
  both logs as work messages. Loop guards: per-channel rate limits, hop count,
  and a Jev Noul check for "does this need a reply".

### Stage 4: work-in-progress board
- Agents maintain an HTML board of current work through a board tool (create
  card, update status, attach artifact, ask the human). Live updates over SSE.
  Each card links to the session and the log lines behind it. Pattern follows
  current artifact and progress-surface practice: agents write structured
  state, the UI renders it; humans answer questions inline.

### Stage 5: infrastructure status board
- Hosts come only from config (name, address, kind, optional SSH alias or
  agent URL). A light collector (local or ssh) gathers uptime, CPU, memory,
  disk, GPU when present, and listed services or processes.
- Grafana integration: read-only via its HTTP API (token from env):
  list datasources and dashboards, query panels through the datasource proxy,
  embed dashboard panels by URL. Tessera does not duplicate metrics storage.

### Stage 6: voice
- Confirmed from xAI docs: Speech to Speech API over WebSocket at
  wss://api.x.ai/v1/realtime?model=grok-voice-latest (alias for grok-voice-3.0),
  OpenAI Realtime-style events (session.update, conversation.item.create,
  response.create, response.output_audio.delta), server VAD, function calling,
  remote MCP tools, 120 minute max session. Auth: API key (env XAI_API_KEY)
  server-side; browsers get a short-lived token from
  POST https://api.x.ai/v1/realtime/client_secrets, minted by the Tessera
  server, and pass it as the xai-client-secret.<token> WebSocket subprotocol.
- Plan: voice button in a session; the voice session gets the session view as
  instructions and Tessera tools as functions; transcripts are logged into the
  same session so voice turns join the tree.

### Stage 7: optional workplace integrations
- Slack, Notion, calendars (Exchange/OWA style via Graph or EWS, or ICS feeds),
  Grafana read-only status: all optional, config-gated, inert until configured.
  Prefer existing MCP servers. Tessera remains its own harness with its own loop.

### Cross-cutting
- Generic and configurable; no hosts, IPs, users, or personal data in code.
- Light smoke tests only against shared model servers during working hours.

## 6. Research addenda and roadmap update

Roadmap changes:
- Stage 1 (stages below are build order, not current scope): adds a JudgeKit in jev.py: one batched request per decision
  point (intake, tool gate, output verification, memory keep/type, compaction
  faithfulness), every decision logged with model id and probabilities.
- Stage 2a, providers and credentials: the same Tessera backends
  (OpenAI-compatible chat, OpenAI Responses, Anthropic Messages, CLI workers
  for subscription accounts), credential resolution from env then keychain then
  a config reference, and accounts as capacity pools.
- New Stage 3a, swarm: blackboard task records, central scheduler per account
  (rate-limit tracking, AIMD, budgets, barrier-aware priority, centralized retry,
  circuit breakers), single-writer scopes, gates (deterministic, then Jev, then
  optional human), durable recovery, calibration reports for Jev gates.
- Order: 1 engine+chat UI, 2 providers, 3 bot channels, 3a swarm, 4 WIP board,
  5 infrastructure board, 6 voice, 7 optional workplace integrations.

## 8. AetherMind is required (hard rule)

AetherMind ships with Tessera and is on by default. The packaged engine is the
memory service. An external URL or endpoint list, taken from config or the
environment, replaces that engine only after the service is reachable and
accepts its auth. An external service that is set and does not validate leaves
the packaged engine running, and the failure is reported. There is no off
switch. The only choice is which AetherMind is active: the packaged engine, or
a validated external service.

When an install does not contain the packaged engine bytes, status and Settings
say so. Capture still queues write-debt with a clear notice. Endpoints, hosts,
IPs, tokens and project paths never appear in code, committed defaults,
config.example.toml (placeholders only), README instructions, or docs that look
like setup instructions. A private local config (or env vars) is the only place
for a user's real external values. Ordered endpoint lists with health-probe
failover apply after that external service has validated. Tessera wires the
packaged engine; it does not modify the engine's own code.

## 9. Everything is configurable (hard rule)

Hosts, paths, ports, model names, providers, credentials, feature toggles, and
outside-system dependencies come from settings (TOML) or environment variables.
The local server defaults to loopback (`127.0.0.1`) and port 0, which asks the
operating system for an ephemeral port. Backend model names default to empty.
An operator sets them in settings or in the environment. The loopback address
and port 0 are replaceable. They are not one deployment's address, account, or
private host. Offline defaults work with no external services. Optional
integrations (Jev, voice, Slack, Grafana) stay off until configured. AetherMind
is required. See section 8. Do not bury a local path in one module. Before a
push, search the diff for IP addresses, hostnames, absolute user paths, real
tokens, and named private services, and reject them.


## 10. Scope lock

Tessera is its own harness with its own loop. It does not include a third-party
agent home. The name is Tessera.

## 11. Stage 1 acceptance honesty

Stage 1 may claim offline pytest and FakeBackend demos only. Live model
acceptance is NOT met: a bounded live smoke showed tool_use
returning truncated results, a truncated final answer, and zero live AetherMind
writes (capture queued as write-debt). Do not claim live acceptance until a
later verified run shows complete tool results, a finished answer, and (when
configured) a live write/read round trip.

### Organic motion and observability

Page changes reveal the destination immediately, then draw a short mineral seam
with branching warm light. The transition changes opacity and artwork strokes,
preserves page geometry, cancels superseded effects and respects reduced motion.
Hover feedback uses independent translate/scale properties so anchored controls
retain their positioning transform. The floating Latest control stays stationary.

Infrastructure retains the complete configured host inventory. Its expanded
Sparks section reads SparkDash snapshots, engine metrics, RoCE counters, hardware
history and recorded token traffic. Source model names are displayed unchanged.
Missing metrics read “Not reported”. Cached prompt tokens are a subset of prompt
tokens and are counted separately from generated tokens. Historical chart labels
identify bucket averages; source tables retain model, node, port and UTC bucket.
The range picker covers one hour through 30 days. History contains only retained
source records. Hardware history collection is controlled in SparkDash itself.

The optional `[infrastructure]` fields `sparkdash_command` (argv list) and
`sparkdash_url` (operator link) configure the connection. The command runs the
bundled `sparkdash_remote.py` on the source host through Python stdin. Its argv
supplies `--env-file` and `--url`; the core appends a validated history range.
Only telemetry GETs run. SparkDash authentication remains on its owning host,
never in browser assets or API responses. Reads cache for 30 seconds, command
failures return a bounded status, and configured host monitoring remains separate.
Telemetry updates retain focused controls, open disclosures and scroll position.

Integrations groups configured connections, searches names and advertised tools,
filters enabled or failed connections and displays measured check times. A check
updates its own card while retaining unchanged cards and expanded capabilities.

Voice places the existing session, start/stop, mute and push-to-talk controls
beside the tree mark and a readable transcript. Setup lives in a disclosure.
Presence status follows actual connection, response, playback and microphone
state. The waveform uses microphone samples. Transcript updates follow the end
only when the reader was already there, preserving review of earlier speech.

## ROADMAP

These items are planned for beta. They are not settings or artwork in this release.

### Strict verification gates (beta)

An opt-in setting that turns the workflow's review rules from guidance into hard gates. When it's on, a job can't finish or exit early until the standard tests and the user-defined tests pass. It adds stricter review passes (pstack-style principle and adversarial review) on everything produced. Because it's a user switch, it holds in every mode, Unleashed included.
