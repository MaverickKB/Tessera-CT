# Companions and the detached inspector

Companions are MCP clients. The inspector is a Unix socket. It adds no server
and does not change the desktop shell's loopback HTTP and SSE contract.

## Companions

Configured under `[companions.atlas]` / `[companions.ember]` (see
`config.example.toml`). Off by default; with `enabled = false` no client is
built and no connection is opened. Each companion hooks `session_start` only
(never `pre_tool`), makes one attempt with no retry, and on failure records only
the exception class and a fixed notice in turn state. Briefs and briefings are
labelled `note` messages plus turn-state context; the system prompt is never
changed. Subagents never dial companions.

| Resident tool | Remote tool | Notes |
| --- | --- | --- |
| `atlas_brief(task)` | `brief_tool` | task capped at `max_brief_bytes`; at most `max_briefs_per_session` including the session-start brief |
| `atlas_contracts()` | `contracts_tool` | empty name = unavailable |
| `atlas_read(uri)` | `read_tool` | URI must start with a `read_uri_prefixes` entry; empty list = always unavailable |
| `atlas_refresh()` | `refresh_tool` | registered only when `allow_index_refresh = true` |
| `ember_decision(text)` | `decision_tool` | explicit only; empty name = unavailable |
| `ember_propose(...)` | `companion_propose` | returned as a note; `wants_to` is never executed |

Session start for the continuity companion calls `report_session_start` and
`get_compact_cortex_briefing` (only when `profile` and `host_label` are set), `companion_encounter`
(only when slug, `instance_id` and `surface` are set) and one
`companion_account` status read. `companion_transition` is reachable only via
the inspector `inspect/ember-transition` editor method, and only with
`allow_transition = true` and `transition_authority_env` present.

`identity_file` (top level, empty by default) appends an operator file to the
user instructions; nothing is imported from another agent profile.

## Inspector

The resident daemon (`python -m tessera --home <RESIDENT> app-server`) binds
`[inspector] socket_path` (default `run/inspector.sock` under the home) when
`enabled = true`: directory 0700, socket 0600, peer uid must equal the daemon
uid (or the peer gid be in `allow_groups`). A bind failure or invalid inspector
config prints a fixed stderr notice and the daemon keeps running. macOS limits
socket paths to 104 bytes; use an absolute short path if the home is deep.

Protocol: JSON-RPC 2.0, one UTF-8 line per frame, `max_frame_bytes`. Send
`initialize` with `role` `observer` (default) or `editor`. Editor requires the
daemon's `operator_approval_env` to be set and the client to present the same
value (compared in the daemon, never logged) and a `clientHome` different from
the resident home.

Observer: `inspect/status`, `inspect/sessions`, `inspect/log`, `inspect/events`,
`inspect/view`, `inspect/zoom`, `inspect/config` (redacted: flags and endpoint
counts only), `inspect/code` (inside `source_root`), `inspect/approvals` (list
only). There is no approval decision method: any such call returns -32601.

Editor: `inspect/note` (appended by the daemon as lock holder),
`inspect/tighten` (`ask` -> `deny_risky` only), `inspect/companion-pause`
(effective at the next session start), `inspect/revert` (note -> retracting
note; tighten -> restore needs operator approval), `inspect/ember-transition`.
Each writes an inverse record to `run/inspector-journal.jsonl` first.

Queue saturation returns -32006 for the inspector request only.

### CLI (detached session, its own home)

```sh
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" status
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" log <slug>
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" note <slug> "text"
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" propose --edits edits.json
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" apply <proposal-id>
python -m tessera --home "<INSPECTOR_HOME>" inspect --attach "<SOCKET>" rollback <proposal-id>
python -m tessera --home "<INSPECTOR_HOME>" inspect-mcp --attach "<SOCKET>"   # MCP on stdio
```

Writer verbs refuse when `--home` is the resident home. With the socket down,
`status` reads `meta.json`, `view.json` and redacted config only (no locks, no
app-server). Proposals (`{path, old, new}` edits) are stored under
`<INSPECTOR_HOME>/proposals/` with a unified diff, scope and sha256 per file;
source is not touched. `apply` needs the editor role, refuses while a turn runs
if `source_root` is the live package, writes a snapshot before the first byte,
uses exact-once anchors, and never restarts, signals or pushes. `rollback`
restores the snapshot only if it still matches the recorded sha256.

## Session lens in the shared UI

The chat sidebar's Session lens uses the existing HTTP core. Context shows the
conversation view's byte usage, merge thresholds, and message spans. Expanding
a span reads its original messages. These numbers describe the conversation
view, rather than the full prompt sent to the model.

The AetherMind tab separates inherited session material from an explicit live
read. Session memory and Personal continuity call the configured capture
client through `GET /api/sessions/<slug>/memory?scope=session|personal`. When
a summary tree is unavailable, the client can return original layers with an
explicit `no_tree` explanation. Connection observations remain separately
labelled. Reads neither capture new memories nor change session permissions.

Settings > Appearance holds Strata, Basalt, and Tide skins, plus a custom
accent and Use skin color reset. Each skin follows the existing light, dark,
or system choice. Settings persist in the core configuration; browser storage
only caches the initial paint. Shared theme tokens also color the boards.

The thinking-budget thumb pulses faster and with a wider halo as the budget
increases. Reduced-motion preferences disable the animation. The animation
reflects the selected budget, rather than model activity or measured load.

### Activity and memory exploration

The activity strip follows the selected session's SSE events: model wait,
thought/text streaming, tool requests and results, Steer delivery, and turn
completion. Its disclosure holds recent observed activity. Agent names and
backends come from currently alive worker threads in the status snapshot.
Disconnected streams show reconnection instead of a working animation.
The pulse stops under reduced-motion preferences. Tool rows use readable
operation names, retain original arguments, and correlate returned/failed/
blocked outcomes by call ID. They remain separate original log records.

Recognized native AetherMind tree coordinates can be expanded with the
existing scoped memory endpoint's `level` and `index` parameters. Branches
use the configured client's zoom operation, including original leaf records.
Unknown tree formats retain their source text. Each read offers local search,
a scope/read-time label, record provenance where supplied, and the original
response. Search filters the loaded read; it does not claim to search the
entire store.

Settings section navigation and search use the existing registry. Skin
previews save through the same setting controls. Phone and short-height views
keep More reachable as a bottom sheet and preserve Steer/Queue/Stop semantics.

### Refresh stability

Routine polls retain unchanged board, infrastructure, session-list and context
panels. Streaming formatting keeps completed blocks in place. Chat commits
replace their live counterpart in the same message batch, avoiding an empty
frame. The transcript owns tail following, with browser scroll anchoring
disabled on its scrolling surfaces.

New interface builds are offered through Settings > Update interface instead
of reloading the page while someone reads. Applying an update retains the
existing draft preservation. Anonymous checkboxes do not count as unsaved text;
anonymous text fields still hold the update until their words can be preserved.

### Microinteractions and the mark

Buttons lift on fine-pointer hover, press inward, and briefly illuminate when
selected. Disclosures fade their content in. These effects use transforms,
color and opacity while the page keeps its layout. Reduced-motion preferences
retain static color/focus feedback and stop animation.

The wordmark has a passing sheen and a warm-node glow.
Clicking or tapping it opens its story, and Escape closes it and restores
focus. The wide banner depicts growth from stone roots into branches. Its
light flow responds to the selected session's reported working state. The
banner hides on narrow screens; the mark and its story remain available.
The warm color is an identity token, independent of custom UI accents.
