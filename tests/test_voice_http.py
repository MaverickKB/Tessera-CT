"""App-owned loopback HTTP and recorded events, without live voice services."""
import io
import json
import threading
from unittest.mock import Mock, patch

import pytest

from tessera.config import Config, load_config
from tessera.server import create_server
from tessera.sessions import SessionManager
from test_server import call
from test_voice import voice_config, mint_fake


def test_real_http_voice_needs_no_bearer_is_passive_and_scoped(tmp_path,monkeypatch):
    config=voice_config(tmp_path)
    config.server.port=0
    monkeypatch.setenv('TESSERA_SERVER_TOKEN','server-fixture')
    server=create_server(config)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        server.manager.create('one')
        requests=mint_fake(monkeypatch,server.manager)
        assert call(server,'GET','/api/status')[1]['voice']['active_count']==0
        assert requests==[] and server.manager._runtimes=={}
        code,start=call(server,'POST','/api/sessions/one/voice/start',{})
        assert code==200 and len(requests)==1
        event={'type':'conversation.item.input_audio_transcription.completed','item_id':'in','transcript':'A spoken question'}
        body={'voice_id':start['voice_id'],'events':[event]}
        code,result=call(server,'POST','/api/sessions/one/voice/events',body)
        assert code==200 and result['messages'][0]['text']=='A spoken question'
        assert call(server,'POST','/api/sessions/one/voice/events',body)[1]['messages']==[]
        status=call(server,'GET','/api/status')[1]['voice']
        assert status=={'enabled':True,'configured':True,'active_count':1,'voices':[],'voice':'','pipe':'duplex','pipes':{'duplex':True,'local':False}}
        assert 'secret' not in json.dumps(status) and 'voice.example.com' not in json.dumps(status)
        assert call(server,'POST','/api/sessions/one/voice/stop',{'voice_id':start['voice_id']})==(200,{'stopped':True})
        assert call(server,'POST','/api/sessions/one/voice/events',body)[0]==409
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_mint_rejects_reflected_keys_expired_overlong_tokens_and_redirects(tmp_path,monkeypatch):
    from tessera.voice import VoiceClient, VoiceError
    config=voice_config(tmp_path)
    monkeypatch.setenv('EXAMPLE_VOICE_KEY','fixture-api-key')
    responses=[
        {'value':'fixture-api-key','expires_at':160},
        {'value':'ephemeral','expires_at':99},
        {'value':'ephemeral','expires_at':1000},
        {'value':'bad token','expires_at':160},
        {'value':'ephemeral','expires_at':float('nan')},
    ]
    for response in responses:
        client=VoiceClient(config.voice,clock=lambda:100,
                           opener=lambda *args,**kw:io.BytesIO(json.dumps(response).encode()))
        with pytest.raises(VoiceError) as error:
            client.mint()
        assert error.value.category=='invalid_response' and 'fixture-api-key' not in str(error.value)
    client=VoiceClient(config.voice,clock=lambda:100,
        opener=lambda *args,**kw:(_ for _ in ()).throw(RuntimeError('fixture-api-key private-endpoint')))
    with pytest.raises(VoiceError) as error:
        client.mint()
    assert error.value.category=='upstream' and 'private' not in str(error.value)


def test_session_options_and_transcripts_cannot_expose_resolved_key_or_ephemeral(tmp_path,monkeypatch):
    config=voice_config(tmp_path)
    config.voice.session_options={'voice':'fixture-voice-api-key'}
    with SessionManager(config) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        assert 'fixture-voice-api-key' not in json.dumps(start)
        result=manager.voice.events('one',start['voice_id'],[
            {'type':'conversation.item.input_audio_transcription.completed','item_id':'in',
             'transcript':'fixture-voice-api-key ephemeral-fixture'}
        ])
        assert result['messages'][0]['text']=='[REDACTED] [REDACTED]'
        assert all('fixture-voice-api-key' not in p.read_text() and 'ephemeral-fixture' not in p.read_text()
                   for p in tmp_path.rglob('*.jsonl'))


def test_risky_voice_tool_keeps_existing_approval_and_never_spawns(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    config=voice_config(tmp_path)
    config.voice.tools=['shell']
    config.tools.approval='deny_risky'
    with SessionManager(config,backend_factory=lambda *a:FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        spawn=Mock(side_effect=AssertionError('Denied tool'))
        with patch('subprocess.Popen',spawn):
            result=manager.voice.events('one',start['voice_id'],[
                {'type':'response.function_call_arguments.done','response_id':'r','call_id':'c','name':'shell','arguments':'{"command":"fixture"}'}
            ])
        spawn.assert_not_called()
        assert json.loads(result['send'][0]['item']['output']).get('error')
        assert not [r for r in manager.get('one').log.iter() if r.kind=='echo' and r.meta.get('success')]


def test_function_response_does_not_continue_until_all_reported_calls_are_resolved(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    config=voice_config(tmp_path)
    config.voice.tools=['date']
    with SessionManager(config,backend_factory=lambda *a:FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        first={'type':'response.function_call_arguments.done','response_id':'r','call_id':'c1','name':'date','arguments':'{}'}
        done={'type':'response.done','response':{'id':'r','status':'completed','output':[
            {'type':'function_call','call_id':'c1'},{'type':'function_call','call_id':'c2'}]}}
        result=manager.voice.events('one',start['voice_id'],[first,done])
        assert [e['type'] for e in result['send']]==['conversation.item.create']
        second={**first,'call_id':'c2'}
        result=manager.voice.events('one',start['voice_id'],[second])
        assert [e['type'] for e in result['send']]==['conversation.item.create','response.create']


def test_busy_tool_call_does_not_wait_past_voice_lease(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    from tessera.voice import VoiceError
    config=voice_config(tmp_path)
    config.voice.tools=['date']
    with SessionManager(config,backend_factory=lambda *a:FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        entered=threading.Event()
        release=threading.Event()
        def hold():
            with manager.get('one')._turn_lock:
                entered.set()
                release.wait(2)
        thread=threading.Thread(target=hold)
        thread.start()
        assert entered.wait(1)
        try:
            with pytest.raises(VoiceError) as error:
                manager.voice.events('one',start['voice_id'],[
                    {'type':'response.function_call_arguments.done','response_id':'r','call_id':'c','name':'date','arguments':'{}'}])
            assert error.value.category=='busy'
        finally:
            release.set()
            thread.join()


def test_expiry_during_a_batch_stops_before_the_next_transcript(tmp_path,monkeypatch):
    from tessera.voice import VoiceError
    from tessera.log import ChatLog
    config=voice_config(tmp_path)
    config.voice.max_session_seconds=1
    with SessionManager(config) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        original=ChatLog.append
        def append(log,*args,**kwargs):
            result=original(log,*args,**kwargs)
            manager.voice.clock=lambda:102
            return result
        with patch.object(ChatLog,'append',append):
            with pytest.raises(VoiceError) as error:
                manager.voice.events('one',start['voice_id'],[
                    {'type':'conversation.item.input_audio_transcription.completed','item_id':'first','transcript':'first'},
                    {'type':'conversation.item.input_audio_transcription.completed','item_id':'second','transcript':'second'}])
            assert error.value.category=='expired'
        with ChatLog(manager.path('one')/'log') as log:
            assert [r.text for r in log.iter()]==['first']


def test_loaded_tool_log_redacts_lazy_credentials_and_uncertain_calls_are_not_retried(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    config=voice_config(tmp_path)
    config.voice.tools=['voice_fixture']
    with SessionManager(config,backend_factory=lambda *a:FakeBackend([])) as manager:
        manager.create('one')
        session=manager.get('one')
        executed=[]
        def handler():
            executed.append(True)
            raise RuntimeError('fixture-voice-api-key ephemeral-fixture private-diagnostic')
        session.tools.register('voice_fixture','Fixture function',{'type':'object','properties':{}},handler)
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        event={'type':'response.function_call_arguments.done','response_id':'r','call_id':'c','name':'voice_fixture','arguments':'{}'}
        first=manager.voice.events('one',start['voice_id'],[event])
        second=manager.voice.events('one',start['voice_id'],[event])
        assert first['send']==second['send'] and len(executed)==1
        assert 'private-diagnostic' not in json.dumps(first['send'])
        assert all('fixture-voice-api-key' not in p.read_text() and 'ephemeral-fixture' not in p.read_text()
                   for p in tmp_path.rglob('*.jsonl'))


def test_rotated_selected_env_key_is_redacted_before_tool_logs_and_replies(tmp_path,monkeypatch):
    from fake_backend import FakeBackend
    import os
    config=voice_config(tmp_path)
    config.voice.tools=['voice_fixture']
    with SessionManager(config,backend_factory=lambda *a:FakeBackend([])) as manager:
        manager.create('one')
        session=manager.get('one')
        session.tools.register('voice_fixture','Fixture',{'type':'object','properties':{}},
                               lambda:os.environ['EXAMPLE_VOICE_KEY'])
        mint_fake(monkeypatch,manager)
        start=manager.voice.start('one')
        monkeypatch.setenv('EXAMPLE_VOICE_KEY','rotated-fixture-key')
        result=manager.voice.events('one',start['voice_id'],[
            {'type':'response.function_call_arguments.done','response_id':'r','call_id':'c','name':'voice_fixture','arguments':'{}'}])
        assert result['send'][0]['item']['output']=='[REDACTED]'
        assert all('rotated-fixture-key' not in p.read_text() for p in tmp_path.rglob('*.jsonl'))


def test_default_mint_transport_never_follows_redirects(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from tessera.voice import VoiceClient,VoiceError
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header('Location','/must-not-follow')
            self.send_header('Content-Length','0')
            self.end_headers()
        def do_GET(self):
            seen.append(self.path)
            self.send_response(500)
            self.send_header('Content-Length','0')
            self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    config=voice_config(tmp_path)
    config.voice.api_base_url='http://127.0.0.1:'+str(server.server_address[1])
    monkeypatch.setenv('EXAMPLE_VOICE_KEY','fixture-api-key')
    try:
        with pytest.raises(VoiceError) as error:
            VoiceClient(config.voice).mint()
        assert error.value.category=='upstream' and seen==[config.voice.client_secret_path]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('fragment',[
    '[voice]\nwebsocket_url="wss://voice.example.com?token=placeholder"',
    '[voice]\nclient_protocol_prefix="invalid space"',
    '[voice]\nmax_session_seconds=0',
    '[voice]\nmodel="placeholder"\nclient_secret_path="//other.example.com"',
    '[voice]\ntools=["date","date"]',
    '[voice.session_options]\ntools=[]',
])
def test_invalid_voice_configuration_is_rejected_without_io(tmp_path,fragment):
    (tmp_path/'config.toml').write_text(fragment)
    with pytest.raises(ValueError):
        load_config(tmp_path)


@pytest.mark.parametrize('audio',[[], 'invalid', {'input':[]}, {'output':{'format':'invalid'}}])
def test_audio_settings_shape_fails_before_mint(tmp_path,audio):
    config=voice_config(tmp_path)
    config.voice.session_options={'audio':audio}
    with pytest.raises(ValueError):
        config.validate()


def test_changed_mint_configuration_cannot_retarget_an_ephemeral_lease(tmp_path,monkeypatch):
    from tessera.voice import VoiceError
    config=voice_config(tmp_path)
    with SessionManager(config) as manager:
        manager.create('one')
        mint_fake(monkeypatch,manager)
        def changed(*args,**kwargs):
            config.voice.websocket_url='wss://changed.example.com/realtime'
            return io.BytesIO(b'{"value":"ephemeral-fixture","expires_at":160}')
        manager.voice.client.opener=changed
        with pytest.raises(VoiceError) as error:
            manager.voice.start('one')
        assert error.value.category=='configuration'
        assert manager.voice.status()['active_count']==0


def test_named_reference_credentials_are_wired_to_the_lazy_voice_client(tmp_path,monkeypatch):
    from tessera.credentials import CredentialReference
    config=voice_config(tmp_path)
    config.voice.api_key_env=''
    config.voice.credential_ref='voice-reference'
    config.credentials={'voice-reference':CredentialReference(env='EXAMPLE_REF_VOICE_KEY')}
    monkeypatch.setenv('EXAMPLE_REF_VOICE_KEY','reference-key-fixture')
    with SessionManager(config) as manager:
        manager.create('one')
        manager.voice.clock=lambda:100
        manager.voice.client.clock=lambda:100
        seen=[]
        def send(request,timeout):
            seen.append(request.get_header('Authorization'))
            return io.BytesIO(b'{"value":"ephemeral-fixture","expires_at":160}')
        manager.voice.client.opener=send
        started=manager.voice.start('one')
        assert seen==['Bearer '+'reference-key-fixture']
        assert 'reference-key-fixture' not in json.dumps(started)


def test_voice_pick_is_limited_to_configured_voices(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.server.port = 0
    config.voice.voices = ['Rex', 'Ara']
    config.voice.session_options = {'voice': 'Rex'}
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.manager.create('one')
        mint_fake(monkeypatch, server.manager)
        status = call(server, 'GET', '/api/status')[1]['voice']
        assert status['voices'] == ['Rex', 'Ara'] and status['voice'] == 'Rex'
        code, start = call(server, 'POST', '/api/sessions/one/voice/start', {'voice': 'Ara'})
        assert code == 200 and start['session_update']['session']['voice'] == 'Ara'
        call(server, 'POST', '/api/sessions/one/voice/stop', {'voice_id': start['voice_id']})
        code, start = call(server, 'POST', '/api/sessions/one/voice/start', {})
        assert code == 200 and start['session_update']['session']['voice'] == 'Rex'
        call(server, 'POST', '/api/sessions/one/voice/stop', {'voice_id': start['voice_id']})
        assert call(server, 'POST', '/api/sessions/one/voice/start', {'voice': 'Nobody'})[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
