"""The local speech pipe: push-to-talk through STT and TTS with stub services.

No model runs here. A stub OpenAI-shaped speech server answers ``/models``,
``/v1/audio/transcriptions`` with a fixed transcript, and ``/v1/audio/speech``
with deterministic PCM. That proves the pipe end to end: audio in, a real session
turn, audio out. The same adapter takes a real endpoint later.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import struct
import threading

import pytest

from tessera.config import load_config
from tessera.local_voice import pcm_to_wav, wav_to_pcm
from tessera.server import create_server
from fake_backend import FakeBackend
from test_server import call


SPEECH_RATE = 24000
REPLY_PCM = struct.pack('<' + 'h' * 240, *([1000] * 240))


class SpeechStub:
    """One server that answers as both a speech-to-text and a text-to-speech endpoint."""

    def __init__(self, transcript='What is on the board?', speech_wav=None, speech_status=200,
                 transcript_status=200):
        self.transcript = transcript
        self.speech_wav = speech_wav if speech_wav is not None else pcm_to_wav(REPLY_PCM, SPEECH_RATE)
        self.speech_status = speech_status
        self.transcript_status = transcript_status
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.requests.append(('GET', self.path, b''))
                if self.path in ('/v1/models', '/models'):
                    body = json.dumps({'data': [{'id': 'stub-stt'}, {'id': 'stub-tts'}]}).encode()
                    self._reply(200, 'application/json', body)
                    return
                self.send_error(404)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(length)
                stub.requests.append(('POST', self.path, raw))
                if self.path == '/v1/audio/transcriptions':
                    body = json.dumps({'text': stub.transcript}).encode()
                    self._reply(stub.transcript_status, 'application/json', body)
                    return
                if self.path == '/v1/audio/speech':
                    self._reply(stub.speech_status, 'audio/wav', stub.speech_wav)
                    return
                self.send_error(404)

            def _reply(self, status, content_type, body):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base_url = 'http://127.0.0.1:' + str(self.httpd.server_address[1]) + '/v1'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self, path):
        return [raw for method, where, raw in self.requests if method == 'POST' and where == path]


def home_text(stub, pipe='local', duplex=False, max_audio=None):
    voice_lines = ['[voice]', 'enabled = true', 'pipe = "' + pipe + '"']
    if duplex:
        voice_lines += ['websocket_url = "wss://api.example.invalid/v1/realtime"',
                        'model = "stub-duplex"',
                        'api_base_url = "https://api.example.invalid/v1"',
                        'client_secret_path = "/tmp/tessera-auth.json"',
                        'client_protocol_prefix = "xai"',
                        'api_key_env = "STUB_SPEECH_KEY"']
    if max_audio is not None:
        voice_lines.append('local_max_audio_bytes = ' + str(max_audio))
    return f'''
[backends.fake]
base_url = "http://127.0.0.1:9/v1"
model = "scripted-local-fake"
chat = true
enabled = true

[roles]
main = "fake"

''' + '\n'.join(voice_lines) + f'''

[voice.stt]
base_url = "{stub.base_url}"
model = "stub-stt"

[voice.tts]
base_url = "{stub.base_url}"
model = "stub-tts"
'''


def serve(tmp_path, stub, script=None, pipe='local', duplex=False, max_audio=None):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(home_text(stub, pipe, duplex, max_audio))
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config, backend_factory=lambda slug, name: FakeBackend(script or ['Board is clear.']))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.manager.create('one')
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join()


def utterance_body(pcm=None, rate=SPEECH_RATE):
    audio = pcm if pcm is not None else struct.pack('<' + 'h' * 4800, *([0] * 4800))
    return {'audio': base64.b64encode(audio).decode('ascii'), 'rate': rate}


def test_wav_round_trip_keeps_pcm_and_rate():
    pcm = struct.pack('<4h', 1, -2, 3, -4)
    raw = pcm_to_wav(pcm, SPEECH_RATE)
    back, rate, channels = wav_to_pcm(raw)
    assert back == pcm and rate == SPEECH_RATE and channels == 1


def test_a_wav_that_is_not_pcm16_is_refused():
    with pytest.raises(ValueError):
        wav_to_pcm(b'RIFF' + struct.pack('<I', 36) + b'WAVE' + b'fmt ' + struct.pack('<I', 16)
                   + struct.pack('<HHIIHH', 3, 1, 24000, 48000, 2, 16) + b'data' + struct.pack('<I', 0))


def test_push_to_talk_turns_speech_into_a_session_turn_and_speech(tmp_path):
    stub = SpeechStub(transcript='What is on the board?')
    server, thread = serve(tmp_path, stub, script=['The board is clear.'])
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body())
        assert code == 200, body
        assert body['transcript'] == 'What is on the board?'
        assert body['reply'] == 'The board is clear.'
        assert body['rate'] == SPEECH_RATE and body['channels'] == 1
        assert base64.b64decode(body['audio']) == REPLY_PCM
        # The transcript is a real turn in the session, tagged as voice.
        messages = call(server, 'GET', '/api/sessions/one/messages')[1]
        users = [row for row in messages if row['kind'] == 'user']
        assert users and users[-1]['text'] == 'What is on the board?'
        assert users[-1]['meta'].get('voice_transcript') is True
        assert any(row['kind'] == 'agent' and row['text'] == 'The board is clear.' for row in messages)
        # The utterance was sent as a WAV, and the reply was asked for by model name.
        uploads = stub.posts('/v1/audio/transcriptions')
        assert len(uploads) == 1 and uploads[0].startswith(b'--') and b'stub-stt' in uploads[0]
        speeches = stub.posts('/v1/audio/speech')
        asked = json.loads(speeches[0])
        assert asked['model'] == 'stub-tts' and asked['input'] == 'The board is clear.'
    finally:
        stop(server, thread)
        stub.close()


def test_the_route_refuses_when_the_local_pipe_is_not_selected(tmp_path):
    stub = SpeechStub()
    server, thread = serve(tmp_path, stub, pipe='duplex', duplex=True)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body())
        assert code == 409, body
        assert 'Local speech in Settings' in body['error']
        assert stub.posts('/v1/audio/transcriptions') == []
    finally:
        stop(server, thread)
        stub.close()


def test_an_oversized_utterance_is_refused_before_any_service_is_called(tmp_path):
    stub = SpeechStub()
    server, thread = serve(tmp_path, stub, max_audio=2000)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local',
                          utterance_body(pcm=b'\x00' * 2002))
        assert code == 413, body
        assert stub.requests == []
    finally:
        stop(server, thread)
        stub.close()


def test_an_empty_transcript_is_reported_not_sent_to_the_session(tmp_path):
    stub = SpeechStub(transcript='   ')
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body())
        assert code == 422, body
        assert 'no text' in body['error']
        messages = call(server, 'GET', '/api/sessions/one/messages')[1]
        assert [row for row in messages if row['kind'] == 'user'] == []
    finally:
        stop(server, thread)
        stub.close()


def test_a_failed_speech_to_text_reports_upstream(tmp_path):
    stub = SpeechStub(transcript_status=500)
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body())
        assert code == 502, body
        assert 'refused the request' in body['error']
    finally:
        stop(server, thread)
        stub.close()


def test_a_text_to_speech_answer_in_the_wrong_format_is_refused(tmp_path):
    stub = SpeechStub(speech_wav=b'\x00' * 64)
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body())
        assert code == 502, body
        assert 'unsupported format' in body['error']
    finally:
        stop(server, thread)
        stub.close()


def test_invalid_audio_encoding_is_refused(tmp_path):
    stub = SpeechStub()
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local',
                          {'audio': 'not base64!', 'rate': SPEECH_RATE})
        assert code == 400, body
        assert 'encoding' in body['error']
        assert stub.requests == []
    finally:
        stop(server, thread)
        stub.close()


def test_a_bad_sample_rate_or_frame_is_refused(tmp_path):
    stub = SpeechStub()
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body(rate=96000))
        assert code == 400, body
        assert 'rate' in body['error']
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body(rate=4000))
        assert code == 400, body
        code, body = call(server, 'POST', '/api/sessions/one/voice/local', utterance_body(pcm=b'\x00\x01\x02'))
        assert code == 400, body
        assert stub.requests == []
    finally:
        stop(server, thread)
        stub.close()


def test_the_duplex_lease_route_refuses_the_local_pipe_by_name(tmp_path):
    stub = SpeechStub()
    server, thread = serve(tmp_path, stub)
    try:
        code, body = call(server, 'POST', '/api/sessions/one/voice/start', {})
        assert code == 400, body
        assert 'push-to-talk' in body['error']
    finally:
        stop(server, thread)
        stub.close()
