# Tessera daemon protocol

The Python daemon owns saved sessions, the append-only ChatLog and its existing
OptChat SummaryTree/View. Clients send requests and render results. The core
protocol has no HTTP server, listener, bearer credential or Origin check. It
runs on POSIX Python 3.11+ (Linux/macOS); no native UI dependency is required.

```sh
python -m tessera --home "<PRIVATE_HOME>" app-server
# Equivalent direct module entrypoint:
python -m tessera.app_server --home "<PRIVATE_HOME>"
```

`TESSERA_HOME` is an alternative to `--home`. The terminal client spawns the
first command and attaches with pipes. Each process supports one attachment and
multiple saved sessions. An exclusive home/session lock refuses a competing
writer. EOF denies unresolved approvals, drains accepted requests/turns and
releases the locks. A later process can resume the persisted state. No service
or background host deployment is installed. A concurrent multi-client socket
service is not part of this first transport.

## Framing and lifecycle

Each stdin/stdout line is one UTF-8 JSON-RPC 2.0 object. IDs are strings or
integers, excluding booleans/null. Batches, duplicate JSON fields and nonfinite
numbers are rejected. Output is serialized across workers; only complete frames
are written. Incidental Python output goes to stderr. Request failures use fixed
messages/codes rather than raw provider exceptions, private paths or credentials.

First send `initialize`, then the `initialized` notification:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"client-placeholder"}}}
{"jsonrpc":"2.0","method":"initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"thread/start","params":{"title":"Example conversation"}}
```

Initialization returns `serverInfo` and `capabilities` with protocolVersion 1,
transport `stdio`, supported method names and explicit `agentMessageDeltas:false`
and `interrupt:false`. There is no unsolicited startup banner. Before the
handshake, requests return -32002. Unknown methods return -32601; invalid params
return -32602; missing resources return -32004; frame limits return -32005 and
queue saturation -32006. Safe turn cancellation is unavailable (-32007); no
successful interruption is fabricated. Core execution uses its configured
timeouts/retry bounds. `shutdown {}` drains the manager and replies
`{stopped:true}`; the client then closes stdin. SIGTERM/interrupt also closes the
attachment and drains. A parent may bound its wait before terminating a child.

## Methods

Parameters are objects with exactly the documented field names. `threadId` is
the saved session slug returned in `thread.id`. A start is metadata-only; an
explicit resume loads the configured core and restores queued turns. Interrupted
running turns keep the core's failure record and are never replayed automatically.
Archived or provider-empty sessions can still be read without executing a turn.

| Method | Parameters | Result |
| --- | --- | --- |
| `thread/start` | `title`, optional `backend` selector | `{thread}` |
| `thread/list` | none | `{data:[thread,...]}` |
| `thread/resume` | `threadId`, optional `afterEventId` | `{thread,messages}` |
| `thread/read` | `threadId`, optional `after` log index, `limit` | `{thread,messages}` |
| `thread/update` | `threadId`, optional `title`, `archived` | `{thread}` |
| `turn/start` | `threadId`, `input:[{type:"text",text:"..."}]` | `{turn:{id,status:"queued"}}` |
| `view/read` | `threadId` | `{lines,bytes,high,low}` |
| `view/zoom` | `threadId`, `start`, optional `count`, `offset`, `limit` | `{text,offset,totalBytes,nextOffset}` for a leaf; `{text}` for a branch |
| `status` | none | existing passive projections plus `mirror` |
| `memory/recall` | `threadId`, either `query` or both `level`,`index` | existing configured read result or unavailable state |
| `memory/sync` | none | retry attached mirror outboxes; return mirror status |
| `swarm/create` | `threadId`,`objective`,`workers` | `{job}` |
| `swarm/run` | `jobId` | `{job}` after one bounded wave and existing gates |
| `swarm/show` | `jobId` | `{job}` without provider execution |
| `board/read` | none | sanitized board projection |
| `voice/start` | `threadId` | expiring lease minted in Python |
| `voice/events` | `threadId`,`voiceId`,`events` | existing transcript/function bridge result |
| `voice/stop` | `threadId`,`voiceId` | `{stopped:true}` |

History reads start strictly after `after` (default -1) and are bounded by
`daemon.history_page_size`. Resume returns its first bounded history page;
page further with `thread/read`. Zoom uses the original power-of-two aligned
ranges. Leaf offsets/limits count UTF-8 bytes, reject split characters, and
return a `nextOffset` until the original text is exhausted. No source log is
rewritten by reattach or zoom. The existing view compaction rules remain intact.

The default event attachment starts at the current journal tail. Supplying
`afterEventId` replays strictly after that durable session cursor, including
history; clients must deduplicate replay against their displayed messages. A
cursor cannot be replaced on an already attached session. Notifications may
precede their triggering request response, so clients must continuously pump
both requests and notifications.

## Notifications and approvals

- `turn/started` and `turn/completed` carry `threadId`, `eventId` and
  `turn:{id,status}`. Completion is `completed` or `failed` from the actual core.
- `item/started` carries a tool item. `item/completed` carries whole durable
  user/agent/result/work/note items. Items have `id`, `type`, `kind` and `text`;
  tool/result items also have `name`/`output`. Token deltas are not synthesized.
- `thread/event` carries route/capture/error journal data and its cursor. The
  journal's event IDs are session-local and survive restart.
- The daemon requests `item/tool/requestApproval` with a server-generated ID,
  `threadId`,`turnId`,`itemId`,`name`,`arguments`. Reply on that ID with
  `result:{decision:"accept"}` or `result:{decision:"decline"}`. Only the exact
  accept object grants the current callback. It never changes persistent policy.
  The existing hook/Jev/tool checks still run; `deny_risky` remains a denial.
- Optional configured Jev human gates request `decision/requestApproval`.
  Missing support, errors, malformed replies, timeouts and disconnect all deny.
  Status reads never ask for approval or resolve credentials.

## Configuration and automatic mirror

All integrations use the existing private TOML/env/credential references.
Never put credential values in TOML, argv, protocol examples or evidence. No
provider, memory service, model or account is discovered or invented. Ordinary
metadata/status reads do not load a provider or resume pending work. Voice is
disabled until configured; its upstream key stays in Python, and only explicitly
requested ephemeral access crosses the owned pipe. Neither access form belongs
in logs or evidence.

Optional bounds (defaults shown):

```toml
[daemon]
max_frame_bytes = 8388608
max_pending_requests = 32
approval_timeout = 30
event_poll_seconds = 0.2
history_page_size = 200
zoom_page_bytes = 16000
mirror_retry_seconds = 30
mirror_dir = "mirror"
```

Existing overrides apply, for example `TESSERA_DAEMON_APPROVAL_TIMEOUT` or
`TESSERA_DAEMON__MAX_FRAME_BYTES`. These are transport bounds, not remote targets.

The daemon mirrors **each managed ChatLog message and each durable session
EventJournal append**, independent of model tool choices. The legacy
`enabled` flag does not turn that off. An external service is used only after
it validates; otherwise the packaged engine is the mirror target. This includes
tool calls/results, agent/user messages, standalone work/voice appends and job
log records. Existing turn-summary capture remains separate. Endpoints, ordered
failover, project root/ID dialect and credentials come only from the existing
configured MCP client. The daemon never creates a project or builds/changes
the memory server; the personal project stays read-only.

Each mirror layer has a stable saved-session/source/index request ID. Its exact
payload and destination are committed to an outbox before delivery. Reattaching
reconciles the saved source log/journal, recovering a crash between a log commit
and its mirror callback. Lost acknowledgements retry the same ID/payload. Old
debt is never silently retargeted after a config change. Mirror writes soft-fail
without rejecting the already committed source append. Upstream outage backoff
is configurable; later appends still queue immediately. Explicit `memory/sync`
retries only the attached outboxes. No mirror worker or scheduled poll starts
outside an explicitly launched daemon.

Disabled mirroring is inert. Enabled with absent/unreachable endpoints leaves
durable debt and a fixed notice. `status.mirror` reports enabled/configured,
attached-outbox count, write-debt, local-failure count and notice. These counters
cover attached sessions; zero does not certify remote memory history or live
tree availability. Existing normal no-tree answers remain normal read states.

Verification uses real subprocess pipes, FakeBackend/synthetic provider fixtures
and fake memory. Tests prove offline protocol/durability behavior, not live
provider quality, native Linux deployment, microphone playback or a remote tree.

## Opt-in session agents and A2A

Ordinary turns remain single-agent. Session fan-out requires an explicit candidate
plan, `[swarm].enabled`, `[agents].enabled`, positive execution budgets and named
backend/account pools. No provider is discovered or started by a plan/list read.
The daemon's legacy `spawn_agent` tool cannot bypass this admission path.

| Method | Parameters | Result |
| --- | --- | --- |
| `agent/spawn` | `threadId`, `objective`, `workers`, optional `requestId` | `{run}` |
| `agent/run` | `runId` | `{run}` after one explicit bounded wave |
| `agent/show` | `runId` | `{run}` saved state, no execution |
| `agent/list` | optional parent `threadId` | `{data:[run,...]}` saved plans, including single-agent decisions |
| `agent/send` | `fromThreadId`, `toThreadId`, `text`, optional `parentMessageId`, `messageId` | `{message:{message_id,recipients,hop}}` |
| `channel/create` | `name` | `{channel}` |
| `channel/list` | none | `{data:[channel,...]}` |
| `channel/subscribe` | `name`, `threadId`, optional boolean `subscribed` | `{subscription}` |
| `channel/send` | `name`, sender `threadId`, `text`, optional `parentMessageId`, `messageId` | `{message}` |
| `channel/deliver` | optional `limit`, `messageId` | `{delivered}` direction acknowledgments |
| `channel/events` | optional `after`, `limit` | `{enabled,data,nextAfter}` passive journal page |

Candidates contain nonempty `objective` and `scope:[text,...]`, optional named
`backend`, optional `budget:{tokens,seconds}` and `dependsOn:[candidateIndex,...]`.
A single candidate, explicit dependency or overlapping scope returns `mode:single`
without creating a child. Independent candidates pass deterministic checks first,
then the configured Jev judge and optional human gate. A configured semantic
judge that fails, returns invalid answers or exhausts its budget cannot authorize
fan-out or a merge via heuristic fallback. With Jev disabled, existing offline
heuristics remain available. Scope declarations are operator-supplied; offline
heuristics cannot certify arbitrary semantic independence.

`requestId`, when supplied, is 32 lowercase hexadecimal characters. Retrying the
same creation preserves run/child identities; a changed plan rejects that identity.
An accepted run has `mode:parallel`, `status:queued`, `parentThreadId`, `depth`,
`workers:[{threadId,taskId,turnId,backend,account,scope,budget,artifact,...}]`,
`admission_gate`, `merge_gate`, `usage` and `charges`. `thread/list` lists sessions;
`agent/list` lists plans. Each child has its own saved ChatLog and OptChat view.
Parent and child logs/events record the same durable link. Start with `thread/read`
or `thread/resume` on a worker's `threadId` to inspect its independent history.
Ordinary `turn/start` and message auto-replies cannot execute a child outside an
`agent/run` claim. Further decomposition uses explicit gated plans and depth caps.

```toml
[agents]
enabled = false
max_concurrent = 0
max_depth = 2
token_budget = 0
judge_tokens = 0
wall_seconds = 0.0
view_bytes = 16000
runs_dir = "agent-runs"
```

Zero token/wall budgets keep fan-out inert. Candidate token reservations plus two
`judge_tokens` reservations must fit `token_budget`. A configured semantic gate
requires a positive judge reservation and `roles.jev` selecting a named account.
The judge uses the configured provider adapter, including CLI workers. Child
turns use their claimed main backend without unreserved secondary judge/compactor
model calls. Account rate limits, circuit/budget policy and active scope
exclusion use the existing scheduler. Concurrent session-agent claims are also
bounded by `agents.max_concurrent` when that value is positive. Zero means unlimited.
A model's own `max_concurrent_requests` bounds how many of its requests run at once.
`max_depth` caps ancestry.

The wall deadline starts at plan creation, includes semantic gates and must be
shorter than `swarm.lease_seconds`. Actual provider timeouts are clipped to the
remaining deadline; retries are disabled per claimed call. HTTP output limits
are clipped to remaining tokens. Official subscription CLIs do not promise a
preemptible upstream token cap: token reservations prevent admission and
subsequent calls, and observed overruns fail the attempt. Missing/partial usage
and ambiguous failed attempts conservatively consume their remaining reservation,
with `tokens_reported:false`; these charges are bounds, not invented metering.
`usage.seconds` sums charged provider-call durations, not elapsed parallel wall time.

Only successful claimed tasks with matching attempt/turn IDs and immutable hashed
artifacts enter the merge gate. A successful `merged.artifacts` is an ordered
bundle in candidate order, published once as parent work; no model synthesis is
claimed. The explicit coordinator does not start a retry loop. A later `agent/run`
can claim unstarted tasks in a blocked plan before its deadline. Ambiguous
interrupted attempts are fenced and charged, never automatically rerun. Explicit
run recovery can finish a lost artifact/publication receipt idempotently. Passive
show/list/resume never execute the coordinator.

Direct and shared sends reuse existing channel membership, message identity,
rate/hop/cycle guards, delivery log deduplication and durable acknowledgments.
`agent/message` session events describe accepted messages; a send receipt is not
a claim of delivery or reply. With `channels.auto_deliver=false`, drain explicitly
with `channel/deliver`. `channel/events` returns native `sequence`/`op` rows,
including channel/subscription/message/ack records; `after` is exclusive and
`nextAfter` is the last returned sequence (unchanged at the tail). It is bounded by
`daemon.history_page_size` and existing passive status file/line limits. Disabled
channels return empty pages without creating journals. These owned local pipes
carry work content; they are not a public sanitized status endpoint.

### Parent planning and isolated self-build work

`agent/plan {threadId,objective,backlog,requestId?}` runs one budgeted parent-model
planning task, records its JSON proposal and submits its candidates through the
same admission gate. It returns `{run}`. It does not ask the caller to impersonate
the parent's planning decision. Invalid or interrupted planning attempts remain
charged/recorded and are not replayed automatically. This planning task appends
parent user/agent evidence and a durable `agent/planning` event; it is not an
ordinary chat turn or a claim that the entire backlog was completed.

Additional optional `[agents]` settings (all inert by default):

```toml
planner_tokens = 0
worker_backend = ""
allowed_scopes = []
worktrees_enabled = false
repo_root = ""
worktrees_dir = "agent-worktrees"
git_command = "git"
worktree_timeout = 30.0
validation_timeout = 120.0
validation_commands = []
```

Worktree enablement requires an explicit repository root and relative allowed
scopes. Each candidate scope must be inside that allowlist. The daemon records
branch/path/base identities before creating separate child and integration
worktrees. CLI cwd is set to the child's assigned worktree; the daemon does not
upgrade a CLI sandbox. A writing CLI profile must be explicitly configured by
the operator. No checkout/merge runs in the operator's original dirty worktree.
Local git commands do not fetch or push and suppress hooks in their owned
worktrees; no global git or client configuration is modified.

`agent/run` returns `status:ready` for successful worktree workers. That means
worker artifacts exist; it does **not** mean tests passed or code merged.
`agent/merge {runId}` -> `{run}` explicitly checks each successful child's clean,
committed, in-scope diff, runs configured validation argv in the child and the
prospective integration worktree, and asks Jev about that actual diff and test
results **before** committing its local merge. Each piece gets its own verdict.
With Jev disabled, the documented offline heuristic applies. Gate failure,
conflict, validation failure or unknown interrupted state prevents that merge.

Validation commands are operator-configured argv arrays, never shell text from a
model. An argument exactly `{tests}` expands to changed, existing test files; no
matching child test is a failure of that contract. For example, configure a Python
executable followed by `-m`, `pytest`, `-q`, existing core regression paths and
`{tests}` as separate arguments. Output stays in private worktree evidence.

`workers[].implementation` pins the child commit/files/tests;
`workers[].landing` records merge status, child/integration commits, check exit
codes and gate. `workspace.parent.branch` is the separate integration branch.
The result is `succeeded` only when all requested pieces land, `partial` when
some land, or `blocked` when none land. Accepted pieces append durable parent
work/events individually. Unknown merge interruption requires private inspection;
no automatic history rewrite or patch repair is attempted.

Token admission reserves `planner_tokens`, worker budgets and one judge budget
for admission plus one per worktree merge. The wall deadline includes planning,
workers, validations and gates. Scope/test receipts prove only the selected
pieces; unattempted backlog items remain open. Record whether model workers or
human supervisors authored each implementation and harness repair.

`agents.commit_children=false` by default. An explicit true setting lets the
engine validate, scope-check, stage and commit model-authored changes in an owned
child branch, so a native CLI sandbox need not write shared git metadata. The
receipt says `model_authored_engine_committed`. No out-of-scope file is staged;
the original checkout remains untouched.

### Official Grok CLI worker

`cli_worker` also accepts `vendor="grok"` with a configured official executable
and cwd. The headless argv uses `--prompt-file`, `{prompt_file}`,
`--output-format`, `json`. The engine writes the prompt to a temporary private
file, checks `stopReason=end_turn`, normalizes numeric usage, then removes the
file. Resume/fork/worktree CLI flags are refused because the daemon owns those
identities. CLI time/output/process-group limits apply as for other vendors.
This follows the official [headless protocol](https://docs.x.ai/build/cli/headless-scripting)
and [CLI reference](https://docs.x.ai/build/cli/reference).

OAuth stays inside the official CLI: Tessera does not replay tokens or cookies.
The CLI uses its existing login or operator-configured `GROK_HOME`. The first
attempt removes the vendor API-key env var to make CLI-login attribution honest.
If sign-in is required, the fixed action is `grok login` or
`grok login --device-auth`; no interactive flow is started automatically.

Optional CLI settings `fallback_credential_ref`, `fallback_api_key_env` and
`fallback_env` select an API-key fallback. It runs only when
`allow_api_key_fallback = true` is also set (default false); selectors alone never
use the key. Only an auth-denied CLI exit (including tier 403) can activate it, within the
original worker deadline. The existing resolver uses env/Keychain references;
the value goes only into that child process environment, never argv/config/logs.
Returned content is redacted against the resolved value. Successful workers
record `auth_path:cli_login` or `auth_path:api_key_fallback`. No key is resolved for
a successful CLI-login attempt. An invalid/incomplete result or ordinary provider
failure does not silently change auth paths. `auth_path` is recorded only after the official CLI returns a successful
result; a timeout or ordinary provider failure leaves it unset by design so a hung
native tool loop cannot claim `cli_login`. Grok CLI progress heartbeats every 30s on
stderr (`stdout_bytes` + remaining deadline). For bounded source work prefer
`agents.worktree_result_format="edits_json"` with `--max-turns 1` and a CLI
`timeout` well above one model turn (native multi-turn tool loops often exceed 360s).

For a bounded source-writing call without native CLI tools, configure
`agents.worktree_result_format="edits_json"`. Exact target source and optional
relative `worktree_context_files=[]` are included up to
`worktree_context_bytes=128000`. Candidates use exact file scopes in this mode.
The worker returns only `{edits:[{path,old,new}]}`: one edit per file, an exact
unique old anchor for an existing file, or empty old plus full content for a new
file. All edits are validated before any file is changed. Absolute/parent/symlink
escapes, duplicate files, mismatched anchors and out-of-scope edits are refused.
Native CLI tools can be disabled in its private profile. The same engine checks,
child commits, per-piece semantic gates and integration merge receipts still
apply. This mode does not turn a model report into a claimed test run.

Structured source context is saved in the child's durable turn journal. Run
metadata contains only its SHA-256 digest, so context and run-record byte limits
remain independent. A successful CLI login path is recorded before source
application or validation; that field proves the provider call, not a passing
implementation or merge. Interrupted and failed attempts are never replayed
implicitly.


## Credentials (sign-in vs API key)

Every backend reports the credential a request would use (`tessera/settings_ladder.py`
`credential_source`; the chat model picker and Integrations show that label).
A grok CLI row carries an explicit opt-in, `API key fallback`. Off, a failed CLI
login never uses an API key. A sign-in grant on an HTTP backend does not fall
back to an API-key variable. A model with no credential is local for the
Safe-mode concurrency default. Each agent reply records `served = {backend, auth}`
where auth is `cli_login`, `api_key_fallback`, `api_key`, `oauth` or `none`.

Grok chat uses this harness's xAI sign-in grant over HTTPS chat completions.
Tools are the normal API tool schema. The engine executes them. The official
CLI is not this path, and an API key is not a fallback.
