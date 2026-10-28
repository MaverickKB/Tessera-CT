"""Edit the live config.toml in place: one key per edit, other lines preserved.

Every save is validated by loading the edited file exactly as the core would, the
previous file is backed up with a timestamp, and the write is atomic.
"""

from datetime import datetime, timezone
import contextlib
from . import locking as fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import tempfile
import tomllib

from .secret_names import is_secret_name

_HEADER = re.compile(r'^\s*\[([^\[\]]+)\]\s*(#.*)?$')
_BARE = re.compile(r'^[A-Za-z0-9_-]+$')
# ConfigEditor names a backup config.before-<label>-<stamp>.toml, and a same-second
# collision adds -<serial>. Integrations use the same prefix. Nothing else matches.
_BACKUP_NAME = re.compile(r'^config\.before-[a-z0-9-]+-\d{8}T\d{6}(?:-\d+)?\.toml$')

# One lock per config file, held across the whole read-modify-validate-write.
# The atomic replace protects against a half-written file; it does nothing about
# two callers who each read the same original text and then replace in turn, so
# the second save silently discards the first. Threading the transaction is what
# makes a concurrent save either land or wait, never vanish.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


@contextlib.contextmanager
def _transaction(path):
    """Hold the in-process lock and the cross-process lock for one save."""
    with _path_lock(path):
        handle = open(str(path) + '.lock', 'a+')
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _path_of(header):
    data = tomllib.loads('[' + header + ']\n')
    path = []
    while isinstance(data, dict) and len(data) == 1:
        key = next(iter(data))
        path.append(key)
        data = data[key]
    return tuple(path)


def _key_of(line):
    if '=' not in line or line.lstrip().startswith('#'):
        return None
    try:
        data = tomllib.loads(line.split('=', 1)[0] + '= 0\n')
    except tomllib.TOMLDecodeError:
        return None
    return next(iter(data)) if len(data) == 1 and isinstance(next(iter(data.values())), int) else None


def _quote(text):
    return json.dumps(text, ensure_ascii=False)


def toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Numbers must be finite')
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(f'{_quote(k)} = {toml_value(v)}' for k, v in value.items()) + '}'
    raise ValueError('Unsupported value type')


def remove_toml_table(text, path):
    """Remove the table at path and every subtable under it. Other lines are kept."""
    path = tuple(path)
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        match = _HEADER.match(line)
        if match:
            found = _path_of(match.group(1))
            skip = found[:len(path)] == path
        if not skip:
            out.append(line)
    return ''.join(out)


def set_toml_value(text, path, key, value):
    """Set (or with value None, remove) one key in the table at path."""
    if value is None:
        trimmed = remove_toml_table(text, (*path, key))
        if trimmed != text:
            return trimmed
    lines = text.splitlines(keepends=True)
    if text and not text.endswith('\n'):
        lines[-1] += '\n'
    path = tuple(path)
    if path:
        start = None
        for index, line in enumerate(lines):
            match = _HEADER.match(line)
            if match and _path_of(match.group(1)) == path:
                start = index
                break
        if start is None:
            if value is None:
                return ''.join(lines)
            header = '.'.join(_quote(part) for part in path)
            return ''.join(lines) + f'\n[{header}]\n{_quote(key)} = {toml_value(value)}\n'
        first = start + 1
    else:
        first = 0
    end = len(lines)
    for index in range(first, len(lines)):
        if _HEADER.match(lines[index]):
            end = index
            break
    for index in range(first, end):
        if _key_of(lines[index]) == key:
            if value is None:
                del lines[index]
            else:
                lines[index] = f'{_quote(key)} = {toml_value(value)}\n'
            return ''.join(lines)
    if value is not None:
        lines.insert(first if path else end, f'{_quote(key)} = {toml_value(value)}\n')
    return ''.join(lines)


class ConfigEditor:
    def __init__(self, config_path, loader=None):
        self.config_path = Path(config_path)
        self.loader = loader

    def read(self):
        text = self.config_path.read_text(encoding='utf-8') if self.config_path.exists() else ''
        return tomllib.loads(text)

    def apply(self, edits, label='settings'):
        if not isinstance(edits, list) or not edits or len(edits) > 200:
            raise ValueError('edits must be a nonempty list')
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {'path', 'value'} or 'path' not in edit:
                raise ValueError('Each edit needs path and value')
            path = edit['path']
            if (not isinstance(path, list) or not path or len(path) > 8
                    or any(not isinstance(part, str) or not part or len(part) > 128 for part in path)):
                raise ValueError('path must be a list of key names')
        # Read, modify, validate and write inside one transaction. Two tabs saving
        # different keys must both land; before this, the later replace threw the
        # earlier edit away while both callers were told the save succeeded.
        with _transaction(self.config_path):
            return self._apply_locked(edits, label)

    def _apply_locked(self, edits, label):
        text = original = self.config_path.read_text(encoding='utf-8') if self.config_path.exists() else ''
        for edit in edits:
            path = edit['path']
            text = set_toml_value(text, path[:-1], path[-1], edit.get('value'))
        if text == original:
            return {'changed': False}
        self._validate(text)
        backup = self._copy_backup(label) if self.config_path.exists() else None
        self._replace(text)
        changes = [_change_row(edit['path'], edit.get('value')) for edit in edits]
        problem = self._remember(text, 'config_editor', label=label,
                                 backup=None if backup is None else backup.name,
                                 summary='Saved through ConfigEditor', changes=changes)
        reply = {'changed': True, 'backup': None if backup is None else backup.name,
                 'restart_required': True}
        if problem:
            reply['journal_error'] = problem
        return reply

    def backups(self):
        """ConfigEditor backup files beside config.toml, newest first.

        A backup that vanished between the directory scan and its stat is
        skipped: that file is gone, and the list is still true. Any other read
        error is raised, so the caller names it instead of the page showing a
        quietly shorter list.
        """
        directory = self.config_path.parent
        if not directory.is_dir():
            return []
        rows = []
        for path in directory.iterdir():
            if not self._is_backup(path):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            rows.append({
                'name': path.name,
                'bytes': stat.st_size,
                'modified': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
        rows.sort(key=lambda row: (row['modified'], row['name']), reverse=True)
        return rows

    def restore(self, name):
        """Replace config.toml with one ConfigEditor backup, after validating it.

        The file being replaced is itself backed up first, so a restore can be undone
        from the same list.
        """
        if not isinstance(name, str):
            raise ValueError('Unknown backup')
        with _transaction(self.config_path):
            source = self._backup_file(name)
            text = source.read_text(encoding='utf-8')
            self._validate(text)
            prior = self._copy_backup('restore') if self.config_path.is_file() else None
            self._replace(text)
            problem = self._remember(text, 'restore', label='restore',
                                     backup=None if prior is None else prior.name,
                                     summary='Restored ' + name,
                                     changes=[{'path': 'config.toml', 'value': 'restored from ' + name}])
        reply = {'changed': True, 'backup': None if prior is None else prior.name,
                 'restored': name, 'restart_required': True}
        if problem:
            reply['journal_error'] = problem
        return reply

    def changes(self):
        """Recorded config writes, oldest first, skipping the first observation."""
        return self.change_report()['entries']

    def change_report(self, keep=None):
        """The newest config changes, and how many records were on disk.

        ``keep`` is how many non-baseline records to return. The file is then
        cut to that many lines. ``total`` is the count before that cut, so the
        page can say it is showing N of M. ``error`` is set when the size could
        not be read or the file could not be cut; the caller shows it rather
        than presenting a shorter log as the whole log.
        """
        from .config import UiConfig
        problems = []
        try:
            keep = self._journal_keep(keep)
        except OSError as error:
            keep = UiConfig().config_journal_entries
            problems.append('the change log size could not be read: '
                            + type(error).__name__ + ': ' + str(error))
        with _transaction(self.config_path):
            rows, total, readable = self._journal_rows()
            shown = rows[-keep:]
            if readable:
                try:
                    self._trim_journal(keep)
                except OSError as error:
                    problems.append('the change log could not be cut to ' + str(keep)
                                    + ' records: ' + type(error).__name__ + ': ' + str(error))
        report = {'entries': shown, 'shown': len(shown), 'total': total, 'keep': keep}
        if problems:
            report['error'] = '; '.join(problems)
        return report

    def notice_drift(self):
        """Record a config.toml write that did not go through ConfigEditor.

        The first observation is a baseline, not a change. A later hash that this
        editor did not write is listed as outside ConfigEditor, with the keys that
        differ. Secret values are not copied into the record. A change-log cut
        failure is reported by the change report the settings view reads next,
        not by this return value.
        """
        with _transaction(self.config_path):
            if not self.config_path.is_file():
                return None
            text = self.config_path.read_text(encoding='utf-8')
            digest = _digest(text)
            previous = self._observed()
            if previous.get('sha256') == digest:
                return None
            try:
                tree = tomllib.loads(text)
            except tomllib.TOMLDecodeError as error:
                self._append_journal({
                    'at': _stamp(), 'source': 'outside_config_editor', 'sha256': digest,
                    'summary': 'config.toml changed outside ConfigEditor and could not be parsed: '
                               + type(error).__name__,
                    'changes': [],
                })
                self._write_observed({'sha256': digest, 'tree': {}})
                return 'outside_config_editor'
            redacted = _redact_tree(tree)
            if not previous:
                self._write_observed({'sha256': digest, 'tree': redacted})
                self._append_journal({
                    'at': _stamp(), 'source': 'baseline', 'sha256': digest,
                    'summary': 'Observed config.toml. Later writes outside ConfigEditor are listed here.',
                    'changes': [],
                })
                return 'baseline'
            changes = _diff_trees(previous.get('tree') or {}, redacted)
            self._write_observed({'sha256': digest, 'tree': redacted})
            self._append_journal({
                'at': _stamp(), 'source': 'outside_config_editor', 'sha256': digest,
                'summary': 'config.toml changed outside ConfigEditor',
                'changes': changes,
            })
            return 'outside_config_editor'

    def _validate(self, text):
        from .config import load_config
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch, 'config.toml').write_text(text, encoding='utf-8')
            try:
                tomllib.loads(text)
                (self.loader or load_config)(scratch)
            except (tomllib.TOMLDecodeError, ValueError, TypeError) as error:
                raise ValueError('Rejected: ' + str(error)[:240]) from None

    def _copy_backup(self, label):
        stamp = datetime.now().strftime('%Y%m%dT%H%M%S')
        base = f'config.before-{re.sub("[^a-z0-9]+", "-", label.lower())}-{stamp}'
        backup = self.config_path.with_name(base + '.toml')
        serial = 1
        while backup.exists():
            serial += 1
            backup = self.config_path.with_name(f'{base}-{serial}.toml')
        shutil.copy2(self.config_path, backup)
        return backup

    def _replace(self, text):
        temporary = self.config_path.with_name(self.config_path.name + '.tmp')
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, self.config_path)

    def _backup_file(self, name):
        if not _BACKUP_NAME.fullmatch(name) or '/' in name or '\\' in name or '..' in name:
            raise ValueError('Unknown backup')
        path = self.config_path.parent / name
        if not self._is_backup(path):
            raise ValueError('Unknown backup')
        return path

    def _is_backup(self, path):
        if not _BACKUP_NAME.fullmatch(path.name):
            return False
        if path.is_symlink() or not path.is_file():
            return False
        try:
            return path.resolve().parent == self.config_path.parent.resolve()
        except OSError:
            return False

    def _journal_path(self):
        return self.config_path.with_name('config-changes.jsonl')

    def _observed_path(self):
        return self.config_path.with_name('config-observed.json')

    def _observed(self):
        path = self._observed_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_observed(self, payload):
        path = self._observed_path()
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding='utf-8')
        os.replace(temporary, path)

    def _journal_keep(self, keep=None):
        """How many change records to keep: the argument, the file, or the default.

        A missing file and a file the parser rejects fall back to the built-in
        default, which is also what the loader holds when the key is absent. A
        file that exists but cannot be read raises, so a caller reports it
        instead of using a count the operator did not choose.
        """
        if type(keep) is int and keep >= 1:
            return keep
        from .config import UiConfig
        fallback = UiConfig().config_journal_entries
        try:
            text = self.config_path.read_text(encoding='utf-8')
            data = tomllib.loads(text)
        except (FileNotFoundError, tomllib.TOMLDecodeError):
            return fallback
        ui = data.get('ui') if isinstance(data, dict) else None
        value = ui.get('config_journal_entries') if isinstance(ui, dict) else None
        if type(value) is int and value >= 1:
            return value
        return fallback

    def _journal_rows(self):
        path = self._journal_path()
        if not path.is_file():
            return [], 0, False
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError as error:
            return [{'source': 'unreadable', 'summary': type(error).__name__ + ': ' + str(error)}], 1, False
        rows = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                rows.append({'source': 'unreadable', 'summary': 'A config change record could not be read'})
                continue
            if not isinstance(row, dict) or row.get('source') == 'baseline':
                continue
            rows.append({key: row[key] for key in ('at', 'source', 'label', 'backup', 'summary', 'changes')
                         if key in row})
        return rows, len(rows), True

    def _trim_journal(self, keep):
        """Cut the journal to its newest ``keep`` lines.

        A journal that is not there has nothing to cut. A journal that is there
        and cannot be read raises: returning quietly would leave the file
        growing with no record of why.
        """
        path = self._journal_path()
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except FileNotFoundError:
            return
        if len(lines) <= keep:
            return
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text('\n'.join(lines[-keep:]) + '\n', encoding='utf-8')
        os.replace(temporary, path)

    def _append_journal(self, row):
        """Append one record, then cut the file to size.

        Returns '' or the reason the cut failed. The record is on disk by then,
        so the caller reports the problem beside a save that did land instead of
        failing a save that succeeded.
        """
        path = self._journal_path()
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        try:
            self._trim_journal(self._journal_keep(None))
        except OSError as error:
            return ('the change log could not be cut to size: '
                    + type(error).__name__ + ': ' + str(error))
        return ''

    def _remember(self, text, source, *, label, backup, summary, changes):
        digest = _digest(text)
        try:
            tree = _redact_tree(tomllib.loads(text))
        except tomllib.TOMLDecodeError:
            tree = {}
        self._write_observed({'sha256': digest, 'tree': tree})
        return self._append_journal({
            'at': _stamp(), 'source': source, 'label': label, 'backup': backup,
            'sha256': digest, 'summary': summary, 'changes': changes,
        })


def _stamp():
    return datetime.now(timezone.utc).isoformat()


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _secret_key(key):
    return is_secret_name(key)


def _holds_secret(name, value):
    """True when this name holds a credential rather than a switch or a count.

    A credential is text, a mapping or a list. `allow_api_key` matches the name
    rule and holds a boolean policy switch, so its value stays readable.
    """
    if not _secret_key(name):
        return False
    return isinstance(value, (str, dict, list, tuple)) or value is None


def _redact_tree(value, key=''):
    if _holds_secret(key, value):
        if value in ('', None):
            return ''
        digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8')).hexdigest()[:16]
        return {'redacted': True, 'sha256': digest}
    if isinstance(value, dict):
        return {str(name): _redact_tree(child, str(name)) for name, child in value.items()}
    if isinstance(value, list):
        return [_redact_tree(item, key) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _shown_change(key, value):
    """One journal value with every nested secret replaced.

    A table edit is recorded as that table, so redaction has to walk dictionaries
    and lists. The key name stays; only the secret value becomes a marker.
    """
    if _holds_secret(key, value):
        return 'changed (not shown)'
    if isinstance(value, dict) and value.get('redacted') is True and set(value) <= {'redacted', 'sha256'}:
        return 'changed (not shown)'
    if isinstance(value, dict):
        return {str(name): _shown_change(str(name), child) for name, child in value.items()}
    if isinstance(value, list):
        return [_shown_change('', item) for item in value]
    return value


def _public_change_value(key, value):
    shown = _shown_change(key, value)
    if shown == 'changed (not shown)':
        return shown
    rendered = json.dumps(shown, ensure_ascii=False, default=str)
    if len(rendered) > 400:
        return rendered[:400] + '… (truncated)'
    return shown


def _change_row(path, value):
    key = path[-1] if path else ''
    if value is None:
        return {'path': '.'.join(path), 'value': 'removed'}
    return {'path': '.'.join(path), 'value': _public_change_value(key, value)}


def _diff_trees(before, after, prefix=''):
    changes = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in list(dict.fromkeys([*before.keys(), *after.keys()])):
            path = prefix + key
            left = before.get(key, _MISSING)
            right = after.get(key, _MISSING)
            if left == right:
                continue
            # A new table is a set of keys, not one blob, so a secret inside it is
            # still redacted per key instead of being copied through as a dump.
            if isinstance(right, dict) and right.get('redacted') is not True and (
                    left is _MISSING or (isinstance(left, dict) and left.get('redacted') is not True)):
                changes.extend(_diff_trees({} if left is _MISSING else left, right, path + '.'))
                continue
            if right is _MISSING:
                changes.append({'path': path, 'value': 'removed'})
            else:
                changes.append({'path': path, 'value': _public_change_value(key, right)})
        return changes
    if before != after:
        changes.append({'path': prefix.rstrip('.'), 'value': _public_change_value(prefix.rstrip('.').split('.')[-1], after)})
    return changes


_MISSING = object()
