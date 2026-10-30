# Tessera UI

The [native desktop shell](../desktop/README.md) (Tauri v2) opens this vanilla
page in a native window loaded from the app-owned loopback core's own origin. The
page uses the same same-origin HTTP/SSE paths as any client and gets no shell IPC.
There are no login/access controls. No frontend build is needed. Voice uses the
page's own WebSocket path with the core-minted short-lived lease (the removed
Electron shell kept that socket in its main process; not yet ported).

The UI identity is the Frame board 02 V1 Binary Strata mark and the Tessera
wordmark. Use the supplied artwork intact on the header, about/access surfaces,
and app icons. Menus use only the supplied plain line glyphs, without tree
decoration. The conversation tree remains a functional view of layers and zoom.
Tessera is the product and keeps its own harness and loop.

Local copies of the approved assets live in `brand/`; their source mapping is
recorded in `brand/README.md`. PNG wordmarks preserve the supplied typography
without external font dependencies. No new mark, presence effect, or animation
was created. The app/favicons use the supplied cool V1 set. Appearance tokens,
including theme-specific wordmarks and glyph URLs, live in `theme.css`. The
paper/ember palette, serif body, and monospace data remain configurable.

The menu contains Chat sessions, WIP board, Infrastructure board, Voice,
Integrations, and Settings. Chat uses the existing session API. WIP renders
read-only Stage 4 board cards and a separate durable-task snapshot. Infrastructure
renders Stage 5 host observations and optional monitoring status. Voice controls
the selected Chat session's optional Stage 6 audio connection. Integrations reads
optional memory/decision-check status and local capture counts. Settings provides
appearance, message-shortcut and local-core status, with readable core-managed
model/session information. Neither panel edits server configuration.

## Session voice

Chat and Voice share Start voice / Stop voice controls for the selected session.
The optional `voice` status projection supplies only enabled/configured booleans
and an active lease count. Missing/invalid metadata is Not reported, disabled is
Off in the session controls; the read-only result surface shows BLOCKED when
disabled or unconfigured, with Not configured detail when appropriate.
Configuration does not prove connectivity or microphone permission. A
non-archived session is required.
Browsing, polling, or opening Voice never mints access or requests a microphone.

An explicit Start click requests microphone permission and resumes audio.
The preload's Voice start method (Electron, removed) asked main to call the core's
`POST /api/sessions/<slug>/voice/start` route. Python mints the expiring lease.
Main retains the configured provider URL and ephemeral connection protocols,
opens the provider socket, and injects only `voice_id`, audio/session timing and
`session_update` into the renderer. Audio frames and provider events travel over
IPC. No browser-minted secret or account sign-in is involved. Provider keys stay
in the core; lease credentials are neither displayed nor persisted. There is no
packaged provider, endpoint, voice ID or model choice.

This client supports mono little-endian PCM16 via JSON audio frames. The bootstrap
must explicitly configure `audio.input` and `audio.output` format `audio/pcm` at
the reported sample rate and JSON transport. Unsupported formats are rejected
before connecting. Automatic speech turn detection must be configured server-side
for continuous voice. The browser waits for a session.updated acknowledgment with
the exact server-provided instructions before sending microphone audio. A local
AudioWorklet resamples the device's rate and emits bounded chunks; no deprecated
ScriptProcessor or external script is used. Playback is scheduled through the
same AudioContext and is cleared when the provider reports new user speech.

Completed input/output transcripts and completed function calls go through
`POST /api/sessions/<slug>/voice/events` with `{voice_id, events}`. Only known event
fields are forwarded; partial transcripts and audio are never sent to that API.
The result contains `{accepted, messages, send}`. Only server-acknowledged log
messages (or their existing SSE/history copies) enter Chat, deduplicated by the
normal log index and labeled Voice transcript. Browser-originated transcripts
trigger a batched refresh of session counts and the conversation tree. They
retain the server's client_reported provenance; the UI does not claim vendor
attestation. No optimistic transcript is inserted as a saved message.

Tool execution stays with the server's configured function allowlist and normal
hook/approval path. The bridge runs sequentially, preserves only function-call
IDs from response.done.output, and relays server-provided send events. Stable
outbound event_id values are sent at most once per connection. No client-only
tool execution or extra permission is introduced.

Stop immediately closes microphone tracks, playback, worklet and WebSocket. Queued
completed event bridges drain before the best-effort authenticated
`POST /api/sessions/<slug>/voice/stop` request. Stop failure reports that local audio
is off and the server lease expires automatically. Changing sessions, archiving,
leaving Chat/Voice, loss of Chat access/status/stream, a real page exit, voice errors and
the supplied lease deadline also stop local audio. A phone screen sleep fires
`pagehide` with `persisted` true and must not stop a hands-free call. The voice socket asks for array-buffer frames. A binary provider frame then
arrives as an ArrayBuffer, which the text-event parser rejects and stops, instead
of arriving as a Blob whose text can only be read asynchronously. Text frames are
unchanged. "Voice data unavailable" is that rejection, or any other unreadable
provider event. Late microphone grants or start
responses cannot restart a cancelled call. There is no automatic voice reconnect.
Permission denial, missing microphone, insecure context, disconnect and failed
transcript sync have fixed readable notices; raw provider errors are never shown.

Client chunk, buffer, queue, connect-timeout and playback limits and the relative
worklet URL are configurable through the body's `data-voice-*` attributes. Remote
settings and session lifetime remain server-owned. Microphone use requires a
[secure browser context and permission](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia).
The static server CSP must permit only the configured voice socket origin when
enabled/configured; it must not include token/query data or a wildcard. Voice works
without AetherMind. Offline acceptance uses injected microphone/recorded socket
fixtures and the real authenticated Tessera start/event/stop and Chat log APIs;
it does not prove live provider audio quality or vendor interoperability.

Track 9 adds a separate read-only Voice observations section. It consumes the
existing `voice.enabled`, `configured` and `active_count` fields. BLOCKED,
Configured and Active on server describe server observations only;
an active lease never changes Start voice into Stop voice or starts a microphone.
The selected session's actual local voice lifecycle continues to own those
controls and the Listening/Stopped/error notices. Refresh reads status only.
Status failures show Status unavailable; an HTTP access challenge shows Access
required. Both clear prior server observations and keep Start disabled.

Optional sanitized handoff fields on `voice`: `transcript_count` is a nonnegative
safe integer counting server-recorded transcripts; missing/invalid values stay
Not reported. `error_category` accepts only the existing voice categories
`credential`, `permission`, `upstream`, `timeout`, `invalid_response`, `limit`,
`expired`, `configuration`, `session`, `event`, `busy`, rendered as fixed copy.
Unknown categories and raw errors, model/provider aliases, endpoints, lease IDs
and secrets never become display labels. Current status does not publish these
two additive fields. Failed/unauthorized status clears prior observations. No
lease count or recorded transcript count establishes successful live audio.

## Saved acceptance preview

The preview reads machine-readable swarm and voice observations through an
explicit local JSON file picker on the WIP board. Nothing loads automatically.
The preview never uploads, fetches an evidence directory, mints access, calls
an integration or persists the file.
The native filename display is hidden; no filename or private target becomes a
label. Clear removes the preview and fences any pending file read. Invalid,
oversized or failed reads show a fixed notice without raw errors. Configurable
`data-proof-max-bytes` and `data-proof-max-workers` bound file and worker reads.
Starting a new read clears the previous results immediately. Later selections
and Clear fence older reads; absent or malformed sections show Not reported.

Only `swarm` and `voice` observations are displayed. Workers require unique
positive safe-integer ordinals. Provider kinds accept only the documented
`openai_chat` and `cli_worker` adapter enums; unknown kinds stay Not reported.
Account/backend identities, model names, endpoints, answers, references and
credential/environment details are ignored. Attempts, reported total tokens and
task caps require safe integer counters. Missing values never become zero or an
estimate. Reported totals include CLI prompt overhead and are not a cost figure;
task caps are budget observations, not a guaranteed output limit. Above-cap
reports are labeled explicitly and cannot show recorded acceptance.

`LIVE-SWARM-ACCEPT` appears only for that exact recorded marker, `fresh_run: true`,
a succeeded job, at least two successful workers with distinct known adapter
kinds, valid token totals/caps, and literal true `hashes_verified`,
`ordered_merge_verified`, and all four known deterministic `merge_checks`.
The checks are all_artifacts_present, all_workers_succeeded,
artifact_hashes_valid and merge_body_within_bound. Unknown checks are ignored;
missing/false observations never establish acceptance. This is a display of a
saved report, not an independent hash verifier, live run or current connection.
The gate source is labeled Offline heuristic only when explicitly reported.

Voice's saved record appears separately on the Voice view. Explicit false
enabled/configured flags or `status: blocked` show BLOCKED with Not configured
copy when appropriate. Only literal booleans describe secret minting, realtime
exchange and live transcript bridging; no secret value or missing setting name
is displayed. The exact `LIVE-VOICE-ACCEPT` marker requires accepted status,
enabled/configured flags, reported mint/lease/WebSocket/session-update/exchange/
bridge/stop/client-exit success, completed response, typed-text input, positive
safe audio/transcript/lease counters, consistent transcript totals, zero replay
duplicates and zero remaining leases. Long-lived-key-in-client and persisted-
plaintext-secret flags must both be explicitly false. Missing, mistyped or
contradictory observations cannot establish acceptance; reported credential-safety
conflicts show Review required. The preview displays only known enums and counts,
never the transcript body, credential source, timestamps or private references.

This is recorded typed-input exchange evidence with client-reported Chat bridge
provenance. It does not prove microphone transcription, speaker playback,
browser audio use or a current connection. Audio counts are observations, not
playable recordings. Literal microphone/speaker proof flags are shown with their
limits; missing flags remain Not reported. Current
Start/Stop controls always use the server's configuration and actual local audio
lifecycle, independently of the preview. Disabled/unconfigured current status
also shows BLOCKED in the Voice observations panel and keeps Start disabled.

`web/tests/fixtures/live-proof.json` is a restricted snapshot of the published
sanitized handoff, retaining its reported counters/checks and blocked Voice
state. `web/tests/fixtures/accepted-proof.json` restricts the later accepted Voice
shape to the fields above. These fixtures contain no transcript text, credentials,
private aliases or targets. Run
`node --test web/tests/proof.test.js web/tests/workplace.test.js`.
Tests also consume the published JSON directly to verify shape compatibility,
private-field exclusion, missing/failed/over-cap states, limits and read races.

## Offline result states and release checks

`web/tests/fixtures/results-status.json` supplies synthetic server projections.
Fixtures demonstrate presentation only. They never connect an integration,
request a microphone or mint credentials. Start/Stop and recorded Chat transcripts
keep their existing lifecycle; a server-active fixture does not start audio.

| Surface | Fixture / observation | Presentation |
| --- | --- | --- |
| Swarm | `off` / disabled | Off; no job execution assumed |
| Swarm | `empty` / configured, empty jobs | Empty; no jobs invented |
| Swarm | `multi-provider` | Saved running/completed worker ordinals, counts and merge/gate enums |
| Voice | `off` / disabled, unconfigured | BLOCKED / Not configured; Start disabled |
| Voice | `configured` | Configuration reported; no connection or permission inferred |
| Voice | `active` | Active on server; local microphone remains off until explicit Start |
| Voice | known error category | Fixed category/copy; credential/access errors point to server settings |
| Either | `offline` / failed refresh | Status unavailable; previous counts/cards removed |
| Either | injected HTTP 403/5xx fixture | Reports status unavailable; no login or access-token controls exist |
| Either | missing or malformed status | Not reported; no fake connectivity |
| Saved proof | blocked / accepted fixtures | Recorded result only, independent of current status |

Release captures use these fixtures at desktop and mobile widths, including
empty, configured, active and failed-request states. All API responses
and assets are supplied locally; nonlocal requests and voice starts are blocked.
Privacy checks inject synthetic canaries into forbidden fields and verify they
do not enter DOM labels, attributes or storage. No private evidence directory is
read by the application. Local development notes and screenshot artifacts stay
outside the public UI payload.

## Infrastructure status and optional monitoring

Infrastructure reads the optional `infrastructure_status` and `grafana` fields on
`GET /api/status`, sharing the existing refresh and stale-response fence. It does
not call collection, SSH, agent or Grafana routes. A failed refresh, disconnect or
access challenge clears the previous snapshot. Older servers and malformed fields
show Not reported. Explicit disabled and absent-inventory flags show Off and Not
configured; an explicitly reported empty hosts array shows Empty. Configuration
does not establish connectivity. Reading status must never probe a host, resolve
a credential, create storage or start background work.

The source owner's Stage 5 projection (also available from `GET /api/infrastructure`)
contains enabled/configured booleans, a hosts array and an opaque revision. Each
host supplies only:

| Field | Accepted display values |
| --- | --- |
| `ordinal` | Positive safe integer; response-position fallback is only a display label. |
| `enabled`, `configured` | Explicit false overrides a contradictory health claim with Off or Not configured. |
| `kind` | `local`, `ssh`, `agent`, `unknown`. |
| `health` | `unknown`, `healthy`, `degraded`, `unreachable`, `stale`, `disabled`, `unconfigured`. |
| `error_category` | `timeout`, `process`, `unavailable`, `invalid_response`, `unsupported`. |
| `age_seconds` | Finite nonnegative observation age; absent age stays unreported. |
| `metrics` | Allowlisted finite numeric fields described below. |
| `gpu` | Ordinal rows with numeric utilization, memory and temperature only. |
| `services` | Ordinal rows with `running`, `stopped`, `unknown` states only. |

Host metrics are `uptime_seconds`, `cpu_percent`, `cpu_count`, `cpu_load1`,
`memory_total_bytes`, `memory_available_bytes`, `memory_used_bytes`,
`disk_total_bytes`, `disk_used_bytes`, `disk_free_bytes`. Core measurements have
explicit Not reported chips when absent. Optional secondary measurements appear
only when supplied. Percentages must be within zero to one hundred; byte counts
and CPU counts must be nonnegative safe integers. Other measurements must be finite,
nonnegative and within JavaScript's safe numeric range. GPU fields are
`utilization_percent`, `memory_total_bytes`, `memory_used_bytes`,
`temperature_celsius`; temperature may be negative. Zero is a measurement, never
an absence. No CPU/memory/disk availability or host health is inferred from another
field. Stale observations retain their Stale badge and reported age.

Grafana uses enabled/configured booleans, finite health (`unknown`, `healthy`,
`unavailable`), optional safe-integer `datasource_count`/`dashboard_count` and fixed
error categories (`configuration`, `credential`, `timeout`, `access`, `http`,
`unavailable`, `invalid_response`). Counts are observations reported by the server,
not browser discovery or global totals. Credentials, endpoints, private aliases,
host/service/GPU names, query results and raw errors never become display inputs.

**Embedding contract missing:** the current passive Grafana projection contains
no embed URLs. The UI therefore loads no iframe or external dashboard, even if
unexpected URL/token fields arrive. A future authenticated read-only API must
explicitly supply server-validated, credential-free embed URLs and opaque panel
ordinals, with configured allowed origins and the matching CSP/frame policy.
Private API/service-account tokens must stay server-side; browser authorization
and any Grafana login remain explicit operator/instance policy. URLs must not be
constructed from host fields, copied into labels, or accepted from arbitrary
raw query results. No client token setting or assumed anonymous access is added.

The backend's explicit collection API is outside this read-only UI. Refresh
does not trigger it, and the board remains useful without Grafana.

Stage 2 provider status uses only `GET /api/status`. The existing `backends`
list proves that a backend is configured, not that its endpoint is healthy or
its credentials are available. Without further status the UI says "Configured"
and "Health not reported". Empty backends show "Not configured"; missing account
metadata says "Account status not reported". A failed refresh or disconnect clears
previous status and asks the operator to reconnect or refresh.

Read-only additive contract for the server owner: optional `provider_status`
contains `accounts` and `backends` arrays. Each record may report:

| Field | Values | Meaning |
| --- | --- | --- |
| `configured` | boolean | Explicit configuration state; false overrides health. |
| `health` | `healthy`, `not_checked`, `error` | Last server observation; never inferred from configuration. |
| `credential` | `available`, `missing`, `not_required`, `not_reported` | Availability only; never a credential or reference. |
| `error_category` | `authentication`, `permission`, `rate_limit`, `quota`, `timeout`, `connection`, `provider`, `configuration` | Fixed category when health is `error`; unknown categories display "Unclassified error". |
| `id` | existing backend selection key | Backend records only; matched to `backends`, never rendered in status. |

The server now publishes `provider_status` from configuration and past request
observations. The UI reads that real field and still works when it is absent.
An HTTP-to-JavaScript regression feeds the actual server response to the panels
with credential resolution forbidden. This is passive status, not a provider probe.
An absent `accounts` array means unreported; an empty array explicitly means no
configured provider accounts. Backend health records must match a listed backend
ID; position alone is not a match. Account and backend titles use ordinal labels
so status panels do not expose private config aliases or account IDs. Ordinals
follow the current response order and are not persistent account identities.
Chat backend choices and session details use the same ordinal labels; opaque
selection keys remain request values and never become displayed labels.
Malformed metadata lists remain unreported rather than being treated as empty.

No endpoint, hostname, model, credential value, credential reference, raw error,
or arbitrary status text is rendered by these panels. Unknown fields are ignored;
unknown credential/health values remain unreported. Missing credentials override
a healthy report until resolved. The server must return only sanitized metadata
and must not include secrets in IDs or diagnostic fields. Refresh reads its last
observation without initiating a probe or model call. No account editing,
credential entry, keychain access, discovery, or new API route is added here.

Stage 4 board cards consume optional `board_status` from `GET /api/status`.
The server also exposes the same passive snapshot at `GET /api/board` with an
ETag; the UI uses its existing configurable status refresh rather than adding a
second poll or assuming a board SSE stream. Missing metadata says Not reported;
disabled/missing configuration says Off/Not configured. Explicit empty cards are
distinct from unreadable, malformed or omitted snapshots. Stale/error/access
states clear cards and navigation instead of retaining an old capability.

The published projection is deliberately limited:

| Field | Values | UI behavior |
| --- | --- | --- |
| `enabled`, `configured` | booleans | Both must be true to render cards. |
| `revision` | opaque SHA-256 hex snapshot token | Kept in memory for fenced navigation, never displayed. |
| `cards` | bounded array | Snapshot-local card ordinals, not a global total. |
| `ordinal` | positive safe integer | Card label and explicit link lookup key. |
| `status` | `queued`, `running`, `blocked`, `succeeded`, `failed`, `cancelled` | Fixed status chip; unknown values stay unreported. |
| `attempt` | nonnegative safe integer | Reported attempt only. |
| `artifact_count` | nonnegative safe integer | Generic Artifact labels, never references or links. |
| `question_pending` | boolean | Human-input notice or explicit no-pending state. |
| `session_ordinal`, `log_start`, `log_end` | safe integers; positive session ordinal and inclusive nonnegative range | Source navigation only with a valid revision. |

Titles, raw card IDs, slugs, question bodies, artifacts, paths, endpoints, private
aliases and model IDs are not read as card display fields. Artifact labels are
bounded by `data-board-artifact-labels` on the body (default six); remaining
attachments are a numeric remainder. Nothing opens, resolves or copies a ref.
Cards describe saved work, without claiming that a worker is alive or a task ran.

Open Session makes an explicit read-only request to
`GET /api/board/cards/<ordinal>/link?revision=<revision>`. The server fences the
ordinal to that exact snapshot and returns only a validated public `session_slug`
and inclusive `log_start`/`log_end`. The UI matches the key against known sessions,
checks the range against the card and uses the existing Chat route. It opens
selected source disclosures, highlights loaded source messages and labels their
range, so an operator can read the actual recorded question. It does not claim to have
loaded a full range that is absent from Chat history. Changed revisions, page
changes, malformed replies, unavailable sessions and auth errors cannot redirect
through a stale card. Error text stays fixed rather than echoing server details.

Ask-human prompt text remains in the linked chat evidence under the current
privacy contract. The board shows a pending-input notice and a source-session
button, not an invented question. Displaying literal question text here would
need a documented sanitized read contract tied to the card revision; raw journal
questions are never a fallback. No operator mutation API is exposed. Cards are
maintained through the server's opt-in session tools. Create/update controls and
human-answer submission need an explicit authenticated API with validated input,
identity/version fencing and a documented operator authority boundary. The UI
does not guess mutation routes or send titles, questions, secrets or private paths.

The WIP board's Swarm / Tasks preview also reads only `GET /api/status`. The
server now publishes passive snapshots from durable tasks and decision logs.
The UI also accepts older servers without those optional fields, retaining
"Not reported" states. It never infers running work from chat activity. The
task snapshot is separate from human-facing board cards and has no mutation API.

Published additive read-only contract: optional `task_status`
is an object containing boolean `enabled` and `configured`, plus a bounded
`tasks` array. Each sanitized record contains only `status` (`queued`, `running`,
`succeeded`, `failed`, or `cancelled`) and an optional nonnegative safe-integer
`attempt`. The UI also accepts an optional positive safe-integer `account_ordinal`,
which the current server does not publish. Account
labels use only that explicit number; private account IDs never become labels
or inputs to ordinal generation. These status enums match the durable TaskStore. Both flags must be
true before the UI displays records. Explicit false flags show "Off" or "Not
configured" without implying that saved records do not exist. An explicit empty
array means no records in the snapshot; absent or malformed arrays remain
unreported. A snapshot count describes only records supplied, not an inferred
total or current worker liveness. Unknown row fields/statuses are never rendered.
Status chips count only records supplied in this snapshot. Their filters work
locally, preserve original row ordinals and the chat draft, and send no request.
An active filter with no matches is distinct from an explicitly empty snapshot.
Missing or unavailable metadata clears/hides counts and filters rather than
displaying invented zeroes. Refresh preserves the selected filter and keyboard
focus while replacing records with the newer snapshot. All task controls remain
read-only; refresh only reads existing status/session routes.
Titles are response-local ordinals, not durable identities. Do not send raw task
objects, objectives, scopes, accounts, owners, IDs, paths, artifact references,
or exception text. The server bounds file reads and task rows through its status
settings. Rows are the latest committed revisions of the selected task identities
in journal order. An omitted array means unknown contents, including unreadable,
corrupt or over-limit data. A future paging contract must explicitly describe coverage.
Reading status must not dispatch, claim, retry, recover leases, repair journals,
create storage, or start a scheduler. There are no task mutation controls.

The Jobs section above the WIP cards groups only explicitly reported jobs. It
never reconstructs membership or merge success from the task/board snapshots,
Chat activity, artifact totals, configured accounts or matching statuses. It is
read-only; the browser has no create/run/retry/merge route. Refresh uses the same
passive status request as the existing boards.

Published read-only contract: optional `job_status` has literal boolean `enabled` and
`configured` flags, plus a bounded `jobs` array. Both flags must be true before
records display. Missing or malformed fields stay "Not reported"; explicit false
flags show "Off" or "Not configured"; an explicit empty array is an empty snapshot,
not proof that no historical job exists. Older servers may omit this field;
`task_status.enabled === false` can establish that swarm execution is off, but
child task records alone never establish a parent job. The server publishes this
projection from bounded saved-job reads; it does not
load a session runtime, resolve credentials or execute work. Configuration requires
at least two distinct declared account lanes. Unreadable or over-limit snapshots
omit the jobs array rather than inventing an empty result.

| Job field | Allowed display values |
| --- | --- |
| `ordinal` | Positive safe integer; otherwise response-local row number. |
| `status` | `queued`, `running`, `blocked`, `succeeded`, `failed`, `cancelled`. |
| `worker_count`, `completed_count`, `failed_count`, `artifact_count` | Nonnegative safe integers. Completion/failure counts above a reported worker count, individually or combined, remain unreported. |
| `merge_status` | `pending`, `succeeded`, `blocked`, `failed`. |
| `gate_status` | `not_checked`, `allowed`, `blocked`. |
| `provider_count` | Optional positive safe integer, no greater than a reported worker count. Missing or inconsistent provider coverage stays Not reported. |

Counts describe only the supplied snapshot. `artifact_count` counts saved child
outputs; the merged artifact has its own outcome. The UI does not derive merge or gate
outcomes from job status, and unknown enums become fixed "Not reported" labels.
Objectives, job IDs, backend/account aliases, child outputs, artifact refs, paths,
raw gates, credentials and errors must stay outside this passive projection. The
browser ignores any extra fields. A source projection must bound file/row reads,
exclude corrupt/unreadable state, and never dispatch or repair during a read.

Jobs adds a read-only overview of known job states in the supplied snapshot.
Running, blocked and queued jobs appear before terminal outcomes. Original server
ordinals (or original response row numbers) remain unchanged by this display order.
Unknown states are counted under "Not reported", never echoed. The outcome bar
appears only with a positive safe worker count and consistent explicit completion
and failure counts. It measures saved terminal outcomes, not worker liveness,
elapsed time, predicted completion, or model quality. Refresh never executes work.

Worker handoff: optional `jobs[].workers` is a bounded array of sanitized records.
Each worker requires an explicit positive safe-integer `ordinal`, unique within
that job's snapshot. Optional `status` uses the TaskStore enums (`queued`,
`running`, `succeeded`, `failed`, `cancelled`); optional `attempt` is a nonnegative
safe integer. Optional `gate_status` uses `not_checked`, `allowed`, `blocked` only
if a worker gate outcome was actually recorded. The UI ignores every other field,
including identities, provider/account names, endpoints, refs and raw errors.

Track 9 optionally accepts positive safe-integer `provider_ordinal` and
`account_ordinal` per worker. These are response-local labels, never private
aliases. Missing provider ordinals explicitly show Provider not reported. Distinct
accounts alone do not establish distinct providers. The provider-count chip uses
only explicit `provider_count`, never account/worker totals; a complete set of
provider ordinals must agree with it. Partial rows cannot establish full coverage.
Current source omits these additive provider fields; existing fallbacks remain.

Local acceptance artifacts are inspected for safe schema only. The browser does
not read an operator evidence directory or expose raw artifact payloads. Existing
legacy artifact shapes (including string job status and worker ordinal/status/
attempt fields) need a server-side sanitized projection into `job_status`/`voice`;
raw IDs, outputs, refs and provider settings are not accepted as UI evidence.
The new local summary wrapper's `voice.enabled/configured/active_count` already
matches the Voice projection. Its `swarm.job_status` string belongs in a job row's
`status`; its worker ordinal/status/attempt subset matches the worker contract.
The wrapper is not a public status endpoint and is never loaded by the browser.
When integrations publishes these bounded fields, the panels consume them on the
next status refresh. Until then, `web/tests/fixtures/results-status.json` exercises
multi-provider outcomes/merge, offline/auth gaps, voice off/configured/active/error
and server-recorded transcript rendering without making live proof claims.

Missing/malformed arrays or duplicate/invalid ordinals show "Worker details not
reported". No worker identity or activity is inferred from `worker_count` or the
parent job status. An explicit empty array means no records in that snapshot;
a partial array is labeled with its reported coverage. Arrays larger than the
reported fan-out are rejected. Unknown enum/attempt values retain fixed missing
labels. The producer must omit unknown child observations and perform only bounded
passive durable reads, with no TaskStore recovery, scheduler, backend or live
connection. Older servers without worker rows remain supported. Current source
handoff is pending; the UI does not fabricate rows to fill that gap.

Stage 7 workplace cards are inert configuration stubs for Slack, Notion and
Calendar. The UI reads only the existing `GET /api/status`; it never calls a
connector, asks for credentials, starts OAuth, reads workspace content or provides
an external destination. There is no connected/healthy state on these cards.

Additive sanitized handoff: optional `workplace_status` contains fixed `slack`,
`notion` and `calendar` objects. Each may report a literal boolean `configured`.
True displays Configured; false displays Not configured. Missing/invalid objects
or values stay Not reported. The current server does not publish this field.
Configuration means a server declaration only; access and connectivity stay
unverified. Unknown connector names and all extra fields (including connected,
health, errors, workspace aliases, endpoints, refs and credentials) are ignored.
Failed/access-required status refresh clears previous cards; existing status-epoch
fencing prevents a late response from restoring a cleared report.

The separate workplace renderer is deliberately transport-free. Contract tests
under `web/tests/` cover booleans, malformed/empty data, secret canaries, loss of
access and inert accessible markup. Run `node --test web/tests/workplace.test.js`
and `node --check web/workplace.js` alongside the existing web/full pytest checks.
Keeping tests here preserves the requested web-only commit boundary.

The Memory tab separates optional AetherMind connection and tree availability
from session inheritance, capture debt and Tessera's local conversation Layers.
Integrations uses the same sanitized status interpretation. These states are:

| Sanitized report | UI state |
| --- | --- |
| `configured: false` | Not configured. Memory has no off state. |
| Enabled/configured with `error_category: authentication` or `permission` | Auth required; credentials stay server-side. |
| Enabled/configured with `reachable: false` | Unreachable; any prior tree availability is cleared. |
| Enabled/configured with `reachable: true` | Connected, meaning the server's last observation only. |
| Connected with `tree_status.present: true` | Present; view/zoom are independent observations. |
| Connected with `tree_status.present: false` | No tree yet, a normal empty state. |
| Missing, malformed or incomplete report | Not reported; configuration alone stays Configured. |

No-tree copy is exactly: "No summary tree for this project yet; use read_layers or
prepare_inheritance." It is plain text, with no build/repair CTA or tool action.
The UI never derives this state by parsing arbitrary errors or inherited content.

Optional handoff contract: `aethermind.tree_status` has literal boolean `present`,
`view_available` and `zoom_available` observations. Enabled/configured/reachable
must all be true before displaying them. Connection alone cannot establish tree
presence, view/zoom or successful live evidence. A no-tree report cannot establish
successful native view/zoom: conflicting true availability flags stay unreported.
Explicit false view/zoom flags remain Unavailable even with a present tree.
The server may publish `tree_status` after a matching cached observation; it is
omitted before one is available. Optional AetherMind `error_category` remains an
additive handoff. Offline presentation uses `web/tests/fixtures/memory-status.json`.
Unknown error categories, raw errors, endpoint/credential fields and private
aliases are ignored. A Tessera status HTTP authentication failure is separate:
it clears prior reports and requests access to Tessera, not a provider key.

Earlier no-summary-tree errors do not establish a missing remote tree: target and
tool-dialect diagnosis requires a separate authorized task. The UI has no server
administration, initialization, registration or builder action. Normal client
continuity follows the existing configured hooks and operator rules, without a
new mandatory per-task workflow.
UI verification uses local fixtures, without a live proof rerun or AWS pilot.
For live use, Tessera's MCP memory target is selected through private
server configuration only. This UI consumes Tessera's sanitized status API; it
does not choose, construct, display or contact the MCP endpoint, and it provides
no local-store fallback. Offline fixtures establish UI behavior only, not the
configured live target's identity or connectivity.

Capture queues are separate from connection and recall. Integrations reads only
safe nonnegative integer `debt_total` (legacy `write_debt` when absent). The Memory
tab reads the selected session's `debt` (legacy `write_debt`, including an array's
length). Missing, invalid, negative, fractional or unsafe counters stay Not
reported and never become raw display values. Zero is Empty, not proof of delivery
or recall. Positive counts are Queued, not proof of successful writes or a retry
schedule. Queues can remain while memory is not configured, unreachable,
auth-required or connected without a tree. No capture enablement or write
permission is inferred from read connectivity. The page never sends/retries a
capture. Failed/access-required status clears queue observations and fences late
session-view reads. Fixtures in `web/tests/fixtures/capture-status.json` cover
these independent states without private targets or credentials.

Live tree evidence stays Not reported until a separate sanitized provenance and
observation contract exists. Arbitrary `live_evidence`, raw error text, paths,
project names, node content or inherited material are never proof inputs. The
browser does not fetch demo evidence or call/build a native tree on refresh.

Failed refresh, lost access and disconnect clear Jobs and tree availability.
Status epochs prevent an older response from restoring cleared information.
The accepted local conversation tree, voice controls and other boards continue
to use their own contracts; they do not become native-memory proof.

Jev / JudgeKit retains `jev.enabled` and `jev.configured` and publishes a passive
`state`: `off`, `not_configured`, `ready`, or `unavailable`, plus a safe `reason`.
The existing status row renders these as Off, Not configured, Ready and
Unavailable. Ready means complete local configuration, not remote health or
successful judging. An unavailable state comes only from an already-running
judge's observed failure; status never probes or retries. Missing endpoint,
model, backend or credential configuration cannot be reported as ready just
because the enable switch is on. Keychain selectors are declarations until real
use observes their availability. Published additive fields on `jev` are:

| Field | Values | Meaning |
| --- | --- | --- |
| `decision_log_present` | boolean | Explicit presence of a durable decision log in the server's reported scope. |
| `decision_count` | nonnegative safe integer | Optional durable record count in that same scope. |
| `last_decision_category` | `intake`, `tool_gate`, `verification`, `memory`, `route`, `needs_memory`, `tool_risk`, `judge` | Optional last recorded decision point, projected to a fixed label. |

The snapshot covers saved sessions known to the manager, including unloaded or
archived sessions, plus the configured task board's decision log. No session
identity is returned. The last category comes from the last committed row of the
most recently modified nonempty log; the server normalizes newer gate/batch points
to the finite categories above. Unknown categories are omitted. Bounded or failed
log reads omit counts and categories rather than inventing totals.

Counts and last categories display when log presence is explicitly true. An
explicit absent-log report with an explicit zero also shows zero logged decisions.
An absent-log flag without a complete zero count remains unreported: a bounded
scan cannot establish global absence. Zero records cannot imply a last decision.
Unknown categories/counts stay unreported. No local rule execution, judge request,
credential resolution or log creation may result from a status read. Never send
raw decisions, prompts, model IDs, paths, endpoints, credentials or diagnostics.
The UI does not inspect session files or infer a log from enabled checks.
Failed refresh, disconnect and access-required states clear both new surfaces;
an older response cannot restore stale task or judge metadata.

Chat tool-result cards show an Incomplete badge and a notice outside the collapsed
output when the server emits a standalone `[clipped]` line or its exact
`[Message ... Continue with zoom(...).]` paging trailer. This includes explicit
parent-source continuation. JSON tool results are checked only in their `output`
and `text` string payloads; the displayed original result remains unchanged.
Arguments, ordinary words such as clipped/continue and output length do not imply
truncation. The UI never inserts a source marker, claims missing content was read,
or automatically requests more pages. Open the result to inspect the emitted
continuation and decide how to read the remaining source.

Navigation uses `#view=<page>&session=<slug>` and accepts legacy `#session=<slug>`
links. Moving between pages preserves the current chat and draft. The mobile
Menu button opens the same six destinations. The skip link focuses the current
page without changing its route.

Light/dark/system appearance and the send shortcut are app-local preferences.
Desktop transport is managed by the desktop shell without an access prompt. Refresh frequency is
configured by `data-refresh-seconds` on the body. Endpoint, model, credential and
integration choices come from the server; no host or service is built into the UI.

Validation: `node --check web/app.js`, `.venv/bin/pytest tests/test_web_static.py -q`,
and `bash scripts/check_no_hardwires.sh`. Rendered acceptance uses a local offline
fixture in headless Chrome for browser-only regressions. Native acceptance uses
`cargo run --release -- --smoke` in `desktop/`, an actual Tauri window, an app-owned core and
synthetic sessions. No model call, microphone or live integration is required.
