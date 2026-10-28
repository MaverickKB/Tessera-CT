"""Session logging, HTTP delivery, reply guards and restart use synthetic workers."""
import json
import threading
from unittest.mock import patch

import pytest

from fake_backend import FakeBackend
from tessera.config import Config, load_config
from tessera.sessions import SessionManager
from tessera.server import create_server
from test_server import call


def config_for(tmp_path, **options):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.channels.enabled = True
    for key, value in options.items():
        setattr(config.channels, key, value)
    return config


def test_disabled_channels_never_register_tools_or_touch_work_store(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        manager.create('two')
        with pytest.raises(ValueError, match='disabled'):
            manager.send_to_session('one', 'two', 'Hello')
        schemas = manager.get('one').tools.schemas()
        assert 'send_to_session' not in json.dumps(schemas)
        assert not (tmp_path / 'channels.jsonl').exists()


def test_shared_delivery_logs_both_sides_without_loading_models(tmp_path):
    config = config_for(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError('Notification logging cannot construct a model or resolve credentials')
    with SessionManager(config, backend_factory=forbidden) as manager:
        for name in ('one', 'two', 'three'):
            manager.create(name)
        manager.create_channel('research')
        for name in ('one', 'two', 'three'):
            manager.subscribe_channel('research', name)
        with patch('tessera.credentials.CredentialResolver.resolve', side_effect=forbidden):
            result = manager.publish_channel('research', 'one', 'Inspect this result')
        assert result['recipients'] == ['two', 'three']
        assert manager._runtimes == {} and manager.channels.pending() == []
        for name in ('one', 'two', 'three'):
            rows = [json.loads(line) for path in (manager.path(name) / 'log').glob('*.jsonl') for line in path.read_text().splitlines()]
            assert len(rows) == 1 and rows[0]['kind'] == 'work'
            assert rows[0]['meta']['work_id'] == result['message_id']


def test_direct_reply_gates_acknowledgements_and_resumes_delivery_idempotently(tmp_path):
    config = config_for(tmp_path, auto_reply=True, auto_deliver=False)
    calls = []
    def factory(slug, backend):
        calls.append(slug)
        return FakeBackend(['Read result'])
    with SessionManager(config, backend_factory=factory) as manager:
        manager.create('one')
        manager.create('two')
        message = manager.direct_work('one', 'two', 'Inspect the source', message_id='stable-message')
        assert calls == []
        original = manager.channels.acknowledge
        failed = [False]
        def fail_ack(message_id, session, direction, **kwargs):
            if direction == 'incoming' and not failed[0]:
                failed[0] = True
                raise OSError('synthetic interruption after enqueue')
            return original(message_id, session, direction, **kwargs)
        with patch.object(manager.channels, 'acknowledge', side_effect=fail_ack):
            with pytest.raises(OSError):
                manager.deliver_work()
        manager.deliver_work()
        assert manager.wait('two', 5)
        runtime = manager.runtime('two')
        assert len(runtime.turns) == 1 and len(runtime.session.backend.calls) == 1
        assert sum(row.kind == 'work' for row in runtime.session.log.iter()) == 1
        manager.direct_work('one', 'two', 'Thanks, received.', message_id='ack-message')
        manager.deliver_work()
        assert len(runtime.turns) == 1  # deterministic acknowledgment never queues a reply
    with SessionManager(config, backend_factory=factory) as restored:
        assert restored.deliver_work()['delivered'] == 0


def test_handoff_carries_sender_view_and_brief_in_both_work_logs(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['A useful source result'])) as manager:
        manager.create('one')
        manager.create('two')
        manager.submit('one', 'Hello')
        assert manager.wait('one', 5)
        source = manager.get('one')
        assert source.compactor.wait_until_summarized(source.log.count() - 1, 5)
        result = manager.handoff('one', 'two', 'Review the source', task_ref='task-placeholder')
        assert result['hop'] == 1
        target_log = list((manager.path('two') / 'log').glob('*.jsonl'))[0].read_text()
        assert 'A useful source result' in target_log and 'Review the source' in target_log
        assert 'task-placeholder' in target_log
        assert 'two' not in manager._runtimes


def test_http_channels_direct_and_explicit_delivery_remain_config_gated(tmp_path):
    config = config_for(tmp_path)
    config.server.port = 0
    server = create_server(config, backend_factory=lambda *args: pytest.fail('No model reply configured'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name in ('one', 'two'):
            assert call(server, 'POST', '/api/sessions', {'title': name})[0] == 201
        assert call(server, 'POST', '/api/channels', {'name': 'research'})[0] == 201
        for name in ('one', 'two'):
            assert call(server, 'POST', '/api/channels/research/subscriptions', {'slug': name})[0] == 200
        code, message = call(server, 'POST', '/api/channels/research/messages', {'source': 'one', 'text': 'Read source'})
        assert code == 202 and message['message_id']
        code, direct = call(server, 'POST', '/api/sessions/two/work', {'source': 'one', 'text': 'A notification'})
        assert code == 202 and direct['message_id']
        assert call(server, 'POST', '/api/work/deliver', {}) == (200, {'delivered': 0})
        assert server.manager._runtimes == {}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_channels_settings_and_env_have_offline_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv('TESSERA_CHANNELS_ENABLED', 'true')
    monkeypatch.setenv('TESSERA_CHANNELS_MAX_HOPS', '2')
    config = load_config(tmp_path)
    assert config.channels.enabled and config.channels.max_hops == 2
    assert config.channels.auto_reply is False


def test_jev_reply_batch_and_deterministic_ack_guard(tmp_path, monkeypatch):
    import io
    from tessera.jev import JudgeKit
    monkeypatch.setenv('EXAMPLE_REPLY_KEY', 'fixture-key')
    kit = JudgeKit(enabled=True, model='judge-placeholder', url='https://judge.example.com/evaluate',
                   api_key_env='EXAMPLE_REPLY_KEY', decision_log=tmp_path / 'decisions.jsonl')
    with patch.object(kit._credentials, 'resolve', side_effect=AssertionError('deterministic first')):
        assert kit.message_gate('Thanks, received.', hop=1, max_hops=4)['needs_reply'] == 0
        assert kit.message_gate('Inspect source', hop=4, max_hops=4)['needs_reply'] == 0
    calls = []
    def respond(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        return io.BytesIO(json.dumps({'model': 'judge-placeholder', 'answers': {
            'needs_reply': {'type': 'noul', 'noul': .2}, 'looping': {'type': 'noul', 'noul': .1}}}).encode())
    with patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
        assert kit.message_gate('Source report', hop=1, max_hops=4)['needs_reply'] == .2
    assert len(calls) == 1 and set(calls[0]['questions']) == {'needs_reply', 'looping'}
    assert 'Source report' not in (tmp_path / 'decisions.jsonl').read_text()


def test_causal_work_cycle_is_blocked_through_real_session_tools(tmp_path):
    config = config_for(tmp_path, auto_reply=True)
    def send(identifier, target):
        return {'tool_calls': [{'id': identifier, 'name': 'send_to_session',
                               'arguments': {'slug': target, 'text': 'Inspect the result'}}]}
    backends = {'one': FakeBackend([send('first', 'two'), 'Sent first', send('cycle', 'two'), 'Stopped cycle']),
                'two': FakeBackend([send('reply', 'one'), 'Sent reply'])}
    with SessionManager(config, backend_factory=lambda slug, name: backends[slug]) as manager:
        manager.create('one')
        manager.create('two')
        manager.submit('one', 'Use send_to_session to inspect the result')
        # Shutdown waits across the full causal chain, including newly queued work.
    rows = [json.loads(line) for line in (tmp_path / 'channels.jsonl').read_text().splitlines()]
    messages = [row['message'] for row in rows if row['op'] == 'message']
    assert len(messages) == 2 and [message['hop'] for message in messages] == [1, 2]
    assert len(backends['one'].calls) == 4 and len(backends['two'].calls) == 2


def test_notification_log_remains_a_valid_session_when_opened_later(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['Read the notification'])) as manager:
        manager.create('one')
        manager.create('two')
        manager.direct_work('one', 'two', 'A source notification')
        assert manager._runtimes == {}
        manager.submit('two', 'Read our previous source notification')
        assert manager.wait('two', 5)
        assert 'A source notification' in manager.get('two').view.zoom(0, 1)


def test_pending_delivery_is_recovered_only_by_explicit_api_after_restart(tmp_path):
    config = config_for(tmp_path, auto_deliver=False)
    with SessionManager(config, backend_factory=lambda *args: pytest.fail('No reply configured')) as manager:
        manager.create('one')
        manager.create('two')
        manager.direct_work('one', 'two', 'Pending source')
    with SessionManager(config, backend_factory=lambda *args: pytest.fail('No reply configured')) as restored:
        assert restored._runtimes == {}
        assert not (restored.path('two') / 'log').exists()
        assert restored.deliver_work() == {'delivered': 2}
        assert restored.deliver_work() == {'delivered': 0}


def test_selected_env_values_are_scrubbed_before_work_journal_and_handoff(tmp_path, monkeypatch):
    config = config_for(tmp_path)
    config.credentials = {'provider': {'env': 'EXAMPLE_WORK_KEY'}}
    monkeypatch.setenv('EXAMPLE_WORK_KEY', 'fixture-secret')
    with SessionManager(config) as manager:
        manager.create('one')
        manager.create('two')
        manager.direct_work('one', 'two', 'Received fixture-secret')
        manager.handoff('one', 'two', 'Inspect fixture-secret', view='<chat>fixture-secret</chat>')
        for path in tmp_path.rglob('*.jsonl'):
            assert 'fixture-secret' not in path.read_text()


def test_disabling_channels_holds_preexisting_queued_work_without_starting_runtime(tmp_path):
    config = config_for(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        manager.create('two')
    queued = {'id': 'turn-placeholder', 'text': 'Inspect source', 'from_slug': 'one', 'hop': 1,
              'state': 'queued', 'created': 'fixture-time', 'work_id': 'work-placeholder'}
    path = tmp_path / 'sessions' / 'two' / 'turns.jsonl'
    path.write_text(json.dumps({'op': 'enqueue', 'turn': queued}) + '\n')
    before = path.read_bytes()
    config.channels.enabled = False
    with SessionManager(config, backend_factory=lambda *args: pytest.fail('Disabled work cannot construct backend')) as manager:
        assert manager._runtimes == {}
        assert path.read_bytes() == before


def test_completed_reply_with_lost_ack_recovers_without_reopening_provider(tmp_path):
    config = config_for(tmp_path, auto_reply=True, auto_deliver=False)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['Inspected source'])) as manager:
        manager.create('one')
        manager.create('two')
        manager.direct_work('one', 'two', 'Inspect source', message_id='stable-work')
        acknowledge = manager.channels.acknowledge
        def lose_ack(message_id, session, direction, **options):
            if direction == 'incoming':
                raise OSError('fixture lost acknowledgement')
            return acknowledge(message_id, session, direction, **options)
        with patch.object(manager.channels, 'acknowledge', side_effect=lose_ack):
            with pytest.raises(OSError):
                manager.deliver_work()
        assert manager.wait('two', 5)
    with SessionManager(config, backend_factory=lambda *args: pytest.fail('Completed work cannot reopen provider')) as restored:
        assert restored._runtimes == {}
        assert restored.deliver_work() == {'delivered': 1}
        assert restored._runtimes == {} and restored.channels.pending() == []


def test_notifications_do_not_bypass_reply_guards_via_an_active_turn(tmp_path):
    config = config_for(tmp_path)
    entered, release = threading.Event(), threading.Event()
    (tmp_path / 'fact.txt').write_text('Source fact')
    def blocked(messages, tools):
        entered.set()
        assert release.wait(5)
        return {'tool_calls': [{'id': 'read', 'name': 'read_file', 'arguments': {'path': 'fact.txt'}}]}
    backend = FakeBackend([blocked, 'Source fact read'])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create('one')
        manager.create('two')
        manager.submit('one', 'Read fact.txt')
        try:
            assert entered.wait(2)
            manager.direct_work('two', 'one', 'Unrequested guarded notification')
        finally:
            release.set()
        assert manager.wait('one', 5)
        assert 'Unrequested guarded notification' not in json.dumps(backend.calls[-1]['messages'])
        assert any('Unrequested guarded notification' in row.text for row in manager.get('one').log.iter())
        assert len(manager.runtime('one').turns) == 1
