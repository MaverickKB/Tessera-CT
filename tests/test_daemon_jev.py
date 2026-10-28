"""Real daemon turn/gate lifecycle with fake semantic and main backends only."""

import json
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from tessera.daemon import Daemon
from test_jev_backend import configured_judge, fake_judge, no_typesafe


def test_daemon_status_does_not_start_judge_or_resolve_its_credentials(tmp_path, monkeypatch):
    config = configured_judge(tmp_path)
    monkeypatch.setattr('tessera.backend.Backend', no_typesafe)
    monkeypatch.setattr('tessera.jev.CredentialResolver.resolve', no_typesafe)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', no_typesafe)
    daemon = Daemon(config)
    try:
        report = daemon.dispatch('status', {})
        assert report['jev']['configured'] is True and report['jev']['enabled'] is True
        assert report['jev']['decision_count'] == 0
        assert not list(tmp_path.rglob('decisions.jsonl'))
    finally:
        daemon.close()


@pytest.mark.parametrize('complete,keep,expected_state', [(True, .95, 'done'), (False, .1, 'error')])
def test_daemon_semantic_gate_and_memory_verdict_are_durable(tmp_path, monkeypatch, complete, keep, expected_state):
    config = configured_judge(tmp_path)
    judge, _ = fake_judge(monkeypatch, complete=complete, keep=keep)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', no_typesafe)
    main = FakeBackend(['The synthetic launch code is forty-two.'])
    daemon = Daemon(config, backend_factory=lambda *args: main)
    daemon.peer = SimpleNamespace(notify=lambda *args: None)
    try:
        slug = daemon.dispatch('thread/start', {'title': 'Fixture decision'})['thread']['id']
        turn = daemon.dispatch('turn/start', {'threadId': slug, 'input': [
            {'type': 'text', 'text': 'We agreed the synthetic launch code is forty-two. State that decision.'}]})['turn']
        assert daemon.manager.wait(slug, timeout=5)
        assert daemon.manager.turn(slug, turn['id'])['state'] == expected_state
        history = daemon.dispatch('thread/read', {'threadId': slug})['messages']
        metadata = history[-1]['meta']
        assert metadata['verification']['source'] == 'jev'
        assert metadata['output_gate']['allowed'] is complete
        runtime = daemon.manager.runtime(slug)
        assert runtime.session.capture.judgekit is runtime.session.router
        debt = runtime.session.capture.debt()
        assert len(debt) == 1  # Low keep never discards source capture.
        assert 'Decision: decision' in debt[0]['layer']['body']
        assert 'Memory judgment' not in debt[0]['layer']['body']
        root = daemon.manager.path(slug)
        records = [json.loads(line) for line in (root / 'decisions.jsonl').read_text().splitlines()]
        assert {r['point'] for r in records} >= {'intake', 'verification', 'output_gate', 'memory'}
        memory = {r['question_id']: r for r in records if r['point'] == 'memory'}
        assert memory['keep']['noul'] == keep and memory['keep']['source'] == 'jev'
        assert memory['layer_type']['choice'] == 'decision'
        assert all(r.get('transport') == 'backend' for r in records if r['point'] != 'output_gate')
        assert len(judge.calls) == 3  # intake, verification, memory; no duplicate verification.
        assert len(main.calls) == 1
        before = (root / 'decisions.jsonl').read_bytes()
        source_before = {p.name: p.read_bytes() for p in (root / 'log').glob('*.jsonl')}
    finally:
        daemon.close()
    restarted = Daemon(config, backend_factory=lambda *args: FakeBackend([]))
    restarted.peer = SimpleNamespace(notify=lambda *args: None)
    try:
        assert restarted.dispatch('thread/resume', {'threadId': slug})['messages'] == history
        assert (root / 'decisions.jsonl').read_bytes() == before
        assert {p.name: p.read_bytes() for p in (root / 'log').glob('*.jsonl')} == source_before
    finally:
        restarted.close()
