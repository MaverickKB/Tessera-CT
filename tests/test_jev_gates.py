"""Deterministic-first semantic gates with no accounts or external services."""
import io
import json
from unittest.mock import patch

from tessera.config import Config
from tessera.jev import JudgeKit


def configured(tmp_path):
    config = Config(home=tmp_path)
    config.jev.enabled = True
    config.jev.url = 'https://judge.example.com/evaluate'
    config.jev.model = 'example-judge'
    config.jev.credential_ref = 'judge'
    config.credentials = {'judge': {'env': 'EXAMPLE_JUDGE_KEY'}}
    return config


def reply_for(body):
    answers = {}
    for name, q in body['questions'].items():
        if q['type'] == 'score':
            answers[name] = {'type': 'score', 'score': 2, 'probabilities': {'0': 0, '1': 0, '2': 1}}
        elif q['type'] == 'choice':
            options = list(q['criteria'])
            answers[name] = {'type': 'choice', 'choice': options[0], 'probabilities': dict.fromkeys(options, 0)}
            answers[name]['probabilities'][options[0]] = 1
        else:
            yes = name.startswith('independent') or name.endswith(('claims_supported', 'evidence_complete'))
            answers[name] = {'type': 'noul', 'noul': .95 if yes else .05}
    return io.BytesIO(json.dumps({'model': 'example-judge', 'answers': answers}).encode())


def test_deterministic_conflict_and_failed_checks_cannot_be_overruled(tmp_path):
    kit = JudgeKit(enabled=False, decision_log=tmp_path / 'decisions.jsonl')
    with patch('tessera.jev.urllib.request.urlopen', side_effect=AssertionError('no semantics before checks')):
        gate = kit.swarm_gates('Build', tasks=[{'scope': ['src']}, {'scope': ['src/../src/main.py']}])
        assert not gate['allowed'] and gate['merge_conflict'] == 1 and gate['source'] == 'deterministic'
        gate = kit.output_gate('Run tests', 'Done', [{'exit_code': 1}])
        assert not gate['allowed'] and gate['goal_met'] == 0
    assert 'Build' not in (tmp_path / 'decisions.jsonl').read_text()


def test_one_swarm_batch_then_optional_human_and_credential_ref(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_JUDGE_KEY', 'synthetic-key')
    kit = JudgeKit.from_config(configured(tmp_path), human_gate=True)
    calls = []
    def respond(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        assert request.get_header('Authorization') == 'Bearer synthetic-key'
        return reply_for(body)
    human = []
    def approve(gate):
        human.append(gate)
        return True
    with patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
        gate = kit.swarm_gates('Build', tasks=[{'scope': ['A']}, {'scope': ['B']}],
                               artifacts=[{'content': 'actual artifact'}], checks={'tests': True}, human=approve)
    assert gate['allowed'] and gate['goal_met'] == 1 and not gate['requires_human']
    assert len(calls) == len(human) == 1
    assert set(calls[0]['questions']) == {'independent_0_1', 'merge_conflict', 'goal_met', 'looping', 'escalate_to_human'}
    assert 'synthetic-key' not in json.dumps(kit.decisions)


def test_looping_and_missing_human_are_safe_offline():
    kit = JudgeKit(enabled=False, loop_window=3)
    gate = kit.swarm_gates('Read', steps=['same', 'same', 'same'])
    assert gate['looping'] > .5 and gate['escalate_to_human'] > .5 and not gate['allowed']
    kit = JudgeKit(enabled=False, human_gate=True)
    gate = kit.swarm_gates('Read', checks={'tests': True}, artifacts=[{'content': 'artifact'}])
    assert gate['requires_human'] and not gate['allowed']


def test_memory_duplicate_batch_and_output_batch_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_JUDGE_KEY', 'synthetic-key')
    kit = JudgeKit.from_config(configured(tmp_path))
    bodies = []
    def respond(request, timeout):
        body = json.loads(request.data)
        bodies.append(body)
        return reply_for(body)
    with patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
        memory = kit.memory('Remember the decision', existing_layers=['Remember the decision'])
        results = kit.verify_outputs([{'request': 'Read', 'reply': 'Read result', 'tool_outputs': [{'success': True}]},
                                      {'request': 'Test', 'reply': 'Done', 'tool_outputs': [{'exit_code': 1}]}])
    assert 'duplicate_of_existing_layer' in memory
    assert len(bodies) == 2 and len(bodies[0]['questions']) == 4
    assert results[1]['allowed'] is False  # no judge can reverse a failed deterministic check
    assert results[0]['completion'] == 1
    offline = JudgeKit(enabled=False)
    assert offline.memory('Remember the decision', existing_layers=['Remember the decision'])['duplicate_of_existing_layer'] > .5


def test_disabled_jev_never_resolves_configured_credentials(tmp_path):
    config = configured(tmp_path)
    config.jev.enabled = False
    kit = JudgeKit.from_config(config)
    with patch('tessera.credentials.CredentialResolver.resolve', side_effect=AssertionError('disabled')), \
         patch('tessera.jev.urllib.request.urlopen', side_effect=AssertionError('offline')):
        assert kit.swarm_gates('Read')['source'] == 'heuristic'


def test_calibration_report_uses_exact_question_outcomes():
    from tessera.jev import calibration_report
    records = [{'batch_id': 'a', 'question_id': 'keep', 'noul': .8, 'source': 'jev'},
               {'batch_id': 'b', 'question_id': 'keep', 'noul': .6, 'source': 'heuristic'}]
    report = calibration_report(records, {('a', 'keep'): True, ('b', 'keep'): False})
    assert abs(report['keep']['brier'] - .2) < 1e-8
    assert report['keep']['precision'] == .5 and report['keep']['n'] == 2


def test_capture_uses_duplicate_candidates_without_dropping_turn_evidence(tmp_path):
    from tessera.capture import Capture
    kit = JudgeKit(enabled=False)
    capture = Capture(None, tmp_path / 'outbox', 'project-placeholder', judgekit=kit)
    for _ in range(2):
        capture.turn_start({'message': 'Remember this decision'})
        capture.turn_end({'reply': 'Understood', 'success': True})
    layers = capture.debt()
    assert len(layers) == 2
    assert 'Duplicate of an existing layer.' in layers[1]['layer']['body']
    assert 'duplicate_of_existing_layer' not in layers[1]['layer']['body']
    duplicate = [row for row in kit.decisions if row['question_id'] == 'duplicate_of_existing_layer']
    assert duplicate[-1]['noul'] > .5


def test_configured_gate_requires_actual_artifact_contents_not_only_reports():
    kit = JudgeKit(enabled=False)
    gate = kit.swarm_gates('Read source', artifacts=[{'artifact_ref': 'A'}], checks={'tests': True})
    assert gate['goal_met'] < kit.goal_threshold and not gate['allowed']


def test_missing_evidence_cannot_be_semantically_declared_complete(tmp_path, monkeypatch):
    monkeypatch.setenv('EXAMPLE_JUDGE_KEY', 'synthetic-key')
    kit = JudgeKit.from_config(configured(tmp_path))
    with patch('tessera.jev.urllib.request.urlopen', side_effect=lambda req, timeout: reply_for(json.loads(req.data))):
        gate = kit.swarm_gates('Build', checks={'tests': True}, artifacts=[{'artifact_ref': 'A'}])
    assert not gate['allowed']


def test_literal_loop_guard_precedes_credentials_and_semantics(tmp_path):
    kit = JudgeKit(enabled=True, url='https://judge.example.com/evaluate', model='example',
                   api_key_env='UNUSED_KEY', loop_window=3)
    with patch.object(kit._credentials, 'resolve', side_effect=AssertionError('deterministic first')):
        gate = kit.swarm_gates('Read', steps=['same'] * 3)
    assert gate['source'] == 'deterministic' and not gate['allowed']


def test_opt_in_session_progress_gate_stops_literal_tool_loop(tmp_path):
    from tessera.config import Config
    from tessera.loop import Session
    from fake_backend import FakeBackend
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.session_gates_enabled = True
    config.jev.loop_window = 2
    script = []
    for index in range(4):
        script.append({'tool_calls': [{'id': str(index), 'name': 'read_file', 'arguments': {'path': 'fact.txt'}}]})
    (tmp_path / 'fact.txt').write_text('A fact')
    backend = FakeBackend(script)
    with Session(config, backend=backend, root=tmp_path / 'session') as session:
        reply = session.run_turn('Read fact.txt')
        assert 'gate' in reply.lower()
        assert session.last_turn_success is False
        assert any(row['point'] == 'swarm_gate' and row['source'] == 'deterministic'
                   for row in session.router.decisions)


def test_opt_in_session_keeps_reply_but_does_not_claim_gate_success(tmp_path):
    from tessera.config import Config
    from tessera.loop import Session
    from fake_backend import FakeBackend
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.session_gates_enabled = True
    with Session(config, backend=FakeBackend(['Done, tests passed']), root=tmp_path / 'session') as session:
        assert session.run_turn('Run tests') == 'Done, tests passed'
        assert session.last_turn_success is False


def test_keychain_reference_is_lazy_and_secret_never_reaches_decision_log(tmp_path):
    from tessera.credentials import ResolvedCredential
    config = configured(tmp_path)
    config.credentials['judge'] = {'keychain_service': 'service-placeholder', 'keychain_account': 'account-placeholder'}
    kit = JudgeKit.from_config(config, decision_log=tmp_path / 'decisions.jsonl')
    def respond(request, timeout):
        assert request.get_header('Authorization') == 'Bearer fixture-keychain'
        return reply_for(json.loads(request.data))
    with patch.object(kit._credentials, 'resolve', return_value=ResolvedCredential('fixture-keychain', 'keychain')) as resolver, \
         patch('tessera.jev.urllib.request.urlopen', side_effect=respond):
        gate = kit.swarm_gates('Read', checks={'tests': True}, artifacts=[{'content': 'Read'}])
    assert gate['allowed']
    resolver.assert_called_once_with('judge', env='')
    assert 'fixture-keychain' not in (tmp_path / 'decisions.jsonl').read_text()


def test_gate_log_explains_deterministic_decision_without_private_input(tmp_path):
    kit = JudgeKit(enabled=False, decision_log=tmp_path / 'decisions.jsonl', loop_window=2)
    kit.swarm_gates('synthetic-private-objective', steps=['synthetic-private-step'] * 2)
    record = json.loads((tmp_path / 'decisions.jsonl').read_text())
    assert record['looping'] == 1 and record['escalate_to_human'] == 1
    assert 'synthetic-private' not in json.dumps(record)
