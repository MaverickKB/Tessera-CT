"""Bounded, read-only projections. No journal constructors, repairs or credentials."""

import errno
import json
import os
from pathlib import Path
import stat


TASK_STATES = {'queued', 'running', 'succeeded', 'failed', 'cancelled'}
DECISION_CATEGORIES = {'intake', 'tool_gate', 'verification', 'memory', 'route', 'needs_memory', 'tool_risk', 'judge'}
CATEGORY_ALIASES = {'verification_batch': 'verification', 'output_gate': 'verification',
                    'action_gate': 'tool_gate',
                    'compaction': 'judge', 'swarm': 'judge', 'swarm_gate': 'judge', 'message_gate': 'judge', 'ask_human_gate': 'judge'}
MAX_SAFE_INTEGER = 2 ** 53 - 1


def configured_path(home, name):
    path = Path(name).expanduser()
    return path if path.is_absolute() else Path(home) / path


def read_snapshot(path, settings, byte_limit=None):
    """Read one committed prefix from a regular file, without following a symlink."""
    present, records, modified, size, _ = read_snapshot_reasoned(path, settings, byte_limit)
    return present, records, modified, size


def read_snapshot_reasoned(path, settings, byte_limit=None):
    """Same read, plus the reason when there is no projection.

    A caller that shows a person a blank panel needs to say why it is blank. The
    four-tuple above cannot: `records is None` covers a file over the reader cap, a
    line over the per-line cap, a line that is not JSON, and a line that is JSON but
    not an object. Those are four different repairs, and a reader told only "no
    cards" has no way to tell them apart. The reason is a short sentence naming the
    fact, and None when there is a projection.

    Each reason is a clause that reads after the file's name, so a caller can prefix
    it without producing "the board journal the file could not be opened".

    This is the single implementation; `read_snapshot` drops the reason so its nine
    existing call sites keep their shape.
    """
    try:
        from .durability import open_regular
        descriptor = open_regular(path)
    except FileNotFoundError:
        return False, [], 0, 0, None
    except OSError as error:
        if error.errno == errno.ELOOP:
            # O_NOFOLLOW refuses a symlink. Naming that is the difference between a
            # person replacing the link and a person hunting a permissions problem.
            return None, None, 0, 0, 'is a symlink, and the reader does not follow links'
        return None, None, 0, 0, 'could not be opened'
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        cap = settings.max_file_bytes if byte_limit is None else min(byte_limit, settings.max_file_bytes)
        if not stat.S_ISREG(info.st_mode):
            return None, None, info.st_mtime_ns, 0, 'is not a regular file'
        if info.st_size > cap:
            return True, None, info.st_mtime_ns, 0, (
                'is %d bytes and the reader cap is %d bytes' % (info.st_size, cap))
        raw = stream.read(info.st_size)
        if len(raw) != info.st_size:
            return True, None, info.st_mtime_ns, 0, 'changed while it was being read'
    rows = []
    for number, line in enumerate(raw.splitlines(keepends=True), 1):
        if not line.endswith(b'\n'):
            break  # uncommitted tail is neither repaired nor counted
        if len(line) > settings.max_line_bytes:
            return True, None, info.st_mtime_ns, 0, (
                'line %d is %d bytes and the per-line cap is %d bytes'
                % (number, len(line), settings.max_line_bytes))
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return True, None, info.st_mtime_ns, 0, 'line %d is not valid JSON' % number
        if not isinstance(row, dict):
            return True, None, info.st_mtime_ns, 0, (
                'line %d is JSON but not an object' % number)
        rows.append(row)
    return True, rows, info.st_mtime_ns, info.st_size, None


def task_status(config):
    result = {'enabled': config.swarm.enabled, 'configured': bool(config.swarm.tasks_file)}
    if not result['enabled'] or not result['configured']:
        return result
    path = configured_path(config.home, config.swarm.tasks_file)
    _, records, _, _ = read_snapshot(path, config.status)
    if records is None:
        return result
    latest = {}
    for sequence, record in enumerate(records, 1):
        task = record.get('task')
        if type(record.get('sequence')) is not int or record.get('sequence') != sequence or not isinstance(task, dict) or not isinstance(task.get('id'), str):
            return result
        status = task.get('status')
        if not isinstance(status, str) or status not in TASK_STATES:
            return result
        latest[task['id']] = task
    rows = []
    # task_limit is a bound, not a filter. Keeping the newest N and saying nothing
    # made a capped read look like the whole task store, so a reader counted the
    # visible rows as the total.
    selected = list(latest.values())[-config.status.task_limit:]
    omitted = len(latest) - len(selected)
    for task in selected:
        row = {'status': task['status']}
        attempt = task.get('attempt')
        if type(attempt) is int and 0 <= attempt <= MAX_SAFE_INTEGER:
            row['attempt'] = attempt
        rows.append(row)
    result['tasks'] = rows
    if omitted > 0:
        result['tasks_omitted'] = omitted
        result['task_limit'] = config.status.task_limit
    return result


def judge_status(config, session_roots, readiness=(), *, include_decisions=True):
    from .jev import config_readiness
    observed = config_readiness(config)
    # Only already-running judges supply observations. Status never creates a
    # judge, resolves a credential, or contacts a provider to obtain one.
    if observed['state'] == 'ready':
        for snapshot in readiness:
            if isinstance(snapshot, dict) and snapshot.get('state') == 'unavailable':
                observed = {'state': 'unavailable',
                            'reason': 'A running judge reported an unavailable response; no status probe was made.'}
                break
    result = {'enabled': config.jev.enabled,
              'configured': observed['state'] in ('ready', 'unavailable'),
              'state': observed['state'], 'reason': observed['reason'],
              'decision_log_present': False}
    if not include_decisions:
        result.pop('decision_log_present')
        return result
    paths = []
    for root in session_roots:
        paths.append(Path(root) / 'decisions.jsonl')
    board = configured_path(config.home, config.swarm.tasks_file)
    paths.append(board.with_name(board.stem + '.decisions.jsonl'))
    count = 0
    remaining_bytes = config.status.max_total_bytes
    complete = len(paths) <= config.status.max_logs
    newest = -1
    category = None
    for path in paths[:config.status.max_logs]:
        present, records, modified, size = read_snapshot(path, config.status, byte_limit=remaining_bytes)
        remaining_bytes -= size
        if present is True:
            result['decision_log_present'] = True
        if records is None:
            complete = False
            continue
        count += len(records)
        if records and modified >= newest:
            point = records[-1].get('point', records[-1].get('kind'))
            category = CATEGORY_ALIASES.get(point, point) if isinstance(point, str) else None
            newest = modified
    if not complete and not result['decision_log_present']:
        result.pop('decision_log_present')
    if complete and count <= MAX_SAFE_INTEGER:
        result['decision_count'] = count
        if count and category in DECISION_CATEGORIES:
            result['last_decision_category'] = category
    return result
