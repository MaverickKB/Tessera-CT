"""Voice credential and recorded realtime contracts run without live accounts."""
import io
import json
from unittest.mock import Mock, patch

import pytest

from tessera.config import Config, load_config
from tessera.sessions import SessionManager


def voice_config(tmp_path):
    config=Config(home=tmp_path,cwd=tmp_path)
    config.voice.enabled=True
    config.voice.websocket_url='wss://voice.example.com/realtime'
    config.voice.model='voice-model-placeholder'
    config.voice.api_base_url='https://voice.example.com'
    config.voice.client_secret_path='/realtime/client-secrets-placeholder'
    config.voice.api_key_env='EXAMPLE_VOICE_KEY'
    config.voice.client_protocol_prefix='example-client-secret.'
    return config


def mint_fake(monkeypatch, manager, *, now=100):
    monkeypatch.setenv('EXAMPLE_VOICE_KEY','fixture-voice-api-key')
    manager.voice.clock=lambda:now
    manager.voice.client.clock=lambda:now
    calls=[]
    def send(request,timeout):
        calls.append(request)
        return io.BytesIO(json.dumps({'value':'ephemeral-fixture','expires_at':now+60,'private':'never-return'}).encode())
    manager.voice.client.opener=send
    return calls


def test_voice_defaults_are_inert_and_settings_override_without_network(tmp_path,monkeypatch):
    config=load_config(tmp_path)
    assert not config.voice.enabled and not config.voice.websocket_url and not config.voice.model
    with SessionManager(config) as manager:
        manager.create('one')
        resolver=Mock(side_effect=AssertionError('disabled'))
        with patch('tessera.credentials.CredentialResolver.resolve',resolver):
            assert manager.status()['voice']=={'enabled':False,'configured':False,'active_count':0,'voices':[],'voice':'','pipe':'','pipes':{'duplex':False,'local':False}}
            with pytest.raises(ValueError):
                manager.voice.start('one')
        resolver.assert_not_called()
        assert manager._runtimes=={}
    monkeypatch.setenv('TESSERA_VOICE_MAX_SESSION_SECONDS','120')
    assert load_config(tmp_path).voice.max_session_seconds==120


def test_mint_is_lazy_expiry_only_and_never_returns_long_lived_key(tmp_path,monkeypatch):
    from tessera.voice import VoiceClient
    from tessera.credentials import CredentialResolver
    config=voice_config(tmp_path)
    monkeypatch.setenv('EXAMPLE_VOICE_KEY','fixture-voice-api-key')
    seen=[]
    def send(request,timeout):
        seen.append(request)
        return io.BytesIO(b'{"value":"ephemeral-fixture","expires_at":160,"api_key":"never"}')
    client=VoiceClient(config.voice,credentials=CredentialResolver(),opener=send,clock=lambda:100)
    assert seen==[]
    assert client.mint()=={'value':'ephemeral-fixture','expires_at':160}
    assert json.loads(seen[0].data)=={'expires_after':{'seconds':config.voice.secret_ttl_seconds}}
    assert seen[0].get_method()=='POST' and seen[0].get_header('Authorization')=='Bearer '+'fixture-voice-api-key'
    assert seen[0].full_url=='https://voice.example.com/realtime/client-secrets-placeholder'


def test_toolless_voice_uses_existing_log_view_without_chat_provider(tmp_path,monkeypatch):
    config=voice_config(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        calls=mint_fake(monkeypatch,manager)
        response=manager.voice.start('one')
        assert manager._runtimes=={} and len(calls)==1
        assert response['protocols']==['example-client-secret.ephemeral-fixture']
        assert 'model=voice-model-placeholder' in response['websocket_url']
        assert response['session_update']['type']=='session.update'
        # The provider is the voice agent, so the lease carries the real session
        # prompt and view, not a "transcribe only, do not answer" instruction.
        assert response['session_update']['session']['instructions'].startswith('You are ')
        assert response['session_update']['session']['tools']==[]
        # turn_detection has no create_response field in the provider schema; sending
        # one would read as a guarantee the server silently ignores.
        assert set(response['session_update']['session']['turn_detection'])=={'type'}
        assert response['session_update']['session']['turn_detection']['type']=='server_vad'
        # The provider emits a transcript only when transcription is configured,
        # and that transcript is the only path from speech into the session.
        # Without this key the socket sends speech_started and response.created
        # and never a transcript event, so the lease carries no spoken text.
        assert response['session_update']['session']['audio']['input']['transcription']=={'model':'grok-transcribe'}
        assert 'fixture-voice-api-key' not in json.dumps(response)
        events=[
            {'type':'conversation.item.input_audio_transcription.updated','item_id':'input-1','transcript':'partial'},
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'input-1','content_index':0,'transcript':'A spoken fixture'},
            {'type':'response.output_audio_transcript.done','item_id':'output-1','content_index':0,'response_id':'response-1','transcript':'An answer fixture'},
        ]
        first=manager.voice.events('one',response['voice_id'],events)
        second=manager.voice.events('one',response['voice_id'],events)
        # The provider answers as the voice agent, so its output transcript is the
        # agent's spoken reply and is recorded as such.
        assert [m['kind'] for m in first['messages']]==['user', 'agent']
        assert second['messages']==[] and second['send']==[]
        from tessera.log import ChatLog
        with ChatLog(manager.path('one')/'log') as log:
            rows=list(log.iter())
            assert [r.text for r in rows if r.meta.get('voice_transcript')]==['A spoken fixture', 'An answer fixture']
            assert all(r.meta.get('provenance')=='client_reported' for r in rows if r.meta.get('voice_transcript'))
        # This fixture has no chat backend, so the spoken line is recorded but cannot start a turn.
        assert all('ephemeral-fixture' not in p.read_text() and 'fixture-voice-api-key' not in p.read_text()
                   for p in tmp_path.rglob('*.jsonl'))


def test_voice_is_scoped_stoppable_and_expiry_enforced_before_logging(tmp_path,monkeypatch):
    config=voice_config(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        manager.create('two')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        event={'type':'conversation.item.input_audio_transcription.completed','item_id':'input','transcript':'text'}
        with pytest.raises(ValueError):
            manager.voice.events('two',start['voice_id'],[event])
        manager.voice.clock=lambda:start['session_expires_at']+1
        with pytest.raises(ValueError):
            manager.voice.events('one',start['voice_id'],[event])
        assert manager.voice.status()['active_count']==0
        manager.voice.clock=lambda:100
        new=manager.voice.start('one')
        assert manager.voice.stop('one',new['voice_id'])=={'stopped':True}
        with pytest.raises(ValueError):
            manager.voice.events('one',new['voice_id'],[event])


def test_recorded_function_batch_reuses_existing_tool_policy_and_dedupes(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    config=voice_config(tmp_path)
    config.voice.tools=['date']
    with SessionManager(config,backend_factory=lambda *args:FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        # The broker advertises the configured tools so the provider can call them.
        assert [t['name'] for t in start['session_update']['session']['tools']]==['date']
        calls=[
            {'type':'response.function_call_arguments.done','response_id':'r1','call_id':'c1','name':'date','arguments':'{}'},
            {'type':'response.function_call_arguments.done','response_id':'r1','call_id':'c2','name':'date','arguments':'{}'},
            {'type':'response.done','response':{'id':'r1','status':'completed'}},
        ]
        result=manager.voice.events('one',start['voice_id'],calls)
        assert [e['type'] for e in result['send']]==['conversation.item.create','conversation.item.create','response.create']
        assert manager.voice.events('one',start['voice_id'],calls)['send']==result['send']
        log=manager.get('one').log
        assert len([r for r in log.iter() if r.kind=='tool'])==2
        assert len([r for r in log.iter() if r.kind=='echo'])==2
        with pytest.raises(ValueError):
            manager.voice.events('one',start['voice_id'],[{'type':'response.function_call_arguments.done',
                'response_id':'r2','call_id':'c3','name':'shell','arguments':'{}'}])


def test_a_cumulative_user_transcript_supersedes_its_prefix(tmp_path, monkeypatch):
    with SessionManager(voice_config(tmp_path)) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        voice_id = manager.voice.start('one')['voice_id']
        done = 'conversation.item.input_audio_transcription.completed'
        manager.voice.events('one', voice_id, [
            {'type': done, 'item_id': 'a', 'content_index': 0, 'transcript': 'Outstanding.'},
            {'type': done, 'item_id': 'b', 'content_index': 0, 'transcript': 'Outstanding. Can you check the notes?'},
            {'type': 'response.output_audio_transcript.done', 'item_id': 'o', 'content_index': 0, 'response_id': 'r', 'transcript': 'Checking.'},
            {'type': done, 'item_id': 'c', 'content_index': 0, 'transcript': 'Outstanding work.'},
        ])
        from tessera.log import ChatLog
        with ChatLog(manager.path('one') / 'log') as log:
            rows = [r for r in log.iter() if r.meta.get('voice_transcript')]
        # The provider is the voice agent, so its output transcript is a real reply
        # and sits between the speaker's lines.
        assert [r.text for r in rows] == ['Outstanding.', 'Outstanding. Can you check the notes?', 'Checking.', 'Outstanding work.']
        assert rows[1].meta.get('supersedes') == rows[0].i
        assert 'supersedes' not in rows[2].meta, 'the agent reply replaces nothing'
        assert 'supersedes' not in rows[3].meta, 'a different utterance replaces nothing'


def test_same_audio_item_completed_events_keep_full_transcript_and_replay_is_inert(tmp_path, monkeypatch):
    with SessionManager(voice_config(tmp_path)) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        voice_id = manager.voice.start('one')['voice_id']
        event = {'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'audio-1'}
        fragments = ["What's the", "What's the status of the workboard", "What's the status of the work board?"]
        received = []
        for text in fragments:
            result = manager.voice.events('one', voice_id, [{**event, 'transcript': text}])
            received.extend(result['messages'])
        assert len(received) == 3
        assert received[1]['meta']['supersedes'] == received[0]['i']
        assert received[2]['meta']['supersedes'] == received[1]['i']
        assert manager.voice._leases[voice_id]['goal'] == fragments[-1]
        for text in (fragments[0], fragments[-1]):
            replay = manager.voice.events('one', voice_id, [{**event, 'transcript': text}])
            assert replay['messages'] == []
        assert manager.voice._leases[voice_id]['goal'] == fragments[-1]


def test_voice_reuses_session_owned_inheritance_without_second_remote_read(tmp_path, monkeypatch):
    from fake_backend import FakeBackend
    config = voice_config(tmp_path)
    config.voice.tools = ['date']
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        runtime = manager.runtime('one')
        runtime.inheritance = 'SESSION OWNED CONTINUITY SNAPSHOT'
        duplicate_read = Mock(side_effect=AssertionError('Voice must reuse the session snapshot'))
        monkeypatch.setattr(runtime.session.capture, 'inheritance_block', duplicate_read)
        started = manager.voice.start('one')
        assert 'SESSION OWNED CONTINUITY SNAPSHOT' in started['session_update']['session']['instructions']
        duplicate_read.assert_not_called()


def test_session_start_shares_failed_inheritance_read_and_explicit_refresh_recovers(tmp_path):
    from fake_backend import FakeBackend
    from tessera.capture import Capture
    from tessera.loop import Session
    client = Mock()
    client.prepare_inheritance.side_effect = OSError('Memory temporarily unavailable')
    capture = Capture(client, tmp_path / 'capture-outbox', 'proof')
    config = voice_config(tmp_path)
    def factory(config, **kwargs):
        return Session(config, capture=capture, **kwargs)
    with SessionManager(config, session_factory=factory, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        runtime = manager.runtime('one')
        assert 'Project inheritance unavailable (OSError: Memory temporarily unavailable).' in runtime.inheritance
        assert capture._inheritance is None, 'Unavailable memory must not be reported as complete'
        assert client.prepare_inheritance.call_count == 1
        client.prepare_inheritance.side_effect = None
        client.prepare_inheritance.return_value = {'layers': []}
        capture.inheritance_block()
        assert client.prepare_inheritance.call_count == 2, 'An explicit read still retries recovery'


def test_a_voice_session_is_a_session_of_its_own_with_its_own_reasoning(tmp_path):
    """Voice is a session of its own, not a picker over ordinary chats.

    The dedicated session exists from boot with its own name and its own reasoning
    posture, is flagged by the server rather than guessed from its slug, and a turn
    submitted through the voice lease runs on THAT session's reasoning settings.
    """
    from tessera.config import Config
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend
    from test_voice import mint_fake
    import json, time

    config = Config(home=tmp_path, cwd=tmp_path)
    config.voice.enabled = True
    config.voice.websocket_url = 'wss://voice.example.com/realtime'
    config.voice.model = 'voice-model-placeholder'
    config.voice.api_base_url = 'https://voice.example.com'
    config.voice.client_secret_path = '/realtime/client-secrets-placeholder'
    config.voice.api_key_env = 'EXAMPLE_VOICE_KEY'
    config.voice.client_protocol_prefix = 'example-client-secret.'
    config.voice.tools_mode = 'handoff'
    config.voice.dedicated_title = 'Talk'
    config.voice.dedicated_reasoning = 'off'
    backend = FakeBackend(['Spoken answer'])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        created = manager.ensure_voice()
        assert created['slug'] == config.voice.dedicated_slug
        assert created['title'] == 'Talk', 'the session carries its configured name'
        assert created['voice'] is True, 'the server flags it, so the page never guesses'
        assert not created.get('infinity'), 'a voice session is not an autonomy session'
        assert created['reasoning'] == 'off', 'its own reasoning posture, set at creation'
        # A second call reuses it and does not re-apply reasoning over a hand-tuned choice.
        manager.update(config.voice.dedicated_slug, reasoning='high')
        again = manager.ensure_voice()
        assert again['slug'] == created['slug']
        assert again['reasoning'] == 'high', 'a hand-tuned choice is kept'

        # A turn submitted through voice runs with THAT session's reasoning.
        import os, io
        os.environ['EXAMPLE_VOICE_KEY'] = 'fixture'
        manager.voice.clock = lambda: 100
        manager.voice.client.clock = lambda: 100
        manager.voice.client.opener = lambda request, timeout: io.BytesIO(
            json.dumps({'value': 'ephemeral', 'expires_at': 160}).encode())
        manager.create('ordinary-chat')
        start = manager.voice.start(config.voice.dedicated_slug)
        voice_id = start['voice_id']
        events = [
            {'type': 'conversation.item.input_audio_transcription.completed',
             'item_id': 'spoken-1', 'transcript': 'Please do the work'},
            {'type': 'response.function_call_arguments.done', 'name': 'session_task',
             'call_id': 'call-1', 'response_id': 'response-call-1',
             'arguments': json.dumps({'action': 'submit', 'text': 'Do the work'})},
        ]
        first = manager.voice.events(config.voice.dedicated_slug, voice_id, events)
        turn_id = json.loads(first['send'][0]['item']['output'])['turn_id']
        deadline = time.monotonic() + 5
        while manager.turn(config.voice.dedicated_slug, turn_id)['state'] not in ('done', 'error'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        call = backend.calls[0]
        assert call['reasoning_effort'] == 'high', 'the voice turn uses the session reasoning'
        # The turn landed on the voice session, not on an ordinary chat.
        assert manager.describe(config.voice.dedicated_slug)['messages'] >= 2
        assert manager.describe('ordinary-chat')['messages'] == 0
