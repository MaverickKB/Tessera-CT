"""Offline daemon consumers of durable guarded work delivery and readback."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fake_backend import FakeBackend
from tessera.channels import ChannelError
from tessera.config import Config
from tessera.daemon import Daemon


@pytest.fixture(autouse=True)
def no_external_work(monkeypatch):
    guards = []
    for target in ('urllib.request.urlopen', 'urllib.request.OpenerDirector.open',
                   'tessera.credentials.CredentialResolver.resolve', 'subprocess.Popen'):
        guard = Mock(side_effect=AssertionError('Daemon A2A contracts must stay offline'))
        monkeypatch.setattr(target, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()


def daemon_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.channels.enabled = True
    config.channels.auto_deliver = False
    config.channels.auto_reply = False
    return config


def attach(config, factory):
    daemon = Daemon(config, backend_factory=factory)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    return daemon


def history(daemon, slug):
    return daemon.dispatch('thread/read', {'threadId': slug})['messages']


def source_bytes(root):
    return {p.name: p.read_bytes() for p in (root / 'log').glob('*.jsonl')}


def test_disabled_daemon_coordination_never_loads_workers_or_creates_outboxes(tmp_path):
    factory = Mock(side_effect=AssertionError('Disabled coordination cannot load workers'))
    daemon = attach(Config(home=tmp_path, cwd=tmp_path), factory)
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Single fixture'})['thread']['id']
        before = history(daemon, slug)
        with pytest.raises(ValueError, match='disabled'):
            daemon.dispatch('swarm/create', {'threadId': slug, 'objective': 'Join fixtures', 'workers': []})
        with pytest.raises(ChannelError, match='disabled'):
            daemon.manager.direct_work(slug, 'other-fixture', 'Fixture report')
        assert history(daemon, slug) == before == []
        assert not (tmp_path / daemon.config.channels.journal_file).exists()
        assert not (tmp_path / daemon.config.swarm.jobs_dir).exists()
        factory.assert_not_called()
    finally:
        daemon.close()


def test_daemon_reopen_repairs_lost_ack_once_and_resumes_exact_work_history(tmp_path, monkeypatch):
    config = daemon_config(tmp_path)
    backend = FakeBackend([])
    factory = Mock(return_value=backend)
    daemon = attach(config, factory)
    try:
        for title in ('alpha', 'beta'):
            daemon.dispatch('thread/start', {'title': title})
        result = daemon.manager.direct_work('alpha', 'beta', 'Synthetic artifact is ready.',
                                            message_id='fixture-stable-message')
        assert result['recipients'] == ['beta']
        assert history(daemon, 'alpha') == history(daemon, 'beta') == []
        acknowledge = daemon.manager.channels.acknowledge
        def interrupted(message_id, session, direction, **options):
            if direction == 'incoming':
                raise OSError('Fixture crash after source append')
            return acknowledge(message_id, session, direction, **options)
        monkeypatch.setattr(daemon.manager.channels, 'acknowledge', interrupted)
        with pytest.raises(OSError, match='Fixture crash'):
            daemon.manager.deliver_work()
        initial = {slug: history(daemon, slug) for slug in ('alpha', 'beta')}
        for slug, direction in (('alpha', 'outgoing'), ('beta', 'incoming')):
            assert len(initial[slug]) == 1 and initial[slug][0]['kind'] == 'work'
            assert initial[slug][0]['meta']['work_id'] == 'fixture-stable-message'
            assert initial[slug][0]['meta']['direction'] == direction
        pending = daemon.manager.channels.pending()
        assert [(row['session'], row['direction']) for row in pending] == [('beta', 'incoming')]
        roots = {slug: daemon.manager.path(slug) for slug in initial}
        saved = {slug: source_bytes(root) for slug, root in roots.items()}
        factory.assert_not_called()
    finally:
        daemon.close()
    reopened = attach(config, factory)
    try:
        manager = reopened._manager()
        assert manager.deliver_work() == {'delivered': 1}
        assert manager.deliver_work() == {'delivered': 0}
        assert manager.channels.pending() == []
        channel_bytes = manager.channels.path.read_bytes()
        retry = manager.direct_work('alpha', 'beta', 'Synthetic artifact is ready.',
                                    message_id='fixture-stable-message')
        assert retry == result and manager.channels.path.read_bytes() == channel_bytes
        with pytest.raises(ChannelError, match='different request'):
            manager.direct_work('alpha', 'beta', 'Conflicting fixture content.',
                                message_id='fixture-stable-message')
        assert manager.channels.path.read_bytes() == channel_bytes
        for slug in initial:
            assert reopened.dispatch('thread/resume', {'threadId': slug})['messages'] == initial[slug]
            assert source_bytes(roots[slug]) == saved[slug]
            assert reopened.dispatch('view/zoom', {'threadId': slug, 'start': 0})['text'] == initial[slug][0]['text']
        assert backend.calls == []
    finally:
        reopened.close()


def test_daemon_causal_reply_hop_limit_is_durable_and_rejection_appends_nothing(tmp_path):
    config = daemon_config(tmp_path)
    config.channels.max_hops = 2
    config.channels.block_cycles = False
    factory = Mock(side_effect=AssertionError('Notifications must not start a reply worker'))
    daemon = attach(config, factory)
    try:
        for title in ('alpha', 'beta'):
            daemon.dispatch('thread/start', {'title': title})
        first = daemon.manager.direct_work('alpha', 'beta', 'Synthetic artifact A.', message_id='fixture-first')
        second = daemon.manager.direct_work('beta', 'alpha', 'Synthetic acknowledgment.',
                                            parent_id=first['message_id'], message_id='fixture-reply')
        assert first['hop'] == 1 and second['hop'] == 2
        assert daemon.manager.deliver_work() == {'delivered': 4}
        path = daemon.manager.channels.path
        before = path.read_bytes()
    finally:
        daemon.close()
    reopened = attach(config, factory)
    try:
        manager = reopened._manager()
        with pytest.raises(ChannelError, match='hop'):
            manager.direct_work('alpha', 'beta', 'Synthetic over-budget reply.',
                                parent_id=second['message_id'], message_id='fixture-rejected')
        assert path.read_bytes() == before
        assert manager.channels.pending() == []
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert sum(r['op'] == 'message' for r in records) == 2
        assert sum(r['op'] == 'ack' for r in records) == 4
        assert len(history(reopened, 'alpha')) == len(history(reopened, 'beta')) == 2
        factory.assert_not_called()
    finally:
        reopened.close()
