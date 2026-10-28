"""Official Grok headless protocol and explicit, secret-safe auth fallback."""

import json
import os
import time

import pytest

from test_cli_worker import fake_cli
from tessera.backend import Backend, BackendError
from tessera.credentials import CredentialResolver


def provider(tmp_path, source, **settings):
    command = fake_cli(tmp_path, 'grok', source)
    return Backend('fixture', '', '', extra={'provider_kind': 'cli_worker',
        'cli': {'vendor': 'grok', 'command': command, 'cwd': str(tmp_path), 'timeout': 3, **settings},
        'credential_sources': {'fixture': {'env': 'FIXTURE_GROK_KEY'}}})


def test_grok_uses_prompt_file_and_cli_login_without_resolving_key(tmp_path, monkeypatch):
    monkeypatch.setenv('XAI_API_KEY', 'fixture-private-key')
    monkeypatch.setattr(CredentialResolver, 'resolve', lambda *a, **k: pytest.fail('OAuth must not resolve an API key'))
    backend = provider(tmp_path, '''import os,sys,json
assert 'XAI_API_KEY' not in os.environ
p=sys.argv[sys.argv.index('--prompt-file')+1]
assert 'Synthetic task' in open(p).read()
assert os.stat(p).st_mode & 0o777 == 0o600
print(json.dumps({'text':'Grok fixture complete','stopReason':'end_turn','usage':{'total_tokens':3}}))
''', fallback_credential_ref='fixture', fallback_env='XAI_API_KEY')
    result = backend.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert result['content'] == 'Grok fixture complete' and result['usage']['total_tokens'] == 3
    assert backend.worker.auth_path == 'cli_login'


def test_grok_auth_denial_can_use_explicit_env_reference_without_leaking_key(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_GROK_KEY', 'fixture-private-key')
    backend = provider(tmp_path, '''import os,sys,json
key=os.environ.get('XAI_API_KEY')
if not key:
 print(json.dumps({'error':{'status':403}}));sys.exit(1)
print(json.dumps({'text':'Result '+key,'stopReason':'end_turn','usage':{'total_tokens':4}}))
''', fallback_credential_ref='fixture', fallback_env='XAI_API_KEY', allow_api_key_fallback=True)
    result = backend.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert result['content'] == 'Result [REDACTED]'
    assert backend.worker.auth_path == 'api_key_fallback'
    assert os.environ.get('XAI_API_KEY') != 'fixture-private-key'


@pytest.mark.parametrize('reason', ['max_turns', 'cancelled', None])
def test_grok_incomplete_result_cannot_be_worker_success(tmp_path, reason):
    backend = provider(tmp_path, 'import json\nprint(' + repr(json.dumps({'text':'Partial', 'stopReason':reason})) + ')\n')
    with pytest.raises(BackendError):
        backend.chat([{'role': 'user', 'content': 'Synthetic task'}])


def test_auth_error_without_configured_fallback_stays_actionable_and_secret_safe(tmp_path):
    backend = provider(tmp_path, 'import sys\nsys.stderr.write("HTTP 403 fixture-private-error");sys.exit(1)\n')
    with pytest.raises(BackendError) as error:
        backend.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == 'authentication'
    assert 'grok login' in str(error.value) and 'fixture-private-error' not in str(error.value)


def test_grok_cli_chat_auth_failure_never_passes_ambient_xai_api_key(tmp_path, monkeypatch):
    """Login failure with XAI_API_KEY in the parent environment must not hand that key to the CLI.

    A configured fallback reference is not an opt-in. allow_api_key_fallback stays false,
    so the child is started once, without the key, and the chat fails as authentication.
    """
    monkeypatch.setenv('XAI_API_KEY', 'must-not-be-sent')
    seen = tmp_path / 'seen.txt'
    source = (
        'import os,sys\n'
        'prompt=open(sys.argv[sys.argv.index("--prompt-file")+1]).read()\n'
        f'open({str(seen)!r}, "w").write(repr(os.environ.get("XAI_API_KEY")) + "\\n" + prompt + "\\n" + "\\n".join(sys.argv))\n'
        'sys.stderr.write("HTTP 401 unauthorized\\n")\n'
        'sys.exit(1)\n'
    )
    backend = provider(tmp_path, source, fallback_credential_ref='fixture', fallback_env='XAI_API_KEY')
    with pytest.raises(BackendError) as error:
        backend.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == 'authentication'
    assert 'grok login' in str(error.value)
    assert 'must-not-be-sent' not in str(error.value)
    assert backend.last_auth == ''
    recorded = seen.read_text()
    assert recorded.startswith('None\n')
    assert 'must-not-be-sent' not in recorded


def test_grok_api_key_fallback_is_off_unless_explicitly_opted_in(tmp_path, monkeypatch):
    """A configured fallback reference alone never bills the API key; only allow_api_key_fallback does."""
    monkeypatch.setenv('FIXTURE_GROK_KEY', 'fixture-private-key')
    monkeypatch.setattr(CredentialResolver, 'resolve', lambda *a, **k: pytest.fail('Fallback is not opted in'))
    backend = provider(tmp_path, '''import os,sys,json
if not os.environ.get('XAI_API_KEY'):
 print(json.dumps({'error':{'status':401}}));sys.exit(1)
print(json.dumps({'text':'billed','stopReason':'end_turn'}))
''', fallback_credential_ref='fixture', fallback_env='XAI_API_KEY')
    with pytest.raises(BackendError) as error:
        backend.chat([{'role': 'user', 'content': 'Synthetic task'}])
    assert error.value.category == 'authentication' and 'grok login' in str(error.value)
    assert backend.last_auth == ''


STREAM = '''import json,sys,time
p=open(sys.argv[sys.argv.index('--prompt-file')+1]).read()
tool='"role": "tool"' not in p and 'engine executes them' in p
def out(e):
    print(json.dumps(e), flush=True)
out({'type':'thought','data':'Checking '})
out({'type':'thought','data':'the clock.'})
time.sleep(0.4)
if tool:
    out({'type':'text','data':'{"tool_calls": [{"name": "clock_now", '})
    out({'type':'text','data':'"arguments": {}}]}'})
else:
    out({'type':'text','data':'It is '})
    out({'type':'text','data':'noon.'})
time.sleep(0.4)
out({'type':'end','stopReason':'end_turn','usage':{'input_tokens':5}})
'''


def test_grok_stream_forwards_thought_and_prose_live_but_holds_back_tool_protocol(tmp_path):
    backend = provider(tmp_path, STREAM, args=['--prompt-file', '{prompt_file}', '--output-format', 'streaming-json'],
                       engine_tools=True)
    seen = []
    backend.on_delta = lambda kind, text: seen.append((kind, text, time.monotonic()))
    started = time.monotonic()
    result = backend.chat([{'role': 'user', 'content': 'time?'}])
    finished = time.monotonic()
    assert [(k, t) for k, t, _ in seen] == [('thought', 'Checking '), ('thought', 'the clock.'), ('text', 'It is '), ('text', 'noon.')]
    assert seen[0][2] - started < finished - started - 0.5  # thought arrived live, well before the end
    assert result['content'] == 'It is noon.' and result['reasoning'] == 'Checking the clock.'
    schema = [{'type': 'function', 'function': {'name': 'clock_now', 'parameters': {'type': 'object'}}}]
    seen.clear()
    result = backend.chat([{'role': 'user', 'content': 'time?'}], tools=schema)
    assert [k for k, _, _ in seen] == ['thought', 'thought']  # protocol JSON is never shown as prose
    assert result['tool_calls'][0]['name'] == 'clock_now' and backend.worker.on_delta is None


FINAL_STREAM = '''import json,sys,time
def out(e):
    print(json.dumps(e), flush=True)
for piece in ['{"fin', 'al": "It\\\\u2019s ', 'noon.\\\\n', 'Say \\\\"hi\\\\"', '."}']:
    out({'type':'text','data':piece})
    time.sleep(0.05)
out({'type':'end','stopReason':'end_turn'})
'''


def test_final_wrapper_streams_its_decoded_text_and_never_the_wrapper(tmp_path):
    backend = provider(tmp_path, FINAL_STREAM, args=['--prompt-file', '{prompt_file}', '--output-format', 'streaming-json'],
                       engine_tools=True)
    seen = []
    backend.on_delta = lambda kind, text: seen.append((kind, text))
    schema = [{'type': 'function', 'function': {'name': 'clock_now', 'parameters': {'type': 'object'}}}]
    result = backend.chat([{'role': 'user', 'content': 'time?'}], tools=schema)
    streamed = ''.join(text for kind, text in seen if kind == 'text')
    assert streamed == 'It\u2019s noon.\nSay "hi".' == result['content']
    assert not any('final' in text or '{' in text for _, text in seen)


@pytest.mark.parametrize('chunks', [['{"tool_calls": [{"name": "x", "arguments": {}}]}'], ['{', '"tool', '_calls": []}'],
                                    ['{"final": 3}'], ['{"answer": "no"}']])
def test_non_final_json_is_held_back_entirely(chunks):
    from tessera.cli_worker import FinalStream
    stream = FinalStream()
    assert ''.join(stream.feed(chunk) for chunk in chunks) == ''
