"""Start the real serve CLI with a local FakeBackend HTTP fixture and probe it."""

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from fake_backend import FakeBackend


def _probe_text(messages):
    for message in messages:
        content = message.get('content')
        if message.get('role') == 'user' and isinstance(content, str) and 'harness-probe' in content:
            return content
    return ''


def _harness_probe(messages, probe):
    last = messages[-1]
    if last.get('role') != 'tool':
        return {'tool_calls': [{'id': 'write-probe', 'name': 'write_file',
                                'arguments': {'path': 'harness-probe.txt', 'text': 'alpha\n'}}]}
    content = last.get('content') or ''
    if 'Wrote' in content:
        return {'tool_calls': [{'id': 'edit-probe', 'name': 'edit_file',
                                'arguments': {'path': 'harness-probe.txt', 'old': 'alpha', 'new': 'beta'}}]}
    if content.startswith('Edited'):
        patch = '*** Begin Patch\n*** Update File: harness-probe.txt\n@@\n-beta\n+gamma\n*** End Patch'
        return {'tool_calls': [{'id': 'patch-probe', 'name': 'apply_patch', 'arguments': {'patch': patch}}]}
    if 'Updated ' in content:
        return {'tool_calls': [{'id': 'shell-probe', 'name': 'shell', 'arguments': {'command': 'printf probe-shell'}}]}
    if 'probe-shell' in content:
        return {'tool_calls': [{'id': 'process-probe', 'name': 'process',
                                'arguments': {'action': 'start', 'command': 'printf probe-process'}}]}
    if '"pid"' in content:
        match = re.search(r'"id": "([0-9a-f]+)"', content)
        return {'tool_calls': [{'id': 'poll-probe', 'name': 'process',
                                'arguments': {'action': 'poll', 'id': match.group(1) if match else ''}}]}
    if 'probe-process' in content or '"running"' in content:
        match = re.search(r'https?://\S+', probe)
        return {'tool_calls': [{'id': 'fetch-probe', 'name': 'web_fetch', 'arguments': {'url': match.group(0)}}]}
    return {'content': 'Harness probe finished.'}


def scripted_reply(messages, tools):
    probe = _probe_text(messages)
    if probe:
        return _harness_probe(messages, probe)
    last = messages[-1]
    if last['role'] == 'tool':
        return {'content': 'The tool completed: ' + last['content']}
    text = last.get('content', '')
    if 'send_to_session' in text:
        return {'tool_calls': [{'id': 'send-' + uuid.uuid4().hex, 'name': 'send_to_session',
                                'arguments': {'slug': 'target', 'text': 'Hello from the source session'}}]}
    if 'read_file' in text:
        return {'tool_calls': [{'id': 'read-' + uuid.uuid4().hex, 'name': 'read_file',
                                'arguments': {'path': 'fact.txt'}}]}
    return {'content': 'Local fake reply: ' + text}


@contextmanager
def model_fixture():
    backend = FakeBackend([scripted_reply] * 200)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            reply = backend.chat(body['messages'], tools=body.get('tools'))
            message = {'role': 'assistant', 'content': reply['content']}
            if reply['tool_calls']:
                message['tool_calls'] = []
                for call in reply['tool_calls']:
                    message['tool_calls'].append({'id': call['id'], 'type': 'function', 'function': {
                        'name': call['name'], 'arguments': json.dumps(call['arguments'])}})
            raw = json.dumps({'choices': [{'message': message}], 'usage': {}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def start_server(home, model_url):
    # Ambient live-home overrides must never retarget this offline fixture.
    env = {}
    for name, value in os.environ.items():
        if not name.startswith('TESSERA_'):
            env[name] = value
    env.update(TESSERA_HOME=str(home), TESSERA_CWD=str(home),
               TESSERA_BACKENDS__FAKE__BASE_URL=model_url)
    process = subprocess.Popen([sys.executable, '-m', 'tessera', 'serve'], cwd=ROOT, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    ready, _, _ = select.select([process.stdout], [], [], 10)
    if not ready:
        process.kill()
        process.wait()
        raise RuntimeError('Serve CLI did not start in ten seconds')
    line = process.stdout.readline().strip()
    if not line.startswith('Serving Tessera at '):
        process.kill()
        process.wait()
        raise RuntimeError('Serve CLI failed to announce its address')
    return process, line.removeprefix('Serving Tessera at ')


def stop_server(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError('Serve CLI did not shut down cleanly') from None
    if process.returncode != 0:
        raise RuntimeError('Serve CLI exited unsuccessfully')


class Probe:
    def __init__(self, url):
        parsed = urlsplit(url)
        self.address = (parsed.hostname, parsed.port)

    def request(self, method, path, body=None, json_result=True):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        headers = {'Content-Type': 'application/json'}
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        status = response.status
        connection.close()
        return status, json.loads(raw) if json_result else raw

    def wait_turn(self, slug, turn_id):
        connection = http.client.HTTPConnection(*self.address, timeout=10)
        connection.request('GET', f'/api/sessions/{slug}/events')
        response = connection.getresponse()
        assert response.status == 200
        events = []
        current = {}
        try:
            while True:
                line = response.readline().decode().rstrip('\r\n')
                if line.startswith('id: '):
                    current['id'] = int(line[4:])
                elif line.startswith('event: '):
                    current['event'] = line[7:]
                elif line.startswith('data: '):
                    current['data'] = json.loads(line[6:])
                elif not line and current:
                    events.append(current)
                    if current['event'] == 'turn_end' and current['data']['turn_id'] == turn_id:
                        assert current['data']['success']
                        return events
                    current = {}
        finally:
            connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'demo' / 'stage1_smoke.txt')
    args = parser.parse_args(argv)
    notes = ['Stage 1 integration smoke', 'Backend: local HTTP fixture powered by scripted FakeBackend',
             'Server: real python -m tessera serve subprocess; ephemeral loopback ports', 'Config: demo/fake_config.toml']
    with tempfile.TemporaryDirectory(prefix='stage1-', dir=ROOT / 'demo') as directory, model_fixture() as (model_url, backend):
        home = Path(directory)
        (home / 'config.toml').write_bytes((ROOT / 'demo' / 'fake_config.toml').read_bytes())
        (home / 'fact.txt').write_text('turquoise', encoding='utf-8')
        process, url = start_server(home, model_url)
        try:
            probe = Probe(url)
            assert probe.request('GET', '/api/status')[0] == 200
            notes.append('PASS desktop loopback access: JSON and SSE need no bearer token')
            for path in ('/', '/static/app.js', '/static/theme.css', '/static/app.css'):
                status, data = probe.request('GET', path, json_result=False)
                assert status == 200 and data
            notes.append('PASS UI delivery: HTML, JavaScript, theme and layout CSS served')
            for title in ('source', 'target'):
                assert probe.request('POST', '/api/sessions', {'title': title})[0] == 201
            _, queued = probe.request('POST', '/api/sessions/source/messages', {'text': 'Use read_file to inspect fact.txt'})
            events = probe.wait_turn('source', queued['turn_id'])
            assert {'turn_start', 'message', 'route', 'capture', 'turn_end'} <= {event['event'] for event in events}
            assert any(event['event'] == 'message' and event['data']['kind'] == 'echo' and 'turquoise' in event['data']['text'] for event in events)
            notes.append('PASS asynchronous turn and SSE: read_file outcome, route, capture and completion observed')
            checkpoint = events[0]['id']
            connection = http.client.HTTPConnection(*probe.address, timeout=5)
            connection.request('GET', '/api/sessions/source/events', headers={'Last-Event-ID': str(checkpoint)})
            response = connection.getresponse()
            assert response.status == 200
            assert response.readline().decode().strip() == 'id: ' + str(checkpoint + 1)
            connection.close()
            notes.append('PASS Last-Event-ID resumes strictly after the acknowledged event')
            _, queued = probe.request('POST', '/api/sessions/source/messages', {'text': 'Use send_to_session to deliver the result to target'})
            probe.wait_turn('source', queued['turn_id'])
            deadline = time.monotonic() + 5
            while True:
                _, messages = probe.request('GET', '/api/sessions/target/messages')
                if any(message['kind'] == 'agent' for message in messages):
                    break
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert messages[0]['kind'] == 'work' and messages[0]['text'].startswith('[source]')
            notes.append('PASS cross-session messaging: target received the source work message and replied')
            _, view = probe.request('GET', '/api/sessions/source/view')
            assert view['lines'] and view['bytes'] == 14 + sum(row['bytes'] for row in view['lines'])
            _, zoom = probe.request('GET', '/api/sessions/source/zoom?start=0&count=1')
            assert zoom['text'] == 'Use read_file to inspect fact.txt'
            notes.append('PASS structured view snapshot and original-message zoom')
            assert probe.request('PATCH', '/api/sessions/source', {'title': 'Renamed source', 'archived': True})[0] == 200
            assert probe.request('POST', '/api/sessions/source/messages', {'text': 'Blocked'})[0] == 409
            assert probe.request('PATCH', '/api/sessions/source', {'archived': False})[0] == 200
            _, before = probe.request('GET', '/api/sessions/source/messages')
            stop_server(process)
            process, url = start_server(home, model_url)
            probe = Probe(url)
            _, sessions = probe.request('GET', '/api/sessions')
            assert sessions[0]['title'] == 'Renamed source'
            _, after = probe.request('GET', '/api/sessions/source/messages')
            assert before == after
            _, queued = probe.request('POST', '/api/sessions/source/messages', {'text': 'Continue after restart'})
            probe.wait_turn('source', queued['turn_id'])
            notes.append('PASS restart: title, history, queue/capture identity preserved and next turn completes')
            _, status = probe.request('GET', '/api/status')
            assert status['debt_total'] >= 3
            assert status['sessions']['source']['inheritance']
            notes.append('PASS status: per-session inheritance and offline write-debt surfaced')
            notes.extend(['Model calls: ' + str(len(backend.calls)), 'PASS all assertions; no live model or memory endpoint used'])
            args.output.write_text('\n'.join(notes) + '\n', encoding='utf-8')
            print('\n'.join(notes), flush=True)

        finally:
            stop_server(process)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
