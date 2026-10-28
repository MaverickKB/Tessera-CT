import io
import json
import uuid

import pytest

from tessera.credentials import ResolvedCredential
from tessera.session_speech import SessionSpeech, SpeechError
from test_server import running_server, call


class MediaCredential:
    def resolve(self, reference, *, env):
        return ResolvedCredential('test-media-secret', 'env')


def test_voice_channel_identity_correlates_normal_runner_replies_and_deduplicates(tmp_path):
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Channel'})
        identity = str(uuid.uuid4())
        body = {'text': 'Use this same session', 'source': 'voice', 'client_request_id': identity}
        code, accepted = call(server, 'POST', '/api/sessions/channel/messages', body)
        assert code == 202 and accepted['turn_id'] == identity
        assert server.manager.wait('channel', 5)
        assert call(server, 'POST', '/api/sessions/channel/messages', body) == (202, accepted)
        assert call(server, 'POST', '/api/sessions/channel/messages', {**body, 'text': 'Different'})[0] == 400
        runtime = server.manager.runtime('channel')
        assert len(runtime.turns) == 1
        assert runtime.turns[identity]['source'] == 'voice'
        messages = runtime.events.after(0)
        replies = []
        for record in messages:
            if record['event'] == 'message' and record['data']['kind'] == 'agent':
                replies.append(record['data'])
        assert replies and all(row['turn_id'] == identity for row in replies)
        assert server.manager.autonomy.state['presence'].get('channel', {}).get('last_message') is None
        assert call(server, 'POST', '/api/sessions/channel/messages', {'text': 'Typed input'})[0] == 202
        assert server.manager.autonomy.state['presence']['channel']['last_message'] is not None


def test_only_committed_agent_text_reaches_speech_media(tmp_path):
    with running_server(tmp_path, script=['Actual harness reply']) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Speech'})
        call(server, 'POST', '/api/sessions/speech/messages', {'text': 'User request'})
        assert server.manager.wait('speech', 5)
        received = []

        def synthesize(request, timeout):
            received.append(json.loads(request.data))
            assert request.full_url == 'https://api.x.ai/v1/tts'
            assert timeout == 60
            return io.BytesIO(b'\x01\x00' * 240)

        adapter = SessionSpeech(server.manager, opener=synthesize, credentials=MediaCredential())
        rows = list(server.manager.get('speech').log.iter())
        agent = next(row for row in rows if row.kind == 'agent')
        user = next(row for row in rows if row.kind == 'user')
        audio, offset = adapter.render('speech', agent.i, limit=100)
        assert audio and offset == -1
        assert received == [{'text': agent.text, 'voice_id': 'rex', 'language': 'en',
                             'output_format': {'codec': 'pcm', 'sample_rate': 24000}}]
        with pytest.raises(ValueError, match='Only agent'):
            adapter.render('speech', user.i)
        for invalid in (-1, True, '0'):
            with pytest.raises(ValueError):
                adapter.render('speech', agent.i, offset=invalid)
        assert len(received) == 1


def test_speech_chunks_reassemble_exact_reply(tmp_path):
    text = 'This is the first sentence. This is the second sentence. Unicode survives too, café.'
    with running_server(tmp_path, script=[text]) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Chunks'})
        call(server, 'POST', '/api/sessions/chunks/messages', {'text': 'Speak'})
        assert server.manager.wait('chunks', 5)
        row = next(row for row in server.manager.get('chunks').log.iter() if row.kind == 'agent')
        chunks = []

        def synthesize(request, timeout):
            chunks.append(json.loads(request.data)['text'])
            return io.BytesIO(b'\x00\x00')

        adapter = SessionSpeech(server.manager, opener=synthesize, credentials=MediaCredential())
        offset = 0
        while offset != -1:
            _, offset = adapter.render('chunks', row.i, offset=offset, limit=20)
        assert ''.join(chunks) == row.text


def test_provider_transcript_never_becomes_a_harness_reply(tmp_path):
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Provenance'})
        runtime = server.manager.runtime('provenance')
        runtime.active = {'id': 'unrelated-running-turn'}
        try:
            message = runtime.session.log.append('agent', 'Provider answer', {
                'voice_transcript': True, 'provenance': 'client_reported'})
            events = runtime.events.after(0)
            event = next(row for row in events if row['event'] == 'message' and row['data']['i'] == message.i)
            assert 'turn_id' not in event['data']
            adapter = SessionSpeech(server.manager)
            with pytest.raises(ValueError, match='Only agent replies'):
                adapter.render('provenance', message.i)
        finally:
            runtime.active = None


def test_phone_steer_keeps_desk_presence_distinct(tmp_path):
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Steering'})
        code, result = call(server, 'POST', '/api/sessions/steering/steer', {'text': 'Phone guidance', 'source': 'voice'})
        assert code == 202 and result['turn_id']
        assert server.manager.wait('steering', 5)
        assert server.manager.autonomy.state['presence'].get('steering', {}).get('last_message') is None
        assert call(server, 'POST', '/api/sessions/steering/steer', {'text': 'Desk guidance'})[0] == 202
        assert server.manager.autonomy.state['presence']['steering']['last_message'] is not None


def test_speech_route_rejects_arbitrary_text(tmp_path):
    with running_server(tmp_path) as server:
        call(server, 'POST', '/api/sessions', {'title': 'Speech'})
        status, _ = call(server, 'POST', '/api/sessions/speech/speech', {'text': 'Copied prompt'})
        assert status == 400
