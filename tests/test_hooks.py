import sys
import shlex

from tessera.hooks import HookBus


def test_shell_hook_receives_json_and_blocks_pre_tool(tmp_path):
    script = tmp_path / 'hook.py'
    script.write_text('import json,sys\np=json.load(sys.stdin)\nprint("blocked " + p["name"])\nsys.exit(2)\n')
    bus = HookBus([{'event': 'pre_tool', 'command': f'{shlex.quote(sys.executable)} {shlex.quote(str(script))}'}])
    result = bus.emit('pre_tool', {'name': 'shell'})
    assert result[0]['block'] is True
    assert 'blocked shell' in result[0]['reason']


def test_hook_failures_block_pre_tool_but_other_hooks_still_run():
    bus = HookBus()
    def broken(payload):
        raise ValueError('hook failed')
    bus.register('pre_tool', broken)
    bus.register('pre_tool', lambda payload: {'context': 'second hook ran'})
    results = bus.emit('pre_tool', {})
    assert results[0]['block'] is True
    assert results[1]['context'] == 'second hook ran'


def test_hook_timeout_terminates_descendants(tmp_path):
    import time
    script = tmp_path / 'late.py'
    marker = tmp_path / 'orphan-marker'
    script.write_text('import time\nfrom pathlib import Path\ntime.sleep(.2)\nPath(' + repr(str(marker)) + ').write_text("orphaned")\n')
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(script))} & wait'
    bus = HookBus([{'event': 'pre_tool', 'command': command, 'timeout': .05}])
    results = bus.emit('pre_tool', {})
    assert results[0]['block'] is True
    time.sleep(.3)
    assert not marker.exists()


def test_conscience_hook_blocks_a_clear_no_and_opens_a_lesson(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tessera.jev import JudgeKit
    from tessera.learning import LearningStore
    router = JudgeKit(enabled=False)
    router.computer_use_jev = SimpleNamespace(enabled=True, credential_ref='', api_key_env='TESSERA_TEST')
    router.credentials = None
    # Consciousness is one lane: the floor. Bind the router to the floor thread.
    router.bind_session(tmp_path / 'infinity', None, floor_slug='infinity')
    router.chat_log = SimpleNamespace(iter=lambda: [SimpleNamespace(
        kind='user', text='Salience 1.8. Pressure: node-a changed healthy -> unreachable.')])
    monkeypatch.setattr('tessera.cua_jev.api_key', lambda settings, credentials: 'key')
    monkeypatch.setattr('tessera.cua_jev.evaluate', lambda *args: {'answers_pressure': {'type': 'noul', 'noul': 0.05}})
    store = LearningStore(tmp_path / 'lessons.json')
    opened = {}

    def record(slug, trigger, failure, check, source=''):
        opened['slug'] = slug
        opened['failure'] = failure
        return {'id': 'ls-test'}

    store.record_lesson = record
    session = SimpleNamespace(root=SimpleNamespace(name='infinity'), manager=SimpleNamespace(autonomy=SimpleNamespace(learning=store)))
    from tessera.loop import Session
    result = router.conscience_hook({'name': 'shell', 'arguments': {'command': 'rm -rf /'}})
    assert result['conscience'] is True
    assert result['block'] is True
    assert 'cloud Jev' in result['reason']
    Session._open_conscience_lesson(session, 'shell', result['reason'])
    assert opened['slug'] == 'infinity'
    assert 'cloud Jev' in opened['failure']


def test_a_normal_session_gets_no_per_tool_conscience_call(tmp_path, monkeypatch):
    """Autonomy gets consciousness; every other session does not.

    A normal session gets the turn-end judge and nothing else. The conscience hook is the
    floor's lane, so on a normal session it must not even reach the model.
    """
    from types import SimpleNamespace
    from tessera.jev import JudgeKit
    router = JudgeKit(enabled=False)
    router.computer_use_jev = SimpleNamespace(enabled=True, credential_ref='', api_key_env='TESSERA_TEST')
    router.credentials = None
    router.bind_session(tmp_path / 'operations', None, floor_slug='infinity')
    router.chat_log = SimpleNamespace(iter=lambda: [SimpleNamespace(
        kind='user', text='Salience 1.8. Pressure: node-a changed healthy -> unreachable.')])
    calls = []
    monkeypatch.setattr('tessera.cua_jev.api_key', lambda settings, credentials: calls.append('key') or 'key')
    monkeypatch.setattr('tessera.cua_jev.evaluate', lambda *args: calls.append('evaluate'))
    assert router.conscience_hook({'name': 'shell', 'arguments': {'command': 'uptime'}}) is None
    assert calls == []


def test_a_stale_pressure_line_in_the_log_does_not_rearm_the_hook(tmp_path, monkeypatch):
    """The old scan read any earlier pressure line and fired on every later tool call.

    The hook must read the pressure from the turn that is running.
    """
    from types import SimpleNamespace
    from tessera.jev import JudgeKit
    router = JudgeKit(enabled=False)
    router.computer_use_jev = SimpleNamespace(enabled=True, credential_ref='', api_key_env='TESSERA_TEST')
    router.credentials = None
    router.bind_session(tmp_path / 'infinity', None, floor_slug='infinity')
    router.chat_log = SimpleNamespace(iter=lambda: [
        SimpleNamespace(kind='user', text='Salience 1.8. Pressure: node-a changed healthy -> unreachable.'),
        SimpleNamespace(kind='agent', text='Answered the pressure.'),
        SimpleNamespace(kind='user', text='now do the board work'),
    ])
    monkeypatch.setattr('tessera.cua_jev.api_key', lambda settings, credentials: 'key')
    monkeypatch.setattr('tessera.cua_jev.evaluate', lambda *args: {'answers_pressure': {'type': 'noul', 'noul': 0.05}})
    assert router.conscience_hook({'name': 'shell', 'arguments': {'command': 'uptime'}}) is None
