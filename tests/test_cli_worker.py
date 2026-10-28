"""Official CLI protocols are exercised by disposable executables, never accounts."""

import json
import sys

import pytest

from tessera.backend import Backend, BackendError
from tessera.config import load_config
from tessera.credentials import CredentialResolver
from cli_fixtures import cli_dispatch, synchronize_cli_start, synthetic_codex_auth


def fake_cli(tmp_path, vendor, source):
    path = tmp_path / vendor
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)
    return str(path)


def test_codex_receives_the_selected_backend_model(tmp_path):
    command = fake_cli(tmp_path, 'codex', '''import json,sys
assert sys.argv[sys.argv.index('--model') + 1] == 'configured-model'
sys.stdin.read()
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Selected model used'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}))
''')
    backend = Backend('worker', '', 'configured-model', extra={
        'provider_kind': 'cli_worker', 'cli': {'vendor': 'codex', 'command': command, 'cwd': str(tmp_path),
                                             'auth_path': synthetic_codex_auth(tmp_path)}})
    assert backend.chat([])['content'] == 'Selected model used'
    assert backend.worker.settings.model == 'configured-model'


def test_codex_structured_bridge_dispatches_engine_tools(tmp_path):
    command = fake_cli(tmp_path, 'codex', '''import json,sys
from pathlib import Path
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert schema['properties']['tool_calls']['items']['properties']['name']['enum'] == ['read_file']
sys.stdin.read()
reply = {'final': None, 'tool_calls': [{'name':'read_file','arguments_json':'{"path":"example.txt"}'}]}
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(reply)}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}))
''')
    backend = Backend('worker', '', 'configured-model', extra={
        'provider_kind': 'cli_worker', 'cli': {'vendor': 'codex', 'command': command,
                                             'cwd': str(tmp_path), 'engine_tools': True,
                                             'auth_path': synthetic_codex_auth(tmp_path)}})
    tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}]
    result = backend.chat([{'role': 'user', 'content': 'Read the file'}], tools=tools)
    assert result['finish_reason'] == 'tool_calls'
    assert result['tool_calls'][0]['arguments'] == {'path': 'example.txt'}


def test_engine_bridge_preserves_operator_native_cli_feature_options(tmp_path):
    from tessera.cli_worker import CliWorker
    command = fake_cli(tmp_path, 'codex', 'pass')
    args = ['exec', '--json', '--sandbox', 'read-only', '-c', 'features.multi_agent=true', '-']
    worker = CliWorker({'vendor': 'codex', 'command': command, 'cwd': str(tmp_path),
                        'engine_tools': True, 'args': args})
    assert worker._argv() == [command] + args


def test_cli_pipe_backpressure_preserves_a_large_prompt(tmp_path, monkeypatch):
    import os
    command = fake_cli(tmp_path, 'codex', '''import json,sys
prompt = sys.stdin.read()
assert 'large-packet-' * 20000 in prompt
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Entire prompt received'}}))
print(json.dumps({'type':'turn.completed'}))
''')
    original_write = os.write
    pressure = []

    def write_with_pressure(descriptor, data):
        if len(data) > 100 and not pressure:
            pressure.append(True)
            raise BlockingIOError('Synthetic pipe is temporarily full')
        return original_write(descriptor, data)

    monkeypatch.setattr('tessera.cli_worker.os.write', write_with_pressure)
    backend = Backend('worker', '', '', extra={'provider_kind': 'cli_worker', 'cli': {
        'vendor': 'codex', 'command': command, 'cwd': str(tmp_path),
        'auth_path': synthetic_codex_auth(tmp_path)}})
    assert backend.chat([{'role': 'user', 'content': 'large-packet-' * 20000}])['content'] == 'Entire prompt received'
    assert pressure


@pytest.mark.parametrize("vendor,output", [
    ("codex", [{"type": "item.completed", "item": {"type": "agent_message", "text": "Worker complete"}},
               {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}]),
    ("claude", [{"type": "result", "subtype": "success", "is_error": False, "result": "Worker complete", "usage": {"input_tokens": 3}}]),
])
def test_configured_cli_selection_uses_stdin_and_no_credential_resolver(tmp_path, vendor, output, monkeypatch, cli_dispatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Subscription workers must not resolve API credentials")
    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    command = fake_cli(tmp_path, vendor, "import sys,json\nprompt=sys.stdin.read()\nassert 'Task input' in prompt and 'Synthetic task' in prompt\n" +
                       "\n".join("print(" + repr(json.dumps(item)) + ")" for item in output) + "\n")
    synchronize_cli_start(monkeypatch, command)
    auth_line = ''
    if vendor == 'codex':
        auth_line = 'auth_path = ' + json.dumps(synthetic_codex_auth(tmp_path)) + '\n'
    (tmp_path / "config.toml").write_text(f'''
[accounts.subscription]
provider_kind = "cli_worker"
[backends.worker]
account = "subscription"
[backends.worker.cli]
vendor = "{vendor}"
command = {json.dumps(command)}
cwd = {json.dumps(str(tmp_path))}
timeout = 2
{auth_line}''')
    settings = load_config(tmp_path).backend_for()
    backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
    result = backend.chat([{"role": "user", "content": "Synthetic task"}])
    assert result["content"] == "Worker complete" and result["tool_calls"] == []
    assert result["finish_reason"] == "stop"


@pytest.mark.parametrize("source,timeout,category", [
    ("import time;time.sleep(3)", .05, "timeout"),
    ("import sys;print('synthetic-private-output');sys.stderr.write('synthetic-private-error');sys.exit(4)", 1, "provider"),
    ("print('synthetic-invalid-json')", 1, "provider"),
])
def test_cli_timeout_nonzero_and_invalid_output_are_bounded_and_sanitized(tmp_path, source, timeout, category):
    command = fake_cli(tmp_path, "codex", source)
    backend = Backend("worker", "", "", extra={"provider_kind": "cli_worker", "cli": {
        "vendor": "codex", "command": command, "cwd": str(tmp_path), "timeout": timeout,
        "auth_path": synthetic_codex_auth(tmp_path)}})
    with pytest.raises(BackendError) as caught:
        backend.chat([{"role": "user", "content": "Synthetic task"}])
    assert caught.value.category == category
    assert "synthetic-private" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("vendor", ["codex", "claude"])
def test_ready_cli_still_enforces_the_call_deadline(tmp_path, vendor, monkeypatch):
    command = fake_cli(tmp_path, vendor,
                       "import sys,threading\nprompt=sys.stdin.read()\nthreading.Event().wait()\n")
    synchronize_cli_start(monkeypatch, command)
    cli = {"vendor": vendor, "command": command, "cwd": str(tmp_path), "timeout": .05}
    if vendor == "codex":
        cli["auth_path"] = synthetic_codex_auth(tmp_path)
    backend = Backend("worker", "", "", extra={"provider_kind": "cli_worker", "cli": cli})
    with pytest.raises(BackendError) as caught:
        backend.chat([{"role": "user", "content": "Synthetic task"}])
    assert caught.value.category == "timeout"
    assert backend.worker.settings.timeout == .05


def test_unset_cli_is_inert_and_configured_api_key_env_is_removed(tmp_path, monkeypatch):
    from tessera.cli_worker import CliWorker, WorkerError
    worker = CliWorker({})
    with pytest.raises(WorkerError, match="configured"):
        worker.chat([])
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-http-only")
    command = fake_cli(tmp_path, "claude", '''import os,json,sys
sys.stdin.read()
assert "OPENAI_API_KEY" not in os.environ
print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"No HTTP key inherited"}))
''')
    result = CliWorker({"vendor": "claude", "command": command, "cwd": str(tmp_path), "strip_env": []}).chat([])
    assert result["content"] == "No HTTP key inherited"


def test_cli_does_not_accept_api_credentials_or_unknown_vendor(tmp_path):
    (tmp_path / "config.toml").write_text('''
[backends.worker]
provider_kind = "cli_worker"
api_key_env = "EXAMPLE_KEY"
''')
    with pytest.raises(ValueError, match="CLI|cli"):
        load_config(tmp_path)


@pytest.mark.parametrize("events", [
    [{"type": "turn.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": "Earlier final"}},
     {"type": "turn.completed"}, {"type": "turn.started"},
     {"type": "item.completed", "item": {"type": "agent_message", "text": "Unfinished later turn"}}],
    [{"type": "item.completed", "item": []}, {"type": "turn.completed"}],
])
def test_codex_last_turn_must_complete_and_event_objects_must_be_valid(tmp_path, events):
    command = fake_cli(tmp_path, "codex", "\n".join("print(" + repr(json.dumps(event)) + ")" for event in events))
    backend = Backend("worker", "", "", extra={"provider_kind": "cli_worker", "cli": {
        "vendor": "codex", "command": command, "cwd": str(tmp_path),
        "auth_path": synthetic_codex_auth(tmp_path)}})
    with pytest.raises(BackendError, match="incomplete"):
        backend.chat([])


def test_claude_requires_an_object_and_returns_only_numeric_usage(tmp_path):
    from tessera.cli_worker import CliWorker, WorkerError
    command = fake_cli(tmp_path, "claude", "print('[]')")
    worker = CliWorker({"vendor": "claude", "command": command, "cwd": str(tmp_path)})
    with pytest.raises(WorkerError, match="invalid"):
        worker.chat([])
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "Complete",
              "usage": {"input_tokens": 3, "opaque": "synthetic-private-metadata"}}
    fake_cli(tmp_path, "claude", "print(" + repr(json.dumps(result)) + ")")
    assert worker.chat([])["usage"] == {"input_tokens": 3}


def test_cli_stderr_noise_is_bounded_tail_and_never_fails_or_leaks(tmp_path):
    # Recorded failure: Codex warning noise exceeded a 32KB combined cap in ~9s and was
    # misreported. Stderr is now a bounded tail for evidence only; stdout keeps the cap.
    result = {"type": "result", "subtype": "success", "is_error": False, "result": "Complete"}
    command = fake_cli(tmp_path, "claude", "import sys\nsys.stdin.read()\nsys.stderr.write('private-noise'*1000)\nprint(" + repr(json.dumps(result)) + ")")
    backend = Backend("worker", "", "", extra={"provider_kind": "cli_worker", "cli": {
        "vendor": "claude", "command": command, "cwd": str(tmp_path), "max_output_bytes": 1024}})
    reply = backend.chat([])
    assert reply["content"] == "Complete" and "private-noise" not in json.dumps(reply)
    assert len(backend.worker._stderr_tail) <= 4096


def test_cli_stdout_over_limit_is_configuration_error_with_evidence(tmp_path):
    command = fake_cli(tmp_path, "claude", "import sys\nsys.stdin.read()\nsys.stderr.write('why')\nprint('x'*5000)")
    backend = Backend("worker", "", "", extra={"provider_kind": "cli_worker", "cli": {
        "vendor": "claude", "command": command, "cwd": str(tmp_path), "max_output_bytes": 1024}})
    with pytest.raises(BackendError, match="limit") as caught:
        backend.chat([])
    assert caught.value.category == "configuration"
    assert backend.worker.last_failure["category"] == "configuration"


def test_grok_timeout_reports_progress_without_claiming_auth_path(tmp_path):
    from tessera.cli_worker import CliWorker, WorkerError
    command = fake_cli(tmp_path, 'grok', 'import time\ntime.sleep(5)\n')
    worker = CliWorker({'vendor': 'grok', 'command': command, 'cwd': str(tmp_path),
                        'timeout': 0.2, 'args': ['--prompt-file', '{prompt_file}', '--output-format', 'json']})
    with pytest.raises(WorkerError) as error:
        worker.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == 'timeout'
    assert 'before auth_path could be recorded' in str(error.value)
    assert worker.auth_path == ''


def test_stop_tolerates_a_group_leader_that_already_exited(monkeypatch):
    """_stop runs in a `finally`, so it must never raise over the real error.

    macOS returns EPERM from killpg() when the group leader has already been
    reaped, and ESRCH when it has not. Catching only ProcessLookupError meant a
    worker that exited before cleanup turned its own WorkerError into a bare
    PermissionError. Observed live: tests/test_cli_worker.py::test_cli_stdout_
    over_limit_is_configuration_error_with_evidence failed 5 of 30 runs while
    the machine was under load, and passed every time when it was idle.
    """
    import errno
    import os
    import signal

    from tessera.cli_worker import CliWorker

    class Gone:
        pid = 999999

    for code, label in ((errno.ESRCH, 'ProcessLookupError'), (errno.EPERM, 'PermissionError')):
        def fake_killpg(pid, sig, code=code):
            raise OSError(code, os.strerror(code))
        monkeypatch.setattr(os, 'killpg', fake_killpg)
        CliWorker._stop(Gone())  # must not raise


def test_codex_argv_defaults_to_read_only(tmp_path, monkeypatch):
    from tessera.cli_worker import CliWorker
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)
    worker = CliWorker({'vendor': 'codex', 'command': 'codex', 'cwd': str(tmp_path)})
    assert worker._argv() == ['codex', 'exec', '--json', '--sandbox', 'read-only', '-']


@pytest.mark.parametrize('argument', ['--sandbox', '-s', '--sandbox=read-only', '-s=read-only'])
def test_explicit_codex_sandbox_overrides_custom_args(tmp_path, monkeypatch, argument):
    from tessera.cli_worker import CliWorker
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)
    args = ['exec', '--json', argument]
    if '=' not in argument:
        args.append('read-only')
    args.append('-')
    worker = CliWorker({'vendor': 'codex', 'command': 'codex', 'cwd': str(tmp_path),
                        'args': args, 'sandbox': 'workspace-write'})
    assert worker._argv() == ['codex', 'exec', '--sandbox', 'workspace-write', '--json', '-']


@pytest.mark.parametrize('args', [
    ['exec', '--json', '--sandbox', '-'],
    ['exec', '--json', '--sandbox', 'resume', '-'],
    ['exec', '--json', 'fork', '-'],
    ['exec', '--json', '--last', '-'],
])
def test_sandbox_override_preserves_fresh_stdin_validation(tmp_path, monkeypatch, args):
    from tessera.cli_worker import CliWorker, WorkerError
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)
    worker = CliWorker({'vendor': 'codex', 'command': 'codex', 'cwd': str(tmp_path),
                        'args': args, 'sandbox': 'workspace-write'})
    with pytest.raises(WorkerError, match='fresh noninteractive'):
        worker._argv()


def test_codex_init_eperm_reports_sandbox_permission(tmp_path):
    stderr = ('WARNING: proceeding, even though we could not create PATH aliases: Operation not permitted\n'
              'Error: failed to initialize in-process app-server client: Operation not permitted (os error 1)\n')
    command = fake_cli(tmp_path, 'codex',
                       'import sys\nsys.stderr.write(' + repr(stderr) + ')\nsys.exit(1)\n')
    backend = Backend('worker', '', '', extra={'provider_kind': 'cli_worker', 'cli': {
        'vendor': 'codex', 'command': command, 'cwd': str(tmp_path),
        'auth_path': synthetic_codex_auth(tmp_path)}})
    with pytest.raises(BackendError) as caught:
        backend.chat([])
    assert caught.value.category == 'permission'
    assert caught.value.diagnostics()['error_kind'] == 'permission'
    assert 'sandbox' in str(caught.value).lower()
    assert 'permission' in str(caught.value).lower()
    assert backend.worker.last_failure['stderr_tail'] == stderr


def test_configured_codex_without_a_grant_file_does_not_spawn(tmp_path):
    """A usable worker still refuses before the subprocess when its grant file is absent.

    The path is explicit. The default remains ~/.codex/auth.json, and this test
    must not pass or fail according to whether the developer machine has one.
    """
    marker = tmp_path / 'spawned'
    command = fake_cli(tmp_path, 'codex',
                       'from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("yes")\n')
    from tessera.cli_worker import CliWorker, WorkerError
    worker = CliWorker({'vendor': 'codex', 'command': command, 'cwd': str(tmp_path),
                        'auth_path': str(tmp_path / 'absent-auth.json')})
    with pytest.raises(WorkerError, match='OAuth grant') as error:
        worker.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == 'configuration'
    assert 'missing' in str(error.value)
    assert not marker.exists()
