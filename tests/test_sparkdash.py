"""SparkDash reads preserve source identity and keep authentication out of replies."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import subprocess
import pytest
from tessera import sparkdash
from tessera.config import InfrastructureConfig


@pytest.fixture(autouse=True)
def clear_cache():
    sparkdash._cache.clear()


def test_unconfigured_source_never_starts_a_command():
    with patch.object(subprocess, 'run') as run:
        assert sparkdash.snapshot(InfrastructureConfig()) == {'configured': False}
        run.assert_not_called()


def test_range_is_validated_before_command_execution():
    with pytest.raises(ValueError, match='range'):
        sparkdash.snapshot(InfrastructureConfig(), '../settings')


def test_snapshot_keeps_source_names_and_history_and_caches_reads():
    settings = InfrastructureConfig(sparkdash_command=['read-source'], sparkdash_url='https://metrics.example.com')
    source = {'sparks': [{'name': 'fixture-spark', 'metrics': {'llm': [{'modelId': 'Exact-Model-Name'}]}}],
              'tokens': {'day': [{'t': '2026-01-01', 'completionTokens': 42}]}}
    with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(source))) as run:
        report = sparkdash.snapshot(settings, '7d')
        assert report['sparks'] == source['sparks']
        assert report['tokens'] == source['tokens']
        assert report['source_url'] == settings.sparkdash_url
        assert sparkdash.snapshot(settings, '7d') is report
        assert run.call_count == 1
        assert run.call_args.args[0] == ['read-source', '7d']
        assert 'SPARKDASH_TOKEN' in run.call_args.kwargs['input']
        assert 'Authorization' not in json.dumps(report)


def test_bridge_failure_does_not_leak_command_diagnostics():
    settings = InfrastructureConfig(sparkdash_command=['read-source'])
    failure = subprocess.CalledProcessError(1, 'read-source', stderr='private credential')
    with patch.object(subprocess, 'run', side_effect=failure):
        report = sparkdash.snapshot(settings)
    assert report == {'configured': True, 'error': 'SparkDash read unavailable'}


def test_invalid_source_schema_is_unavailable():
    settings = InfrastructureConfig(sparkdash_command=['read-source'])
    with patch.object(subprocess, 'run', return_value=SimpleNamespace(stdout='{}')):
        assert sparkdash.snapshot(settings)['error'] == 'SparkDash read unavailable'
