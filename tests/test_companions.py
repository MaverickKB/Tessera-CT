"""Atlas and Ember companions against a fake loopback MCP. No live companion or model call."""

import json
from pathlib import Path
import socket
import subprocess

import pytest

from fake_backend import FakeBackend
from fake_companion_mcp import fake_companion
from tessera.config import Config, load_config
from tessera.loop import Session

ROOT = Path(__file__).resolve().parents[1]


def config_for(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    return config


def atlas_on(config, url, **extra):
    atlas = config.companions.atlas
    atlas.enabled, atlas.endpoints, atlas.brief_tool = True, [url], 'brief'
    atlas.timeout, atlas.probe_timeout = 0.5, 0.5
    for key, value in extra.items():
        setattr(atlas, key, value)
    return config


def turn_state(call):
    return next(m['content'] for m in call['messages'] if str(m.get('content', '')).startswith('<turn-state>'))


def run_one(config, reply='done'):
    backend = FakeBackend([reply])
    with Session(config, backend=backend) as session:
        assert session.run_turn('Plan the work') == reply
        return backend, session


def test_defaults_are_off_and_disabled_companions_open_no_socket(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    atlas, ember = config.companions.atlas, config.companions.ember
    assert not atlas.enabled and not ember.enabled and atlas.endpoints == [] and ember.helmet_endpoints == []
    assert not atlas.allow_index_refresh and not ember.allow_transition and atlas.token_env == ''
    assert config.inspector.enabled is False and config.inspector.socket_path == 'run/inspector.sock'
    assert config.identity_file == ''
    def refuse(*args, **kwargs):
        raise AssertionError('Disabled companions must not open a network connection')
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    config = config_for(tmp_path)
    config.companions.atlas.endpoints = ['http://127.0.0.1:9/mcp']  # configured but disabled: still inert
    def factory(*args, **kwargs):
        raise AssertionError('No MCP client may be built while companions are off')
    backend = FakeBackend(['done'])
    with Session(config, backend=backend, companion_client_factory=factory) as session:
        assert session.run_turn('hello') == 'done'
        assert not any(name.startswith(('atlas_', 'ember_')) for name in
                       [s['function']['name'] for s in session.tools.schemas()])
        assert session.status()['companions']['atlas'] == {'enabled': False, 'configured': False, 'reachable': None, 'error': None}


def test_enabled_timeout_soft_fails_into_turn_state_with_identical_system_prompt(tmp_path):
    baseline, _ = run_one(config_for(tmp_path / 'a'))
    with fake_companion(delay=2.0) as fake:
        backend, session = run_one(atlas_on(config_for(tmp_path / 'b'), fake.url))
    messages = backend.calls[0]['messages']
    assert messages[0]['content'].encode() == baseline.calls[0]['messages'][0]['content'].encode()
    assert 'Companion notice: Orientation unavailable (McpError)' in turn_state(backend.calls[0])
    assert 'Orientation unavailable' not in messages[0]['content']
    status = session.companions.status()['atlas']
    assert status['enabled'] and status['configured'] and status['reachable'] is False and status['error'] == 'McpError'
    assert fake.url not in json.dumps(session.companions.status())


def test_atlas_brief_is_an_orientation_note_in_turn_state_not_the_prompt(tmp_path):
    with fake_companion({'brief': 'MAP: tessera/loop.py owns the turn loop'}) as fake:
        backend, session = run_one(atlas_on(config_for(tmp_path), fake.url))
        assert fake.calls == [('brief', {'task': 'chat'})]
    messages = backend.calls[0]['messages']
    state = turn_state(backend.calls[0])
    assert 'MAP: tessera/loop.py' in state and 'not evidence' in state
    assert 'MAP:' not in messages[0]['content']
    notes = [m for m in session.log.iter() if m.kind == 'note' and m.meta.get('companion') == 'atlas']
    assert notes and notes[0].meta['label'] == 'orientation'


def test_background_start_brief_never_blocks_and_joins_a_later_turn(tmp_path):
    import time
    with fake_companion({'brief': 'MAP: slow atlas'}, delay=0.3) as fake:
        config = atlas_on(config_for(tmp_path), fake.url, start_brief_background=True)
        config.companions.atlas.timeout = 5
        backend = FakeBackend(['one', 'two'])
        with Session(config, backend=backend) as session:
            assert session.run_turn('first') == 'one'
            assert 'MAP: slow atlas' not in backend.calls[0]['messages'][2]['content']
            for _ in range(50):
                if any('MAP: slow atlas' in c for c in session._session_context):
                    break
                time.sleep(0.1)
            assert session.run_turn('second') == 'two'
            assert 'MAP: slow atlas' in json.dumps(backend.calls[1]['messages'])


def test_refresh_is_never_called_unless_allowed_and_reads_honor_prefixes(tmp_path):
    with fake_companion() as fake:
        config = atlas_on(config_for(tmp_path), fake.url, brief_tool='read_index', refresh_tool='refresh_index',
                          contracts_tool='contracts', read_tool='read', read_uri_prefixes=['atlas://tessera/'],
                          max_briefs_per_session=2)
        backend = FakeBackend(['ok'])
        with Session(config, backend=backend) as session:
            names = [s['function']['name'] for s in session.tools.schemas()]
            assert 'atlas_refresh' not in names
            assert session.tools.execute('atlas_brief', {'task': 'x' * 10000})['evidence'] is False
            assert session.tools.execute('atlas_brief', {'task': 'again'}) == {'error': 'Orientation brief limit reached for this session'}
            assert session.tools.execute('atlas_contracts', {})['atlas'] == 'ok:contracts'
            assert session.tools.execute('atlas_read', {'uri': 'file:///etc/passwd'}) == {'error': 'That URI is not allowlisted'}
            assert session.tools.execute('atlas_read', {'uri': 'atlas://tessera/loop'})['atlas'] == 'ok:read'
            assert all(len(args.get('task', '').encode()) <= config.companions.atlas.max_brief_bytes for _, args in fake.calls)
        assert 'refresh_index' not in [name for name, _ in fake.calls]
        assert [name for name, _ in fake.calls].count('read_index') == 2
        config.companions.atlas.read_uri_prefixes = []
        with Session(config, backend=FakeBackend([])) as session:
            assert 'atlas_read' not in [s['function']['name'] for s in session.tools.schemas()], 'no prefixes: no read tool'
        config.companions.atlas.allow_index_refresh = True
        with Session(config, backend=FakeBackend([])) as session:
            assert 'atlas_refresh' in [s['function']['name'] for s in session.tools.schemas()]


def test_companions_never_register_pre_tool_handlers(tmp_path):
    with fake_companion() as atlas, fake_companion() as ember:
        config = atlas_on(config_for(tmp_path / 'on'), atlas.url)
        e = config.companions.ember
        e.enabled, e.helmet_endpoints, e.companion_endpoints = True, [ember.url], [ember.url]
        e.profile, e.host_label, e.surface, e.instance_id = 'p', 'h', 's', 'i'
        with Session(config, backend=FakeBackend([])) as on, Session(config_for(tmp_path / 'off'), backend=FakeBackend([])) as off:
            assert len(on.hooks._handlers.get('pre_tool', [])) == len(off.hooks._handlers.get('pre_tool', []))
            assert len(on.hooks._handlers.get('session_start', [])) == len(off.hooks._handlers.get('session_start', [])) + 2
            for handler in on.hooks._handlers.get('pre_tool', []):
                assert 'companions' not in getattr(handler, '__module__', '')


def test_ember_records_are_explicit_identity_gated_and_never_executed(tmp_path):
    with fake_companion({'get_compact_cortex_briefing': 'cortex healthy', 'companion_propose': 'noted'}) as fake:
        config = config_for(tmp_path)
        e = config.companions.ember
        e.enabled, e.helmet_endpoints, e.companion_endpoints, e.timeout = True, [fake.url], [fake.url], 1
        e.profile, e.host_label, e.surface, e.decision_tool = 'operator-profile', 'operator-host', 'tessera', 'record_decision'
        backend = FakeBackend(['done'])
        with Session(config, backend=backend) as session:
            names = [name for name, _ in fake.calls]
            assert names == ['report_session_start', 'get_compact_cortex_briefing', 'companion_account']
            assert 'companion_encounter' not in names, 'no instance id: encounter is skipped, never invented'
            # Argument names match the remote tools/list.
            assert fake.calls[0][1] == {'profile': 'operator-profile', 'host': 'operator-host', 'model': fake.calls[0][1]['model'], 'intention': 'chat'}
            assert fake.calls[1][1] == {'session_id': 'chat', 'profile': 'operator-profile'}
            assert session.run_turn('go') == 'done'
            state = turn_state(backend.calls[0])
            assert 'cortex healthy' in state and 'does not change tool approval' in state
            assert 'cortex' not in backend.calls[0]['messages'][0]['content']
            reply = session.tools.execute('ember_propose', {'title': 't', 'noticed': 'n', 'rationale': 'r', 'wants_to': 'rm -rf /'})
            assert reply == {'note': 'noted', 'executed': False}
            assert session.tools.execute('ember_decision', {'text': 'Ship it'}) == {'recorded': True}
            with pytest.raises(PermissionError):
                session.companions.member('ember').transition({'intention': 'x'})
            assert 'turn_end' not in json.dumps(fake.calls)
        e.profile = ''
        fake.calls.clear()
        with Session(config, backend=FakeBackend([])):
            assert 'report_session_start' not in [name for name, _ in fake.calls]


def test_identity_file_is_an_operator_path_and_empty_by_default(tmp_path):
    with Session(config_for(tmp_path / 'none'), backend=FakeBackend([])) as session:
        assert 'SOUL' not in session.system_prompt
    config = config_for(tmp_path / 'set')
    (tmp_path / 'set').mkdir(exist_ok=True)
    (tmp_path / 'set' / 'soul.md').write_text('SOUL: operator identity text')
    config.identity_file = 'soul.md'
    with Session(config, backend=FakeBackend([])) as session:
        assert 'SOUL: operator identity text' in session.system_prompt
    config.identity_file = 'missing.md'
    with pytest.raises(FileNotFoundError):
        Session(config, backend=FakeBackend([]))


def test_companion_config_validation_rejects_credentials_in_urls(tmp_path):
    (tmp_path / 'config.toml').write_text('[companions.atlas]\nendpoints = ["https://user:pw@example.com/mcp"]\n')
    with pytest.raises(ValueError):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text('[companions.node-a]\nenabled = true\n')
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_companion_source_names_no_host():
    for name in ('companions.py', 'inspector.py', 'inspector_client.py'):
        text = (ROOT / 'tessera' / name).read_text()
        assert 'http://' not in text and 'https://' not in text and '.aem' not in text, name
    companions = (ROOT / 'tessera' / 'companions.py').read_text()
    assert 'import' not in companions.split('aethermind')[0][-0:] or 'from .aethermind' not in companions
    assert 'from .aethermind' not in companions and 'import aethermind' not in companions
    result = subprocess.run(['bash', 'scripts/check_no_hardwires.sh'], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_identity_file_accepts_ordered_list_with_combined_bound(tmp_path):
    root = tmp_path / 'list'
    root.mkdir()
    (root / 'SOUL.md').write_text('SOUL: who I am')
    (root / 'CORTEX.md').write_text('CORTEX: continuity contract')
    config = config_for(root)
    config.identity_file = ['SOUL.md', str(root / 'CORTEX.md')]
    with Session(config, backend=FakeBackend([])) as session:
        prompt = session.system_prompt
        assert 'SOUL: who I am' in prompt and 'CORTEX: continuity contract' in prompt
        assert prompt.index('SOUL: who I am') < prompt.index('CORTEX: continuity contract')
    config.identity_max_bytes = len('SOUL: who I am') + 3
    with pytest.raises(ValueError, match='identity_max_bytes'):
        Session(config, backend=FakeBackend([]))


def test_identity_file_list_loads_from_toml_and_rejects_bad_entries(tmp_path):
    (tmp_path / 'config.toml').write_text('identity_file = ["a.md", "b.md"]\n')
    assert load_config(tmp_path).identity_files() == ['a.md', 'b.md']
    (tmp_path / 'config.toml').write_text('identity_file = "a.md"\n')
    assert load_config(tmp_path).identity_files() == ['a.md']
    for bad in ('identity_file = ["a.md", 3]\n', 'identity_file = ["a.md", ""]\n', 'identity_file = 3\n'):
        (tmp_path / 'config.toml').write_text(bad)
        with pytest.raises(ValueError, match='identity_file'):
            load_config(tmp_path)


def test_resumed_session_is_not_briefed_again(tmp_path):
    with fake_companion({'brief': 'MAP: once'}) as fake:
        config = atlas_on(config_for(tmp_path), fake.url)
        with Session(config, backend=FakeBackend(['one']), root=tmp_path / 's', session_id='x') as session:
            session.run_turn('first')
        with Session(config, backend=FakeBackend(['two']), root=tmp_path / 's', session_id='x') as session:
            session.run_turn('second')
            notes = [m for m in session.log.iter() if m.kind == 'note' and m.meta.get('companion') == 'atlas']
        assert len(notes) == 1 and [call[0] for call in fake.calls] == ['brief']
