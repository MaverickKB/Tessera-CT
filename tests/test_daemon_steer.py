import threading
import time

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.daemon import Daemon


class Peer:
    def notify(self, *args):
        pass


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


def blocking_daemon(tmp_path, script_after):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    config.tools.approval = 'never'
    started, release = threading.Event(), threading.Event()

    def first(messages, tools):
        started.set()
        assert release.wait(10)
        return 'Hello'
    daemon = Daemon(config, backend_factory=lambda *args: FakeBackend([first, *script_after]))
    daemon.peer = Peer()
    return daemon, started, release


def agent_texts(daemon, slug):
    return [m.text for m in daemon.manager.get(slug).log.iter() if m.kind == 'agent']


def test_daemon_steer_reaches_running_turn_and_idle_steer_queues(tmp_path):
    daemon, started, release = blocking_daemon(tmp_path, ['Bonjour', 'Queued answer'])
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Steer'})['thread']['id']
        daemon.dispatch('turn/start', {'threadId': slug, 'input': [{'type': 'text', 'text': 'Say hello'}]})
        assert started.wait(10)
        result = daemon.dispatch('turn/steer', {'threadId': slug, 'text': 'In French please'})
        assert result['mode'] == 'steer' and result['turnId']
        release.set()
        assert wait_for(lambda: 'Bonjour' in agent_texts(daemon, slug))
        steers = [m.text for m in daemon.manager.get(slug).log.iter() if m.meta.get('steer')]
        assert steers == ['In French please']
        assert wait_for(lambda: not daemon.manager.runtime(slug).active)
        idle = daemon.dispatch('turn/steer', {'threadId': slug, 'text': 'Another question'})
        assert idle['mode'] == 'queued'
        assert wait_for(lambda: 'Queued answer' in agent_texts(daemon, slug))
    finally:
        release.set()
        daemon.close()


def test_daemon_interrupt_stops_running_turn(tmp_path):
    daemon, started, release = blocking_daemon(tmp_path, [])
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Stop'})['thread']['id']
        assert daemon.dispatch('turn/interrupt', {'threadId': slug}) == {'interrupted': False, 'turnId': None}
        daemon.dispatch('turn/start', {'threadId': slug, 'input': [{'type': 'text', 'text': 'Long job'}]})
        assert started.wait(10)
        result = daemon.dispatch('turn/interrupt', {'threadId': slug})
        assert result['interrupted'] is True
        release.set()
        assert wait_for(lambda: 'Interrupted by the user.' in agent_texts(daemon, slug))
    finally:
        release.set()
        daemon.close()
