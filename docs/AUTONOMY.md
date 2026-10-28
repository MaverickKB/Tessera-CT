# Autonomy: wakes, timers, background tasks, presence

The agent can be woken without the operator typing. A wake is one turn whose
input is a short event digest, not a user message. The agent decides what to do
with it: act, speak, or call `rest`. Rest marks a wake's final reply as silent.
A standing agenda can wake a session through this same bus after a gap.
The autonomy thread also has an independent reflection opportunity after the configured
agenda gap when no urgent signal is reported.

Two loops, not one with a fallback. A working session wakes itself for its own
open lesson, question, intention, timer, or task. That wake stays on that
session. One session is the continuing autonomy thread for independent thought,
wider awareness and self-regulation. Other sessions and agents stay separate.
That thread observes infrastructure, shared WIP and active session progress,
delegates work, steers drift and follows up on outcomes. It can choose creative
work, computer exploration, reflection or rest during quiet time.
A working session's `set_timer`, `background_task continue`, `park_question`,
and `commit_intention` are tools of that session. They are not that thread.

## Data shapes

All state lives in one JSON snapshot (`[autonomy] state_file`, relative to home),
rewritten atomically after each change.

- **Timer** `{id, slug, due, note, repeat, created}`. `due` is epoch seconds.
  `repeat` is seconds or null. A fired one-shot timer is removed; a repeating
  timer moves `due` forward past now (missed repeats coalesce into one fire).
- **Task** `{id, slug, label, command, remote, cwd, pid, state, started, ended,
  exit_code, output, last_growth, notified}`. `state` is one of `running`,
  `done`, `failed`, `stopped`, `timed_out`, `stalled`, `lost`. Output goes to a
  file under `tasks_dir`. `stalled` is reported once per stall and the task keeps
  running.
- **Event** `{kind, at, text, data}`. `kind` is `timer`, `task`, `presence`,
  `continue`, `infra`, `self_prompt`, `pressure`, `coordination` or `reflection`.
  `self_prompt` is a session agenda. The last three carry that thread's wider
  awareness frame. Events queue per session under `held`.
- **Wake** a turn enqueued on the session with `wake = {events, held_seconds}`.
  The user log record carries `meta.wake`. The final agent message carries
  `meta.silent` (rest or empty) or `meta.proactive` (spoke).

## Delivery and caps

One scheduler thread per core. Each tick it fires due timers, polls tasks,
optionally polls infrastructure, then delivers held events for a session only
when that session is idle (no queued or running turn). Events that pile up
while the operator is mid-turn coalesce into one wake.

Caps are a gate on delivery, never a drop: `min_wake_gap_seconds`,
`max_wakes_per_hour`, `max_wakes_per_day`. A held wake says how long it was held.
Inputs are also capped: `max_timers`, `min_timer_seconds`, `max_tasks`,
`task_max_seconds`. A wake turn is not stopped by an iteration count. Neither is a normal turn. The old `max_iterations` and `wake_max_iterations` keys are gone. Do not add them back.

## Presence

The web UI reports `arrive` (window focus or page visible), `here` (heartbeat
while visible), and `leave` (blur or hidden). A heartbeat is not a person: if
nobody has focused or typed in the window for five minutes, the client reports
`leave` instead of another `here`. An `arrive` after at least
`presence_away_seconds` away becomes a `presence` event with what changed since.
The composer reports `typing` at most every 15 seconds.

## Wakes never barge in

The operator counts as engaged while a turn runs, for `engaged_message_seconds` after
they send a message, steer or voice transcript, for `engaged_typing_seconds` after
they type, and while their window is focused until `engaged_focus_seconds` pass with
no message or typing (a focus heartbeat older than `presence_stale_seconds` stops
counting). While they are engaged, held events wait; caps still apply once they
deliver. A watch does not share the conversation thread. The operator talking here
must not be the thing that holds or fires the watch, and a due watch must not cut
into the turn they are having. Put the watch on its own session. A `presence` event is folded instead of woken: the latest one rides on
the operator's next turn as a context line the companion may ignore. Kinds in
`engaged_wake_kinds` (default `approval`, the answer to the operator's own click)
still deliver. Several arrivals in one digest collapse to the latest. A failed wake
records its error without an error banner in the chat.

## Free will

The wake digest ends with one line: the agent may act, reply, or rest, and only a
reply is shown. `rest` marks the turn silent. The UI hides wake inputs and silent
wake turns; a spoken wake shows as a normal message and can notify.

## Self-prompt and lessons

Lessons live in `[autonomy] lesson_file` (default `learning/lessons.json` under
home). A lesson has a trigger, a failure, a check, and an optional skill name.
It is not a continuity layer. Continuity capture still writes turn layers on its
own path.

`record_lesson` writes one. A user turn that does not complete also writes one,
unless an open lesson already has that trigger, the text is a wake, or the
open-lesson cap is reached. `lesson_bind`, or a `skill_write` that names the
lesson skill (or the only unbound open lesson), binds the skill and clears the
fail count. A skill written in the same turn is not failed for not being loaded
yet.

On a later user turn whose text contains the trigger, turn state includes one
line naming the check and the skill to load. At turn end the harness records
whether `skill_load` succeeded. A bound skill that was not loaded is a missed
check. A judge completion score below `lesson_pass_score` (0.5 means the reply
got at least halfway; 1.0 means fully accomplished) is a missed check. `lesson_fail_limit` misses (default 2) set
`needs_revision`. A passing score with the skill loaded absorbs the lesson.
`lesson_check` can pass or retire a lesson without a judge. A judge score below
`lesson_pass_score` on a working session binds `compare-before-adopt` when that
lesson has no skill yet. The next wake on that session says to load it and run
it. The autonomy thread does not get that line, and a lesson that already names a skill
keeps that skill.

`park_question` leaves a standing question. `close_question` stops it.

When autonomy is enabled and `self_prompt_enabled` is true, a tick with an open
lesson, question, or committed intention arms `self_prompt_gap_seconds` the
first time. A later due tick posts one `self_prompt` event, unless a named
suppress rule says not to call a model:

- `engaged:<reason>` while the operator is in the conversation. The agenda stays due.
- `hold:<cap>` while a wake cap would hold the turn, or a self-prompt is already queued.
- `answerable` when every open lesson names a file that already exists. Those lessons are absorbed. No model call.

A specific lesson, question, or intention is the agenda. When none is open and
`standing_floor` is true, the autonomy thread gets the floor. Measured host
pressure, unclaimed work and active sessions all inform its next wake. New
coordination pressure can wake it immediately; subsequent reviews follow the
configured agenda gap and delivery caps. Infrastructure observations stay in
that digest alongside work. Quiet periods allow independent pursuits and rest.
`coordination_status` reads current requests, recent messages and delegated
workers without starting dormant sessions. `steer_session` guides an active
submind without enqueuing an idle turn or resuming an operator Stop.
That thread is one session
(`infinity_slug`, default `infinity`). It is created if missing and reopened if
archived. Every floor wake appends to its chat log, so the wake history stays
in one place. The rear of that thread still compacts into a coarser view. The
original lines stay in the log. Turn capture writes continuity layers with that
session id as the thread key. Rest does not delete the floor. Typing in another
app does not move the thread. Engaged and hold on the autonomy thread still
suppress it. `standing_floor = false` restores silence when nothing specific
is open. A working session does not wait for this floor. Its own timers and
intentions wake that session on the same bus.
Other wakes include `[agenda]` lines when the digest does not already contain a
self-prompt.

A committed intention lives in the same lesson file. `commit_intention` writes
one. It stays until `abandon_intention` names `done`, `failed`, or `dropped`.
Restart does not close it. An already-open session tool whose name ends with
`intention_status` can adopt open titles into that same object, at most once
per gap. It is not a second stack. A missing tool leaves that part unchanged.

## Approvals

Outside Unleashed, a risky tool call the policy refuses becomes an approval request
instead of an error: a chat card with Approve and Deny (and a notification when
the page is hidden). The turn continues. The outcome (ran with its result,
failed, denied, or expired after `approval_expire_seconds`) arrives as a wake.
Unleashed runs actions directly. Looking tools are never gated. A request does not
survive a core restart; it is marked expired.

## Computer use

On macOS, `[computer_use] provider = "native"` is the default when `provider` is
empty and `backend` is `auto`. Tessera's helper, TesseraCUA.app, reads one app's
accessibility tree and a screenshot of that app's window, and acts with
Accessibility (AXPress, AXValue, and the rest) or by posting events to that
app's pid. It does not move the cursor, raise the app, or type into whichever
app is in front. `computer_list_apps` and `computer_get_app_state` are
read-only. The other `computer_*` tools follow Safe/Unleashed. Screenshots in the
tool result are shown to the model. Accessibility text is capped by
`computer_use.text_chars`. `computer_use.helper` is the app or the executable
(default `cua/TesseraCUA.app` under the Tessera home). `computer_use.timeout`
is the wait for one response.

Build the helper on the Mac, from a Tessera checkout, then restart the core:

```
bash scripts/build-cua-helper.sh "$HOME/.tessera-live/cua/TesseraCUA.app"
bash scripts/restart-core.sh
```

The live config switches the provider to native:

```
[computer_use]
provider = "native"
backend = "auto"
```

`helper` and `timeout` can stay at their defaults. `mcp_servers` paths and
`computer_use.helper` expand a leading `~` and `$VAR` / `${VAR}` when the
variable is set. An unset variable is left as written.

If a tool says Accessibility or Screen Recording is not granted to TesseraCUA,
enable TesseraCUA in System Settings > Privacy & Security for that pane. An
ad-hoc signature (the build script's default) can drop an earlier grant. The
helper reports the missing permission and does not prompt on an ordinary call.

`[computer_use.jev]` is an optional picker on the native helper. It stays off
unless `enabled` is true and a key is available. `credential_ref` names a
`[credentials]` entry and is resolved the same way as other Tessera clients.
A value in the variable named by `api_key_env` (default `TYPESAFE_API_KEY`)
is used when that variable is set. `computer_find_element` asks Jev which
indexed line matches a description, and returns that index, the line, the
probability, and the runner-up. Below `min_confidence` it returns `not sure`
and those candidates. `computer_check` asks a yes/no question about the current
tree and returns yes or no with the probability. Both are read-only. The key
is not written into tool results or logs.

`provider = "mcp:<server>"` still exposes that MCP server's tools as
`computer_*` and hides the native set. On Linux, `auto` keeps the local
`computer_*` tools (xdotool, scrot, and similar) even if an MCP provider is
set. `provider = "native"` on Linux selects the helper and fails clearly until
that helper exists; the binary runs on macOS. `session_tools = false` keeps an
MCP server off the generic tool list; the provider setting still exposes it
when the computer-use backend is `mcp`.

`mac_look`, `mac_act`, and `mac_permissions` remain only when the backend is
the older mac backend and `[cua] mac_enabled` is set. They use TesseraMacLook.app
(`[cua] mac_helper_app`, bundle id `mac_bundle_id`), a separate bundle from
TesseraCUA.app. The core launches it with `open`. Grant Screen Recording and
Accessibility to TesseraMacLook for that path. Full Disk Access for the core
itself is the Python.app the launchd job runs.

`desk_look`, `desk_act`, `desk_status` work on remotes with `desk = true`. The
defaults expect GNOME on Wayland with a logged-in seat session: screenshots use
`gnome-screenshot` (allowed by GNOME Shell's screenshot sender list) with the
session bus, input uses `native/desk_input.py`, a stdlib uinput helper run
with `sudo -n python3 -`. Coordinates are pixels in the latest screenshot.

Integrations shows three independent switches per remote: SSH tools (`enabled`), RDP (`rdp_enabled`) and desk tools (`desk`). Turning RDP off no longer disables SSH or the desk.

## Approval rules

`[tools] approval_classes` groups tools by fnmatch patterns over their names, and `[tools] approval_rules` maps a class or a single tool to `allow` or `gate`. `allow` skips approval under Ask and Safe. `gate` follows the policy switch. A rule that names a tool beats its class rule. Entries you set add to or override the defaults:

```toml
[tools.approval_classes]
memory-self = ["clock_heartbeat", "clock_journal_write", "*write_layer"]   # default
[tools.approval_rules]
memory-self = "allow"   # default; acting (mac_act, desk_act, shell, file and remote writes, background_task) = "gate"
```

## Memory and delegation prompt

`[prompt]` adds the standing rules to the cached system prompt: memory guidance (AetherMind is a lookup tool you call, not injected context: `memory_tree` at session start, `memory_recall` before answering about past context, `memory_note` with a type for decisions, corrections, locks and lessons) and a delegation list built from the tools the session actually has. The first turn repeats that list in turn state, because a route may offer fewer tools than the session. Later turns do not. A long thread was re-teaching the same rules on every message, and the voice flattened. `turn_reminder` is the one nudge on that first turn. The opening line names the agent and tells the reply to use the identity as voice, not as a help-desk script, so the reply sounds like the agent rather than a form letter. `[tools] direct_route_tools` lets the fast direct route keep a few tools, such as memory lookup.
