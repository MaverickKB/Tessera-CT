"""Four synthetic live HTTP turns, with a durable six-request ceiling.

Endpoint, model and home are supplied only through TESSERA_LIVE_BASE_URL,
TESSERA_LIVE_MODEL and TESSERA_HOME. This script never resets its budget.
"""

from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from demo.stage1_smoke import Probe
from demo.evidence import redact_text
from tessera import locking as fcntl
from tessera.backend import Backend
from tessera.durability import sync_directory
from tessera.config import load_config
from tessera.server import create_server


class CallBudget:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def claim(self):
        # Lock a separate inode so replacing the ledger remains atomic.
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            attempts = json.loads(self.path.read_text())['attempts'] if self.path.exists() else 0
            if type(attempts) is not int or not 0 <= attempts < 6:
                raise RuntimeError('Live model call budget exhausted or invalid')
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('w') as stream:
                json.dump({'attempts': attempts + 1, 'limit': 6}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            sync_directory(self.path.parent)
            return attempts + 1


class BudgetBackend:
    def __init__(self, delegate, budget):
        self.delegate = delegate
        self.budget = budget

    def chat(self, messages, tools=None, **kw):
        number = self.budget.claim()
        name = {1: 'read_file', 3: 'zoom'}.get(number)
        kw['tool_choice'] = {'type': 'function', 'function': {'name': name}} if name else 'none'
        return self.delegate.chat(messages, tools=tools, **kw)


def redact(text, url, home):
    parsed = urlsplit(url)
    replacements = [(str(home), '<TESSERA_HOME>')]
    for value in (url, parsed.netloc, parsed.hostname):
        if value:
            replacements.append((value, '<model-host>'))
    return redact_text(text, replacements)


def visible_answer(text):
    """Reject unfinished provider thinking and exclude it from smoke acceptance."""
    depth = 0
    for tag in re.finditer(r'<(/?)think>', text):
        depth = max(0, depth - 1) if tag[1] else depth + 1
    if depth:
        raise AssertionError('Provider returned unfinished thinking without a completed answer')
    return text.rsplit('</think>', 1)[-1].strip()


@contextmanager
def running_server(config, factory):
    server = create_server(config, backend_factory=factory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Probe(server.url)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def turn(probe, slug, text):
    status, queued = probe.request('POST', f'/api/sessions/{slug}/messages', {'text': text})
    assert status == 202, 'Turn was not accepted'
    # Poll the real HTTP surface; do not consume model calls for readiness.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        _, state = probe.request('GET', '/api/status')
        session = state['sessions'][slug]
        if not session['busy'] and not session['queued']:
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Turn did not settle within three minutes')
    _, messages = probe.request('GET', f'/api/sessions/{slug}/messages')
    probe.wait_turn(slug, queued['turn_id'])
    assert messages[-1]['kind'] == 'agent', 'Missing final answer'
    assert messages[-1]['meta'].get('success', True), 'Turn failed'
    assert visible_answer(messages[-1]['text']), 'Missing visible final answer'
    return queued['turn_id'], messages


def main():
    now = datetime.now(ZoneInfo('America/New_York'))
    if now.weekday() > 4 or not 9 <= now.hour < 17:
        raise RuntimeError('Live smoke requires weekday working hours')
    home = Path(os.environ['TESSERA_HOME']).expanduser().resolve()
    url = os.environ['TESSERA_LIVE_BASE_URL']
    model = os.environ['TESSERA_LIVE_MODEL']
    home.mkdir(parents=True, exist_ok=True)
    budget = CallBudget(home / 'stage1b-budget.json')
    if budget.path.exists():
        raise RuntimeError('An existing live budget cannot be reset by this harness')
    if (home / 'config.toml').exists():
        raise RuntimeError('Use a fresh authorized live home; existing config is preserved')
    if os.environ.get('TESSERA_LIVE_JEV_KEY'):
        raise RuntimeError('The core smoke requires the dedicated Jev credential to be unset')
    quote = json.dumps
    (home / 'config.toml').write_text(f'''cwd = {quote(str(home))}
user_instructions = "Keep answers brief. Use only the requested tool, once. Do not delegate."
[backends.live]
base_url = {quote(url)}
model = {quote(model)}
api_key_env = ""
timeout = 120
retries = 0
max_tokens = 400
[roles]
main = "live"
[jev]
enabled = true
api_key_env = "TESSERA_LIVE_JEV_KEY"
[aethermind]
[tools]
approval = "deny_risky"
[agents]
max_depth = 0
[server]
port = 0
''', encoding='utf-8')
    key = 'continuity-' + uuid.uuid4().hex[:12]
    (home / 'smoke_fact.txt').write_text(f'Synthetic continuity key: {key}\nColor: turquoise\n')
    config = load_config(home)
    assert config.jev.enabled and 'aethermind.enabled' not in config.retired_settings
    assert not config.jev.api_key_env or not os.environ.get(config.jev.api_key_env)
    assert 'compactor' not in config.roles and config.agents.max_depth == 0
    def factory(slug, name):
        item = config.backends[name]
        assert item.extra['retries'] == 0
        return BudgetBackend(Backend(item.name, item.base_url, item.model, '', item.extra), budget)
    with running_server(config, factory) as probe:
        code, session = probe.request('POST', '/api/sessions', {'title': 'Live smoke'})
        assert code == 201
        slug = session['slug']
        _, messages = turn(probe, slug, 'Use read_file exactly once on smoke_fact.txt with offset=0 and limit=4096 (bytes). Report its continuity key and color.')
        assert any(m['kind'] == 'echo' and key in m['text'] for m in messages)
        assert key in visible_answer(messages[-1]['text'])
        first = messages[0]['text']
        _, messages = turn(probe, slug, 'Use zoom exactly once with start=0 and count=1. What was my original request?')
        assert any(m['kind'] == 'echo' and first in m['text'] and m['meta'].get('turn') == 2 for m in messages)
        _, messages = turn(probe, slug, 'What is 7 + 5? Answer briefly without tools.')
        assert '12' in visible_answer(messages[-1]['text'])
        before = messages
    # A fresh server, manager, sessions, log and view replay the same disk state.
    with running_server(load_config(home), factory) as probe:
        _, restored = probe.request('GET', f'/api/sessions/{slug}/messages')
        assert restored == before
        _, messages = turn(probe, slug, 'From our earlier file result, what was the exact synthetic continuity key? Answer without tools.')
        assert key in visible_answer(messages[-1]['text'])
        _, status = probe.request('GET', '/api/status')
        assert status['debt_total'] >= 4
        _, view = probe.request('GET', f'/api/sessions/{slug}/view')
        assert view['lines']
    assert json.loads(budget.path.read_text())['attempts'] == 6
    print('PASS four live HTTP turns across restart; six completion attempts.')


if __name__ == '__main__':
    main()
