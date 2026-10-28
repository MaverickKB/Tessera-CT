"""Public demo writers must not publish configured or provider-echoed locations."""

import io
import json
from pathlib import Path
import subprocess
from urllib.parse import unquote

import pytest

from demo import judgekit_smoke
from demo.live_smoke import redact


@pytest.mark.parametrize('variant', ['uppercase', 'escaped', 'encoded_host', 'foreign_url',
                                    'malformed_url', 'unc', 'foreign_host', 'ipv4', 'ipv6', 'private_path'])
def test_transcript_redacts_network_locations_beyond_exact_config_strings(variant, tmp_path):
    configured = 'model.example.test'
    foreign = '.'.join(['edge-canary', 'internal'])
    ipv4 = '.'.join(['10', '71', '23', '8'])
    ipv6 = ':'.join(['fd01', '', '31'])
    private_path = '/'.join(['', 'home', 'operator-canary', 'private-record'])
    inputs = {
        'uppercase': 'HTTPS://' + configured.upper() + ':9443/v1',
        'escaped': 'https:\\/\\/' + foreign + '/events',
        'encoded_host': ''.join('%' + format(byte, '02x') for byte in configured.encode()),
        'foreign_url': 'wss://' + foreign + ':9443/events?token=secret-canary',
        'malformed_url': 'https:/' + foreign + ':9443/private',
        'unc': '\\\\' + foreign + '\\private',
        'foreign_host': foreign + ':9443',
        'ipv4': ipv4 + ':9443',
        'ipv6': '[' + ipv6 + ']:9443',
        'private_path': private_path,
    }
    cleaned = redact(inputs[variant], 'https://' + configured + '/v1', tmp_path)
    decoded = unquote(cleaned).casefold()
    for private in (configured, foreign, ipv4, ipv6, private_path, 'secret-canary'):
        assert private not in decoded
    assert cleaned.strip()


def test_real_judgekit_writer_redacts_provider_metadata_before_publishing(tmp_path, monkeypatch, capsys):
    foreign = '.'.join(['judge-canary', 'internal'])
    remote_model = 'https://' + foreign + '/private-model'
    model = 'configured-model-canary'
    monkeypatch.setenv('TESSERA_JEV_ENABLED', 'true')
    monkeypatch.setenv('TESSERA_JEV_URL', 'https://judge.example.test/evaluate')
    monkeypatch.setenv('TESSERA_JEV_MODEL', model)
    monkeypatch.setenv('TESSERA_JEV_API_KEY_ENV', 'DEMO_JUDGE_TEST_KEY')
    monkeypatch.setenv('DEMO_JUDGE_TEST_KEY', 'credential-canary')
    monkeypatch.setenv('TESSERA_HOME', str(tmp_path / 'private'))
    monkeypatch.setattr(judgekit_smoke, 'ROOT', tmp_path)

    def respond(request, timeout):
        body = json.loads(request.data)
        answers = {}
        for name, question in body['questions'].items():
            kind = question['type']
            if kind == 'noul':
                answer = {'type': kind, 'noul': .8}
            elif kind == 'score':
                answer = {'type': kind, 'score': 1.5,
                          'probabilities': {'0': 0, '1': .5, '2': .5}, 'confidence': .5}
            else:
                choices = list(question['criteria'])
                probabilities = {}
                for index, choice in enumerate(choices):
                    probabilities[choice] = 1.0 if index == 0 else 0.0
                answer = {'type': kind, 'choice': choices[0],
                          'probabilities': probabilities, 'confidence': 1.0}
            answers[name] = answer
        return io.BytesIO(json.dumps({'model': remote_model, 'answers': answers}).encode())

    monkeypatch.setattr('tessera.jev.urllib.request.urlopen', respond)
    assert judgekit_smoke.main() == 0
    published = (tmp_path / 'tool-output' / 'judgekit-smoke.json').read_text()
    result = json.loads(published)
    assert result['summary']['turn_count'] == 5 and result['summary']['all_live'] is True
    assert result['turns'][0]['intake']['needs_memory'] == .8
    private_log = tmp_path / 'private' / 'judgekit-smoke' / 'decisions.jsonl'
    assert foreign in private_log.read_text()
    stdout = capsys.readouterr().out
    for private in (foreign, remote_model, model, 'credential-canary', str(tmp_path)):
        assert private not in published
        assert private not in stdout


def test_json_writer_redacts_nested_keys_metadata_and_explicit_secrets(tmp_path):
    from demo.evidence import write_json

    foreign = '.'.join(['new-edge-canary', 'test'])
    private = 'secret-canary'
    source = {
        'marker': 'LIVE-VOICE-ACCEPT', 'status': 'accepted',
        'enabled': True, 'reported_tokens': 1.25, 'missing': None,
        'workers': [{foreign: 'https://' + foreign + '/private',
                     'model_id': 'private-model-canary',
                     '%68ost': 'private-alias-canary',
                     'message': 'credential ' + private}],
        'opaque': 'edge_alias_canary:9443',
    }
    before = json.loads(json.dumps(source))
    output = tmp_path / 'published.json'
    write_json(output, source, [(private, '[REDACTED]')])
    published = output.read_text()
    result = json.loads(published)
    for value in (foreign, private, 'private-model-canary', 'private-alias-canary', 'edge_alias_canary'):
        assert value not in published
    assert source == before
    assert result['marker'] == 'LIVE-VOICE-ACCEPT'
    assert result['status'] == 'accepted' and result['enabled'] is True
    assert result['reported_tokens'] == 1.25 and result['missing'] is None
    assert result['workers'][0]['model_id'] == '[REDACTED]'


def test_redacted_key_collisions_fail_without_replacing_saved_evidence(tmp_path):
    from demo.evidence import write_json

    output = tmp_path / 'published.json'
    previous = '{"status": "previous"}\n'
    output.write_text(previous)
    hosts = ['.'.join(['first-canary', 'test']), '.'.join(['second-canary', 'test'])]
    with pytest.raises(ValueError, match='Evidence') as error:
        write_json(output, {hosts[0]: 1, hosts[1]: 2})
    assert output.read_text() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ['published.json']
    for host in hosts:
        assert host not in str(error.value)


def test_failed_publish_is_atomic_and_removes_only_its_temporary_file(tmp_path, monkeypatch):
    from demo.evidence import write_json

    output = tmp_path / 'published.json'
    previous = '{"status": "previous"}\n'
    output.write_text(previous)

    def fail_replace(*args):
        raise OSError('synthetic publish failure')

    monkeypatch.setattr('os.replace', fail_replace)
    with pytest.raises(OSError):
        write_json(output, {'status': 'replacement'})
    assert output.read_text() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == ['published.json']


def test_publication_does_not_follow_a_destination_symlink(tmp_path):
    from demo.evidence import write_json

    private = tmp_path / 'private.json'
    private.write_text('private canary stays untouched')
    output = tmp_path / 'published.json'
    output.symlink_to(private)
    write_json(output, {'status': 'accepted'})
    assert private.read_text() == 'private canary stays untouched'
    assert not output.is_symlink()
    assert json.loads(output.read_text()) == {'status': 'accepted'}


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), {1: 'unsupported-key'}, object()])
def test_invalid_evidence_fails_closed_without_changing_output(tmp_path, invalid):
    from demo.evidence import write_json

    output = tmp_path / 'published.json'
    output.write_text('{"status": "previous"}\n')
    with pytest.raises(ValueError, match='Evidence'):
        write_json(output, {'result': invalid})
    assert output.read_text() == '{"status": "previous"}\n'
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize('wrapper', ['unicode', 'html', 'nested_percent', 'deep_percent'])
def test_encoded_locations_cannot_survive_public_json_roundtrip(wrapper, tmp_path):
    from demo.evidence import write_json

    host = '.'.join(['unicode-edge-canary', 'test'])
    if wrapper == 'unicode':
        text = ''.join('\\u' + format(ord(char), '04x') for char in host)
    elif wrapper == 'html':
        text = ''.join('&#' + str(ord(char)) + ';' for char in host)
    else:
        text = ''.join('%' + format(byte, '02x') for byte in host.encode())
        layers = 12 if wrapper == 'deep_percent' else 2
        for _ in range(layers):
            text = text.replace('%', '%25')
    output = tmp_path / 'published.json'
    write_json(output, {'notice': text})
    notice = json.loads(output.read_text())['notice']
    assert notice == '[REDACTED]'
    assert host not in output.read_text()


def test_cyclic_data_is_rejected_without_publishing(tmp_path):
    from demo.evidence import write_json

    values = []
    values.append(values)
    output = tmp_path / 'published.json'
    with pytest.raises(ValueError, match='Evidence'):
        write_json(output, values)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_tracked_demo_json_contains_no_unredacted_network_locations():
    from demo.evidence import redact_text
    from git_checkout import require_git_checkout

    require_git_checkout()

    root = Path(__file__).resolve().parents[1]
    paths = subprocess.check_output(['git', 'ls-files', '-z', '--', 'demo/*.json'], cwd=root).split(b'\0')
    if not any(paths):
        pytest.skip('no tracked demo JSON files to scan')
    for raw_path in paths:
        if not raw_path:
            continue
        name = raw_path.decode()
        pending = [json.loads((root / name).read_text())]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str) and redact_text(item) != item:
                pytest.fail('Public demo JSON requires location redaction: ' + name)
