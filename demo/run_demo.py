"""Run a small, assertion-checked offline session, or opt in to live services."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

from fake_backend import FakeBackend
from local_memory import test_local_memory
from tessera.config import Config, load_config
from tessera.loop import Session


def tool(name, arguments, call_id):
    return {'tool_calls': [{'id': call_id, 'name': name, 'arguments': arguments}]}


def offline_demo(root):
    config = Config(home=root, cwd=root)
    config.tools.approval = 'never'
    config.jev.enabled = False
    config.view.node_bytes = 128
    config.view.high_bytes = 900
    config.view.low_bytes = 450
    note = 'Decision: keep the original request recoverable through zoom.\n' + 'Tool output details. ' * 80
    holder = {}
    def child_backend():
        return FakeBackend([
            tool('read_file', {'path': 'note.txt'}, 'child-read'),
            'note.txt says to keep the original request recoverable through zoom.',
        ])

    def after_report(messages, tools):
        assert holder['session'].agents.wait(timeout=5), 'Helper did not finish'
        return {'content': 'The helper report confirms the note contents.'}

    backend = FakeBackend([
        tool('write_file', {'path': 'note.txt', 'text': note}, 'write'),
        tool('read_file', {'path': 'note.txt'}, 'read'),
        tool('shell', {'command': 'printf "shell smoke passed\\n"'}, 'shell'),
        'Created and read note.txt. The shell smoke passed. The decision is preserved in the note.',
        tool('spawn_agent', {'name': 'reader', 'task': 'Read note.txt and report the decision.', 'tools': ['read_file', 'zoom', 'date']}, 'spawn'),
        after_report,
        {'tool_calls': [{'id': 'zoom', 'name': 'zoom', 'arguments': {'start': 0, 'count': 1}},
                        {'id': 'date', 'name': 'date', 'arguments': {'i': 0}}]},
        'Zoom recovered the exact first request; date returned its original UTC timestamp.',
        'Four.',
    ])
    prompts = [
        'Create note.txt with the decision and inspect it using tools.',
        'Use tools to ask a helper to read note.txt and report its contents.',
        'Use zoom tools to recover the original first request and date.',
        'What is two plus two?',
        'Delegate an independent task to read note.txt and report the decision.',
    ]
    turns = []
    with test_local_memory(), Session(config, backend=backend, agent_backend_factory=child_backend) as session:
        holder['session'] = session
        for prompt in prompts:
            reply = session.run_turn(prompt)
            turns.append((prompt, reply))
        assert session.agents.wait(timeout=5)
        upto = session.log.count() - 1
        assert session.compactor.wait_until_summarized(upto, timeout=5), 'Compaction did not finish'
        rendered = session.view.render()
        assert any(level > 0 for level, index in session.view.nodes), 'No view compaction happened'
        assert len(rendered.encode()) <= config.view.high_bytes, 'View is above the high watermark'
        assert session.view.zoom(0, 1) == prompts[0], 'Raw request did not survive compaction'
        reports = []
        tool_names = set()
        routes = []
        echo_success = []
        for message in session.log.iter():
            if message.kind == 'work':
                reports.append(message.text)
            if message.kind == 'tool':
                tool_names.add(json.loads(message.text)['name'])
            if message.kind == 'echo':
                echo_success.append(message.meta['success'])
            if message.kind == 'agent' and message.meta.get('route'):
                route = message.meta['route']
                if route not in routes:
                    routes.append(route)
        assert reports and reports[0].startswith('[reader]')
        assert {'write_file', 'read_file', 'shell', 'spawn_agent', 'zoom', 'date'} <= tool_names
        assert echo_success and all(echo_success), 'A demo tool failed'
        assert session.capture.debt(), 'Offline captures were not queued'
        assert any(route['route'] == 'direct' for route in routes)
        assert any(route['route'] == 'tools' for route in routes)
        assert any(route['route'] == 'delegate' for route in routes)
        assert len(reports) == 2 and 'recoverable through zoom' in reports[1]
        status = session.status()
        capture_count = len(session.capture.debt())
    # close() also creates a session-level capture. Reopen the durable journal reader.
    assert len(session.capture.debt()) == capture_count + 1
    return turns, reports, rendered, routes, status, len(session.capture.debt())


def live_demo(root):
    config = load_config()
    config.backend_for('main')
    # Live capture follows the user's configuration, while local artifacts stay isolated.
    config.cwd = root
    prompts = [
        'Use write_file to create demo-note.txt containing "A recoverable demo note". Read it back.',
        'Use tools to ask a helper through spawn_agent to read demo-note.txt and report the contents.',
        'Use zoom on message 0 and date on message 0 to recover my first request and its timestamp.',
    ]
    turns = []
    with Session(config, root=root / 'chat', approve=lambda name, args: name == 'write_file' and args.get('path') == 'demo-note.txt') as session:
        for prompt in prompts:
            turns.append((prompt, session.run_turn(prompt)))
        session.agents.wait()
        session.compactor.wait_until_summarized(session.log.count() - 1, timeout=20)
        reports = []
        routes = []
        for message in session.log.iter():
            if message.kind == 'work':
                reports.append(message.text)
            if message.meta.get('route'):
                routes.append(message.meta['route'])
        rendered = session.view.render()
        status = session.status()
    return turns, reports, rendered, routes, status, len(session.capture.debt())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Use configured model/memory services; can send captures')
    parser.add_argument('--output', type=Path, default=REPO / 'demo' / 'transcript.md')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='demo-session-', dir=REPO / 'demo') as directory:
        root = Path(directory)
        turns, reports, view, routes, status, debt = live_demo(root) if args.live else offline_demo(root)
    lines = ['# Tessera demo transcript', '',
             'Mode: ' + ('live services (opt-in)' if args.live else 'scripted FakeBackend; offline; temporary files'), '',
             'The offline path asserts every listed feature against the real Session modules.', '']
    for index, (prompt, reply) in enumerate(turns, 1):
        lines.extend([f'## Turn {index}', '', '**User:** ' + prompt, '', '**Agent:** ' + reply, ''])
    lines.extend(['## Background report', '', *reports, '', '## Persisted summary view after compaction', '', '```text', view, '```', '', '## Routing and capture evidence', '', '```json', json.dumps({'routes': routes, 'status_before_close': status, 'queued_captures_after_close': debt}, indent=2), '```', '',
                  'Offline write-debt proves durable local queuing. It does not prove delivery to a live memory service.', ''])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('\n'.join(lines), encoding='utf-8')
    print(f'Demo passed. Transcript: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
