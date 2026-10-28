"""A real session reaches the bundled engine through the constructor.

Discovery finds the engine root and enables memory without a typed
bundled_path. These tests drive Session construction. They do not spawn an
engine. `ensure_bundled_engine` is replaced with a recorder, so the test
measures whether the session asks for the engine.
"""
import tomllib

import pytest

from fake_backend import FakeBackend
from tessera import bundled_engine
from tessera.config import Config, load_config
from tessera.loop import Session


def config_with_bundle(tmp_path, *, bundled_path='', endpoints=(), url=''):
    """A config shaped like a fresh install: memory on, nothing configured."""
    config = Config(home=tmp_path, cwd=tmp_path)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.aethermind.bundled_path = bundled_path
    config.aethermind.endpoints = list(endpoints)
    config.aethermind.url = url
    return config


@pytest.fixture
def recorder(monkeypatch):
    """Record every ensure_bundled_engine call and answer with a stand-in URL."""
    calls = []

    def fake(config, clock=None, *, force=False):
        calls.append(config.aethermind.bundled_path)
        return 'http://127.0.0.1:59999/mcp'

    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', fake)
    return calls


def test_a_config_that_omits_enabled_still_runs_aethermind(tmp_path, monkeypatch):
    """Omitting the key is not an off switch, with or without a build receipt."""
    calls = []

    def fake(config, clock=None, *, force=False):
        calls.append(force)
        return 'http://127.0.0.1:59999/mcp'

    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', fake)
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    (tmp_path / 'config.toml').write_text('[aethermind]\nproject = "work"\n')
    config = load_config(tmp_path)
    assert 'enabled' not in tomllib.loads((tmp_path / 'config.toml').read_text())['aethermind']
    assert not hasattr(config.aethermind, 'enabled')
    assert 'aethermind.enabled' not in config.retired_settings
    session = Session(config, backend=FakeBackend([]))
    try:
        assert calls == [True]
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['url'] == 'http://127.0.0.1:59999/mcp'
        assert session.capture.client is not None
    finally:
        session.close()
    bare = tmp_path / 'bare'
    bare.mkdir()
    (bare / 'config.toml').write_text('')
    bare_config = load_config(bare)
    assert not hasattr(bare_config.aethermind, 'enabled')
    assert 'aethermind.enabled' not in bare_config.retired_settings


@pytest.mark.aethermind_unavailable
def test_a_missing_tree_reports_that_the_packaged_engine_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    monkeypatch.setattr('tessera.engine_manifest.vendored_status', lambda root=None: 'absent')
    config = config_with_bundle(tmp_path)
    session = Session(config, backend=FakeBackend([]))
    try:
        assert session.capture.client is None
        assert session.memory_choice['url'] == ''
        assert session.memory_choice['reason'] == 'Packaged engine is not in this install'
    finally:
        session.close()


def test_a_session_with_nothing_configured_asks_the_launcher_for_the_engine(tmp_path, recorder):
    """The bug: bundled_path is empty on a fresh install, and it gated the launch.

    `ensure_bundled_engine` already re-checks endpoints, url and the engine
    root. A stored enabled flag is not a gate. Requiring `bundled_path` in
    loop.py as well skipped exactly the install the bundle exists for.
    """
    config = config_with_bundle(tmp_path)
    assert config.aethermind.bundled_path == '', 'precondition: a fresh install sets no bundled_path'

    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == [''], 'the session never asked the launcher for the bundled engine'
        assert session.capture.client is not None, 'the session was built with no memory client'
    finally:
        session.close()


def test_a_validated_endpoint_wins_and_the_bundle_stays_quiet(tmp_path, recorder, monkeypatch):
    """A configured service replaces the packaged engine only after it validates."""
    monkeypatch.setattr(bundled_engine, 'probe_external', lambda config: None)
    config = config_with_bundle(tmp_path, endpoints=['http://127.0.0.1:8452/mcp'])
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == [], 'the launcher was called even though the endpoint validated'
        assert session.memory_choice['source'] == 'external'
        assert session.capture.client is not None
    finally:
        session.close()


def test_a_validated_url_wins_and_the_bundle_stays_quiet(tmp_path, recorder, monkeypatch):
    monkeypatch.setattr(bundled_engine, 'probe_external', lambda config: None)
    config = config_with_bundle(tmp_path, url='http://127.0.0.1:8452/mcp')
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == []
        assert session.memory_choice['source'] == 'external'
        assert session.memory_choice['reason'] == 'External service validated'
        assert session.capture.client is not None
    finally:
        session.close()


def test_an_invalid_external_service_keeps_the_embedded_engine(tmp_path, recorder, monkeypatch):
    from tessera.mcp_client import McpError

    def reject(config):
        raise McpError('MCP HTTP 401')

    monkeypatch.setattr(bundled_engine, 'probe_external', reject)
    config = config_with_bundle(tmp_path, url='https://memory.example.invalid/mcp')
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == [''], 'an invalid external service left the packaged engine off'
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['external_error_name'] == 'McpError'
        assert '401' in session.memory_choice['external_error']
        assert session.capture.client is not None
        text = session._turn_state('main', [])
        assert 'AetherMind external service was not used (McpError: MCP HTTP 401).' in text
        assert 'The embedded engine is active.' in text
    finally:
        session.close()


def test_a_legacy_disabled_flag_still_asks_for_the_embedded_engine(tmp_path, recorder):
    config = config_with_bundle(tmp_path)
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == ['']
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['external_error'] is None
        assert session.capture.client is not None
    finally:
        session.close()


def test_a_stored_false_flag_still_runs_the_embedded_engine_and_settings_says_so(tmp_path, recorder, monkeypatch):
    from tessera.sessions import SessionManager

    monkeypatch.setenv('TESSERA_AETHERMIND_ENABLED', 'false')
    config = load_config(tmp_path)
    assert config.retired_settings['aethermind.enabled'] is False
    assert not hasattr(config.aethermind, 'enabled')
    config.tools.approval = 'never'
    with SessionManager(config, backend_factory=lambda *args, **kwargs: FakeBackend([])) as manager:
        manager.create('one')
        session = manager.runtime('one').session
        assert recorder == ['']
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['reason'] == 'No external service configured'
        assert session.capture.client is not None
        report = manager.status()['aethermind']
        assert 'enabled' not in report
        assert report['active'] == 'embedded'
        assert report['process'] == 'http://127.0.0.1:59999/mcp'
        assert report['reason'] == 'No external service configured'
        fields = {field['id']: field for section in manager.settings_view()['sections']
                  if section['id'] == 'integrations' for field in section['fields']}
        assert fields['aethermind.enabled']['inert'] is True
        assert fields['aethermind.enabled']['value'] == 'ignored (False)'
        status = fields['aethermind.source']['status']
        assert status.startswith('Embedded · No external service configured')
        assert 'Off' not in status


@pytest.mark.aethermind_unavailable
def test_no_engine_anywhere_leaves_the_session_without_a_client(tmp_path, monkeypatch):
    """The other half of the control: a real absence must not invent a client.

    An engine-less wheel reports no engine rather than a broken one. Here nothing
    is monkeypatched, so the real
    launcher runs against a checkout with no packaged engine at the path this
    home would resolve.
    """
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda config: None)
    config = config_with_bundle(tmp_path)
    session = Session(config, backend=FakeBackend([]))
    try:
        assert session.capture.client is None, 'a client appeared with no engine to talk to'
        assert session.memory_choice['url'] == ''
        assert session.memory_choice['reason'].startswith('bytes present, not started')
        assert 'build receipt' in session.memory_choice['reason']
    finally:
        session.close()


def _settings_status(tmp_path, **bundle):
    from tessera.sessions import SessionManager

    config = config_with_bundle(tmp_path, **bundle)
    with SessionManager(config, backend_factory=lambda *args, **kwargs: FakeBackend([])) as manager:
        manager.create('one')
        manager.runtime('one')
        field = next(field for section in manager.settings_view()['sections']
                     if section['id'] == 'integrations'
                     for field in section['fields'] if field['id'] == 'aethermind.url')
        return field['status'], manager.status()['aethermind']


def test_settings_names_the_embedded_engine_when_nothing_external_is_set(tmp_path, recorder):
    status, report = _settings_status(tmp_path)
    assert recorder == ['']
    assert report['active'] == 'embedded'
    assert report['reason'] == 'No external service configured'
    assert status.startswith('Embedded · No external service configured')


def test_settings_names_an_external_service_after_it_validates(tmp_path, recorder, monkeypatch):
    monkeypatch.setattr(bundled_engine, 'probe_external', lambda config: None)
    status, report = _settings_status(tmp_path, url='http://127.0.0.1:8452/mcp')
    assert recorder == []
    assert report['active'] == 'external'
    assert status.startswith('External')
    assert 'External service validated' in status or 'Unreachable' in status


def test_settings_names_the_embedded_engine_when_the_external_service_fails(tmp_path, recorder, monkeypatch):
    from tessera.mcp_client import McpError

    def reject(config):
        raise McpError('MCP HTTP 401')

    monkeypatch.setattr(bundled_engine, 'probe_external', reject)
    status, report = _settings_status(tmp_path, url='https://memory.example.invalid/mcp')
    assert recorder == ['']
    assert report['active'] == 'embedded'
    assert report['external_error'] == 'McpError: MCP HTTP 401'
    assert 'Embedded' in status
    assert 'did not validate' in status
    assert 'Last error: McpError' in status
    assert report['error_category'] in status
    assert 'https://memory.example.invalid/mcp' in status


def test_an_explicit_embedded_choice_ignores_a_saved_url(tmp_path, recorder, monkeypatch):
    def refuse(config):
        pytest.fail('an embedded choice must not probe the external service')

    monkeypatch.setattr(bundled_engine, 'probe_external', refuse)
    config = config_with_bundle(tmp_path, url='https://memory.example.invalid/mcp')
    config.aethermind.source = 'embedded'
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == ['']
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['external_error'] is None
        assert session.memory_choice['reason'] == 'Embedded engine selected'
        assert session.capture.client is not None
    finally:
        session.close()


def test_an_external_choice_without_a_service_falls_back(tmp_path, recorder, monkeypatch):
    def refuse(config):
        pytest.fail('a missing external service must not open a socket')

    monkeypatch.setattr(bundled_engine, 'probe_external', refuse)
    config = config_with_bundle(tmp_path)
    config.aethermind.source = 'external'
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == ['']
        assert session.memory_choice['source'] == 'embedded'
        assert session.memory_choice['external_error_name'] == 'ValueError'
        assert 'No external service is configured' in session.memory_choice['external_error']
        text = session._turn_state('main', [])
        assert 'was not used' in text and 'The embedded engine is active.' in text
    finally:
        session.close()


def test_settings_choice_is_embedded_or_external_and_names_the_active_one(tmp_path, recorder):
    from tessera.sessions import SessionManager

    config = config_with_bundle(tmp_path)
    with SessionManager(config, backend_factory=lambda *args, **kwargs: FakeBackend([])) as manager:
        fields = {field['id']: field for section in manager.settings_view()['sections']
                  if section['id'] == 'integrations' for field in section['fields']}
        assert 'aethermind.enabled' not in fields
        choice = fields['aethermind.source']
        assert choice['type'] == 'enum'
        assert [option['value'] for option in choice['options']] == ['embedded', 'external']
        assert choice['value'] == 'embedded'
        assert choice['status'].startswith('bytes present, not started')
        assert 'build receipt' in choice['status']
        (tmp_path / 'config.toml').write_text('[aethermind]\nsource = "embedded"\n')
        result = manager.apply_settings({'id': 'aethermind.source', 'value': 'external'})
        assert result['changed'] is True
        assert manager.config.aethermind.source == 'external'


def test_an_explicit_bundled_path_is_still_honoured(tmp_path, recorder):
    """A human who typed bundled_path must keep working."""
    config = config_with_bundle(tmp_path, bundled_path='/somewhere/aethermind-engine')
    session = Session(config, backend=FakeBackend([]))
    try:
        assert recorder == ['/somewhere/aethermind-engine']
        assert session.capture.client is not None
    finally:
        session.close()
