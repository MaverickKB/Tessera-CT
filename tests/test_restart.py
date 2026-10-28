import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from fake_backend import FakeBackend
from tessera.sessions import SessionManager
from test_loop import session_config


def turn_states(root, slug):
    ids, states = {}, {}
    for line in (root / 'sessions' / slug / 'turns.jsonl').read_text().splitlines():
        record = json.loads(line)
        if record['op'] == 'enqueue':
            ids[record['turn']['text']] = record['turn']['id']
        else:
            states[record['id']] = (record['state'], record['error'])
    return {text: states.get(turn_id, ('queued', None)) for text, turn_id in ids.items()}


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_drain_finishes_the_running_turn_keeps_queued_turns_and_they_resume(tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow(messages, tools):
        started.set()
        assert release.wait(10)
        return 'First done.'

    backend = FakeBackend([slow])
    manager = SessionManager(session_config(tmp_path), backend_factory=lambda *args: backend)
    slug = manager.create(title='r')['slug']
    manager.submit(slug, 'first')
    assert started.wait(10)
    manager.submit(slug, 'second')
    result = {}
    drainer = threading.Thread(target=lambda: result.update(manager.drain(10)))
    drainer.start()
    time.sleep(0.2)
    assert drainer.is_alive(), 'drain waits for the running turn'
    try:
        manager.submit(slug, 'refused')
    except RuntimeError:
        pass
    else:
        raise AssertionError('new turns are refused while draining')
    release.set()
    drainer.join(10)
    assert result == {'drained': True, 'interrupted': []}
    manager.close()
    states = turn_states(tmp_path, slug)
    assert states['first'] == ('done', None)
    assert states['second'] == ('queued', None)
    resumed = FakeBackend(['Second done.'])
    with SessionManager(session_config(tmp_path), backend_factory=lambda *args: resumed) as again:
        runtime = again.runtime(slug)
        assert wait_for(lambda: any(m.text == 'Second done.' for m in runtime.session.log.iter()))
    assert turn_states(tmp_path, slug)['second'] == ('done', None)


def test_drain_timeout_interrupts_the_turn_cleanly_as_a_restart(tmp_path):
    started, cancelled = threading.Event(), threading.Event()

    class Slow:
        def chat(self, messages, tools=None, **kw):
            started.set()
            assert cancelled.wait(10)
            raise RuntimeError('killed')

        def cancel(self):
            cancelled.set()
            return True

    manager = SessionManager(session_config(tmp_path), backend_factory=lambda *args: Slow())
    slug = manager.create(title='r')['slug']
    manager.submit(slug, 'long')
    assert started.wait(10)
    assert manager.drain(0.2) == {'drained': True, 'interrupted': [slug]}
    log = list(manager.runtime(slug).session.log.iter())
    errors = [e['data'] for e in manager.runtime(slug).events.after(0) if e['event'] == 'error']
    manager.close()
    assert errors and all(e.get('at') for e in errors), 'errors carry a time so replays open no banner'
    assert log[-1].text == 'Interrupted by a core restart.'
    assert turn_states(tmp_path, slug)['long'] == ('error', 'Interrupted by a core restart')


def test_core_restart_keeps_steering_available_until_the_running_turn_finishes(tmp_path):
    from tessera.server import create_server
    from test_server import call
    started, release = threading.Event(), threading.Event()

    def slow(messages, tools):
        started.set()
        assert release.wait(10)
        return 'Finished before the restart.'

    def acknowledge(messages, tools):
        expected = '[Steer from the user during this turn] Pause for the restart'
        assert any(message.get('content') == expected for message in messages)
        return 'Paused for the restart.'

    config = session_config(tmp_path)
    config.server.port = 0
    config.server.supervised = True
    server = create_server(config, backend_factory=lambda *args: FakeBackend([slow, acknowledge]))
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    slug = server.manager.create(title='r')['slug']
    idle = server.manager.create(title='idle')['slug']
    server.manager.submit(slug, 'work')
    assert started.wait(10)
    assert call(server, 'POST', '/api/core/restart', {})[0] == 202
    time.sleep(0.2)
    try:
        assert call(server, 'GET', '/api/status')[0] == 200, 'still serving while draining'
        status, result = call(server, 'POST', f'/api/sessions/{slug}/steer', {'text': 'Pause for the restart'})
        assert status == 202
        assert result['mode'] == 'steer'
        assert call(server, 'POST', f'/api/sessions/{idle}/steer', {'text': 'Start new work'})[0] == 409
    finally:
        release.set()
        serving.join(10)
        server.server_close()
    assert not serving.is_alive()
    assert turn_states(tmp_path, slug)['work'] == ('done', None)


def test_supervised_restart_exits_when_a_model_ignores_interruption(tmp_path):
    script = '''
import sys, threading
from pathlib import Path
sys.path.insert(0, 'tests')
from fake_backend import FakeBackend
from local_memory import test_local_memory
from test_loop import session_config
import tessera.server as server_module

config = session_config(Path(sys.argv[1]))
config.server.port = 0
config.server.supervised = True
config.server.drain_seconds = .05
config.server.interrupt_grace_seconds = .05
entered = threading.Event()

def stuck(messages, tools):
    entered.set()
    threading.Event().wait()

original_create = server_module.create_server

def create(config):
    server = original_create(config, backend_factory=lambda *args: FakeBackend([stuck]))
    slug = server.manager.create('restart-recovery')['slug']
    server.manager.submit(slug, 'running')
    assert entered.wait(5)
    server.manager.submit(slug, 'queued')
    return server

server_module.create_server = create
with test_local_memory():
    server_module.serve(config)
'''
    process = subprocess.Popen(
        [sys.executable, '-c', script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert line.startswith('Serving Tessera at '), line
        url = line.strip().split()[-1]
        request = Request(url + '/api/core/restart', data=b'{}',
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=5) as response:
            assert response.status == 202
        output, errors = process.communicate(timeout=8)
        assert process.returncode == 0, errors
        assert 'handing core back to supervisor' in output
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    assert turn_states(tmp_path, 'restart-recovery')['queued'] == ('queued', None)
    config = session_config(tmp_path)
    config.server.resume_turns = False
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['Queued work recovered.'])) as manager:
        manager.runtime('restart-recovery')
        assert manager.wait('restart-recovery', timeout=5)
        assert turn_states(tmp_path, 'restart-recovery')['running'][0] == 'error'
        assert turn_states(tmp_path, 'restart-recovery')['queued'] == ('done', None)


def test_a_turn_that_asks_for_an_idle_restart_finishes_before_the_core_exits(tmp_path):
    """A restart has to recover the work that was in progress.

    The turn that asks for a restart must not kill itself. scripts/restart-core.sh
    POSTs /api/core/restart and then polls for a new pid, which means the calling
    turn sits in subprocess.run while drain waits for that same turn to finish:
    core exit needs drain, drain needs the turn, the turn needs the new pid. At
    drain_seconds the turn is interrupted with 'restart' and marked error.

    A turn that asks for an idle restart must finish its work first, and the core
    must exit after it, not kill it.
    """
    from tessera.server import create_server
    from test_server import call

    holder = {}

    def work(messages, tools):
        # Exactly what the script now sends when a turn is running.
        request = Request(holder['url'] + '/api/core/restart',
                          data=json.dumps({'when': 'idle'}).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=5) as response:
            assert response.status == 202
            holder['reply'] = json.loads(response.read())
        return 'Finished the work.'

    config = session_config(tmp_path)
    config.server.port = 0
    config.server.supervised = True
    config.server.drain_seconds = 0.2
    server = create_server(config, backend_factory=lambda *args: FakeBackend([work]))
    holder['url'] = server.url
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    slug = server.manager.create(title='r')['slug']
    server.manager.submit(slug, 'work')
    try:
        assert server.manager.wait(slug, timeout=10), 'the turn must finish, not be interrupted'
        serving.join(10)
    finally:
        if serving.is_alive():
            server.shutdown()
        server.server_close()
    state = turn_states(tmp_path, slug)['work']
    assert state == ('done', None), 'the requesting turn must not be killed: %r' % (state,)
    assert holder.get('reply', {}).get('deferred') is True, holder.get('reply')


def test_an_idle_restart_does_not_wait_for_queued_turns_that_a_drain_would_keep(tmp_path):
    """The idle wait must cover running turns only.

    A drain already refuses new turns, keeps queued ones, and resumes them on
    the next runtime (test_drain_finishes_the_running_turn_keeps_queued_turns_
    and_they_resume). So waiting for `pending` as well buys nothing and costs
    the full restart_stall_seconds: a session with a queue holds the reload
    open until the queue drains itself, then the stall bound falls through to
    drain and interrupts anyway. That is the card's open question about the
    1800s bound, and the answer is that the bound should not be reachable in
    the ordinary case.
    """
    from tessera.server import create_server

    started, release = threading.Event(), threading.Event()

    def slow(messages, tools):
        started.set()
        assert release.wait(10)
        return 'First done.'

    config = session_config(tmp_path)
    config.server.port = 0
    config.server.supervised = True
    config.server.drain_seconds = 0.2
    config.server.restart_stall_seconds = 30.0
    backend = FakeBackend([slow, 'Second done.'])
    server = create_server(config, backend_factory=lambda *args: backend)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    slug = server.manager.create(title='r')['slug']
    server.manager.submit(slug, 'first')
    assert started.wait(10)
    server.manager.submit(slug, 'second')

    # Ask from outside any turn, which is the case this predicate governs.
    request = Request(server.url + '/api/core/restart',
                      data=json.dumps({'when': 'idle'}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=5) as response:
        assert response.status == 202
    time.sleep(0.3)
    assert serving.is_alive(), 'the running turn must finish before the core exits'
    release.set()
    try:
        assert server.manager.wait(slug, timeout=10)
        serving.join(10)
    finally:
        if serving.is_alive():
            server.shutdown()
        server.server_close()

    states = turn_states(tmp_path, slug)
    assert states['first'] == ('done', None), states
    assert states['second'] == ('queued', None), (
        'a queued turn survives a drain; the idle wait must not run it: %r' % (states,))


def test_an_idle_restart_waits_for_a_long_turn_that_is_still_working(tmp_path):
    """The bound measures silence, not duration.

    Measured on this home: 110 of 663 turns ran longer than 30 minutes and 26
    longer than an hour. A wait bounded by turn duration therefore interrupts
    the very work it exists to protect, roughly one turn in six. What actually
    distinguishes a wedged turn is that it stops producing anything: of those
    663 turns only 8 ever went quiet for more than 30 minutes, and each was a
    single model call that returned nothing for hours.

    So a turn that keeps emitting events must survive a stall bound far shorter
    than its total run time.
    """
    from tessera.server import create_server

    calls = {'n': 0}
    finished = threading.Event()

    def working(messages, tools):
        # Every hop takes longer than the stall bound, but each hop also writes
        # a durable event, so the turn is never silent for the bound.
        calls['n'] += 1
        time.sleep(0.4)
        if calls['n'] <= 4:
            return {'tool_calls': [{'id': 'c%d' % calls['n'], 'name': 'write_file',
                                    'arguments': {'path': 'hop%d.txt' % calls['n'], 'text': 'x'}}]}
        finished.set()
        return 'Long but productive.'

    config = session_config(tmp_path)
    config.server.port = 0
    config.server.supervised = True
    config.server.drain_seconds = 0.2
    config.server.restart_stall_seconds = 0.8
    config.server.max_hops = 6
    backend = FakeBackend([working] * 8)
    server = create_server(config, backend_factory=lambda *args: backend)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    slug = server.manager.create(title='r')['slug']
    server.manager.submit(slug, 'work for a while')

    # Let the turn get past the stall bound while still working.
    assert wait_for(lambda: calls['n'] >= 2, timeout=10)

    request = Request(server.url + '/api/core/restart',
                      data=json.dumps({'when': 'idle'}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=5) as response:
        assert response.status == 202

    # The turn's total run time exceeds the stall bound many times over; the
    # core must still be serving and the turn must still be running.
    time.sleep(1.2)
    assert serving.is_alive(), 'a working turn must not be interrupted by the stall bound'
    assert not finished.is_set(), 'the turn was cut short'

    try:
        assert server.manager.wait(slug, timeout=20)
        serving.join(10)
    finally:
        if serving.is_alive():
            server.shutdown()
        server.server_close()

    states = turn_states(tmp_path, slug)
    assert states['work for a while'] == ('done', None), states


def test_an_idle_restart_gives_up_on_a_turn_that_has_gone_silent(tmp_path):
    """The stall bound still has teeth: a wedged turn must not block the reload.

    Without this the wait would be unbounded and a hung model call would hold a
    restart open forever, which is worse than interrupting one turn.
    """
    from tessera.server import create_server

    started, cancelled = threading.Event(), threading.Event()

    class Wedged:
        def chat(self, messages, tools=None, **kw):
            started.set()
            # Longer than the stall bound and emits nothing: the turn is silent.
            assert cancelled.wait(10)
            raise RuntimeError('killed')

        def cancel(self):
            cancelled.set()
            return True

    config = session_config(tmp_path)
    config.server.port = 0
    config.server.supervised = True
    config.server.drain_seconds = 0.2
    config.server.restart_stall_seconds = 0.5
    server = create_server(config, backend_factory=lambda *args: Wedged())
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    slug = server.manager.create(title='r')['slug']
    server.manager.submit(slug, 'hung')
    assert started.wait(10)

    request = Request(server.url + '/api/core/restart',
                      data=json.dumps({'when': 'idle'}).encode(),
                      headers={'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=5) as response:
        assert response.status == 202

    try:
        serving.join(10)
        assert not serving.is_alive(), 'a silent turn must not block the reload forever'
    finally:
        if serving.is_alive():
            server.shutdown()
        server.server_close()

    states = turn_states(tmp_path, slug)
    assert states['hung'] == ('error', 'Interrupted by a core restart'), states
