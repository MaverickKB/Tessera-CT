"""Passive saved job projections expose finite counts, never execution selectors."""

import json
from unittest.mock import Mock

import pytest

from tessera.job_status import job_status
from tessera.jobs import SwarmJobs
from tessera.log import ChatLog
from test_swarm_jobs import configured, workers
from fake_backend import FakeBackend


def test_job_projection_is_passive_sanitized_and_bound_to_saved_state(tmp_path, monkeypatch):
    config = configured(tmp_path)
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' evidence']))
    with ChatLog(tmp_path / 'log') as log:
        job = jobs.create('private-objective-placeholder', workers(), session_slug='private-subject', log=log)
        jobs.run(job['id'], log=log)
    paths = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    denied = []
    for target in ('tessera.scheduler.Scheduler.from_config','tessera.credentials.CredentialResolver.resolve',
                   'tessera.jobs.SwarmJobs.get','tessera.tasks.TaskStore._load','subprocess.Popen',
                   'urllib.request.OpenerDirector.open'):
        spy = Mock(side_effect=AssertionError('Passive status cannot execute'))
        monkeypatch.setattr(target,spy)
        denied.append(spy)
    assert job_status(config) == {'enabled':True,'configured':True,'jobs':[
        {'ordinal':1,'status':'succeeded','worker_count':2,'completed_count':2,'failed_count':0,
         'artifact_count':2,'merge_status':'succeeded','gate_status':'allowed',
         'workers':[
             {'ordinal':1,'status':'succeeded','attempt':1,'provider_ordinal':1},
             {'ordinal':2,'status':'succeeded','attempt':1,'provider_ordinal':2},
         ]}]}
    for spy in denied:
        spy.assert_not_called()
    assert all(p.read_bytes() == content for p,content in paths.items())
    text = json.dumps(job_status(config))
    for value in ('private-',job['id'],str(tmp_path),'alpha','beta','swarm:'):
        assert value not in text


def test_an_over_limit_job_stays_listed_after_leaving_yolo(tmp_path):
    config = configured(tmp_path)
    config.swarm.distinct_workers = False
    config.tools.approval = 'yolo'
    jobs = SwarmJobs(config, backend_factory=lambda settings: FakeBackend([settings.name + ' evidence']))

    def fan(count, label):
        return [{
            'backend': 'alpha', 'objective': f'Inspect {label} {index}',
            'scope': [f'{label}-{index}'], 'budget': {'tokens': 1},
        } for index in range(count)]

    with ChatLog(tmp_path / 'log') as log:
        jobs.create('small-objective-placeholder', fan(2, 'small'), session_slug='private-subject', log=log)
        jobs.create('large-objective-placeholder', fan(9, 'large'), session_slug='private-subject', log=log)
    config.tools.approval = 'ask'
    report = job_status(config)
    assert len(report['jobs']) == 2
    by_count = {row['worker_count']: row for row in report['jobs']}
    assert 'over_limit' not in by_count[2]
    assert by_count[9]['over_limit'] is True
    assert 'swarm.max_job_tasks' in by_count[9]['limit_reason']
    assert '9' in by_count[9]['limit_reason'] and '8' in by_count[9]['limit_reason']
    text = json.dumps(report)
    for value in ('private-', 'small-objective', 'large-objective', 'alpha', str(tmp_path)):
        assert value not in text


def test_job_rows_are_bounded_and_say_how_many_the_bound_left_out(tmp_path):
    config = configured(tmp_path)
    config.status.task_limit = 1
    jobs = SwarmJobs(config, backend_factory=lambda s: FakeBackend([s.name + ' evidence']))
    with ChatLog(tmp_path / 'log') as log:
        for objective in ('first-objective-placeholder', 'second-objective-placeholder'):
            job = jobs.create(objective, workers(), session_slug='private-subject', log=log)
            jobs.run(job['id'], log=log)
    report = job_status(config)
    # task_limit is a bound, not a filter: two saved jobs, one row shown, and the
    # reader is told the difference instead of counting one row as the whole store.
    assert len(report['jobs']) == 1
    assert report['jobs_omitted'] == 1
    assert report['job_limit'] == 1
    # The disclosure names no objective, id, path or worker identity.
    text = json.dumps(report)
    for value in ('private-', 'first-objective', 'second-objective', str(tmp_path), 'alpha', 'beta', 'swarm:'):
        assert value not in text


@pytest.mark.parametrize('condition',['disabled','empty','corrupt','symlink','oversized'])
def test_job_projection_reports_unknown_or_inert_without_repairs(tmp_path, condition):
    config = configured(tmp_path)
    root = tmp_path / config.swarm.jobs_dir
    if condition == 'disabled':
        config.swarm.enabled = False
    elif condition == 'empty':
        config.backends.clear()
    else:
        root.mkdir()
        job = root / ('a' * 32)
        job.mkdir()
        if condition == 'corrupt':
            (job/'job.json').write_text('{"status": []}\n')
        elif condition == 'oversized':
            config.status.max_file_bytes=5
            (job/'job.json').write_text('too long\n')
        else:
            outside = tmp_path/'private-file'
            outside.write_text('private-placeholder\n')
            (job/'job.json').symlink_to(outside)
    before = {p:p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = job_status(config)
    assert 'jobs' not in result
    assert result['enabled'] is (condition != 'disabled')
    assert result['configured'] is (condition != 'empty')
    assert all(p.read_bytes() == content for p,content in before.items())
