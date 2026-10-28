"""Voice delegates to the actual selected session without owning a second worker."""
import json
import threading
import time

import pytest

from fake_backend import FakeBackend
from tessera.sessions import SessionManager
from test_voice import voice_config, mint_fake


def transcript(text, item='spoken-1'):
    return {'type': 'conversation.item.input_audio_transcription.completed',
            'item_id': item, 'transcript': text}


def task(text, call='call-1'):
    return {'type': 'response.function_call_arguments.done', 'name': 'session_task',
            'call_id': call, 'response_id': 'response-' + call,
            'arguments': json.dumps({'action': 'submit', 'text': text})}


def test_handoff_uses_real_session_and_delivers_actual_result_once(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def work(messages, tools):
        entered.set()
        assert release.wait(3)
        return {'content': 'Completed the requested work'}

    backend = FakeBackend([work])
    config = voice_config(tmp_path)
    config.voice.tools_mode = 'handoff'
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        runtime = manager.runtime('one')
        runtime.session.system_prompt += '\nSESSION SPECIFIC INSTRUCTIONS'
        runtime.session.log.append('user', 'Earlier shared context')
        start = manager.voice.start('one')
        instructions = start['session_update']['session']['instructions']
        assert 'SESSION SPECIFIC INSTRUCTIONS' in instructions
        assert 'Earlier shared context' in instructions
        assert [tool['name'] for tool in start['session_update']['session']['tools']] == ['session_task']
        voice_id = start['voice_id']
        first = manager.voice.events('one', voice_id, [transcript('Please do the work'), task('Do the work')])
        output = json.loads(first['send'][0]['item']['output'])
        turn_id = output['turn_id']
        assert first['poll_after_ms'] == 1000
        assert entered.wait(2)
        # A second provider call for this same utterance must not enqueue twice.
        replay = manager.voice.events('one', voice_id, [task('Do the work', 'call-2')])
        assert json.loads(replay['send'][0]['item']['output'])['turn_id'] == turn_id
        during = manager.voice.events('one', voice_id, [transcript('Thanks', 'spoken-2')])
        assert during['send'] == []
        assert len(runtime.turns) == 1
        release.set()
        deadline = time.monotonic() + 3
        while manager.turn('one', turn_id)['state'] not in ('done', 'error'):
            assert time.monotonic() < deadline
            time.sleep(.01)
        completed = manager.voice.events('one', voice_id, [])
        assert completed['send'][-1]['type'] == 'response.create'
        result = json.loads(completed['send'][1]['item']['output'])
        assert result['state'] == 'done'
        assert result['replies'] == ['Completed the requested work']
        assert manager.voice.events('one', voice_id, [])['send'] == []
        assert len(backend.calls) == 1
        assert any('Earlier shared context' in str(message) for message in backend.calls[0]['messages'])


def test_operator_stop_fences_late_voice_dispatch_and_new_speech_can_resume(tmp_path, monkeypatch):
    backend = FakeBackend(['Resumed'])
    config = voice_config(tmp_path)
    config.voice.tools_mode = 'handoff'
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        voice_id = manager.voice.start('one')['voice_id']
        manager.voice.events('one', voice_id, [transcript('Do work')])
        manager.interrupt('one')
        late = manager.voice.events('one', voice_id, [task('Do work')])
        assert 'predates operator Stop' in json.loads(late['send'][0]['item']['output'])['error']
        assert manager.runtime('one').operator_stopped
        assert not manager.runtime('one').turns
        fresh = manager.voice.events('one', voice_id, [transcript('Resume work', 'spoken-2'), task('Resume work', 'call-2')])
        assert 'turn_id' in json.loads(fresh['send'][0]['item']['output'])


def test_spoken_stop_cannot_be_dispatched_as_work(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.tools_mode = 'handoff'
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        voice_id = manager.voice.start('one')['voice_id']
        result = manager.voice.events('one', voice_id, [transcript('Stop'), task('Continue anyway')])
        assert 'error' in json.loads(result['send'][0]['item']['output'])
        assert not manager.runtime('one').turns


def test_audio_commit_allows_handoff_before_async_transcript_but_retains_stop_fence(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.tools_mode = 'handoff'
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['Done'])) as manager:
        manager.create('one')
        mint_fake(monkeypatch, manager)
        voice_id = manager.voice.start('one')['voice_id']
        committed = {'type': 'input_audio_buffer.committed', 'item_id': 'spoken-1'}
        result = manager.voice.events('one', voice_id, [committed, task('Do the work')])
        assert 'turn_id' in json.loads(result['send'][0]['item']['output'])
        manager.interrupt('one')
        late = manager.voice.events('one', voice_id, [transcript('Do the work'), task('Another task', 'call-2')])
        assert 'predates operator Stop' in json.loads(late['send'][0]['item']['output'])['error']
        assert manager.runtime('one').operator_stopped
