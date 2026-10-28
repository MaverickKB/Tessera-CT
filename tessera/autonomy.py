"""Event-driven wakes: timers, background tasks, presence and subscriptions.

A wake is a turn whose input is an event digest. Delivery waits for the session to
be idle and passes rate caps; events are held, never dropped. See docs/AUTONOMY.md.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .board import CLAIMABLE_STATES
from .host_metrics import collect_metrics
from .aethermind import drop_superseded
from .floor_definition import (
    FloorDefinitionError, active_rule_ids, compose_wake, default_definition,
    disk_limit_for, effective_world_view, evaluate, exercise_definition, export_text,
    import_text, load_definition, read_audit, save_definition, select_offers,
    validate_definition,
)
from .floor_signals import (
    agenda_rows, board_rows, endpoint_rows, engine_rows, host_rows, session_card_rows,
    session_rows, timer_rows,
)
from .learning import LearningStore, intention_titles, OPEN
from .systems import due_endpoints, host_thresholds, thresholds_by_ordinal
from .remote import ssh_argv
from .tools import object_schema, AUTO_APPROVE

TASK_FINAL = ('done', 'failed', 'stopped', 'timed_out', 'lost')
PRESENCE_STATES = ('arrive', 'here', 'leave', 'typing')
def wake_footer(config, definition=None):
    """The autonomy footer for this install, from the floor definition.

    The sentence is editable template text. Names come from configuration, and
    an install that has configured neither gets generic wording. An empty
    template turns the footer off.
    """
    from .floor_definition import render_template
    definition = definition or default_definition()
    return render_template(definition['compose']['footer'], {
        'agent': config.agent_name or 'this session',
        'operator': config.operator_label(),
    }).strip()


def pressure_lines(report, previous, now, thresholds=None, definition=None, mode='safe'):
    """Live house pressure, from the floor definition's host rules.

    `thresholds` is the systems registry keyed by host ordinal. The disk line
    and the disk latch share that host's declared limit.
    """
    definition = definition or default_definition()
    hits = evaluate(definition, {'host': host_rows(report, previous, thresholds)}, mode=mode)
    return [hit['text'] for hit in hits if hit['signal'] == 'host']


def _note_excerpt(note, limit=300):
    """The end of a board note, which is where the newest evidence lives.

    append_note() appends each line and trims oldest-first when the field fills,
    so the opening sentence of a long-lived card is the least current thing it
    says. Quoting note[:limit] re-woke sessions with the card's original premise
    long after the work had moved on. Start at a line boundary so the excerpt
    does not begin mid-sentence, and say when text was left out.
    """
    if len(note) <= limit:
        return note
    tail = note[-limit:]
    newline = tail.find('\n')
    if newline != -1:
        tail = tail[newline + 1:]
    else:
        # One long line with no break: start at a word boundary, not mid-word.
        space = tail.find(' ')
        if space != -1:
            tail = tail[space + 1:]
    return '[…] ' + tail


def _one_line(text):
    """Collapse a fragment onto a single physical line.

    Board notes are markdown written over many lines, and a pending question can
    carry its own line breaks. Both are embedded inline in a board offer, so any
    newline left in them splits one card line into several lines in the composed
    agenda. _refresh_agenda classifies agenda lines by prefix: it rebuilds lines
    that start with "Your board card " and preserves every other line verbatim.
    A split excerpt therefore leaves its continuation behind as a "static" line,
    which survives the refresh and is hoisted above the rebuilt card line — so a
    compose-time fragment gets delivered as if it were current agenda content.

    A fragment split this way is delivered as if it were current agenda
    content. Collapsing whitespace keeps the excerpt's words and keeps it one
    line.
    """
    return ' '.join(str(text).split())


def _board_offer_line(item):
    """One card, as an offer to act on it.

    Built here rather than inline because the line is composed twice: when the
    agenda is armed and again at delivery, so a held wake cannot quote a status
    the card has already left. Two copies of this format would drift.

    The return value is always exactly one physical line. See _one_line for why
    that is a contract and not a style choice.
    """
    line = f'Your board card {item["ordinal"]}: "{item["title"]}" ({item["status"]}).'
    if item.get('note'):
        line += ' Note: ' + _one_line(_note_excerpt(item['note']))
    pending = [question for question in (item.get('questions') or [])
               if question.get('pending') and question.get('text')]
    if pending:
        line += ' Waiting on an answer: ' + _one_line(pending[-1]['text'][:300])
    return line


def board_pressure_lines(brief, definition=None, mode='safe'):
    """Work the board is holding that nothing is acting on.

    Which cards, how old, how many ordinals are named, and the filing sentence
    all come from the floor definition. These lines are never latched: they show
    for as long as the fact is true.
    """
    definition = definition or default_definition()
    if not isinstance(brief, dict) or 'cards' not in brief:
        return []
    hits = evaluate(definition, {'board_card': board_rows(brief)}, mode=mode)
    return [hit['text'] for hit in hits if hit['signal'] == 'board_card' and hit.get('emit') != 'offers']


def _coordination_fingerprint(card):
    """A card's offer identity. The version moves when the card actually changes.

    The card's id lives in the record's key, not in here. The position is left out
    on purpose: an ordinal is assigned by enumeration at read time, and compaction
    renumbers every card while resetting versions, so a position in the fingerprint
    made a card look changed whenever another card was removed above it.
    """
    return f'{card.get("status")}:{card.get("version")}'


def coordination_offers(brief, limit, seen=None, *, now=None, hold_seconds=None, definition=None, mode='safe'):
    """Unclaimed work is the floor session's coordination agenda, highest priority first.

    Two facts can starve this agenda's backlog.

    First, it had no memory. `wip_coordination_lines` sorted the same unowned cards
    on every wake and returned the first `limit`, so a project holding eleven
    unowned cards offered the same three forever and the ninth was unreachable.
    Measured: only two delivered wakes in the live log ever carried a
    `Work coordination:` line, naming cards 42/43/44 and then 55. No card in the
    active alpha planning project was ever offered. The session's own-card lane
    already solved this with a seen record keyed on ordinal:status:version
    (`_own_board_lines`), so coordination uses the same identity here.

    Second, it ranked by title alone. Every alpha card ties at the fallback
    priority 100, so the order was pure ordinal and filing had no effect. A card
    filed under an active project is work that project is waiting on; an unfiled
    card is one nobody has placed yet. At equal priority the filed card goes first.

    A card that was offered and never claimed is not offered again immediately —
    that would be the same three lines every wake. It is also not dropped forever,
    which would make work nobody picked up invisible. It comes back after
    `hold_seconds`, the same window lessons and questions use, so an ignored card
    resurfaces rather than disappearing.

    Returns (lines, record). The record is what the caller persists so the next
    wake starts after the cards this one named.
    """
    if not isinstance(brief, dict) or not isinstance(brief.get('cards'), list):
        return [], {}
    definition = definition or default_definition()
    hits, record = select_offers(
        definition, {'board_card': board_rows(brief)}, seen, now, limit, hold_seconds,
        sort='priority', mode=mode)
    return [hit['text'] for hit in hits], record


def wip_coordination_lines(brief, limit):
    """The coordination agenda as plain lines, for readers that do not wake anyone."""
    return coordination_offers(brief, limit)[0]


def _rule_for(reading, line):
    for hit in reading.get('hits') or []:
        if hit.get('text') == line:
            return hit.get('rule') or ''
    return ''


def _audit_errors(entries):
    """Bad audit lines, shaped like the other floor errors the page shows."""
    errors = []
    for entry in entries or []:
        if not isinstance(entry, dict) or entry.get('action') != 'error':
            continue
        errors.append({
            'source': entry.get('source') or 'floor-audit',
            'error_type': entry.get('error_type') or 'JSONDecodeError',
            'message': entry.get('error') or 'unreadable audit line',
        })
    return errors


def rank_pressure(items):
    """Highest score first. A steady hit stays out of the ranking.

    Items are (score, line), (score, line, steady), or a hit dict with score,
    text, and steady. The steady flag is the signal. The sentence is not read.
    """
    ranked = []
    for item in items:
        steady = False
        if isinstance(item, dict):
            score = item.get('score')
            line = item.get('text')
            steady = bool(item.get('steady'))
        elif isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], (int, float)) and not isinstance(item[0], bool):
            score, line = item[0], item[1]
            steady = bool(item[2]) if len(item) > 2 else False
        else:
            continue
        if steady or not isinstance(line, str):
            continue
        ranked.append((score, line))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def pressure_seen(report, now, thresholds=None, definition=None, on_error=None):
    """The facts this reading already judged, so the next reading only fires on a change.

    disk_low uses the same limit the disk rule uses: the host's declared
    disk_free_percent_below, or the definition's disk default.
    """
    definition = definition or default_definition()
    compose = definition['compose']
    seen = {}
    for row in host_rows(report, {}, thresholds, on_error=on_error):
        percent = row.get('disk_free_percent')
        limit = disk_limit_for(row, compose)
        disk_low = isinstance(percent, (int, float)) and not isinstance(percent, bool) and percent < limit
        seen[row['label']] = {'health': row.get('health') or 'unknown', 'disk_low': disk_low, 'at': now}
    return seen


def weather_lines(home, now, judged=None):
    """Facts the body measured. Already-judged facts stay quiet until they change."""
    judged = judged or {}
    lines = []
    held = []
    metrics = collect_metrics(str(home))
    load = metrics.get('cpu_load1')
    count = metrics.get('cpu_count') or 1
    if load is not None and load > count:
        key = 'load'
        prior = judged.get(key) or {}
        if prior.get('name') and now - float(prior.get('at') or 0) < 6 * 3600:
            held.append(f'Already judged load as {prior["name"]}.')
        else:
            lines.append(f'Weather: this Mac load is {load:.1f} on {count} cores. Find what is using it and name it.')
    free = metrics.get('disk_free_bytes')
    total = metrics.get('disk_total_bytes')
    if free is not None and total:
        ratio = free / total
        if ratio < 0.1:
            lines.append(f'Weather: disk free is {int(ratio * 100)} percent. Name the largest disposable path. Do not delete it.')
    log_dir = Path(home) / 'sessions'
    quiet = []
    if log_dir.is_dir():
        for child in sorted(log_dir.iterdir()):
            if not child.is_dir() or child.name == 'infinity':
                continue
            logs = sorted((child / 'log').glob('*.jsonl')) if (child / 'log').is_dir() else []
            if not logs:
                continue
            stamp = logs[-1].stat().st_mtime
            if now - stamp <= 3600:
                continue
            prior = judged.get('session:' + child.name) or {}
            if prior.get('verdict') == 'finished' and float(prior.get('log_mtime') or 0) == stamp:
                held.append(child.name + ' finished')
                continue
            quiet.append(child.name)
    if quiet:
        shown = ', '.join(quiet[:4])
        extra = ''
        if len(quiet) > 4:
            extra = f' and {len(quiet) - 4} more'
        lines.append(f'Weather: no new log for over an hour in {shown}{extra}. Read one and say whether it is stuck or finished.')
    if held:
        lines.append('Already judged, do not reread unless it changed: ' + '; '.join(held[:6]) + '.')
    lessons = Path(home) / 'learning' / 'lessons.json'
    if lessons.is_file():
        try:
            saved = json.loads(lessons.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            saved = {}
        open_count = 0
        for lesson in (saved.get('lessons') or {}).values():
            if isinstance(lesson, dict) and lesson.get('status') in ('open', 'written', 'needs_revision'):
                open_count += 1
        if open_count:
            lines.append(f'Weather: {open_count} open lessons. Advance one check. Do not retire it to clear the board.')
    if not any(line.startswith('Weather:') for line in lines):
        lines.append('Weather: no new alarm. Continue the last unfinished thing in this chat. Do not hunt for a task.')
    return lines


def human_seconds(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f'{seconds}s'
    if seconds < 5400:
        return f'{round(seconds / 60)}m'
    if seconds < 129600:
        return f'{seconds / 3600:.1f}h'
    return f'{seconds / 86400:.1f}d'


def load_judged(home):
    path = Path(home) / 'learning' / 'weather-judged.json'
    if not path.is_file():
        return {}
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    facts = saved.get('facts') if isinstance(saved, dict) else None
    return facts if isinstance(facts, dict) else {}


def record_judgment(home, key, **fields):
    key = str(key or '').strip()
    if not key:
        raise ValueError('key is required')
    path = Path(home) / 'learning' / 'weather-judged.json'
    saved = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                saved = loaded
        except (OSError, json.JSONDecodeError):
            saved = {}
    facts = saved.get('facts')
    if not isinstance(facts, dict):
        facts = {}
    facts[key] = {'at': time.time(), **fields}
    saved['facts'] = facts
    _atomic_json(path, saved)
    return {'recorded': True, 'key': key}


def recent_chat(home, slug, limit=4, operator='the operator'):
    """The last few turns of this session, so a wake continues instead of starting cold.

    operator names the human in the transcript; it defaults to generic wording so
    a caller without config cannot label the operator as someone else.
    """
    log_dir = Path(home) / 'sessions' / slug / 'log'
    if not log_dir.is_dir():
        return []
    logs = sorted(log_dir.glob('*.jsonl'))
    if not logs:
        return []
    kept = []
    for line in logs[-1].read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = msg.get('kind')
        text = (msg.get('text') or '').strip().replace('\n', ' ')
        if kind == 'agent' and text and not text.startswith('[wake]'):
            kept.append('you: ' + text[:180])
        elif kind == 'user' and text and not text.startswith('[wake]'):
            kept.append(operator + ': ' + text[:180])
    return kept[-limit:]


def recent_notes(client, project, limit=3):
    """Short AetherMind notes. A cover, not a verified fact, and never the whole tree."""
    if client is None or not project:
        return []
    try:
        result = client.read_layers(project, last_n=limit)
    except Exception:
        return ['AetherMind notes unavailable.']
    # read_layers returns immutable history, so an archived layer arrives with
    # no self-signal. Showing one as a current note tells the floor about
    # something that was deliberately retired.
    result = drop_superseded(result)
    layers = result.get('layers') if isinstance(result, dict) else result
    if not isinstance(layers, list):
        return []
    lines = []
    for layer in layers[:limit]:
        if not isinstance(layer, dict):
            continue
        body = str(layer.get('body') or '').replace('\n', ' ').strip()
        if body:
            lines.append(body[:200])
    return lines


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _home_path(home, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(home) / path


def _alive(pid):
    from .procutil import alive
    return alive(pid)


class Autonomy:
    def __init__(self, manager, *, clock=time.time):
        self.manager = manager
        self.config = manager.config
        self.settings = manager.config.autonomy
        self.clock = clock
        self.path = _home_path(self.config.home, self.settings.state_file)
        self.tasks_dir = _home_path(self.config.home, self.settings.tasks_dir)
        self._lock = threading.RLock()
        self._wakeup = threading.Condition(self._lock)
        self._children = {}
        self._infra_baseline = None
        self._infra_checked = 0.0
        self._stopping = False
        self.state = {'timers': {}, 'tasks': {}, 'held': {}, 'wakes': [], 'presence': {}, 'subscriptions': {},
                      'capped': None, 'approvals': {}, 'awareness': {}, 'deferred': {}, 'self_prompt_at': {},
                      'suppress': {}, 'coordination_at': {}, 'reflection_at': {},
                      # The compose path creates these lazily with setdefault(), and the
                      # load loop below only restores keys that are already in this dict.
                      # A key missing here is written to disk by _save() and silently
                      # dropped on the next restart, so the dedup records have to be
                      # declared here rather than discovered at compose time.
                      'agenda_seen': {}, 'board_seen': {}, 'coordination_seen': {}, 'dispatch_seen': {},
                      'pressure_at': {}, 'pressure_seen': {},
                      'floor_errors': [], 'last_wake': {}, 'rule_fired': {},
                      # Watched endpoints: when each was last probed, and the answer it
                      # gave. These belong in state for the reason above -- as instance
                      # attributes they would be reset on every restart, and each launch
                      # would re-announce every endpoint's current state as news.
                      'endpoint_checked': {}, 'endpoint_health': {}}
        self._runs = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            for key in self.state:
                if key in saved:
                    self.state[key] = saved[key]
        for approval in self.state['approvals'].values():
            if approval['state'] == 'pending':
                # The action closure did not survive the restart; the request cannot be honored.
                approval['state'] = 'expired'
        lesson_path = _home_path(self.config.home, self.settings.lesson_file)
        self.learning = LearningStore(lesson_path, pass_score=self.settings.lesson_pass_score,
                                      max_open=self.settings.max_open_lessons,
                                      fail_limit=self.settings.lesson_fail_limit,
                                      clock=lambda: self.clock())
        self.learning.floor_slug = self.settings.infinity_slug
        self._intentions_at = 0.0
        self.thread = None

    def start(self):
        if self.settings.enabled and self.settings.standing_floor and self.settings.self_prompt_enabled:
            self._floor_slug()
        if self.settings.enabled and self.thread is None:
            self.thread = threading.Thread(target=self._run, name='tessera-autonomy', daemon=True)
            self.thread.start()

    def _save(self):
        _atomic_json(self.path, self.state)

    def _check_slug(self, slug):
        self.manager.path(slug)
        if slug not in self.manager._metadata:
            raise KeyError(slug)

    def _require_enabled(self):
        if not self.settings.enabled:
            raise RuntimeError('Autonomy is disabled ([autonomy] enabled = false)')

    def post(self, slug, kind, text, **data):
        with self._lock:
            event = {'kind': kind, 'at': self.clock(), 'text': text, 'data': data}
            self.state['held'].setdefault(slug, []).append(event)
            self._save()
            self._wakeup.notify_all()
            return event

    # Timers

    def set_timer(self, slug, note, in_seconds=None, at=None, repeat_seconds=None):
        self._require_enabled()
        self._check_slug(slug)
        settings = self.settings
        now = self.clock()
        if (in_seconds is None) == (at is None):
            raise ValueError('Give exactly one of in_seconds or at')
        if at is not None:
            moment = datetime.fromisoformat(at)
            if moment.tzinfo is None:
                moment = moment.astimezone()
            due = moment.astimezone(timezone.utc).timestamp()
        else:
            due = now + float(in_seconds)
        delay = due - now
        if delay < settings.min_timer_seconds or delay > settings.max_timer_seconds:
            raise ValueError(f'Timer must be {human_seconds(settings.min_timer_seconds)} to '
                             f'{human_seconds(settings.max_timer_seconds)} away')
        if repeat_seconds is not None and repeat_seconds < settings.min_timer_seconds:
            raise ValueError(f'Repeat must be at least {human_seconds(settings.min_timer_seconds)}')
        with self._lock:
            mine = [timer for timer in self.state['timers'].values() if timer['slug'] == slug]
            if len(mine) >= settings.max_timers:
                raise ValueError(f'Timer cap reached ({settings.max_timers}); cancel one first')
            timer = {'id': 't-' + uuid.uuid4().hex[:6], 'slug': slug, 'due': due, 'note': str(note)[:500],
                     'repeat': repeat_seconds, 'created': now}
            self.state['timers'][timer['id']] = timer
            self._save()
            self._wakeup.notify_all()
            return self._timer_view(timer, now)

    def _timer_view(self, timer, now):
        due = datetime.fromtimestamp(timer['due']).astimezone().isoformat(timespec='seconds')
        view = {'id': timer['id'], 'note': timer['note'], 'due': due, 'in': human_seconds(timer['due'] - now)}
        if timer['repeat']:
            view['repeat'] = human_seconds(timer['repeat'])
        return view

    def list_timers(self, slug):
        now = self.clock()
        with self._lock:
            timers = sorted((t for t in self.state['timers'].values() if t['slug'] == slug), key=lambda t: t['due'])
            return [self._timer_view(timer, now) for timer in timers]

    def cancel_timer(self, slug, timer_id):
        with self._lock:
            timer = self.state['timers'].get(timer_id)
            if timer is None or timer['slug'] != slug:
                raise KeyError(f'No timer {timer_id}')
            del self.state['timers'][timer_id]
            self._save()
            return {'cancelled': timer_id}

    def cancel_timer_for_operator(self, timer_id):
        with self._lock:
            timer = self.state['timers'].get(timer_id)
            if timer is None:
                raise KeyError(f'No timer {timer_id}')
            del self.state['timers'][timer_id]
            self._save()
        runtime = self.manager.runtime(timer['slug'], internal=True)
        runtime.session.log.append('note',
                                   f'{self.config.operator_label()} cancelled timer {timer_id} ("{timer["note"]}")',
                                   {'timer_cancelled': {'id': timer_id}})
        return {'cancelled': timer_id, 'slug': timer['slug']}

    # Background tasks

    def start_task(self, slug, label, command, remote=None, cwd=None):
        self._require_enabled()
        self._check_slug(slug)
        with self._lock:
            running = [task for task in self.state['tasks'].values() if task['state'] in ('running', 'stalled')]
            if len(running) >= self.settings.max_tasks:
                raise ValueError(f'Background task cap reached ({self.settings.max_tasks}); stop one first')
            task_id = 'bg-' + uuid.uuid4().hex[:6]
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
            output = self.tasks_dir / f'{task_id}.log'
            from .procutil import popen, shell_argv
            if remote:
                settings = self.config.remotes.get(remote)
                if settings is None or not settings.enabled:
                    raise ValueError(f'Unknown or disabled remote: {remote}')
                argv = ssh_argv(settings, self.config.home) + [command]
                directory = str(self.config.cwd)
            else:
                argv = shell_argv(command)
                directory = str(Path(cwd).expanduser() if cwd else self.config.cwd)
            with output.open('wb') as stream:
                process = popen(argv, cwd=directory, stdin=subprocess.DEVNULL, stdout=stream,
                                stderr=subprocess.STDOUT)
            now = self.clock()
            task = {'id': task_id, 'slug': slug, 'label': str(label)[:200], 'command': command, 'remote': remote or '',
                    'cwd': directory, 'pid': process.pid, 'state': 'running', 'started': now, 'ended': None,
                    'exit_code': None, 'output': str(output), 'size': 0, 'last_growth': now}
            self._children[task_id] = process
            self.state['tasks'][task_id] = task
            self._prune_tasks()
            self._save()
            return self._task_view(task)

    def _prune_tasks(self):
        finished = sorted((t for t in self.state['tasks'].values() if t['state'] in TASK_FINAL),
                          key=lambda t: t['ended'] or 0)
        for task in finished[:max(0, len(finished) - self.settings.keep_finished_tasks)]:
            del self.state['tasks'][task['id']]

    def _tail(self, task, chars):
        try:
            with open(task['output'], 'rb') as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - chars * 4))
                return stream.read().decode('utf-8', errors='replace')[-chars:]
        except FileNotFoundError:
            return ''

    def _task_view(self, task, tail=0):
        end = task['ended'] or self.clock()
        view = {'id': task['id'], 'label': task['label'], 'state': task['state'],
                'elapsed': human_seconds(end - task['started']), 'exit_code': task['exit_code']}
        if task['remote']:
            view['remote'] = task['remote']
        if tail:
            view['output_tail'] = self._tail(task, tail)
        return view

    def task_status(self, slug, task_id=None, tail=2000):
        with self._lock:
            tasks = [t for t in self.state['tasks'].values() if t['slug'] == slug]
            if task_id is None:
                return [self._task_view(task) for task in sorted(tasks, key=lambda t: t['started'])]
            for task in tasks:
                if task['id'] == task_id:
                    if task['state'] in TASK_FINAL:
                        self._drop_task_events(slug, task_id)
                    return self._task_view(task, tail)
            raise KeyError(f'No task {task_id}')

    def _drop_task_events(self, slug, task_id):
        held = self.state['held'].get(slug) or []
        kept = [e for e in held if not (e['kind'] == 'task' and e['data'].get('task_id') == task_id)]
        if len(kept) != len(held):
            self.state['held'][slug] = kept
            self._save()

    def stop_task(self, slug, task_id):
        with self._lock:
            task = self.state['tasks'].get(task_id)
            if task is None or task['slug'] != slug:
                raise KeyError(f'No task {task_id}')
            if task['state'] in TASK_FINAL:
                return self._task_view(task)
            from .procutil import terminate_group
            try:
                terminate_group(task['pid'])
            except ProcessLookupError:
                pass
            task['state'] = 'stopped'
            task['ended'] = self.clock()
            task['notified'] = True
            process = self._children.pop(task_id, None)
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    from .procutil import kill_group
                    kill_group(task['pid'])
                    process.wait()
                task['exit_code'] = process.returncode
            self._save()
            return self._task_view(task)

    def continue_later(self, slug, note):
        self._require_enabled()
        self._check_slug(slug)
        self.post(slug, 'continue', f'Continuation you queued: {str(note)[:2000]}')
        return {'queued': True,
                'note': f'Runs as a wake after this turn ends; reply to {self.config.operator_label()} now if needed.'}

    # Presence and subscriptions

    def presence(self, slug, state, source=''):
        if state not in PRESENCE_STATES:
            raise ValueError('state must be arrive, here, leave or typing')
        self._check_slug(slug)
        now = self.clock()
        with self._lock:
            seen = self.state['presence'].setdefault(slug, {'last_seen': None, 'present': False, 'last_event': 0})
            previous = seen['last_seen']
            woke = False
            if state == 'arrive' and self.settings.enabled:
                away = None if previous is None else now - previous
                if (away is None or (not seen['present'] and away >= self.settings.presence_away_seconds)) \
                        and now - seen['last_event'] >= self.settings.presence_away_seconds:
                    since = previous or 0
                    changes = self._changes_since(slug, since)
                    lead = self.config.operator_label_sentence() + ' is here' + (f' ({source})' if source else '')
                    lead += f'; away {human_seconds(away)}.' if away is not None else '; first visit seen.'
                    self.post(slug, 'presence', lead + (' Since then: ' + changes if changes else ''),
                              away_seconds=away, source=source)
                    seen['last_event'] = now
                    woke = True
            if state == 'arrive':
                seen['arrived_at'] = now
            if state == 'typing':
                seen['last_typing'] = now
            seen['present'] = state != 'leave'
            seen['last_seen'] = now
            self._save()
            return {'state': state, 'woke': woke}

    def activity(self, slug, kind='message'):
        """The operator sent a message or steer here; wakes wait until they go quiet."""
        with self._lock:
            seen = self.state['presence'].setdefault(slug, {'last_seen': None, 'present': False, 'last_event': 0})
            seen['last_' + kind] = self.clock()
            self._save()

    def engaged(self, slug, now=None):
        """Why the operator counts as in the conversation right now, or None."""
        now = self.clock() if now is None else now
        settings = self.settings
        seen = self.state['presence'].get(slug) or {}
        message, typing = seen.get('last_message') or 0, seen.get('last_typing') or 0
        if message and now - message < settings.engaged_message_seconds:
            return 'message'
        if typing and now - typing < settings.engaged_typing_seconds:
            return 'typing'
        last_seen = seen.get('last_seen') or 0
        if settings.engaged_focus_seconds and seen.get('present') and now - last_seen < settings.presence_stale_seconds \
                and now - max(seen.get('arrived_at') or 0, message, typing) < settings.engaged_focus_seconds:
            return 'focus'
        return None

    def take_awareness(self, slug):
        """A presence note folded while the operator was engaged; it rides on the next turn, at most once."""
        with self._lock:
            text = self.state['awareness'].pop(slug, None)
            if text is not None:
                self._save()
            return text

    def _changes_since(self, slug, since):
        parts = []
        finished = [t for t in self.state['tasks'].values()
                    if t['slug'] == slug and t['ended'] and t['ended'] >= since]
        if finished:
            parts.append(f'{len(finished)} background task(s) ended')
        wakes = [w for w in self.state['wakes'] if w['slug'] == slug and w['at'] >= since]
        if wakes:
            spoke = sum(1 for w in wakes if w.get('spoke'))
            parts.append(f'you woke {len(wakes)} time(s) and spoke {spoke}')
        return '; '.join(parts) + ('.' if parts else '')

    def subscribe(self, slug, topic, enabled=True):
        self._require_enabled()
        if topic != 'infrastructure':
            raise ValueError('Supported topics: infrastructure')
        with self._lock:
            subscribers = set(self.state['subscriptions'].get(topic, []))
            (subscribers.add if enabled else subscribers.discard)(slug)
            self.state['subscriptions'][topic] = sorted(subscribers)
            self._save()
            return {'topic': topic, 'subscribed': enabled}

    # Approvals

    def gate(self, runtime, registry, name, arguments, run):
        if registry.approval in AUTO_APPROVE or (registry.allowed and registry.allowed(name)):
            return run()
        verdict = self._conscience(runtime, name, arguments)
        if verdict.get('verdict') == 'no':
            runtime.session.log.append('note', f'Conscience blocked {name}: {verdict.get("reason")}',
                                       {'conscience': verdict})
            return {'blocked': True, 'reason': verdict.get('reason')}
        if registry.approval == 'ask' and registry.approve and registry.approve(name, arguments):
            return run()
        return self.request_approval(runtime, name, arguments, run)

    def _conscience(self, runtime, name, arguments):
        settings = getattr(self.config, 'computer_use', None)
        jev = getattr(settings, 'jev', None) if settings is not None else None
        if jev is None or not jev.enabled:
            return {'verdict': 'abstain', 'reason': 'cloud Jev off'}
        pressure = ''
        for item in runtime.session.log.iter():
            text = getattr(item, 'text', '') or ''
            if 'Salience' in text and 'Pressure:' in text:
                pressure = text
        if not pressure:
            return {'verdict': 'abstain', 'reason': 'not a pressure wake'}
        from .cua_jev import api_key, conscience
        try:
            key = api_key(jev, self.config.credentials)
        except ValueError as error:
            return {'verdict': 'abstain', 'reason': str(error)}
        return conscience(pressure, name, arguments, jev, key)

    def request_approval(self, runtime, name, arguments, run):
        shown = json.dumps(arguments, ensure_ascii=False)
        shown = shown if len(shown) <= 600 else shown[:600] + '...'
        with self._lock:
            approval = {'id': 'ap-' + uuid.uuid4().hex[:8], 'slug': runtime.slug, 'tool': name, 'arguments': shown,
                        'state': 'pending', 'created': self.clock(), 'result': None}
            self.state['approvals'][approval['id']] = approval
            self._runs[approval['id']] = run
            pending = sorted((a for a in self.state['approvals'].values() if a['state'] != 'pending'),
                             key=lambda a: a['created'])
            for old in pending[:max(0, len(pending) - self.settings.keep_finished_tasks)]:
                del self.state['approvals'][old['id']]
            self._save()
        runtime.session.log.append('note', f'{self.config.agent_name} asks to run {name}: {shown}',
                                   {'approval': {'id': approval['id'], 'tool': name, 'state': 'pending'}})
        return {'approval': 'requested', 'id': approval['id'],
                'note': f'{self.config.operator_label_sentence()} sees Approve and Deny. '
                        'You are woken with the outcome. Keep working meanwhile.'}

    def resolve(self, approval_id, approve, source='api'):
        """source 'card' is the operator's chat card; anything else is a direct API call (a script or another agent)."""
        if not isinstance(approve, bool):
            raise ValueError('approve must be true or false')
        with self._lock:
            approval = self.state['approvals'].get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            if approval['state'] != 'pending':
                raise ValueError(f'Approval is already {approval["state"]}')
            approval['state'] = 'approved' if approve else 'denied'
            approval['resolved'] = self.clock()
            approval['by'] = 'card' if source == 'card' else 'api'
            run = self._runs.pop(approval_id, None)
            self._save()
        self._log_result(approval)
        if not approve:
            self.post(approval['slug'], 'approval', f'{self._resolver(approval)} denied {approval["tool"]} ({approval_id}).',
                      approval_id=approval_id, by=approval['by'])
            return {'id': approval_id, 'state': 'denied'}
        threading.Thread(target=self._run_approved, args=(approval, run), name=f'tessera-approval-{approval_id}',
                         daemon=True).start()
        return {'id': approval_id, 'state': 'approved'}

    def _resolver(self, approval):
        if approval.get('by') == 'card':
            return f'{self.config.operator_label_sentence()} (from the chat card)'
        return f'Someone through the API, not {self.config.operator_label()}\'s chat card,'

    def _log_result(self, approval):
        runtime = self.manager.runtime(approval['slug'], internal=True)
        runtime.session.log.append('note', f'{approval["tool"]} {approval["state"]}',
                                   {'approval_result': {'id': approval['id'], 'state': approval['state']}})

    def _run_approved(self, approval, run):
        try:
            value = run()
            if isinstance(value, dict):
                value = {key: item for key, item in value.items() if key != 'image'}
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
            outcome = 'ran'
        except Exception as error:
            text = f'{type(error).__name__}: {error}'
            outcome = 'failed'
        text = text[-self.settings.wake_output_chars:]
        with self._lock:
            approval['result'] = outcome
            self._save()
        self.post(approval['slug'], 'approval', f'{self._resolver(approval)} approved {approval["tool"]} ({approval["id"]}); '
                  f'it {outcome}. Result: {text}', approval_id=approval['id'], by=approval.get('by', 'api'))

    def _expire_approvals(self, now):
        for approval in self.state['approvals'].values():
            if approval['state'] == 'pending' and now - approval['created'] > self.settings.approval_expire_seconds:
                approval['state'] = 'expired'
                self._runs.pop(approval['id'], None)
                self._save()
                self._log_result(approval)
                self.post(approval['slug'], 'approval', f'Your request to run {approval["tool"]} ({approval["id"]}) '
                          'expired without an answer.', approval_id=approval['id'])

    # Delivery

    def wake_record(self, slug, turn_id, spoke):
        with self._lock:
            for wake in reversed(self.state['wakes']):
                if wake['turn_id'] == turn_id:
                    wake['spoke'] = spoke
                    self._save()
                    return

    def _cap_reason(self, now):
        settings = self.settings
        wakes = self.state['wakes']
        if wakes and now - wakes[-1]['at'] < settings.min_wake_gap_seconds:
            return 'min_wake_gap_seconds'
        return None

    def _deliver(self, now):
        for slug in list(self.state['held']):
            if not self.state['held'][slug]:
                del self.state['held'][slug]
                continue
            if slug not in self.manager._metadata or self.manager._metadata[slug].get('archived'):
                continue
            events = self._deliverable(slug, now)
            if not events:
                continue
            reason = self._cap_reason(now)
            if reason:
                if not self.state['capped'] or self.state['capped']['reason'] != reason:
                    self.state['capped'] = {'reason': reason, 'since': now, 'held': len(events)}
                    print(f'autonomy: holding {len(events)} event(s) for {slug} ({reason})', file=sys.stderr, flush=True)
                    self._save()
                return
            runtime = self.manager.runtime(slug, internal=True)
            if runtime.operator_stopped or not runtime.idle():
                continue
            held_for = now - min(event['at'] for event in events)
            lines = []
            has_self_prompt = False
            floor_wake = False
            for event in events:
                text = event['text']
                if event['kind'] == 'self_prompt':
                    text = self._refresh_agenda(slug, text, now)
                    if not text:
                        # The agenda was entirely live state that has since settled.
                        continue
                    has_self_prompt = True
                lines.append(f'[wake] {text}')
                if event['kind'] in ('pressure', 'coordination', 'reflection'):
                    floor_wake = True
            if not lines:
                self.state['held'][slug] = [event for event in self.state['held'][slug] if event not in events]
                self._save()
                continue
            session_wake = slug != self.settings.infinity_slug
            if not has_self_prompt and session_wake:
                for line in self._agenda_lines(slug):
                    lines.append('[agenda] ' + line)
            if session_wake:
                push = self.learning.improvement_line(slug)
                if push:
                    lines.append(push)
            if self.state['capped'] and held_for >= 1:
                lines.append(f'(Held {human_seconds(held_for)} by wake caps: {self.state["capped"]["reason"]}.)')
            if floor_wake:
                lines.append(wake_footer(self.config))
            sent = '\n'.join(lines)
            rules = []
            for event in events:
                rules.extend((event.get('data') or {}).get('rules') or [])
            wake = {'events': [event['kind'] for event in events], 'held_seconds': round(held_for, 1),
                    'rules': rules, 'text': sent}
            if floor_wake:
                posted = next((event['text'] for event in events
                               if event['kind'] in ('pressure', 'coordination', 'reflection')), '')
                self.state['last_wake'] = {
                    'at': now, 'slug': slug, 'sent': sent, 'posted': posted,
                    'rules': rules, 'kind': wake['events'][-1] if wake['events'] else '',
                }
            turn_id = runtime.enqueue(sent, wake=wake)
            self.state['wakes'].append({'slug': slug, 'at': now, 'turn_id': turn_id, 'kinds': wake['events'],
                                        'spoke': None})
            self.state['wakes'] = [w for w in self.state['wakes'] if now - w['at'] < 86400]
            self.state['held'][slug] = [event for event in self.state['held'][slug] if event not in events]
            self.state['capped'] = None
            if not self.state['held'][slug]:
                self.state['deferred'].pop(slug, None)
            self._save()

    def _deliverable(self, slug, now):
        """The held events that may wake now. While the operator is engaged only their own answers (approvals) do."""
        held = self.state['held'][slug]
        presence = [event for event in held if event['kind'] == 'presence']
        reason = self.engaged(slug, now)
        if not reason:
            # Several arrivals collapse to the latest one.
            return [event for event in held if event['kind'] != 'presence'] + presence[-1:]
        if presence:
            # The operator is already here; a greeting wake would barge in. Keep a quiet note for the next turn.
            self.state['awareness'][slug] = presence[-1]['text']
            self.state['held'][slug] = held = [event for event in held if event['kind'] != 'presence']
        allowed = set(self.settings.engaged_wake_kinds)
        deliver = [event for event in held if event['kind'] in allowed]
        waiting = len(held) - len(deliver)
        changed = self.state['deferred'].get(slug) != reason
        if changed and waiting:
            print(f'autonomy: deferring {waiting} event(s) for {slug} (operator engaged: {reason})',
                  file=sys.stderr, flush=True)
        self.state['deferred'][slug] = reason
        if presence or changed:
            self._save()
        return deliver

    def _fire_timers(self, now):
        for timer in list(self.state['timers'].values()):
            if timer['due'] > now:
                continue
            late = now - timer['due']
            text = f'Timer {timer["id"]} "{timer["note"]}" is due (set {human_seconds(now - timer["created"])} ago'
            text += f', {human_seconds(late)} late).' if late > 120 else ').'
            self.post(timer['slug'], 'timer', text, timer_id=timer['id'])
            if timer['repeat']:
                while timer['due'] <= now:
                    timer['due'] += timer['repeat']
            else:
                del self.state['timers'][timer['id']]
            self._save()

    def _poll_tasks(self, now):
        settings = self.settings
        for task in list(self.state['tasks'].values()):
            if task['state'] not in ('running', 'stalled'):
                continue
            process = self._children.get(task['id'])
            code = None
            finished = False
            if process is not None:
                code = process.poll()
                finished = code is not None
            elif not _alive(task['pid']):
                finished = True
            try:
                size = os.path.getsize(task['output'])
            except FileNotFoundError:
                size = 0
            if size != task['size']:
                task['size'] = size
                task['last_growth'] = now
                if task['state'] == 'stalled':
                    task['state'] = 'running'
            if finished:
                self._children.pop(task['id'], None)
                task['ended'] = now
                task['exit_code'] = code
                if process is None:
                    task['state'] = 'lost'
                    outcome = 'ended while the core was restarting (exit code unknown)'
                else:
                    task['state'] = 'done' if code == 0 else 'failed'
                    outcome = f'{task["state"]} with exit {code}'
                tail = self._tail(task, settings.wake_output_chars).strip()
                text = f'Background task {task["id"]} "{task["label"]}" {outcome} after {human_seconds(now - task["started"])}.'
                if tail:
                    text += f' Output tail:\n{tail}'
                self.post(task['slug'], 'task', text, task_id=task['id'], state=task['state'])
            elif now - task['started'] > settings.task_max_seconds:
                from .procutil import kill_group
                try:
                    kill_group(task['pid'])
                except ProcessLookupError:
                    pass
                if process is not None:
                    process.wait()
                    task['exit_code'] = process.returncode
                self._children.pop(task['id'], None)
                task['state'] = 'timed_out'
                task['ended'] = now
                self.post(task['slug'], 'task', f'Background task {task["id"]} "{task["label"]}" was killed at the '
                          f'{human_seconds(settings.task_max_seconds)} limit.', task_id=task['id'], state='timed_out')
            elif task['state'] == 'running' and now - task['last_growth'] > settings.task_stall_seconds:
                task['state'] = 'stalled'
                self.post(task['slug'], 'task', f'Background task {task["id"]} "{task["label"]}" has printed nothing for '
                          f'{human_seconds(now - task["last_growth"])}; it is still running.', task_id=task['id'], state='stalled')
            self._save()

    def _poll_infrastructure(self, now):
        subscribers = self.state['subscriptions'].get('infrastructure') or []
        if not subscribers or now - self._infra_checked < self.settings.infra_poll_seconds:
            return
        self._infra_checked = now
        report = self.manager.infrastructure.status()
        health = {}
        for host in report.get('hosts', []):
            health[host.get('label') or f'host {host["ordinal"]}'] = host.get('health', 'unknown')
        previous, self._infra_baseline = self._infra_baseline, health
        if previous is None:
            return
        changes = [f'{name} {previous.get(name, "new")} -> {state}' for name, state in health.items()
                   if previous.get(name) != state]
        if changes:
            for slug in subscribers:
                self.post(slug, 'infra', 'Infrastructure changed: ' + '; '.join(changes) + '.')

    def probe_endpoint(self, address, timeout=None):
        """What a watched endpoint answers: 'up', 'down' or 'unknown'.

        Deliberately the smallest useful reading. This is a reachability poll, not
        a health model: a 200 and a 500 both mean the endpoint answered, so the
        distinction between them belongs to whoever runs the service, not to a
        loop that only knows how to knock. No body is read and nothing is
        interpreted, so a probe cannot mistake a rendered error page for health.
        """
        if timeout is None:
            # getattr with a fallback rather than a bare attribute: a settings object
            # assembled by hand (a test, an older saved config path) may predate this
            # field, and a missing timeout must not turn a poll into an exception.
            timeout = getattr(self.settings, 'endpoint_timeout_seconds', None) or 5.0

        try:
            with urllib.request.urlopen(address, timeout=timeout) as response:
                # read a single byte so the connection closes cleanly
                response.read(1)
            return 'up'
        except urllib.error.HTTPError:
            # It answered. A status code is an answer.
            return 'up'
        except (urllib.error.URLError, OSError, ValueError):
            return 'down'

    def _endpoints_due(self, now):
        """The endpoints to poll at `now`, chosen under the lock.

        Split from the probing on purpose. `tick()` holds the autonomy lock, and a
        poll is a network read that can block until its timeout: doing it under the
        lock would stall every timer and delivery behind one unresponsive service.
        Selection is a cheap bookkeeping step; the fetch happens outside.
        """
        records = getattr(self.config, 'systems', None)
        checked = self.state['endpoint_checked']
        due = dict(due_endpoints(records, checked, now,
                                 default_interval=getattr(self.settings, 'endpoint_poll_seconds', None)))
        # The legacy list has its own per-entry interval and its own names; it is
        # keyed separately so an id collision cannot make one shadow the other.
        for source in (getattr(self.settings, 'watch_sources', None) or []):
            name = source.get('name') if isinstance(source, dict) else None
            if not name:
                continue
            key = f'watch_source:{name}'
            last = checked.get(key)
            interval = source.get('interval_seconds')
            if type(last) in (int, float) and type(interval) in (int, float) and now - last < interval:
                continue
            due[key] = {'address': source.get('url', ''), 'role': 'declared watch source',
                        'label': name, 'interval_seconds': interval}
        return due

    def _poll_endpoints(self, now, due=None):
        """Poll watched registry endpoints and announce only a change of answer.

        `autonomy.watch_sources` was declared, validated and never read: an install
        could list a URL, a name and an interval and nothing ever fetched it. The
        registry now carries the same idea as a first-class system kind, but a
        config that already declares `watch_sources` should not have to be rewritten
        to be honoured, so both are read here.

        Same rule as the infrastructure poll: a steady fact does not wake anyone.
        Only a transition is posted, and only to sessions that subscribe.

        `due` may be supplied by a caller that selected the endpoints outside the
        lock, which is how tick() avoids probing under it.
        """
        subscribers = self.state['subscriptions'].get('infrastructure') or []
        if not subscribers:
            return
        checked = self.state['endpoint_checked']
        due = self._endpoints_due(now) if due is None else due
        if not due:
            return
        changes = []
        health = self.state['endpoint_health']
        for key, poll in due.items():
            state = self.probe_endpoint(poll['address']) if poll['address'] else 'unknown'
            checked[key] = now
            previous = health.get(key)
            health[key] = state
            if previous is not None and previous != state:
                changes.append(f"{poll['label']} {previous} -> {state}")
        self._save()
        if changes:
            for slug in subscribers:
                self.post(slug, 'infra', 'Watched endpoints changed: ' + '; '.join(changes) + '.')

    def tick(self):
        now = self.clock()
        self._refresh_intentions(now)
        with self._lock:
            self._fire_timers(now)
            self._expire_approvals(now)
            self._poll_tasks(now)
            try:
                self._poll_infrastructure(now)
            except Exception as error:
                print(f'autonomy: infrastructure poll failed ({type(error).__name__})', file=sys.stderr, flush=True)
            # Choose under the lock, probe outside it: a watched service that never
            # answers must not hold every timer in the process behind its timeout.
            due_endpoints_now = {}
            try:
                due_endpoints_now = self._endpoints_due(now) if (
                    self.state['subscriptions'].get('infrastructure')) else {}
            except Exception as error:
                print(f'autonomy: endpoint selection failed ({type(error).__name__})', file=sys.stderr, flush=True)
        if due_endpoints_now:
            try:
                self._poll_endpoints(now, due_endpoints_now)
            except Exception as error:
                print(f'autonomy: endpoint poll failed ({type(error).__name__})', file=sys.stderr, flush=True)
        with self._lock:
            self._queue_self_prompts(now)
            try:
                # Bounded A/B trials advance one step per tick. This is the only
                # place an 'auto' trust dial can change a session, and only when
                # a trial's own arm has completed and the comparison says a win.
                self.manager.advance_trials()
            except Exception as error:
                print(f'autonomy: trial advance failed ({type(error).__name__}: {error})',
                      file=sys.stderr, flush=True)
            self._deliver(now)

    def _run(self):
        while True:
            with self._wakeup:
                if self._stopping:
                    return
                self._wakeup.wait(self.settings.tick_seconds)
                if self._stopping:
                    return
            try:
                self.tick()
            except Exception as error:
                print(f'autonomy: tick failed ({type(error).__name__}: {error})', file=sys.stderr, flush=True)

    def status(self):
        now = self.clock()
        with self._lock:
            settings = self.settings
            return {'enabled': settings.enabled,
                    'timers': [{'slug': t['slug'], **self._timer_view(t, now)} for t in self.state['timers'].values()],
                    'tasks': [{'slug': t['slug'], **self._task_view(t)} for t in self.state['tasks'].values()],
                    'held': {slug: len(events) for slug, events in self.state['held'].items() if events},
                    'capped': self.state['capped'],
                    'deferred': {slug: reason for slug, reason in self.state['deferred'].items()
                                 if self.state['held'].get(slug)},
                    'approvals': [{k: a[k] for k in ('id', 'slug', 'tool', 'arguments', 'state')}
                                  for a in self.state['approvals'].values() if a['state'] == 'pending'],
                    'wakes_last_hour': sum(1 for w in self.state['wakes'] if now - w['at'] < 3600),
                    'wakes_last_day': len(self.state['wakes']),
                    'learning': self.learning.status(),
                    'suppress': dict(self.state.get('suppress') or {}),
                    'caps': {'min_wake_gap_seconds': settings.min_wake_gap_seconds,
                             'max_wakes_per_hour': settings.max_wakes_per_hour,
                             'max_wakes_per_day': settings.max_wakes_per_day}}

    def _approval_mode(self):
        try:
            status = self.manager.approval_status()
        except Exception as exc:
            self._record_floor_error('approval-status', exc)
            return 'unknown'
        self._clear_floor_error('approval-status')
        return 'yolo' if status.get('yolo') else 'safe'

    def _read_floor(self, source, read, fallback, errors=(Exception,)):
        """Read one pressure input. A failure is recorded and the fallback is returned."""
        try:
            value = read()
        except errors as exc:
            self._record_floor_error(source, exc)
            return fallback
        self._clear_floor_error(source)
        return value

    def _infrastructure_report(self):
        infrastructure = getattr(self.manager, 'infrastructure', None)
        if infrastructure is None:
            return {}
        report = self._read_floor('infrastructure-status', infrastructure.status, {})
        return report if isinstance(report, dict) else {}

    def _engine_rows(self):
        def record(exc):
            if exc is None:
                self._clear_floor_error('engine-snapshot')
            else:
                self._record_floor_error('engine-snapshot', exc)
        return engine_rows(self.config, on_error=record)

    def _record_floor_error(self, source, exc):
        record = {'source': source, 'error_type': type(exc).__name__, 'message': str(exc)}
        with self._lock:
            current = [item for item in self.state.get('floor_errors') or [] if item.get('source') != source]
            current.append(record)
            self.state['floor_errors'] = current
            self._save()

    def _clear_floor_error(self, source):
        with self._lock:
            current = list(self.state.get('floor_errors') or [])
            kept = [item for item in current if item.get('source') != source]
            if len(kept) == len(current):
                return
            self.state['floor_errors'] = kept
            self._save()

    def _definition_or_error(self):
        try:
            definition = load_definition(self.config)
        except FloorDefinitionError as exc:
            self._record_floor_error('floor-definition', exc)
            empty = default_definition()
            empty['rules'] = []
            empty['source'] = 'refused'
            return empty, str(exc)
        self._clear_floor_error('floor-definition')
        return definition, ''

    def _voice_of(self, slug):
        return bool((self.manager._metadata.get(slug) or {}).get('voice'))

    def _note_host_ordinal(self, exc):
        if exc is None:
            self._clear_floor_error('host-ordinal')
        else:
            self._record_floor_error('host-ordinal', exc)

    def _collect_rows(self, report, previous, now, brief=None):
        """Every signal the definition can name, including ones with no shipped rule."""
        if brief is None and self.manager.config.board.enabled:
            brief = self._read_floor('board-brief', self.manager.board_brief, {})
        elif brief is None:
            brief = {}
        brief = brief if isinstance(brief, dict) else {}
        sessions = self._read_floor('coordination-status', self.manager.coordination_status, [])
        floor = self.settings.infinity_slug
        rows = {
            'host': host_rows(report, previous, self._thresholds(), on_error=self._note_host_ordinal),
            'board_card': board_rows(brief, floor),
            'session': session_rows(sessions, floor),
            'engine': self._engine_rows(),
            'endpoint': self._read_floor('endpoint-health', lambda: endpoint_rows(self.state), []),
            'timer': self._read_floor('timers', lambda: timer_rows(self.state.get('timers'), now), []),
            'agenda_item': self._read_floor('agenda-items', lambda: agenda_rows(self.learning, self._voice_of), []),
            'session_card': self._read_floor(
                'session-cards', lambda: session_card_rows(brief, floor, _board_offer_line), []),
            'notice': [dict(item) for item in (self.state.get('floor_errors') or [])],
        }
        return rows, brief

    def _apply_rule_errors(self, ran_ids, caught):
        """Keep one visible error per rule that failed, and clear rules this call ran."""
        failed = {}
        for exc in caught:
            failed['definition-rule:' + str(exc.rule_id)] = exc
        for source, exc in failed.items():
            self._record_floor_error(source, exc)
        ran = {'definition-rule:' + str(rule_id) for rule_id in ran_ids}
        for item in list(self.state.get('floor_errors') or []):
            source = item.get('source') or ''
            if source in ran and source not in failed:
                self._clear_floor_error(source)

    def _retire_inactive_rule_errors(self, definition, mode):
        """Drop failures for rules that are missing or not running in this mode.

        A full reading and a save or import use this. A partial pass does not:
        it clears only the rules it actually ran.
        """
        active = active_rule_ids(definition, mode)
        prefix = 'definition-rule:'
        for item in list(self.state.get('floor_errors') or []):
            source = item.get('source') or ''
            if source.startswith(prefix) and source[len(prefix):] not in active:
                self._clear_floor_error(source)

    def _floor_reading(self, report, previous, now, brief=None, definition=None):
        error = ''
        full = definition is None
        if definition is None:
            definition, error = self._definition_or_error()
        mode = self._approval_mode()
        if full and not error:
            self._retire_inactive_rule_errors(definition, mode)
        rows, brief = self._collect_rows(report, previous, now, brief)
        caught = []
        ran = []
        hits = [] if error else evaluate(definition, rows, mode=mode, errors=caught, ran=ran)
        if not error:
            self._apply_rule_errors(ran, caught)
            if caught:
                rows['notice'] = [dict(item) for item in (self.state.get('floor_errors') or [])]
                notice_rules = [rule for rule in definition['rules'] if rule['signal'] == 'notice']
                if notice_rules:
                    solo = {
                        'version': 1, 'source': 'validated', 'compose': definition['compose'],
                        'sources': definition.get('sources') or {}, 'rules': notice_rules,
                        'draft': definition.get('draft'),
                    }
                    extra = []
                    notice_ran = []
                    notice_hits = evaluate(solo, rows, mode=mode, errors=extra, ran=notice_ran)
                    self._apply_rule_errors(notice_ran, extra)
                    hits = [hit for hit in hits if hit['signal'] != 'notice']
                    hits.extend(notice_hits)
        fired = self.state.get('rule_fired') or {}
        rules = []
        for rule in definition['rules']:
            output = [hit['text'] for hit in hits if hit['rule'] == rule['id']]
            rules.append({
                'id': rule['id'], 'name': rule['name'], 'enabled': rule['enabled'],
                'score': rule['score'], 'wake': rule['wake'], 'lines': output,
                'acting': bool(output) and rule['enabled'] and rule['wake'],
                'last_fired': fired.get(rule['id']),
                'pressure': bool(rule.get('pressure')),
                'emit': rule.get('emit') or 'each',
            })
        world = ''
        rule_error = ''
        if caught:
            rule_error = str(caught[0])
        if not error:
            try:
                _, world, _ = compose_wake(
                    definition, self.config, hits, effective_world_view(definition, self.config))
            except FloorDefinitionError as exc:
                self._record_floor_error('floor-compose', exc)
                rule_error = rule_error or str(exc)
        return {'definition': definition, 'hits': hits, 'brief': brief, 'rules': rules,
                'world_view': world, 'error': error, 'rows': rows, 'approval_mode': mode,
                'rule_error': rule_error}

    def pressure_report(self):
        """What the body measures as pressure right now, ranked, with the floor's verdict.

        Infrastructure pressure is the same reading the floor wake uses, so the board
        cannot claim a pressure the system would not act on, nor hide one it would.

        Coordination work is deliberately not the same reading, and the difference is
        said out loud rather than hidden. The floor holds a card it already offered for
        `hold_seconds` so it does not nag the same session every wake; that hold is a
        nudge-suppression memo for a model, not a statement that the work stopped
        mattering. Dropping those cards from the page would hide real work from the
        person reading it. So the page shows the full inventory and marks the ones the
        floor is currently holding back, which is a different claim from "the floor
        would act on this now."

        Before this, the page called wip_coordination_lines (seen=None) while the wake
        passed its seen record, so the two disagreed whenever a card was in its hold
        window: the page reported score 1.3 pressure for a card the floor was
        deliberately not acting on.
        """
        now = self.clock()
        with self._lock:
            report = self._infrastructure_report()
            previous = self.state.get('pressure_seen') or {}
            reading = self._floor_reading(report, previous, now)
            board_lines = [hit['text'] for hit in reading['hits'] if hit['signal'] == 'board_card'
                           and hit.get('emit') != 'offers']
            brief = reading['brief']
            steady = any(hit.get('steady') for hit in reading['hits'])
            board_set = set(board_lines)
            # The page reads the same offers function as the wake, twice: once as the
            # live inventory a person should see, once with the floor's seen record to
            # learn which of those the floor is holding back right now. The difference
            # is reported as a per-item flag rather than by dropping the line, so the
            # page can say "nobody has claimed this, the floor is not nagging about it
            # yet" instead of either hiding work or implying the floor is acting on it.
            seen = (self.state.get('coordination_seen') or {}).get(self.settings.infinity_slug) or {}
            definition = reading['definition']
            offer_errors = []
            offer_ran = []
            mode = reading.get('approval_mode') or 'safe'
            limit = self.settings.board_offer_limit
            rows = reading['rows']
            inventory_hits, _ = select_offers(
                definition, rows, None, now, limit,
                sort='priority', mode=mode, errors=offer_errors, ran=offer_ran)
            actionable_hits, _ = select_offers(
                definition, rows, seen, now, limit,
                sort='priority', mode=mode, errors=offer_errors, ran=offer_ran)
            input_hits, _ = select_offers(
                definition, rows, None, now, limit,
                sort='input', mode=mode, errors=offer_errors, ran=offer_ran)
            self._apply_rule_errors(offer_ran, offer_errors)
            other_offers = [hit for hit in inventory_hits + input_hits if hit['signal'] != 'board_card']
            inventory_hits = [hit for hit in inventory_hits if hit['signal'] == 'board_card']
            actionable_set = {hit['text'] for hit in actionable_hits if hit['signal'] == 'board_card'}
            session_hits = [hit for hit in reading['hits'] if hit['signal'] == 'session']
            coordination_items = []
            for hit in inventory_hits:
                held_back = hit['text'] not in actionable_set
                coordination_items.append({
                    'score': hit['score'], 'text': hit['text'], 'held_back': held_back,
                    'rule': hit['rule'], 'wake': False if held_back else hit['wake'],
                })
            coordination_items += [{'score': hit['score'], 'text': hit['text'], 'held_back': False,
                                    'rule': hit['rule'], 'wake': hit['wake']}
                                   for hit in session_hits]
            coordination_items += [{'score': hit['score'], 'text': hit['text'], 'held_back': False,
                                    'rule': hit['rule'], 'wake': hit['wake']}
                                   for hit in other_offers]
            reported = [(hit['score'], hit['text']) for hit in reading['hits']
                        if hit['text'] in board_set]
            # Read the configured floor slug; do not ensure() it. A read must not
            # create a session as a side effect.
            floor_slug = self.settings.infinity_slug
            meta = self.manager._metadata.get(floor_slug) or {}
            if not floor_slug or meta.get('archived') or floor_slug not in self.manager._metadata:
                floor_slug = ''
            items = [{'score': round(item['score'], 2), 'text': item['text'],
                      'held_back': item['held_back'], 'rule': item.get('rule') or '',
                      'wake': bool(item.get('wake'))} for item in coordination_items]
            host_hits = [hit for hit in reading['hits'] if hit['signal'] == 'host' and not hit.get('steady')]
            items += [{'score': round(hit['score'], 2), 'text': hit['text'], 'held_back': False,
                       'rule': hit['rule'], 'wake': hit['wake']} for hit in host_hits]
            notice_hits = [hit for hit in reading['hits'] if hit['signal'] == 'notice']
            items += [{'score': round(hit['score'], 2), 'text': hit['text'], 'held_back': False,
                       'rule': hit['rule'], 'wake': hit['wake']} for hit in notice_hits]
            return {'steady': bool(steady) and not items,
                    'items': items,
                    # Filing and age observations supplement the actionable coordination agenda.
                    'board': {'items': [{'score': round(score, 2), 'text': line, 'rule': _rule_for(reading, line)}
                                        for score, line in reported if line in board_set],
                              'open': sum(1 for card in (brief.get('cards') or [])
                                          if isinstance(card, dict) and card.get('status') in ('queued', 'running', 'blocked'))
                              if isinstance(brief, dict) else 0},
                    'floor_slug': floor_slug,
                    'suppress': dict(self.state.get('suppress') or {}),
                    'wakes_last_hour': sum(1 for w in self.state['wakes'] if now - w['at'] < 3600),
                    'wakes_last_day': len(self.state['wakes']),
                    'learning': self.learning.status(),
                    # The counts above are a summary. These are the items themselves, so
                    # the page can show what the number means instead of the number alone.
                    'open_items': self.learning.open_items(),
                    'rules': reading['rules'],
                    'world_view': reading['world_view'],
                    'errors': list(self.state.get('floor_errors') or []),
                    'last_wake': dict(self.state.get('last_wake') or {}),
                    'definition_error': reading.get('error') or reading.get('rule_error') or '',
                    'approval_mode': reading.get('approval_mode') or ''}

    def floor_view(self):
        """The definition, its live output, the last wake text, and the change log."""
        from .floor_signals import SIGNALS
        from .floor_definition import definition_path
        path = definition_path(self.config)
        error = ''
        raw = ''
        try:
            definition = load_definition(self.config)
        except FloorDefinitionError as exc:
            error = str(exc)
            definition = None
        if path.exists():
            try:
                raw = path.read_text(encoding='utf-8')
            except OSError as exc:
                error = error or ('could not be read (%s)' % exc)
        # A broken file is not replaced with the shipped rules. The editor shows
        # the error and the bytes on disk, and a save is what replaces them.
        audit = read_audit(self.config)
        audit_errors = _audit_errors(audit)
        if definition is None:
            return {
                'definition': None,
                'source': 'invalid',
                'error': error or 'The floor definition file is invalid.',
                'raw': raw,
                'signals': SIGNALS,
                'audit': audit,
                'last_wake': dict(self.state.get('last_wake') or {}),
                'errors': list(self.state.get('floor_errors') or []) + audit_errors,
                'approval_mode': '',
                'preview': None,
            }
        shown = deepcopy(definition)
        shown['compose']['world_view'] = effective_world_view(shown, self.config)
        preview = None
        try:
            report = self.pressure_report()
            preview = {'world_view': report.get('world_view') or '', 'rules': report.get('rules') or [],
                       'items': report.get('items') or []}
            last_wake = report.get('last_wake') or {}
            errors = report.get('errors') or []
            approval_mode = report.get('approval_mode') or ''
        except Exception as exc:
            error = error or str(exc)
            last_wake = dict(self.state.get('last_wake') or {})
            errors = list(self.state.get('floor_errors') or [])
            approval_mode = ''
        return {
            'definition': shown,
            'source': shown.get('source'),
            'error': error,
            'raw': raw,
            'signals': SIGNALS,
            'audit': audit,
            'last_wake': last_wake,
            'errors': list(errors) + audit_errors,
            'approval_mode': approval_mode,
            'preview': preview,
        }

    def floor_save(self, definition, actor='operator'):
        checked = validate_definition(definition)
        report = self._infrastructure_report()
        now = self.clock()
        rows, _brief = self._collect_rows(report, self.state.get('pressure_seen') or {}, now)
        exercise_definition(checked, rows)
        stored = save_definition(self.config, checked, actor or 'operator')
        self.config.autonomy.world_view = stored['compose']['world_view']
        self._retire_inactive_rule_errors(stored, self._approval_mode())
        return self.floor_view()

    def floor_import(self, text, actor='operator'):
        return self.floor_save(import_text(text), actor)

    def floor_export(self, fmt='json'):
        definition = load_definition(self.config)
        return export_text(definition, fmt)

    def floor_preview(self, definition=None):
        """What the floor would say right now, including an unsaved definition.

        A draft is rendered as written, including its world view. One rule that
        cannot run is named and skipped. The rest of the draft is still shown.
        """
        if definition is not None:
            definition = validate_definition(definition)
            definition['draft'] = True
        report = self._infrastructure_report()
        now = self.clock()
        reading = self._floor_reading(report, self.state.get('pressure_seen') or {}, now, definition=definition)
        if reading['error']:
            return {'error': reading['error'], 'world_view': '', 'kind': '', 'rules': [], 'hits': []}
        definition = reading['definition']
        rows = reading['rows']
        caught = []
        ran = []
        mode = reading.get('approval_mode') or 'safe'
        limit = self.settings.board_offer_limit
        work, _ = select_offers(
            definition, rows, None, now, limit,
            sort='priority', mode=mode, errors=caught, ran=ran)
        dispatch, _ = select_offers(
            definition, rows, None, now, limit,
            sort='input', mode=mode, errors=caught, ran=ran)
        self._apply_rule_errors(ran, caught)
        hits = [hit for hit in reading['hits'] if hit.get('pressure') or hit.get('steady') or not hit.get('wake')]
        hits.extend(dispatch)
        hits.extend(work)
        hits.extend(hit for hit in reading['hits'] if hit.get('wake') and not hit.get('pressure') and not hit.get('steady'))
        try:
            kind, text, causing = compose_wake(
                definition, self.config, hits, effective_world_view(definition, self.config))
        except FloorDefinitionError as exc:
            self._record_floor_error('floor-compose', exc)
            return {'error': str(exc), 'world_view': '', 'kind': '', 'rules': [], 'hits': hits,
                    'rule_lines': reading['rules']}
        error = reading.get('rule_error') or (str(caught[0]) if caught else '')
        return {'world_view': text, 'kind': kind, 'rules': causing, 'hits': hits,
                'rule_lines': reading['rules'], 'error': error}

    def floor_dry_run(self, rule_id, definition=None):
        if definition is None:
            definition = load_definition(self.config)
        else:
            definition = validate_definition(definition)
        rule = next((item for item in definition['rules'] if item['id'] == rule_id), None)
        if rule is None:
            raise FloorDefinitionError(str(rule_id), 'id', 'is not in this definition')
        report = self._infrastructure_report()
        solo = {'version': 1, 'source': 'validated', 'compose': definition['compose'],
                'sources': definition.get('sources') or {}, 'rules': [rule]}
        # source validated skips a second validate only when the rule already passed.
        solo = validate_definition(solo)
        reading = self._floor_reading(report, self.state.get('pressure_seen') or {}, self.clock(), definition=solo)
        if rule['emit'] == 'offers':
            hits, _ = select_offers(
                solo, reading['rows'], None, self.clock(),
                self.settings.board_offer_limit, sort=rule.get('sort'))
        else:
            hits = [hit for hit in reading['hits'] if hit['rule'] == rule_id]
        return {'rule': rule_id, 'name': rule['name'], 'enabled': rule['enabled'], 'wake': rule['wake'],
                'score': rule['score'], 'hits': hits}

    def close(self):
        with self._wakeup:
            self._stopping = True
            self._wakeup.notify_all()
        if self.thread is not None:
            self.thread.join(timeout=10)

    def _agenda_lines(self, slug):
        return list(self.learning.agenda_lines(slug))

    def _suppress_reason(self, slug, now, kind='self_prompt'):
        """Why a due agenda must not call a model, or empty when it may.

        `kind` is what is being armed. Only a held event of that same kind counts as
        already queued: a standing agenda waiting out the wake cap is not a reason to
        silence a coordination wake. A self_prompt held for 76.7 minutes would
        otherwise return 'hold:already_queued' for every coordination wake in
        that window, so unclaimed project work never reaches the floor.

        Reflection is the exception, and it keeps the broad guard on purpose. A
        reflection wake says "no urgent signal is reported, choose how to spend this
        time" — that statement is only true when nothing else is already queued for
        the floor. Arming it beside a held self_prompt would deliver both, and the
        second would contradict the first. Measured by the full suite: relaxing the
        guard for every kind made one floor wake arrive as
        ['self_prompt', 'reflection'].
        """
        engaged = self.engaged(slug, now)
        if engaged:
            return 'engaged:' + engaged
        hold = self._cap_reason(now)
        if hold:
            return 'hold:' + hold
        held = self.state['held'].get(slug) or []
        for event in held:
            if event['kind'] == kind:
                return 'hold:already_queued'
        if kind == 'reflection' and held:
            # Any pending floor wake already answers the question reflection asks.
            return 'hold:already_queued'
        return ''

    def _note_suppress(self, slug, reason):
        previous = (self.state.get('suppress') or {}).get(slug)
        if previous == reason:
            return
        self.state.setdefault('suppress', {})[slug] = reason
        print(f'autonomy: suppressing self_prompt for {slug} ({reason})', file=sys.stderr, flush=True)
        self._save()

    def _intention_tool_name(self, runtime):
        try:
            schemas = runtime.session.tools.schemas()
        except Exception:
            return ''
        for schema in schemas:
            function = schema.get('function') or {}
            name = function.get('name') or ''
            if name.endswith('intention_status'):
                return name
        return ''

    def _refresh_intentions(self, now):
        if not self.settings.enabled or not self.settings.self_prompt_enabled:
            return
        gap = self.settings.self_prompt_gap_seconds
        if self._intentions_at and gap > 0 and now - self._intentions_at < gap:
            return
        runtimes = getattr(self.manager, '_runtimes', {})
        for slug, runtime in list(runtimes.items()):
            name = self._intention_tool_name(runtime)
            if not name:
                continue
            try:
                result = runtime.session.tools.execute(name, {})
            except Exception as exc:
                # The refresh continues for the other sessions. The failure is a floor
                # error, not an empty intention list.
                self._record_floor_error('intention-tool:' + slug, exc)
                continue
            self._clear_floor_error('intention-tool:' + slug)
            titles = intention_titles(result)
            if titles:
                self.learning.adopt_observed(slug, titles)
        self._intentions_at = now

    def _floor_slug(self):
        """The floor session. Floor wakes stay here so the log and layers stay one history."""
        slug = self.settings.infinity_slug
        try:
            self.manager.ensure(slug, self.config.floor_label())
        except (ValueError, RuntimeError, OSError):
            return ''
        meta = self.manager._metadata.get(slug) or {}
        if meta.get('archived'):
            return ''
        return slug

    def _arm_or_post(self, slug, now, body, last_map, gap, kind='self_prompt', rules=None):
        reason = self._suppress_reason(slug, now, kind)
        if reason:
            self._note_suppress(slug, reason)
            return
        self.state.setdefault('suppress', {}).pop(slug, None)
        last = last_map.get(slug)
        if last is None:
            last_map[slug] = now
            self._save()
            return
        if gap > 0 and now - last < gap:
            return
        last_map[slug] = now
        self.post(slug, kind, body, rules=list(rules or []))
        return True

    def _queue_self_prompts(self, now):
        if not self.settings.enabled or not self.settings.self_prompt_enabled:
            return
        targets = list(self.learning.wake_targets())
        # Unclaimed board work reaches sessions that have no agenda of their own yet.
        # Otherwise the board is only visible to sessions already busy with something.
        for slug in self._board_targets():
            if slug not in targets:
                targets.append(slug)
        gap = self.settings.self_prompt_gap_seconds
        last_map = self.state.setdefault('self_prompt_at', {})
        settled_answerable = []
        for slug in targets:
            if slug not in self.manager._metadata or self.manager._metadata[slug].get('archived'):
                continue
            # A voice session is a place to talk, not a mission. It has no agenda of
            # its own and must not be woken by the floor: a wake there would spend a
            # reasoning turn on work nobody asked for, and the next thing a speaker
            # heard would be the result of a turn they never requested.
            if self.manager._metadata[slug].get('voice'):
                continue
            settled = self.learning.settle_answerable(slug)
            seen = (self.state.get('agenda_seen') or {}).get(slug) or {}
            held_ids = self.learning.unchanged_lesson_ids(slug, seen, now, 6 * 3600)
            # Questions and intentions were never deduplicated, so one parked question kept
            # the agenda non-empty forever and woke this session every gap period with the
            # same text. Hold them the same way lessons are held.
            held_ids = held_ids + self.learning.unchanged_item_ids(slug, seen, now, 6 * 3600)
            text_lines = self.learning.agenda_lines(slug, held_ids)
            board_lines, board_seen = self._own_board_lines(slug, now)
            text_lines = text_lines + board_lines
            if not text_lines:
                if held_ids:
                    self._note_suppress(slug, 'agenda_unchanged')
                elif settled:
                    self._note_suppress(slug, 'answerable')
                    settled_answerable.append(slug)
                else:
                    self.state.setdefault('suppress', {}).pop(slug, None)
                continue
            body = 'Standing agenda is due.\n' + '\n'.join(text_lines)
            posted = self._arm_or_post(slug, now, body, last_map, gap)
            if posted:
                record = self.state.setdefault('agenda_seen', {}).setdefault(slug, {})
                for lesson in self.learning._open_lessons(slug):
                    if lesson['id'] in held_ids:
                        continue
                    record[lesson['id']] = {'fp': self.learning.lesson_fingerprint(lesson), 'at': now}
                for item in list(self.learning._open_questions(slug)) + list(self.learning._open_intentions(slug)):
                    if item['id'] in held_ids:
                        continue
                    record[item['id']] = {'fp': self.learning.item_fingerprint(item), 'at': now}
                self.state.setdefault('board_seen', {}).setdefault(slug, {}).update(board_seen)
                self._save()
        self._queue_floor_wake(now, gap)

    def _board_targets(self):
        """Live sessions that own a card needing action.

        A session wakes for its own cards. Another session's card is readable on the
        board but never wakes this one; awareness is not a call to act. An unclaimed
        card reaches the floor session's coordination agenda instead.
        """
        if not self.manager.config.board.enabled or self.settings.board_offer_limit <= 0:
            return []
        brief = self._read_floor('board-brief', self.manager.board_brief, {},
                                 errors=(ValueError, RuntimeError, OSError))
        cards = brief.get('cards') if isinstance(brief, dict) else None
        if not cards:
            return []
        owners = []
        for card in cards:
            owner = card.get('owner') or ''
            if (not owner or owner == self.settings.infinity_slug
                    or card.get('status') not in CLAIMABLE_STATES):
                continue
            if owner not in owners:
                owners.append(owner)
        return [slug for slug in owners
                if slug in self.manager._metadata and not self.manager._metadata[slug].get('archived')
                and not self.manager._metadata[slug].get('voice')]

    def _thresholds(self):
        """Registry thresholds keyed by host ordinal, resolved from config.

        A host row carries no name on purpose, so the join happens here and the
        floor never sees a selector. Returns {} when nothing is declared, which
        leaves every existing threshold behaviour exactly as it was.
        """
        config = getattr(self, 'config', None) or getattr(getattr(self, 'manager', None), 'config', None)
        if config is None:
            return {}
        try:
            resolved = thresholds_by_ordinal(getattr(config, 'systems', None),
                                             list(getattr(config, 'hosts', {}) or {}))
        except Exception as exc:
            # An empty registry would hide this and fall through to the disk default.
            self._record_floor_error('systems-registry', exc)
            return {}
        self._clear_floor_error('systems-registry')
        return resolved

    def _session_pressure_lines(self):
        definition, error = self._definition_or_error()
        if error:
            return []
        rows = session_rows(self.manager.coordination_status(), self.settings.infinity_slug)
        session_rules = [rule for rule in definition['rules']
                         if rule['signal'] == 'session' and rule.get('emit') != 'offers']
        solo = {
            'version': 1, 'source': 'validated', 'compose': definition['compose'],
            'sources': definition.get('sources') or {}, 'rules': session_rules,
        }
        caught = []
        ran = []
        hits = evaluate(solo, {'session': rows}, mode=self._approval_mode(), errors=caught, ran=ran)
        self._apply_rule_errors(ran, caught)
        return [hit['text'] for hit in hits if hit['signal'] == 'session']

    def _queue_floor_wake(self, now, gap):
        """The floor session dispatches work and responds to session and host pressure.

        The body is the floor definition: rules decide the lines, compose templates
        decide the sentences around them, and a refused definition does not fall
        back to the shipped rules.
        """
        if not self.settings.standing_floor:
            return
        floor_slug = self._floor_slug()
        if not floor_slug:
            return
        runtime = self.manager._runtimes.get(floor_slug)
        if runtime is not None and (runtime.operator_stopped or not runtime.idle()):
            self._note_suppress(floor_slug, 'operator_stopped' if runtime.operator_stopped else 'busy')
            return
        brief = {}
        if self.manager.config.board.enabled:
            brief = self._read_floor('board-brief', self.manager.board_brief, {},
                                     errors=(ValueError, RuntimeError, OSError))
        report = self._infrastructure_report()
        previous = self.state.get('pressure_seen') or {}
        reading = self._floor_reading(report, previous, now, brief=brief)
        if reading['error']:
            return
        definition = reading['definition']
        seen = (self.state.get('coordination_seen') or {}).get(floor_slug) or {}
        caught = []
        ran = []
        mode = reading.get('approval_mode') or 'safe'
        work_hits, coordination_seen = select_offers(
            definition, reading['rows'], seen, now,
            self.settings.board_offer_limit, sort='priority',
            mode=mode, errors=caught, ran=ran)
        dispatch_hits, dispatch_seen = self._floor_dispatch_lines(
            brief, now, definition=definition, errors=caught, mode=mode,
            rows_by_signal=reading['rows'], ran=ran)
        self._apply_rule_errors(ran, caught)
        hits = [hit for hit in reading['hits'] if hit.get('pressure') or hit.get('steady') or not hit.get('wake')]
        hits.extend(dispatch_hits)
        hits.extend(work_hits)
        hits.extend(hit for hit in reading['hits']
                    if hit.get('wake') and not hit.get('pressure') and not hit.get('steady'))
        try:
            kind, text, causing = compose_wake(
                definition, self.config, hits, effective_world_view(definition, self.config))
        except FloorDefinitionError as exc:
            self._record_floor_error('floor-compose', exc)
            return
        last_map = self.state.setdefault(kind + '_at', {})
        if kind != 'reflection':
            last_map.setdefault(floor_slug, now - gap)
        posted = self._arm_or_post(floor_slug, now, text, last_map, gap, kind=kind, rules=causing)
        if posted:
            self.state['pressure_seen'] = pressure_seen(
                report, now, thresholds=self._thresholds(), definition=definition,
                on_error=self._note_host_ordinal)
            if coordination_seen:
                self.state.setdefault('coordination_seen', {}).setdefault(floor_slug, {}).update(coordination_seen)
            if dispatch_seen:
                self.state.setdefault('dispatch_seen', {}).setdefault(floor_slug, {}).update(dispatch_seen)
            fired = self.state.setdefault('rule_fired', {})
            for rule_id in causing:
                fired[rule_id] = now
            self.state['last_wake'] = {
                'at': now, 'slug': floor_slug, 'kind': kind, 'rules': causing,
                'posted': text, 'sent': '',
            }
            self._save()

    def _floor_dispatch_lines(self, brief, now, definition=None, errors=None, mode='safe',
                              rows_by_signal=None, ran=None):
        """Floor ownership is a dispatch obligation, never personal execution work.

        The hold is the floor definition's stale_seconds, the same hold coordination
        offers use, unless this rule sets its own. Every offers rule with sort
        input runs, each against the rows of its own signal.
        """
        limit = self.settings.board_offer_limit
        definition = definition or default_definition()
        floor = self.settings.infinity_slug
        seen = (self.state.get('dispatch_seen') or {}).get(floor) or {}
        if rows_by_signal is None:
            rows_by_signal = {}
            if self.manager.config.board.enabled:
                rows_by_signal['board_card'] = board_rows(brief, floor)
        if limit < 0:
            limit = 0
        return select_offers(
            definition, rows_by_signal, seen, now, limit,
            sort='input', mode=mode, errors=errors, ran=ran)

    def _own_board_lines(self, slug, now):
        """This session's own cards that still need action.

        Worker sessions wake for the work they own. The floor only dispatches through
        its separate coordination wake; it never receives personal card work. The
        whole board stays readable through wip_board so a session can see where its work
        overlaps another's, but seeing is not waking. A session that has already been
        told about a card is not told again; the card's version is the fingerprint, so an
        edit or a status move re-arms it.

        One shape of "already been told" is wrong.

        A note this session wrote is not news to it. The fingerprint included the
        version, and append_note() moves the version, so the act of recording progress
        re-armed the card that progress was recorded on. Most card-naming agenda
        wakes then fire within 15 minutes of this session's own board_note on that
        same card, and the delivered line quotes the note back. That is a turn spent rereading what the
        turn had just written. `updated_by` is already carried on the card, so the card
        holds the evidence of who moved it.

        The rule is narrow on purpose: a version whose last writer is this session does
        not re-arm the offer while the status is unchanged. A status move still re-arms
        even when this session made it, because a status is a statement about the work
        rather than a record of it — a card this session moved to 'failed' has to come
        back for another attempt. A version moved by anyone else still re-arms, and so
        does this session's own note written after such a move, because the earlier
        fingerprint then no longer describes the card's status.

        A card parked on a person still offers itself, deliberately: the offer line
        names the pending question, so the session can see that what it owes is an
        answer rather than work. test_a_blocked_offer_names_the_question_it_is_waiting_on
        pins that decision. Whether such a card should wake at all is a separate
        question and is left as it stands.
        """
        if slug == self.settings.infinity_slug or not self.manager.config.board.enabled:
            return [], {}
        brief = self._read_floor('board-brief', self.manager.board_brief, {},
                                 errors=(ValueError, RuntimeError, OSError))
        cards = brief.get('cards') if isinstance(brief, dict) else None
        if not cards:
            return [], {}
        seen = (self.state.get('board_seen') or {}).get(slug) or {}
        lines = []
        record = {}
        limit = self.settings.board_offer_limit
        for item in cards:
            if (item.get('owner') or '') != slug:
                continue
            if item.get('status') not in CLAIMABLE_STATES:
                continue
            # The card's identity is its status and the version that matters to this
            # session. Age is deliberately excluded: including it meant a card changed
            # bucket every sixty seconds, so every wake offered it again forever.
            identity = f'{item["status"]}'
            own_note = (item.get('updated_by') or '') == slug
            fingerprint = identity + ':own' if own_note else identity + f':{item.get("version")}'
            # The key is the card's own identity, not its position. An ordinal is
            # assigned by enumeration at read time (board.py:403) and the journal
            # does not store one, so removing a card renumbers every card after it.
            # Keyed on a position, a record left by one card suppresses whichever
            # card later lands on that number, and a card that only shifts position
            # is re-offered as news. Keyed on card_id, the record follows the card.
            card_key = item.get('card_id') or str(item['ordinal'])
            previous = seen.get(card_key)
            if previous == fingerprint:
                continue
            # A version this session wrote is not news, so a note written after some
            # earlier offer does not re-arm the card while its status is unchanged.
            # Comparing the status prefix rather than the whole fingerprint is what
            # keeps that true after another session moved the card and this session
            # then recorded its own follow-up work.
            if own_note and isinstance(previous, str) and previous.startswith(identity + ':'):
                continue
            if len(lines) >= limit:
                break
            lines.append(_board_offer_line(item))
            record[card_key] = fingerprint
        return lines, record

    def _refresh_agenda(self, slug, text, now):
        """Rebuild a composed agenda against the board as it is now.

        A self_prompt is composed once and stored as frozen text, then delivered
        whenever the wake caps allow, which can be tens of minutes later. In
        that gap a card's status moves and its note grows, so a held agenda
        arrives quoting a state the board has left: it can name a card as
        "blocked" long after that card was closed succeeded, and the cost is a
        turn spent re-reading the board to find out which of the two is true.

        Only the board half is rebuilt. Lessons, questions and intentions were
        deduplicated against agenda_seen at compose time; recomputing them here
        would hold everything and deliver nothing, which is the opposite fault.

        The cards are looked up by the ordinals the composed text named, not by
        re-running _own_board_lines: that function filters on board_seen, which
        was written when this agenda was armed, so an unchanged card would be
        filtered out here and its still-true line dropped.

        Returns the text to deliver, or '' when nothing in the agenda is still
        true — a wake whose only content was live state that has since settled
        should not be sent at all.
        """
        header = 'Standing agenda is due.'
        if not text.startswith(header):
            return text
        body = text[len(header):].lstrip('\n')
        # Join continuation lines back onto the line they belong to before
        # classifying anything. Every line _queue_self_prompts emits carries a
        # prefix (Lesson/Question/Intention/Your board card); an unprefixed line
        # can only be the tail of a board note or pending question that wrapped.
        # Held text composed before _one_line existed still has those breaks, so
        # the classifier has to survive them rather than trust the composer.
        prefixes = ('Your board card ', 'Lesson ', 'Reminder ', 'Question ', 'Intention ')
        logical = []
        for line in body.split('\n'):
            if not line:
                continue
            if logical and not line.startswith(prefixes):
                logical[-1] += ' ' + line
            else:
                logical.append(line)
        static_lines = []
        named = []
        for line in logical:
            if line.startswith('Your board card '):
                try:
                    ordinal = int(line[len('Your board card '):].split(':', 1)[0])
                except ValueError:
                    continue
                # A note is free prose, so a continuation can begin with this very
                # prefix — a note that quotes a delivered wake line does exactly
                # that. The join above treats such a line as a new logical line,
                # so it arrives here as a second offer for an ordinal already
                # named, and _board_offer_line would rebuild it into a duplicate.
                # The composer emits at most one line per ordinal, so keeping the
                # first is lossless: it cannot drop an offer the composer made.
                if ordinal not in named:
                    named.append(ordinal)
                continue
            static_lines.append(line)
        static_lines = self._drop_settled_items(slug, static_lines)
        if not named:
            if not static_lines:
                return ''
            return header + '\n' + '\n'.join(static_lines)
        try:
            brief = self.manager.board_brief()
        except (ValueError, RuntimeError, OSError) as exc:
            # The board is unreadable now; the composed lines are the best
            # available statement of it, so deliver them rather than nothing.
            self._record_floor_error('board-brief', exc)
            return text
        self._clear_floor_error('board-brief')
        cards = brief.get('cards') if isinstance(brief, dict) else None
        if not cards:
            return text
        live = {card['ordinal']: card for card in cards if isinstance(card, dict)}
        board_lines = []
        for ordinal in named:
            card = live.get(ordinal)
            if card is None:
                continue
            if (card.get('owner') or '') != slug:
                continue
            if card.get('status') not in CLAIMABLE_STATES:
                continue
            board_lines.append(_board_offer_line(card))
        lines = static_lines + board_lines
        if not lines:
            return ''
        return header + '\n' + '\n'.join(lines)

    def _drop_settled_items(self, slug, lines):
        """Drop agenda lines whose lesson, question or intention is no longer open.

        A held agenda can quote work that finished while it waited: it can name
        a lesson this session already retired and tell the turn to load that
        skill and run the gate. The cost is the same one the board half already paid — a turn spent
        re-reading state to find out which claim is true.

        This is a filter, not a recompute. It removes lines and never adds any,
        so it cannot reintroduce the opposite fault the docstring above warns
        about: recomputing here would hold everything and deliver nothing.
        A line whose id cannot be read is kept, because an unreadable id is not
        evidence that the work settled.
        """
        lessons = self.learning._lessons
        questions = self.learning._questions
        intentions = self.learning._intentions
        kept = []
        for line in lines:
            kind, _, rest = line.partition(' ')
            item_id, _, _ = rest.partition(':')
            item_id = item_id.strip()
            # A reminder is a lesson row too. learning.agenda_lines emits
            # "Reminder <id>:" for a failed-turn row, with the same id in the
            # same store. Both prefixes are recognized, so a settled reminder
            # leaves the agenda.
            if kind in ('Lesson', 'Reminder'):
                item = lessons.get(item_id)
                if item is not None and item.get('status') not in OPEN:
                    continue
            elif kind == 'Question':
                item = questions.get(item_id)
                if item is not None and item.get('status') != 'open':
                    continue
            elif kind == 'Intention':
                item = intentions.get(item_id)
                if item is not None and item.get('status') != 'committed':
                    continue
            kept.append(line)
        return kept

    def _register_learning_tools(self, runtime, registry, slug, string):
        boolean = {'type': 'boolean'}

        def record_lesson(trigger, failure, check, skill='', evidence_path=''):
            return self.learning.record_lesson(slug, trigger, failure, check, skill=skill, evidence_path=evidence_path)

        def lesson_bind(lesson_id, skill):
            return self.learning.bind_skill(slug, lesson_id, skill)

        def lesson_check(lesson_id, passed, note='', retire=False):
            return self.learning.check(slug, lesson_id, passed, note=note, retire=retire)

        def park_question(text):
            return self.learning.park_question(slug, text)

        def close_question(question_id):
            return self.learning.close_question(slug, question_id)

        def commit_intention(title):
            return self.learning.commit_intention(slug, title)

        def abandon_intention(intention_id, condition):
            return self.learning.abandon_intention(slug, intention_id, condition)

        def weather_judged(key, name='', verdict='', log_mtime=0):
            fields = {}
            if name:
                fields['name'] = str(name)[:120]
            if verdict:
                fields['verdict'] = str(verdict)[:40]
            if log_mtime:
                fields['log_mtime'] = float(log_mtime)
            return record_judgment(self.manager.config.home, key, **fields)

        registry.register('record_lesson', 'Save a lesson the harness will wake you with: trigger, failure, and check. '
                          'Optional skill names the procedure to load next time. Optional evidence_path is a file the '
                          'harness can check without a model. This is not a continuity layer.',
                          object_schema({'trigger': string, 'failure': string, 'check': string, 'skill': string,
                                         'evidence_path': string},
                                        ['trigger', 'failure', 'check']), record_lesson)
        registry.register('lesson_bind', 'Attach a shelf skill to one of your open lessons.',
                          object_schema({'lesson_id': string, 'skill': string}, ['lesson_id', 'skill']), lesson_bind)
        registry.register('lesson_check', 'Record whether a lesson passed. retire=true stops it from waking you.',
                          object_schema({'lesson_id': string, 'passed': boolean, 'note': string, 'retire': boolean},
                                        ['lesson_id', 'passed']), lesson_check)
        registry.register('list_lessons', 'List your open lessons.', object_schema({}),
                          lambda: self.learning.list_lessons(slug))
        number = {'type': 'number'}
        registry.register('weather_judged',
                          'Record a weather fact you just judged so the next wake does not repeat it. '
                          'key "load" with name of the process, or key "session:<slug>" with verdict finished or stuck '
                          'and log_mtime of the log you read. A new log, or six hours, brings it back.',
                          object_schema({'key': string, 'name': string, 'verdict': string, 'log_mtime': number}, ['key']),
                          weather_judged)
        registry.register('park_question', 'Leave a standing question the harness will wake you with until you close it.',
                          object_schema({'text': string}, ['text']), park_question)
        registry.register('close_question', 'Close a standing question so it stops waking you.',
                          object_schema({'question_id': string}, ['question_id']), close_question)
        registry.register('commit_intention', 'Commit to one intention. It stays until you abandon it as done, failed, or dropped.',
                          object_schema({'title': string}, ['title']), commit_intention)
        registry.register('abandon_intention', 'Close a committed intention. condition must be done, failed, or dropped.',
                          object_schema({'intention_id': string, 'condition': string}, ['intention_id', 'condition']),
                          abandon_intention)
        registry.register('list_intentions', 'List your committed intentions.', object_schema({}),
                          lambda: self.learning.list_intentions(slug))

    # Tools

    def register_tools(self, runtime, registry):
        if not self.settings.enabled:
            return
        registry.defer = lambda name, arguments, run: self.request_approval(runtime, name, arguments, run)
        slug = runtime.slug
        string = {'type': 'string'}
        number = {'type': 'number'}

        if slug == self.settings.infinity_slug:
            registry.register('coordination_status',
                              'Read all session metadata plus active requests, current activity, recent messages and '
                              'delegated agents of loaded sessions. Compare progress to the shared board and operator intent.',
                              object_schema({}), self.manager.coordination_status)
            registry.register('steer_session',
                              'Guide an active session out of drift or ask it to preserve work and pause. '
                              'Does not resume stopped sessions or enqueue idle work. This guidance is not an immediate Stop.',
                              object_schema({'slug': string, 'text': string}, ['slug', 'text']),
                              self.manager.coordinate_steer)

        def rest(note=''):
            runtime.session.rest_requested = True
            return {'resting': True, 'note': note}

        registry.register('rest', 'Choose quiet reflection or rest for this wake. Suppresses a proactive announcement; future events remain available.',
                          object_schema({'note': string}), rest)
        registry.register('set_timer', 'Set this session\'s own wake timer. It is a tool of this session, not the system floor. '
                          'You are woken with the note when it is due; then decide freely. Use it to keep watching a session, '
                          'process, or job and continue the work. Give in_seconds or an ISO time in at; repeat_seconds makes it recur.',
                          object_schema({'note': string, 'in_seconds': number, 'at': string, 'repeat_seconds': number},
                                        ['note']),
                          lambda note, in_seconds=None, at=None, repeat_seconds=None:
                          self.set_timer(slug, note, in_seconds, at, repeat_seconds))
        registry.register('list_timers', 'List your pending wake timers.', object_schema({}),
                          lambda: self.list_timers(slug))
        registry.register('cancel_timer', 'Cancel one of your wake timers by id.',
                          object_schema({'timer_id': string}, ['timer_id']), lambda timer_id: self.cancel_timer(slug, timer_id))
        remotes = sorted(name for name, remote in self.config.remotes.items() if remote.enabled)

        def background_task(action, command=None, label=None, remote=None, cwd=None, task_id=None, note=None):
            if action == 'start':
                if not command:
                    raise ValueError('start needs command')
                arguments = {'action': 'start', 'command': command, **({'remote': remote} if remote else {})}
                return self.gate(runtime, registry, 'background_task', arguments,
                                 lambda: self.start_task(slug, label or command[:60], command, remote=remote, cwd=cwd))
            if action == 'continue':
                if not note:
                    raise ValueError('continue needs note')
                return self.continue_later(slug, note)
            if action == 'status':
                return self.task_status(slug, task_id)
            if action == 'stop':
                if not task_id:
                    raise ValueError('stop needs task_id')
                return self.stop_task(slug, task_id)
            raise ValueError('action must be start, status, stop or continue')

        properties = {'action': {'type': 'string', 'enum': ['start', 'status', 'stop', 'continue']},
                      'command': string, 'label': string, 'cwd': string, 'task_id': string, 'note': string}
        if remotes:
            properties['remote'] = {'type': 'string', 'enum': remotes}
        registry.register('background_task', 'Run work in the background and get woken when it finishes or stalls. '
                          'start runs a shell command (locally, or on a remote); status/stop take task_id; continue '
                          'queues a note that wakes you after this turn so you can keep working after replying. '
                          'start follows the approval policy; outside Unleashed it asks the operator and wakes you '
                          'with their answer.',
                          object_schema(properties, ['action']), background_task, risky=False)
        registry.register('subscribe', 'Subscribe to wake events. Topics: infrastructure (host health changes).',
                          object_schema({'topic': {'type': 'string', 'enum': ['infrastructure']},
                                         'enabled': {'type': 'boolean'}}, ['topic']),
                          lambda topic, enabled=True: self.subscribe(slug, topic, enabled))
        self._register_learning_tools(runtime, registry, slug, string)
