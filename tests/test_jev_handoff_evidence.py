"""A bounded worker excerpt cannot erase an earlier deterministic failure."""

import json
from types import SimpleNamespace

import pytest

from fake_backend import FakeBackend
from tessera.agents import AgentManager
from tessera.config import Config
from tessera.jev_review import AdvisoryReviewer
from tessera.log import ChatLog
from tessera.loop import Session


def test_failure_before_sixty_four_successes_survives_the_raw_tail(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        log.append('echo', 'blocked', {'success': False, 'blocked': True})
        for _ in range(64):
            log.append('echo', 'a successful but long tool response', {'success': True})
        outputs, complete = AgentManager._handoff_tool_evidence(log, 16)
    assert complete
    assert len(outputs) == 65
    assert all(row['success'] is True and len(row['result']) <= 16 for row in outputs[:64])
    summary = outputs[-1]
    assert summary['name'] == 'worker_tool_failure_summary' and summary['success'] is False
    assert summary['result'] == {'failed_tool_count': 1, 'blocked_tool_count': 1,
                                 'observed_tool_count': 65, 'evidence_complete': True}
    settings = Config().jev
    settings.handoff_review_enabled = True
    router = SimpleNamespace(configured=lambda: True, _batch=lambda *args, **kwargs: {})
    report = AdvisoryReviewer(router, settings).review('handoff', 'Complete the work.',
                                                      reply='Everything passed.', tool_outputs=outputs)
    assert len(report['facts']) == 1
    assert report['facts'][0]['tools_failed'] is True


@pytest.mark.parametrize('failed', [False, True])
def test_read_error_marks_scope_unknown_and_keeps_observed_failure(failed):
    def rows():
        yield SimpleNamespace(kind='echo', text='observed result', meta={'success': not failed})
        raise OSError('private-error-detail')
    outputs, complete = AgentManager._handoff_tool_evidence(SimpleNamespace(iter=rows), 100)
    assert complete is False
    summaries = [row for row in outputs if row['name'] == 'worker_tool_failure_summary']
    assert len(summaries) == int(failed)
    if failed:
        assert summaries[0]['result']['evidence_complete'] is False
        assert summaries[0]['result']['failed_tool_count'] == 1
        assert summaries[0]['result']['observed_tool_count'] == 1
    assert 'private-error-detail' not in json.dumps(outputs)


def test_failed_exit_code_is_counted_even_when_its_metadata_says_success(tmp_path):
    with ChatLog(tmp_path / 'log') as log:
        log.append('echo', '{"exit_code":2}', {'success': True})
        outputs, complete = AgentManager._handoff_tool_evidence(log, 100)
    assert complete and outputs[-1]['result']['failed_tool_count'] == 1
    assert outputs[0]['success'] is True  # Preserve the original record.


def test_disabled_handoff_never_reads_worker_tool_evidence(tmp_path, monkeypatch):
    config = Config(home=tmp_path, cwd=tmp_path)
    monkeypatch.setattr(AgentManager, '_handoff_tool_evidence',
                        lambda *args: pytest.fail('A disabled review read worker evidence'))
    with Session(config, backend=FakeBackend([]),
                 agent_backend_factory=lambda: FakeBackend(['Finished.'])) as parent:
        parent.agents.spawn('reader', 'Inspect the file.', ['read_file'])
        assert parent.agents.wait(timeout=5)
        report = next(row for row in parent.log.iter() if row.kind == 'work')
        assert report.meta['success'] is True


def test_real_handoff_keeps_original_success_and_aggregate_failure(tmp_path, monkeypatch):
    captured = []

    class Reviewer:
        def __init__(self, router, settings):
            self.settings = settings

        def begin_turn(self):
            pass

        def enabled(self, checkpoint):
            return checkpoint == 'handoff' and self.settings.handoff_review_enabled

        def review(self, checkpoint, request, **evidence):
            captured.append(evidence)
            return {'checkpoint': checkpoint, 'advisory': True, 'state': 'reviewed'}

    monkeypatch.setattr('tessera.jev_review.AdvisoryReviewer', Reviewer)
    (tmp_path / 'present.txt').write_text('Present.')
    calls = [{'id': str(index), 'name': 'read_file',
              'arguments': {'path': 'missing.txt' if index == 0 else 'present.txt'}}
             for index in range(65)]
    worker = FakeBackend([{'tool_calls': calls}, 'Finished inspecting.'])
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.handoff_review_enabled = True
    with Session(config, backend=FakeBackend([]), agent_backend_factory=lambda: worker) as parent:
        parent.agents.spawn('reader', 'Inspect the files.', ['read_file'])
        assert parent.agents.wait(timeout=5)
        report = next(row for row in parent.log.iter() if row.kind == 'work')
        assert report.meta['success'] is True
        outputs = captured[-1]['tool_outputs']
        assert outputs[0]['name'] == 'worker_report' and outputs[0]['success'] is True
        assert outputs[-1]['name'] == 'worker_tool_failure_summary'
        assert outputs[-1]['result']['failed_tool_count'] == 1
        assert outputs[-1]['result']['observed_tool_count'] == 65
        assert outputs[-1]['result']['evidence_complete'] is True
        assert len([row for row in outputs if row['name'] == 'worker_tool_output']) == 64
