"""An exclusively owned, append-only JSONL chat with synchronous subscribers."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from . import locking as fcntl
from .durability import sync_directory
import json
import os
from pathlib import Path
import threading
import uuid


@dataclass
class Message:
    i: int
    kind: str
    text: str
    size: int
    date: str
    meta: dict


class ChatLog:
    def __init__(self, root: Path, secret_values=()):
        self._secrets = []
        for value in secret_values:
            if value:
                self._secrets.append(value)
        self._secrets.sort(key=len, reverse=True)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()
        self._append_lock = threading.RLock()
        self._subscribers = []
        self._messages = []
        self._closed = False
        self._lock_file = (self.root / '.lock').open('a')
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise RuntimeError(f'Chat log is locked: {self.root}') from exc
        try:
            paths = sorted(self.root.glob('*.jsonl'))
            for path in paths:
                with path.open('r+b') as stream:
                    while True:
                        offset = stream.tell()
                        line = stream.readline()
                        if not line:
                            break
                        if not line.endswith(b'\n'):
                            if path != paths[-1]:
                                raise ValueError(f'Incomplete record before the end of chat: {path}')
                            self._preserve_tail(path, stream, offset, line)
                            break
                        try:
                            message = Message(**json.loads(line))
                        except (ValueError, TypeError):
                            raise ValueError(f'Invalid chat record in {path} at byte {offset}') from None
                        if message.i != len(self._messages):
                            raise ValueError(f'Noncontiguous chat log at {path}')
                        self._messages.append(message)
        except Exception:
            self.close()
            raise

    def _preserve_tail(self, path, stream, offset, fragment):
        # Only newline-terminated records are committed. Keep the incomplete
        # bytes as evidence before removing the uncommitted tail for append.
        recovery = path.with_name(path.name + '.incomplete-' + uuid.uuid4().hex)
        with recovery.open('xb') as saved:
            saved.write(fragment)
            saved.flush()
            os.fsync(saved.fileno())
        sync_directory(self.root)
        stream.truncate(offset)
        stream.flush()
        os.fsync(stream.fileno())

    def append(self, kind, text, meta=None):
        if kind not in ('user', 'agent', 'tool', 'echo', 'work', 'note'):
            raise ValueError(f'Unknown message kind: {kind}')
        text = self.redact(str(text))
        meta = self.redact(dict(meta or {}))
        with self._append_lock:
            with self._mutex:
                if self._closed:
                    raise RuntimeError('Chat log is closed')
                date = datetime.now(timezone.utc).isoformat()
                message = Message(len(self._messages), kind, str(text), len(str(text).encode('utf-8')), date, dict(meta or {}))
                path = self.root / (date[:10] + '.jsonl')
                # Index order survives a clock correction across midnight.
                paths = sorted(self.root.glob('*.jsonl'))
                if paths and paths[-1].name > path.name:
                    path = paths[-1]
                existed = path.exists()
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(asdict(message), ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                if not existed:
                    sync_directory(self.root)
                self._messages.append(message)
                callbacks = tuple(self._subscribers)
            for callback in callbacks:
                callback(message)
            return message

    def add_secrets(self, values):
        """Register newly resolved session credentials before related tool logging."""
        with self._mutex:
            secrets = list(self._secrets)
            for value in values:
                if isinstance(value, str) and value and value not in secrets:
                    secrets.append(value)
            self._secrets = sorted(secrets, key=len, reverse=True)

    def redact(self, value):
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, '[REDACTED]')
            return value
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                cleaned[self.redact(key)] = self.redact(item)
            return cleaned
        if isinstance(value, (list, tuple)):
            cleaned = []
            for item in value:
                cleaned.append(self.redact(item))
            return cleaned
        return value

    def get(self, i):
        with self._mutex:
            if i < 0:
                raise IndexError(i)
            return self._messages[i]

    def count(self):
        with self._mutex:
            return len(self._messages)

    def iter(self, start=0):
        with self._mutex:
            snapshot = self._messages[start:].copy()
        return iter(snapshot)

    def subscribe(self, callback):
        with self._mutex:
            self._subscribers.append(callback)

    def close(self):
        with self._append_lock, self._mutex:
            if not self._closed:
                self._closed = True
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def read_tail(directory, count):
    """Newest complete chat records, read from the end of the log files.

    This does not build a session or a summary tree. The first page can paint
    before that work starts.
    """
    if type(count) is not int or count < 1:
        raise ValueError('tail count must be a positive integer')
    root = Path(directory)
    if not root.is_dir():
        return []
    files = sorted(path for path in root.glob('*.jsonl') if path.is_file())
    collected = []
    for path in reversed(files):
        lines = _tail_lines(
            path, count - len(collected), tolerate_fragment=path == files[-1]
        )
        for line in reversed(lines):
            collected.append(json.loads(line.decode('utf-8')))
            if len(collected) >= count:
                break
        if len(collected) >= count:
            break
    collected.reverse()
    return collected


def _tail_lines(path, count, tolerate_fragment=False):
    if count < 1:
        return []
    found = []
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        # ChatLog.append writes the newline before it fsyncs, so only
        # newline-terminated records are committed. Only the newest file can be
        # mid-append; an interrupted append leaves an unterminated fragment
        # after its last newline, which ChatLog preserves as evidence the next
        # time it opens the log. Parsing that fragment here would raise and deny
        # the reader every older message in the session, so scan only up to the
        # last committed newline. Older files are closed by definition, and a
        # record there is read as it always was.
        if (
            tolerate_fragment
            and position
            and not _newline_at(stream, position - 1)
        ):
            position = _last_newline_end(stream, position)
        pending = b''
        while position > 0 and len(found) < count:
            step = min(65536, position)
            position -= step
            stream.seek(position)
            chunk = stream.read(step)
            pending = chunk + pending
            parts = pending.split(b'\n')
            pending = parts[0]
            complete = parts[1:]
            for part in reversed(complete):
                if not part:
                    continue
                found.append(part)
                if len(found) >= count:
                    break
        if pending and len(found) < count:
            found.append(pending)
    found.reverse()
    return found


def _newline_at(stream, offset):
    stream.seek(offset)
    return stream.read(1) == b'\n'


def _last_newline_end(stream, end):
    """Offset just past the last newline, or 0 when the file has none."""
    while end > 0:
        step = min(65536, end)
        stream.seek(end - step)
        index = stream.read(step).rfind(b'\n')
        if index >= 0:
            return end - step + index + 1
        end -= step
    return 0
