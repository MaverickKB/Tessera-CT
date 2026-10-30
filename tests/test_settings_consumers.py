"""Every Settings field is law: flip the value, call the consumer, the output must move.

A grep for the attribute name is not enforcement. This test sets each spec from
``settings_model.catalog`` to two variants and calls the production function that
reads it. A field with no consumer, or a consumer that ignores the value, cannot
pass without being recorded in settings_consumers_baseline.txt. Nothing is
skipped or xfailed. New findings and fixed-but-still-recorded debt fail the gate.

- ``catalog(Config(), {})`` yields the scalar specs.
- ``project`` on a home with named rows yields more fields. The extras are
  list-of-records rows (oauth_accounts, named_accounts, providers,
  credentials, mcp_servers). An empty Config has no named backend rows, so
  its projection is the scalar catalog plus the record rows.

``autonomy.world_view`` is read by ``effective_world_view`` until a
floor-definition file exists, and that text is appended to the floor wake.
It is consumed, not dead.
"""

from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import Mock

import pytest

from debt_baseline import assert_matches_baseline

from tessera.aethermind import AetherMindClient
from tessera.autonomy import Autonomy
from tessera.config import AccountConfig, BackendConfig, Config, McpServerConfig
from tessera.credentials import CredentialResolver
from tessera.cua_jev import key_available
from tessera.jev import JudgeKit
from tessera.mcp_servers import computer_backend, open_server
from tessera.native_cua import NativeHelper, register_native_tools
from tessera.prompts import SYSTEM_PROMPT_TEMPLATE
from tessera.settings_model import catalog, project
from tessera.sessions import SessionManager
from tessera.voice import VoiceSessions, configured as voice_configured, pipes as voice_pipes, selected_pipe as voice_selected_pipe


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No network, no live store, no live home. The swarm-consumer tripwire idiom."""
    tripwires = []
    for owner, name in ((CredentialResolver, 'resolve'), (JudgeKit, '_evaluate_batch'),
                        (socket.socket, 'connect')):
        tripwire = Mock(side_effect=AssertionError('Settings consumers cannot contact live capabilities'))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ('socket.create_connection', 'urllib.request.urlopen',
                   'urllib.request.OpenerDirector.open'):
        tripwire = Mock(side_effect=AssertionError('Settings consumers cannot use network transports'))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    forbidden = Mock(side_effect=AssertionError('Settings consumers cannot spawn a process'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    tripwires.append(forbidden)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


def bare(tmp_path):
    return Config(home=tmp_path, cwd=tmp_path)


def set_path(config, path, value):
    target = config
    for key in path[:-1]:
        target = target[key] if isinstance(target, dict) else getattr(target, key)
    if isinstance(target, dict):
        target[path[-1]] = value
    else:
        setattr(target, path[-1], value)
    return config


def with_chat_backend(config, name='alpha'):
    config.accounts[name] = AccountConfig(name=name, provider_kind='openai_chat')
    config.backends[name] = BackendConfig(
        name=name, account=name, base_url='https://models.example.invalid/' + name,
        model=name + '-model', chat=True, enabled=True)
    return config


def manager_for(config):
    """The slice of SessionManager these readers touch. No session, no disk, no network."""
    manager = Mock()
    manager.config = config
    manager._runtimes = {}
    manager.coordination_status.return_value = []
    return manager


def enum_pair(spec):
    values = list(dict.fromkeys(value for value, _label in spec.options))
    if len(values) >= 2:
        return values[0], values[1]
    return '', 'native'


def variants(spec):
    if spec.id == 'roles.main':
        return 'alpha', 'beta'
    if spec.id == 'roles.failover':
        return '', 'alpha'
    if spec.id in ('roles.fast', 'roles.compactor'):
        return '', 'alpha'
    if spec.id == 'computer_use.provider':
        # '' and 'native' both resolve to native on macOS (mcp_servers.py:64).
        return 'native', 'mcp:desk'
    if spec.id == 'swarm.max_job_tasks':
        return '2', '9'
    if spec.type == 'bool':
        return False, True
    if spec.type == 'enum':
        return enum_pair(spec)
    if spec.type == 'int':
        low = spec.minimum if isinstance(spec.minimum, int) else 1
        return low, low + 1
    if spec.type == 'float':
        low = float(spec.minimum) if spec.minimum is not None else 0.0
        high = low + 1.0
        if spec.maximum is not None and high > float(spec.maximum):
            high = float(spec.maximum)
            low = max(float(spec.minimum or 0), high - 0.5)
        return low, high
    if spec.type == 'color':
        return '#112233', '#445566'
    if spec.type == 'list':
        return [], ['https://memory-a.example.invalid/mcp']
    if spec.id == 'aethermind.url':
        return '', 'https://memory.example.invalid/mcp'
    if spec.id in ('autonomy.self_prompt_gap_seconds', 'autonomy.trial_poll_seconds',
                   'autonomy.min_wake_gap_seconds'):
        return 0.0, 10_000.0
    return 'alpha-value', 'beta-value'


# --- consumers. Each calls a production reader and returns only what that reader emits. ---

def consume_agent_name(config):
    # tessera/prompts.py:3, rendered by tessera/loop.py:197
    return SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=config.agent_name, operator=config.operator_label(), user_instructions='')


def consume_operator_name(config):
    # tessera/config.py:799 operator_label; tessera/sessions.py:1224 reads operator_label_sentence
    return config.operator_label()


def consume_floor_name(config):
    # tessera/config.py:811 floor_label; tessera/autonomy.py:1394 and sessions.py:1022 read it
    return config.floor_label()


def consume_identity_enabled(config):
    # tessera/sessions.py:731: ensure_operator runs only when identity.enabled and a name is set
    return bool(config.identity.enabled and config.operator_name.strip())


def consume_role_main(config):
    # tessera/config.py:1206 backend_for. prepare() has already placed both backends
    # and set roles.main, so this only resolves.
    return config.backend_for('main').name


def consume_role_failover(config):
    # tessera/loop.py:629 reads roles['failover'] and builds that backend on a failed turn
    with_chat_backend(config)
    target = config.roles.get('failover')
    if not target:
        return ''
    return config.backends[target].name


def consume_role_fast(config):
    # tessera/loop.py:99 reads roles['fast'] for direct-routed turns
    with_chat_backend(config)
    target = config.roles.get('fast')
    if not target:
        return ''
    return config.backends[target].name


def consume_role_compactor(config):
    # tessera/loop.py:209 reads roles['compactor'] for background summaries
    with_chat_backend(config)
    target = config.roles.get('compactor')
    if not target:
        return ''
    return config.backends[target].name


def consume_computer_provider(config):
    # tessera/mcp_servers.py:64. provider 'native' and provider '' both return
    # 'native' on macOS. An mcp: name returns 'mcp'. Flip between those two,
    # because the page's own first option does not move this function.
    return computer_backend(config)


def consume_computer_timeout(config):
    # tessera/native_cua.py:39 NativeHelper.timeout
    return NativeHelper(config).timeout


def consume_computer_jev_enabled(config):
    # tessera/native_cua.py:286 registers the Jev tools only when jev.enabled,
    # and only after key_available. Give it a key so the branch is reachable,
    # then see whether find_element was registered.
    config.computer_use.jev.credential_ref = 'present'
    config.computer_use.jev.base_url = 'https://jev.example.com'
    config.computer_use.jev.model = 'configured-judge'
    registry = Mock()
    registry.register = Mock()
    register_native_tools(registry, config)
    names = [call.args[0] for call in registry.register.call_args_list]
    return any(name.endswith('find_element') for name in names)


def consume_computer_jev_credential(config):
    # tessera/cua_jev.py:31 key_available. A credential_ref short-circuits to True;
    # an empty one falls through to the env var, which this fixture does not set.
    jev = config.computer_use.jev
    jev.api_key_env = ''
    return (key_available(jev), jev.credential_ref)


def consume_computer_jev_attr(attr):
    def consumer(config):
        # The block is copied whole onto the router at tessera/loop.py:165.
        # min_confidence is compared at tessera/cua_jev.py:215.
        return getattr(config.computer_use.jev, attr)
    return consumer


def consume_aethermind_source(config):
    # tessera/bundled_engine.py _wants_external. The page's choice is this field.
    memory = config.aethermind
    if memory.source == 'embedded':
        return 'embedded'
    if memory.source == 'external':
        return 'external'
    return 'external' if (memory.url or memory.endpoints) else 'embedded'


def consume_aethermind_client(config):
    # tessera/aethermind.py:136. A configured list becomes the client's endpoint
    # order; a bare url is used only when the list is empty (loop.py:153).
    # McpClient rejects a non-HTTP url, so the empty side is the unresolved
    # current rather than a constructed client.
    memory = config.aethermind
    endpoints = [item for item in (memory.endpoints or []) if item]
    if endpoints:
        return ('endpoints', tuple(endpoints))
    if memory.url and memory.url.startswith(('http://', 'https://')):
        client = AetherMindClient(memory.url)
        return ('url', client._current)
    return ('none', None)


def consume_retired_aethermind_enabled(config):
    # Settings shows the stored key. The loader records it and the turn path does not read it.
    from tessera.settings_model import retired_memory_label
    return retired_memory_label(config)


def consume_aethermind_project(config):
    # tessera/loop.py:161 passes memory.project to Capture
    return config.aethermind.project


def consume_engine_log_cap(config):
    from tessera.bundled_engine import _log_limit
    return _log_limit(config)


def consume_config_journal(config):
    from tessera.config_editor import ConfigEditor
    path = Path(config.home) / 'config.toml'
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text('agent_name = "Tessera"\n', encoding='utf-8')
    journal = path.with_name('config-changes.jsonl')
    journal.write_text(
        '{"at":"t0","source":"baseline","summary":"base","changes":[]}\n'
        '{"at":"t1","source":"config_editor","summary":"one","changes":[]}\n'
        '{"at":"t2","source":"config_editor","summary":"two","changes":[]}\n',
        encoding='utf-8')
    report = ConfigEditor(path).change_report(config.ui.config_journal_entries)
    return report['shown'], report['keep']


def consume_bundled_path(config):
    # tessera/bundled_engine.py:30 _engine_root. Empty means the packaged asset,
    # a set path means that path. The two must not resolve to the same place.
    from tessera.bundled_engine import _engine_root
    root = _engine_root(config)
    return '' if root is None else str(root)


def consume_autonomy_enabled(config):
    # tessera/autonomy.py:564 starts the floor thread only when settings.enabled
    return Autonomy(manager_for(config)).settings.enabled


def consume_infinity_slug(config):
    # tessera/autonomy.py:557 learning.floor_slug = settings.infinity_slug
    return Autonomy(manager_for(config)).settings.infinity_slug


def consume_wake_gap(config):
    # tessera/autonomy.py:980 _cap_reason: a wake inside the gap is refused by name
    service = Autonomy(manager_for(config))
    service.state = {'wakes': [{'at': 1000.0}]}
    return service._cap_reason(1000.0 + 0.5)


def consume_self_prompt_gap(config):
    # tessera/autonomy.py:1371 reads the gap only when autonomy and self-prompt
    # are both enabled. Gap 0 falls through; a gap larger than the elapsed time
    # returns with the stamp unchanged.
    config.autonomy.enabled = True
    config.autonomy.self_prompt_enabled = True
    service = Autonomy(manager_for(config))
    service._intentions_at = 1000.0
    service._refresh_intentions(1000.0 + 0.5)
    return service._intentions_at


def consume_oversight_policy(config):
    # tessera/oversight.py:446. The branch is the consumer; call it with no sessions
    # so it cannot touch a log, and record which branch the policy selected.
    from tessera.oversight import oversight_report
    report = oversight_report(config, [])
    return (config.autonomy.oversight_policy, report.get('policy'))


def consume_trial_poll(config):
    # tessera/sessions.py:1516 compares now against trial_poll_seconds. The test
    # clock is fixed so the comparison is the only thing that moves: inside the
    # interval the stamp stays, past it the stamp becomes now.
    import time as _time
    from tessera.sessions import SessionManager
    host = Mock()
    host.config = config
    host._trials_advanced_at = _time.time()
    host.trials.open_for_all.return_value = []
    host.trials.trials = {}
    SessionManager.advance_trials(host)
    return host._trials_advanced_at


@pytest.mark.parametrize('gap, posted', [(0.0, True), (10.0, False)])
def test_self_prompt_zero_removes_delay_instead_of_disabling(tmp_path, gap, posted):
    config = bare(tmp_path)
    config.autonomy.enabled = True
    config.autonomy.self_prompt_enabled = True
    config.autonomy.self_prompt_gap_seconds = gap
    service = Autonomy(manager_for(config))
    service._intentions_at = 1000.0
    service._refresh_intentions(1000.5)
    assert service._intentions_at == (1000.5 if posted else 1000.0)

    service._suppress_reason = Mock(return_value='')
    service.post = Mock()
    last = {'session': 1000.0}
    service._arm_or_post('session', 1000.5, 'Agenda needs attention', last, gap)
    if posted:
        service.post.assert_called_once_with('session', 'self_prompt', 'Agenda needs attention', rules=[])
    else:
        service.post.assert_not_called()


@pytest.mark.parametrize('policy, has_proposals, has_trial_status', [
    ('findings', False, False), ('approve', True, False), ('auto', False, True),
])
def test_oversight_policy_selects_report_content(tmp_path, monkeypatch, policy,
                                               has_proposals, has_trial_status):
    from tessera.oversight import oversight_report

    config = bare(tmp_path)
    config.autonomy.oversight_policy = policy
    proposal = {'lever': 'reasoning', 'candidate': 'high'}
    monkeypatch.setattr('tessera.oversight._proposals', lambda loops, slow: [proposal])
    report = oversight_report(config, [])
    assert report['proposals'] == ([proposal] if has_proposals else [])
    trial_findings = []
    for finding in report['findings_detail']:
        if finding['kind'] == 'trial':
            trial_findings.append(finding)
    assert bool(trial_findings) == has_trial_status


def consume_world_view(config):
    """Capture the real floor-wake output without starting a session or model."""
    manager = manager_for(config)
    manager.infrastructure = None
    config.board.enabled = False
    service = Autonomy(manager)
    service._floor_slug = lambda: config.autonomy.infinity_slug
    service._arm_or_post = Mock(return_value=False)
    service._queue_floor_wake(0, 0)
    return service._arm_or_post.call_args.args[2]


def consume_jev(config):
    # tessera/jev.py:89 JudgeKit.from_config. This is the constructor the session uses.
    kit = JudgeKit.from_config(config)
    return (kit.enabled, kit.action_gate_enabled, kit.url, kit.model, kit.api_key_env,
            kit.credential_ref, kit.timeout, kit.gate_threshold, kit.goal_threshold,
            kit.risk_threshold, kit.loop_window, kit.batch_items, kit.human_gate)


def consume_session_gates(config):
    # tessera/loop.py:919 and :991. from_config pops the field (jev.py:91), so the
    # gate reads config.jev.session_gates_enabled directly. That is still a read.
    return config.jev.session_gates_enabled


def consume_voice_enabled(config):
    # tessera/voice.py:256 VoiceSessions.status, served at tessera/sessions.py:1811
    host = manager_for(config)
    return VoiceSessions(host, client=Mock()).status()['enabled']


def consume_voice_configured(config):
    # tessera/voice.py:78. The socket, the model and a credential are all required,
    # so the other two are held present and the field under test is the one that moves.
    voice = config.voice
    voice.api_base_url = 'https://voice.example.invalid'
    voice.client_secret_path = 'secret'
    voice.client_protocol_prefix = 'prefix'
    return (voice.websocket_url, voice.model, voice.credential_ref, voice_configured(voice))


def _speech_baseline(config):
    """Hold both services configured so a field under test is the only mover."""
    voice = config.voice
    voice.websocket_url = 'wss://voice.example.invalid/socket'
    voice.model = 'voice-model'
    voice.api_base_url = 'https://voice.example.invalid'
    voice.client_secret_path = '/client-secrets'
    voice.client_protocol_prefix = 'prefix.'
    voice.credential_ref = 'voice-key'
    voice.stt.base_url = 'https://stt.example.invalid/v1'
    voice.stt.model = 'stt-model'
    voice.tts.base_url = 'https://tts.example.invalid/v1'
    voice.tts.model = 'tts-model'
    return voice


def consume_voice_pipe(config):
    # tessera/voice.py selected_pipe: a choice exists only when both pipes are
    # configured, and the saved choice decides which one a start uses.
    voice = _speech_baseline(config)
    return (voice.pipe, voice_selected_pipe(voice))


def consume_stt_endpoint(config):
    # tessera/voice.py pipes: local speech needs both services. The other three
    # pieces are held present and this endpoint is the mover.
    voice = config.voice
    voice.stt.model = 'stt-model'
    voice.tts.base_url = 'https://tts.example.invalid/v1'
    voice.tts.model = 'tts-model'
    return (voice.stt.base_url, voice_pipes(voice))


def consume_stt_model(config):
    voice = config.voice
    voice.stt.base_url = 'https://stt.example.invalid/v1'
    voice.tts.base_url = 'https://tts.example.invalid/v1'
    voice.tts.model = 'tts-model'
    return (voice.stt.model, voice_pipes(voice))


def consume_tts_endpoint(config):
    voice = config.voice
    voice.stt.base_url = 'https://stt.example.invalid/v1'
    voice.stt.model = 'stt-model'
    voice.tts.model = 'tts-model'
    return (voice.tts.base_url, voice_pipes(voice))


def consume_tts_model(config):
    voice = config.voice
    voice.stt.base_url = 'https://stt.example.invalid/v1'
    voice.stt.model = 'stt-model'
    voice.tts.base_url = 'https://tts.example.invalid/v1'
    return (voice.tts.model, voice_pipes(voice))


def consume_voice_lease(config):
    # tessera/voice.py:375 adds max_session_seconds to the deadline;
    # :388 copies session_options, which is where the voice name lives.
    voice = config.voice
    return (voice.max_session_seconds, (voice.session_options or {}).get('voice', ''))


def consume_voice_seed(config):
    # tessera/sessions.py:946, :976, :977. Written once, into the voice session's
    # meta, at creation. A later edit of the setting does not move an existing
    # session — measured, and the reason these three are flagged below.
    voice = config.voice
    return (voice.dedicated_title.strip(), voice.dedicated_reasoning, voice.dedicated_thinking_budget)


def consume_ui(config):
    # tessera/sessions.py:1803. The page renders this block; it does not re-read config.
    ui = config.ui
    return (ui.theme, ui.mark, ui.skin, ui.accent_color, ui.show_thoughts, ui.send_shortcut)


def consume_yolo_banner(config):
    """Read the production status projection used by app.js to show the banner."""
    manager = SessionManager(config)
    try:
        return manager.status()['ui']['yolo_banner']
    finally:
        manager.close()


def consume_easter_egg(config):
    """Read the status flag web/unleashed-label.js uses to paint the button.

    tessera/sessions.py:2063 publishes it; web/unleashed-label.js:76 reads it.
    """
    manager = SessionManager(config)
    try:
        return manager.status()['ui']['easter_egg_f_it']
    finally:
        manager.close()


def consume_permissions(config, approval):
    # tessera/settings_model.py:405. The value comes from the approval dict that
    # tessera/sessions.py:1848 set_yolo writes. The spec has no config path.
    return 'yolo' if approval.get('yolo') else 'safe'


def consume_goblin_avatar(config):
    # tessera/sessions.py publishes it in the status ui block. web/app.js
    # applyMark passes it to web/unleashed-label.js avatarUrl, which builds the
    # F-it mark and the icon href.
    return config.ui.goblin_avatar


def consume_safe_policy(config):
    # tessera/sessions.py:2129. Safe mode is the configured policy; the switch
    # above only chooses whether Safe or Unleashed is running.
    return config.tools.approval


def consume_mcp_env(server):
    # tessera/mcp_servers.py:28-32. env reaches StdioMcpClient. The HTTP branch
    # takes token_env and drops server.env, so an Env refs box on an HTTP row is a lie.
    if server.url:
        return ('http',)
    return ('stdio', open_server(server).env)


def consume_pressure(config):
    from unittest.mock import patch
    from tessera.scheduler import Scheduler
    config.accounts['pressure'] = AccountConfig(name='pressure', provider_kind='openai_chat')
    config.backends['pressure'] = BackendConfig(name='pressure', account='pressure', model='fixture-model')
    config.infrastructure.pressure_engines = [dict(host='engine.invalid', port=8888, model='fixture-model', slots=4)]
    def transport(url, timeout):
        if url.endswith('/health'):
            return '{"max_batch_size":4,"warming":false}'
        return 'tensorfold:requests_running 3\n tensorfold:requests_waiting 0\n'
    with patch('tessera.engine_pressure._get', transport):
        return Scheduler.from_config(config).pressure_allows('pressure', 'pressure')


def consume_job_tasks(config):
    from tessera.swarm_bounds import job_task_limit
    return job_task_limit(config)


def consume_distinct_workers(config):
    from tessera.swarm_bounds import require_distinct_workers
    return require_distinct_workers(config)


def consume_pressure_threshold(config):
    config.swarm.pressure_hold_enabled = True
    return consume_pressure(config)


def consume_codex_catalog(config):
    from tessera.model_discovery import codex_request_url
    return codex_request_url(config)


CONSUMERS = {
    'swarm.pressure_hold_enabled': consume_pressure,
    'swarm.pressure_soft_threshold': consume_pressure_threshold,
    'swarm.max_job_tasks': consume_job_tasks,
    'swarm.distinct_workers': consume_distinct_workers,
    'agent_name': consume_agent_name,
    'operator_name': consume_operator_name,
    'autonomy.floor_name': consume_floor_name,
    'identity.enabled': consume_identity_enabled,
    'roles.main': consume_role_main,
    'roles.fast': consume_role_fast,
    'roles.compactor': consume_role_compactor,
    'roles.failover': consume_role_failover,
    'computer_use.provider': consume_computer_provider,
    'computer_use.timeout': consume_computer_timeout,
    'computer_use.jev.enabled': consume_computer_jev_enabled,
    'computer_use.jev.credential_ref': consume_computer_jev_credential,
    'computer_use.jev.base_url': consume_computer_jev_attr('base_url'),
    'computer_use.jev.model': consume_computer_jev_attr('model'),
    'computer_use.jev.timeout': consume_computer_jev_attr('timeout'),
    'computer_use.jev.min_confidence': consume_computer_jev_attr('min_confidence'),
    'aethermind.source': consume_aethermind_source,
    'aethermind.url': consume_aethermind_client,
    'aethermind.endpoints': consume_aethermind_client,
    'aethermind.project': consume_aethermind_project,
    'aethermind.bundled_path': consume_bundled_path,
    'aethermind.engine_log_max_bytes': consume_engine_log_cap,
    'aethermind.enabled': consume_retired_aethermind_enabled,
    'autonomy.enabled': consume_autonomy_enabled,
    'autonomy.infinity_slug': consume_infinity_slug,
    'autonomy.world_view': consume_world_view,
    'autonomy.min_wake_gap_seconds': consume_wake_gap,
    'autonomy.self_prompt_gap_seconds': consume_self_prompt_gap,
    'autonomy.oversight_policy': consume_oversight_policy,
    'autonomy.trial_poll_seconds': consume_trial_poll,
    'jev.enabled': consume_jev,
    'jev.action_gate_enabled': consume_jev,
    'jev.url': consume_jev,
    'jev.model': consume_jev,
    'jev.api_key_env': consume_jev,
    'jev.credential_ref': consume_jev,
    'jev.timeout': consume_jev,
    'jev.gate_threshold': consume_jev,
    'jev.goal_threshold': consume_jev,
    'jev.risk_threshold': consume_jev,
    'jev.loop_window': consume_jev,
    'jev.batch_items': consume_jev,
    'jev.human_gate': consume_jev,
    'jev.session_gates_enabled': consume_session_gates,
    'voice.enabled': consume_voice_enabled,
    'voice.account': consume_voice_configured,
    'voice.pipe': consume_voice_pipe,
    'voice.stt.base_url': consume_stt_endpoint,
    'voice.stt.model': consume_stt_model,
    'voice.tts.base_url': consume_tts_endpoint,
    'voice.tts.model': consume_tts_model,
    'voice.voice': consume_voice_lease,
    'voice.model': consume_voice_configured,
    'voice.websocket_url': consume_voice_configured,
    'voice.max_session_seconds': consume_voice_lease,
    'voice.dedicated_title': consume_voice_seed,
    'voice.dedicated_reasoning': consume_voice_seed,
    'voice.dedicated_thinking_budget': consume_voice_seed,
    'ui.theme': consume_ui,
    'ui.mark': consume_ui,
    'ui.skin': consume_ui,
    'ui.accent_color': consume_ui,
    'ui.show_thoughts': consume_ui,
    'ui.yolo_banner': consume_yolo_banner,
    'ui.easter_egg_f_it': consume_easter_egg,
    'ui.send_shortcut': consume_ui,
    'ui.config_journal_entries': consume_config_journal,
    'ui.goblin_avatar': consume_goblin_avatar,
    'permissions.mode': consume_permissions,
    'permissions.safe_policy': consume_safe_policy,
    'codex.models_url': consume_codex_catalog,
    'codex.client_version': consume_codex_catalog,
}


def lookup(spec_id):
    if spec_id in CONSUMERS:
        return CONSUMERS[spec_id]
    return None


def prepare(tmp_path, spec, value):
    config = bare(tmp_path)
    if spec.id == 'identity.enabled':
        config.operator_name = 'Ada'
    if spec.id == 'roles.main':
        with_chat_backend(config, 'alpha')
        with_chat_backend(config, 'beta')
        config.roles['main'] = value or 'alpha'
        return config
    if spec.id == 'roles.failover':
        with_chat_backend(config)
        config.roles['failover'] = 'alpha' if value else ''
        return config
    if spec.id in ('roles.fast', 'roles.compactor'):
        with_chat_backend(config, 'alpha')
        with_chat_backend(config, 'beta')
        config.roles[spec.id.split('.', 1)[1]] = value or ''
        return config
    if spec.id == 'computer_use.provider':
        config.computer_use.backend = 'auto'
    if spec.id == 'autonomy.world_view':
        config.autonomy.world_view = value
        return config
    if spec.id == 'aethermind.enabled':
        config.retired_settings = {'aethermind.enabled': value}
        return config
    if spec.path:
        set_path(config, spec.path, value)
    return config


def observe(spec, config, approval):
    if spec.id == 'permissions.mode':
        return consume_permissions(config, approval)
    consumer = lookup(spec.id)
    if consumer is None:
        return None
    return consumer(config)


def test_every_catalog_spec_is_registered(tmp_path):
    missing = [spec.id for spec, _value in catalog(bare(tmp_path), {}) if lookup(spec.id) is None]
    assert not missing, 'no consumer registered: ' + ', '.join(missing)


def test_flip_moves_the_consumer(tmp_path):
    stale = []
    findings = set()
    for spec, _value in catalog(bare(tmp_path), {}):
        left, right = variants(spec)
        if spec.id == 'roles.failover':
            left, right = '', 'alpha'
        if spec.id == 'permissions.mode':
            seen = (observe(spec, bare(tmp_path), {'yolo': False}),
                    observe(spec, bare(tmp_path), {'yolo': True}))
        else:
            seen = (observe(spec, prepare(tmp_path, spec, left), {}),
                    observe(spec, prepare(tmp_path, spec, right), {}))
        if seen[0] == seen[1]:
            stale.append(spec.id + ' -> ' + repr(seen[0]))
            findings.add(spec.id)
    findings.update(http_mcp_env_findings())
    findings.update(provider_option_findings(tmp_path))
    print('\n'.join(stale))
    baseline = Path(__file__).with_name('settings_consumers_baseline.txt')
    if sys.platform != 'darwin':
        # That baseline line is the macOS collapse of Default and native.
        # provider_option_findings records it only where the two options match.
        lines = [line for line in baseline.read_text(encoding='utf-8').splitlines()
                 if not line.startswith('computer_use.provider')]
        baseline = tmp_path / 'settings_consumers_baseline.txt'
        baseline.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert_matches_baseline(findings, baseline)
    # A recorded dead option must not hide a wholly dead catalog field.
    assert not stale, 'NEW: setting did not move its consumer:\n' + '\n'.join(stale)


@pytest.mark.parametrize('view, expected', [
    ('  alpha matters here  ', 'alpha matters here'),
    ('beta matters here', 'beta matters here'),
    ('', ''),
    (' \n\t ', ''),
    ('x' * 2001, 'x' * 1999 + '…'),
])
def test_world_view_is_in_the_floor_wake(tmp_path, view, expected):
    config = bare(tmp_path)
    config.autonomy.world_view = view
    text = consume_world_view(config)
    marker = 'What matters to you, in your own words:\n'
    if expected:
        assert marker + expected + '\n' in text
    else:
        assert marker not in text


def http_mcp_env_findings():
    """Env refs are a field on every MCP row. Flip the value; the client must move.

    tessera/mcp_servers.py:31 builds McpClient(url, token_env=...) and never reads
    server.env. Line 32 passes env to StdioMcpClient. The stdio row passes. The
    HTTP row does not, and that failure is the lie: the box is on the page.
    """
    http_off = McpServerConfig(name='http', url='https://mcp.example.invalid', env={})
    http_on = McpServerConfig(name='http', url='https://mcp.example.invalid', env={'TOKEN': 'secret-ref'})
    stdio_off = McpServerConfig(name='stdio', command=['echo'], env={})
    stdio_on = McpServerConfig(name='stdio', command=['echo'], env={'TOKEN': 'secret-ref'})
    findings = set()
    if consume_mcp_env(stdio_off) == consume_mcp_env(stdio_on):
        findings.add('mcp_servers.*.env.stdio')
    if consume_mcp_env(http_off) == consume_mcp_env(http_on):
        findings.add('mcp_servers.*.env')
    return findings


def provider_option_findings(tmp_path):
    """The Provider dropdown offers '' (Default) and 'native'.

    On macOS both resolve to the native backend. On Linux an empty provider
    selects the Linux tools, so the duplicate is not debt there. The baseline
    records the macOS line with platform=darwin, and this check observes the
    host it is actually running on.
    """
    config = bare(tmp_path)
    config.computer_use.backend = 'auto'
    config.computer_use.provider = ''
    empty = computer_backend(config)
    config.computer_use.provider = 'native'
    named = computer_backend(config)
    if empty == named:
        return {'computer_use.provider'}
    return set()


def test_projection_shape_is_pinned(tmp_path):
    """Catalog specs on an empty Config, plus the nine record rows, is the page."""
    config = bare(tmp_path)
    ids = [field['id'] for section in project(config, {})['sections'] for field in section['fields']]
    records = [field['id'] for section in project(config, {})['sections'] for field in section['fields']
               if field['type'] == 'list-of-records']
    # mcp_servers is the Plugins section, after Hosts and Systems. The ordered
    # AetherMind endpoints are a record list under Integrations, not a text box.
    assert records == ['providers', 'oauth_accounts', 'worker_policy', 'credentials', 'named_accounts',
                       'aethermind_endpoints', 'hosts', 'systems', 'mcp_servers']
    # A stored aethermind.enabled value adds its notice. A bare config has no such row.
    # swarm.max_job_tasks, swarm.distinct_workers, aethermind.engine_log_max_bytes,
    # ui.config_journal_entries, the two Codex catalog settings, and ui.mark
    # are scalars on that bare page.
    assert 'aethermind.enabled' not in ids
    assert len(list(catalog(config, {}))) == 74
    # 74 catalog fields plus the nine record rows, plus update.status.
    assert len(ids) == 84


def test_yolo_banner_settings_save_reaches_status_and_disk(tmp_path):
    from tessera.config import load_config

    (tmp_path / 'config.toml').write_text('[ui]\nyolo_banner = true\n')
    manager = SessionManager(load_config(tmp_path))
    try:
        appearance = next(section for section in manager.settings_view()['sections']
                          if section['id'] == 'appearance')
        field = next(field for field in appearance['fields'] if field['id'] == 'ui.yolo_banner')
        assert field['type'] == 'bool'
        assert field['value'] is True
        for value in (False, True):
            result = manager.apply_settings({'id': field['id'], 'value': value})
            assert result['changed'] is True
            assert result['applied'] == 'live'
            assert result['restart_required'] is False
            assert manager.config.ui.yolo_banner is value
            assert manager.status()['ui']['yolo_banner'] is value
            assert load_config(tmp_path).ui.yolo_banner is value
    finally:
        manager.close()
