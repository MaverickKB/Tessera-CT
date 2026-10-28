"""Offline consumers of a configured backend as the shared Jev judge."""

import json

import pytest

from fake_backend import FakeBackend
from tessera.backend import BackendError
from tessera.config import BackendConfig, Config
from tessera.jev import JudgeKit


def configured_judge(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.backends['fixture-judge'] = BackendConfig('fixture-judge', provider_kind='cli_worker')
    config.roles = {'main': 'fixture-judge', 'jev': 'fixture-judge'}
    config.jev.enabled = True
    config.jev.session_gates_enabled = True
    return config.validate()


def typed_answers(questions, *, complete=True, keep=.95):
    answers = {}
    for name, question in questions.items():
        kind = question['type']
        if kind == 'choice':
            options = list(question['criteria'])
            chosen = 'direct' if 'direct' in options else 'decision'
            answers[name] = {'type': kind, 'choice': chosen, 'confidence': 1,
                             'probabilities': {option: float(option == chosen) for option in options}}
        elif kind == 'score':
            score = len(question['criteria']) - 1 if complete else 0
            answers[name] = {'type': kind, 'score': score, 'confidence': 1,
                             'probabilities': {str(i): float(i == score) for i in range(len(question['criteria']))}}
        else:
            probability = .95 if name == 'claims_supported' else .05
            if name == 'keep':
                probability = keep
            answers[name] = {'type': kind, 'noul': probability}
    return answers


def fake_judge(monkeypatch, *, complete=True, keep=.95, malformed=None):
    def respond(messages, tools):
        assert tools is None
        packet = json.loads(messages[-1]['content'])
        assert set(packet) == {'state', 'questions'}
        answers = typed_answers(packet['questions'], complete=complete, keep=keep)
        if malformed in answers:
            answers[malformed] = {'type': 'noul', 'noul': 4}
        return {'content': json.dumps({'answers': answers, 'model': 'fixture-private-model-label'}),
                'finish_reason': 'stop'}
    backend = FakeBackend([respond] * 12)
    constructions = []
    def construct(*args):
        constructions.append(args)
        return backend
    monkeypatch.setattr('tessera.backend.Backend', construct)
    return backend, constructions


def no_typesafe(*args, **kwargs):
    raise AssertionError('Backend judgments must not contact TypeSafe or resolve its credentials')


def test_role_is_lazy_and_disabled_jev_never_constructs_or_resolves(tmp_path, monkeypatch):
    config = configured_judge(tmp_path)
    config.jev.enabled = False
    monkeypatch.setattr('tessera.backend.Backend', no_typesafe)
    monkeypatch.setattr('tessera.jev.CredentialResolver.resolve', no_typesafe)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', no_typesafe)
    kit = JudgeKit.from_config(config)
    assert kit.intake('Hello')['source'] == 'heuristic'


def test_backend_role_takes_precedence_without_typesafe_credentials(tmp_path, monkeypatch):
    config = configured_judge(tmp_path)
    config.jev.url = 'https://judge.example.com/evaluate'
    config.jev.model = 'fixture-unused-judge'
    config.jev.api_key_env = 'FIXTURE_UNUSED_JUDGE_KEY'
    backend, constructions = fake_judge(monkeypatch)
    monkeypatch.setattr('tessera.jev.CredentialResolver.resolve', no_typesafe)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', no_typesafe)
    kit = JudgeKit.from_config(config).bind_session(tmp_path, 'fixture-session')
    assert not constructions and not backend.calls
    assert kit.intake('fixture-private request')['source'] == 'jev'
    assert len(constructions) == len(backend.calls) == 1
    assert constructions[0][-1]['provider_kind'] == 'cli_worker'
    assert kit.intake('fixture-private request')['source'] == 'jev'
    assert len(backend.calls) == 1
    records = [json.loads(line) for line in (tmp_path / 'decisions.jsonl').read_text().splitlines()]
    assert len(records) == 6 and sum(row['cache_hit'] for row in records) == 3
    assert {row['transport'] for row in records} == {'backend'}
    assert 'fixture-private' not in json.dumps(records)


def test_bad_backend_answer_preserves_valid_siblings(tmp_path, monkeypatch):
    config = configured_judge(tmp_path)
    backend, _ = fake_judge(monkeypatch, malformed='needs_clarification')
    kit = JudgeKit.from_config(config)
    result = kit.intake('A precise fixture question')
    assert result['source'] == 'jev' and result['needs_clarification'] == .1
    assert {r['question_id']: r['source'] for r in kit.decisions} == {
        'route': 'jev', 'needs_memory': 'jev', 'needs_clarification': 'heuristic'}
    assert len(backend.calls) == 1


@pytest.mark.parametrize('response', [
    BackendError('fixture-private provider failure'),
    {'content': 'not JSON'},
    {'content': '[]'},
    {'content': '{"answers": []}'},
    {'content': '{}'},
    {'content': '{"answers": {}}', 'finish_reason': 'length'},
    {'content': '{"answers": {}}', 'finish_reason': 'content_filter'},
    {'content': '{"answers": {}}', 'tool_calls': [{'name': 'fixture-tool'}]},
])
def test_failed_backend_falls_back_without_http_or_private_error(tmp_path, monkeypatch, response):
    config = configured_judge(tmp_path)
    backend = FakeBackend([response])
    monkeypatch.setattr('tessera.backend.Backend', lambda *args: backend)
    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', no_typesafe)
    kit = JudgeKit.from_config(config).bind_session(tmp_path, 'fixture-session')
    assert kit.intake('Hello')['source'] == 'heuristic'
    assert len(backend.calls) == 1
    assert len(kit.decisions) == 3
    assert 'fixture-private' not in (tmp_path / 'decisions.jsonl').read_text()


def test_deterministic_failure_checks_precede_backend_and_human(tmp_path, monkeypatch):
    config = configured_judge(tmp_path)
    monkeypatch.setattr('tessera.backend.Backend', no_typesafe)
    kit = JudgeKit.from_config(config).bind_session(tmp_path, 'fixture-session')
    result = kit.output_gate('Read fixture', 'Done', checks={'fixture-check': False}, human=no_typesafe)
    assert result == {'allowed': False, 'requires_human': False, 'goal_met': 0, 'source': 'deterministic'}
    assert kit.decisions[-1]['point'] == 'output_gate'


@pytest.mark.parametrize('reason', ['length', 'content_filter', 'tool_calls'])
def test_incomplete_finish_with_valid_typed_answers_is_not_a_verdict(tmp_path, monkeypatch, reason):
    config = configured_judge(tmp_path)
    def respond(messages, tools):
        questions = json.loads(messages[-1]['content'])['questions']
        return {'content': json.dumps({'answers': typed_answers(questions)}),
                'finish_reason': reason, 'tool_calls': []}
    backend = FakeBackend([respond])
    monkeypatch.setattr('tessera.backend.Backend', lambda *args: backend)
    kit = JudgeKit.from_config(config)
    assert kit.intake('Hello')['source'] == 'heuristic'
    assert len(backend.calls) == 1


def test_passive_status_recognizes_backend_role_without_starting_it(tmp_path, monkeypatch):
    from tessera.passive_status import judge_status
    config = configured_judge(tmp_path)
    monkeypatch.setattr('tessera.backend.Backend', no_typesafe)
    monkeypatch.setattr('tessera.jev.CredentialResolver.resolve', no_typesafe)
    assert judge_status(config, [])['configured'] is True
    assert judge_status(config, [])['enabled'] is True
