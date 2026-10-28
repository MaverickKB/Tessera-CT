"""Identities: who a card's owner is, in one namespace for people and sessions.

A person is an identity, and most installs have one person. The model allows more:
cards and tasking are shared by several people who coordinate only through projects
and cards. Authentication is a later layer; nothing here decides who may do what.

The board already models work and ownership, but ownership is a free string: it accepts
any name-shaped value, so a card owned by a typo wakes nobody. This module is the record
layer under that string. It is deliberately not the credential layer — authentication
comes later, and nothing here decides who may do what.

Two kinds share one namespace:

- ``person`` — recorded in an append-only journal beside the board, so a rename or a
  later permission change is evidence rather than a row that was overwritten.
- ``session`` — derived live from the session manager. A session is not copied into the
  journal: its meta.json already holds the serialized id, and duplicating it would give
  two places to disagree.

The journal is read through the same bounded, torn-tail-repairing reader the board uses,
so a partially written line is preserved as evidence and truncated rather than making
every identity unreadable.
"""

from contextlib import contextmanager
from . import locking as fcntl
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .channels import valid_name

KINDS = {'person', 'session'}
DISPLAY_LIMIT = 200
HANDLE_LIMIT = 63
SAFE_INTEGER = 2 ** 53 - 1


class IdentityError(ValueError):
    pass


def handle_for(name):
    """Fold a name a person types into a handle a board owner string can carry.

    A person's handle and a session's slug share one namespace, and an owner string
    also has to survive the board journal's own validation, so the handle uses the
    slug charset. The typed form is kept in ``display_name`` rather than discarded:
    'Example Person' becomes 'example-person' and still reads as 'Example Person' on the board.

    Refusing the typed form outright is the defect this replaces. In a fresh home an
    operator recorded as 'alex' produced a person, 'Alex' produced none, and the
    operator's full name produced none, and the caller in sessions.py swallowed the
    error, so every card owned by a typed name silently showed as unresolved.
    """
    if not isinstance(name, str):
        raise IdentityError('Invalid identity name')
    folded = re.sub(r'[^a-z0-9]+', '-', name.strip().lower()).strip('-')
    if not folded or len(folded) > HANDLE_LIMIT:
        raise IdentityError('Invalid identity name')
    return folded


def validate_identity(identity):
    try:
        if not isinstance(identity, dict):
            raise ValueError()
        name = identity['name']
        valid_name(name)
        if not isinstance(identity['id'], str) or len(identity['id']) != 32:
            raise ValueError()
        if any(char not in '0123456789abcdef' for char in identity['id']):
            raise ValueError()
        if identity['kind'] not in KINDS:
            raise ValueError()
        display = identity['display_name']
        if not isinstance(display, str) or not display.strip() or len(display) > DISPLAY_LIMIT:
            raise ValueError()
        if type(identity['version']) is not int or not 0 < identity['version'] <= SAFE_INTEGER:
            raise ValueError()
        if type(identity['created']) not in (int, float) or type(identity['updated']) not in (int, float):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise IdentityError('Invalid identity record') from None


def replay_identities(records):
    """Latest version of each identity, plus the highest sequence seen."""
    identities = {}
    sequence = 0
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('identity'), dict):
            raise IdentityError('Invalid identity journal record')
        identity = record['identity']
        validate_identity(identity)
        stored = record.get('sequence')
        if type(stored) is not int or stored < 0:
            raise IdentityError('Invalid identity journal sequence')
        sequence = max(sequence, stored)
        identities[identity['id']] = identity
    return identities, sequence


class IdentityStore:
    """Append-only person records. Sessions are resolved elsewhere and never stored here."""

    def __init__(self, path, *, enabled=False, clock=time.time):
        self.path = Path(path)
        self.enabled = enabled
        self.clock = clock
        self._lock = threading.RLock()

    def _require_enabled(self):
        if not self.enabled:
            raise IdentityError('Identity records are disabled')

    @contextmanager
    def _locked(self):
        self._require_enabled()
        self._lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.with_name(self.path.name + '.lock').open('a+b')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(stream, fcntl.LOCK_UN)
            finally:
                stream.close()
                self._lock.release()

    def _read_records(self, repair=False):
        if not self.path.exists():
            return []
        records = []
        with self.path.open('r+b' if repair else 'rb') as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    # A torn tail is evidence, not garbage: keep it beside the journal
                    # before truncating, exactly as the board journal does.
                    if repair:
                        backup = self.path.with_name(self.path.name + '.incomplete-' + uuid.uuid4().hex)
                        with backup.open('xb') as evidence:
                            evidence.write(line)
                            evidence.flush()
                            os.fsync(evidence.fileno())
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError()
                    records.append(record)
                except (ValueError, TypeError):
                    raise IdentityError('Invalid identity journal record') from None
        return records

    def _append(self, identity, sequence):
        validate_identity(identity)
        record = {'sequence': sequence + 1, 'identity': identity}
        existed = self.path.exists()
        with self.path.open('ab') as stream:
            stream.write((json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        if not existed:
            directory = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return deepcopy(identity)

    def all(self, repair=False):
        self._require_enabled()
        identities, _ = replay_identities(self._read_records(repair))
        return sorted(identities.values(), key=lambda item: (item['kind'], item['name']))

    def get(self, identity_id):
        self._require_enabled()
        identities, _ = replay_identities(self._read_records())
        return identities.get(identity_id)

    def by_name(self, name):
        self._require_enabled()
        identities, _ = replay_identities(self._read_records())
        for identity in identities.values():
            if identity['name'] == name:
                return identity
        return None

    def create(self, name, display_name=None, kind='person', *, identity_id=None):
        """Record a person. A name is unique within the person records.

        Refusing a duplicate name rather than overwriting is deliberate: two people
        answering to one name is exactly the ambiguity this layer exists to remove.

        ``name`` is folded to a handle, so a name typed the way a person types it is
        recorded instead of refused. The typed form is what the board shows.
        """
        self._require_enabled()
        if kind not in KINDS:
            raise IdentityError('Unknown identity kind')
        typed = name.strip() if isinstance(name, str) else name
        name = handle_for(name)
        display = (display_name or typed).strip() if isinstance(display_name or typed, str) else ''
        if not display or len(display) > DISPLAY_LIMIT:
            raise IdentityError('Invalid identity display name')
        chosen = uuid.uuid4().hex if identity_id is None else identity_id
        with self._locked():
            identities, sequence = replay_identities(self._read_records(repair=True))
            for existing in identities.values():
                if existing['name'] == name:
                    raise IdentityError('An identity already answers to that name')
            timestamp = self.clock()
            identity = {'id': chosen, 'name': name, 'display_name': display, 'kind': kind,
                        'created': timestamp, 'updated': timestamp, 'version': 1}
            return self._append(identity, sequence)

    def rename(self, identity_id, name=None, display_name=None):
        self._require_enabled()
        with self._locked():
            identities, sequence = replay_identities(self._read_records(repair=True))
            current = identities.get(identity_id)
            if current is None:
                raise IdentityError('Identity does not exist')
            updated = deepcopy(current)
            if name is not None:
                typed = name.strip() if isinstance(name, str) else name
                name = handle_for(name)
                for other in identities.values():
                    if other['id'] != identity_id and other['name'] == name:
                        raise IdentityError('An identity already answers to that name')
                updated['name'] = name
                # A rename keeps the typed form as the display unless one was given,
                # so renaming to 'Example Person' reads as 'Example Person', not 'example-person'.
                if display_name is None and isinstance(typed, str) and typed.strip():
                    updated['display_name'] = typed.strip()[:DISPLAY_LIMIT]
            if display_name is not None:
                display = display_name.strip()
                if not display or len(display) > DISPLAY_LIMIT:
                    raise IdentityError('Invalid identity display name')
                updated['display_name'] = display
            updated['updated'] = self.clock()
            updated['version'] = current['version'] + 1
            return self._append(updated, sequence)


class IdentityRegistry:
    """One namespace over person records and live sessions.

    ``resolve`` accepts a serialized id, a person name, or a session slug, so a card
    owner, a project manager and a message sender can all be checked against the same
    thing. A session is read from the manager rather than copied, so its identity cannot
    drift from its meta.json.
    """

    def __init__(self, store, sessions=None):
        self.store = store
        self.sessions = sessions

    @property
    def enabled(self):
        return self.store.enabled

    def _session_rows(self):
        if self.sessions is None:
            return []
        try:
            return self.sessions.list_sessions()
        except Exception:
            # A registry read must never be the reason a board or a page fails.
            return []

    def sessions_as_identities(self):
        rows = []
        for row in self._session_rows():
            identity_id = row.get('id')
            if not isinstance(identity_id, str) or not identity_id:
                continue
            rows.append({'id': identity_id, 'name': row['slug'], 'display_name': row.get('title') or row['slug'],
                         'kind': 'session', 'created': None, 'updated': None, 'version': None})
        return rows

    def people(self):
        if not self.enabled:
            return []
        return [item for item in self.store.all() if item['kind'] == 'person']

    def all(self):
        people = self.people()
        return sorted(people + self.sessions_as_identities(),
                      key=lambda item: (item['kind'], item['name']))

    def resolve(self, handle):
        """Return the identity a handle names, or None.

        A handle that names both a person and a session is ambiguous and resolves to
        neither: guessing which one a caller meant is the failure this layer removes.

        The handle is folded before matching, because ``ensure_operator`` folds when it
        CREATES a person record. Without the same fold here the module disagreed with
        itself: a config naming its operator 'Alex' recorded a person 'alex' and then
        refused to resolve 'Alex'. resolve('alex') answered, while resolve('Alex') and
        resolve('ALEX') answered nothing.

        This does NOT make 'Alex' a valid board owner. ``board._valid_owner`` enforces the
        slug charset before identity resolution is reached, so an owner string must be
        the folded handle; that gate is deliberate and pinned by a test.

        A serialized id is matched exactly and never folded: an id is an opaque handle,
        not a name, so folding one could collide with a different id.
        """
        if not isinstance(handle, str) or not handle:
            return None
        # Fold the typed name to the handle form so 'Alex', 'alex' and 'ALEX' name the same
        # person, but never fold a serialized id: an id must match itself and only itself.
        try:
            by_name = handle_for(handle)
        except IdentityError:
            by_name = ''
        matches = []
        if self.enabled:
            for person in self.store.all():
                if handle == person['id'] or by_name == person['name']:
                    matches.append(person)
        for session in self.sessions_as_identities():
            if handle == session['id'] or by_name == session['name']:
                matches.append(session)
        if len(matches) == 1:
            return matches[0]
        return None

    def describe(self, handle):
        """What a board row can show about an owner without inventing one.

        Returns ``{'owner_id': ..., 'owner_known': bool, 'owner_kind': ...}``. An unknown
        owner keeps its string: the card was written before this layer existed, and
        rewriting history to hide it would be worse than showing it as unresolved.
        """
        identity = self.resolve(handle)
        if identity is None:
            return {'owner_id': '', 'owner_known': False, 'owner_kind': ''}
        return {'owner_id': identity['id'], 'owner_known': True, 'owner_kind': identity['kind']}

    def ensure_operator(self, name, display_name=None):
        """Give the configured operator a person record once, and keep answering to it.

        Empty config means nobody is named, so nothing is created: a fresh install must
        not inherit someone else's identity.

        The configured name is folded to a handle before lookup and creation, so the
        idempotency holds whether the config says 'Alex' or 'alex'.
        """
        if not self.enabled or not isinstance(name, str) or not name.strip():
            return None
        typed = name.strip()
        handle = handle_for(typed)
        existing = self.store.by_name(handle)
        if existing is not None:
            return existing
        return self.store.create(handle, display_name or typed, 'person')

    def ensure_companion(self, name, display_name=None):
        """Give the companion a person record once, so its own projects resolve.

        Turning records on lists every project managed by the companion as owned by
        nobody. Those projects are the companion's own work, so the gap is not a typo or
        a stale handle: only the OPERATOR was ever recorded. Every install that flips
        records on would show its own companion as an unknown owner, which reads as a
        data problem and is really a missing record.

        Recording the companion as a person, not a session, is deliberate: the companion
        is one identity across many sessions, and a project manager should not stop
        resolving when the session that created it is gone.
        """
        if not self.enabled or not isinstance(name, str) or not name.strip():
            return None
        typed = name.strip()
        handle = handle_for(typed)
        existing = self.store.by_name(handle)
        if existing is not None:
            return existing
        return self.store.create(handle, display_name or typed, 'person')

    def status(self):
        """A small payload for a page or a coordinator: counts, not session history."""
        if not self.enabled:
            return {'enabled': False, 'configured': True, 'people': 0, 'sessions': 0,
                    'identities': [], 'unresolved_owners': []}
        return {'enabled': True, 'configured': True,
                'people': len(self.people()),
                'sessions': len(self.sessions_as_identities()),
                'identities': self.all()}


def unresolved_owners(registry, view):
    """Owner and manager strings on the board that resolve to nobody.

    This is the useful half of the identity surface. A card owned by a typo wakes
    nobody, and the string alone cannot say whether it is a typo or a person who has
    not been recorded yet. The list names both, so the reader can fix the record
    instead of hunting the board.

    Reads the ``owner_known`` / ``manager_known`` flags ``board_view`` already set,
    so there is one resolution path rather than two that can disagree.
    """
    if registry is None or not registry.enabled:
        return []
    found = []
    for row in view.get('cards') or []:
        owner = row.get('owner') or ''
        if owner and row.get('owner_known') is False:
            found.append({'kind': 'card', 'handle': owner, 'card_id': row.get('card_id') or '',
                          'ordinal': row.get('ordinal'), 'title': row.get('title') or '',
                          'status': row.get('status') or ''})
    for row in view.get('projects') or []:
        manager = row.get('manager') or ''
        if manager and row.get('manager_known') is False:
            found.append({'kind': 'project', 'handle': manager, 'project_id': row.get('id') or '',
                          'ordinal': None, 'title': row.get('title') or '',
                          'status': row.get('status') or ''})
    return found
