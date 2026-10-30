"""Inline terminal client for a separately spawned stdio engine."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time
import uuid

from .rpc_client import RpcClient, RpcError


# Engine text is data, never terminal escape sequences or cursor commands.
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))')


def plain(value):
    text = ANSI.sub('', str(value))
    cleaned = []
    for char in text:
        if char in '\n\t' or ord(char) >= 32 and not 127 <= ord(char) <= 159:
            cleaned.append(char)
    return ''.join(cleaned)


class Terminal:
    def __init__(self, peer, *, source=None, output=None, errors=None,
                 approval='on-request', color='auto', turn_timeout=0,
                 history_limit=100, event_limit=100, event_poll_seconds=1):
        self.peer = peer
        self.source = source or sys.stdin
        self.output = output or sys.stdout
        self.errors = errors or sys.stderr
        self.approval = approval
        self.turn_timeout = turn_timeout
        self.history_limit = history_limit
        self.event_limit = event_limit
        self.event_poll_seconds = event_poll_seconds
        self.event_cursor = 0
        self.seen_items = set()
        self.resuming = False
        self.deferred = []
        self.color = color == 'always' or color == 'auto' and self.output.isatty() and 'NO_COLOR' not in os.environ
        self.thread_id = None
        self.turn_id = None
        self.completed = {}
        self.streamed = {}
        self.line_open = False
        peer.notify = self.notification
        peer.approve = self.request_approval

    def write(self, text='', *, end='\n'):
        self.output.write(plain(text) + end)
        self.output.flush()

    def heading(self, text):
        if self.color:
            self.output.write('\x1b[38;5;130m' + plain(text) + '\x1b[0m\n')
            self.output.flush()
        else:
            self.write(text)

    def finish_line(self):
        if self.line_open:
            self.write()
            self.line_open = False

    def read(self, prompt):
        if self.source.isatty():
            self.output.write(prompt)
            self.output.flush()
        line = self.source.readline()
        if not line:
            raise EOFError
        return line.rstrip('\r\n')

    def request_approval(self, method, params):
        if method not in ('item/tool/requestApproval', 'decision/requestApproval'):
            return None
        self.finish_line()
        name = params.get('name', 'tool' if method == 'item/tool/requestApproval' else 'decision gate')
        self.heading('Approval: ' + plain(name))
        details = params.get('arguments', {})
        if method == 'decision/requestApproval':
            details = params.get('decision', {})
        self.write(json.dumps(details, ensure_ascii=False, indent=2))
        allow = False
        if self.approval == 'on-request' and self.source.isatty():
            try:
                allow = self.read('Allow this tool once? [y/N] ').strip().lower() in ('y', 'yes')
            except (EOFError, KeyboardInterrupt):
                pass
        else:
            self.write('Declined: interactive approval is unavailable or disabled.')
        return {'decision': 'accept' if allow else 'decline'}

    def notification(self, method, params):
        if self.resuming:
            self.deferred.append((method, params))
            return
        thread_id = params.get('threadId')
        if thread_id is not None and self.thread_id is not None and thread_id != self.thread_id:
            return
        if method == 'item/agentMessage/delta':
            item_id = params.get('itemId')
            delta = params.get('delta')
            if isinstance(delta, str) and isinstance(item_id, str):
                self.streamed[item_id] = self.streamed.get(item_id, '') + delta
                self.write(delta, end='')
                self.line_open = True
        elif method == 'item/started':
            item = params.get('item', {})
            if not isinstance(item, dict):
                return
            if item.get('id') in self.seen_items:
                return
            if item.get('type') == 'tool':
                self.finish_line()
                self.heading('Tool: ' + str(item.get('name', 'tool')))
        elif method == 'item/completed':
            item = params.get('item', {})
            if not isinstance(item, dict):
                return
            item_id = item.get('id')
            if item_id in self.seen_items:
                return
            if isinstance(item_id, str):
                self.seen_items.add(item_id)
            if item.get('type') == 'agentMessage' and isinstance(item.get('text'), str):
                previous = self.streamed.pop(item.get('id'), '')
                text = item['text']
                if text.startswith(previous):
                    self.write(text[len(previous):], end='')
                elif text:
                    self.finish_line()
                    self.write(text, end='')
                self.line_open = True
                self.finish_line()
            elif item.get('type') in ('toolResult', 'tool'):
                self.finish_line()
                if isinstance(item.get('output'), str):
                    self.write(item['output'])
        elif method == 'turn/completed':
            turn = params.get('turn', {})
            if not isinstance(turn, dict):
                return
            turn_id = turn.get('id', params.get('turnId'))
            if isinstance(turn_id, str):
                self.completed[turn_id] = turn
            self.finish_line()
        elif method == 'thread/event' and params.get('event') in ('agent/planning', 'agent/admission', 'agent/link', 'agent/message', 'agent/merge'):
            self.finish_line()
            self.heading('Coordination: ' + params['event'])
            data = params.get('data')
            if isinstance(data, dict):
                gate = data.get('gate')
                if isinstance(gate, dict):
                    self.write(self.agent_gate('Gate', gate))
        elif method == 'error':
            self.finish_line()
            self.write('The engine reported an error. Use /status to inspect current state.')

    def call(self, method, params=None, timeout=None):
        result = self.peer.call(method, params, timeout)
        if not isinstance(result, dict):
            raise RpcError('The engine returned an invalid result for ' + method + '.')
        return result

    def initialize(self):
        result = self.call('initialize', {'clientInfo': {'name': 'tessera-terminal', 'version': '0.1.1'}})
        info = result.get('serverInfo')
        if not isinstance(info, dict) or info.get('protocolVersion') != 1:
            raise RpcError('This client requires daemon protocol version 1.')
        self.peer.notification('initialized')
        self.heading('Tessera')
        self.write('Chat over stdio. /help for commands; Ctrl-D or /quit to leave.')

    def threads(self):
        result = self.call('thread/list')
        rows = result.get('data')
        if not isinstance(rows, list):
            raise RpcError('The engine returned an invalid session list.')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise RpcError('The engine returned an invalid saved session.')
        return rows

    def selected(self, result, *, resumed=False):
        thread = result.get('thread', {})
        if not isinstance(thread, dict):
            raise RpcError('The engine returned an invalid session.')
        thread_id = thread.get('id')
        if not isinstance(thread_id, str) or not thread_id:
            raise RpcError('The engine did not return a session ID.')
        self.thread_id = thread_id
        self.seen_items.clear()
        self.heading(('Resumed: ' if resumed else 'Session: ') + thread_id)
        if resumed:
            self.history(result)

    def history(self, result):
        messages = result.get('messages')
        if not isinstance(messages, list):
            self.write('Earlier messages were not included by the engine.')
            return
        total = result['thread'].get('messages')
        if type(total) is int and total > len(messages):
            after = max(-1, total - self.history_limit - 1)
            messages = []
            while after < total - 1:
                page = self.call('thread/read', {'threadId': self.thread_id, 'after': after}).get('messages')
                if not isinstance(page, list) or not page:
                    break
                index = page[-1].get('i') if isinstance(page[-1], dict) else None
                if type(index) is not int or index <= after:
                    raise RpcError('The engine returned an invalid history cursor.')
                messages.extend(page)
                after = index
        if len(messages) > self.history_limit:
            messages = messages[-self.history_limit:]
        if type(total) is int and total > len(messages):
            self.write(f'Showing {len(messages)} of {total} saved messages. Use /zoom for earlier source.')
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get('text'), str):
                continue
            if type(message.get('i')) is int:
                self.seen_items.add(str(message['i']))
            self.write(str(message.get('kind', 'message')) + ': ' + message['text'])

    def resume_id(self, selector):
        self.resuming = True
        self.deferred = []
        try:
            result = self.call('thread/resume', {'threadId': selector})
            self.selected(result, resumed=True)
        finally:
            self.resuming = False
            deferred = self.deferred
            self.deferred = []
        for method, params in deferred:
            self.notification(method, params)

    def new(self, title=None):
        self.selected(self.call('thread/start', {'title': title or 'New conversation'}))

    def resume(self, selector=None, *, last=False, create_missing=False):
        if selector:
            if create_missing:
                for row in self.threads():
                    if selector in (row.get('id'), row.get('title')):
                        selector = row['id']
                        break
                else:
                    self.new(selector)
                    return
            self.resume_id(selector)
            return
        rows = self.threads()
        if not rows:
            self.write('No saved sessions. Use /new to start one.')
            return
        if last:
            selector = max(rows, key=lambda row: str(row.get('updated', row.get('created', ''))))['id']
        else:
            for index, row in enumerate(rows, 1):
                self.write(f"{index}. {row.get('title', 'Untitled')} [{row.get('id', '?')}]")
            if not self.source.isatty():
                raise RpcError('Choose a session ID with resume <id>, or use resume --last.')
            choice = self.read('Resume session number (Enter cancels): ').strip()
            if not choice:
                return
            try:
                number = int(choice)
                if number < 1 or number > len(rows):
                    raise ValueError()
                selector = rows[number - 1]['id']
            except (ValueError, KeyError):
                raise RpcError('Choose one of the listed session numbers.') from None
        self.resume_id(selector)

    def turn(self, text):
        if not self.thread_id:
            self.new()
        self.completed.clear()
        self.streamed.clear()
        result = self.call('turn/start', {'threadId': self.thread_id, 'input': [{'type': 'text', 'text': text}]})
        turn = result.get('turn', {})
        if not isinstance(turn, dict):
            raise RpcError('The engine returned an invalid turn.')
        self.turn_id = turn.get('id')
        if not isinstance(self.turn_id, str):
            raise RpcError('The engine did not return a turn ID.')
        deadline = time.monotonic() + self.turn_timeout if self.turn_timeout > 0 else None
        try:
            while self.turn_id not in self.completed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise RpcError('The turn exceeded the configured wait limit. Use /status before retrying.')
                self.peer.pump(remaining)
            outcome = self.completed.pop(self.turn_id)
            if outcome.get('status') in ('failed', 'error', 'interrupted'):
                self.write('Turn ' + outcome['status'] + '. Use /status for current state.')
        except KeyboardInterrupt:
            self.finish_line()
            self.call('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id})
            self.write('Interrupt requested.')
        finally:
            self.turn_id = None
            self.finish_line()

    def zoom(self, args):
        if not self.thread_id:
            raise RpcError('Start or resume a session before /zoom.')
        positions = []
        options = {}
        index = 0
        while index < len(args):
            word = args[index]
            if word in ('--offset', '--limit'):
                if index + 1 >= len(args) or word[2:] in options:
                    raise ValueError('Provide one integer per zoom paging option.')
                options[word[2:]] = int(args[index + 1])
                index += 2
            else:
                positions.append(word)
                index += 1
        if not positions or len(positions) > 2:
            raise ValueError('Usage: /zoom start[+count] [count] [--offset N] [--limit N]')
        span = positions[0].split('+')
        if len(span) > 2 or len(span) == 2 and len(positions) == 2:
            raise ValueError('Specify one start and one count.')
        start = int(span[0])
        count = 1
        if len(span) == 2:
            count = int(span[1])
        elif len(positions) == 2:
            count = int(positions[1])
        if start < 0 or count < 1 or options.get('offset', 0) < 0 or options.get('limit', 1) < 1:
            raise ValueError('Zoom needs a nonnegative start/offset and positive count/limit.')
        result = self.call('view/zoom', {'threadId': self.thread_id, 'start': start, 'count': count, **options})
        text = result.get('text')
        if not isinstance(text, str):
            raise RpcError('The engine did not return zoom text.')
        self.write(text)
        next_offset = result.get('nextOffset')
        if type(next_offset) is int and next_offset >= 0:
            if next_offset <= options.get('offset', 0):
                self.write('More source remains, but this page did not advance. Increase --limit.')
            else:
                command = f'/zoom {start}+{count} --offset {next_offset}'
                if 'limit' in options:
                    command += ' --limit ' + str(options['limit'])
                self.write('More source remains. Continue with ' + command)

    def agents(self):
        rows = self.threads()
        runs = []
        try:
            result = self.call('agent/list')
            runs = result.get('data')
            if not isinstance(runs, list):
                raise RpcError('The engine returned an invalid agent run list.')
        except RpcError as error:
            if error.code != -32601:
                raise
            self.write('Agent topology is unavailable; showing saved sessions.')
        parents = {}
        known_parents = set()
        for run in runs:
            if not isinstance(run, dict) or not isinstance(run.get('id'), str):
                raise RpcError('The engine returned an invalid agent run.')
            parent = run.get('parentThreadId')
            workers = run.get('workers')
            if not isinstance(parent, str) or not parent or not isinstance(workers, list):
                raise RpcError('The engine returned invalid agent topology.')
            known_parents.add(parent)
            for worker in workers:
                if not isinstance(worker, dict) or not isinstance(worker.get('threadId'), str):
                    raise RpcError('The engine returned an invalid agent child.')
                child = worker['threadId']
                if not child or child in parents and parents[child] != parent:
                    raise RpcError('The engine returned conflicting agent topology.')
                parents[child] = parent
        if not rows:
            self.write('No saved agents or sessions. Use /new to start one.')
            return
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise RpcError('The engine returned an invalid agent session.')
        self.heading('Agents / sessions')
        for row in rows:
            label = '* ' if row['id'] == self.thread_id else '  '
            label += row['id']
            parent = parents.get(row['id'])
            if isinstance(parent, str) and parent:
                label += '  parent=' + parent
            elif row['id'] in known_parents:
                label += '  parent session'
            else:
                label += '  parent unreported'
            status = row.get('status')
            if status in ('idle', 'queued', 'running', 'completed', 'failed', 'blocked', 'cancelled'):
                label += '  [' + status + ']'
            if row.get('archived') is True:
                label += '  [archived]'
            self.write(label)
        for run in runs:
            label = 'Run ' + run['id'] + '  parent=' + run['parentThreadId']
            if run.get('mode') in ('single', 'parallel'):
                label += '  mode=' + run['mode']
            if run.get('status') in ('single', 'queued', 'running', 'ready', 'partial', 'succeeded', 'failed', 'blocked'):
                label += '  [' + run['status'] + ']'
            label += '  workers=' + str(len(run['workers']))
            self.write(label)
        self.write('Attach with /resume <session-id>. * is the selected session.')

    @staticmethod
    def agent_gate(label, gate):
        outcome = 'unreported'
        source = 'unreported'
        if isinstance(gate, dict):
            if gate.get('allowed') is True:
                outcome = 'allowed'
            elif gate.get('allowed') is False:
                outcome = 'denied'
            if gate.get('source') in ('deterministic', 'jev', 'fallback', 'unavailable', 'per_piece'):
                source = gate['source']
        return label + ': ' + outcome + ' (source=' + source + ')'

    @staticmethod
    def validation_state(checks):
        if not isinstance(checks, list) or not checks:
            return 'unreported'
        for check in checks:
            if not isinstance(check, dict) or type(check.get('exit_code')) is not int:
                return 'unreported'
            if check['exit_code'] != 0:
                return 'failed'
        return 'passed'

    @staticmethod
    def landing_lines(worker):
        receipt = worker.get('landing')
        if not isinstance(receipt, dict):
            return ['Git landing receipt: unreported']
        child_checks = Terminal.validation_state(receipt.get('child_checks'))
        integration_checks = Terminal.validation_state(receipt.get('integration_checks'))
        gate = receipt.get('gate')
        commit = receipt.get('commit')
        child_commit = receipt.get('child_commit')
        commit_valid = isinstance(commit, str) and re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', commit)
        child_valid = isinstance(child_commit, str) and re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', child_commit)
        status = receipt.get('status')
        if status == 'merged':
            jev_allowed = isinstance(gate, dict) and gate.get('allowed') is True and gate.get('source') == 'jev'
            if not (commit_valid and child_valid and jev_allowed and child_checks == integration_checks == 'passed'):
                status = 'incomplete'
        elif status != 'blocked':
            status = 'unreported'
        lines = ['Git landing receipt: ' + status,
                 'Child checks: ' + child_checks, 'Integration checks: ' + integration_checks,
                 Terminal.agent_gate('Landing gate', gate)]
        reason = receipt.get('reason')
        if isinstance(reason, str) and reason.strip():
            lines.append('Reason: ' + reason.strip())
        if child_valid:
            lines.append('Child commit: ' + child_commit)
        if status == 'merged':
            lines.append('Integration commit: ' + commit)
        return lines

    def agent_run_summary(self, result):
        run = result.get('run')
        if not isinstance(run, dict) or not isinstance(run.get('id'), str) or not run['id']:
            raise RpcError('The engine did not return an agent run ID.')
        workers = run.get('workers')
        if not isinstance(workers, list):
            raise RpcError('The engine did not return an agent worker list.')
        lines = []
        parent = run.get('parentThreadId')
        if isinstance(parent, str) and parent:
            lines.append('Parent session: ' + parent)
        mode = run.get('mode')
        lines.append('Mode: ' + (mode if mode in ('single', 'parallel') else 'unreported'))
        status = run.get('status')
        known_states = ('single', 'queued', 'running', 'ready', 'partial', 'succeeded', 'failed', 'blocked')
        lines.append('State: ' + (status if status in known_states else 'unreported'))
        lines.append(self.agent_gate('Admission', run.get('admission_gate')))
        lines.append(self.agent_gate('Result merge gate', run.get('merge_gate')))
        planning = run.get('planning')
        if isinstance(planning, dict) and planning.get('source') == 'backend':
            lines.append('Parent planning: backend record present')
        else:
            lines.append('Parent planning: unreported')
        lines.append('Workers: ' + str(len(workers)))
        for worker in workers:
            if not isinstance(worker, dict) or not isinstance(worker.get('threadId'), str) or not worker['threadId']:
                raise RpcError('The engine returned an invalid agent child.')
            lines.append('Child session: ' + worker['threadId'])
            lines.extend(self.landing_lines(worker))
        usage = run.get('usage')
        if isinstance(usage, dict):
            tokens = usage.get('tokens')
            if type(tokens) in (int, float) and math.isfinite(tokens) and tokens >= 0:
                lines.append('Token accounting: ' + str(tokens))
        self.heading('Agent run: ' + run['id'])
        for line in lines:
            self.write(line)

    def agent_action(self, args):
        if len(args) != 2 or args[0] not in ('plan', 'spawn', 'run', 'show', 'merge'):
            raise ValueError('Usage: /agents | /agents plan backlog.json | spawn spec.json | run id | show id | merge id')
        action, target = args
        timeout = None
        if action in ('plan', 'spawn'):
            if not self.thread_id:
                raise RpcError('Start or resume a parent session before /agents ' + action + '.')
            spec = json.loads(Path(target).read_text(encoding='utf-8'))
            required = {'objective', 'backlog'} if action == 'plan' else {'objective', 'workers'}
            if not isinstance(spec, dict) or set(spec) - required - {'requestId'} or not required <= set(spec):
                raise ValueError('Agent ' + action + ' spec needs ' + ', '.join(sorted(required)) + ' and optional requestId only.')
            if action == 'plan' and (not isinstance(spec['backlog'], str) or not spec['backlog'].strip()):
                raise ValueError('Parent planning backlog must be nonempty text.')
            request_id = spec.get('requestId', uuid.uuid4().hex)
            if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
                raise ValueError('Agent requestId needs 32 lowercase hexadecimal characters.')
            params = {'threadId': self.thread_id, **spec, 'requestId': request_id}
            self.write(('Parent plan request: ' if action == 'plan' else 'Operator plan request: ') + request_id)
            target = request_id
            timeout = self.turn_timeout
        else:
            params = {'runId': target}
            if action in ('run', 'merge'):
                timeout = self.turn_timeout
        try:
            result = self.call('agent/' + action, params, timeout=timeout)
        except (RpcError, KeyboardInterrupt):
            if action != 'show':
                self.write('No retry performed. Inspect /agents show ' + target + ' before retrying.')
                self.write('Leaving this wait does not confirm cancellation of accepted work.')
            raise
        if action in ('plan', 'spawn') and (not isinstance(result.get('run'), dict) or result['run'].get('id') != target):
            raise RpcError('The engine did not confirm the matching agent plan. Inspect /agents show ' + target + '.')
        self.agent_run_summary(result)

    def send_agent(self, args):
        if len(args) < 2 or not ' '.join(args[1:]).strip():
            raise ValueError('Usage: /send target-id message')
        if not self.thread_id:
            raise RpcError('Start or resume a sender session before /send.')
        request_id = uuid.uuid4().hex
        self.write('Message request: ' + request_id)
        result = self.call('agent/send', {'fromThreadId': self.thread_id,
                                        'toThreadId': args[0], 'text': ' '.join(args[1:]),
                                        'messageId': request_id})
        message = result.get('message')
        if not isinstance(message, dict) or message.get('message_id') != request_id:
            raise RpcError('The engine did not confirm a durable message receipt. Inspect /events before retrying.')
        self.write('Recorded message: ' + request_id)
        self.write('Acceptance alone does not confirm delivery, acknowledgement or a reply. Use /events.')

    def coordination_row(self, row):
        prefix = '[' + str(row['sequence']) + '] '
        operation = row.get('op')
        if operation == 'message':
            message = row.get('message', {})
            if not isinstance(message, dict):
                raise RpcError('The engine returned an invalid coordination message.')
            kind = message.get('kind')
            if kind not in ('channel', 'direct', 'handoff'):
                return prefix + 'message (kind unreported)'
            label = prefix + kind
            for field in ('id', 'sender', 'target', 'channel'):
                value = message.get(field)
                if isinstance(value, str) and value:
                    label += '  ' + field + '=' + value
            text = message.get('text')
            if isinstance(text, str):
                label += '\n' + text
            return label
        if operation == 'ack':
            label = prefix + 'ack'
            for field in ('message_id', 'session', 'direction'):
                value = row.get(field)
                if isinstance(value, str) and value:
                    label += '  ' + field + '=' + value
            return label
        if operation == 'channel':
            return prefix + 'channel created'
        if operation == 'subscription':
            return prefix + 'subscription changed'
        return prefix + 'coordination event (kind unreported)'

    def coordination_page(self, after, limit, *, quiet=False):
        result = self.call('channel/events', {'after': after, 'limit': limit})
        rows = result.get('data')
        cursor = result.get('nextAfter')
        if not isinstance(rows, list) or len(rows) > limit or type(cursor) is not int or cursor < after:
            raise RpcError('The engine returned an invalid coordination page.')
        previous = after
        rendered = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get('sequence')) is not int or row['sequence'] <= previous:
                raise RpcError('The engine returned a non-advancing coordination cursor.')
            previous = row['sequence']
            rendered.append(self.coordination_row(row))
        if cursor < previous:
            raise RpcError('The engine returned a non-advancing coordination cursor.')
        for text in rendered:
            self.write(text)
        self.event_cursor = cursor
        if result.get('enabled') is False:
            self.write('Coordination channels are disabled.')
        elif not rows and not quiet:
            self.write('No coordination events after cursor ' + str(after) + '.')
        if rows or not quiet or cursor != after:
            self.write('Coordination cursor: ' + str(cursor))
        return result.get('enabled') is not False

    def events(self, args):
        after = self.event_cursor
        limit = self.event_limit
        watch = False
        options = set()
        index = 0
        while index < len(args):
            option = args[index]
            if option in options:
                raise ValueError('Specify each events option once.')
            options.add(option)
            if option == '--watch':
                watch = True
                index += 1
            elif option in ('--after', '--limit') and index + 1 < len(args):
                value = int(args[index + 1])
                if option == '--after':
                    after = value
                else:
                    limit = value
                index += 2
            else:
                raise ValueError('Usage: /events [--after N] [--limit N] [--watch]')
        if after < 0 or limit < 1:
            raise ValueError('Events need a nonnegative cursor and positive limit.')
        if watch:
            self.write('Watching coordination events. Ctrl-C stops watching; queued work continues.')
        first_page = True
        try:
            while True:
                enabled = self.coordination_page(after, limit, quiet=watch and not first_page)
                first_page = False
                if not watch or not enabled:
                    break
                after = self.event_cursor
                time.sleep(self.event_poll_seconds)
        except KeyboardInterrupt:
            self.write('Stopped watching at cursor ' + str(self.event_cursor) + '.')

    def command(self, text):
        words = shlex.split(text)
        name = words[0]
        args = words[1:]
        if name in ('/quit', '/exit'):
            return False
        if name == '/help':
            self.write('/new [title]  /resume [id|--last]  /status  /view  /zoom start[+count] [count] [--offset N] [--limit N]')
            self.write('/swarm create spec.json  /swarm run id  /swarm show id  /quit')
            self.write('/agents  /send target-id message  /events [--after N] [--limit N] [--watch]')
            self.write('/agents plan backlog.json  /agents spawn spec.json  /agents run id  /agents show id  /agents merge id')
        elif name == '/new':
            self.new(' '.join(args) or None)
        elif name == '/resume':
            self.resume(None if not args or args == ['--last'] else args[0], last=args == ['--last'])
        elif name == '/agents':
            if args:
                self.agent_action(args)
            else:
                self.agents()
        elif name == '/send':
            self.send_agent(args)
        elif name == '/events':
            self.events(args)
        elif name == '/status':
            self.write(json.dumps(self.call('status'), ensure_ascii=False, indent=2))
        elif name == '/view':
            if not self.thread_id:
                raise RpcError('Start or resume a session before /view.')
            result = self.call('view/read', {'threadId': self.thread_id})
            lines = result.get('lines')
            if not isinstance(lines, list):
                raise RpcError('The engine did not report working-view lines.')
            if not lines:
                self.write('The working view is empty.')
            for line in lines:
                if isinstance(line, dict) and isinstance(line.get('text'), str):
                    self.write(f"[{line.get('start', '?')}+{line.get('count', '?')}] " + line['text'])
        elif name == '/zoom':
            self.zoom(args)
        elif name == '/swarm':
            if len(args) != 2 or args[0] not in ('create', 'run', 'show'):
                raise ValueError('Usage: /swarm create spec.json | run id | show id')
            if args[0] == 'create':
                if not self.thread_id:
                    raise RpcError('Start or resume a session before creating a job.')
                spec = json.loads(Path(args[1]).read_text(encoding='utf-8'))
                if not isinstance(spec, dict) or set(spec) != {'objective', 'workers'}:
                    raise ValueError('Swarm spec needs objective and workers only.')
                params = {'threadId': self.thread_id, **spec}
            else:
                params = {'jobId': args[1]}
            result = self.call('swarm/' + args[0], params, timeout=self.turn_timeout)
            self.write(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            self.write('Unknown command. Use /help.')
        return True

    def run(self, *, session=None, resume=False, last=False):
        self.initialize()
        if resume or last:
            self.resume(session, last=last)
        elif session:
            self.resume(session, create_missing=True)
        while True:
            try:
                text = self.read('> ').strip()
                if not text:
                    continue
                if text.startswith('/'):
                    if not self.command(text):
                        break
                else:
                    self.turn(text)
            except EOFError:
                break
            except KeyboardInterrupt:
                self.write('\nUse /quit or Ctrl-D to leave.')
            except (RpcError, ValueError, OSError) as error:
                if isinstance(error, RpcError) and error.code == -32601:
                    self.write('This engine method is unavailable. No operation was assumed successful.')
                else:
                    self.write('tessera: ' + str(error))
                if self.peer.failure or self.peer.process.poll() is not None:
                    return 1
        return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    utility = {'init', 'seed', 'inspect', 'inspect-mcp', 'app-server', 'serve', 'desktop', 'run', 'status', 'zoom', 'swarm-create', 'swarm-run', 'swarm-show', 'start', 'install-launcher', 'install-desktop'}
    position = 0
    while position < len(argv):
        if argv[position] == '--home':
            position += 2
        elif argv[position].startswith('--home='):
            position += 1
        else:
            break
    if position < len(argv) and argv[position] in utility:
        from .cli import main as utility_main
        return utility_main(argv)
    parser = argparse.ArgumentParser(prog='tessera', description='Terminal client for the Tessera stdio engine.')
    parser.add_argument('--home', type=Path, help='Private configuration/session directory')
    parser.add_argument('--engine-command', default=os.environ.get('TESSERA_ENGINE_COMMAND'), help='Engine argv as a JSON array; no shell')
    parser.add_argument('--request-timeout', type=float, default=os.environ.get('TESSERA_RPC_TIMEOUT', '30'))
    parser.add_argument('--turn-timeout', type=float, default=os.environ.get('TESSERA_TURN_TIMEOUT', '0'), help='Turn wait seconds; zero waits until completion or Ctrl-C')
    parser.add_argument('--max-frame-bytes', type=int, default=os.environ.get('TESSERA_RPC_MAX_FRAME_BYTES', str(8 * 1024 * 1024)))
    parser.add_argument('--history-limit', type=int, default=os.environ.get('TESSERA_HISTORY_LIMIT', '100'), help='Maximum saved messages displayed on resume')
    parser.add_argument('--event-limit', type=int, default=os.environ.get('TESSERA_EVENT_LIMIT', '100'), help='Coordination events per page')
    parser.add_argument('--event-poll-seconds', type=float, default=os.environ.get('TESSERA_EVENT_POLL_SECONDS', '1'), help='Coordination watch polling interval')
    parser.add_argument('--shutdown-timeout', type=float, default=os.environ.get('TESSERA_RPC_SHUTDOWN_TIMEOUT', '5'))
    parser.add_argument('--ask-for-approval', choices=('on-request', 'never'), default=os.environ.get('TESSERA_APPROVAL_POLICY', 'on-request'))
    parser.add_argument('--color', choices=('auto', 'always', 'never'), default=os.environ.get('TESSERA_TERMINAL_COLOR', 'auto'))
    commands = parser.add_subparsers(dest='command')
    chat = commands.add_parser('chat', help='Start interactive chat')
    chat.add_argument('--session', help='Resume a saved session ID/title, or create a named session')
    resume = commands.add_parser('resume', help='Pick a saved session, or resume its ID')
    resume.add_argument('session', nargs='?')
    resume.add_argument('--last', action='store_true')
    args = parser.parse_args(argv)
    try:
        for value in (args.request_timeout, args.turn_timeout, args.shutdown_timeout):
            if not math.isfinite(value) or value < 0:
                raise ValueError('Timeouts must be finite and nonnegative.')
        if args.max_frame_bytes < 1 or args.history_limit < 1:
            raise ValueError('Frame and history limits must be positive.')
        if args.event_limit < 1 or not math.isfinite(args.event_poll_seconds) or args.event_poll_seconds <= 0:
            raise ValueError('Event page limit and finite polling interval must be positive.')
        if args.engine_command:
            command = json.loads(args.engine_command)
            if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
                raise ValueError('Engine command must be a nonempty JSON array of argv strings.')
        else:
            command = [sys.executable, '-m', 'tessera']
            if args.home:
                command.extend(['--home', str(args.home)])
            command.append('app-server')
        with RpcClient(command, timeout=args.request_timeout, max_frame_bytes=args.max_frame_bytes,
                       shutdown_timeout=args.shutdown_timeout) as peer:
            terminal = Terminal(peer, approval=args.ask_for_approval, color=args.color, turn_timeout=args.turn_timeout,
                                history_limit=args.history_limit, event_limit=args.event_limit,
                                event_poll_seconds=args.event_poll_seconds)
            return terminal.run(session=getattr(args, 'session', None), resume=args.command == 'resume', last=getattr(args, 'last', False))
    except (RpcError, ValueError, OSError) as error:
        print('tessera: ' + plain(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
