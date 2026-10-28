# Floor definition

The floor's world view and pressure are a document this home owns. Ordered rules
say which signal rows become sentences, and the compose block says how those
sentences are wrapped into a wake. Python supplies the signal rows and one
evaluator. Thresholds, scores, hold times and wording live in the document.

A home with no file uses the shipped definition below. A file that does not
validate is refused: the error names the rule and the field, and the shipped
rules are not substituted for it. Settings and `GET /api/autonomy/floor-definition`
show that error and the bytes on disk. The next successful save replaces them.

The file is `<home>/floor-definition.json`, or
`<home>/floor-definition-<profile>.json` when `config.profile` is a single path
segment. Each save appends one line to the matching `*-audit.jsonl`: who, when,
and which rules changed.

## Compose

| Field | Shipped value | What it does |
| --- | --- | --- |
| `footer` | the autonomy-thread paragraph | First paragraph of every floor wake. `{agent}` and `{operator}` come from this install. |
| `coordination_paragraph` | the WIP coordination paragraph | Written when a wake has coordination lines. |
| `quiet_paragraph` | the quiet-time paragraph | Written when nothing is waking. |
| `pressure_intro` | "The body measured pressure…" | Written above waking lines whose rule is marked pressure. |
| `world_view` | empty until set | The person's own text. A saved file owns this. Until a file exists, the wake reads `autonomy.world_view`. |
| `world_view_intro` | "What matters to you, in your own words:" | Line above the world view. |
| `world_view_max_chars` | 2000 | Clip length. `0` does not clip. A clipped view keeps `limit - 1` characters and an ellipsis. |
| `wake_score` | 1.0 | A pressure rule wakes when its score is at least this. |
| `stale_seconds` | 21600 | Hold for stale cards, coordination offers, and floor dispatch. A rule's own `hold_seconds` overrides it for that rule. |
| `stale_label` | `six hours` | The words the stale-card sentence uses. The number and the words are both editable. |
| `disk_free_percent_below` | 10 | Disk limit when the host has not declared one. The wake line and the `disk_low` latch use this same limit. |
| `missing_priority` | 100 | Sort key when a card's `priority` field is absent and the rule's title pattern does not match. |

## Pressure sources

`sources` is one switch per signal (`host`, `board_card`, `notice`, `session`,
`engine`, `endpoint`, `timer`, `agenda_item`, `session_card`). Each ships on.
Off removes that signal's lines, offers, and holds in Safe and in Unleashed. A
missing switch stays on. An unknown source name is refused at load.

## Rules

A rule has `id`, `name`, `enabled`, `signal`, `when`, `template`, `score`,
`wake`, `pressure`, `emit` (`each`, `join`, `fallback`, `offers`), `sort`
(`input` or `priority`), `name_limit`, `name_field`, `owner`, `note`,
`hold_seconds`, `missing_priority`, `title_priority_pattern`, `guard`, and
`last_fired`.

`pressure` chooses the pressure section of a wake. A waking rule without it
joins the coordination section. The signal name does not make that choice.
`title_priority_pattern` is a regular expression with one number group. An
offers rule uses it only when the card's integer `priority` field is unset.

`enabled: false` removes the rule in Safe and in Unleashed. `guard: true` runs in
Safe and when the approval mode is unknown. Unleashed skips it. No shipped rule is
a guard. Manual off wins over both modes.

`when` may use `and`, `or`, `not`, comparisons, `in (...)`, `true`, `false`,
`null`, strings, and field names from that signal. An unknown field is refused
at load. Join templates also have `count` and `names`. Host rows also carry
`disk_limit`, `disk_percent`, `stale_seconds`, and `stale_label`. Session rows
carry `activity_label`.

An offers rule reads the rows of its own signal. `sort: priority` orders
matches by the card's `priority` field, then `title_priority_pattern` when
that field is unset, then `missing_priority`, then filed before unfiled, then
ordinal. `sort: input` keeps row order and stops at the offer limit, which is
how floor dispatch walks cards. The wake runs both lanes, and each lane keeps
its own hold record. `name_limit` caps the names inside a join. The count is
the full match count.

The exact text of the last floor wake is stored on the scheduler as `last_wake`
(`posted` when it was queued, `sent` when it was delivered) with the rule ids
that caused it. Settings and the board pressure panel both show that text.

## Signals

Providers do not choose thresholds or wording. The floor reading includes
engine, endpoint, timer, agenda item, and session-card rows. The shipped
definition does not emit them. A rule this home adds can.

### host

One row per infrastructure host.

| Field | Meaning |
| --- | --- |
| `ordinal` | Position in the infrastructure report. |
| `label` | Display label, or `host {ordinal}`. |
| `label_lower` | That label in lower case. |
| `health` | Current health word. |
| `prior_health` | Health at the previous wake, or empty. |
| `health_changed` | A previous health exists and differs. |
| `disk_free_bytes`, `disk_total_bytes` | Disk reading, or null. |
| `disk_free_percent` | Free disk as a percent, or null. |
| `prior_disk_low` | The previous wake already latched this host low. |
| `declared_managed` | `true`, `false`, or `null` from the systems registry. |
| `declared_disk_free_percent_below` | Per-host disk threshold, or null. |

### board_card

One row per card on the board brief.

| Field | Meaning |
| --- | --- |
| `ordinal`, `title`, `status`, `card_id`, `version`, `note` | The card. |
| `project` | Project id, or empty when unfiled. |
| `age_seconds` | Age of the last update. A missing age is 0. |
| `owner` | Owning session, or empty. |
| `card_key` | `card_id`, or the ordinal as text. |
| `pending_question` | Latest pending question, or empty. |
| `session_known` | The brief included the live session list. |
| `holder`, `holder_live` | Session named on a running card, and whether it is live. |
| `priority` | Integer stored on the card, or null. An offers rule may read a title pattern only when this is null. |
| `unfiled` | 0 with a project, 1 without. |
| `title_one_line` | Title with line breaks collapsed. |
| `owner_is_floor` | Owner is the floor session. |
| `fingerprint` | `status:version`. |

### notice

One row per reading the floor failed to take. A systems-registry error, an
intention-tool failure, and a failed approval, coordination, infrastructure,
board, or engine snapshot read are rows of this signal (`source`, `error_type`,
`message`). The source names are `systems-registry`, `intention-tool:<slug>`,
`approval-status`, `coordination-status`, `infrastructure-status`, `board-brief`,
and `engine-snapshot`. They are also listed on `pressure_report.errors` and in
Settings. An approval status that cannot be read leaves the mode `unknown` and
stays visible. A Safe-only guard stays on. The refresh continues after an
intention-tool failure so one session does not stop the rest.

### session

One row per `coordination_status` session: `slug`, `busy`, `is_floor`,
`turn_created`, `activity`, `voice`.

### engine, endpoint, timer, agenda_item, session_card

`engine` is one row per `infrastructure.pressure_engines` entry (`host`,
`port`, `model`, `slots_total`, `running`, `waiting`, `warming`, `free`,
`reachable`, `error`). `endpoint` is one probed answer (`id`, `label`,
`state`). `timer` is one autonomy timer (`id`, `slug`, `note`, `due`,
`repeat`, `created`, `late_seconds`). `agenda_item` is one lesson, question,
or intention line (`slug`, `voice`, `id`, `text`, `fingerprint`, `kind`).
`session_card` is one board offer a session has not already been shown
(`slug`, `text`, `voice`, `ordinal`, `title`, `status`, `note`,
`pending_question`).

## Shipped rules

Each rule carries its own score. Board lines are report-only. Rules marked
pressure wake when their score is at least `wake_score`. Session, dispatch,
and offer lines wake as coordination content.

| Id | Signal | Score | Pressure | Wakes | Sentence |
| --- | --- | --- | --- | --- | --- |
| `host-changed-unreachable-managed` | host | 1.8 | yes | yes | `{label} changed {prior} -> {health}. Repair it. Restart the service if that is the fix. Do not replace the kiosk.` |
| `host-changed-unreachable-unmanaged` | host | 1.8 | yes | yes | Same change. `Name what changed. Do not restart it.` |
| `host-changed-managed` | host | 1.5 | yes | yes | Any other health change, repair wording. |
| `host-changed-unmanaged` | host | 1.5 | yes | yes | Any other health change, name-what-changed wording. |
| `host-unhealthy-managed` | host | 1.4 | yes | yes | First sight of an unhealthy host, repair wording. |
| `host-unhealthy-unmanaged` | host | 1.4 | yes | yes | First sight. `Name it. Do not invent a repair.` |
| `host-disk` | host | 1.4 | yes | yes | `{label} disk free is {percent} percent. Name the largest disposable path. Do not delete it.` Fires when free percent is below the shared limit and the latch is not already set. |
| `host-steady` | host | 0 | no | no | `Pressure: house is steady. Do not invent work. Do not continue the last wake.` Fallback when no other host rule matched. The hit carries `steady`. |
| `board-unfiled` | board | 0.2 | no | no | `{count} board card(s) unfiled ({names}). File them under a project. Do not ask a person to file them.` Names capped at 5. |
| `board-stale` | board | 0.2 | no | no | `{count} filed board card(s) older than {stale_label} ({names}). Claim one and move it.` |
| `board-abandoned` | board | 0.2 | no | no | Running cards whose session is gone. |
| `floor-dispatch` | board | 1.3 | no | yes | A card owned by the floor lane in queued, blocked, or failed. `sort` is `input`. Same hold as offers. |
| `coordination-offer` | board | 1.3 | no | yes | `Work coordination: #{ordinal} {title} (card_id={id}).` `sort` is `priority`. Title pattern `(?i)\bP([0-9]+)\b` applies when `priority` is unset. |
| `session-pressure` | session | 1.3 | no | yes | A non-floor session with an active turn. Duration alone is not failure. |
| `visible-error` | notice | 1.4 | yes | yes | `{source} failed: {error_type}: {message}` |

The repair sentences are the rules whose condition matches `declared_managed == true`,
or a null declaration whose `label_lower` contains `kiosk`. The name-it sentences
are the other branch: `declared_managed == false`, or a null declaration whose
label does not contain `kiosk`. Those conditions are the shipped rules. Edit or
delete them and the floor follows the document. The repair sentence still
contains the words "Do not replace the kiosk" because that is the instruction.

## Editor and API

Settings has a Floor definition section: a switch for each pressure source,
compose text, and every rule field, including emit, sort, name field, missing
priority, the pressure flag, and the title-priority pattern. Reorder, enable,
duplicate, delete, preview the
wake, dry-run one rule, import and export JSON or TOML, and read the audit log
and the last wake that was sent. An invalid import is refused with the rule and
field, and the file already on disk stays. Reading errors and an unreadable
approval status are shown in this section and on the board pressure panel. The
same operations are:

- `GET` and `PUT /api/autonomy/floor-definition`
- `POST /api/autonomy/floor-definition/preview`
- `POST /api/autonomy/floor-definition/dry-run` with `rule_id`
- `POST /api/autonomy/floor-definition/import` with `text`
- `GET /api/autonomy/floor-definition/export?format=json|toml`

The board pressure panel shows each rule's lines, score, and whether it is
acting, report-only, or quiet; the composed world view; the last wake text and
its rule ids; and any reading error. Card priority is an integer on the card,
edited from the board. An absent field is shown as "Priority unset" and sorts
as `missing_priority`.
