"""Per-append recovery uses fake memory only, never an external store."""

from dataclasses import asdict
import json

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.daemon import Daemon
from tessera.log import ChatLog
from tessera.mirror import AppendMirror


class Memory:
    def __init__(self):
        self.calls = []
        self.writes = {}
        self.fail = False

    def write_layer(self, project, request_id, layer):
        self.calls.append((project, request_id, layer))
        self.writes[request_id] = layer
        if self.fail:
            raise OSError('private-diagnostic-fixture')
        return {'ok': True}


def settings(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.project = 'project-placeholder'
    config.aethermind.personal_project = 'personal-placeholder'
    return config


def test_disabled_mirror_never_opens_an_outbox_or_constructs_a_client(tmp_path):
    config = Config(home=tmp_path)
    def forbidden():
        raise AssertionError('Disabled mirror cannot construct an integration')
    mirror = AppendMirror(config, client_factory=forbidden)
    mirror.record('subject', 'session', 'chat', {'i': 0})
    assert mirror.sync()['write_debt'] == 0
    assert list(tmp_path.iterdir()) == []


def test_enabled_mirror_without_endpoints_queues_and_reports_the_boundary(tmp_path):
    config = settings(tmp_path)
    mirror = AppendMirror(config)
    with ChatLog(tmp_path / 'log') as log:
        mirror.attach('subject', 'session', log)
        log.append('user', 'Offline append remains durable')
    status = mirror.status()
    assert status['enabled'] is True and status['configured'] is False
    assert status['write_debt'] == 1 and status['notice']
    assert mirror.sync()['write_debt'] == 1


def test_personal_target_never_receives_a_mirror_write(tmp_path):
    config = settings(tmp_path)
    config.aethermind.project = config.aethermind.personal_project
    memory = Memory()
    mirror = AppendMirror(config, client_factory=lambda: memory)
    mirror.record('subject', 'session', 'chat', {'i': 0, 'text': 'Read-only target'})
    assert mirror.sync()['write_debt'] == 1
    assert memory.calls == []


def test_selected_credentials_are_redacted_before_json_serialization(tmp_path, monkeypatch):
    from tessera.credentials import CredentialReference
    config = settings(tmp_path)
    config.credentials['fixture'] = CredentialReference(env='MIRROR_KEY_PLACEHOLDER')
    monkeypatch.setenv('MIRROR_KEY_PLACEHOLDER', 'secret"fixture\\value')
    memory = Memory()
    mirror = AppendMirror(config, client_factory=lambda: memory)
    mirror.record('subject', 'session', 'event', {'id': 1, 'echo': 'secret"fixture\\value'})
    envelope = json.loads(memory.writes['session-event-1']['body'])
    assert envelope['record']['echo'] == '[REDACTED]'


def test_each_daemon_chat_and_journal_append_is_mirrored(tmp_path):
    config = settings(tmp_path)
    memory = Memory()
    mirror = AppendMirror(config, client_factory=lambda: memory)
    daemon = Daemon(config, mirror=mirror, backend_factory=lambda *args: FakeBackend(['Fixture answer']))
    class Peer:
        def notify(self, *args):
            pass
    daemon.peer = Peer()
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Mirror subject'})['thread']['id']
        daemon.dispatch('turn/start', {'threadId': slug, 'input': [{'type': 'text', 'text': 'Hello'}]})
        assert daemon.manager.wait(slug, 5)
        runtime = daemon.manager.runtime(slug)
        session_id = daemon.manager._metadata[slug]['id']
        for message in runtime.session.log.iter():
            key = session_id + '-chat-' + str(message.i)
            assert json.loads(memory.writes[key]['body']) == {'source': 'chat', 'record': asdict(message)}
        for event in runtime.events.after():
            key = session_id + '-event-' + str(event['id'])
            assert json.loads(memory.writes[key]['body']) == {'source': 'event', 'record': event}
        assert mirror.status()['write_debt'] == 0
        assert {call[0] for call in memory.calls} == {'project-placeholder'}
    finally:
        daemon.close()


def test_lost_ack_offline_backoff_replay_and_stable_payload(tmp_path):
    config = settings(tmp_path)
    memory = Memory()
    memory.fail = True
    mirror = AppendMirror(config, client_factory=lambda: memory, clock=lambda: 100)
    with ChatLog(tmp_path / 'log') as log:
        mirror.attach('subject', 'session', log)
        for index in range(4):
            log.append('note', 'Fixture ' + str(index))
        assert mirror.status()['write_debt'] == 4
        assert len(memory.calls) == 1  # outage cannot cost a timeout per appended message
        original = memory.calls[0]
    memory.fail = False
    reopened = AppendMirror(config, client_factory=lambda: memory)
    with ChatLog(tmp_path / 'log') as log:
        reopened.attach('subject', 'session', log)
        assert reopened.sync()['write_debt'] == 0
        assert memory.calls[1] == original
        assert len(memory.writes) == 4
    count = len(memory.calls)
    acknowledged = AppendMirror(config, client_factory=lambda: memory)
    with ChatLog(tmp_path / 'log') as log:
        acknowledged.attach('subject', 'session', log)
    assert len(memory.calls) == count
    for path in (tmp_path / 'mirror').rglob('*.jsonl'):
        assert 'private-diagnostic-fixture' not in path.read_text()


def test_reattach_recovers_log_committed_before_mirror_callback_and_fences_old_target(tmp_path):
    config = settings(tmp_path)
    memory = Memory()
    with ChatLog(tmp_path / 'log') as log:
        log.append('work', 'Committed before attaching a mirror')
    memory.fail = True
    mirror = AppendMirror(config, client_factory=lambda: memory)
    with ChatLog(tmp_path / 'log') as log:
        mirror.attach('subject', 'session', log)
    assert mirror.status()['write_debt'] == 1
    config.aethermind.project = 'changed-project-placeholder'
    memory.fail = False
    memory.calls.clear()
    restored = AppendMirror(config, client_factory=lambda: memory)
    with ChatLog(tmp_path / 'log') as log:
        restored.attach('subject', 'session', log)
        restored.sync()
        assert memory.calls == []
        assert restored.status()['write_debt'] == 1
        log.append('note', 'New destination explicitly configured')
    assert len(memory.calls) == 1 and memory.calls[0][0] == 'changed-project-placeholder'
    assert restored.status()['write_debt'] == 1


def test_standalone_voice_and_work_writers_are_observed_without_loading_a_backend(tmp_path, monkeypatch):
    from test_voice import mint_fake
    config = settings(tmp_path)
    config.channels.enabled = True
    config.voice.enabled = True
    config.voice.websocket_url = 'wss://voice.example.com/realtime'
    config.voice.model = 'model-placeholder'
    config.voice.api_base_url = 'https://voice.example.com'
    config.voice.client_secret_path = '/mint-placeholder'
    config.voice.client_protocol_prefix = 'fixture.'
    config.voice.api_key_env = 'EXAMPLE_VOICE_KEY'
    memory = Memory()
    mirror = AppendMirror(config, client_factory=lambda: memory)
    daemon = Daemon(config, mirror=mirror)
    try:
        first = daemon.dispatch('thread/start', {'title': 'First'})['thread']['id']
        second = daemon.dispatch('thread/start', {'title': 'Second'})['thread']['id']
        daemon.manager.direct_work(first, second, 'Standalone work fixture')
        mint_fake(monkeypatch, daemon.manager)
        lease = daemon.dispatch('voice/start', {'threadId': second})
        # A spoken line is what the speaker said. The provider's own transcript is its
        # echo and is never recorded, so the mirrored text is the user line.
        daemon.dispatch('voice/events', {'threadId': second, 'voiceId': lease['voice_id'],
            'events': [{'type': 'conversation.item.input_audio_transcription.completed',
                        'item_id': 'voice-fixture', 'transcript': 'Standalone voice fixture'}]})
        daemon.dispatch('voice/stop', {'threadId': second, 'voiceId': lease['voice_id']})
        assert daemon.manager._runtimes == {}
        bodies = [json.loads(row['body']) for row in memory.writes.values()]
        texts = [row['record']['text'] for row in bodies if row['source'] == 'chat']
        assert 'Standalone voice fixture' in texts
        assert any('Standalone work fixture' in text for text in texts)
    finally:
        daemon.close()


def test_swarm_protocol_reuses_gated_artifacts_and_mirrors_parent_log(tmp_path):
    from test_swarm_jobs import configured, workers
    config = configured(tmp_path)
    config.aethermind.project = 'project-placeholder'
    memory = Memory()
    mirror = AppendMirror(config, client_factory=lambda: memory)
    daemon = Daemon(config, mirror=mirror,
                    swarm_backend_factory=lambda setting: FakeBackend([setting.name + ' finding']))
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Swarm'})['thread']['id']
        job = daemon.dispatch('swarm/create', {'threadId': slug, 'objective': 'Merge fixture findings',
                                              'workers': workers()})['job']
        result = daemon.dispatch('swarm/run', {'jobId': job['id']})['job']
        assert result['status'] == 'succeeded' and len(result['artifacts']) == 2
        assert result['gate']['allowed'] is True and result['merged']
        assert daemon.dispatch('swarm/show', {'jobId': job['id']})['job']['status'] == 'succeeded'
        assert daemon.dispatch('board/read', {})['enabled'] is True
        history = daemon.dispatch('thread/read', {'threadId': slug})['messages']
        assert history and len(memory.writes) == len(history)
        assert mirror.status()['write_debt'] == 0
    finally:
        daemon.close()
