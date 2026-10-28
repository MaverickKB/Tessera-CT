"""Jev element picking speaks to a fake TypeSafe server. It does not call the real API."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import ComputerUseJevConfig, load_config
from tessera.credentials import CredentialReference, CredentialResolver
from tessera.cua_jev import api_key, check_tree, find_in_tree, format_check, format_find, parse_tree
from tessera.loop import Session
from test_native_cua import HELPER, base_config, executable


FIXTURE = Path(__file__).parent / 'fixtures' / 'textedit_tree.txt'
ROOT = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        body = json.loads(self.rfile.read(length) or b'{}')
        self.server.seen.append({
            'path': self.path,
            'authorization': self.headers.get('Authorization'),
            'long_context': self.headers.get('TypeSafe-Danger-Zone-Long-Context'),
            'content_type': self.headers.get('Content-Type'),
            'body': body,
        })
        if self.server.status != 200:
            raw = b'{"error":"no"}'
            self.send_response(self.server.status)
        else:
            raw = json.dumps(self.server.reply(body)).encode('utf-8')
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


class FakeJev:
    def __init__(self, reply, status=200):
        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._httpd.seen = []
        self._httpd.reply = reply
        self._httpd.status = status
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self):
        host, port = self._httpd.server_address
        return f'http://{host}:{port}'

    @property
    def seen(self):
        return self._httpd.seen

    def close(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()


def choice_reply(winner='3', probability=0.82, confidence=0.91):
    def reply(body):
        criteria = body['questions']['element']['criteria']
        names = list(criteria)
        selected = winner if winner in names else names[0]
        others = [name for name in names if name != selected]
        probabilities = {name: 0.0 for name in names}
        probabilities[selected] = probability
        if others:
            share = (1 - probability) / len(others)
            for name in others:
                probabilities[name] = share
        else:
            probabilities[selected] = 1
        return {'model': 'jev-1.13.0', 'answers': {'element': {
            'type': 'choice', 'choice': selected, 'probabilities': probabilities, 'confidence': confidence,
        }}, 'usage': {'input_tokens': 10, 'output_tokens': 4}}
    return reply


def noul_reply(probability):
    def reply(body):
        assert 'landed' in body['questions']
        return {'model': 'jev-1.13.0', 'answers': {'landed': {'type': 'noul', 'noul': probability}},
                'usage': {'input_tokens': 8, 'output_tokens': 2}}
    return reply


def routed_reply(choice_probability=0.82, choice_confidence=0.91, noul=0.93):
    pick = choice_reply(probability=choice_probability, confidence=choice_confidence)
    judge = noul_reply(noul)

    def reply(body):
        if 'element' in body['questions']:
            return pick(body)
        return judge(body)
    return reply


def settings(server, **overrides):
    values = dict(
        enabled=True, base_url=server.url, model='configured-judge', api_key_env='TYPESAFE_API_KEY',
        timeout=5, min_confidence=0.5, max_state_chars=24000, long_context=False,
    )
    values.update(overrides)
    return ComputerUseJevConfig(**values)


def test_loader_keeps_the_picker_off_and_rejects_a_key_in_the_url(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('[computer_use]\nprovider = "native"\n', encoding='utf-8')
    loaded = load_config(tmp_path)
    assert loaded.computer_use.jev.enabled is False
    assert loaded.computer_use.jev.base_url == ''
    assert loaded.computer_use.jev.model == ''
    assert loaded.computer_use.jev.api_key_env == 'TYPESAFE_API_KEY'
    assert loaded.computer_use.jev.min_confidence == 0.5
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\nenabled = true\nmin_confidence = 0.8\nmax_state_chars = 1000\n',
        encoding='utf-8')
    with pytest.raises(ValueError, match='configuration error') as missing:
        load_config(tmp_path)
    assert 'computer_use.jev.base_url' in str(missing.value)
    assert 'computer_use.jev.model' in str(missing.value)
    assert 'api.typesafe.ai' not in str(missing.value)
    assert 'jev-latest' not in str(missing.value)
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\nenabled = true\nbase_url = "https://jev.example.com"\n',
        encoding='utf-8')
    with pytest.raises(ValueError, match='computer_use.jev.model') as model_only:
        load_config(tmp_path)
    assert 'computer_use.jev.base_url' not in str(model_only.value)
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\nenabled = true\nmodel = "configured-judge"\n',
        encoding='utf-8')
    with pytest.raises(ValueError, match='computer_use.jev.base_url') as host_only:
        load_config(tmp_path)
    assert 'computer_use.jev.model' not in str(host_only.value)
    monkeypatch.setenv('TESSERA_COMPUTER_USE__JEV__BASE_URL', 'https://jev.example.com')
    monkeypatch.setenv('TESSERA_COMPUTER_USE__JEV__MODEL', 'from-env')
    from_env = load_config(tmp_path)
    assert from_env.computer_use.jev.enabled is True
    assert from_env.computer_use.jev.base_url == 'https://jev.example.com'
    assert from_env.computer_use.jev.model == 'from-env'
    monkeypatch.delenv('TESSERA_COMPUTER_USE__JEV__BASE_URL')
    monkeypatch.delenv('TESSERA_COMPUTER_USE__JEV__MODEL')
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\nenabled = true\nmin_confidence = 0.8\nmax_state_chars = 1000\n'
        'base_url = "https://jev.example.com"\nmodel = "configured-judge"\n',
        encoding='utf-8')
    tuned = load_config(tmp_path)
    assert tuned.computer_use.jev.enabled is True
    assert tuned.computer_use.jev.base_url == 'https://jev.example.com'
    assert tuned.computer_use.jev.model == 'configured-judge'
    assert tuned.computer_use.jev.min_confidence == 0.8
    assert tuned.computer_use.jev.max_state_chars == 1000
    (tmp_path / 'config.toml').write_text('[computer_use.jev]\napi_key = "secret"\n', encoding='utf-8')
    with pytest.raises(ValueError, match='computer_use.jev'):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\nbase_url = "https://user:secret@jev.example.com"\n', encoding='utf-8')
    with pytest.raises(ValueError, match='base_url'):
        load_config(tmp_path).validate()


def test_tools_stay_unregistered_until_the_key_is_set(tmp_path, monkeypatch):
    executable(HELPER)
    config = base_config(tmp_path, HELPER)
    with Session(config, backend=FakeBackend([])) as session:
        names = {tool['function']['name'] for tool in session.tools.schemas()}
    assert 'computer_find_element' not in names and 'computer_check' not in names
    config.computer_use.jev.enabled = True
    with pytest.raises(ValueError, match='configuration error') as missing:
        Session(config, backend=FakeBackend([]))
    assert 'computer_use.jev.base_url' in str(missing.value)
    assert 'computer_use.jev.model' in str(missing.value)
    config.computer_use.jev.base_url = 'https://jev.example.com'
    config.computer_use.jev.model = 'configured-judge'
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    with pytest.raises(ValueError, match='TYPESAFE_API_KEY'):
        Session(config, backend=FakeBackend([]))


def test_find_and_check_use_the_documented_question_shape(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'probe-secret')
    server = FakeJev(routed_reply())
    try:
        jev = settings(server)
        tree = FIXTURE.read_text(encoding='utf-8')
        found = find_in_tree(tree, 'the Hello text', jev, 'probe-secret')
        checked = check_tree(tree, 'Does the document contain Hello?', jev, 'probe-secret')
        text = format_find(found)
        assert text.startswith('index 3')
        assert 'not sure' not in text
        assert '3 AXTextArea - - Hello 141,96,656,384 AXShowMenu' in text
        assert 'probability 0.820' in text
        assert 'runner-up' in text
        assert format_check(checked) == 'yes\nprobability 0.930'
        seen = server.seen
        assert [item['path'] for item in seen] == ['/v1/systemone', '/v1/systemone']
        assert all(item['authorization'] == 'Bearer probe-secret' for item in seen)
        assert all(item['content_type'] == 'application/json' for item in seen)
        assert all(item['long_context'] is None for item in seen)
        choice = seen[0]['body']
        assert choice['model'] == 'configured-judge'
        assert set(choice) == {'state', 'model', 'questions'}
        question = choice['questions']['element']
        assert question['type'] == 'choice'
        assert 'Hello' in question['instructions']
        assert '7' not in question['criteria']
        assert 'AXCloseButton' in choice['state']
        assert 'Saved' not in choice['state']
        assert all(value is None for value in question['criteria'].values())
        noul = seen[1]['body']['questions']['landed']
        assert noul['type'] == 'noul'
        assert noul['criteria']['true'].startswith('Yes')
        assert noul['criteria']['false'].startswith('No')
        assert 'probe-secret' not in json.dumps(choice) and 'probe-secret' not in text
    finally:
        server.close()


def test_low_confidence_returns_not_sure_and_a_role_hint_filters(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'probe-secret')
    server = FakeJev(choice_reply(winner='1', probability=0.41, confidence=0.2))
    try:
        tree = FIXTURE.read_text(encoding='utf-8')
        unsure = format_find(find_in_tree(tree, 'Close button', settings(server), 'probe-secret'))
        assert unsure.startswith('not sure\n')
        assert 'index 1' in unsure
        buttons = FakeJev(choice_reply(winner='1', probability=0.9, confidence=0.95))
        try:
            find_in_tree(tree, 'Hello', settings(buttons), 'probe-secret', role='AXButton')
            body = buttons.seen[0]['body']
            assert set(body['questions']['element']['criteria']) == {'1', '2'}
            assert 'Hello' not in body['state']
            find_in_tree(tree, 'Hello', settings(buttons, max_state_chars=80), 'probe-secret')
            narrowed = buttons.seen[1]['body']
            assert '3' in narrowed['questions']['element']['criteria']
            assert len(narrowed['state']) <= 80
        finally:
            buttons.close()
    finally:
        server.close()
    empty = FakeJev(noul_reply(0.2))
    try:
        no = format_check(check_tree(FIXTURE.read_text(encoding='utf-8'), 'Is the document empty?', settings(empty), 'probe-secret'))
    finally:
        empty.close()
    assert no == 'no\nprobability 0.200'


def test_long_context_header_and_http_failure(monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'probe-secret')
    server = FakeJev(routed_reply(), status=422)
    try:
        jev = settings(server, long_context=True)
        with pytest.raises(ValueError, match='HTTP 422'):
            find_in_tree(FIXTURE.read_text(encoding='utf-8'), 'Hello', jev, 'probe-secret')
        assert server.seen[0]['long_context'] == 'true'
        assert 'probe-secret' not in str(server.seen[0]['path'])
    finally:
        server.close()


def test_session_tools_are_read_only_and_skip_jev_when_the_helper_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'probe-secret')
    executable(HELPER)
    log_path = tmp_path / 'helper.log'
    wrapper = tmp_path / 'helper'
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {HELPER} {log_path}\n', encoding='utf-8')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    server = FakeJev(routed_reply())
    try:
        config = base_config(tmp_path, wrapper)
        config.tools.approval = 'ask'
        config.computer_use.jev = settings(server)
        with Session(config, backend=FakeBackend([])) as session:
            find = session.tools._tools['computer_find_element']
            check = session.tools._tools['computer_check']
            assert find.risky is False and check.risky is False
            assert 'before computer_click' in find.description
            assert 'confirm the action landed' in check.description
            result = session.tools.execute('computer_find_element', {'app': 'Picker', 'description': 'the Hello text'})
            assert result.startswith('index 3')
            assert 'probe-secret' not in result
            verdict = session.tools.execute('computer_check', {'app': 'Picker', 'question': 'Does the document contain Hello?'})
            assert verdict.startswith('yes')
            denied = session.tools.execute('computer_find_element', {'app': 'Denied', 'description': 'Close'})
        assert denied.startswith('unavailable:') and 'Accessibility' in denied and 'TesseraCUA' in denied
        assert len(server.seen) == 2
    finally:
        server.close()


def test_many_elements_stay_within_the_choice_limit():
    lines = [f'{index} AXButton Item{index} - 0,0,10,10 AXPress' for index in range(300)]
    server = FakeJev(choice_reply(winner='0', probability=1, confidence=1))
    try:
        jev = settings(server, max_state_chars=100000)
        find_in_tree('\n'.join(lines), 'Item0', jev, 'probe-secret')
        criteria = server.seen[0]['body']['questions']['element']['criteria']
        assert len(criteria) == 255
        assert '0' in criteria
    finally:
        server.close()


def test_probe_script_prints_index_probability_and_latency(tmp_path, monkeypatch):
    monkeypatch.setenv('TYPESAFE_API_KEY', 'probe-secret')
    server = FakeJev(routed_reply())
    try:
        home = tmp_path / 'home'
        home.mkdir()
        (home / 'config.toml').write_text(
            '[computer_use.jev]\n'
            f'base_url = "{server.url}"\n'
            'model = "configured-judge"\n',
            encoding='utf-8')
        env = os.environ.copy()
        env['TESSERA_HOME'] = str(home)
        env['TYPESAFE_API_KEY'] = 'probe-secret'
        completed = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'jev_probe.py'), str(FIXTURE), 'the Hello text', 'Does the document contain Hello?'],
            capture_output=True, text=True, env=env, check=False,
        )
        assert completed.returncode == 0, completed.stderr
        lines = completed.stdout.splitlines()
        assert lines[0].startswith('find index 3 probability 0.820 sure latency_ms ')
        assert lines[1].startswith('check yes probability 0.930 latency_ms ')
        assert 'probe-secret' not in completed.stdout and 'probe-secret' not in completed.stderr
        assert server.seen[0]['authorization'] == 'Bearer probe-secret'
    finally:
        server.close()


def test_tree_lines_keep_the_role_and_carry_the_subrole():
    rows = {row['index']: row for row in parse_tree(FIXTURE.read_text(encoding='utf-8'))}
    assert rows['1']['role'] == 'AXButton'
    assert rows['1']['subrole'] == 'AXCloseButton'
    assert rows['1']['line'].startswith('1 AXButton AXCloseButton ')
    assert rows['3']['role'] == 'AXTextArea'
    assert rows['4']['line'].split()[3] == 'Close'


def test_credential_ref_uses_the_keychain_and_a_set_env_var_wins():
    class Result:
        returncode = 0
        stdout = 'from-keychain\n'
        stderr = ''

    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return Result()

    sources = {'typesafe': {
        'keychain_service': 'TYPESAFE_API_KEY',
        'keychain_account': 'TypeSafe API',
    }}
    jev = ComputerUseJevConfig(enabled=True, credential_ref='typesafe')
    resolver = CredentialResolver(
        sources, environ={}, platform='darwin', which=lambda _name: '/usr/bin/security', run=run)
    assert api_key(jev, resolver) == 'from-keychain'
    assert calls[0][1:6] == ['find-generic-password', '-s', 'TYPESAFE_API_KEY', '-a', 'TypeSafe API']
    env_resolver = CredentialResolver(
        sources, environ={'TYPESAFE_API_KEY': 'from-env'}, platform='darwin',
        which=lambda _name: '/usr/bin/security', run=run)
    assert api_key(jev, env_resolver) == 'from-env'
    assert len(calls) == 1

    def fail(argv, **_kwargs):
        class Miss:
            returncode = 1
            stdout = 'super-secret-value'
            stderr = 'super-secret-value'
        return Miss()

    missed = CredentialResolver(
        sources, environ={}, platform='darwin', which=lambda _name: '/usr/bin/security', run=fail)
    with pytest.raises(ValueError, match='credential typesafe') as caught:
        api_key(jev, missed)
    assert 'super-secret-value' not in str(caught.value)


def test_unknown_credential_ref_is_rejected_and_a_known_one_registers_tools(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text(
        '[computer_use.jev]\ncredential_ref = "typesafe"\n', encoding='utf-8')
    with pytest.raises(ValueError, match='computer_use.jev credential_ref'):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text(
        '[credentials.typesafe]\n'
        'keychain_service = "TYPESAFE_API_KEY"\n'
        'keychain_account = "TypeSafe API"\n'
        '\n[computer_use.jev]\n'
        'enabled = true\n'
        'credential_ref = "typesafe"\n'
        'base_url = "https://jev.example.com"\n'
        'model = "configured-judge"\n',
        encoding='utf-8')
    loaded = load_config(tmp_path)
    assert loaded.computer_use.jev.credential_ref == 'typesafe'
    assert loaded.credentials['typesafe'].keychain_account == 'TypeSafe API'
    monkeypatch.delenv('TYPESAFE_API_KEY', raising=False)
    executable(HELPER)
    home = tmp_path / 'session'
    home.mkdir()
    config = base_config(home, HELPER)
    config.computer_use.jev.enabled = True
    config.computer_use.jev.credential_ref = 'typesafe'
    config.computer_use.jev.base_url = 'https://jev.example.com'
    config.computer_use.jev.model = 'configured-judge'
    config.credentials['typesafe'] = CredentialReference(
        keychain_service='TYPESAFE_API_KEY', keychain_account='TypeSafe API')
    with Session(config, backend=FakeBackend([])) as session:
        names = {tool['function']['name'] for tool in session.tools.schemas()}
    assert 'computer_find_element' in names and 'computer_check' in names
