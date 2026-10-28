"""Admission observations use fake engine HTTP, never the operator's engines."""

import json

import pytest

from tessera.config import AccountConfig, BackendConfig, Config
from tessera.scheduler import Scheduler


def configured(tmp_path, monkeypatch, *, enabled=True, kind='openai_chat', model='local-model'):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.enabled = True
    if enabled is not None:
        config.swarm.pressure_hold_enabled = enabled
    # Assert the default contract too, so these gates detect an unimplemented main.
    assert config.swarm.pressure_hold_enabled is (False if enabled is None else enabled)
    assert config.swarm.pressure_soft_threshold == 0.75
    config.accounts['pool'] = AccountConfig(name='pool', provider_kind=kind)
    config.backends['worker'] = BackendConfig(name='worker', account='pool', model=model)
    config.infrastructure.pressure_engines = [dict(host='engine.invalid', port=8888, model='local-model', slots=8)]
    state = {'running': 0, 'waiting': 0, 'warming': False, 'unreachable': False, 'calls': []}

    def transport(url, timeout):
        state['calls'].append(url)
        if state['unreachable']:
            raise ConnectionRefusedError('fixture engine refused')
        if url.endswith('/health'):
            return json.dumps(dict(max_batch_size=4, warming=state['warming']))
        return (f"tensorfold:requests_running {state['running']}\n"
                f"tensorfold:requests_waiting {state['waiting']}\n")

    monkeypatch.setattr('tessera.engine_pressure._get', transport)
    scheduler = Scheduler.from_config(config)
    task = scheduler.store.create('Read source', ['source'], {}, 'pool')
    scheduler.enqueue(task['id'])
    return scheduler, task, state


def test_local_engine_threshold_holds_and_retries(tmp_path, monkeypatch):
    scheduler, task, state = configured(tmp_path, monkeypatch)
    state.update(running=1, waiting=1, warming=True)
    assert scheduler.claim_next('pool', 'worker', backend='worker') is None
    held = scheduler.store.get(task['id'])
    assert held['status'] == 'queued' and held['attempt'] == 0
    state['warming'] = False
    assert scheduler.claim_next('pool', 'worker', backend='worker')['id'] == task['id']


def test_cli_worker_bypasses_pressure(tmp_path, monkeypatch):
    scheduler, task, state = configured(tmp_path, monkeypatch, kind='cli_worker')
    state.update(running=4, unreachable=True)
    assert scheduler.claim_next('pool', 'worker', backend='worker')['id'] == task['id']
    assert state['calls'] == []


def test_unreachable_engine_does_not_admit(tmp_path, monkeypatch):
    scheduler, task, state = configured(tmp_path, monkeypatch)
    state['unreachable'] = True
    for _ in range(2):
        assert scheduler.claim_next('pool', 'worker', backend='worker') is None
    assert scheduler.store.get(task['id'])['attempt'] == 0
    state['unreachable'] = False
    assert scheduler.claim_next('pool', 'worker', backend='worker')['id'] == task['id']


def test_no_config_preserves_admission(tmp_path, monkeypatch):
    scheduler, task, state = configured(tmp_path, monkeypatch, enabled=None)
    state['unreachable'] = True
    assert scheduler.claim_next('pool', 'worker')['id'] == task['id']
    assert state['calls'] == []


@pytest.mark.parametrize('approval', ['ask', 'yolo'])
def test_explicit_pressure_hold_applies_in_safe_and_yolo(tmp_path, monkeypatch, approval):
    scheduler, task, state = configured(tmp_path, monkeypatch, enabled=True)
    scheduler.pressure_config.tools.approval = approval
    state.update(running=1, waiting=1, warming=True)
    assert scheduler.pressure_allows('pool', 'worker') is False
    assert scheduler.claim_next('pool', 'worker', backend='worker') is None
    scheduler.pressure_config.swarm.pressure_hold_enabled = False
    assert scheduler.claim_next('pool', 'worker', backend='worker')['id'] == task['id']


def test_hosted_backend_bypasses_pressure(tmp_path, monkeypatch):
    scheduler, task, state = configured(tmp_path, monkeypatch, model='hosted-model')
    state['unreachable'] = True
    assert scheduler.claim_next('pool', 'worker', backend='worker')['id'] == task['id']
    assert state['calls'] == []


@pytest.mark.parametrize('value', [0, -1, 1.1, float('nan'), True])
def test_invalid_soft_threshold(tmp_path, value):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.swarm.pressure_soft_threshold = value
    with pytest.raises(ValueError, match='soft.threshold'):
        config.validate()
