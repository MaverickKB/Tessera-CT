"""Fault-injected acceptance errors must never become public evidence text."""

import json

import pytest

from demo import swarm_live_smoke as smoke
from test_swarm_live_smoke import echo_factory, private_home


@pytest.mark.parametrize('phase', ['prepare', 'artifact'])
def test_unknown_failure_text_never_reaches_stdout_or_saved_report(private_home, monkeypatch, capsys, phase):
    private = 'https://private.example.com/private-path-placeholder credential-canary-placeholder'

    def fail(*args, **kwargs):
        raise smoke.SmokeFailure(private)

    if phase == 'prepare':
        monkeypatch.setattr(smoke, 'prepare', fail)
    else:
        monkeypatch.setattr(smoke.SwarmJobs, 'artifact', fail)
    selected = []
    assert smoke.main(backend_factory=echo_factory(selected)) == 2
    serialized = capsys.readouterr().out
    report = json.loads(serialized)
    assert report['status'] == 'failed'
    assert report['reason'] == 'execution_failed'
    for value in (private, 'private.example.com', 'private-path-placeholder', 'credential-canary-placeholder'):
        assert value not in serialized
    if phase == 'prepare':
        assert selected == []
        assert report['execution'] == 'not_started'
        assert not (private_home / 'swarm-smoke-report.json').exists()
    else:
        assert report['execution'] == 'offline_fake'
        assert len(selected) == 2
        assert json.loads((private_home / 'swarm-smoke-report.json').read_text()) == report
