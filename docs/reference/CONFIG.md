# Configuration reference

Generated from `tessera/config.py` dataclasses and loader declarations, plus
`CredentialReference`, with availability from `settings_model.catalog` and explicit retired-key annotations.
Do not edit by hand. Regenerate from the repository root:

```sh
.venv/bin/python scripts/gen-config-reference.py
```

35 top-level tables and 7 top-level scalar keys.

Defaults below are declared defaults, before validation or runtime discovery.
No host config, environment values, credential values or runtime paths are read.
`cwd` defaults to the launch directory. Named tables default to `{}`; `*` is
a caller-chosen name, supplied as the dataclass name by the loader.

Env values use TOML types when parseable, otherwise text. The loader recognizes
single-underscore section prefixes only for: `inspector`, `aethermind`, `jev`, `view`, `tools`, `agents`, `roles`, `server`, `daemon`, `compactor`, `accounts`, `credentials`, `swarm`, `status`, `channels`, `board`, `hosts`, `systems`, `infrastructure`, `grafana`, `voice`, `autonomy`, `cua`, `computer_use`, `prompt`, `skills`, `codex`.
Double underscores separate every nested path component (including `*`).
`TESSERA_HOME` selects the home; it is not a table override. Credential selector
fields ending in `token_env` or `api_key_env` name secret variables; configure
these selectors in TOML. The variables they name are excluded from overrides,
as are other configured credential source variables and `TESSERA_SERVER_TOKEN`.

Availability is derived from stored paths in the Settings catalog, from
record-row editors named in this generator, and from the MCP server fields
the Plugins editor accepts. ``name`` is the entry key, not a field. A
file-only row has no catalog Spec, no named record-row entry, and none of
those editor fields for that exact path. The environment override still
applies where listed.
Retired keys remain accepted for compatibility but are ignored.

## Top-level scalars

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `cwd` | launch directory | `TESSERA_CWD` | file-only: no Settings row — set this in config.toml |
| `workspace` | `""` | `TESSERA_WORKSPACE` | file-only: no Settings row — set this in config.toml |
| `agent_name` | `"Tessera"` | `TESSERA_AGENT_NAME` | Settings row |
| `operator_name` | `""` | `TESSERA_OPERATOR_NAME` | Settings row |
| `user_instructions` | `""` | `TESSERA_USER_INSTRUCTIONS` | file-only: no Settings row — set this in config.toml |
| `identity_file` | `""` | `TESSERA_IDENTITY_FILE` | file-only: no Settings row — set this in config.toml |
| `identity_max_bytes` | `65536` | `TESSERA_IDENTITY_MAX_BYTES` | file-only: no Settings row — set this in config.toml |

## `accounts`

Default: `{}`. Entries live under `accounts.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `accounts.*.provider_kind` | `"openai_chat"` | `TESSERA_ACCOUNTS__*__PROVIDER_KIND` | file-only: no Settings row — set this in config.toml |
| `accounts.*.credential_ref` | `""` | `TESSERA_ACCOUNTS__*__CREDENTIAL_REF` | file-only: no Settings row — set this in config.toml |
| `accounts.*.limits` | `{}` | `TESSERA_ACCOUNTS__*__LIMITS` | file-only: no Settings row — set this in config.toml |
| `accounts.*.budget` | `{}` | `TESSERA_ACCOUNTS__*__BUDGET` | file-only: no Settings row — set this in config.toml |
| `accounts.*.limits.initial_concurrency` | `"ignored"` | `TESSERA_ACCOUNTS__*__LIMITS__INITIAL_CONCURRENCY` | retired: nothing reads it (accepted for compatibility; ignored); file-only: no Settings row |
| `accounts.*.limits.max_concurrent` | `"ignored"` | `TESSERA_ACCOUNTS__*__LIMITS__MAX_CONCURRENT` | retired: not a cap; set max_concurrent_requests on the model (accepted for compatibility; ignored); file-only: no Settings row |

## `aethermind`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `aethermind.url` | `""` | `TESSERA_AETHERMIND_URL` | Settings row |
| `aethermind.auto_recall` | `true` | `TESSERA_AETHERMIND_AUTO_RECALL` | file-only: no Settings row — set this in config.toml |
| `aethermind.endpoints` | `[]` | `TESSERA_AETHERMIND_ENDPOINTS` | Settings row |
| `aethermind.token_env` | `"AETHERMIND_TOKEN"` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `aethermind.project` | `""` | `TESSERA_AETHERMIND_PROJECT` | Settings row |
| `aethermind.personal_project` | `""` | `TESSERA_AETHERMIND_PERSONAL_PROJECT` | file-only: no Settings row — set this in config.toml |
| `aethermind.author` | `"tessera"` | `TESSERA_AETHERMIND_AUTHOR` | file-only: no Settings row — set this in config.toml |
| `aethermind.source` | `""` | `TESSERA_AETHERMIND_SOURCE` | Settings row |
| `aethermind.timeout` | `15.0` | `TESSERA_AETHERMIND_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `aethermind.probe_timeout` | `2.0` | `TESSERA_AETHERMIND_PROBE_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `aethermind.probe_interval` | `30.0` | `TESSERA_AETHERMIND_PROBE_INTERVAL` | file-only: no Settings row — set this in config.toml |
| `aethermind.create_project` | `false` | `TESSERA_AETHERMIND_CREATE_PROJECT` | file-only: no Settings row — set this in config.toml |
| `aethermind.note_project` | `""` | `TESSERA_AETHERMIND_NOTE_PROJECT` | file-only: no Settings row — set this in config.toml |
| `aethermind.personal_read_only` | `true` | `TESSERA_AETHERMIND_PERSONAL_READ_ONLY` | file-only: no Settings row — set this in config.toml |
| `aethermind.bundled_path` | `""` | `TESSERA_AETHERMIND_BUNDLED_PATH` | Settings row |
| `aethermind.engine_log_max_bytes` | `1048576` | `TESSERA_AETHERMIND_ENGINE_LOG_MAX_BYTES` | Settings row |
| `aethermind.recall_scan` | `400` | `TESSERA_AETHERMIND_RECALL_SCAN` | file-only: no Settings row — set this in config.toml |
| `aethermind.recall_limit` | `10` | `TESSERA_AETHERMIND_RECALL_LIMIT` | file-only: no Settings row — set this in config.toml |
| `aethermind.capture_omit_outcomes` | `["memory_recall", "memory_tree", "session_briefs"]` | `TESSERA_AETHERMIND_CAPTURE_OMIT_OUTCOMES` | file-only: no Settings row — set this in config.toml |
| `aethermind.inheritance_refresh_seconds` | `900.0` | `TESSERA_AETHERMIND_INHERITANCE_REFRESH_SECONDS` | file-only: no Settings row — set this in config.toml |
| `aethermind.enabled` | `"ignored"` | `TESSERA_AETHERMIND_ENABLED` | retired: memory is required; the packaged engine runs unless an external service validates (accepted for compatibility; ignored); file-only: no Settings row |

## `agents`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `agents.max_concurrent` | `0` | `TESSERA_AGENTS_MAX_CONCURRENT` | file-only: no Settings row — set this in config.toml |
| `agents.concurrency_by_kind` | `{}` | `TESSERA_AGENTS_CONCURRENCY_BY_KIND` | file-only: no Settings row — set this in config.toml |
| `agents.max_depth` | `2` | `TESSERA_AGENTS_MAX_DEPTH` | file-only: no Settings row — set this in config.toml |
| `agents.enabled` | `false` | `TESSERA_AGENTS_ENABLED` | file-only: no Settings row — set this in config.toml |
| `agents.token_budget` | `0` | `TESSERA_AGENTS_TOKEN_BUDGET` | file-only: no Settings row — set this in config.toml |
| `agents.judge_tokens` | `0` | `TESSERA_AGENTS_JUDGE_TOKENS` | file-only: no Settings row — set this in config.toml |
| `agents.wall_seconds` | `0.0` | `TESSERA_AGENTS_WALL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `agents.view_bytes` | `16000` | `TESSERA_AGENTS_VIEW_BYTES` | file-only: no Settings row — set this in config.toml |
| `agents.runs_dir` | `"agent-runs"` | `TESSERA_AGENTS_RUNS_DIR` | file-only: no Settings row — set this in config.toml |
| `agents.planner_tokens` | `0` | `TESSERA_AGENTS_PLANNER_TOKENS` | file-only: no Settings row — set this in config.toml |
| `agents.worker_backend` | `""` | `TESSERA_AGENTS_WORKER_BACKEND` | file-only: no Settings row — set this in config.toml |
| `agents.backend_policy` | `{}` | `TESSERA_AGENTS_BACKEND_POLICY` | file-only: no Settings row — set this in config.toml |
| `agents.allowed_scopes` | `[]` | `TESSERA_AGENTS_ALLOWED_SCOPES` | file-only: no Settings row — set this in config.toml |
| `agents.worktrees_enabled` | `false` | `TESSERA_AGENTS_WORKTREES_ENABLED` | file-only: no Settings row — set this in config.toml |
| `agents.commit_children` | `false` | `TESSERA_AGENTS_COMMIT_CHILDREN` | file-only: no Settings row — set this in config.toml |
| `agents.worktree_result_format` | `"native"` | `TESSERA_AGENTS_WORKTREE_RESULT_FORMAT` | file-only: no Settings row — set this in config.toml |
| `agents.worktree_context_files` | `[]` | `TESSERA_AGENTS_WORKTREE_CONTEXT_FILES` | file-only: no Settings row — set this in config.toml |
| `agents.worktree_context_bytes` | `128000` | `TESSERA_AGENTS_WORKTREE_CONTEXT_BYTES` | file-only: no Settings row — set this in config.toml |
| `agents.repo_root` | `""` | `TESSERA_AGENTS_REPO_ROOT` | file-only: no Settings row — set this in config.toml |
| `agents.worktrees_dir` | `"agent-worktrees"` | `TESSERA_AGENTS_WORKTREES_DIR` | file-only: no Settings row — set this in config.toml |
| `agents.git_command` | `"git"` | `TESSERA_AGENTS_GIT_COMMAND` | file-only: no Settings row — set this in config.toml |
| `agents.worktree_timeout` | `30.0` | `TESSERA_AGENTS_WORKTREE_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `agents.validation_timeout` | `120.0` | `TESSERA_AGENTS_VALIDATION_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `agents.validation_commands` | `[]` | `TESSERA_AGENTS_VALIDATION_COMMANDS` | file-only: no Settings row — set this in config.toml |

`agents.local_serving_slots` is retired. A config that still sets it loads, records a migration note naming the saved value, and does not apply it. Local agent lanes use each model's `max_concurrent_requests`. A positive `agents.max_concurrent` remains an operator ceiling over every lane in both modes. Zero leaves local lanes to the endpoint.

## `autonomy`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `autonomy.enabled` | `false` | `TESSERA_AUTONOMY_ENABLED` | Settings row |
| `autonomy.state_file` | `"autonomy/state.json"` | `TESSERA_AUTONOMY_STATE_FILE` | file-only: no Settings row — set this in config.toml |
| `autonomy.tasks_dir` | `"autonomy/tasks"` | `TESSERA_AUTONOMY_TASKS_DIR` | file-only: no Settings row — set this in config.toml |
| `autonomy.tick_seconds` | `1.0` | `TESSERA_AUTONOMY_TICK_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.min_wake_gap_seconds` | `60.0` | `TESSERA_AUTONOMY_MIN_WAKE_GAP_SECONDS` | Settings row |
| `autonomy.max_wakes_per_hour` | `0` | `TESSERA_AUTONOMY_MAX_WAKES_PER_HOUR` | file-only: no Settings row — set this in config.toml |
| `autonomy.max_wakes_per_day` | `0` | `TESSERA_AUTONOMY_MAX_WAKES_PER_DAY` | file-only: no Settings row — set this in config.toml |
| `autonomy.max_timers` | `24` | `TESSERA_AUTONOMY_MAX_TIMERS` | file-only: no Settings row — set this in config.toml |
| `autonomy.min_timer_seconds` | `30.0` | `TESSERA_AUTONOMY_MIN_TIMER_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.max_timer_seconds` | `604800.0` | `TESSERA_AUTONOMY_MAX_TIMER_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.max_tasks` | `4` | `TESSERA_AUTONOMY_MAX_TASKS` | file-only: no Settings row — set this in config.toml |
| `autonomy.task_max_seconds` | `3600.0` | `TESSERA_AUTONOMY_TASK_MAX_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.task_stall_seconds` | `900.0` | `TESSERA_AUTONOMY_TASK_STALL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.keep_finished_tasks` | `50` | `TESSERA_AUTONOMY_KEEP_FINISHED_TASKS` | file-only: no Settings row — set this in config.toml |
| `autonomy.wake_output_chars` | `1500` | `TESSERA_AUTONOMY_WAKE_OUTPUT_CHARS` | file-only: no Settings row — set this in config.toml |
| `autonomy.presence_away_seconds` | `300.0` | `TESSERA_AUTONOMY_PRESENCE_AWAY_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.engaged_message_seconds` | `300.0` | `TESSERA_AUTONOMY_ENGAGED_MESSAGE_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.engaged_typing_seconds` | `120.0` | `TESSERA_AUTONOMY_ENGAGED_TYPING_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.engaged_focus_seconds` | `900.0` | `TESSERA_AUTONOMY_ENGAGED_FOCUS_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.presence_stale_seconds` | `300.0` | `TESSERA_AUTONOMY_PRESENCE_STALE_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.engaged_wake_kinds` | `["approval"]` | `TESSERA_AUTONOMY_ENGAGED_WAKE_KINDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.infra_poll_seconds` | `300.0` | `TESSERA_AUTONOMY_INFRA_POLL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.endpoint_poll_seconds` | `60.0` | `TESSERA_AUTONOMY_ENDPOINT_POLL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.endpoint_timeout_seconds` | `5.0` | `TESSERA_AUTONOMY_ENDPOINT_TIMEOUT_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.approval_expire_seconds` | `3600.0` | `TESSERA_AUTONOMY_APPROVAL_EXPIRE_SECONDS` | file-only: no Settings row — set this in config.toml |
| `autonomy.self_prompt_enabled` | `true` | `TESSERA_AUTONOMY_SELF_PROMPT_ENABLED` | file-only: no Settings row — set this in config.toml |
| `autonomy.self_prompt_gap_seconds` | `900.0` | `TESSERA_AUTONOMY_SELF_PROMPT_GAP_SECONDS` | Settings row |
| `autonomy.board_offer_limit` | `3` | `TESSERA_AUTONOMY_BOARD_OFFER_LIMIT` | file-only: no Settings row — set this in config.toml |
| `autonomy.standing_floor` | `true` | `TESSERA_AUTONOMY_STANDING_FLOOR` | file-only: no Settings row — set this in config.toml |
| `autonomy.infinity_slug` | `"infinity"` | `TESSERA_AUTONOMY_INFINITY_SLUG` | Settings row |
| `autonomy.floor_name` | `""` | `TESSERA_AUTONOMY_FLOOR_NAME` | Settings row |
| `autonomy.world_view` | `""` | `TESSERA_AUTONOMY_WORLD_VIEW` | Settings row |
| `autonomy.watch_sources` | `[]` | `TESSERA_AUTONOMY_WATCH_SOURCES` | file-only: no Settings row — set this in config.toml |
| `autonomy.lesson_file` | `"learning/lessons.json"` | `TESSERA_AUTONOMY_LESSON_FILE` | file-only: no Settings row — set this in config.toml |
| `autonomy.lesson_pass_score` | `0.5` | `TESSERA_AUTONOMY_LESSON_PASS_SCORE` | file-only: no Settings row — set this in config.toml |
| `autonomy.max_open_lessons` | `12` | `TESSERA_AUTONOMY_MAX_OPEN_LESSONS` | file-only: no Settings row — set this in config.toml |
| `autonomy.lesson_fail_limit` | `2` | `TESSERA_AUTONOMY_LESSON_FAIL_LIMIT` | file-only: no Settings row — set this in config.toml |
| `autonomy.oversight_policy` | `"approve"` | `TESSERA_AUTONOMY_OVERSIGHT_POLICY` | Settings row |
| `autonomy.trial_poll_seconds` | `60.0` | `TESSERA_AUTONOMY_TRIAL_POLL_SECONDS` | Settings row |

## `backends`

Default: `{}`. Entries live under `backends.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `backends.*.base_url` | `""` | `TESSERA_BACKENDS__*__BASE_URL` | file-only: no Settings row — set this in config.toml |
| `backends.*.model` | `""` | `TESSERA_BACKENDS__*__MODEL` | file-only: no Settings row — set this in config.toml |
| `backends.*.api_key_env` | `""` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `backends.*.extra` | `{}` | `TESSERA_BACKENDS__*__EXTRA` | file-only: no Settings row — set this in config.toml |
| `backends.*.provider_kind` | `""` | `TESSERA_BACKENDS__*__PROVIDER_KIND` | file-only: no Settings row — set this in config.toml |
| `backends.*.credential_ref` | `""` | `TESSERA_BACKENDS__*__CREDENTIAL_REF` | file-only: no Settings row — set this in config.toml |
| `backends.*.account` | `""` | `TESSERA_BACKENDS__*__ACCOUNT` | file-only: no Settings row — set this in config.toml |
| `backends.*.chat` | `true` | `TESSERA_BACKENDS__*__CHAT` | file-only: no Settings row — set this in config.toml |
| `backends.*.enabled` | `true` | `TESSERA_BACKENDS__*__ENABLED` | file-only: no Settings row — set this in config.toml |
| `backends.*.max_concurrent_requests` | `null` | `TESSERA_BACKENDS__*__MAX_CONCURRENT_REQUESTS` | Settings row |
| `backends.*.throttle_requests_per_minute` | `null` | `TESSERA_BACKENDS__*__THROTTLE_REQUESTS_PER_MINUTE` | Settings row |
| `backends.*.throttle_tokens_per_minute` | `null` | `TESSERA_BACKENDS__*__THROTTLE_TOKENS_PER_MINUTE` | Settings row |

Additional backend fields are retained in `extra`; `extra` accepts provider-specific
request options, including `parameters`. Names and defaults depend on the provider.

`max_concurrent_requests` is how many requests may run at once on that one model.
`null` means unset. A positive integer you set by hand is the cap in Safe mode and in Unleashed mode.
Safe mode, when the field is omitted and the model has no credential, allows 1
request at a time. That default is not saved. Unleashed mode drops it and does not add a number.
A model that names a credential or a vendor CLI stays
unlimited until you set a number by hand. That follows the credential, not the host name
or the model id. A hand-set number is also the
local agent lane, and the chat holds one slot. An omitted number does not
refuse a local subagent. A cloud lane stays unbounded unless `[agents]` sets
`max_concurrent` or `concurrency_by_kind`. `agents.local_serving_slots`
is retired: a file that still sets it is noted at load and ignored.

`throttle_requests_per_minute` and `throttle_tokens_per_minute` pace that one endpoint.
Both default to `null`, which is unlimited in Safe mode and in Unleashed mode.
There is no system default. A positive integer you set by hand is enforced in both modes.
Unleashed drops only defaults the system adds, and these fields add none.
The pace is not a swarm-wide cap. Token pace counts usage reported when the request finishes.
Environment: `TESSERA_BACKENDS__<NAME>__THROTTLE_REQUESTS_PER_MINUTE` and
`TESSERA_BACKENDS__<NAME>__THROTTLE_TOKENS_PER_MINUTE`.

## `board`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `board.enabled` | `false` | `TESSERA_BOARD_ENABLED` | file-only: no Settings row — set this in config.toml |
| `board.journal_file` | `"board.jsonl"` | `TESSERA_BOARD_JOURNAL_FILE` | file-only: no Settings row — set this in config.toml |
| `board.max_text_bytes` | `8192` | `TESSERA_BOARD_MAX_TEXT_BYTES` | file-only: no Settings row — set this in config.toml |
| `board.max_artifacts` | `32` | `TESSERA_BOARD_MAX_ARTIFACTS` | file-only: no Settings row — set this in config.toml |
| `board.max_questions` | `32` | `TESSERA_BOARD_MAX_QUESTIONS` | file-only: no Settings row — set this in config.toml |
| `board.judge_asks` | `false` | `TESSERA_BOARD_JUDGE_ASKS` | file-only: no Settings row — set this in config.toml |
| `board.acceptance_required` | `false` | `TESSERA_BOARD_ACCEPTANCE_REQUIRED` | file-only: no Settings row — set this in config.toml |
| `board.acceptance_by` | `[]` | `TESSERA_BOARD_ACCEPTANCE_BY` | file-only: no Settings row — set this in config.toml |
| `board.ask_human_threshold` | `0.7` | `TESSERA_BOARD_ASK_HUMAN_THRESHOLD` | file-only: no Settings row — set this in config.toml |

## `channels`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `channels.enabled` | `false` | `TESSERA_CHANNELS_ENABLED` | file-only: no Settings row — set this in config.toml |
| `channels.journal_file` | `"channels.jsonl"` | `TESSERA_CHANNELS_JOURNAL_FILE` | file-only: no Settings row — set this in config.toml |
| `channels.auto_deliver` | `true` | `TESSERA_CHANNELS_AUTO_DELIVER` | file-only: no Settings row — set this in config.toml |
| `channels.auto_reply` | `false` | `TESSERA_CHANNELS_AUTO_REPLY` | file-only: no Settings row — set this in config.toml |
| `channels.max_hops` | `4` | `TESSERA_CHANNELS_MAX_HOPS` | file-only: no Settings row — set this in config.toml |
| `channels.rate_limit` | `20` | `TESSERA_CHANNELS_RATE_LIMIT` | file-only: no Settings row — set this in config.toml |
| `channels.rate_window` | `60.0` | `TESSERA_CHANNELS_RATE_WINDOW` | file-only: no Settings row — set this in config.toml |
| `channels.rate_overrides` | `{}` | `TESSERA_CHANNELS_RATE_OVERRIDES` | file-only: no Settings row — set this in config.toml |
| `channels.max_text_bytes` | `65536` | `TESSERA_CHANNELS_MAX_TEXT_BYTES` | file-only: no Settings row — set this in config.toml |
| `channels.delivery_limit` | `100` | `TESSERA_CHANNELS_DELIVERY_LIMIT` | file-only: no Settings row — set this in config.toml |
| `channels.needs_reply_threshold` | `0.7` | `TESSERA_CHANNELS_NEEDS_REPLY_THRESHOLD` | file-only: no Settings row — set this in config.toml |
| `channels.block_cycles` | `true` | `TESSERA_CHANNELS_BLOCK_CYCLES` | file-only: no Settings row — set this in config.toml |

## `codex`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `codex.models_url` | `"https://chatgpt.com/backend-api/codex/models"` | `TESSERA_CODEX_MODELS_URL` | Settings row |
| `codex.client_version` | `"99.0.0"` | `TESSERA_CODEX_CLIENT_VERSION` | Settings row |

## `compactor`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `compactor.workers` | `4` | `TESSERA_COMPACTOR_WORKERS` | file-only: no Settings row — set this in config.toml |
| `compactor.retries` | `2` | `TESSERA_COMPACTOR_RETRIES` | file-only: no Settings row — set this in config.toml |
| `compactor.context_bytes` | `16000` | `TESSERA_COMPACTOR_CONTEXT_BYTES` | file-only: no Settings row — set this in config.toml |
| `compactor.judge_enabled` | `false` | `TESSERA_COMPACTOR_JUDGE_ENABLED` | file-only: no Settings row — set this in config.toml |
| `compactor.judge_threshold` | `0.8` | `TESSERA_COMPACTOR_JUDGE_THRESHOLD` | file-only: no Settings row — set this in config.toml |

## `companions`

### `companions.atlas`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `companions.atlas.enabled` | `false` | `TESSERA_COMPANIONS__ATLAS__ENABLED` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.endpoints` | `[]` | `TESSERA_COMPANIONS__ATLAS__ENDPOINTS` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.token_env` | `""` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `companions.atlas.timeout` | `8.0` | `TESSERA_COMPANIONS__ATLAS__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.probe_timeout` | `2.0` | `TESSERA_COMPANIONS__ATLAS__PROBE_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.probe_interval` | `30.0` | `TESSERA_COMPANIONS__ATLAS__PROBE_INTERVAL` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.allow_index_refresh` | `false` | `TESSERA_COMPANIONS__ATLAS__ALLOW_INDEX_REFRESH` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.brief_tool` | `""` | `TESSERA_COMPANIONS__ATLAS__BRIEF_TOOL` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.contracts_tool` | `""` | `TESSERA_COMPANIONS__ATLAS__CONTRACTS_TOOL` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.read_tool` | `""` | `TESSERA_COMPANIONS__ATLAS__READ_TOOL` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.refresh_tool` | `""` | `TESSERA_COMPANIONS__ATLAS__REFRESH_TOOL` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.read_uri_prefixes` | `[]` | `TESSERA_COMPANIONS__ATLAS__READ_URI_PREFIXES` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.max_brief_bytes` | `4000` | `TESSERA_COMPANIONS__ATLAS__MAX_BRIEF_BYTES` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.max_briefs_per_session` | `3` | `TESSERA_COMPANIONS__ATLAS__MAX_BRIEFS_PER_SESSION` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.brief_arguments` | `{}` | `TESSERA_COMPANIONS__ATLAS__BRIEF_ARGUMENTS` | file-only: no Settings row — set this in config.toml |
| `companions.atlas.start_brief_background` | `false` | `TESSERA_COMPANIONS__ATLAS__START_BRIEF_BACKGROUND` | file-only: no Settings row — set this in config.toml |

### `companions.ember`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `companions.ember.enabled` | `false` | `TESSERA_COMPANIONS__EMBER__ENABLED` | file-only: no Settings row — set this in config.toml |
| `companions.ember.helmet_endpoints` | `[]` | `TESSERA_COMPANIONS__EMBER__HELMET_ENDPOINTS` | file-only: no Settings row — set this in config.toml |
| `companions.ember.companion_endpoints` | `[]` | `TESSERA_COMPANIONS__EMBER__COMPANION_ENDPOINTS` | file-only: no Settings row — set this in config.toml |
| `companions.ember.token_env` | `""` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `companions.ember.timeout` | `8.0` | `TESSERA_COMPANIONS__EMBER__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `companions.ember.probe_timeout` | `2.0` | `TESSERA_COMPANIONS__EMBER__PROBE_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `companions.ember.probe_interval` | `30.0` | `TESSERA_COMPANIONS__EMBER__PROBE_INTERVAL` | file-only: no Settings row — set this in config.toml |
| `companions.ember.surface` | `""` | `TESSERA_COMPANIONS__EMBER__SURFACE` | file-only: no Settings row — set this in config.toml |
| `companions.ember.profile` | `""` | `TESSERA_COMPANIONS__EMBER__PROFILE` | file-only: no Settings row — set this in config.toml |
| `companions.ember.host_label` | `""` | `TESSERA_COMPANIONS__EMBER__HOST_LABEL` | file-only: no Settings row — set this in config.toml |
| `companions.ember.instance_id` | `""` | `TESSERA_COMPANIONS__EMBER__INSTANCE_ID` | file-only: no Settings row — set this in config.toml |
| `companions.ember.decision_tool` | `""` | `TESSERA_COMPANIONS__EMBER__DECISION_TOOL` | file-only: no Settings row — set this in config.toml |
| `companions.ember.allow_transition` | `false` | `TESSERA_COMPANIONS__EMBER__ALLOW_TRANSITION` | file-only: no Settings row — set this in config.toml |
| `companions.ember.transition_authority_env` | `""` | `TESSERA_COMPANIONS__EMBER__TRANSITION_AUTHORITY_ENV` | file-only: no Settings row — set this in config.toml |


## `computer_use`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `computer_use.backend` | `"auto"` | `TESSERA_COMPUTER_USE_BACKEND` | file-only: no Settings row — set this in config.toml |
| `computer_use.provider` | `""` | `TESSERA_COMPUTER_USE_PROVIDER` | Settings row |
| `computer_use.tool_prefix` | `"computer_"` | `TESSERA_COMPUTER_USE_TOOL_PREFIX` | file-only: no Settings row — set this in config.toml |
| `computer_use.text_chars` | `12000` | `TESSERA_COMPUTER_USE_TEXT_CHARS` | file-only: no Settings row — set this in config.toml |
| `computer_use.helper` | `"cua/TesseraCUA.app"` | `TESSERA_COMPUTER_USE_HELPER` | file-only: no Settings row — set this in config.toml |
| `computer_use.timeout` | `30.0` | `TESSERA_COMPUTER_USE_TIMEOUT` | Settings row |
| `computer_use.jev.enabled` | `false` | `TESSERA_COMPUTER_USE__JEV__ENABLED` | Settings row |
| `computer_use.jev.base_url` | `""` | `TESSERA_COMPUTER_USE__JEV__BASE_URL` | Settings row |
| `computer_use.jev.model` | `""` | `TESSERA_COMPUTER_USE__JEV__MODEL` | Settings row |
| `computer_use.jev.api_key_env` | `"TYPESAFE_API_KEY"` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `computer_use.jev.credential_ref` | `""` | `TESSERA_COMPUTER_USE__JEV__CREDENTIAL_REF` | Settings row |
| `computer_use.jev.timeout` | `8.0` | `TESSERA_COMPUTER_USE__JEV__TIMEOUT` | Settings row |
| `computer_use.jev.min_confidence` | `0.5` | `TESSERA_COMPUTER_USE__JEV__MIN_CONFIDENCE` | Settings row |
| `computer_use.jev.max_state_chars` | `24000` | `TESSERA_COMPUTER_USE__JEV__MAX_STATE_CHARS` | file-only: no Settings row — set this in config.toml |
| `computer_use.jev.long_context` | `false` | `TESSERA_COMPUTER_USE__JEV__LONG_CONTEXT` | file-only: no Settings row — set this in config.toml |

## `credentials`

Default: `{}`. Entries live under `credentials.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `credentials.*.env` | `""` | `TESSERA_CREDENTIALS__*__ENV` | file-only: no Settings row — set this in config.toml |
| `credentials.*.keychain_service` | `""` | `TESSERA_CREDENTIALS__*__KEYCHAIN_SERVICE` | file-only: no Settings row — set this in config.toml |
| `credentials.*.keychain_account` | `""` | `TESSERA_CREDENTIALS__*__KEYCHAIN_ACCOUNT` | file-only: no Settings row — set this in config.toml |
| `credentials.*.config_ref` | `""` | `TESSERA_CREDENTIALS__*__CONFIG_REF` | file-only: no Settings row — set this in config.toml |
| `credentials.*.keychain_command` | `""` | `TESSERA_CREDENTIALS__*__KEYCHAIN_COMMAND` | file-only: no Settings row — set this in config.toml |
| `credentials.*.keychain_timeout` | `3.0` | `TESSERA_CREDENTIALS__*__KEYCHAIN_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `credentials.*.oauth` | `""` | `TESSERA_CREDENTIALS__*__OAUTH` | file-only: no Settings row — set this in config.toml |

## `cua`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `cua.mac_enabled` | `false` | `TESSERA_CUA_MAC_ENABLED` | file-only: no Settings row — set this in config.toml |
| `cua.mac_helper_app` | `"cua/TesseraMacLook.app"` | `TESSERA_CUA_MAC_HELPER_APP` | file-only: no Settings row — set this in config.toml |
| `cua.mac_bundle_id` | `"app.tessera.maclook"` | `TESSERA_CUA_MAC_BUNDLE_ID` | file-only: no Settings row — set this in config.toml |
| `cua.swiftc` | `"/usr/bin/swiftc"` | `TESSERA_CUA_SWIFTC` | file-only: no Settings row — set this in config.toml |
| `cua.codesign_identity` | `"-"` | `TESSERA_CUA_CODESIGN_IDENTITY` | file-only: no Settings row — set this in config.toml |
| `cua.screencapture` | `"/usr/sbin/screencapture"` | `TESSERA_CUA_SCREENCAPTURE` | file-only: no Settings row — set this in config.toml |
| `cua.sips` | `"/usr/bin/sips"` | `TESSERA_CUA_SIPS` | file-only: no Settings row — set this in config.toml |
| `cua.max_image_px` | `1600` | `TESSERA_CUA_MAX_IMAGE_PX` | file-only: no Settings row — set this in config.toml |
| `cua.attach_images` | `true` | `TESSERA_CUA_ATTACH_IMAGES` | file-only: no Settings row — set this in config.toml |
| `cua.timeout` | `30.0` | `TESSERA_CUA_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `cua.backend` | `"auto"` | `TESSERA_CUA_BACKEND` | file-only: no Settings row — set this in config.toml |
| `cua.screenshot_scale` | `1.0` | `TESSERA_CUA_SCREENSHOT_SCALE` | file-only: no Settings row — set this in config.toml |
| `cua.osascript` | `"osascript"` | `TESSERA_CUA_OSASCRIPT` | file-only: no Settings row — set this in config.toml |
| `cua.input_command` | `"xdotool"` | `TESSERA_CUA_INPUT_COMMAND` | file-only: no Settings row — set this in config.toml |
| `cua.window_command` | `"wmctrl"` | `TESSERA_CUA_WINDOW_COMMAND` | file-only: no Settings row — set this in config.toml |
| `cua.screenshot_commands` | `["scrot", "import", "gnome-screenshot"]` | `TESSERA_CUA_SCREENSHOT_COMMANDS` | file-only: no Settings row — set this in config.toml |

## `daemon`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `daemon.max_frame_bytes` | `8388608` | `TESSERA_DAEMON_MAX_FRAME_BYTES` | file-only: no Settings row — set this in config.toml |
| `daemon.max_pending_requests` | `32` | `TESSERA_DAEMON_MAX_PENDING_REQUESTS` | file-only: no Settings row — set this in config.toml |
| `daemon.approval_timeout` | `30.0` | `TESSERA_DAEMON_APPROVAL_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `daemon.event_poll_seconds` | `0.2` | `TESSERA_DAEMON_EVENT_POLL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `daemon.history_page_size` | `200` | `TESSERA_DAEMON_HISTORY_PAGE_SIZE` | file-only: no Settings row — set this in config.toml |
| `daemon.zoom_page_bytes` | `16000` | `TESSERA_DAEMON_ZOOM_PAGE_BYTES` | file-only: no Settings row — set this in config.toml |
| `daemon.mirror_retry_seconds` | `30.0` | `TESSERA_DAEMON_MIRROR_RETRY_SECONDS` | file-only: no Settings row — set this in config.toml |
| `daemon.mirror_dir` | `"mirror"` | `TESSERA_DAEMON_MIRROR_DIR` | file-only: no Settings row — set this in config.toml |

## `grafana`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `grafana.enabled` | `false` | `TESSERA_GRAFANA_ENABLED` | file-only: no Settings row — set this in config.toml |
| `grafana.base_url` | `""` | `TESSERA_GRAFANA_BASE_URL` | file-only: no Settings row — set this in config.toml |
| `grafana.token_env` | `""` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `grafana.credential_ref` | `""` | `TESSERA_GRAFANA_CREDENTIAL_REF` | file-only: no Settings row — set this in config.toml |
| `grafana.timeout` | `5.0` | `TESSERA_GRAFANA_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `grafana.max_response_bytes` | `1048576` | `TESSERA_GRAFANA_MAX_RESPONSE_BYTES` | file-only: no Settings row — set this in config.toml |
| `grafana.list_limit` | `100` | `TESSERA_GRAFANA_LIST_LIMIT` | file-only: no Settings row — set this in config.toml |
| `grafana.datasources_path` | `"/api/datasources"` | `TESSERA_GRAFANA_DATASOURCES_PATH` | file-only: no Settings row — set this in config.toml |
| `grafana.dashboards_path` | `"/api/search"` | `TESSERA_GRAFANA_DASHBOARDS_PATH` | file-only: no Settings row — set this in config.toml |
| `grafana.proxy_path` | `"/api/datasources/proxy/uid/{uid}/{path}"` | `TESSERA_GRAFANA_PROXY_PATH` | file-only: no Settings row — set this in config.toml |
| `grafana.embed_path` | `"/d-solo/{uid}/{slug}"` | `TESSERA_GRAFANA_EMBED_PATH` | file-only: no Settings row — set this in config.toml |
| `grafana.allowed_proxy_paths` | `[]` | `TESSERA_GRAFANA_ALLOWED_PROXY_PATHS` | file-only: no Settings row — set this in config.toml |

## `hooks`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `hooks.shell` | `[]` | `TESSERA_HOOKS__SHELL` | file-only: no Settings row — set this in config.toml |

`shell` is a list of hook objects requiring `event` and `command` strings;
additional hook options (such as `timeout`) belong to the hook consumer.

## `hosts`

Default: `{}`. Entries live under `hosts.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `hosts.*.enabled` | `false` | `TESSERA_HOSTS__*__ENABLED` | file-only: no Settings row — set this in config.toml |
| `hosts.*.address` | `""` | `TESSERA_HOSTS__*__ADDRESS` | file-only: no Settings row — set this in config.toml |
| `hosts.*.kind` | `""` | `TESSERA_HOSTS__*__KIND` | file-only: no Settings row — set this in config.toml |
| `hosts.*.ssh_alias` | `""` | `TESSERA_HOSTS__*__SSH_ALIAS` | file-only: no Settings row — set this in config.toml |
| `hosts.*.agent_url` | `""` | `TESSERA_HOSTS__*__AGENT_URL` | file-only: no Settings row — set this in config.toml |
| `hosts.*.disk_path` | `""` | `TESSERA_HOSTS__*__DISK_PATH` | file-only: no Settings row — set this in config.toml |
| `hosts.*.timeout` | `3.0` | `TESSERA_HOSTS__*__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `hosts.*.probe_command` | `[]` | `TESSERA_HOSTS__*__PROBE_COMMAND` | file-only: no Settings row — set this in config.toml |
| `hosts.*.ssh_command` | `[]` | `TESSERA_HOSTS__*__SSH_COMMAND` | file-only: no Settings row — set this in config.toml |
| `hosts.*.services` | `{}` | `TESSERA_HOSTS__*__SERVICES` | file-only: no Settings row — set this in config.toml |
| `hosts.*.label` | `""` | `TESSERA_HOSTS__*__LABEL` | file-only: no Settings row — set this in config.toml |
| `hosts.*.prometheus_instance` | `""` | `TESSERA_HOSTS__*__PROMETHEUS_INSTANCE` | file-only: no Settings row — set this in config.toml |
| `hosts.*.prometheus_up_query` | `""` | `TESSERA_HOSTS__*__PROMETHEUS_UP_QUERY` | file-only: no Settings row — set this in config.toml |
| `hosts.*.prometheus_queries` | `{}` | `TESSERA_HOSTS__*__PROMETHEUS_QUERIES` | file-only: no Settings row — set this in config.toml |
| `hosts.*.prometheus_gpu_queries` | `{}` | `TESSERA_HOSTS__*__PROMETHEUS_GPU_QUERIES` | file-only: no Settings row — set this in config.toml |

## `identity`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `identity.enabled` | `false` | `TESSERA_IDENTITY__ENABLED` | Settings row |
| `identity.journal_file` | `"identities.jsonl"` | `TESSERA_IDENTITY__JOURNAL_FILE` | file-only: no Settings row — set this in config.toml |
| `identity.record_operator` | `true` | `TESSERA_IDENTITY__RECORD_OPERATOR` | file-only: no Settings row — set this in config.toml |
| `identity.record_companion` | `true` | `TESSERA_IDENTITY__RECORD_COMPANION` | file-only: no Settings row — set this in config.toml |

## `infrastructure`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `infrastructure.pressure_engines` | `[]` | `TESSERA_INFRASTRUCTURE_PRESSURE_ENGINES` | file-only: no Settings row — set this in config.toml |
| `infrastructure.sparkdash_command` | `[]` | `TESSERA_INFRASTRUCTURE_SPARKDASH_COMMAND` | file-only: no Settings row — set this in config.toml |
| `infrastructure.sparkdash_url` | `""` | `TESSERA_INFRASTRUCTURE_SPARKDASH_URL` | file-only: no Settings row — set this in config.toml |
| `infrastructure.enabled` | `false` | `TESSERA_INFRASTRUCTURE_ENABLED` | file-only: no Settings row — set this in config.toml |
| `infrastructure.snapshot_file` | `"infrastructure.jsonl"` | `TESSERA_INFRASTRUCTURE_SNAPSHOT_FILE` | file-only: no Settings row — set this in config.toml |
| `infrastructure.stale_after` | `300.0` | `TESSERA_INFRASTRUCTURE_STALE_AFTER` | file-only: no Settings row — set this in config.toml |
| `infrastructure.max_hosts` | `100` | `TESSERA_INFRASTRUCTURE_MAX_HOSTS` | file-only: no Settings row — set this in config.toml |
| `infrastructure.max_output_bytes` | `65536` | `TESSERA_INFRASTRUCTURE_MAX_OUTPUT_BYTES` | file-only: no Settings row — set this in config.toml |
| `infrastructure.prometheus_url` | `""` | `TESSERA_INFRASTRUCTURE_PROMETHEUS_URL` | file-only: no Settings row — set this in config.toml |
| `infrastructure.prometheus_query_path` | `"/api/v1/query"` | `TESSERA_INFRASTRUCTURE_PROMETHEUS_QUERY_PATH` | file-only: no Settings row — set this in config.toml |
| `infrastructure.collect_interval` | `0.0` | `TESSERA_INFRASTRUCTURE_COLLECT_INTERVAL` | file-only: no Settings row — set this in config.toml |

## `inspector`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `inspector.enabled` | `false` | `TESSERA_INSPECTOR_ENABLED` | file-only: no Settings row — set this in config.toml |
| `inspector.socket_path` | `"run/inspector.sock"` | `TESSERA_INSPECTOR_SOCKET_PATH` | file-only: no Settings row — set this in config.toml |
| `inspector.source_root` | `""` | `TESSERA_INSPECTOR_SOURCE_ROOT` | file-only: no Settings row — set this in config.toml |
| `inspector.max_frame_bytes` | `8388608` | `TESSERA_INSPECTOR_MAX_FRAME_BYTES` | file-only: no Settings row — set this in config.toml |
| `inspector.allow_groups` | `[]` | `TESSERA_INSPECTOR_ALLOW_GROUPS` | file-only: no Settings row — set this in config.toml |
| `inspector.operator_approval_env` | `""` | `TESSERA_INSPECTOR_OPERATOR_APPROVAL_ENV` | file-only: no Settings row — set this in config.toml |
| `inspector.max_note_bytes` | `8192` | `TESSERA_INSPECTOR_MAX_NOTE_BYTES` | file-only: no Settings row — set this in config.toml |
| `inspector.max_pending_requests` | `8` | `TESSERA_INSPECTOR_MAX_PENDING_REQUESTS` | file-only: no Settings row — set this in config.toml |

## `intake`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `intake.limit` | `20` | `TESSERA_INTAKE__LIMIT` | file-only: no Settings row — set this in config.toml |
| `intake.timeout_seconds` | `60.0` | `TESSERA_INTAKE__TIMEOUT_SECONDS` | file-only: no Settings row — set this in config.toml |

## `jev`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `jev.enabled` | `false` | `TESSERA_JEV_ENABLED` | Settings row |
| `jev.url` | `""` | `TESSERA_JEV_URL` | Settings row |
| `jev.model` | `""` | `TESSERA_JEV_MODEL` | Settings row |
| `jev.api_key_env` | `""` | — (credential selector; configure in TOML) | Settings row |
| `jev.credential_ref` | `""` | `TESSERA_JEV_CREDENTIAL_REF` | Settings row |
| `jev.timeout` | `8.0` | `TESSERA_JEV_TIMEOUT` | Settings row |
| `jev.risk_threshold` | `0.7` | `TESSERA_JEV_RISK_THRESHOLD` | Settings row |
| `jev.gate_threshold` | `0.7` | `TESSERA_JEV_GATE_THRESHOLD` | Settings row |
| `jev.goal_threshold` | `0.8` | `TESSERA_JEV_GOAL_THRESHOLD` | Settings row |
| `jev.loop_window` | `4` | `TESSERA_JEV_LOOP_WINDOW` | Settings row |
| `jev.batch_items` | `64` | `TESSERA_JEV_BATCH_ITEMS` | Settings row |
| `jev.human_gate` | `false` | `TESSERA_JEV_HUMAN_GATE` | Settings row |
| `jev.session_gates_enabled` | `false` | `TESSERA_JEV_SESSION_GATES_ENABLED` | Settings row |
| `jev.cache_size` | `256` | `TESSERA_JEV_CACHE_SIZE` | file-only: no Settings row — set this in config.toml |
| `jev.gated_tools` | `["shell", "write_file", "edit_file"]` | `TESSERA_JEV_GATED_TOOLS` | file-only: no Settings row — set this in config.toml |
| `jev.action_gate_enabled` | `false` | `TESSERA_JEV_ACTION_GATE_ENABLED` | Settings row |
| `jev.router_probes` | `{}` | `TESSERA_JEV_ROUTER_PROBES` | file-only: no Settings row — set this in config.toml |

## `mcp_servers`

Default: `{}`. Entries live under `mcp_servers.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `mcp_servers.*.enabled` | `true` | `TESSERA_MCP_SERVERS__*__ENABLED` | Settings row |
| `mcp_servers.*.description` | `""` | `TESSERA_MCP_SERVERS__*__DESCRIPTION` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.url` | `""` | `TESSERA_MCP_SERVERS__*__URL` | Settings row |
| `mcp_servers.*.command` | `[]` | `TESSERA_MCP_SERVERS__*__COMMAND` | Settings row |
| `mcp_servers.*.env` | `{}` | `TESSERA_MCP_SERVERS__*__ENV` | Settings row |
| `mcp_servers.*.cwd` | `""` | `TESSERA_MCP_SERVERS__*__CWD` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.token_env` | `""` | — (credential selector; configure in TOML) | Settings row |
| `mcp_servers.*.timeout` | `30.0` | `TESSERA_MCP_SERVERS__*__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.list_ttl` | `300.0` | `TESSERA_MCP_SERVERS__*__LIST_TTL` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.include` | `[]` | `TESSERA_MCP_SERVERS__*__INCLUDE` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.exclude` | `[]` | `TESSERA_MCP_SERVERS__*__EXCLUDE` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.safe_tools` | `[]` | `TESSERA_MCP_SERVERS__*__SAFE_TOOLS` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.tool_prefix` | `""` | `TESSERA_MCP_SERVERS__*__TOOL_PREFIX` | file-only: no Settings row — set this in config.toml |
| `mcp_servers.*.session_tools` | `true` | `TESSERA_MCP_SERVERS__*__SESSION_TOOLS` | Settings row |

## `prompt`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `prompt.memory_guidance` | `"AetherMind: your continuity store of experiences, decisions and corrections. It is a manual lookup tool, not injected context; nothing from it reaches you unless you call it. The memory_* tools reach it.\n- Stores: {project} (this harness){personal}. Pass project_id for others.\n- A missing summary tree is normal. When memory_tree is available, it falls back to recall and says so in one line. Do not request a tree build.\n- Before answering about past work, decisions, people, projects, the operator, or any name you do not recognize, call memory_recall with distinctive terms. Answer from the layers and say when nothing matched.\n- When something durable happens (a decision, correction or lock), write memory_note with that type. No log-style busy-work. A repeated failure is not a memory note. record_lesson holds the trigger, the failure and the check when that tool is available."` | `TESSERA_PROMPT_MEMORY_GUIDANCE` | file-only: no Settings row — set this in config.toml |
| `prompt.skill_guidance` | `"Procedures live on the skill shelf; search before house-specific or repeated work; load a skill before acting on a hit; a skill never restricts delegation."` | `TESSERA_PROMPT_SKILL_GUIDANCE` | file-only: no Settings row — set this in config.toml |
| `prompt.turn_reminder` | `"Memory check: if this touches past context, call memory_recall before answering; if something durable happened, write a memory_note."` | `TESSERA_PROMPT_TURN_REMINDER` | file-only: no Settings row — set this in config.toml |
| `prompt.delegation_guidance` | `{"background_task": "background_task runs a long command and wakes you when it ends or stalls.", "board_accept_card": "board_accept_card records that an identity accepts a card as work, with the evidence it was accepted against. When the board requires acceptance, a card cannot move to succeeded until this is recorded. Acceptance is history: reopening the card keeps it.", "board_accept_intake": "board_accept_intake accepts a triaged intake card as work: it becomes claimable, with who accepted and why kept as history.", "board_create_card": "board_* tools track work on the WIP board; wip_board reads it.", "board_gather_intake": "board_gather_intake gathers open pull requests from ONE named repository (owner/name) into intake cards. An intake card is not work: nobody can claim it and it wakes no worker until it is triaged and accepted.", "board_ignore_intake": "board_ignore_intake declines an intake card as work and keeps it on the board as evidence with the reason. Nothing is deleted.", "board_release_card": "board_release_card releases a claim and returns a running card to the open pool; use it when you are handing off rather than finishing.", "board_start_card": "board_start_card is the transition from claimed to running; call it when the work actually begins, with a short receipt, so the board shows in-flight work instead of a card that still reads queued.", "board_triage_intake": "board_triage_intake records what an intake reviewer found -- subject (bug, fix, feature-addition, feature-expansion, other), compatibility with this codebase, and a summary. The acceptance decision reads these.", "board_update_status": "board_update_status is how a card leaves running: move it to succeeded, blocked or queued before your turn ends. A card left running after you stop reads as in-flight work nobody holds.", "lesson_bind": "lesson_bind attaches a shelf skill to an open lesson.", "lesson_check": "lesson_check records whether a lesson passed, or retires a lesson that should stop waking you.", "park_question": "park_question leaves a standing question the harness will wake you with.", "record_lesson": "record_lesson saves a trigger, a failure and a check. The harness wakes you with it later.", "send_to_session": "send_to_session and handoff pass work to other sessions.", "set_timer": "set_timer is this session's own wake, separate from the system floor. It wakes you later so you can keep watching and working.", "spawn_agent": "spawn_agent starts a background agent with this session's tools unless tools names a shorter list. Its report returns to this chat.", "wait_agents": "wait_agents blocks until background agents finish and returns their reports."}` | `TESSERA_PROMPT_DELEGATION_GUIDANCE` | file-only: no Settings row — set this in config.toml |
| `prompt.delegation_intro` | `"Delegation: use your own tools first; delegate work that genuinely benefits from a separate worker. Give children compact goals and point them at files; verify their not-found claims."` | `TESSERA_PROMPT_DELEGATION_INTRO` | file-only: no Settings row — set this in config.toml |
| `prompt.body_map` | `true` | `TESSERA_PROMPT_BODY_MAP` | file-only: no Settings row — set this in config.toml |
| `prompt.architecture` | `""` | `TESSERA_PROMPT_ARCHITECTURE` | file-only: no Settings row — set this in config.toml |

## `remotes`

Default: `{}`. Entries live under `remotes.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `remotes.*.enabled` | `true` | `TESSERA_REMOTES__*__ENABLED` | file-only: no Settings row — set this in config.toml |
| `remotes.*.description` | `""` | `TESSERA_REMOTES__*__DESCRIPTION` | file-only: no Settings row — set this in config.toml |
| `remotes.*.host` | `""` | `TESSERA_REMOTES__*__HOST` | file-only: no Settings row — set this in config.toml |
| `remotes.*.port` | `22` | `TESSERA_REMOTES__*__PORT` | file-only: no Settings row — set this in config.toml |
| `remotes.*.user` | `""` | `TESSERA_REMOTES__*__USER` | file-only: no Settings row — set this in config.toml |
| `remotes.*.identity_file` | `""` | `TESSERA_REMOTES__*__IDENTITY_FILE` | file-only: no Settings row — set this in config.toml |
| `remotes.*.known_hosts_file` | `""` | `TESSERA_REMOTES__*__KNOWN_HOSTS_FILE` | file-only: no Settings row — set this in config.toml |
| `remotes.*.strict_host_key_checking` | `"yes"` | `TESSERA_REMOTES__*__STRICT_HOST_KEY_CHECKING` | file-only: no Settings row — set this in config.toml |
| `remotes.*.ssh_command` | `["ssh"]` | `TESSERA_REMOTES__*__SSH_COMMAND` | file-only: no Settings row — set this in config.toml |
| `remotes.*.connect_timeout` | `10` | `TESSERA_REMOTES__*__CONNECT_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `remotes.*.timeout` | `60.0` | `TESSERA_REMOTES__*__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `remotes.*.rdp_host` | `""` | `TESSERA_REMOTES__*__RDP_HOST` | file-only: no Settings row — set this in config.toml |
| `remotes.*.rdp_port` | `0` | `TESSERA_REMOTES__*__RDP_PORT` | file-only: no Settings row — set this in config.toml |
| `remotes.*.rdp_doc` | `""` | `TESSERA_REMOTES__*__RDP_DOC` | file-only: no Settings row — set this in config.toml |
| `remotes.*.rdp_enabled` | `true` | `TESSERA_REMOTES__*__RDP_ENABLED` | file-only: no Settings row — set this in config.toml |
| `remotes.*.desk` | `false` | `TESSERA_REMOTES__*__DESK` | file-only: no Settings row — set this in config.toml |
| `remotes.*.desk_screenshot_command` | `"f=$(mktemp --suffix=.png) && XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus gnome-screenshot -f \"$f\" && cat \"$f\"; rm -f \"$f\""` | `TESSERA_REMOTES__*__DESK_SCREENSHOT_COMMAND` | file-only: no Settings row — set this in config.toml |
| `remotes.*.desk_input_command` | `"sudo -n python3 -"` | `TESSERA_REMOTES__*__DESK_INPUT_COMMAND` | file-only: no Settings row — set this in config.toml |

## `roles`

Default: `{}`. Maps role names to configured backend names.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `roles.main` | `""` | `TESSERA_ROLES_MAIN` | Settings row |
| `roles.fast` | `""` | `TESSERA_ROLES_FAST` | Settings row |
| `roles.compactor` | `""` | `TESSERA_ROLES_COMPACTOR` | Settings row |
| `roles.failover` | `""` | `TESSERA_ROLES_FAILOVER` | Settings row |
| `roles.*` | `""` | `TESSERA_ROLES_*` | file-only: no Settings row — set this in config.toml |

## `server`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `server.host` | `"127.0.0.1"` | `TESSERA_SERVER_HOST` | file-only: no Settings row — set this in config.toml |
| `server.port` | `0` | `TESSERA_SERVER_PORT` | file-only: no Settings row — set this in config.toml |
| `server.max_hops` | `4` | `TESSERA_SERVER_MAX_HOPS` | file-only: no Settings row — set this in config.toml |
| `server.resume_turns` | `true` | `TESSERA_SERVER_RESUME_TURNS` | file-only: no Settings row — set this in config.toml |
| `server.allowed_hosts` | `[]` | `TESSERA_SERVER_ALLOWED_HOSTS` | file-only: no Settings row — set this in config.toml |
| `server.auth_token` | `""` | `TESSERA_SERVER_AUTH_TOKEN` | file-only: no Settings row — set this in config.toml |
| `server.auth_mode` | `"loopback"` | `TESSERA_SERVER_AUTH_MODE` | file-only: no Settings row — set this in config.toml |
| `server.supervised` | `false` | `TESSERA_SERVER_SUPERVISED` | file-only: no Settings row — set this in config.toml |
| `server.drain_seconds` | `180.0` | `TESSERA_SERVER_DRAIN_SECONDS` | file-only: no Settings row — set this in config.toml |
| `server.interrupt_grace_seconds` | `20.0` | `TESSERA_SERVER_INTERRUPT_GRACE_SECONDS` | file-only: no Settings row — set this in config.toml |
| `server.restart_stall_seconds` | `1800.0` | `TESSERA_SERVER_RESTART_STALL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `server.reply_quote_chars` | `600` | `TESSERA_SERVER_REPLY_QUOTE_CHARS` | file-only: no Settings row — set this in config.toml |
| `server.compress_min_bytes` | `1024` | `TESSERA_SERVER_COMPRESS_MIN_BYTES` | file-only: no Settings row — set this in config.toml |
| `server.event_replay_limit` | `200` | `TESSERA_SERVER_EVENT_REPLAY_LIMIT` | file-only: no Settings row — set this in config.toml |

## `skills`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `skills.enabled` | `true` | `TESSERA_SKILLS_ENABLED` | file-only: no Settings row — set this in config.toml |
| `skills.shelf` | `""` | `TESSERA_SKILLS_SHELF` | file-only: no Settings row — set this in config.toml |
| `skills.trusted_projects` | `[]` | `TESSERA_SKILLS_TRUSTED_PROJECTS` | file-only: no Settings row — set this in config.toml |
| `skills.project_dirs` | `[".tessera/skills", ".agents/skills"]` | `TESSERA_SKILLS_PROJECT_DIRS` | file-only: no Settings row — set this in config.toml |
| `skills.note_bytes` | `4096` | `TESSERA_SKILLS_NOTE_BYTES` | file-only: no Settings row — set this in config.toml |
| `skills.note_description_chars` | `160` | `TESSERA_SKILLS_NOTE_DESCRIPTION_CHARS` | file-only: no Settings row — set this in config.toml |
| `skills.search_limit` | `20` | `TESSERA_SKILLS_SEARCH_LIMIT` | file-only: no Settings row — set this in config.toml |
| `skills.disabled` | `[]` | `TESSERA_SKILLS_DISABLED` | file-only: no Settings row — set this in config.toml |
| `skills.extra_dirs` | `[]` | `TESSERA_SKILLS_EXTRA_DIRS` | file-only: no Settings row — set this in config.toml |

Operator switches (`skills.enabled` off, and names in `skills.disabled`) apply in every approval mode, including Unleashed. Search and load honor them. Unleashed removes approval prompts and other guards the system adds; it does not turn a shelf or a skill back on. Unleashed is the label for config value `yolo`.

## `status`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `status.task_limit` | `100` | `TESSERA_STATUS_TASK_LIMIT` | file-only: no Settings row — set this in config.toml |
| `status.max_file_bytes` | `4194304` | `TESSERA_STATUS_MAX_FILE_BYTES` | file-only: no Settings row — set this in config.toml |
| `status.max_total_bytes` | `8388608` | `TESSERA_STATUS_MAX_TOTAL_BYTES` | file-only: no Settings row — set this in config.toml |
| `status.max_line_bytes` | `262144` | `TESSERA_STATUS_MAX_LINE_BYTES` | file-only: no Settings row — set this in config.toml |
| `status.max_logs` | `1000` | `TESSERA_STATUS_MAX_LOGS` | file-only: no Settings row — set this in config.toml |

## `swarm`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `swarm.enabled` | `false` | `TESSERA_SWARM_ENABLED` | file-only: no Settings row — set this in config.toml |
| `swarm.tasks_file` | `"tasks.jsonl"` | `TESSERA_SWARM_TASKS_FILE` | file-only: no Settings row — set this in config.toml |
| `swarm.jobs_dir` | `"jobs"` | `TESSERA_SWARM_JOBS_DIR` | file-only: no Settings row — set this in config.toml |
| `swarm.max_job_tasks` | `null` | `TESSERA_SWARM_MAX_JOB_TASKS` | Settings row |
| `swarm.max_artifact_bytes` | `65536` | `TESSERA_SWARM_MAX_ARTIFACT_BYTES` | file-only: no Settings row — set this in config.toml |
| `swarm.pressure_hold_enabled` | `false` | `TESSERA_SWARM_PRESSURE_HOLD_ENABLED` | Settings row |
| `swarm.pressure_soft_threshold` | `0.75` | `TESSERA_SWARM_PRESSURE_SOFT_THRESHOLD` | Settings row |
| `swarm.max_attempts` | `3` | `TESSERA_SWARM_MAX_ATTEMPTS` | file-only: no Settings row — set this in config.toml |
| `swarm.distinct_workers` | `null` | `TESSERA_SWARM_DISTINCT_WORKERS` | Settings row |
| `swarm.lease_seconds` | `600.0` | `TESSERA_SWARM_LEASE_SECONDS` | file-only: no Settings row — set this in config.toml |
| `swarm.retry_base` | `1.0` | `TESSERA_SWARM_RETRY_BASE` | file-only: no Settings row — set this in config.toml |
| `swarm.retry_cap` | `60.0` | `TESSERA_SWARM_RETRY_CAP` | file-only: no Settings row — set this in config.toml |
| `swarm.circuit_failures` | `3` | `TESSERA_SWARM_CIRCUIT_FAILURES` | file-only: no Settings row — set this in config.toml |
| `swarm.circuit_cooldown` | `30.0` | `TESSERA_SWARM_CIRCUIT_COOLDOWN` | file-only: no Settings row — set this in config.toml |
| `swarm.gates_enabled` | `false` | `TESSERA_SWARM_GATES_ENABLED` | file-only: no Settings row — set this in config.toml |
| `swarm.aimd_add` | `"ignored"` | `TESSERA_SWARM_AIMD_ADD` | retired: nothing reads it (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.aimd_factor` | `"ignored"` | `TESSERA_SWARM_AIMD_FACTOR` | retired: nothing reads it (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.aimd_successes` | `"ignored"` | `TESSERA_SWARM_AIMD_SUCCESSES` | retired: nothing reads it (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.backend_limits` | `"ignored"` | `TESSERA_SWARM_BACKEND_LIMITS` | retired: not a cap; each model uses max_concurrent_requests (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.default_backend_limit` | `"ignored"` | `TESSERA_SWARM_DEFAULT_BACKEND_LIMIT` | retired: not a cap; each model uses max_concurrent_requests (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.initial_concurrency` | `"ignored"` | `TESSERA_SWARM_INITIAL_CONCURRENCY` | retired: nothing reads it (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.job_workers` | `"ignored"` | `TESSERA_SWARM_JOB_WORKERS` | retired: not a cap; each model uses max_concurrent_requests (accepted for compatibility; ignored); file-only: no Settings row |
| `swarm.max_concurrent` | `"ignored"` | `TESSERA_SWARM_MAX_CONCURRENT` | retired: not a cap; each model uses max_concurrent_requests (accepted for compatibility; ignored); file-only: no Settings row |

`swarm.max_job_tasks` omitted: Safe mode allows 8 workers in one job; Unleashed mode does not add that cap. A saved integer holds in both modes. `swarm.distinct_workers` omitted: Safe mode requires each worker's own backend and account so the evidence is independent. That is a guard, not a rule that a judge must differ from a worker. Unleashed mode drops it. A saved true or false holds in both modes. `job_workers`, `max_concurrent`, `backend_limits`, `default_backend_limit`, `initial_concurrency` and the AIMD settings are retired: a file that still sets them loads, records a migration note, and does not apply them. A model's cap is `max_concurrent_requests`. `pressure_hold_enabled` off is the default; turning it on holds in Safe mode and in Unleashed mode.

## `systems`

Default: `{}`. Entries live under `systems.*`.

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `systems.*.kind` | `""` | `TESSERA_SYSTEMS__*__KIND` | file-only: no Settings row — set this in config.toml |
| `systems.*.label` | `""` | `TESSERA_SYSTEMS__*__LABEL` | file-only: no Settings row — set this in config.toml |
| `systems.*.role` | `""` | `TESSERA_SYSTEMS__*__ROLE` | file-only: no Settings row — set this in config.toml |
| `systems.*.address` | `""` | `TESSERA_SYSTEMS__*__ADDRESS` | file-only: no Settings row — set this in config.toml |
| `systems.*.auth` | `""` | `TESSERA_SYSTEMS__*__AUTH` | file-only: no Settings row — set this in config.toml |
| `systems.*.watch` | `false` | `TESSERA_SYSTEMS__*__WATCH` | file-only: no Settings row — set this in config.toml |
| `systems.*.signals` | `{}` | `TESSERA_SYSTEMS__*__SIGNALS` | file-only: no Settings row — set this in config.toml |
| `systems.*.notes` | `""` | `TESSERA_SYSTEMS__*__NOTES` | file-only: no Settings row — set this in config.toml |

## `tools`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `tools.approval` | `"ask"` | `TESSERA_TOOLS_APPROVAL` | file-only: no Settings row — set this in config.toml |
| `tools.yolo_switch` | `true` | `TESSERA_TOOLS_YOLO_SWITCH` | file-only: no Settings row — set this in config.toml |
| `tools.approval_state_file` | `"state/approval.json"` | `TESSERA_TOOLS_APPROVAL_STATE_FILE` | file-only: no Settings row — set this in config.toml |
| `tools.approval_classes` | `{"acting": ["mac_act", "desk_act", "shell", "write_file", "edit_file", "apply_patch", "move_path", "copy_path", "delete_path", "mkdir", "process", "remote_shell", "remote_write_file", "background_task", "computer_mouse", "computer_scroll", "computer_type", "computer_key", "computer_focus", "computer_click", "computer_perform_secondary_action", "computer_set_value", "computer_select_text", "computer_drag", "computer_press_key", "computer_type_text"], "memory-self": ["clock_heartbeat", "clock_journal_write", "*write_layer", "skill_write"]}` | `TESSERA_TOOLS_APPROVAL_CLASSES` | file-only: no Settings row — set this in config.toml |
| `tools.direct_route_tools` | `[]` | `TESSERA_TOOLS_DIRECT_ROUTE_TOOLS` | retired: every route offers the full registry; this list does not hide tools (accepted for compatibility; ignored); file-only: no Settings row |
| `tools.promote_direct_route` | `true` | `TESSERA_TOOLS_PROMOTE_DIRECT_ROUTE` | file-only: no Settings row — set this in config.toml |
| `tools.approval_rules` | `{"acting": "gate", "memory-self": "allow"}` | `TESSERA_TOOLS_APPROVAL_RULES` | file-only: no Settings row — set this in config.toml |
| `tools.clip_chars` | `30000` | `TESSERA_TOOLS_CLIP_CHARS` | file-only: no Settings row — set this in config.toml |
| `tools.timeout` | `30.0` | `TESSERA_TOOLS_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `tools.list_limit` | `200` | `TESSERA_TOOLS_LIST_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.grep_limit` | `40` | `TESSERA_TOOLS_GREP_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.grep_scan` | `2000` | `TESSERA_TOOLS_GREP_SCAN` | file-only: no Settings row — set this in config.toml |
| `tools.git_log_limit` | `20` | `TESSERA_TOOLS_GIT_LOG_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.spill_dir` | `"tool-output"` | `TESSERA_TOOLS_SPILL_DIR` | file-only: no Settings row — set this in config.toml |
| `tools.output_tail_chars` | `4000` | `TESSERA_TOOLS_OUTPUT_TAIL_CHARS` | file-only: no Settings row — set this in config.toml |
| `tools.read_line_limit` | `400` | `TESSERA_TOOLS_READ_LINE_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.glob_limit` | `200` | `TESSERA_TOOLS_GLOB_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.read_bytes_limit` | `1000000` | `TESSERA_TOOLS_READ_BYTES_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.image_bytes_limit` | `5000000` | `TESSERA_TOOLS_IMAGE_BYTES_LIMIT` | file-only: no Settings row — set this in config.toml |
| `tools.fetch_max_bytes` | `1000000` | `TESSERA_TOOLS_FETCH_MAX_BYTES` | file-only: no Settings row — set this in config.toml |
| `tools.fetch_timeout` | `20.0` | `TESSERA_TOOLS_FETCH_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `tools.shell_max_timeout` | `600.0` | `TESSERA_TOOLS_SHELL_MAX_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `tools.disabled` | `[]` | `TESSERA_TOOLS_DISABLED` | file-only: no Settings row — set this in config.toml |

Names in `tools.disabled` are operator switches. They apply in every approval mode, including Unleashed: the next turn omits the tool and a call is refused. Unleashed removes approval prompts for tools that stay on.

## `ui`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `ui.yolo_banner` | `true` | `TESSERA_UI__YOLO_BANNER` | Settings row |
| `ui.easter_egg_f_it` | `false` | `TESSERA_UI__EASTER_EGG_F_IT` | Settings row |
| `ui.error_banner_seconds` | `600.0` | `TESSERA_UI__ERROR_BANNER_SECONDS` | file-only: no Settings row — set this in config.toml |
| `ui.history_page` | `60` | `TESSERA_UI__HISTORY_PAGE` | file-only: no Settings row — set this in config.toml |
| `ui.view_page_lines` | `80` | `TESSERA_UI__VIEW_PAGE_LINES` | file-only: no Settings row — set this in config.toml |
| `ui.item_body_chars` | `4000` | `TESSERA_UI__ITEM_BODY_CHARS` | file-only: no Settings row — set this in config.toml |
| `ui.item_summary_chars` | `120` | `TESSERA_UI__ITEM_SUMMARY_CHARS` | file-only: no Settings row — set this in config.toml |
| `ui.long_press_ms` | `500` | `TESSERA_UI__LONG_PRESS_MS` | file-only: no Settings row — set this in config.toml |
| `ui.approval_poll_ms` | `1000` | `TESSERA_UI__APPROVAL_POLL_MS` | file-only: no Settings row — set this in config.toml |
| `ui.theme` | `"system"` | `TESSERA_UI__THEME` | Settings row |
| `ui.mark` | `"cairn"` | `TESSERA_UI__MARK` | Settings row; values: cairn (default art with the Tessera-CT wordmark and the tree), gremlin (sticker logo and header crew; shown only while ui.easter_egg_f_it is on) |
| `ui.skin` | `"strata"` | `TESSERA_UI__SKIN` | Settings row |
| `ui.accent_color` | `""` | `TESSERA_UI__ACCENT_COLOR` | Settings row |
| `ui.show_thoughts` | `true` | `TESSERA_UI__SHOW_THOUGHTS` | Settings row |
| `ui.send_shortcut` | `"enter"` | `TESSERA_UI__SEND_SHORTCUT` | Settings row |
| `ui.config_journal_entries` | `40` | `TESSERA_UI__CONFIG_JOURNAL_ENTRIES` | Settings row |

`ui.mark` is `cairn` or `gremlin`, both shown in Settings. `cairn` is the default: the Tessera art with the Tessera-CT wordmark and the header tree. `gremlin` is shown only while `ui.easter_egg_f_it` is on. There is no separate gremlin switch. The logo in the header and `/favicon.ico` is `web/brand/gremlin-logo.svg`. The F-it button keeps `web/brand/f-it-gremlin.svg`. The open page updates its header and icon links. The web app manifest, the macOS dock icon, and the iOS app icon stay the Tessera art. This release does not install alternate native icons. While gremlin is showing, the header trail replaces the tree with the crew. The tree returns when the mark is cairn. The crew reads signals the page already has, and only while the page is connected, the same gate that speeds the tree. Nothing running: one goblin napping. Load adds the open session busy flag, that session's `active_agents`, its `queued` turns, swarm jobs whose status is `running`, and board cards whose status is `running`. One goblin is hauling at load 1, two at 2 or 3, and three at 4 or more. One goblin tumbles when the open session error string is set, the live turn ended with an error, a shown job is `failed` or `blocked`, or a shown board card is `failed` or `blocked`. Up to two others keep hauling when load is above zero. While the page is not connected the crew naps and ignores those counts. Movement follows `prefers-reduced-motion` the same way the tree does. The trail stays hidden below 1020px. The iOS example has no native trail; its embedded page is this header, and a phone-width view already hides it. No separate iOS crew was added. Skins remain `strata`, `basalt`, and `tide`. A way to add a custom skin is a future update and is not part of this release.

## `view`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `view.node_bytes` | `512` | `TESSERA_VIEW_NODE_BYTES` | file-only: no Settings row — set this in config.toml |
| `view.high_bytes` | `128000` | `TESSERA_VIEW_HIGH_BYTES` | file-only: no Settings row — set this in config.toml |
| `view.low_bytes` | `64000` | `TESSERA_VIEW_LOW_BYTES` | file-only: no Settings row — set this in config.toml |
| `view.prompt_bytes` | `8000` | `TESSERA_VIEW_PROMPT_BYTES` | file-only: no Settings row — set this in config.toml |

## `voice`

| Key | Default | Environment override | Availability |
| --- | --- | --- | --- |
| `voice.enabled` | `false` | `TESSERA_VOICE_ENABLED` | Settings row |
| `voice.pipe` | `""` | `TESSERA_VOICE_PIPE` | Settings row |
| `voice.websocket_url` | `""` | `TESSERA_VOICE_WEBSOCKET_URL` | Settings row |
| `voice.model` | `""` | `TESSERA_VOICE_MODEL` | Settings row |
| `voice.api_base_url` | `""` | `TESSERA_VOICE_API_BASE_URL` | file-only: no Settings row — set this in config.toml |
| `voice.client_secret_path` | `""` | `TESSERA_VOICE_CLIENT_SECRET_PATH` | file-only: no Settings row — set this in config.toml |
| `voice.api_key_env` | `""` | — (credential selector; configure in TOML) | file-only: no Settings row — set this in config.toml |
| `voice.credential_ref` | `""` | `TESSERA_VOICE_CREDENTIAL_REF` | Settings row |
| `voice.client_protocol_prefix` | `""` | `TESSERA_VOICE_CLIENT_PROTOCOL_PREFIX` | file-only: no Settings row — set this in config.toml |
| `voice.model_query_param` | `"model"` | `TESSERA_VOICE_MODEL_QUERY_PARAM` | file-only: no Settings row — set this in config.toml |
| `voice.max_session_seconds` | `600` | `TESSERA_VOICE_MAX_SESSION_SECONDS` | Settings row |
| `voice.secret_ttl_seconds` | `60` | `TESSERA_VOICE_SECRET_TTL_SECONDS` | file-only: no Settings row — set this in config.toml |
| `voice.clock_skew_seconds` | `5.0` | `TESSERA_VOICE_CLOCK_SKEW_SECONDS` | file-only: no Settings row — set this in config.toml |
| `voice.timeout` | `5.0` | `TESSERA_VOICE_TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `voice.max_response_bytes` | `65536` | `TESSERA_VOICE_MAX_RESPONSE_BYTES` | file-only: no Settings row — set this in config.toml |
| `voice.max_event_bytes` | `65536` | `TESSERA_VOICE_MAX_EVENT_BYTES` | file-only: no Settings row — set this in config.toml |
| `voice.max_text_bytes` | `16384` | `TESSERA_VOICE_MAX_TEXT_BYTES` | file-only: no Settings row — set this in config.toml |
| `voice.max_events` | `1000` | `TESSERA_VOICE_MAX_EVENTS` | file-only: no Settings row — set this in config.toml |
| `voice.max_sessions` | `4` | `TESSERA_VOICE_MAX_SESSIONS` | file-only: no Settings row — set this in config.toml |
| `voice.idle_reap_seconds` | `150` | `TESSERA_VOICE_IDLE_REAP_SECONDS` | file-only: no Settings row — set this in config.toml |
| `voice.audio_sample_rate` | `24000` | `TESSERA_VOICE_AUDIO_SAMPLE_RATE` | file-only: no Settings row — set this in config.toml |
| `voice.audio_format` | `"audio/pcm"` | `TESSERA_VOICE_AUDIO_FORMAT` | file-only: no Settings row — set this in config.toml |
| `voice.turn_detection` | `{"type": "server_vad"}` | `TESSERA_VOICE_TURN_DETECTION` | file-only: no Settings row — set this in config.toml |
| `voice.session_options` | `{}` | `TESSERA_VOICE_SESSION_OPTIONS` | file-only: no Settings row — set this in config.toml |
| `voice.tools` | `[]` | `TESSERA_VOICE_TOOLS` | file-only: no Settings row — set this in config.toml |
| `voice.tools_mode` | `"configured"` | `TESSERA_VOICE_TOOLS_MODE` | file-only: no Settings row — set this in config.toml |
| `voice.voices` | `[]` | `TESSERA_VOICE_VOICES` | file-only: no Settings row — set this in config.toml |
| `voice.dedicated_slug` | `"voice"` | `TESSERA_VOICE_DEDICATED_SLUG` | file-only: no Settings row — set this in config.toml |
| `voice.dedicated_title` | `"Voice"` | `TESSERA_VOICE_DEDICATED_TITLE` | Settings row |
| `voice.dedicated_reasoning` | `"low"` | `TESSERA_VOICE_DEDICATED_REASONING` | Settings row |
| `voice.dedicated_thinking_budget` | `0` | `TESSERA_VOICE_DEDICATED_THINKING_BUDGET` | Settings row |
| `voice.stt.base_url` | `""` | `TESSERA_VOICE__STT__BASE_URL` | Settings row |
| `voice.stt.model` | `""` | `TESSERA_VOICE__STT__MODEL` | Settings row |
| `voice.stt.models` | `[]` | `TESSERA_VOICE__STT__MODELS` | file-only: no Settings row — set this in config.toml |
| `voice.stt.timeout` | `10.0` | `TESSERA_VOICE__STT__TIMEOUT` | file-only: no Settings row — set this in config.toml |
| `voice.tts.base_url` | `""` | `TESSERA_VOICE__TTS__BASE_URL` | Settings row |
| `voice.tts.model` | `""` | `TESSERA_VOICE__TTS__MODEL` | Settings row |
| `voice.tts.models` | `[]` | `TESSERA_VOICE__TTS__MODELS` | file-only: no Settings row — set this in config.toml |
| `voice.tts.timeout` | `10.0` | `TESSERA_VOICE__TTS__TIMEOUT` | file-only: no Settings row — set this in config.toml |
