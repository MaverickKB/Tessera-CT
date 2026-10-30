"""Readiness is local configuration plus observed outcomes, never a probe."""

import pytest

from tessera.config import AccountConfig, BackendConfig, Config
from tessera.credentials import CredentialReference
from tessera.jev import JudgeKit, config_readiness
from tessera.settings_model import catalog


def forbidden(*args, **kwargs):
    raise AssertionError('Passive readiness must not invoke a provider or secret helper')


def test_readiness_is_passive_and_missing_configuration_never_attempts_a_judge(tmp_path, monkeypatch):
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', forbidden)
    monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
    monkeypatch.setattr('tessera.backend.Backend', forbidden)
    config = Config(home=tmp_path, cwd=tmp_path)
    assert config_readiness(config)['state'] == 'off'
    config.jev.enabled = True
    assert config_readiness(config)['state'] == 'not_configured'
    kit = JudgeKit.from_config(config)
    assert kit.readiness()['state'] == 'not_configured'
    assert not kit.configured()
    assert kit.intake('Hello')['source'] == 'heuristic'


def test_passive_readiness_follows_environment_credentials_without_leaking_them(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = True
    config.jev.url = 'https://judge.example.com/evaluate'
    config.jev.model = 'fixture-judge'
    config.jev.api_key_env = 'JEV_READINESS_KEY'
    monkeypatch.delenv('JEV_READINESS_KEY', raising=False)
    assert config_readiness(config)['state'] == 'not_configured'
    monkeypatch.setenv('JEV_READINESS_KEY', 'private-fixture-key')
    monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
    snapshot = config_readiness(config)
    assert snapshot['state'] == 'ready'
    assert 'private-fixture-key' not in str(snapshot)
    assert JudgeKit.from_config(config).readiness() == snapshot


def test_passive_keychain_readiness_does_not_run_helper(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = True
    config.jev.url = 'https://judge.example.com/evaluate'
    config.jev.model = 'fixture-judge'
    config.credentials['judge'] = CredentialReference(keychain_service='fixture', keychain_account='judge')
    config.jev.credential_ref = 'judge'
    monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
    assert config_readiness(config)['state'] == 'ready'


def test_backend_judge_with_missing_selected_credential_never_constructs_provider(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = True
    config.backends['judge'] = BackendConfig('judge', base_url='https://judge.example.com/v1',
                                             model='fixture-judge', api_key_env='JEV_READINESS_KEY')
    config.roles['jev'] = 'judge'
    monkeypatch.delenv('JEV_READINESS_KEY', raising=False)
    monkeypatch.setattr('tessera.backend.Backend', forbidden)
    kit = JudgeKit.from_config(config)
    assert config_readiness(config)['state'] == 'not_configured'
    assert kit.readiness()['state'] == 'not_configured'
    assert kit.intake('Hello')['source'] == 'heuristic'


def test_account_judge_requires_saved_oauth_but_readiness_never_refreshes_it(tmp_path, monkeypatch):
    from tessera.account_oauth import save_grant
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = True
    config.credentials['signed-in'] = CredentialReference(oauth='openai-codex')
    config.accounts['judge-account'] = AccountConfig('judge-account', provider_kind='openai_responses',
                                                     credential_ref='signed-in')
    config.backends['judge'] = BackendConfig('judge', base_url='https://judge.example.com/v1',
                                             model='fixture-judge', account='judge-account')
    config.roles['jev'] = 'judge'
    monkeypatch.setattr('tessera.account_oauth.access_token', forbidden)
    monkeypatch.setattr('tessera.credentials.CredentialResolver.resolve', forbidden)
    monkeypatch.setattr('tessera.backend.Backend', forbidden)
    assert config_readiness(config)['state'] == 'not_configured'
    kit = JudgeKit.from_config(config)
    assert not kit.configured()
    assert kit.intake('Hello')['source'] == 'heuristic'
    save_grant(tmp_path, 'openai-codex', {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
                                        'expires_at': 1})
    # Expired access with a saved refresh grant is configured; readiness itself
    # must never perform the refresh or claim the remote service is healthy.
    assert config_readiness(config)['state'] == 'ready'
    assert kit.readiness()['state'] == 'ready'


def test_account_backend_without_a_credential_is_not_treated_as_public_http(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = True
    config.accounts['judge-account'] = AccountConfig('judge-account', provider_kind='openai_responses')
    config.backends['judge'] = BackendConfig('judge', base_url='https://judge.example.com/v1',
                                             model='fixture-judge', account='judge-account')
    config.roles['jev'] = 'judge'
    assert config_readiness(config)['state'] == 'not_configured'


def test_observed_judge_failure_is_visible_and_success_clears_it():
    attempts = []
    def evaluator(state, questions):
        attempts.append(state)
        if len(attempts) == 1:
            raise OSError('private-fixture-error')
        return {'answers': {'decision': {'type': 'noul', 'noul': .2}}}
    kit = JudgeKit(enabled=True, evaluator=evaluator)
    assert kit.readiness()['state'] == 'ready'
    kit.tool_risk('first fixture command')
    snapshot = kit.readiness()
    assert snapshot['state'] == 'unavailable'
    assert 'private-fixture-error' not in str(snapshot)
    kit.tool_risk('second fixture command')
    assert kit.readiness()['state'] == 'ready'
    kit.enabled = False
    assert kit.readiness()['state'] == 'off'


@pytest.mark.parametrize('name', ['completion', 'plan', 'handoff', 'summary', 'lesson'])
def test_advisory_checkpoints_are_independent_and_off_by_default(tmp_path, name):
    config = Config(home=tmp_path, cwd=tmp_path)
    assert not getattr(config.jev, name + '_review_enabled')
    setattr(config.jev, name + '_review_enabled', True)
    kit = JudgeKit.from_config(config)
    assert getattr(kit, name + '_review_enabled') is True
    assert not config.jev.action_gate_enabled
    assert not config.jev.session_gates_enabled
    specs = {spec.id for spec, value in catalog(config, {})}
    assert 'jev.' + name + '_review_enabled' in specs


@pytest.mark.parametrize('field,value', [('review_max_calls_per_turn', 0), ('review_max_input_chars', -1),
                                       ('review_max_calls_per_turn', True), ('plan_review_enabled', 'yes'),
                                       ('review_max_calls_per_turn', 65), ('review_max_input_chars', 1048577)])
def test_review_options_are_validated(tmp_path, field, value):
    config = Config(home=tmp_path, cwd=tmp_path)
    setattr(config.jev, field, value)
    with pytest.raises(ValueError):
        config.validate()


@pytest.mark.parametrize('url', ['http://:9000', 'http://judge.example.com:invalid',
                                'http://judge.example.com:70000', 'http://judge.example.com:0'])
def test_invalid_endpoint_never_attempts_a_judge_request(monkeypatch, url):
    monkeypatch.setenv('JEV_READINESS_FIXTURE', 'fixture-key')
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen',
                        lambda *args, **kwargs: pytest.fail('Invalid endpoint was contacted'))
    kit = JudgeKit(enabled=True, url=url, model='fixture-judge', api_key_env='JEV_READINESS_FIXTURE')
    assert kit.readiness()['state'] == 'not_configured'
    kit.tool_risk('Read a file')
