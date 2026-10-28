"""The Settings consumer map: which fields have a reader, and where it is.

Every settings field has to actually have function, not just capture text that reads
well and is never used. A page that renders a field nobody reads is a lie, and a wizard that
writes one is worse. This module is the executable inventory the wizard's
doctor step reads, so a field without a consumer is a build-time finding, not a
post-install surprise.

Two evidence classes exist here, and they are kept distinct:

- The catalog scalars are pinned by tests/test_settings_consumers.py, which
  FLIPS each value and asserts a production reader moves. That is behavioral
  proof, stronger than a citation. This map records the citation; the test
  holds the proof.
- Record-row fields (providers, accounts, credentials, mcp_servers, hosts)
  have no flip test at HEAD, so they are classified by named reader: a
  production code site, measured by reading the code, that changes behaviour
  with the value. Writing, validating and rendering the value are excluded.

No field is NO-READER at HEAD. Two are partial: HTTP MCP env is dropped by
tessera/mcp_servers.py:31, and on macOS the Provider dropdown's "Default"
option resolves to the same native backend as "Native". Linux Default selects
the local desktop tools, so that debt is recorded with ``platform=darwin`` in
tests/settings_consumers_baseline.txt. The HTTP env debt is on every platform.

``autonomy.world_view`` is read by ``effective_world_view`` until a
floor-definition file exists, and that text is appended to the floor wake.
"""

from .settings_model import catalog


class Reader:
    """One production read site. A citation, not a proof; the flip test holds that."""

    __slots__ = ('site', 'note')

    def __init__(self, site, note=''):
        self.site = site
        self.note = note

    def as_json(self):
        return {'site': self.site, 'note': self.note} if self.note else {'site': self.site}


# Catalog scalars. Sites are the same production readers tests/test_settings_consumers.py
# flips; the note names the reader function where that adds anything.
CATALOG_READERS = {
    'agent_name': [Reader('tessera/prompts.py:3 (SYSTEM_PROMPT_TEMPLATE), rendered by tessera/loop.py:207')],
    'operator_name': [Reader('tessera/config.py:844 operator_label; tessera/sessions.py:1224 reads operator_label_sentence')],
    'autonomy.floor_name': [Reader('tessera/config.py:863 floor_label; read by tessera/autonomy.py:1529 and tessera/sessions.py:1071')],
    'identity.enabled': [Reader('tessera/sessions.py:766 ensure_operator runs only when enabled; tessera/board.py:280 and :715 resolve owners through the registry')],
    'roles.main': [Reader('tessera/config.py:1278 backend_for resolves the role to a backend')],
    'roles.fast': [Reader('tessera/loop.py:99 uses the fast role for direct-routed turns')],
    'roles.compactor': [Reader('tessera/loop.py:209 uses the compactor role for background summaries')],
    'roles.failover': [Reader('tessera/loop.py:629 builds the failover backend on a failed turn')],
    'computer_use.provider': [Reader('tessera/mcp_servers.py:64 computer_backend; on macOS an empty provider also returns native — partial, baseline debt', 'macOS only: the Default option is a duplicate of Native. The baseline line is platform=darwin')],
    'computer_use.timeout': [Reader('tessera/native_cua.py:39 NativeHelper.timeout')],
    'computer_use.jev.enabled': [Reader('tessera/native_cua.py:286 registers Jev screen tools only when enabled')],
    'computer_use.jev.credential_ref': [Reader('tessera/cua_jev.py:31 key_available short-circuits on a credential_ref')],
    'computer_use.jev.base_url': [Reader('tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165')],
    'computer_use.jev.model': [Reader('tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165')],
    'computer_use.jev.timeout': [Reader('tessera/cua_jev.py:215 block copied onto the router at tessera/loop.py:165')],
    'computer_use.jev.min_confidence': [Reader('tessera/cua_jev.py:215 confidence compared against min_confidence')],
    'aethermind.source': [Reader('tessera/bundled_engine.py _wants_external: embedded stays on the packaged engine; external is used only after it validates')],
    'aethermind.url': [Reader('tessera/bundled_engine.py probe_external validates this URL; loop.py uses it only after that probe succeeds')],
    'aethermind.endpoints': [Reader('tessera/bundled_engine.py probe_external validates this list; loop.py uses it only after that probe succeeds')],
    'aethermind.project': [Reader('tessera/loop.py:161 passes the project to Capture')],
    'aethermind.bundled_path': [Reader('tessera/bundled_engine.py:30 _engine_root resolves the packaged vs configured engine')],
    'aethermind.engine_log_max_bytes': [Reader('tessera/bundled_engine.py _log_limit keeps the engine log to this many bytes; engine_log_text returns that cap')],
    'aethermind.enabled': [Reader('tessera/settings_model.py retired_memory_label shows the stored value; tessera/config.py load_config records it and does not apply it')],
    'autonomy.enabled': [Reader('tessera/autonomy.py:564 starts the floor thread only when enabled')],
    # Local-engine admission hold. Both are read by the scheduler, not by the
    # page. The map needs these swarm keys, or the generator reports the pair
    # as unresolved and the coverage test fails on a field that does have a reader.
    'swarm.pressure_hold_enabled': [Reader('tessera/scheduler.py pressure_allows holds a listed local engine when the switch is on, in Safe mode and in Unleashed')],
    'swarm.pressure_soft_threshold': [Reader('tessera/scheduler.py:138 holds a backend when occupied >= slots_total * threshold')],
    'swarm.max_job_tasks': [Reader('tessera/swarm_bounds.py job_task_limit; tessera/jobs.py refuses a job larger than the effective bound. Unset allows 8 in Safe mode and no cap in Unleashed. A saved integer holds in both modes')],
    'swarm.distinct_workers': [Reader('tessera/swarm_bounds.py require_distinct_workers; tessera/jobs.py requires a distinct backend and account when the guard is on. Unset is on in Safe mode and off in Unleashed. A saved true or false holds in both modes. Overlapping scopes stay refused')],
    'autonomy.infinity_slug': [Reader('tessera/autonomy.py:557 floor_slug keys the floor session and cockpit lane')],
    'autonomy.world_view': [Reader('tessera/floor_definition.py:775 effective_world_view reads config.autonomy.world_view until a definition file is saved', 'Read until a floor-definition file exists. A saved file owns the text.')],
    'autonomy.min_wake_gap_seconds': [Reader('tessera/autonomy.py:980 _cap_reason refuses a wake inside the gap')],
    'autonomy.self_prompt_gap_seconds': [Reader('tessera/autonomy.py:1506 and :1563 gate self-prompt refreshes on the gap')],
    'autonomy.oversight_policy': [Reader('tessera/oversight.py:446 selects report content; tessera/sessions.py:1604 echoes it')],
    'autonomy.trial_poll_seconds': [Reader('tessera/sessions.py:1571 throttle on trial checks')],
    'jev.enabled': [Reader('tessera/jev.py:89 JudgeKit.from_config reads the whole jev block')],
    'jev.action_gate_enabled': [Reader('tessera/jev.py:89 JudgeKit.from_config; gates mutating tools in the session loop')],
    'jev.url': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.model': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.api_key_env': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.credential_ref': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.timeout': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.gate_threshold': [Reader('tessera/jev.py:89 JudgeKit.from_config; bar for session/merge/completion gates')],
    'jev.goal_threshold': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.risk_threshold': [Reader('tessera/jev.py:89 JudgeKit.from_config; session-tool risk enforcement')],
    'jev.loop_window': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.batch_items': [Reader('tessera/jev.py:89 JudgeKit.from_config')],
    'jev.human_gate': [Reader('tessera/jev.py:89 JudgeKit.from_config; tessera/board_tools.py:91 ask_human_gate consults it')],
    'jev.session_gates_enabled': [Reader('tessera/loop.py:928 and :1000 gate session tools on it (jev.py:91 pops it from the from_config dict, so the loop reads config directly)')],
    'voice.enabled': [Reader('tessera/voice.py:187, :244, :280 all branch on settings.enabled')],
    'voice.account': [Reader('tessera/settings_model.py _voice_account writes voice.credential_ref; tessera/voice.py configured() requires it to mint')],
    'voice.pipe': [Reader('tessera/voice.py selected_pipe() chooses the pipe a start uses; VoiceSessions.start refuses the local pipe until its runtime lands')],
    'voice.stt.base_url': [Reader('tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model')],
    'voice.stt.model': [Reader('tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model')],
    'voice.tts.base_url': [Reader('tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model')],
    'voice.tts.model': [Reader('tessera/voice.py pipes() counts local speech configured only when both services have an endpoint and a model')],
    'voice.voice': [Reader('tessera/voice.py:388 copies session_options into the lease; the voice name selects the spoken voice on next start')],
    'voice.model': [Reader('tessera/voice.py:78 configured()')],
    'voice.websocket_url': [Reader('tessera/voice.py:78 configured()')],
    'voice.max_session_seconds': [Reader('tessera/voice.py:375 added to the session deadline')],
    'voice.dedicated_title': [Reader('tessera/sessions.py:973 seeds the dedicated voice session title; :1025 reconciles it on restart', 'write-once at session creation; a later edit does not move an existing session — recorded debt in tests/test_settings_consumers.py')],
    'voice.dedicated_reasoning': [Reader('tessera/sessions.py:980, :1026 seeds/reconciles the dedicated session reasoning', 'same write-once caveat as voice.dedicated_title')],
    'voice.dedicated_thinking_budget': [Reader('tessera/sessions.py:981, :1026 seeds/reconciles the thinking budget; tessera/voice.py:130 validates it', 'same write-once caveat as voice.dedicated_title')],
    'ui.theme': [Reader('tessera/sessions.py:1870 status ui block; web/app.js applies it')],
    'ui.mark': [Reader('tessera/sessions.py:2066 status ui block publishes mark; web/app.js applyMark paints the header and icons. Values: cairn, gremlin.')],
    'ui.skin': [Reader('tessera/sessions.py:1870 status ui block; web/app.js applies it')],
    'ui.accent_color': [Reader('tessera/sessions.py:1870 status ui block; web/app.js:6780 applyAccent applies it')],
    'ui.show_thoughts': [Reader('tessera/sessions.py:1871 status ui block; web/app.js applies it')],
    'ui.yolo_banner': [Reader('tessera/sessions.py:1803 status ui block; web/app.js shows the banner from it')],
    'ui.easter_egg_f_it': [Reader('tessera/sessions.py:2063 status ui block; web/unleashed-label.js:76 paints the approval button from it; web/app.js:6961 paintApprovalLabel applies it')],
    'ui.send_shortcut': [Reader('tessera/sessions.py:1872 status ui block; web/app.js:6782 applyShortcut applies it')],
    'ui.config_journal_entries': [Reader('tessera/config_editor.py change_report keeps and shows this many config changes; tessera/settings_model.py SettingsService.view publishes shown and total')],
    'permissions.mode': [Reader('tessera/settings_model.py:405 reads the approval dict; tessera/sessions.py:1848 set_yolo writes it', 'no config path: the spec writes approval state, not a config field')],
    'codex.models_url': [Reader('tessera/model_discovery.py codex_request_url; discover_codex_models GETs it with the stored sign-in token')],
    'codex.client_version': [Reader('tessera/model_discovery.py codex_request_url appends client_version to that request')],
}

# Record-row fields. Served as list-of-records rows; the reader is per-row production code.
RECORD_READERS = {
    # providers.<name>.* — a provider row is a [backends.<name>] table.
    'providers.*.enabled': [Reader('tessera/config.py:1278 backend_for refuses a disabled backend ("Backend X is disabled"); settings_model.py:186 drops it from chat options')],
    'providers.*.chat': [Reader('tessera/config.py:45 offered_for_chat excludes chat=false backends from every chat role')],
    'providers.*.vendor': [Reader('tessera/cli_worker.py:105 requires a vendor; :200,:211,:223,:229,:333,:360 branch per vendor')],
    'providers.*.command': [Reader('tessera/cli_worker.py:105 requires it; :107 shutil.which resolves it; :268 Popen runs it')],
    'providers.*.cwd': [Reader('tessera/cli_worker.py:105 requires it; :268 Popen cwd argument')],
    'providers.*.args': [Reader('tessera/cli_worker.py:110 argument list passed to the CLI; empty uses the vendor default argv')],
    'providers.*.allow_api_key_fallback': [Reader('tessera/grok_worker.py:30 refuses the API-key fallback unless explicitly true; tessera/settings_ladder.py credential_source names it when on')],
    'providers.*.fallback_env': [Reader('tessera/grok_worker.py:31,:40 injects the fallback credential into that env var for the CLI process')],
    'providers.*.fallback_credential': [Reader('tessera/grok_worker.py:31,:35 resolves the named credential for the fallback')],
    'providers.*.base_url': [Reader('tessera/backend.py:91 stores it; :200,:244 every request URL is built from it')],
    'providers.*.model': [Reader('tessera/backend.py:92; sent on every chat request; cli_worker.py:163 conflicts checked against it')],
    'providers.*.credential': [Reader('tessera/backend.py:228 _credentials.resolve(credential_ref) on each call; tessera/settings_ladder.py credential_source names that reference')],
    # accounts.<name>.* — a worker pool row is an [accounts.<name>] table.
    'accounts.*.provider_kind': [Reader('tessera/config.py:1227 and :1291 route the backend through the account kind; tessera/agents.py:54,:98 branch per kind')],
    'accounts.*.credential_ref': [Reader('tessera/config.py:1292 becomes the backend credential reference; tessera/settings_ladder.py credential_source reads it')],
    'providers.*.max_concurrent_requests': [Reader('tessera/endpoint_limit.py:43 request_limit; tessera/backend.py:189 chat holds one slot and bind_endpoint re-reads a hand-set limit; tessera/jobs.py:264 each swarm worker waits on that slot')],
    'providers.*.throttle_requests_per_minute': [Reader('tessera/endpoint_throttle.py:24 throttle_limits; tessera/backend.py:189 and tessera/jobs.py pace that endpoint in requests per minute. A hand-set number is enforced in Unleashed; there is no system default')],
    'providers.*.throttle_tokens_per_minute': [Reader('tessera/endpoint_throttle.py:24 throttle_limits; tessera/backend.py:189 charges reported usage. A hand-set number is enforced in Unleashed; there is no system default')],
    # credentials.<name>.* — a credential row is a [credentials.<name>] table.
    'credentials.*.keychain_service': [Reader('tessera/credentials.py:110,_keychain runs security/secret-tool with the service')],
    'credentials.*.keychain_account': [Reader('tessera/credentials.py:114,:118 the keychain lookup account argument')],
    'credentials.*.env': [Reader('tessera/credentials.py:98,:100 resolves the value from that environment variable')],
    'credentials.*.secret': [Reader('tessera/settings_model.py:1668 _store_secret writes it to the keychain; credentials.py:143 put is the write path', 'the page writes the keychain; the value is read back through keychain_service/keychain_account')],
    # mcp_servers.<name>.* — a Plugins row. Memory keeps using a server named by
    # aethermind.endpoints (mcp:<name>) when Enabled is off; that read is
    # resolve_endpoint_factory, which ignores enabled.
    'mcp_servers.*.enabled': [Reader('tessera/mcp_servers.py:58,:346 session_plugin_servers offers the server only when enabled; register_mcp_server_tools registers only enabled servers')],
    'mcp_servers.*.url': [Reader('tessera/mcp_servers.py:30,:31 McpClient(url, ...) for an HTTP server')],
    'mcp_servers.*.command': [Reader('tessera/mcp_servers.py:32 StdioMcpClient(command, ...) launches it')],
    'mcp_servers.*.args': [Reader('tessera/mcp_servers.py:32 passed into the stdio server launch')],
    'mcp_servers.*.env': [Reader('tessera/mcp_servers.py:32 env= reaches StdioMcpClient', 'HTTP servers drop env (mcp_servers.py:31 builds McpClient without it) — baseline debt; the hint on the field says so')],
    'mcp_servers.*.token_env': [Reader('tessera/mcp_servers.py:31 open_server passes token_env to McpClient; the token value is read from that variable at call time')],
    'mcp_servers.*.session_tools': [Reader('tessera/mcp_servers.py:58 session_plugin_servers offers the server\'s tools only when session_tools is on')],
    # hosts.<name>.* — a hosts row is a [hosts.<name>] table the floor reads.
    'hosts.*.enabled': [Reader('tessera/infrastructure.py:275 a disabled host is not watched; :366 reported in the row')],
    'hosts.*.label': [Reader('tessera/infrastructure.py:368,:369 the board shows this label')],
    'hosts.*.kind': [Reader('tessera/infrastructure.py:83,:108 the kind selects the probe path (prometheus/local/ssh/agent)')],
    'hosts.*.address': [Reader('tessera/infrastructure.py:75,:111,:288 probe target for local and ssh kinds')],
    'hosts.*.prometheus_instance': [Reader('tessera/infrastructure.py:233 substituted into the PromQL query template')],
    'hosts.*.ssh_alias': [Reader('tessera/infrastructure.py:75,:111,:288 ssh target alias for kind=ssh')],
    'hosts.*.probe_command': [Reader('tessera/infrastructure.py:70,:111,:283,:288 the argv run for kind=local/ssh')],
    'hosts.*.timeout': [Reader('tessera/infrastructure.py:68,:289,:303 probe and request deadline')],
    # identities.* — the People rows, present only when identity.enabled.
    'identities.name': [Reader('tessera/board.py:280,:715 resolve an owner handle; tessera/identities.py:233 rename')],
    'identities.display_name': [Reader('tessera/identities.py:233 rename; settings_model.py:601 renders it')],
    # oauth_accounts — the two fixed OAuth sign-in rows. The row's only action is sign-in/refresh,
    # handled by the OAuth grant code, which is not part of the settings surface.
    'oauth_accounts.sign-in': [Reader('tessera/account_oauth.py grant flow; refresh action in _oauth_actions (settings_model.py:491)', 'record container, not a scalar field; the page only triggers sign-in')],
    # service_accounts — a placeholder collection with no records and no actions at HEAD.
    'service_accounts': [Reader(None, 'no reader: the container serves an empty list with actions=[] and nothing consumes it yet; the hint says so on the page')],
}

# Fields rendered conditionally or not at all for a given install. Not counted as silently dead:
# the page says why (aethermind.url carries status text when endpoints are set; identities appears
# only when identity records are on; providers.*.vendor etc. appear only on cli_worker rows).
CONDITIONAL_NOTES = {
    'aethermind.url': 'inert while aethermind.endpoints is non-empty; the page marks it "Not used" and refuses the save',
    'identities.name': 'served only when identity.enabled is on; off leaves the payload unchanged',
    'identities.display_name': 'served only when identity.enabled is on',
    'oauth_accounts.sign-in': 'always served; actions depend on the live grant state',
    'providers.*.vendor': 'served only on cli_worker provider rows',
    'providers.*.command': 'served only on cli_worker provider rows',
    'providers.*.cwd': 'served only on cli_worker provider rows',
    'providers.*.args': 'served only on cli_worker provider rows',
    'providers.*.allow_api_key_fallback': 'served only on grok cli_worker rows',
    'providers.*.fallback_env': 'served only on grok cli_worker rows',
    'providers.*.base_url': 'set when the endpoint is added; the model row does not edit it',
    'providers.*.model': 'not served on cli_worker rows (the CLI uses its own login)',
    'providers.*.credential': 'the model inherits its provider connection; the row has no credential dropdown',
    'providers.*.fallback_credential': 'edited in config.toml; the model row has no credential dropdown',
}


def _field_entry(field_id, readers, classification, note=''):
    entry = {
        'id': field_id,
        'status': classification,
        'readers': [reader.as_json() for reader in readers],
    }
    if field_id in CONDITIONAL_NOTES:
        entry['rendering'] = CONDITIONAL_NOTES[field_id]
    if note:
        entry['note'] = note
    return entry


def build_map(config, approval):
    """Every field the Settings page serves, with its reader or its absence.

    The catalog scalars are derived from the live ``catalog`` projection, so the
    map cannot drift from the page: a new spec without a CATALOG_READERS entry
    is UNRESOLVED here, and the coverage test fails on it.
    """
    fields = []
    for spec, _value in catalog(config, approval):
        readers = CATALOG_READERS.get(spec.id)
        if readers:
            note = next((r.note for r in readers if r.note), '')
            fields.append(_field_entry(spec.id, readers, 'CONSUMED', note))
        else:
            fields.append(_field_entry(spec.id, [], 'UNRESOLVED',
                                      'no reader registered in settings_consumers.CATALOG_READERS; add one or remove the spec'))
    for field_id, readers in sorted(RECORD_READERS.items()):
        note = next((r.note for r in readers if r.note), '')
        if all(r.site is None for r in readers):
            fields.append(_field_entry(field_id, readers, 'NO-READER', note))
        else:
            fields.append(_field_entry(field_id, readers, 'CONSUMED', note))
    return {
        'source': 'tessera/settings_model.py (catalog + record-row specs)',
        'proof': ('catalog scalars are flip-tested in tests/test_settings_consumers.py; '
                  'record rows are classified by named reader site'),
        'counts': {},
        'fields': fields,
    }


def annotate_counts(mapping):
    counts = {}
    for entry in mapping['fields']:
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    mapping['counts'] = counts
    return mapping


def unread_set_fields(mapping):
    """The doctor's question: which SET fields does nothing read?

    A NO-READER entry is only actionable against a config that actually sets
    it, so the doctor reports the intersection. permissions.mode and other
    no-config-path specs never intersect and are skipped by construction.
    """
    return [entry['id'] for entry in mapping['fields']
            if entry['status'] == 'NO-READER']
