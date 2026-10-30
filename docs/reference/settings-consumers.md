# Settings consumer map

Generated from `tessera/settings_model.py` (the `catalog` projection plus the
record-row specs) and `tessera/settings_consumers.py`. The machine-readable
map is `settings-consumers.json` beside this file. Regenerate both from the
repository root:

```sh
.venv/bin/python scripts/gen-settings-consumers.py
```

Do not edit either by hand.

## Why this exists

Every settings field has to actually have function, not just capture text that
reads well and is never used. A Settings
field with no reader is a lie the page tells, and a first-run wizard that wrote
one would move the lie from the page into setup. This map is the inventory the
wizard's doctor (`tessera init --check`) consults: every field the page serves,
and the code site that reads it — or an explicit NO-READER.

## Evidence classes

- The **catalog scalars** are flip-tested in `tests/test_settings_consumers.py`:
each value is set to two variants, a production reader is called, and the output
must move. That test is the proof; the citation in the JSON is the pointer.
- The **record-row fields** (providers, accounts, credentials, mcp_servers,
hosts, identities, oauth_accounts, service_accounts) have no flip test at HEAD.
They are classified by named reader: a production code site, verified by
reading the code, whose behaviour changes with the value. Writing,
type-checking and rendering the value do not count as reads.

## Summary at HEAD

119 fields: 119 CONSUMED, 0 NO-READER, 0 UNRESOLVED.

## Partial findings, recorded as debt

Both live in `tests/settings_consumers_baseline.txt` and fail the gate if
they change without the debt being fixed or re-recorded:

- `computer_use.provider` — the dropdown's "Default" and "Native" options
  resolve to the same backend on macOS. An `mcp:` name moves it, so the
  field is live; the first option is not. The baseline line is tagged
  `platform=darwin` because Linux Default selects the local desktop tools.
- `mcp_servers.*.env` — an HTTP MCP server drops `env` (mcp_servers.py:31);
  only stdio servers receive it (mcp_servers.py:32). The field hint says so.

## The resolved contradiction

`autonomy.world_view` is not a readerless value. The floor reads it.
The label names that reader. This map records the same fact:
`tessera/autonomy.py:1700`, inside `Autonomy._queue_floor_wake`, reads
`config.autonomy.world_view`, trims it, caps it at 2000 characters and appends
it to the floor wake as "What matters to you, in your own words". Treat that
audit as a field-name inventory only.

## The doctor

`tessera init --check` keeps its existing placeholder-host, credential and DNS
behaviour, and adds the consumer map: a config that SETS a NO-READER field is
named and the exit code is nonzero. The map-coverage test fails first when a
catalog spec appears without a reader entry, so a dead scalar cannot ship

## Fields

| Field | Status | Reader |
| --- | --- | --- |
| `agent_name` | CONSUMED | tessera/prompts.py:3 (SYSTEM_PROMPT_TEMPLATE), rendered by tessera/loop.py:207 |
| `operator_name` | CONSUMED | tessera/config.py:844 operator_label; tessera/sessions.py:1224 reads operator_label_sentence |
| `autonomy.floor_name` | CONSUMED | tessera/config.py:863 floor_label; read by tessera/autonomy.py:1529 and tessera/sessions.py:1071 |
| `identity.enabled` | CONSUMED | tessera/sessions.py:766 ensure_operator runs only when enabled; tessera/board.py:280 and :715 resolve owners through the registry |
| `swarm.pressure_hold_enabled` | CONSUMED | tessera/scheduler.py pressure_allows holds a listed local engine when the switch is on, in Safe mode and in Unleashed |
| `swarm.pressure_soft_threshold` | CONSUMED | tessera/scheduler.py:138 holds a backend when occupied >= slots_total * threshold |
| `swarm.max_job_tasks` | CONSUMED | tessera/swarm_bounds.py job_task_limit; tessera/jobs.py refuses a job larger than the effective bound. Unset allows 8 in Safe mode and no cap in Unleashed. A saved integer holds in both modes |
| `swarm.distinct_workers` | CONSUMED | tessera/swarm_bounds.py require_distinct_workers; tessera/jobs.py requires a distinct backend and account when the guard is on. Unset is on in Safe mode and off in Unleashed. A saved true or false holds in both modes. Overlapping scopes stay refused |
| `roles.main` | CONSUMED | tessera/config.py:1278 backend_for resolves the role to a backend |
| `roles.fast` | CONSUMED | tessera/loop.py:99 uses the fast role for direct-routed turns |
| `roles.compactor` | CONSUMED | tessera/loop.py:209 uses the compactor role for background summaries |
| `roles.failover` | CONSUMED | tessera/loop.py:629 builds the failover backend on a failed turn |
| `codex.models_url` | CONSUMED | tessera/model_discovery.py codex_request_url; discover_codex_models GETs it with the stored sign-in token |
| `codex.client_version` | CONSUMED | tessera/model_discovery.py codex_request_url appends client_version to that request |
| `computer_use.provider` | CONSUMED | tessera/mcp_servers.py:64 computer_backend; on macOS an empty provider also returns native — partial, baseline debt |
| `computer_use.timeout` | CONSUMED | tessera/native_cua.py:39 NativeHelper.timeout |
| `computer_use.jev.enabled` | CONSUMED | tessera/native_cua.py:286 registers Jev screen tools only when enabled |
| `computer_use.jev.credential_ref` | CONSUMED | tessera/cua_jev.py:31 key_available short-circuits on a credential_ref |
| `computer_use.jev.base_url` | CONSUMED | tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165 |
| `computer_use.jev.model` | CONSUMED | tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165 |
| `computer_use.jev.timeout` | CONSUMED | tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165 |
| `computer_use.jev.min_confidence` | CONSUMED | tessera/cua_jev.py:215 confidence compared against min_confidence |
| `aethermind.source` | CONSUMED | tessera/bundled_engine.py _wants_external: embedded stays on the packaged engine; external is used only after it validates |
| `aethermind.url` | CONSUMED | tessera/bundled_engine.py probe_external validates this URL; loop.py uses it only after that probe succeeds |
| `aethermind.project` | CONSUMED | tessera/loop.py:161 passes the project to Capture |
| `aethermind.bundled_path` | CONSUMED | tessera/bundled_engine.py:30 _engine_root resolves the packaged vs configured engine |
| `aethermind.engine_log_max_bytes` | CONSUMED | tessera/bundled_engine.py _log_limit keeps the engine log to this many bytes; engine_log_text returns that cap |
| `autonomy.enabled` | CONSUMED | tessera/autonomy.py:564 starts the floor thread only when enabled |
| `autonomy.infinity_slug` | CONSUMED | tessera/autonomy.py:557 floor_slug keys the floor session and cockpit lane |
| `autonomy.world_view` | CONSUMED | tessera/floor_definition.py:775 effective_world_view reads config.autonomy.world_view until a definition file is saved |
| `autonomy.min_wake_gap_seconds` | CONSUMED | tessera/autonomy.py:980 _cap_reason refuses a wake inside the gap |
| `autonomy.self_prompt_gap_seconds` | CONSUMED | tessera/autonomy.py:1506 and :1563 gate self-prompt refreshes on the gap |
| `autonomy.oversight_policy` | CONSUMED | tessera/oversight.py:446 selects report content; tessera/sessions.py:1604 echoes it |
| `autonomy.trial_poll_seconds` | CONSUMED | tessera/sessions.py:1571 throttle on trial checks |
| `jev.enabled` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config reads the whole jev block |
| `jev.action_gate_enabled` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config; gates mutating tools in the session loop |
| `jev.url` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.model` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.api_key_env` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.credential_ref` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.timeout` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.gate_threshold` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config; bar for session/merge/completion gates |
| `jev.goal_threshold` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.risk_threshold` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config; session-tool risk enforcement |
| `jev.loop_window` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.batch_items` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config |
| `jev.human_gate` | CONSUMED | tessera/jev.py:89 JudgeKit.from_config; tessera/board_tools.py:91 ask_human_gate consults it |
| `jev.session_gates_enabled` | CONSUMED | tessera/loop.py:928 and :1000 gate session tools on it (jev.py:91 pops it from the from_config dict, so the loop reads config directly) |
| `jev.completion_review_enabled` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.enabled and review select completion checkpoints |
| `jev.plan_review_enabled` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.enabled and review select plan checkpoints |
| `jev.handoff_review_enabled` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.enabled and review select handoff checkpoints |
| `jev.summary_review_enabled` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.enabled and review select summary checkpoints |
| `jev.lesson_review_enabled` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.enabled and review select lesson checkpoints |
| `jev.review_max_calls_per_turn` | CONSUMED | tessera/jev_review.py AdvisoryReviewer.review stops optional review attempts at this turn budget |
| `jev.review_max_input_chars` | CONSUMED | tessera/jev_review.py AdvisoryReviewer._packet bounds the serialized evidence and question payload |
| `voice.enabled` | CONSUMED | tessera/voice.py:187, :244, :280 all branch on settings.enabled |
| `voice.account` | CONSUMED | tessera/settings_model.py _voice_account writes voice.credential_ref; tessera/voice.py configured() requires it to mint |
| `voice.model` | CONSUMED | tessera/voice.py:78 configured() |
| `voice.websocket_url` | CONSUMED | tessera/voice.py:78 configured() |
| `voice.pipe` | CONSUMED | tessera/voice.py selected_pipe() chooses the pipe a start uses; VoiceSessions.start refuses the local pipe until its runtime lands |
| `voice.stt.base_url` | CONSUMED | tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model |
| `voice.stt.model` | CONSUMED | tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model |
| `voice.tts.base_url` | CONSUMED | tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model |
| `voice.tts.model` | CONSUMED | tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model |
| `voice.voice` | CONSUMED | tessera/voice.py:388 copies session_options into the lease; the voice name selects the spoken voice on next start |
| `voice.max_session_seconds` | CONSUMED | tessera/voice.py:375 added to the session deadline |
| `voice.dedicated_title` | CONSUMED | tessera/sessions.py:973 seeds the dedicated voice session title; :1025 reconciles it on restart |
| `voice.dedicated_reasoning` | CONSUMED | tessera/sessions.py:980, :1026 seeds/reconciles the dedicated session reasoning |
| `voice.dedicated_thinking_budget` | CONSUMED | tessera/sessions.py:981, :1026 seeds/reconciles the thinking budget; tessera/voice.py:130 validates it |
| `ui.theme` | CONSUMED | tessera/sessions.py:1870 status ui block; web/app.js applies it |
| `ui.mark` | CONSUMED | tessera/sessions.py:2066 status ui block publishes mark; web/app.js applyMark paints the header and icons. Values: cairn, gremlin. |
| `ui.skin` | CONSUMED | tessera/sessions.py:1870 status ui block; web/app.js applies it |
| `ui.accent_color` | CONSUMED | tessera/sessions.py:1870 status ui block; web/app.js:6780 applyAccent applies it |
| `ui.show_thoughts` | CONSUMED | tessera/sessions.py:1871 status ui block; web/app.js applies it |
| `ui.yolo_banner` | CONSUMED | tessera/sessions.py:1803 status ui block; web/app.js shows the banner from it |
| `ui.easter_egg_f_it` | CONSUMED | tessera/sessions.py:2063 status ui block; web/unleashed-label.js:76 paints the approval button from it; web/app.js:6961 paintApprovalLabel applies it |
| `ui.send_shortcut` | CONSUMED | tessera/sessions.py:1872 status ui block; web/app.js:6782 applyShortcut applies it |
| `ui.config_journal_entries` | CONSUMED | tessera/config_editor.py change_report keeps and shows this many config changes; tessera/settings_model.py SettingsService.view publishes shown and total |
| `permissions.mode` | CONSUMED | tessera/settings_model.py:405 reads the approval dict; tessera/sessions.py:1848 set_yolo writes it |
| `permissions.safe_policy` | CONSUMED | tessera/sessions.py:2129 _init_approval derives the safe mode from tools.approval; set_yolo(false) restores it |
| `accounts.*.credential_ref` | CONSUMED | tessera/config.py:1292 becomes the backend credential reference; tessera/settings_ladder.py credential_source reads it |
| `accounts.*.provider_kind` | CONSUMED | tessera/config.py:1227 and :1291 route the backend through the account kind; tessera/agents.py:54,:98 branch per kind |
| `credentials.*.env` | CONSUMED | tessera/credentials.py:98,:100 resolves the value from that environment variable |
| `credentials.*.keychain_account` | CONSUMED | tessera/credentials.py:114,:118 the keychain lookup account argument |
| `credentials.*.keychain_service` | CONSUMED | tessera/credentials.py:110,_keychain runs security/secret-tool with the service |
| `credentials.*.secret` | CONSUMED | tessera/settings_model.py:1668 _store_secret writes it to the keychain; credentials.py:143 put is the write path |
| `hosts.*.address` | CONSUMED | tessera/infrastructure.py:75,:111,:288 probe target for local and ssh kinds |
| `hosts.*.enabled` | CONSUMED | tessera/infrastructure.py:275 a disabled host is not watched; :366 reported in the row |
| `hosts.*.kind` | CONSUMED | tessera/infrastructure.py:83,:108 the kind selects the probe path (prometheus/local/ssh/agent) |
| `hosts.*.label` | CONSUMED | tessera/infrastructure.py:368,:369 the board shows this label |
| `hosts.*.probe_command` | CONSUMED | tessera/infrastructure.py:70,:111,:283,:288 the argv run for kind=local/ssh |
| `hosts.*.prometheus_instance` | CONSUMED | tessera/infrastructure.py:233 substituted into the PromQL query template |
| `hosts.*.ssh_alias` | CONSUMED | tessera/infrastructure.py:75,:111,:288 ssh target alias for kind=ssh |
| `hosts.*.timeout` | CONSUMED | tessera/infrastructure.py:68,:289,:303 probe and request deadline |
| `identities.display_name` | CONSUMED | tessera/identities.py:233 rename; settings_model.py:601 renders it |
| `identities.name` | CONSUMED | tessera/board.py:280,:715 resolve an owner handle; tessera/identities.py:233 rename |
| `mcp_servers.*.args` | CONSUMED | tessera/mcp_servers.py:32 passed into the stdio server launch |
| `mcp_servers.*.command` | CONSUMED | tessera/mcp_servers.py:32 StdioMcpClient(command, ...) launches it |
| `mcp_servers.*.enabled` | CONSUMED | tessera/mcp_servers.py:58,:346 session_plugin_servers offers the server only when enabled; register_mcp_server_tools registers only enabled servers |
| `mcp_servers.*.env` | CONSUMED | tessera/mcp_servers.py:32 env= reaches StdioMcpClient |
| `mcp_servers.*.session_tools` | CONSUMED | tessera/mcp_servers.py:58 session_plugin_servers offers the server's tools only when session_tools is on |
| `mcp_servers.*.token_env` | CONSUMED | tessera/mcp_servers.py:31 open_server passes token_env to McpClient; the token value is read from that variable at call time |
| `mcp_servers.*.url` | CONSUMED | tessera/mcp_servers.py:30,:31 McpClient(url, ...) for an HTTP server |
| `oauth_accounts.sign-in` | CONSUMED | tessera/account_oauth.py grant flow; refresh action in _oauth_actions (settings_model.py:491) |
| `providers.*.allow_api_key_fallback` | CONSUMED | tessera/grok_worker.py:30 refuses the API-key fallback unless explicitly true; tessera/settings_ladder.py credential_source names it when on |
| `providers.*.args` | CONSUMED | tessera/cli_worker.py:110 argument list passed to the CLI; empty uses the vendor default argv |
| `providers.*.base_url` | CONSUMED | tessera/backend.py:91 stores it; :200,:244 every request URL is built from it |
| `providers.*.chat` | CONSUMED | tessera/config.py:45 offered_for_chat excludes chat=false backends from every chat role |
| `providers.*.command` | CONSUMED | tessera/cli_worker.py:105 requires it; :107 shutil.which resolves it; :268 Popen runs it |
| `providers.*.credential` | CONSUMED | tessera/backend.py:228 _credentials.resolve(credential_ref) on each call; tessera/settings_ladder.py credential_source names that reference |
| `providers.*.cwd` | CONSUMED | tessera/cli_worker.py:105 requires it; :268 Popen cwd argument |
| `providers.*.enabled` | CONSUMED | tessera/config.py:1278 backend_for refuses a disabled backend ("Backend X is disabled"); settings_model.py:186 drops it from chat options |
| `providers.*.fallback_credential` | CONSUMED | tessera/grok_worker.py:31,:35 resolves the named credential for the fallback |
| `providers.*.fallback_env` | CONSUMED | tessera/grok_worker.py:31,:40 injects the fallback credential into that env var for the CLI process |
| `providers.*.max_concurrent_requests` | CONSUMED | tessera/endpoint_limit.py:43 request_limit; tessera/backend.py:189 chat holds one slot and bind_endpoint re-reads a hand-set limit; tessera/jobs.py:264 each swarm worker waits on that slot |
| `providers.*.model` | CONSUMED | tessera/backend.py:92; sent on every chat request; cli_worker.py:163 conflicts checked against it |
| `providers.*.throttle_requests_per_minute` | CONSUMED | tessera/endpoint_throttle.py:24 throttle_limits; tessera/backend.py:189 and tessera/jobs.py pace that endpoint in requests per minute. A hand-set number is enforced in Unleashed; there is no system default |
| `providers.*.throttle_tokens_per_minute` | CONSUMED | tessera/endpoint_throttle.py:24 throttle_limits; tessera/backend.py:189 charges reported usage. A hand-set number is enforced in Unleashed; there is no system default |
| `providers.*.vendor` | CONSUMED | tessera/cli_worker.py:105 requires a vendor; :200,:211,:223,:229,:333,:360 branch per vendor |

## NO-READER and UNRESOLVED fields

None at HEAD.
