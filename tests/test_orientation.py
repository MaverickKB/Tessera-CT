"""Orientation stays scoped; submitted credentials never enter model/log/memory paths."""
from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest

from fake_backend import FakeBackend
from test_server import call
from tessera.config import load_config
from tessera.credentials import MemoryKeychain
from tessera.orientation import CredentialPrompts, documentation
from tessera.server import create_server


@contextmanager
def core(home, script=None):
    home.mkdir(exist_ok=True)
    target = home / 'config.toml'
    if not target.exists():
        target.write_text('[backends.local]\nbase_url="http://127.0.0.1:9/v1"\nmodel="local-test"\n[roles]\nmain="local"\n')
    config = load_config(home)
    config.server.port = 0
    backend = FakeBackend(script or ['Welcome. Would you like help with Jev, autonomy, infrastructure, or accounts?', 'The connection reference is ready.'])
    server = create_server(config, backend_factory=lambda *_: backend)
    server.manager.settings.keychain = MemoryKeychain()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def orientation(server, title='Getting started'):
    code, session = call(server, 'POST', '/api/sessions', {'title': title, 'purpose': 'orientation'})
    assert code == 201 and session['purpose'] == 'orientation'
    return session['slug']


def test_scope_survives_rename_restart_and_does_not_touch_other_sessions(tmp_path):
    with core(tmp_path) as (server, backend):
        slug = orientation(server)
        normal = server.manager.create('Getting started')
        runtime = server.manager.runtime(slug)
        ordinary = server.manager.runtime(normal['slug'])
        assert 'getting-started' in [s['name'] for s in runtime.session.shelf.skills()]
        assert 'getting-started' not in [s['name'] for s in ordinary.session.shelf.skills()]
        assert 'request_credential' in runtime.session.tools.names()
        assert 'request_credential' not in ordinary.session.tools.names()
        call(server, 'PATCH', f'/api/sessions/{slug}', {'title': 'My setup companion'})
        code, _ = call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'Hello, orient me.'})
        assert code == 202 and server.manager.wait(slug, 5)
        prompt = json.dumps(backend.calls)
        assert 'orientation and guided-setup companion' in prompt
        assert 'docs/JEV.md' in prompt
        assert 'request_credential' in prompt
    with core(tmp_path) as (server, _):
        assert server.manager._metadata[slug]['title'] == 'My setup companion'
        assert server.manager.runtime(slug).orientation
        assert server.manager.runtime(slug).session.shelf.find('getting-started')['source'] == 'session'


def test_protected_value_goes_to_store_but_not_any_model_event_transcript_config_or_outbox(tmp_path):
    with core(tmp_path) as (server, backend):
        slug = orientation(server)
        manager = server.manager
        secret = 'synthetic-protected-entry-value'
        request = manager.credential_prompts.request(slug, 'judge-key', 'Connect the Jev judge')
        code, listed = call(server, 'GET', f'/api/sessions/{slug}/credential-prompts')
        assert code == 200 and listed['requests'][0]['id'] == request['id']
        code, result = call(server, 'POST', f'/api/sessions/{slug}/credential-prompts', {'request_id': request['id'], 'value': secret})
        assert code == 200 and result['state'] == 'saved'
        assert manager.wait(slug, 5)
        reference = manager.config.credentials['judge-key']
        assert manager.settings.keychain.get(reference.keychain_service, reference.keychain_account)[0] == secret
        assert secret not in json.dumps(manager.credential_prompts.requests)
        assert secret not in json.dumps(backend.calls)
        assert secret not in json.dumps(result)
        assert secret not in json.dumps(call(server, 'GET', f'/api/sessions/{slug}/messages')[1])
        assert secret not in json.dumps(manager.runtime(slug).events.records)
        for path in tmp_path.rglob('*'):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), str(path.relative_to(tmp_path))
        turns = len(backend.calls)
        code, repeated = call(server, 'POST', f'/api/sessions/{slug}/credential-prompts', {'request_id': request['id'], 'value': 'different-synthetic-value'})
        assert code == 200 and repeated['state'] == 'saved'
        assert len(backend.calls) == turns, 'a repeated save cannot enqueue a second continuation'
        assert manager.settings.keychain.get(reference.keychain_service, reference.keychain_account)[0] == secret


def test_cancel_stop_expiry_and_cross_session_requests_cannot_write_secrets(tmp_path):
    with core(tmp_path) as (server, _):
        manager = server.manager
        first = orientation(server)
        second = orientation(server, 'Other orientation')
        request = manager.credential_prompts.request(first, 'key', 'Set up a provider')
        code, _ = call(server, 'POST', f'/api/sessions/{second}/credential-prompts', {'request_id': request['id'], 'value': 'synthetic-key'})
        assert code == 400
        code, _ = call(server, 'POST', f'/api/sessions/{first}/credential-prompts', {'request_id': request['id'], 'cancel': True})
        assert code == 200
        assert manager.settings.keychain.items == {}
        stopped = manager.credential_prompts.request(first, 'key', 'Set up a provider')
        manager.interrupt(first)
        with pytest.raises(ValueError):
            manager.credential_prompts.resolve(first, stopped['id'], 'synthetic-key')
        with pytest.raises(ValueError):
            manager.credential_prompts.request(first, 'key', 'Set up a provider')
        clock = [0]
        manager.credential_prompts = CredentialPrompts(manager, clock=lambda: clock[0])
        expired = manager.credential_prompts.request(second, 'key', 'Set up a provider')
        clock[0] = 1801
        with pytest.raises(ValueError):
            manager.credential_prompts.resolve(second, expired['id'], 'synthetic-key')
        assert manager.settings.keychain.items == {}


def test_failed_storage_is_sanitized_and_can_be_retried(tmp_path):
    with core(tmp_path) as (server, _):
        slug = orientation(server)
        manager = server.manager
        request = manager.credential_prompts.request(slug, 'key', 'Set up a provider')
        class Failing(MemoryKeychain):
            def put(self, service, account, secret):
                raise ValueError('backend echoed ' + secret)
        manager.settings.keychain = Failing()
        code, result = call(server, 'POST', f'/api/sessions/{slug}/credential-prompts', {'request_id': request['id'], 'value': 'synthetic-private-value'})
        assert code == 400
        assert 'synthetic-private-value' not in json.dumps(result)
        assert manager.credential_prompts.pending(slug)[0]['state'] == 'pending'
        assert not server.manager.config.credentials.get('key')


def test_docs_are_bounded_current_and_cannot_read_outside_bundle(monkeypatch, tmp_path):
    (tmp_path / 'docs').mkdir()
    path = tmp_path / 'docs' / 'SETUP.md'
    path.write_text('Current guidance')
    monkeypatch.setattr('tessera.orientation.asset_path', lambda name: tmp_path / name)
    assert documentation('docs/SETUP.md')['text'] == 'Current guidance'
    path.write_text('Updated guidance')
    assert documentation('docs/SETUP.md')['text'] == 'Updated guidance'
    path.write_text('x' * 13000)
    assert len(documentation('docs/SETUP.md')['text']) == 12000
    assert documentation('docs/SETUP.md')['next_offset'] == 12000
    for path in ('../config.toml', 'docs/../../config.toml', '/config.toml', 'docs/key.txt'):
        with pytest.raises(ValueError):
            documentation(path)
    assert 'docs/SETUP.md' in documentation()['files']


def test_changed_destination_and_normal_chats_cannot_replace_keys(tmp_path):
    from tessera.credentials import CredentialReference
    with core(tmp_path) as (server, _):
        manager = server.manager
        slug = orientation(server)
        normal = manager.create('Normal chat')['slug']
        with pytest.raises(ValueError):
            manager.credential_prompts.request(normal, 'key', 'Connect provider')
        request = manager.credential_prompts.request(slug, 'key', 'Connect provider')
        manager.config.credentials['key'] = CredentialReference(env='EXAMPLE_OTHER_KEY')
        with pytest.raises(ValueError, match='changed'):
            manager.credential_prompts.resolve(slug, request['id'], 'synthetic-value')
        assert manager.settings.keychain.items == {}


def test_orientation_adapters_use_existing_oauth_and_settings_paths_without_secret_results(tmp_path):
    with core(tmp_path) as (server, _):
        manager = server.manager
        slug = orientation(server)
        tools = manager.runtime(slug).session.tools
        class Desk:
            def start(self, provider, config):
                assert provider == 'xai' and config is manager.config
                return {'status': 'starting', 'verification_url': '', 'user_code': ''}
            def status(self, provider):
                assert provider == 'xai'
                return {'status': 'signed_in', 'verification_url': '', 'user_code': '', 'access_token': 'synthetic-token-that-must-stay-private'}
        manager.oauth_login = Desk()
        # Never call the real account service in offline tests; normal permission
        # policy still applies to starts/edits, so explicitly allow this test's calls.
        tools.approval = 'never'
        assert tools.execute('orientation_sign_in', {'provider': 'xai'})['status'] == 'starting'
        public = tools.execute('orientation_sign_in_status', {'provider': 'xai', 'wait_seconds': 1})
        assert public['status'] == 'signed_in'
        assert 'synthetic-token-that-must-stay-private' not in json.dumps(public)
        assert 'sections' in tools.execute('orientation_settings', {})
        with pytest.raises(ValueError):
            tools.execute('orientation_configure', {'body': {'id': 'credentials', 'action': 'add', 'record': {}}})


def test_wheel_plan_carries_the_orientation_skill_and_current_docs(tmp_path):
    from setuptools.dist import Distribution
    from build_support import BuildPy
    builder = BuildPy(Distribution())
    builder.build_lib = str(tmp_path)
    targets = [Path(target).relative_to(tmp_path).as_posix() for target, _ in builder.runtime_files()]
    assert 'tessera/_assets/README.md' in targets
    assert 'tessera/_assets/docs/JEV.md' in targets
    assert 'tessera/_assets/docs/SETUP.md' in targets
    assert 'tessera/_assets/orientation/skills/getting-started/SKILL.md' in targets


def test_archive_cancels_private_entry_even_after_restore(tmp_path):
    with core(tmp_path) as (server, _):
        slug = orientation(server)
        manager = server.manager
        request = manager.credential_prompts.request(slug, 'key', 'Connect provider')
        manager.update(slug, archived=True)
        assert manager.credential_prompts.pending(slug) == []
        manager.update(slug, archived=False)
        with pytest.raises(ValueError):
            manager.credential_prompts.resolve(slug, request['id'], 'synthetic-key')
        assert manager.settings.keychain.items == {}


@pytest.mark.parametrize('resume', [False, True])
def test_stop_invalidates_request_before_prompt_cancellation(tmp_path, monkeypatch, resume):
    with core(tmp_path) as (server, backend):
        manager = server.manager
        slug = orientation(server)
        runtime = manager.runtime(slug)
        request = manager.credential_prompts.request(slug, 'key', 'Connect provider')
        # Reproduce the window after Stop changes its generation but before
        # cancellation can acquire the protected-prompt lock.
        monkeypatch.setattr(manager.credential_prompts, 'cancel', lambda _: None)
        manager.interrupt(slug)
        if resume:
            manager.submit(slug, 'Continue our conversation')
            assert manager.wait(slug, 5)
        turns = len(backend.calls)
        with pytest.raises(ValueError, match='predates operator Stop'):
            manager.credential_prompts.resolve(slug, request['id'], 'synthetic-private-value')
        assert runtime.operator_stopped is (not resume)
        assert manager.settings.keychain.items == {}
        assert 'key' not in manager.config.credentials
        assert len(backend.calls) == turns
        assert not runtime.pending
