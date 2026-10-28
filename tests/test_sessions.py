import json
import re
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.sessions import SessionManager


def config_for(root):
    config = Config(home=root, cwd=root)
    config.jev.enabled = False
    config.channels.enabled = True
    config.channels.auto_reply = True
    return config


def test_named_sessions_are_unique_lazy_and_survive_restart(tmp_path):
    config = config_for(tmp_path)
    created = []
    def factory(slug, backend):
        created.append(slug)
        return FakeBackend(['First answer', 'Second answer'])
    with SessionManager(config, backend_factory=factory) as manager:
        first = manager.create('Planning notes')
        second = manager.create('Planning notes')
        assert first['slug'] == 'planning-notes'
        assert second['slug'] == 'planning-notes-2'
        assert created == []
        turn = manager.submit(first['slug'], 'Hello')
        assert manager.wait(first['slug'], timeout=5)
        assert manager.turn(first['slug'], turn)['state'] == 'done'
        session = manager.get(first['slug'])
        assert [m.text for m in session.log.iter()] == ['Hello', 'First answer']
        manager.update(first['slug'], title='Renamed', archived=True)
        with pytest.raises(ValueError, match='archived'):
            manager.submit(first['slug'], 'Blocked')
    with SessionManager(config, backend_factory=factory) as restored:
        rows = restored.list_sessions()
        saved = next(row for row in rows if row['slug'] == 'planning-notes')
        assert rows[0]['updated'] >= saved['updated']
        assert saved['title'] == 'Renamed'
        assert saved['messages'] == 2
        restored.update(first['slug'], archived=False)
        restored.submit(first['slug'], 'Continue')
        assert restored.wait(first['slug'], 5)
        messages = list(restored.get(first['slug']).log.iter())
        assert [m.text for m in messages[:2]] == ['Hello', 'First answer']
        captures = restored.get(first['slug']).capture.debt()
        assert len(captures) == 2
        assert re.fullmatch(r'.+-t1-[0-9a-f]{32}', captures[0]['request_id'])
        assert re.fullmatch(r'.+-t2-[0-9a-f]{32}', captures[1]['request_id'])


def test_queue_serializes_a_session_but_other_sessions_can_run(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def block(messages, tools):
        entered.set()
        assert release.wait(5)
        return {'content': 'Slow finished'}
    backends = {'slow': FakeBackend([block, 'Queued finished']), 'fast': FakeBackend(['Independent'])}
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backends[slug]) as manager:
        manager.create('slow')
        manager.create('fast')
        try:
            first = manager.submit('slow', 'Hello')
            assert entered.wait(2)
            second = manager.submit('slow', 'Thanks')
            manager.submit('fast', 'Hello')
            assert manager.wait('fast', 2)
            assert manager.turn('slow', first)['state'] == 'running'
            assert manager.turn('slow', second)['state'] == 'queued'
        finally:
            release.set()
        assert manager.wait('slow', 5)
        assert [m.text for m in manager.get('slow').log.iter()] == ['Hello', 'Slow finished', 'Thanks', 'Queued finished']


def test_cross_session_delivery_and_hop_guard(tmp_path):
    source = FakeBackend([{'tool_calls': [{'id': 'send', 'name': 'send_to_session', 'arguments': {'slug': 'target', 'text': 'Inspect this result'}}]}, 'Sent.'])
    target = FakeBackend(['Received the result.'])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: source if slug == 'source' else target) as manager:
        manager.create('source')
        manager.create('target')
        manager.submit('source', 'Use tools to send the result')
        assert manager.wait('source', 5)
        assert manager.wait('target', 5)
        received = list(manager.get('target').log.iter())
        assert received[0].kind == 'work'
        assert received[0].text == '[source] Inspect this result'
        assert received[0].meta['hop'] == 1
        assert received[-1].text == 'Received the result.'
        with pytest.raises(ValueError, match='hop'):
            manager.send_to_session('source', 'target', 'Too far', hop=manager.config.server.max_hops)


def test_unsafe_slugs_and_parallel_managers_are_rejected(tmp_path):
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend([])) as manager:
        with pytest.raises(RuntimeError, match='locked'):
            SessionManager(config_for(tmp_path))
        for slug in ('../outside', '/tmp/outside', 'UPPER', 'a/b', '..'):
            with pytest.raises(ValueError):
                manager.get(slug)


def test_restart_resumes_only_not_started_turns_without_client_activity(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = '''
import json,sys,threading,time
from pathlib import Path
sys.path.insert(0, "tests")
from fake_backend import FakeBackend
from local_memory import test_local_memory
from tessera.config import Config
from tessera.sessions import SessionManager
config=Config(home=Path(sys.argv[1]),cwd=Path(sys.argv[1]))
config.jev.enabled=False
entered=threading.Event()
def blocked(messages,tools):
    entered.set()
    time.sleep(60)
    return {"content":"Should never complete"}
with test_local_memory(), SessionManager(config,backend_factory=lambda slug,name: FakeBackend([blocked])) as manager:
    manager.create("recovery")
    first=manager.submit("recovery","Hello")
    assert entered.wait(5)
    second=manager.submit("recovery","Continue")
    print(json.dumps([first,second]),flush=True)
    threading.Event().wait()
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first, second = json.loads(process.stdout.readline())
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    started = threading.Event()
    def factory(slug, backend):
        started.set()
        return FakeBackend(['Recovered queued answer'])
    with SessionManager(config_for(tmp_path), backend_factory=factory) as manager:
        assert started.wait(2), 'Pending turns did not resume until a client loaded the session'
        assert manager.wait('recovery', 5)
        assert manager.turn('recovery', first)['state'] == 'error'
        assert manager.turn('recovery', second)['state'] == 'done'
        assert [m.text for m in manager.get('recovery').log.iter()] == ['Hello', 'Continue', 'Recovered queued answer']


def test_unknown_recipient_is_tool_feedback_and_model_can_recover(tmp_path):
    backend = FakeBackend([
        {'tool_calls': [{'id': 'x', 'name': 'send_to_session', 'arguments': {'slug': 'missing', 'text': 'Hello'}}]},
        'The recipient does not exist; I will keep the result here.',
    ])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        manager.create('source')
        turn = manager.submit('source', 'Use send_to_session to deliver a result')
        assert manager.wait('source', 5)
        assert manager.turn('source', turn)['state'] == 'done'
        assert len(backend.calls) == 2
        assert any(m.kind == 'echo' and not m.meta['success'] for m in manager.get('source').log.iter())


def test_shutdown_waits_for_inflight_delivery_before_stopping_worker(tmp_path, monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    backend = FakeBackend(['Delivery completed'])
    errors = []
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        manager.create('source')
        manager.create('target')
        target = manager.get('target')
        append = target.log.append
        def paused_append(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return append(*args, **kwargs)
        monkeypatch.setattr(target.log, 'append', paused_append)
        def send():
            try:
                manager.send_to_session('source', 'target', 'Hello')
            except Exception as error:
                errors.append(error)
        def close():
            manager.close()
            closed.set()
        sender = threading.Thread(target=send)
        sender.start()
        assert entered.wait(2)
        closer = threading.Thread(target=close)
        closer.start()
        try:
            assert not closed.wait(.1), 'Shutdown ignored a delivery already being committed'
        finally:
            release.set()
            sender.join(5)
            closer.join(5)
        assert closed.is_set() and not errors
        assert len(backend.calls) == 1


@pytest.mark.parametrize('failed_state', ['running', 'done'])
def test_storage_failure_unblocks_wait_and_shutdown(tmp_path, monkeypatch, failed_state):
    import tessera.sessions as module
    manager = SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend(['Answer']))
    manager.create('storage')
    runtime = manager.runtime('storage')
    append = module.append_record
    def fail_state(path, record):
        if record.get('op') == 'state' and record.get('state') == failed_state:
            raise OSError('Injected disk failure')
        return append(path, record)
    monkeypatch.setattr(module, 'append_record', fail_state)
    try:
        manager.submit('storage', 'Hello')
        with pytest.raises(RuntimeError, match='storage'):
            manager.wait('storage', 1)
        assert manager.status()['sessions']['storage']['error']
    finally:
        # Allow the pre-fix dead-worker case to release its log during RED.
        if not runtime.thread.is_alive():
            with runtime.condition:
                runtime.active = None
                runtime.pending.clear()
                runtime.condition.notify_all()
        manager.close()


def test_selected_backend_handles_direct_route_over_global_fast_role(tmp_path):
    from fake_backend import fake_http_server
    from tessera.config import BackendConfig
    first_reply = {'choices': [{'message': {'content': 'Global fast answer'}}]}
    selected_reply = {'choices': [{'message': {'content': 'Selected backend answer'}}]}
    with fake_http_server([(200, first_reply)]) as (first_url, first_calls), fake_http_server([(200, selected_reply)]) as (second_url, second_calls):
        config = config_for(tmp_path)
        config.backends = {'first': BackendConfig('first', first_url, 'first'), 'second': BackendConfig('second', second_url, 'second')}
        config.roles = {'main': 'first', 'fast': 'first'}
        with SessionManager(config) as manager:
            manager.create('selected', backend='second')
            manager.submit('selected', 'Hello')
            assert manager.wait('selected', 5)
            assert list(manager.get('selected').log.iter())[-1].text == 'Selected backend answer'
        assert not first_calls
        assert len(second_calls) == 1


def test_shutdown_rejects_a_new_lazy_runtime(tmp_path, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend([])) as manager:
        manager.create('loaded')
        manager.create('lazy')
        runtime = manager.runtime('loaded')
        idle = runtime.idle
        def paused_idle():
            entered.set()
            assert release.wait(5)
            return idle()
        monkeypatch.setattr(runtime, 'idle', paused_idle)
        def close():
            manager.close()
            finished.set()
        closer = threading.Thread(target=close)
        closer.start()
        assert entered.wait(2)
        try:
            with pytest.raises(RuntimeError, match='stopping'):
                manager.get('lazy')
        finally:
            release.set()
            closer.join(5)
        assert finished.is_set()
        assert runtime.session.log._closed


def test_route_events_include_the_effective_tool_override(tmp_path):
    backend = FakeBackend([
        {'tool_calls': [{'id': 'z', 'name': 'zoom', 'arguments': {'start': 0, 'count': 1}}]},
        'Recovered the message.',
    ])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        manager.create('routing')
        manager.submit('routing', 'Zoom message zero')
        assert manager.wait('routing', 5)
        routes = [record['data'] for record in manager.runtime('routing').events.after() if record['event'] == 'route']
        assert routes[-1]['route'] == 'tools'
        assert routes[-1]['override'] == 'Explicit registered tool reference'


def test_submit_while_running_queues_and_does_not_steer(tmp_path):
    import time
    started, release = threading.Event(), threading.Event()

    def blocking(messages, tools):
        started.set()
        assert release.wait(10)
        return 'Still on the first ask'

    backend = FakeBackend([blocking, 'Second ask, after the first finished'])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        slug = manager.create('queue')['slug']
        first = manager.submit(slug, 'Keep learning')
        assert started.wait(10)
        second = manager.submit(slug, 'Next prompt, do not interrupt')
        assert second != first
        assert manager.turn(slug, second)['state'] == 'queued'
        steers = [m for m in manager.get(slug).log.iter() if m.meta.get('steer')]
        assert steers == []
        release.set()
        assert manager.wait(slug, 10)
        texts = [m.text for m in manager.get(slug).log.iter() if m.kind == 'agent']
        assert texts == ['Still on the first ask', 'Second ask, after the first finished']
        users = [m.text for m in manager.get(slug).log.iter() if m.kind == 'user']
        assert users == ['Keep learning', 'Next prompt, do not interrupt']


def test_queued_prompt_can_be_removed_before_it_starts(tmp_path):
    started, release = threading.Event(), threading.Event()

    def blocking(messages, tools):
        started.set()
        assert release.wait(10)
        return 'First ask finished'

    backend = FakeBackend([blocking, 'This should not run'])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        slug = manager.create('queue-remove')['slug']
        manager.submit(slug, 'Keep going')
        assert started.wait(10)
        waiting = manager.submit(slug, 'Changed my mind')
        shown = manager.queued_turns(slug)
        assert [item['id'] for item in shown] == [waiting]
        assert shown[0]['text'] == 'Changed my mind'
        assert manager.cancel_queued(slug, waiting) == {'removed': waiting}
        assert manager.queued_turns(slug) == []
        assert manager.turn(slug, waiting)['state'] == 'cancelled'
        release.set()
        assert manager.wait(slug, 10)
        texts = [m.text for m in manager.get(slug).log.iter() if m.kind == 'user']
        assert texts == ['Keep going']


def test_queued_prompt_can_be_revised_before_it_starts(tmp_path):
    started, release = threading.Event(), threading.Event()

    def blocking(messages, tools):
        started.set()
        assert release.wait(10)
        return 'First ask finished'

    backend = FakeBackend([blocking, 'Second ask, revised'])
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: backend) as manager:
        slug = manager.create('queue-revise')['slug']
        manager.submit(slug, 'Keep going')
        assert started.wait(10)
        waiting = manager.submit(slug, 'Original wording')
        assert manager.revise_queued(slug, waiting, 'Revised wording')['text'] == 'Revised wording'
        assert manager.queued_turns(slug)[0]['text'] == 'Revised wording'
        release.set()
        assert manager.wait(slug, 10)
        texts = [m.text for m in manager.get(slug).log.iter() if m.kind == 'user']
        assert texts == ['Keep going', 'Revised wording']


def test_default_session_preserves_configured_fast_role(tmp_path):
    from fake_backend import fake_http_server
    from tessera.config import BackendConfig
    response = lambda text: {'choices': [{'message': {'content': text}}]}
    with fake_http_server([(200, response('Main answer'))]) as (main_url, main_calls), fake_http_server([(200, response('Fast answer'))]) as (fast_url, fast_calls):
        config = config_for(tmp_path)
        config.backends = {'main': BackendConfig('main', main_url, 'main'), 'fast': BackendConfig('fast', fast_url, 'fast')}
        config.roles = {'main': 'main', 'fast': 'fast'}
        with SessionManager(config) as manager:
            manager.create('default')
            manager.submit('default', 'Hello')
            assert manager.wait('default', 5)
            assert list(manager.get('default').log.iter())[-1].text == 'Fast answer'
        assert not main_calls and len(fast_calls) == 1


def test_session_pinned_to_removed_backend_falls_back_to_main_with_notice(tmp_path):
    from fake_backend import fake_http_server
    from tessera.config import BackendConfig
    reply = {'choices': [{'message': {'content': 'Main answer'}}]}
    with fake_http_server([(200, reply)]) as (url, calls):
        config = config_for(tmp_path)
        config.backends = {'main': BackendConfig('main', url, 'm'), 'old': BackendConfig('old', url, 'o')}
        config.roles = {'main': 'main'}
        with SessionManager(config) as manager:
            manager.create('pinned', backend='old')
        config.backends = {'main': BackendConfig('main', url, 'm')}
        with SessionManager(config) as manager:
            manager.submit('pinned', 'Hello')
            assert manager.wait('pinned', 5)
            texts = [m.text for m in manager.get('pinned').log.iter()]
            assert any('no longer configured' in t for t in texts) and texts[-1] == 'Main answer'
        assert calls and calls[0]['body']['model'] == 'm'


def test_one_unreadable_session_does_not_take_down_the_list(tmp_path, capsys):
    """One unreadable session does not take down the list.

    A float `updated` raises TypeError inside the sort. The list has to name
    that session and keep returning the others, instead of answering a bare
    TypeError that empties the sidebar.
    """
    config = config_for(tmp_path)
    with SessionManager(config) as manager:
        manager.create('healthy')
        manager.create('second')

    broken = tmp_path / 'sessions' / 'hand-edited'
    broken.mkdir(parents=True)
    (broken / 'meta.json').write_text(json.dumps({
        'id': 'x', 'slug': 'hand-edited', 'title': 'Hand edited',
        'created': 1792814907.7, 'updated': 1792814907.7,   # float, not ISO
        'messages': 0, 'archived': False, 'backend': 'main', 'backend_override': True,
        'debt': 0, 'workspace': '',
    }))

    with SessionManager(config) as manager:
        rows = manager.list_sessions()          # must not raise
        slugs = [row['slug'] for row in rows]
        assert 'healthy' in slugs and 'second' in slugs
        assert 'hand-edited' not in slugs
        # The record and the field are named, so the file can be found and fixed.
        printed = capsys.readouterr().out
        assert 'hand-edited' in printed
        assert 'created' in printed or 'updated' in printed
        assert 'not an ISO timestamp' in printed
        # A print is not a payload. The status a client actually reads must name
        # the same record, or the sidebar shows a shorter list with no reason,
        # which reads as "that is all there is".
        unreadable = manager.status()['sessions_unreadable']
        assert [row['slug'] for row in unreadable] == ['hand-edited']
        assert unreadable[0]['field'] in ('created', 'updated')
        assert 'not an ISO timestamp' in unreadable[0]['reason']

    # A meta missing a required key is refused the same way, naming that key.
    missing = tmp_path / 'sessions' / 'missing-key'
    missing.mkdir(parents=True)
    (missing / 'meta.json').write_text(json.dumps({
        'id': 'y', 'slug': 'missing-key', 'title': 'Missing key',
        'created': '2026-10-24T12:00:00+00:00', 'updated': '2026-10-24T12:00:00+00:00',
        'messages': 0, 'archived': False, 'backend_override': True,
    }))
    with SessionManager(config) as manager:
        slugs = [row['slug'] for row in manager.list_sessions()]
        assert 'healthy' in slugs and 'missing-key' not in slugs
        printed = capsys.readouterr().out
        assert 'missing-key' in printed and 'backend' in printed
        unreadable = manager.status()['sessions_unreadable']
        # Both broken records are still on disk, so both are named. The healthy
        # ones are not, which is the point: the list is shorter than the folder,
        # and the payload says why.
        by_slug = {row['slug']: row for row in unreadable}
        assert set(by_slug) == {'hand-edited', 'missing-key'}
        assert by_slug['missing-key']['field'] == 'backend'
