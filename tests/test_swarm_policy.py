"""Offline policy acceptance through explicit queues and synthetic response headers."""
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tessera.backend import Backend, BackendError
from tessera.config import AccountConfig, Config
from tessera.scheduler import Scheduler, SchedulerHooks
from tessera.tasks import TaskStore


def configured(tmp_path, clock, **options):
    config = Config(home=tmp_path)
    config.swarm.enabled = True
    for key, value in options.items():
        setattr(config.swarm, key, value)
    config.accounts['one'] = AccountConfig(name='one')
    return config, Scheduler.from_config(config, clock=lambda: clock[0], random=lambda: .5)


def test_aimd_headers_jitter_and_half_open_are_account_local(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock, circuit_failures=2, circuit_cooldown=10, retry_base=2, retry_cap=20)
    hooks = scheduler.hooks
    hooks.observe('one', False, 'rate_limit', rate_limits={'retry_at': 105})
    assert not hooks.rate_ready('one', 104)
    assert hooks.rate_ready('one', 105)
    assert hooks.retry_delay({'attempt': 3}, 'rate_limit') == 4
    assert hooks.retry_delay({'attempt': 1}, 'authentication') is None
    hooks.observe('one', False, 'timeout')
    assert not hooks.circuit_allows('one')
    clock[0] = 110
    assert hooks.circuit_allows('one')
    hooks.observe('one', True)
    assert hooks.circuit_allows('other')


def test_account_budget_reservations_are_atomic_and_survive_restart(tmp_path):
    clock = [100.0]
    config, left = configured(tmp_path, clock)
    config.accounts['one'].budget = {'tokens_per_day': 100}
    left = Scheduler.from_config(config, clock=lambda: clock[0])
    tasks = []
    for label in ('A', 'B'):
        tasks.append(left.store.create('Read ' + label, [label], {'tokens': 60}, 'one'))
        left.enqueue(tasks[-1]['id'])
    first = left.claim_next('one', 'left')
    right = Scheduler.from_config(config, clock=lambda: clock[0])
    assert right.claim_next('one', 'right') is None
    left.complete(first, usage={'input_tokens': 30, 'output_tokens': 20})
    assert right.claim_next('one', 'right') is None  # 50 used + 60 reserved > 100
    clock[0] += 86400
    assert right.claim_next('one', 'tomorrow')['id'] == tasks[1]['id']


def test_task_cap_unknown_usage_and_expiry_are_conservative(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock, lease_seconds=5)
    config.accounts['one'].budget = {'tokens_per_day': 20}
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0])
    task = scheduler.store.create('Read A', ['A'], {'tokens': 20}, 'one')
    scheduler.enqueue(task['id'])
    claim = scheduler.claim_next('one', 'worker')
    scheduler.complete(claim)  # absent usage must not make this execution free
    assert scheduler.store.get(task['id'])['usage']['tokens'] == 20
    second = scheduler.store.create('Read B', ['B'], {'tokens': 1}, 'one')
    scheduler.enqueue(second['id'])
    assert scheduler.claim_next('one', 'blocked') is None
    clock[0] += 86400
    retry = Scheduler.from_config(config, clock=lambda: clock[0])
    claim = retry.claim_next('one', 'crashed')
    clock[0] += 6
    recovered = Scheduler.from_config(config, clock=lambda: clock[0])
    assert recovered.claim_next('one', 'retry') is None  # expired attempt consumes task cap


def test_configured_concurrency_zero_and_invalid_options(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock)
    config.accounts['one'].limits = {'max_concurrent': 0}
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0])
    task = scheduler.store.create('Read', ['A'], {}, 'one')
    scheduler.enqueue(task['id'])
    # A recorded pool concurrency of zero does not pause swarm admission.
    assert scheduler.claim_next('one', 'not-paused')['id'] == task['id']
    config.swarm.retry_base = -1
    with pytest.raises(ValueError):
        config.validate()


def test_http_success_and_429_export_only_normalized_rate_metadata():
    response = io.BytesIO(json.dumps({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}).encode())
    response.headers = {'X-RateLimit-Remaining-Requests': '0', 'X-RateLimit-Reset-Requests': '1m2s',
                        'Set-Cookie': 'private-cookie', 'X-Private': 'private-path'}
    backend = Backend('example', 'https://models.example.com/v1', 'example-model', extra={'retries': 0})
    with patch.object(backend._opener, 'open', return_value=response), patch('time.time', return_value=100):
        result = backend.chat([])
    assert result['rate_limits']['requests_remaining'] == 0
    assert result['rate_limits']['requests_reset_at'] == 162
    assert 'private-' not in json.dumps(result)
    from urllib.error import HTTPError
    error = HTTPError('https://models.example.com', 429, 'private-message', {'Retry-After': '7'}, None)
    with patch.object(backend._opener, 'open', side_effect=error), patch('time.time', return_value=100):
        with pytest.raises(BackendError) as raised:
            backend.chat([])
    assert raised.value.rate_limits == {'retry_at': 107}


def test_explicit_scheduler_enforces_scope_and_completion_gates(tmp_path):
    clock = [100.0]
    config, _ = configured(tmp_path, clock)
    config.swarm.gates_enabled = True
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0])
    task = scheduler.store.create('Read source', ['A'], {}, 'one')
    scheduler.enqueue(task['id'])
    result = scheduler.run_next('one', lambda task: {'artifact_ref': 'A', 'content': 'Read source',
                                                   'checks': {'tests': False}})
    assert result['task']['status'] == 'failed'
    assert result['gate']['source'] == 'deterministic'
    first = scheduler.store.create('Read source', ['directory'], {}, 'one')
    second = scheduler.store.create('Read source', ['directory/file'], {}, 'one')
    scheduler.enqueue(first['id'])
    scheduler.enqueue(second['id'])
    assert scheduler.claim_next('one', 'first')['id'] == first['id']
    assert scheduler.claim_next('one', 'conflict') is None
    assert scheduler.store.get(second['id'])['attempt'] == 0


def test_session_uses_reference_credentials_and_records_output_gate(tmp_path, monkeypatch):
    from fake_backend import FakeBackend
    from tessera.loop import Session
    from test_jev_gates import configured as judge_config, reply_for
    monkeypatch.setenv('EXAMPLE_JUDGE_KEY', 'synthetic-key')
    config = judge_config(tmp_path)
    config.cwd = tmp_path
    def respond(request, timeout):
        return reply_for(json.loads(request.data))
    # Choose a tools route without relying on the choice fixture's direct preference.
    backend = FakeBackend(['Read result'])
    # Since 42b2737 the judge call goes through CancellableHTTP, not urllib.request.urlopen,
    # whenever a cancellation registry is present. Patching urlopen left the real opener in
    # place, so the request reached the network and the offline guard fired. Patch the
    # transport the current path actually uses, and keep the same fake reply.
    class FakeTransport:
        def __init__(self, *handlers):
            self.cancel_event = None
            self.response = None
            self.opener = SimpleNamespace(open=lambda request, timeout: respond(request, timeout))
        def cancel(self):
            pass
        def check_cancelled(self):
            pass
        def finish(self):
            pass
    with patch('tessera.jev.CancellableHTTP', FakeTransport):
        with Session(config, backend=backend, root=tmp_path / 'session') as session:
            session.run_turn('Read result')
            assert session.router.credential_ref == 'judge'
            assert any(record['point'] == 'output_gate' for record in session.router.decisions)


def test_retry_headers_reach_policy_without_sleep_and_success_clears_circuit(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock, retry_base=4, retry_cap=4)
    task = scheduler.store.create('Read A', ['A'], {}, 'one')
    scheduler.enqueue(task['id'])
    attempts = []
    def execute(claim):
        attempts.append(claim['attempt'])
        if len(attempts) == 1:
            raise BackendError('synthetic-private-error', 'rate_limit', rate_limits={'retry_at': 110})
        return {'artifact_ref': 'A', 'usage': {'total_tokens': 7}}
    assert scheduler.run_next('one', execute)['task']['status'] == 'queued'
    clock[0] = 103
    assert scheduler.run_next('one', execute) is None
    clock[0] = 110
    assert scheduler.run_next('one', execute)['task']['status'] == 'succeeded'
    assert attempts == [1, 2]


def test_response_buckets_reserve_admission_and_refresh_when_reset(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock)
    scheduler.hooks.observe('one', True, rate_limits={'requests_remaining': 1, 'requests_reset_at': 110,
                                                    'tokens_remaining': 20, 'tokens_reset_at': 110})
    for name in ('A', 'B'):
        task = scheduler.store.create('Read ' + name, [name], {'tokens': 15}, 'one')
        scheduler.enqueue(task['id'])
    assert scheduler.claim_next('one', 'first') is not None
    assert scheduler.claim_next('one', 'second') is None
    clock[0] = 110
    assert scheduler.claim_next('one', 'after-reset') is not None


def test_malformed_header_values_and_usage_cannot_leak_or_disable_bounds():
    from tessera.rate_limits import parse_rate_limits, usage_totals
    limits = parse_rate_limits({'anthropic-ratelimit-tokens-remaining': '0',
                                'anthropic-ratelimit-tokens-reset': '2026-10-15T00:00:00Z',
                                'Retry-After': 'nan', 'X-Private': 'synthetic-secret'}, now=100)
    assert set(limits) == {'tokens_remaining', 'tokens_reset_at'}
    assert parse_rate_limits({'Retry-After': '1e999'}, now=100) == {}
    assert usage_totals({'total_tokens': -1, 'input_tokens': 3, 'output_tokens': 4, 'debug': 'private'}) == {'tokens': 7}


def test_lifetime_account_tokens_and_task_overrun_are_enforced(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock)
    config.accounts['one'].budget = {'tokens': 20}
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0])
    task = scheduler.store.create('Read A', ['A'], {'tokens': 20}, 'one')
    scheduler.enqueue(task['id'])
    claim = scheduler.claim_next('one', 'worker')
    assert scheduler.complete(claim, usage={'total_tokens': 25})['status'] == 'failed'
    clock[0] += 86400
    other = scheduler.store.create('Read B', ['B'], {'tokens': 1}, 'one')
    scheduler.enqueue(other['id'])
    assert scheduler.claim_next('one', 'budget-blocked') is None


def test_hard_cooldown_does_not_restore_an_auth_failure_to_full_capacity(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock, circuit_cooldown=5)
    scheduler.hooks.observe('one', False, 'authentication')
    assert not scheduler.hooks.circuit_allows('one')
    clock[0] = 105
    assert scheduler.hooks.circuit_allows('one')


def test_exhausted_budget_retry_is_terminal_instead_of_queued_forever(tmp_path):
    clock = [100.0]
    config, scheduler = configured(tmp_path, clock)
    task = scheduler.store.create('Read', ['A'], {'tokens': 10}, 'one')
    scheduler.enqueue(task['id'])
    claim = scheduler.claim_next('one', 'worker')
    result = scheduler.fail(claim, 'timeout')
    # Terminal (no retry), but a conservative charge is not an observed overrun:
    # the real failure category survives instead of being relabeled quota.
    assert result['status'] == 'failed' and result['error_category'] == 'timeout'


def test_real_offline_scheduler_gate_success_and_denied_human(tmp_path):
    clock = [100.0]
    config, _ = configured(tmp_path, clock)
    config.swarm.gates_enabled = True
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0])
    task = scheduler.store.create('Read source', ['A'], {'tokens': 5}, 'one')
    scheduler.enqueue(task['id'])
    result = scheduler.run_next('one', lambda claim: {'artifact_ref': 'artifact-placeholder',
                                                      'artifacts': [{'content': 'Read source'}],
                                                      'checks': {'tests': True}, 'usage': {'total_tokens': 2}})
    assert result['task']['status'] == 'succeeded' and result['gate']['allowed']
    assert 'Read source' not in (tmp_path / 'tasks.decisions.jsonl').read_text()
    config.jev.human_gate = True
    calls = []
    scheduler = Scheduler.from_config(config, clock=lambda: clock[0], human=lambda gate: calls.append(gate))
    task = scheduler.store.create('Read source', ['B'], {}, 'one')
    scheduler.enqueue(task['id'])
    assert scheduler.run_next('one', lambda claim: pytest.fail('human denied')) is None
    assert len(calls) == 1 and scheduler.store.get(task['id'])['attempt'] == 0


def test_disabled_completion_and_failure_cannot_mutate_preexisting_claim(tmp_path):
    from tessera.tasks import TaskError
    store = TaskStore(tmp_path / 'tasks.jsonl', enabled=True)
    task = store.create('Read', ['A'], {}, 'one')
    claim = store.claim(task['id'], 'worker', lease_seconds=60)
    scheduler = Scheduler(store, ['one'], enabled=False)
    before = store.path.read_bytes()
    with pytest.raises(TaskError, match='disabled'):
        scheduler.complete(claim)
    with pytest.raises(TaskError, match='disabled'):
        scheduler.fail(claim, 'provider')
    assert store.path.read_bytes() == before
