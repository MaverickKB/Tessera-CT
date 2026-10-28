"""Deterministic provider-boundary consumers of configured agent budgets."""

from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from tessera.agent_budget import AgentBackend
from tessera.backend import BackendError
from tessera.config import AccountConfig, BackendConfig, Config


def adapter(tmp_path, monkeypatch, response, *, tokens=7, seconds=2.5, elapsed=0):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.accounts['fixture-pool'] = AccountConfig('fixture-pool', provider_kind='cli_worker')
    config.backends['fixture-worker'] = BackendConfig('fixture-worker', account='fixture-pool',
        extra={'timeout': 30, 'cli': {'timeout': 30}})
    config.roles['main'] = 'fixture-worker'
    selected = config.backend_for()
    clock = [10.0]
    monkeypatch.setattr('tessera.agent_budget.time', SimpleNamespace(monotonic=lambda: clock[0]))
    def reply(messages, tools):
        clock[0] += elapsed
        if isinstance(response, Exception):
            raise response
        return response
    delegate = FakeBackend([reply])
    observations = []
    provider = AgentBackend(selected, lambda: {'tokens': tokens, 'seconds': seconds},
                            lambda usage, reported: observations.append((usage, reported)), delegate=delegate)
    return provider, delegate, observations, config


@pytest.mark.parametrize('tokens,seconds', [(0, 2.5), (7, 0)])
def test_exhausted_budget_prevents_provider_and_usage_callback(tmp_path, monkeypatch, tokens, seconds):
    provider, delegate, observed, _ = adapter(tmp_path, monkeypatch, {'content': 'unused'}, tokens=tokens, seconds=seconds)
    with pytest.raises(BackendError) as raised:
        provider.chat([])
    assert raised.value.category == 'quota'
    assert delegate.calls == observed == []


def test_cancel_targets_the_delegate_that_actually_runs_the_call(tmp_path, monkeypatch):
    provider, delegate, observed, _ = adapter(tmp_path, monkeypatch, {'content': 'unused'})
    cancellations = []
    delegate.cancel = lambda: cancellations.append('delegate') or True
    assert provider.cancel()
    assert cancellations == ['delegate']


def test_remaining_wall_budget_clips_cli_and_transport_without_mutating_config(tmp_path, monkeypatch):
    response = {'content': 'Synthetic result', 'usage': {'input_tokens': 2, 'output_tokens': 1}}
    provider, delegate, observed, config = adapter(tmp_path, monkeypatch, response, elapsed=2)
    assert provider.chat([])['content'] == 'Synthetic result'
    assert provider.worker.settings.timeout == provider.extra['timeout'] == 2.5
    assert config.backends['fixture-worker'].extra['cli']['timeout'] == 30
    assert config.backends['fixture-worker'].extra['timeout'] == 30
    assert observed == [({'tokens': 3, 'seconds': 2}, True)]
    assert len(delegate.calls) == 1


def test_missing_usage_reserves_entire_remaining_token_cap(tmp_path, monkeypatch):
    provider, delegate, observed, _ = adapter(tmp_path, monkeypatch, {'content': 'Synthetic result'})
    assert provider.chat([])['content'] == 'Synthetic result'
    assert observed == [({'tokens': 7, 'seconds': 0}, False)]
    assert len(delegate.calls) == 1


@pytest.mark.parametrize('reported_tokens,elapsed', [(8, 0), (3, 3)])
def test_token_or_wall_overrun_is_charged_and_never_returned_as_success(tmp_path, monkeypatch, reported_tokens, elapsed):
    provider, delegate, observed, _ = adapter(tmp_path, monkeypatch,
        {'content': 'Unusable over-budget result', 'usage': {'total_tokens': reported_tokens}}, elapsed=elapsed)
    with pytest.raises(BackendError) as raised:
        provider.chat([])
    assert raised.value.category == 'quota'
    assert raised.value.usage == {'tokens': reported_tokens, 'seconds': elapsed}
    assert observed == [({'tokens': reported_tokens, 'seconds': elapsed}, True)]
    assert len(delegate.calls) == 1


def test_provider_failure_consumes_reservation_without_retry(tmp_path, monkeypatch):
    provider, delegate, observed, _ = adapter(tmp_path, monkeypatch,
        BackendError('Synthetic connection failure', 'connection'), elapsed=1)
    with pytest.raises(BackendError) as raised:
        provider.chat([])
    assert raised.value.category == 'connection'
    assert observed == [({'tokens': 7, 'seconds': 1}, False)]
    assert len(delegate.calls) == 1
