"""Displayed credential source matches the source a turn would resolve."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tessera.config import AccountConfig, BackendConfig, Config
from tessera.credentials import CredentialError, CredentialReference, CredentialResolver, KeychainStore, MemoryKeychain
from tessera.server import create_server
from tessera.settings_ladder import credential_source
from tessera.settings_model import project
from test_server import call
import threading


class _Completed:
    returncode = 0
    stdout = 'keychain-secret-value\n'


def _resolver_source(config, name, environ):
    backend = config.backends[name]
    pool = config.accounts.get(backend.account) if backend.account else None
    kind = pool.provider_kind if pool else (backend.provider_kind or 'openai_chat')
    reference = pool.credential_ref if pool else backend.credential_ref
    if kind == 'cli_worker':
        return 'cli'
    env_name = backend.api_key_env or ''
    cred = config.credentials.get(reference) if reference else None
    if cred is not None and getattr(cred, 'oauth', ''):
        env_name = ''
    resolver = CredentialResolver(
        config.credentials, environ=environ, platform='linux', oauth_home=str(config.home),
        which=lambda _name: '/usr/bin/secret-tool', run=lambda *_args, **_kwargs: _Completed())
    try:
        return resolver.resolve(reference, env=env_name).source
    except CredentialError:
        return 'unavailable'


def _config(tmp_path):
    config = Config()
    config.home = tmp_path
    return config


def _backend(name, **kwargs):
    return BackendConfig(name=name, base_url='http://127.0.0.1:9/v1', model='m', **kwargs)


CASES = [
    'env_overrides_credential',
    'credential_env',
    'sign_in_clears_env',
    'keychain',
    'config_ref_chain',
    'nothing',
    'unset_env',
    'pool_credential',
    'cli_worker',
]


def _prepare(tmp_path, case, environ):
    config = _config(tmp_path)
    if case == 'env_overrides_credential':
        config.credentials['slot'] = CredentialReference(env='SLOT_ENV')
        config.backends['chat'] = _backend('chat', api_key_env='DIRECT', credential_ref='slot')
        environ['DIRECT'] = 'direct-secret-value'
        environ['SLOT_ENV'] = 'slot-secret-value'
    elif case == 'credential_env':
        config.credentials['slot'] = CredentialReference(env='SLOT_ENV')
        config.backends['chat'] = _backend('chat', credential_ref='slot')
        environ['SLOT_ENV'] = 'slot-secret-value'
    elif case == 'sign_in_clears_env':
        grant = tmp_path / 'oauth' / 'xai.json'
        grant.parent.mkdir()
        grant.write_text(json.dumps({'access_token': 'oauth-secret-value', 'expires_at': 9_999_999_999}))
        config.credentials['xai-oauth'] = CredentialReference(oauth='xai')
        config.backends['chat'] = _backend('chat', api_key_env='DIRECT', credential_ref='xai-oauth')
        environ['DIRECT'] = 'direct-secret-value'
    elif case == 'keychain':
        config.credentials['slot'] = CredentialReference(keychain_service='svc', keychain_account='acct')
        config.backends['chat'] = _backend('chat', credential_ref='slot')
    elif case == 'config_ref_chain':
        config.credentials['outer'] = CredentialReference(config_ref='inner')
        config.credentials['inner'] = CredentialReference(env='SLOT_ENV')
        config.backends['chat'] = _backend('chat', credential_ref='outer')
        environ['SLOT_ENV'] = 'slot-secret-value'
    elif case == 'nothing':
        config.backends['chat'] = _backend('chat')
    elif case == 'unset_env':
        config.backends['chat'] = _backend('chat', api_key_env='MISSING_ENV')
    elif case == 'pool_credential':
        config.credentials['pooled'] = CredentialReference(env='SLOT_ENV')
        config.accounts['pool'] = AccountConfig(name='pool', provider_kind='openai_chat', credential_ref='pooled')
        config.backends['chat'] = _backend('chat', account='pool', credential_ref='other')
        environ['SLOT_ENV'] = 'slot-secret-value'
    elif case == 'cli_worker':
        config.backends['chat'] = BackendConfig(
            name='chat', provider_kind='cli_worker', model='m',
            extra={'cli': {'vendor': 'grok', 'command': 'grok', 'cwd': 'workers'}})
    return config


@pytest.mark.parametrize('case', CASES)
def test_displayed_source_matches_the_resolved_source(tmp_path, case):
    environ = {}
    config = _prepare(tmp_path, case, environ)
    shown = credential_source(config, 'chat', environ=environ)
    assert shown['source'] == _resolver_source(config, 'chat', environ)
    blob = json.dumps(shown) + json.dumps(project(config, {}))
    for secret in ('direct-secret-value', 'slot-secret-value', 'oauth-secret-value', 'keychain-secret-value'):
        assert secret not in blob


def test_no_model_dropdown_asks_for_a_credential(tmp_path):
    config = _prepare(tmp_path, 'credential_env', {'SLOT_ENV': 'slot-secret-value'})
    body = project(config, {})

    def enums(node, found):
        if isinstance(node, dict):
            if node.get('type') == 'enum':
                found.append(node)
            for child in node.values():
                enums(child, found)
        elif isinstance(node, list):
            for child in node:
                enums(child, found)

    found = []
    enums(body, found)
    named = [field['id'] for field in found
             if any(option.get('value') == 'slot' for option in field.get('options') or [])]
    allowed = ('accounts.', 'jev.credential_ref', 'computer_use.jev.credential_ref')
    assert named and all(item.startswith(allowed) for item in named)


def _serve(tmp_path, text=''):
    (tmp_path / 'config.toml').write_text(text)
    from tessera.config import load_config
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_a_signed_in_provider_can_add_a_model_and_shows_the_sign_in(tmp_path):
    grant = tmp_path / 'oauth' / 'xai.json'
    grant.parent.mkdir()
    grant.write_text(json.dumps({'access_token': 'oauth-secret-value', 'expires_at': 9_999_999_999}))
    server, thread = _serve(tmp_path)
    try:
        added = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'grokmain', 'account': 'xai', 'model': 'grok-4', 'chat': True}})
        assert added[0] == 200, added[1]
        code, body = call(server, 'GET', '/api/settings')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert code == 200
    providers = next(field for section in body['sections'] if section['id'] == 'providers'
                     for field in section['fields'] if field['id'] == 'providers')
    row = next(record for record in providers['records'] if record['name'] == 'grokmain')
    assert row['source_kind'] == 'oauth'
    assert row['group'] == 'xAI Grok'
    assert 'oauth-secret-value' not in json.dumps(body)
    text = (tmp_path / 'config.toml').read_text()
    assert 'xai-oauth' in text


def test_where_stored_matches_resolve_when_env_and_keychain_are_both_set(tmp_path, monkeypatch):
    monkeypatch.setenv('BOTH_KEY', 'env-secret-value')
    config = _config(tmp_path)
    config.credentials['both'] = CredentialReference(
        env='BOTH_KEY', keychain_service='svc', keychain_account='acct')
    resolved = CredentialResolver(config.credentials, oauth_home=str(config.home)).resolve('both')
    assert resolved.source == 'env'
    body = project(config, {})
    creds = next(field for section in body['sections'] if section['id'] == 'credentials'
                 for field in section['fields'] if field['id'] == 'credentials')
    record = next(row for row in creds['records'] if row['name'] == 'both')
    assert record['source_kind'] == resolved.source
    assert 'BOTH_KEY' in record['stored']
    assert 'Keychain' not in record['stored']
    blob = json.dumps(body)
    assert 'env-secret-value' not in blob
    assert resolved.value not in blob


def test_first_settings_save_creates_a_missing_config(tmp_path):
    assert not (tmp_path / 'config.toml').exists()
    from tessera.config import load_config
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'localbox', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'one', 'chat': True}})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert code == 200, body
    text = (tmp_path / 'config.toml').read_text()
    assert 'localbox' in text


def test_settings_load_does_not_resolve_or_refresh_an_expired_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(CredentialResolver, 'resolve', lambda *a, **k: pytest.fail('resolve'))
    monkeypatch.setattr('tessera.account_oauth.access_token', lambda *a, **k: pytest.fail('refresh'))
    monkeypatch.setattr('tessera.account_oauth.refresh', lambda *a, **k: pytest.fail('refresh'))
    grant = tmp_path / 'oauth' / 'xai.json'
    grant.parent.mkdir()
    grant.write_text(json.dumps({
        'access_token': 'oauth-secret-value', 'refresh_token': 'refresh-secret-value', 'expires_at': 1,
    }))
    config = _config(tmp_path)
    config.credentials['xai-oauth'] = CredentialReference(oauth='xai')
    config.credentials['both'] = CredentialReference(
        env='BOTH_KEY', keychain_service='svc', keychain_account='acct')
    monkeypatch.setenv('BOTH_KEY', 'env-secret-value')
    body = project(config, {})
    text = json.dumps(body)
    assert 'Signed in, token expired' in text
    assert 'oauth-secret-value' not in text and 'refresh-secret-value' not in text
    creds = next(field for section in body['sections'] if section['id'] == 'credentials'
                 for field in section['fields'] if field['id'] == 'credentials')
    both = next(row for row in creds['records'] if row['name'] == 'both')
    assert both['source_kind'] == 'env'
    assert 'Keychain' not in both['stored']
    accounts = body['provider_pages']['accounts']['available']
    xai = next(row for row in accounts if row['id'] == 'xai')
    assert xai['pill'] == 'Not connected'
    assert 'Not signed in' not in (xai.get('line') or '')
    assert 'Signed in, token expired' in xai['line']


def test_env_name_fields_accept_names_and_reject_secret_values(tmp_path):
    server, thread = _serve(tmp_path)
    token = 'AbCdEfGhIjKlMnOpQrStUv0123456789'
    try:
        for secret in (token, 'xai-live-secret-value'):
            refused = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
                'name': 'remote', 'account': 'other', 'kind': 'openai_chat',
                'base_url': 'http://127.0.0.1:9/v1', 'model': 'one',
                'api_key_env': secret, 'chat': True}})
            assert refused[0] == 400
            assert 'environment variable name' in refused[1]['error']
        kept = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'remote', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'llama-3.1-8b-demo',
            'api_key_env': 'MODEL_API_KEY', 'chat': True}})
        assert kept[0] == 200, kept[1]
        other = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'other', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'one',
            'api_key_env': 'pasted_api_key', 'chat': True}})
        assert other[0] == 200, other[1]
        named = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'add', 'record': {
            'name': 'desk', 'env': 'api_key'}})
        assert named[0] == 200, named[1]
        bad = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'add', 'record': {
            'name': 'desk2', 'env': token}})
        assert bad[0] == 400 and bad[1]['error'] == 'Env var must be a name'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    text = (tmp_path / 'config.toml').read_text()
    assert 'MODEL_API_KEY' in text
    assert 'pasted_api_key' in text
    assert '"env" = "api_key"' in text
    assert token not in text
    assert 'xai-live-secret-value' not in text
    assert 'remote' in text and 'other' in text


def test_api_key_env_rejects_a_pasted_secret_and_clears(tmp_path):
    server, thread = _serve(tmp_path)
    try:
        refused = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'remote', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'one',
            'api_key_env': 'sk-live-secret-value', 'chat': True}})
        assert refused[0] == 400
        assert 'environment variable name' in refused[1]['error']
        added = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'remote', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'one',
            'api_key_env': 'DEMO_XAI_KEY', 'chat': True}})
        assert added[0] == 200, added[1]
        cleared = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'edit', 'name': 'remote',
                                                         'record': {'api_key_env': ''}})
        assert cleared[0] == 200, cleared[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    text = (tmp_path / 'config.toml').read_text()
    assert 'DEMO_XAI_KEY' not in text
    assert 'sk-live-secret-value' not in text


def _credential_row(body, name):
    creds = next(field for section in body['sections'] if section['id'] == 'credentials'
                 for field in section['fields'] if field['id'] == 'credentials')
    return next(row for row in creds['records'] if row['name'] == name)


def test_keychain_status_checks_the_item_without_reading_it(tmp_path):
    config = _config(tmp_path)
    config.credentials['slot'] = CredentialReference(keychain_service='svc', keychain_account='acct')
    secret = 'synthetic-os-secret'
    empty = MemoryKeychain()
    missing_record = _credential_row(project(config, {}, keychain=empty), 'slot')
    assert missing_record['pill'] == 'Keychain item missing'
    assert missing_record['status'] == 'Keychain item missing'
    assert missing_record['pill'] != 'Configured'
    held = MemoryKeychain()
    held.put('svc', 'acct', secret)
    found_record = _credential_row(project(config, {}, keychain=held), 'slot')
    assert found_record['pill'] == 'Keychain item found'
    assert found_record['line'] == 'Keychain item: slot'
    assert found_record['added'] == ''
    assert 'Not recorded' not in json.dumps(found_record)
    blob = json.dumps(project(config, {}, keychain=held))
    assert secret not in blob
    assert held.items[('svc', 'acct')] == secret
    calls = []
    store = KeychainStore(
        platform='linux',
        which=lambda name: calls.append(name) or name,
        run=lambda *_args, **_kwargs: calls.append('run'),
    )
    unavailable_record = _credential_row(project(config, {}, keychain=store), 'slot')
    assert unavailable_record['pill'] == 'Keychain not available on this system'
    assert unavailable_record['status'] == 'Keychain not available on this system'
    assert calls == []
    assert 'Configured' not in unavailable_record['pill']



def test_a_sign_in_credential_reports_its_grant_not_a_stored_value(tmp_path, monkeypatch):
    """Saved credentials and Accounts must not disagree about the same account."""
    from tessera import settings_model

    config = _config(tmp_path)
    config.credentials['xai-oauth'] = CredentialReference(oauth='xai')

    expired = {'state': 'expired', 'status': 'Access expired. Refresh or sign in again.',
               'has_refresh': True, 'expires_at': 0}
    monkeypatch.setattr(settings_model, 'grant_report', lambda home, provider: expired)
    creds = next(field for section in settings_model.project(config, {})['sections']
                 if section['id'] == 'credentials' for field in section['fields']
                 if field['id'] == 'credentials')
    row = next(item for item in creds['records'] if item['name'] == 'xai-oauth')
    assert row['pill'] == 'Sign-in expired'
    assert row['status'] == 'Access expired. Refresh or sign in again.'
    assert row['line'] == 'Sign-in: xai-oauth'

    current = {'state': 'current', 'status': 'Signed in until 2026-11-07',
               'has_refresh': True, 'expires_at': 0}
    monkeypatch.setattr(settings_model, 'grant_report', lambda home, provider: current)
    creds = next(field for section in settings_model.project(config, {})['sections']
                 if section['id'] == 'credentials' for field in section['fields']
                 if field['id'] == 'credentials')
    row = next(item for item in creds['records'] if item['name'] == 'xai-oauth')
    assert row['pill'] == 'Connected'
