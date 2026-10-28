"""The duplex provider's voice list, asked once and used as the dropdown.

The provider answers GET /tts/voices with voice ids. The account's own credential
is resolved for the request, including an OAuth sign-in, and the answer is the
only source of names offered on the page.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading

import pytest

from tessera.config import load_config
from tessera.speech_services import DUPLEX_PROVIDERS, SpeechProbeError, probe_voices


GRANT = {'access_token': 'example-access', 'refresh_token': 'example-refresh', 'expires_at': 9_999_999_999}
VOICES = {'voices': [
    {'voice_id': 'ara', 'name': 'Ara', 'language': 'multilingual', 'gender': 'female'},
    {'voice_id': 'eve', 'name': 'Eve', 'language': 'multilingual', 'gender': 'female'},
    {'voice_id': 'rex', 'name': 'Rex', 'language': 'multilingual', 'gender': 'male'},
]}


def voice_server(payload, status=200):
    seen = []
    body = json.dumps(payload).encode('utf-8')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({'path': self.path, 'authorization': self.headers.get('Authorization', '')})
            if self.path != '/v1/tts/voices':
                self.send_error(404)
                return
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, seen


def configured(tmp_path, base_url):
    (tmp_path / 'oauth').mkdir()
    (tmp_path / 'oauth' / 'xai.json').write_text(json.dumps(GRANT), encoding='utf-8')
    (tmp_path / 'config.toml').write_text(f'''
[voice]
enabled = true
api_base_url = "{base_url}"
credential_ref = "xai-oauth"

[credentials.xai-oauth]
oauth = "xai"
''')
    return load_config(tmp_path)


def connection():
    return {'reference': 'xai-oauth', 'label': 'xAI Grok sign-in', 'provider': 'xai',
            'status': 'Signed in', 'connected': True,
            'models': DUPLEX_PROVIDERS['xai']['models']}


def test_the_provider_voice_list_is_read_with_the_account_credential(tmp_path, monkeypatch):
    httpd, seen = voice_server(VOICES)
    base = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
    monkeypatch.setitem(DUPLEX_PROVIDERS, 'xai', {**DUPLEX_PROVIDERS['xai'], 'api_base_url': base})
    config = configured(tmp_path, base)
    try:
        voices = probe_voices(config, connection())
    finally:
        httpd.shutdown()
    assert voices == ['ara', 'eve', 'rex']
    assert seen == [{'path': '/v1/tts/voices', 'authorization': 'Bearer example-access'}]


def test_a_refused_voice_list_says_the_provider_refused(tmp_path, monkeypatch):
    httpd, _seen = voice_server({'error': 'unauthorized'}, status=401)
    base = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
    monkeypatch.setitem(DUPLEX_PROVIDERS, 'xai', {**DUPLEX_PROVIDERS['xai'], 'api_base_url': base})
    config = configured(tmp_path, base)
    try:
        with pytest.raises(SpeechProbeError) as caught:
            probe_voices(config, connection())
    finally:
        httpd.shutdown()
    assert 'HTTP 401' in str(caught.value)


def test_a_provider_that_names_no_voices_is_refused(tmp_path, monkeypatch):
    httpd, _seen = voice_server({'voices': []})
    base = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
    monkeypatch.setitem(DUPLEX_PROVIDERS, 'xai', {**DUPLEX_PROVIDERS['xai'], 'api_base_url': base})
    config = configured(tmp_path, base)
    try:
        with pytest.raises(SpeechProbeError) as caught:
            probe_voices(config, connection())
    finally:
        httpd.shutdown()
    assert 'named no voices' in str(caught.value)


def test_a_missing_sign_in_is_named_not_guessed(tmp_path, monkeypatch):
    httpd, _seen = voice_server(VOICES)
    base = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
    monkeypatch.setitem(DUPLEX_PROVIDERS, 'xai', {**DUPLEX_PROVIDERS['xai'], 'api_base_url': base})
    config = configured(tmp_path, base)
    (tmp_path / 'oauth' / 'xai.json').unlink()
    try:
        with pytest.raises(SpeechProbeError) as caught:
            probe_voices(config, connection())
    finally:
        httpd.shutdown()
    assert 'not available' in str(caught.value)
