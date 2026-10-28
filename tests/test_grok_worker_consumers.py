"""Grok credential consumers use local stubs, never a login store or provider."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tessera.cli_worker import CliWorker, WorkerError
from tessera.credentials import CredentialResolver


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    guards = []
    for target in ('subprocess.Popen', 'subprocess.run', 'urllib.request.urlopen',
                   'urllib.request.OpenerDirector.open'):
        guard = Mock(side_effect=AssertionError('Grok consumers cannot start external work'))
        monkeypatch.setattr(target, guard)
        guards.append(guard)
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)
    yield
    for guard in guards:
        guard.assert_not_called()


def worker_for(tmp_path):
    return CliWorker({'vendor': 'grok', 'command': 'grok', 'cwd': str(tmp_path),
                      'timeout': 5, 'fallback_credential_ref': 'fixture',
                      'fallback_env': 'XAI_API_KEY', 'allow_api_key_fallback': True}, credential_sources={'fixture': {
                          'keychain_service': 'fixture-service', 'keychain_account': 'fixture-account'}})


def test_authentication_fallback_uses_keychain_stub_and_redacts_nested_results(tmp_path, monkeypatch):
    worker = worker_for(tmp_path)
    monkeypatch.setenv('XAI_API_KEY', 'fixture-inherited-key')
    secret = 'fixture-keychain-secret'
    keychain_calls = []
    def lookup(arguments, **options):
        keychain_calls.append((arguments, options))
        return SimpleNamespace(returncode=0, stdout=secret + '\n')
    def resolver(sources):
        return CredentialResolver(sources, environ={}, platform='darwin',
                                  which=lambda command: command, run=lookup)
    monkeypatch.setattr('tessera.grok_worker.CredentialResolver', resolver)
    attempts = []
    def invoke(arguments, environment, prompt):
        attempts.append('XAI_API_KEY' in environment)
        assert prompt == b''
        if len(attempts) == 1:
            assert 'XAI_API_KEY' not in environment
            raise WorkerError('Synthetic authentication failure', 'authentication')
        assert environment['XAI_API_KEY'] == secret
        return {'content': 'Synthetic ' + secret, 'usage': {'total_tokens': 3},
                'fixture_metadata': {secret: [secret]}}
    monkeypatch.setattr(worker, '_invoke', invoke)
    result = worker.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert worker.auth_path == 'api_key_fallback' and len(attempts) == 2
    assert attempts == [False, True]
    assert len(keychain_calls) == 1
    assert keychain_calls[0][0] == ['security', 'find-generic-password', '-s', 'fixture-service',
                                  '-a', 'fixture-account', '-w']
    assert keychain_calls[0][1]['stdin'] is not None
    assert keychain_calls[0][1]['timeout'] > 0
    assert result == {'content': 'Synthetic [REDACTED]', 'usage': {'total_tokens': 3},
                      'fixture_metadata': {'[REDACTED]': ['[REDACTED]']}}
    assert secret not in repr(result)
    assert os.environ['XAI_API_KEY'] == 'fixture-inherited-key'


@pytest.mark.parametrize('category', ['timeout', 'provider'])
def test_non_authentication_failure_never_resolves_fallback_or_keeps_stale_auth_path(tmp_path, monkeypatch, category):
    worker = worker_for(tmp_path)
    worker.auth_path = 'cli_login'
    resolver = Mock(side_effect=AssertionError('Only authentication failure permits credential fallback'))
    monkeypatch.setattr('tessera.grok_worker.CredentialResolver', resolver)
    invoke = Mock(side_effect=WorkerError('Synthetic worker failure', category))
    monkeypatch.setattr(worker, '_invoke', invoke)
    with pytest.raises(WorkerError) as error:
        worker.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == category
    assert worker.auth_path == ''
    invoke.assert_called_once()
    resolver.assert_not_called()
