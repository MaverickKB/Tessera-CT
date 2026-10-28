"""The Voice page names accounts and models, never credentials.

Ken rejected the first attempt at this section: it offered a credential in a
dropdown, a model in a free-text box, and a field labelled "Socket". This file
pins the shape he asked for. A duplex account is something the app already
holds (a sign-in or a provider); its provider profile supplies the endpoint, so
nobody types a socket. Local speech services list the models their endpoints
report. A pipe choice appears only when both pipes are configured, and duplex
is the default.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading

from tessera.config import load_config
from tessera.credentials import MemoryKeychain
from tessera.server import create_server
from tessera.voice import pipes as voice_pipes, selected_pipe as voice_selected_pipe
from test_server import call


HOME = '''
[voice]
enabled = true
'''

GRANT = {'access_token': 'example-access', 'refresh_token': 'example-refresh', 'expires_at': 9_999_999_999}


def write_grant(home, provider='xai'):
    path = home / 'oauth' / (provider + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(GRANT), encoding='utf-8')


def serve(tmp_path, text=HOME, grant=True):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(text)
    if grant:
        write_grant(tmp_path)
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    server.manager.settings.keychain = MemoryKeychain()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join()


def voice_fields(server):
    code, body = call(server, 'GET', '/api/settings')
    assert code == 200
    section = next(item for item in body['sections'] if item['id'] == 'voice')
    return {field['id']: field for field in section['fields']}


def speech_service(payload):
    """A tiny OpenAI-shaped speech server that answers /models."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ('/models', '/v1/models'):
                body = json.dumps(payload).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


def test_the_credential_row_is_gone_and_the_account_is_a_picker(tmp_path):
    server, thread = serve(tmp_path)
    try:
        fields = voice_fields(server)
    finally:
        stop(server, thread)
    assert 'voice.credential_ref' not in fields
    account = fields['voice.account']
    assert account['type'] == 'enum'
    assert account['write'] == 'set'
    assert 'inert' not in account
    labels = [option['label'] for option in account['options']]
    assert 'xAI Grok sign-in' in labels
    assert account['value'] == ''


def test_choosing_the_account_writes_the_provider_profile(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': 'xai-oauth'})
        assert code == 200, body
        assert body['changed'] is True
        saved = load_config(tmp_path)
        assert saved.voice.credential_ref == 'xai-oauth'
        assert saved.voice.websocket_url == 'wss://api.x.ai/v1/realtime'
        assert saved.voice.api_base_url == 'https://api.x.ai/v1'
        assert saved.voice.client_secret_path == '/realtime/client_secrets'
        assert saved.voice.client_protocol_prefix == 'xai-client-secret.'
        assert saved.voice.model == 'grok-voice-latest'
        assert saved.credentials['xai-oauth'].oauth == 'xai'
        # The running core takes the new profile without a restart.
        client = server.manager.voice.client.settings
        assert client.websocket_url == 'wss://api.x.ai/v1/realtime'
        assert client.credential_ref == 'xai-oauth'
    finally:
        stop(server, thread)


def test_clearing_the_account_clears_the_provider_profile(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': 'xai-oauth'})
        assert code == 200, body
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': ''})
        assert code == 200, body
        saved = load_config(tmp_path)
        assert saved.voice.credential_ref == ''
        assert saved.voice.websocket_url == ''
        assert saved.voice.api_base_url == ''
        assert saved.voice.client_secret_path == ''
        assert saved.voice.client_protocol_prefix == ''
        assert saved.voice.model == ''
    finally:
        stop(server, thread)


def test_the_model_is_a_dropdown_of_models_the_account_offers(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': 'xai-oauth'})
        assert code == 200, body
        fields = voice_fields(server)
    finally:
        stop(server, thread)
    model = fields['voice.model']
    assert model['type'] == 'enum'
    assert model['write'] == 'set'
    options = [option['value'] for option in model['options']]
    assert options == ['grok-voice-latest', 'grok-voice-3.0']
    assert model['value'] == 'grok-voice-latest'
    endpoint = fields['voice.websocket_url']
    assert endpoint['label'] == 'Realtime endpoint'
    assert endpoint['type'] == 'display'
    assert endpoint['inert'] is True
    assert endpoint['value'] == 'wss://api.x.ai/v1/realtime'
    assert fields['voice.enabled']['status'].startswith('Ready: duplex voice over')
    labels = [field['label'] for field in fields.values()]
    assert 'Socket' not in labels
    assert 'Credential in use' not in labels


def test_no_connected_account_points_at_providers(tmp_path):
    server, thread = serve(tmp_path, grant=False)
    try:
        fields = voice_fields(server)
    finally:
        stop(server, thread)
    assert 'Sign in under Providers' in fields['voice.account']['status']
    assert fields['voice.model']['inert'] is True
    assert fields['voice.model']['status'] == 'Choose an account first.'


def test_saving_a_speech_endpoint_lists_its_models(tmp_path):
    httpd = speech_service({'data': [{'id': 'parakeet-tdt-0.6b-v3'}, {'id': 'whisper-large'}]})
    base_url = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.stt.base_url', 'value': base_url})
        assert code == 200, body
        assert '2 models listed' in body['detail'], body
        saved = load_config(tmp_path)
        assert saved.voice.stt.base_url == base_url
        assert saved.voice.stt.models == ['parakeet-tdt-0.6b-v3', 'whisper-large']
        fields = voice_fields(server)
        model = fields['voice.stt.model']
        assert model['type'] == 'enum'
        assert [option['value'] for option in model['options']] == ['parakeet-tdt-0.6b-v3', 'whisper-large']
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.stt.model', 'value': 'parakeet-tdt-0.6b-v3'})
        assert code == 200, body
        assert load_config(tmp_path).voice.stt.model == 'parakeet-tdt-0.6b-v3'
    finally:
        stop(server, thread)
        httpd.shutdown()


def test_a_pipe_choice_appears_only_when_both_pipes_are_configured(tmp_path):
    server, thread = serve(tmp_path)
    try:
        fields = voice_fields(server)
        pipe = fields['voice.pipe']
        assert pipe['type'] == 'display', 'no choice is offered before a pipe exists'
        assert pipe['status'] == 'No pipe is configured yet.'
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': 'xai-oauth'})
        assert code == 200, body
        fields = voice_fields(server)
        pipe = fields['voice.pipe']
        assert pipe['type'] == 'display', 'duplex alone must not offer a choice'
        assert 'only configured pipe' in pipe['status']
    finally:
        stop(server, thread)


def test_both_pipes_default_to_duplex_and_a_choice_holds(tmp_path):
    stt = speech_service({'data': [{'id': 'parakeet'}]})
    tts = speech_service({'data': [{'id': 'kokoro'}]})
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'voice.account', 'value': 'xai-oauth'})
        assert code == 200, body
        for setting_id, httpd in (('voice.stt.base_url', stt), ('voice.tts.base_url', tts)):
            base_url = 'http://127.0.0.1:' + str(httpd.server_address[1]) + '/v1'
            code, body = call(server, 'POST', '/api/settings', {'id': setting_id, 'value': base_url})
            assert code == 200, body
        for setting_id, model in (('voice.stt.model', 'parakeet'), ('voice.tts.model', 'kokoro')):
            code, body = call(server, 'POST', '/api/settings', {'id': setting_id, 'value': model})
            assert code == 200, body
        saved = load_config(tmp_path)
        assert voice_pipes(saved.voice) == {'duplex': True, 'local': True}
        assert voice_selected_pipe(saved.voice) == 'duplex'
        fields = voice_fields(server)
        assert fields['voice.pipe']['value'] == 'duplex'
        code, body = call(server, 'POST', '/api/settings', {'id': 'voice.pipe', 'value': 'local'})
        assert code == 200, body
        saved = load_config(tmp_path)
        assert saved.voice.pipe == 'local'
        assert voice_selected_pipe(saved.voice) == 'local'
        # The local pipe has its own route. A duplex start says so instead of pretending.
        try:
            server.manager.voice.start('voice', None)
            raise AssertionError('the local pipe must not start silently')
        except ValueError as error:
            assert 'push-to-talk' in str(error)
    finally:
        stop(server, thread)
        stt.shutdown()
        tts.shutdown()
