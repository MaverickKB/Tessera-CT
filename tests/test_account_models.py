"""Connected accounts must list their models, and each switch must be its own.

The Models page reads account_rows(), and a switch calls set_model_state().
These tests hold the whole contract: a failing account does not blank the
others, and listing a model never silently takes over the default.
"""

import json

from tessera.account_models import account_backend_name, account_rows, matching_backend, role_for
from tessera.config import Config, load_config
from tessera.providers import ResponsesAdapter
from tessera.sessions import SessionManager


def bare(tmp_path):
    return Config(home=tmp_path, cwd=tmp_path)


def test_account_rows_lists_every_connected_account_and_keeps_a_failure_local(tmp_path, monkeypatch):
    from tessera import account_models

    config = bare(tmp_path)
    states = {'xai': 'current', 'openai-codex': 'current'}
    monkeypatch.setattr(account_models, 'grant_report',
                        lambda home, provider: {'state': states[provider], 'status': 'Signed in'})

    def fake_discover(config_arg, account):
        if account == 'openai-codex':
            raise account_models.DiscoveryError('The provider refused the credential', 'authentication')
        return ['grok-4.7', 'grok-4.6']

    monkeypatch.setattr(account_models, 'discover_account', fake_discover)
    rows = account_rows(config)

    by_id = {}
    for row in rows:
        by_id[row['id']] = row
    assert list(by_id) == ['xai', 'openai-codex']
    assert [model['id'] for model in by_id['xai']['models']] == ['grok-4.7', 'grok-4.6']
    assert by_id['xai']['error'] == ''
    assert by_id['openai-codex']['models'] == []
    assert 'refused the credential' in by_id['openai-codex']['error']


def test_a_signed_out_account_lists_no_models(tmp_path, monkeypatch):
    from tessera import account_models

    config = bare(tmp_path)
    monkeypatch.setattr(account_models, 'grant_report',
                        lambda home, provider: {'state': 'missing', 'status': 'Not signed in'})
    monkeypatch.setattr(account_models, 'discover_account',
                        lambda config_arg, account: ['should not be asked'])
    rows = account_rows(config)
    for row in rows:
        assert row['models'] == []
        assert row['state'] == 'missing'


def test_backend_name_is_derived_once_and_reused(tmp_path):
    config = bare(tmp_path)
    first = account_backend_name(config, 'openai-codex', 'gpt-6.1-sol')
    assert first == 'codex-gpt-6-1-sol'

    config.backends['codex-other'] = type('Backend', (), {
        'model': 'gpt-6.1-sol', 'credential_ref': 'openai-codex', 'enabled': True,
    })()
    assert matching_backend(config, 'openai-codex', 'gpt-6.1-sol') == 'codex-other'
    assert account_backend_name(config, 'openai-codex', 'gpt-6.1-sol') == 'codex-other'


def test_role_for_names_the_role_a_backend_holds(tmp_path):
    config = bare(tmp_path)
    config.roles['main'] = 'codex-gpt-6-1-sol'
    config.roles['fast'] = 'grok-4.7'
    assert role_for(config, 'codex-gpt-6-1-sol') == 'main'
    assert role_for(config, 'grok-4.7') == 'fast'
    assert role_for(config, '') == ''
    assert role_for(config, 'unknown') == ''


AUTO_HOME = '''[backends.Auto]
model = "auto"
base_url = "http://127.0.0.1:9/v1"

[roles]
main = "Auto"
'''


def test_listing_a_model_never_changes_the_default(tmp_path):
    (tmp_path / 'config.toml').write_text(AUTO_HOME)
    manager = SessionManager(load_config(tmp_path))
    try:
        assert manager.config.roles['main'] == 'Auto'
        result = manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=True)
        assert result['changed'] is True
        config = manager.config
        assert 'codex-gpt-6-1-sol' in config.backends
        assert config.backends['codex-gpt-6-1-sol'].chat is True
        assert config.roles['main'] == 'Auto', 'listing a model must not take over chat'

        again = manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=True)
        assert again['changed'] is False, 'the same switch writes no second row'
    finally:
        manager.close()


def test_making_a_model_default_is_its_own_switch(tmp_path):
    (tmp_path / 'config.toml').write_text(AUTO_HOME)
    manager = SessionManager(load_config(tmp_path))
    try:
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', make_default=True)
        assert manager.config.roles['main'] == 'codex-gpt-6-1-sol'
    finally:
        manager.close()


def test_the_default_cannot_leave_the_chat_list_first(tmp_path):
    (tmp_path / 'config.toml').write_text(AUTO_HOME)
    manager = SessionManager(load_config(tmp_path))
    try:
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', make_default=True)
        try:
            manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=False)
        except ValueError as error:
            assert 'another default model' in str(error)
        else:
            raise AssertionError('the default model must not leave the chat list first')
        manager.settings.set_model_state('openai-codex', 'gpt-6-luna', make_default=True)
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=False)
        assert manager.config.backends['codex-gpt-6-1-sol'].chat is False
    finally:
        manager.close()


def test_state_refuses_an_unknown_account(tmp_path):
    (tmp_path / 'config.toml').write_text(AUTO_HOME)
    manager = SessionManager(load_config(tmp_path))
    try:
        try:
            manager.settings.set_model_state('nobody', 'model', chat=True)
        except ValueError as error:
            assert 'Unknown account' in str(error)
        else:
            raise AssertionError('an unknown account must be refused')
    finally:
        manager.close()


def test_account_rows_report_chat_and_default_state(tmp_path, monkeypatch):
    from tessera import account_models

    config = bare(tmp_path)
    monkeypatch.setattr(account_models, 'grant_report',
                        lambda home, provider: {'state': 'current', 'status': 'Signed in'})
    monkeypatch.setattr(account_models, 'discover_account',
                        lambda config_arg, account: ['gpt-6.1-sol'])
    config.backends['codex-gpt-6-1-sol'] = type('Backend', (), {
        'model': 'gpt-6.1-sol', 'credential_ref': 'openai-codex',
        'enabled': True, 'chat': True,
    })()
    config.roles['main'] = 'codex-gpt-6-1-sol'
    rows = account_rows(config)
    codex = next(row for row in rows if row['id'] == 'openai-codex')
    model = codex['models'][0]
    assert model['chat'] is True
    assert model['enabled'] is True
    assert model['role'] == 'main'


def _stream(events):
    lines = []
    for event in events:
        lines.append(('event: ' + event['type'] + '\n').encode())
        lines.append(('data: ' + json.dumps(event) + '\n').encode())
        lines.append(b'\n')
    return lines


def test_responses_assemble_folds_a_stream_and_reports_text():
    adapter = ResponsesAdapter()
    seen = []
    completed = {
        'type': 'response.completed',
        'response': {
            'status': 'completed',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}],
            'usage': {},
        },
    }
    lines = _stream([
        {'type': 'response.created', 'response': {'status': 'in_progress'}},
        {'type': 'response.output_text.delta', 'delta': 'o'},
        {'type': 'response.output_text.delta', 'delta': 'k'},
        completed,
    ])
    # A keepalive frame carries no payload and must not be parsed as JSON.
    lines.insert(3, b'data:\n')
    final = adapter.assemble(lines, lambda kind, text: seen.append((kind, text)))
    assert final['status'] == 'completed'
    result = adapter.decode(final)
    assert result['content'] == 'ok'
    assert seen == [('text', 'o'), ('text', 'k')]


def test_responses_assemble_refuses_a_stream_without_a_response():
    adapter = ResponsesAdapter()
    try:
        adapter.assemble(_stream([{'type': 'response.created', 'response': {'status': 'in_progress'}}]),
                         lambda kind, text: None)
    except ValueError as error:
        assert 'without a response' in str(error)
    else:
        raise AssertionError('a stream with no completed event must be refused')


def test_choosing_default_restores_chat_eligibility(tmp_path):
    """A default the picker does not offer is a state nobody can use."""
    (tmp_path / 'config.toml').write_text(AUTO_HOME)
    manager = SessionManager(load_config(tmp_path))
    try:
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=True)
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', chat=False)
        assert manager.config.backends['codex-gpt-6-1-sol'].chat is False
        manager.settings.set_model_state('openai-codex', 'gpt-6.1-sol', make_default=True)
        backend = manager.config.backends['codex-gpt-6-1-sol']
        assert manager.config.roles['main'] == 'codex-gpt-6-1-sol'
        assert backend.chat is True, 'the default is in the picker'
        assert backend.enabled is True
    finally:
        manager.close()
