"""Per-message marks in one session: good, or a flag that can later be resolved."""

from datetime import datetime, timezone
import json
import os
import threading

RATINGS = ('good', 'flag')


class Feedback:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def all(self):
        with self._lock:
            return self._read()

    def mark(self, index, rating=..., resolved=None, note=None):
        """Set, change, or clear (rating=None) the mark on message index; returns all marks."""
        if rating is not ... and rating is not None and rating not in RATINGS:
            raise ValueError('rating must be good, flag, or null')
        if resolved is not None and not isinstance(resolved, bool):
            raise ValueError('resolved must be true or false')
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError('note must be text up to 2000 characters')
        with self._lock:
            marks = self._read()
            key = str(index)
            current = dict(marks.get(key, {}))
            if rating is None:
                marks.pop(key, None)
            else:
                if rating is not ...:
                    if rating != current.get('rating'):
                        current['resolved'] = False
                    current['rating'] = rating
                if 'rating' not in current:
                    raise ValueError('Message has no mark to update')
                if resolved is not None:
                    if current['rating'] != 'flag':
                        raise ValueError('Only a flag can be resolved')
                    current['resolved'] = resolved
                if note is not None:
                    current['note'] = note
                current.setdefault('resolved', False)
                current['updated'] = datetime.now(timezone.utc).isoformat()
                marks[key] = current
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.tmp')
            temporary.write_text(json.dumps(marks, sort_keys=True), encoding='utf-8')
            os.replace(temporary, self.path)
            return marks

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        return value if isinstance(value, dict) else {}
