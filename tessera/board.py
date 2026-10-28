"""Opt-in durable WIP cards. Artifact references are data, never file operations."""

from contextlib import contextmanager
from copy import deepcopy
from . import locking as fcntl
from .durability import sync_directory
import json
import math
import os
from pathlib import Path
import threading
import time
import uuid

from .channels import valid_name

# 'intake' is an artifact that arrived from outside (a pull request, a report) and is
# not work yet: someone must read it before it becomes claimable. It is deliberately
# absent from CLAIMABLE_STATES, so no worker can take it, and the autonomy wake logic
# keys on queued/blocked, so an intake card wakes nobody but the intake identity.
CARD_STATES = {'queued', 'running', 'blocked', 'succeeded', 'failed', 'cancelled', 'intake'}
CLAIMABLE_STATES = {'queued', 'blocked', 'failed'}
PROJECT_STATES = {'active', 'done', 'dropped'}
MAX_STEPS = 64
SAFE_INTEGER = 2 ** 53 - 1
# The card journal holds one full card snapshot per version, so it grows with every
# write. Readers refuse a journal larger than status.max_file_bytes, which means a
# board that outgrows its own log stops being readable — permanently, because the
# next write makes it bigger. Compact before it reaches the reader's cap.
COMPACT_AT = 0.75


class BoardError(ValueError):
    pass


_MISSING = object()


def _priority_value(value):
    """A card's priority is a stored integer. A title is not a priority."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoardError('Invalid board priority')
    if value < 0 or value > 100000:
        raise BoardError('Invalid board priority')
    return value


class BoardSizeError(BoardError):
    """A text field could not hold what was written. Distinct from a version conflict.

    Size is a different fact from a version conflict and gets its own identity,
    so the page can say the note is too long instead of asking for a refresh.
    """
    pass


def safe_integer(value):
    return type(value) is int and 0 <= value <= SAFE_INTEGER


def log_range(start, end):
    if not safe_integer(start) or not safe_integer(end) or start > end:
        raise BoardError('Invalid inclusive board log range')


def validate_card(card):
    try:
        if not isinstance(card, dict) or not isinstance(card['id'], str) or not card['id']:
            raise ValueError()
        valid_name(card['session_slug'])
        if not isinstance(card['title'], str) or not card['title'].strip():
            raise ValueError()
        if not isinstance(card['status'], str) or card['status'] not in CARD_STATES:
            raise ValueError()
        # Project filing arrived after the first journal version; older cards are unfiled.
        if 'project' in card and (not isinstance(card['project'], str) or len(card['project']) > 256):
            raise ValueError()
        # Owner, note and updated_by arrived after the first journal version; older cards omit them.
        if 'owner' in card:
            if not isinstance(card['owner'], str) or (card['owner'] and not _valid_owner(card['owner'])):
                raise ValueError()
        if 'note' in card and (not isinstance(card['note'], str) or len(card['note'].encode()) > 8192):
            raise ValueError()
        if 'updated_by' in card and not isinstance(card['updated_by'], str):
            raise ValueError()
        # A person can take a card back off the board. The journal is append-only, so the
        # card is never erased: it is flagged and filtered out of every reader. Without
        # this, a card written and then decided against could not be taken back.
        if 'removed' in card and type(card['removed']) is not bool:
            raise ValueError()
        # Intake arrived after the first journal version; older cards have none.
        if 'intake' in card:
            _validate_intake(card['intake'])
        if 'acceptances' in card and not isinstance(card['acceptances'], list):
            raise ValueError()
        if 'ignored' in card and not isinstance(card['ignored'], list):
            raise ValueError()
        if not safe_integer(card['attempt']) or not safe_integer(card['version']) or card['version'] == 0:
            raise ValueError()
        log_range(card['log_start'], card['log_end'])
        for field in ('created_at', 'updated_at'):
            value = card[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
        if not isinstance(card['artifacts'], list) or not isinstance(card['questions'], list):
            raise ValueError()
        validate_evidence(card.get('evidence', []))
        for reference in card['artifacts']:
            if not isinstance(reference, str) or not reference.strip():
                raise ValueError()
        for question in card['questions']:
            if (not isinstance(question, dict) or not isinstance(question['id'], str)
                    or not isinstance(question['text'], str) or not question['text'].strip()
                    or type(question['pending']) is not bool):
                raise ValueError()
            gate = question['gate']
            if not isinstance(gate, dict) or gate.get('source') not in ('jev', 'heuristic', 'deterministic', 'mixed'):
                raise ValueError()
            for field in ('ask_human', 'needs_authority'):
                value = gate[field]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError()
        # Execution came after the first board journal version; older cards omit it.
        execution = card.get('execution')
        if execution is not None:
            if not isinstance(execution, dict) or set(execution) != {'session', 'since', 'receipt'}:
                raise ValueError()
            if not execution['session'] or not _valid_owner(execution['session']):
                raise ValueError()
            if type(execution['since']) not in (int, float) or not math.isfinite(execution['since']) or execution['since'] < 0:
                raise ValueError()
            if not isinstance(execution['receipt'], str) or len(execution['receipt'].encode()) > 8192:
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise BoardError('Invalid board journal card') from None


def validate_project(project):
    try:
        if not isinstance(project, dict) or not isinstance(project['id'], str) or not project['id']:
            raise ValueError()
        valid_name(project['session_slug'])
        if not isinstance(project['title'], str) or not project['title'].strip():
            raise ValueError()
        for field in ('description', 'note'):
            if not isinstance(project[field], str) or len(project[field].encode()) > 8192:
                raise ValueError()
        if not isinstance(project['status'], str) or project['status'] not in PROJECT_STATES:
            raise ValueError()
        if project.get('kind', 'planning') not in ('planning', 'subject'):
            raise ValueError()
        if not isinstance(project['manager'], str) or (project['manager'] and not _valid_owner(project['manager'])):
            raise ValueError()
        if not safe_integer(project['version']) or project['version'] == 0:
            raise ValueError()
        for field in ('created_at', 'updated_at'):
            value = project[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
        if not isinstance(project['steps'], list) or len(project['steps']) > MAX_STEPS:
            raise ValueError()
        for step in project['steps']:
            if (not isinstance(step, dict) or not isinstance(step['id'], str) or not step['id']
                    or not isinstance(step['text'], str) or not step['text'].strip()
                    or type(step['done']) is not bool):
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise BoardError('Invalid board journal project') from None


def _valid_owner(value):
    try:
        valid_name(value)
    except ValueError:
        return False
    return True


# What an intake card records about the thing it gathered. These are the fields the
# acceptance decision reads, so they are named rather than free prose.
INTAKE_SUBJECTS = ('bug', 'fix', 'feature-addition', 'feature-expansion', 'other')
INTAKE_KEYS = {'source', 'reference', 'subject', 'compatibility', 'summary'}


def _validate_intake(intake):
    """Validate the gathered-artifact record a PR (or other artifact) arrives as."""
    if not isinstance(intake, dict):
        raise BoardError('Intake must be a record')
    unknown = set(intake) - INTAKE_KEYS
    if unknown:
        raise BoardError('Unknown intake fields: ' + ', '.join(sorted(unknown)))
    cleaned = {}
    for key in ('source', 'reference', 'summary'):
        value = intake.get(key, '')
        if not isinstance(value, str) or len(value.encode()) > 4096:
            raise BoardError(f'Invalid intake {key}')
        cleaned[key] = value.strip()
    if not cleaned['reference']:
        raise BoardError('An intake card needs a reference to the artifact it gathered')
    subject = intake.get('subject', '')
    if subject and subject not in INTAKE_SUBJECTS:
        raise BoardError('Intake subject must be one of: ' + ', '.join(INTAKE_SUBJECTS))
    cleaned['subject'] = subject
    compatibility = intake.get('compatibility', '')
    if not isinstance(compatibility, str):
        raise BoardError('Invalid intake compatibility')
    cleaned['compatibility'] = compatibility.strip()
    return cleaned


def validate_evidence(items):
    from .attachments import MAX_BYTES, MAX_PER_TURN, KINDS, NAME_LIMIT
    if not isinstance(items, list) or len(items) > MAX_PER_TURN:
        raise BoardError('Invalid card evidence')
    for item in items:
        if not isinstance(item, dict) or set(item) != {'session_slug', 'id', 'name', 'media_type', 'size'}:
            raise BoardError('Invalid card evidence')
        valid_name(item['session_slug'])
        file_id = item['id']
        if not isinstance(file_id, str) or len(file_id) != 32 or any(c not in '0123456789abcdef' for c in file_id):
            raise BoardError('Invalid evidence attachment')
        if not isinstance(item['name'], str) or not item['name'] or len(item['name']) > NAME_LIMIT:
            raise BoardError('Invalid evidence name')
        if not isinstance(item['media_type'], str) or item['media_type'] not in KINDS or not safe_integer(item['size']) or item['size'] > MAX_BYTES:
            raise BoardError('Invalid evidence media or size')


def replay_cards(records):
    """Pure validation/replay also used by the passive status reader."""
    cards = {}
    sequence = 0
    for record in records:
        sequence += 1
        if type(record.get('sequence')) is not int or record['sequence'] != sequence:
            raise BoardError('Invalid board journal sequence at record %d' % sequence)
        card = record.get('card')
        try:
            validate_card(card)
        except BoardError as error:
            raise BoardError('Invalid board journal record %d: %s' % (sequence, error)) from None
        previous = cards.get(card['id'])
        expected = previous['version'] + 1 if previous else 1
        if card['version'] != expected:
            raise BoardError('Invalid board journal version at record %d' % sequence)
        # The creating session is fixed; the title and note are editable, so a card's
        # name can be corrected after it is written. Ownership moves with a claim.
        if previous and card['session_slug'] != previous['session_slug']:
            raise BoardError('Invalid board journal ownership at record %d' % sequence)
        card = dict(card)
        card.setdefault('owner', card['session_slug'])
        card.setdefault('note', '')
        card.setdefault('updated_by', card['session_slug'])
        card.setdefault('project', '')
        card.setdefault('removed', False)
        card.setdefault('execution', None)
        cards[card['id']] = card
    return cards, sequence


def replay_projects(records):
    """Projects live in their own journal.

    A reader that predates projects reads the card journal unchanged and simply has no
    projects. Sharing one file would have made an older core refuse the whole board and
    show nothing, which is a worse failure than a missing project list.
    """
    projects = {}
    sequence = 0
    for record in records:
        sequence += 1
        if type(record.get('sequence')) is not int or record['sequence'] != sequence:
            raise BoardError('Invalid board project journal sequence')
        project = record.get('project')
        validate_project(project)
        previous = projects.get(project['id'])
        expected = previous['version'] + 1 if previous else 1
        if project['version'] != expected:
            raise BoardError('Invalid board project journal version')
        if previous and project['session_slug'] != previous['session_slug']:
            raise BoardError('Invalid board project journal ownership')
        projects[project['id']] = dict(project)
        projects[project['id']].setdefault('kind', 'planning')
    return projects, sequence


class BoardStore:
    def __init__(self, path, *, enabled=False, max_text_bytes=8192, max_artifacts=32, max_questions=32,
                 max_journal_bytes=None, clock=time.time):
        for limit in (max_text_bytes, max_artifacts, max_questions):
            if type(limit) is not int or limit <= 0:
                raise BoardError('Board limits must be positive integers')
        if max_journal_bytes is not None and (type(max_journal_bytes) is not int or max_journal_bytes <= 0):
            raise BoardError('Board journal limit must be a positive integer')
        # Set by the session manager when identity records are configured. None means the
        # board knows nothing about identities and every view keeps its exact old shape.
        self.identities = None
        self.path = Path(path)
        # Projects are their own journal beside the cards. Older cores read the card
        # journal and ignore this file, so a project write never blanks the board.
        self.project_path = self.path.with_name(self.path.stem + '.projects' + self.path.suffix)
        self.enabled = enabled
        self.max_text_bytes = max_text_bytes
        self.max_artifacts = max_artifacts
        self.max_questions = max_questions
        # Acceptance gate. Defaults match an install that has not opted in, so
        # constructing a Board directly leaves acceptance off.
        self.acceptance_required = False
        self.acceptance_by = ()
        # The reader's cap, so compaction keeps the journal readable rather than merely
        # smaller. None means no compaction: the caller did not say what the reader allows.
        self.max_journal_bytes = max_journal_bytes
        self.clock = clock
        self._lock = threading.RLock()

    def _known_owner(self, value):
        """Refuse an owner that names nobody, when identity records are configured.

        With identity records off this is a no-op and ownership stays a free string,
        exactly as it was before this layer: an install that has not opted in keeps
        its existing cards and callers working. With them on, an owner must resolve to
        a person record or a live session, which closes the failure the previous
        behavior pinned knowingly — a card owned by a typo wakes nobody, and the
        typo is indistinguishable from a real name.

        An empty owner is not a name: it means the card is unclaimed, which is a
        deliberate state, so it is allowed through.
        """
        if not value:
            return value
        if self.identities is None or not self.identities.enabled:
            return value
        if self.identities.resolve(value) is None:
            raise BoardError('No identity answers to that owner; name a person or a session')
        return value

    def _require_enabled(self):
        if not self.enabled:
            raise BoardError('WIP board is disabled')

    def _text(self, value):
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > self.max_text_bytes:
            raise BoardSizeError('Board text is empty or exceeds configured byte limit')
        return value

    def _note_room(self, text):
        """Refuse only a line that cannot fit on its own; say why and by how much."""
        if not isinstance(text, str) or not text.strip():
            raise BoardSizeError('Board note text is empty')
        size = len(text.encode())
        if size > self.max_text_bytes:
            raise BoardSizeError('Board note text is %d bytes; the limit is %d bytes'
                                 % (size, self.max_text_bytes))
        return size

    def _appended_note(self, previous, text):
        """Previous note plus this line, trimmed oldest-first when the field is full.

        A note is one bounded field, and a card that lives long enough fills it. Appending
        must still work: the line the caller just wrote is the newest evidence, so it is
        kept whole and the oldest history goes, with a marker saying so. Only a single
        line larger than the whole field is refused.
        """
        size = self._note_room(text)
        text = text.strip()
        if not previous:
            return text
        joined = previous + '\n' + text
        if len(joined.encode()) <= self.max_text_bytes:
            return joined
        marker = '[earlier note history trimmed]'
        budget = self.max_text_bytes - size - 1 - len(marker) - 1
        if budget < 0:
            raise BoardSizeError('Board note text leaves no room for prior history; the limit is %d bytes'
                                 % self.max_text_bytes)
        kept = previous.encode()[-budget:].decode('utf-8', 'ignore')
        # Start at a line boundary so the kept history is not half a sentence.
        newline = kept.find('\n')
        if newline != -1:
            kept = kept[newline + 1:]
        return marker + '\n' + kept + '\n' + text

    def _sync_directory(self):
        sync_directory(self.path.parent)

    @contextmanager
    def _locked(self):
        self._require_enabled()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.with_name(self.path.name + '.lock').open('a+b') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _load_cards(self, repair=False):
        cards, sequence = self._read_journal(self.path, repair)
        return cards, sequence

    def _compact_if_needed(self, path, rewrite, projected):
        """Compact one journal after a write, never before one.

        Compacting before a write would reset a record's version fence between the moment
        the caller read it and the moment it is checked, so a caller that just wrote a
        record could be told it changed when nobody else touched it. Compacting after the
        write keeps the caller's own fence honest; the next caller reads the new one.

        Both journals are read through the same bounded reader (status.max_file_bytes), so
        both need this treatment: a journal that outgrows the cap stops being readable
        permanently, because the next write makes it bigger.

        `projected` returns the exact bytes a rewrite would produce. Compaction is skipped
        when it would reclaim nothing, and skipped when the compacted form itself cannot
        fit the reader's cap: in that state a rewrite reproduces bytes that are still over
        the cap, so it leaves the board just as blind while rewriting the whole file on
        every subsequent write. `_require_room` is what refuses those writes.
        """
        if not self.max_journal_bytes:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size <= int(self.max_journal_bytes * COMPACT_AT):
            return False
        records, _ = self._read_journal(path, repair=True)
        projected_size = projected(records)
        if projected_size >= size or projected_size > self.max_journal_bytes:
            return False
        rewrite(records)
        return True

    def _require_room(self, path, item, key, validate):
        """Refuse a write the reader's cap cannot hold even after compaction.

        Compaction keeps one record per item, so the compacted form is the smallest the
        journal can be. Once that form is over status.max_file_bytes the bounded reader
        returns nothing and every category on the page goes blank — and compaction cannot
        recover it, because the next write makes it bigger again. Refusing the write is
        the only outcome that tells a person the cap is too small instead of going quiet.

        The check is cheap in the common case: if the journal plus this one record still
        fits the cap, the append leaves the board readable and there is nothing to refuse,
        so it returns on a single stat and never reads the journal.

        This path must not change the journal: it exists to refuse a write, and a caller
        told "this did not fit" has no reason to expect the file to have moved. So the
        read here is repair=False. Repairing a torn tail is the allow path's job, where a
        record is about to be appended and an unhealed torn tail would glue the new record
        onto a broken line and make the journal unreadable for good.
        """
        if not self.max_journal_bytes:
            return
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        except OSError:
            return
        record_bytes = len((json.dumps({key: item}, ensure_ascii=False, allow_nan=False)
                            + '\n').encode())
        if size + record_bytes <= self.max_journal_bytes:
            return
        items, _ = self._read_journal(path, repair=False)
        merged = dict(items)
        merged[item['id']] = item
        projected_size = len(self._compacted_body(list(merged.values()), key, validate))
        if projected_size > self.max_journal_bytes:
            raise BoardSizeError(
                'Board %s journal cannot hold %d items within the reader cap: compacted '
                'they need %d bytes and the cap is %d bytes'
                % (key, len(merged), projected_size, self.max_journal_bytes))

    def _compacted_body(self, ordered, key, validate):
        """The exact bytes a compaction would write, without writing them.

        Compaction restarts every version fence at 1 and keeps one record per item, so
        this is the smallest the journal can be. Used both to decide whether a rewrite is
        worth doing and to refuse a write the reader's cap cannot hold.
        """
        records = []
        for ordinal, item in enumerate(ordered, 1):
            compacted = dict(deepcopy(item))
            compacted['version'] = 1
            validate(compacted)
            records.append({'sequence': ordinal, key: compacted})
        body = ''.join(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
                       for record in records)
        return body.encode()

    def _rewrite_journal(self, path, ordered, key, validate):
        """Rewrite one journal as one record per item, keeping first-appearance order.

        Replay demands strictly sequential record numbers and per-item versions starting
        at 1, so compaction renumbers both. Every item keeps its content, status and
        fields; only the version fence restarts. A page holding an older expected_version
        gets the ordinary "this changed, refresh" answer, and the board revision digest
        changes, so open pages redraw.

        The previous journal is kept beside the file as evidence. The rename is atomic, so
        a reader sees either the old journal or the compacted one.
        """
        body = self._compacted_body(ordered, key, validate)
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + '.pre-compact')
        if path.exists():
            backup.write_bytes(path.read_bytes())
        temporary = path.with_name(path.name + '.compacting-' + uuid.uuid4().hex)
        with temporary.open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self._sync_directory()

    def _rewrite_cards(self, cards):
        self._rewrite_journal(self.path, list(cards.values()), 'card', validate_card)

    def _rewrite_projects(self, projects):
        self._rewrite_journal(self.project_path, list(projects.values()), 'project', validate_project)

    def _project_cards(self, cards):
        return len(self._compacted_body(list(cards.values()), 'card', validate_card))

    def _project_projects(self, projects):
        return len(self._compacted_body(list(projects.values()), 'project', validate_project))

    def compact(self):
        """Rewrite both journals now, without waiting for a mutation.

        Returns the size before and after for each journal. Call it to repair a board that is
        already over the reader's cap: a running core reads the journals from disk on every
        request, so a compaction takes effect without a reload.
        """
        self._require_enabled()
        with self._locked():
            sizes = {}
            for name, path, rewrite in (
                    ('cards', self.path, self._rewrite_cards),
                    ('projects', self.project_path, self._rewrite_projects)):
                if not path.exists():
                    sizes[name] = None
                    continue
                items, _ = self._read_journal(path, repair=True)
                before = path.stat().st_size
                rewrite(items)
                sizes[name] = {'before': before, 'after': path.stat().st_size}
            return sizes

    def _load_projects(self, repair=False):
        projects, sequence = self._read_journal(self.project_path, repair)
        return projects, sequence

    def _read_records(self, path, repair=False):
        if not path.exists():
            return []
        records = []
        with path.open('r+b' if repair else 'rb') as stream:
            while True:
                offset = stream.tell()
                line = stream.readline()
                if not line:
                    break
                if not line.endswith(b'\n'):
                    if repair:
                        backup = path.with_name(path.name + '.incomplete-' + uuid.uuid4().hex)
                        with backup.open('xb') as evidence:
                            evidence.write(line)
                            evidence.flush()
                            os.fsync(evidence.fileno())
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                        self._sync_directory()
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError()
                    records.append(record)
                except (ValueError, TypeError):
                    raise BoardError('Invalid board journal record') from None
        return records

    def _read_journal(self, path, repair=False):
        records = self._read_records(path, repair)
        if path is self.project_path:
            return replay_projects(records)
        return replay_cards(records)

    @property
    def canvas_path(self):
        return self.path.with_name(self.path.stem + '.canvas' + self.path.suffix)

    def _load_canvas(self, project_id, repair=False):
        from .project_canvas import replay_canvas
        return replay_canvas(self._read_records(self.canvas_path, repair), project_id)

    def _append_card(self, card, sequence):
        validate_card(card)
        # Refuse before appending: an append makes the journal bigger, and once the
        # compacted form cannot fit the reader's cap there is no rewrite that recovers it.
        self._require_room(self.path, card, 'card', validate_card)
        written = self._append_record(self.path, {'card': card}, sequence)
        # Compaction runs after the append so the version the caller just wrote is the one
        # it gets back, and a page fenced to that version can still read its card. When the
        # append triggered a compaction the version fence restarts at 1, so the caller gets
        # the card as the journal now holds it rather than a version nothing can be written
        # against.
        if self._compact_if_needed(self.path, self._rewrite_cards, self._project_cards):
            return deepcopy(self._read_journal(self.path)[0].get(card['id'], written))
        return written

    def _append_project(self, project, sequence):
        validate_project(project)
        self._require_room(self.project_path, project, 'project', validate_project)
        written = self._append_record(self.project_path, {'project': project}, sequence)
        # Same reasoning as the card journal: the project journal is read through the same
        # bounded reader, so it needs the same cap. Compacting after the write keeps the
        # fence the caller just received honest.
        if self._compact_if_needed(self.project_path, self._rewrite_projects, self._project_projects):
            return deepcopy(self._read_journal(self.project_path)[0].get(project['id'], written))
        return written

    def _append_record(self, path, payload, sequence):
        existed = path.exists()
        record = {'sequence': sequence + 1, **payload}
        with path.open('ab') as stream:
            stream.write((json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        if not existed:
            self._sync_directory()
        return deepcopy(payload.get('card') or payload.get('project'))

    def create(self, title, session_slug, log_start, log_end, *, attempt=0, card_id=None, note='', owner=None,
               project='', evidence=None, intake=None, priority=None):
        self._require_enabled()
        title = self._text(title)
        note = '' if note is None else note
        if not isinstance(note, str) or len(note.encode()) > self.max_text_bytes:
            raise BoardError('Board note is empty or exceeds configured byte limit')
        if not isinstance(project, str) or len(project) > 256:
            raise BoardError('Invalid board project')
        valid_name(session_slug)
        # A card starts claimed by its author unless the caller leaves it open for others.
        owner = session_slug if owner is None else owner
        if owner and not _valid_owner(owner):
            raise BoardError('Invalid board owner')
        self._known_owner(owner)
        log_range(log_start, log_end)
        if not safe_integer(attempt):
            raise BoardError('Invalid board attempt')
        card_id = uuid.uuid4().hex if card_id is None else card_id
        if not isinstance(card_id, str) or not card_id or len(card_id) > 256:
            raise BoardError('Invalid board card id')
        request = {'title': title, 'session_slug': session_slug, 'log_start': log_start, 'log_end': log_end,
                   'attempt': attempt, 'note': note, 'owner': owner, 'project': project}
        if evidence is not None:
            validate_evidence(evidence)
            if len(evidence) > self.max_artifacts:
                raise BoardError('Card evidence exceeds the artifact limit')
            if evidence:
                request['evidence'] = deepcopy(evidence)
        # An intake card is an artifact that arrived from outside -- a pull request, a
        # report -- and is not work yet. It starts in 'intake' instead of 'queued' so no
        # worker can claim it and no worker wake offers it: someone has to read it first.
        if intake is not None:
            request['intake'] = _validate_intake(intake)
        if priority is not None:
            request['priority'] = _priority_value(priority)
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            if card_id in cards:
                if cards[card_id]['creation'] != request:
                    raise BoardError('Board card id was used for another request')
                return deepcopy(cards[card_id])
            stamp = self.clock()
            card = {'id': card_id, **request, 'creation': request,
                    'status': 'intake' if intake is not None else 'queued', 'version': 1,
                    'artifacts': [], 'questions': [], 'created_at': stamp, 'updated_at': stamp,
                    # Present from the first write so a card read back through the journal
                    # has the same shape as the card the create call returned.
                    'updated_by': session_slug, 'removed': False, 'execution': None}
            return self._append_card(card, sequence)

    def list(self):
        if not self.enabled:
            return []
        cards, _ = self._load_cards()
        # A removed card is kept in the journal as evidence but is not work any more.
        return deepcopy([card for card in cards.values() if not card.get('removed')])

    def get(self, card_id):
        self._require_enabled()
        cards, _ = self._load_cards()
        if card_id not in cards:
            raise BoardError('Board card does not exist')
        return deepcopy(cards[card_id])

    def _owned(self, cards, card_id, actor, expected_version):
        if not isinstance(card_id, str) or card_id not in cards:
            raise BoardError('Board card does not exist')
        card = cards[card_id]
        # The board is shared. A claimed card is the claimant's; an unclaimed card is
        # anyone's, including the operator. actor=None is the operator.
        if actor is not None and card.get('owner') and card['owner'] != actor:
            raise BoardError('Board card is claimed by another session')
        if expected_version is not None:
            if not safe_integer(expected_version) or card['version'] != expected_version:
                raise BoardError('Board card version is stale')
        return card

    def _finish(self, card, sequence, log_start, log_end):
        if log_start is not None or log_end is not None:
            log_range(log_start, log_end)
            card.update(log_start=log_start, log_end=log_end)
        card.update(version=card['version'] + 1, updated_at=self.clock())
        return self._append_card(card, sequence)

    def update_status(self, card_id, status, *, attempt=None, actor=None, expected_version=None, log_start=None, log_end=None):
        if not isinstance(status, str) or status not in CARD_STATES:
            raise BoardError('Unknown board status')
        if attempt is not None and not safe_integer(attempt):
            raise BoardError('Invalid board attempt')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            self._require_acceptance(card, status)
            if attempt is not None:
                if attempt < card['attempt']:
                    raise BoardError('Board attempt cannot decrease')
                card['attempt'] = attempt
            card['status'] = status
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, log_start, log_end)

    def _require_acceptance(self, card, status):
        """Refuse the move into succeeded unless an identity accepted the work.

        A card is not accepted as work until a reviewer or manager clears it. The gate
        is on the TRANSITION, not on the card, and it
        is off by default so an existing install does not suddenly require a sign-off it
        has no one to perform.

        Acceptance is a recorded event, not a status field: a card accepted and then
        reopened keeps that acceptance in its history, because the journal is append-only
        and the acceptance lives on the card snapshot as a list.
        """
        if status != 'succeeded' or not self.acceptance_required:
            return
        acceptances = card.get('acceptances') or []
        if not acceptances:
            raise BoardError('This card needs acceptance before it can be succeeded; '
                             'record who accepted it and against what evidence')
        # An acceptance covers the version it was given against, not the card forever.
        # Without this, a card reopened BECAUSE the work was wrong could be re-closed by the
        # acceptance of the wrong work: accept at v1, succeed, reopen (v3), succeed again with
        # no new acceptance. The latest acceptance must cover the current version.
        latest = acceptances[-1]
        accepted_version = latest.get('version')
        if type(accepted_version) is not int or accepted_version < card['version']:
            raise BoardError('The acceptance on this card predates its current version; '
                             'accept the work as it stands before succeeding it')
        allowed = self.acceptance_by or ()
        if allowed and latest.get('by') not in allowed:
            raise BoardError('The accepting identity is not one this board accepts from')

    def accept(self, card_id, by, *, evidence='', note='', expected_version=None):
        """Record that an identity accepts this card as work.

        ``by`` must resolve to a known identity when identity records are on, so an
        acceptance cannot be signed by a name nobody holds. The record carries who, when,
        the evidence offered and an optional reason, and it is appended -- never replaced.
        """
        valid_name(by)
        if not isinstance(evidence, str) or not isinstance(note, str):
            raise BoardError('Invalid acceptance text')
        if len(evidence.encode()) > self.max_text_bytes or len(note.encode()) > self.max_text_bytes:
            raise BoardError('Acceptance text is too long')
        if self.identities is not None and self.identities.enabled and self.identities.resolve(by) is None:
            raise BoardError('No identity answers to that acceptor')
        if self.acceptance_by and by not in self.acceptance_by:
            raise BoardError('This board does not accept from that identity')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, None, expected_version)
            # The version this acceptance covers: the snapshot that CARRIES it, which is the
            # version this write produces. _finish bumps the version after this, so record
            # card['version'] + 1. Recording the pre-write version would make every accept
            # immediately stale against the card it was written on.
            record = {'by': by, 'at': self.clock(), 'evidence': evidence.strip(), 'note': note.strip(),
                      'version': card['version'] + 1}
            card.setdefault('acceptances', []).append(record)
            card['updated_by'] = by
            return self._finish(card, sequence, None, None)

    def triage_intake(self, card_id, *, subject=None, compatibility=None, summary=None, expected_version=None):
        """Record what the intake reviewer found, on the card, as evidence.

        The intake desk reviews the change and notes the compatibility and the subject
        matter (bug, fix, feature addition, feature expansion, and so on). These are not
        prose for its own sake: they are what the acceptance decision reads, so they are
        typed fields on the card rather than a note nobody parses.
        """
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, None, expected_version)
            intake = card.get('intake')
            if intake is None:
                raise BoardError('This card was not gathered for intake')
            if card['status'] != 'intake':
                raise BoardError('This card has already been triaged out of intake')
            if subject is not None:
                if subject not in INTAKE_SUBJECTS:
                    raise BoardError('Intake subject must be one of: ' + ', '.join(INTAKE_SUBJECTS))
                intake['subject'] = subject
            if compatibility is not None:
                if not isinstance(compatibility, str) or len(compatibility.encode()) > 4096:
                    raise BoardError('Invalid intake compatibility')
                intake['compatibility'] = compatibility.strip()
            if summary is not None:
                if not isinstance(summary, str) or len(summary.encode()) > 4096:
                    raise BoardError('Invalid intake summary')
                intake['summary'] = summary.strip()
            card['updated_by'] = card.get('updated_by', '')
            return self._finish(card, sequence, None, None)

    def accept_intake(self, card_id, by, *, note='', expected_version=None):
        """Accept a gathered artifact as work: intake -> queued, claimable, with history.

        The acceptance gate is this board's one gate, aimed at intake. Accepting
        records who accepted and why on the card, then makes it real work.
        """
        valid_name(by)
        if not isinstance(note, str) or len(note.encode()) > self.max_text_bytes:
            raise BoardError('Invalid acceptance note')
        if self.identities is not None and self.identities.enabled and self.identities.resolve(by) is None:
            raise BoardError('No identity answers to that acceptor')
        if self.acceptance_by and by not in self.acceptance_by:
            raise BoardError('This board does not accept from that identity')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, None, expected_version)
            if card['status'] != 'intake':
                raise BoardError('Only a card in intake can be accepted as work')
            intake = card.get('intake') or {}
            if not intake.get('subject'):
                raise BoardError('Triage the artifact before accepting it as work: no subject recorded')
            card.setdefault('acceptances', []).append({
                'by': by, 'at': self.clock(), 'evidence': intake.get('reference', ''),
                'note': note.strip(), 'kind': 'intake', 'version': card['version'] + 1})
            card['status'] = 'queued'
            card['owner'] = ''
            card['updated_by'] = by
            return self._finish(card, sequence, None, None)

    def ignore_intake(self, card_id, by, *, reason, expected_version=None):
        """Decline a gathered artifact as work, keeping it as evidence with the reason.

        An intake has two outcomes: accepted, or ignored. Ignored is not deleted. The
        journal is append-only and the card stays readable with the reason recorded, so a
        later reader can see what was turned away and why.
        """
        valid_name(by)
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > self.max_text_bytes:
            raise BoardError('Ignoring an intake card needs a reason')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, None, expected_version)
            if card['status'] != 'intake':
                raise BoardError('Only a card in intake can be ignored')
            card.setdefault('ignored', []).append({
                'by': by, 'at': self.clock(), 'reason': reason.strip()})
            card['status'] = 'cancelled'
            card['updated_by'] = by
            return self._finish(card, sequence, None, None)

    def start_card(self, card_id, session, receipt='', *, expected_version=None):
        """Mark a claimed card as actively executed by its owner.

        Starting is the transition claim (assigned) to running (actually being
        worked). It records the executing session, the start clock and an optional
        receipt so the cockpit can show who is on a card and what they said they
        were doing. Moving the card off running later keeps this record as the
        latest execution evidence.
        """
        self._require_enabled()
        valid_name(session)
        if not isinstance(receipt, str) or len(receipt.encode()) > self.max_text_bytes:
            raise BoardError('Invalid board start receipt')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, None, expected_version)
            if not card.get('owner'):
                raise BoardError('Board card is unclaimed; claim it before starting work')
            if card['owner'] != session:
                raise BoardError('Board card is claimed by another session')
            card['status'] = 'running'
            card['execution'] = {'session': session, 'since': self.clock(),
                                 'receipt': receipt.strip() or ''}
            card['updated_by'] = session
            return self._finish(card, sequence, None, None)

    def remove(self, card_id, *, actor=None, expected_version=None):
        """Take a card off the board without erasing the record of it.

        The journal is append-only, so removal is a tombstone: the card stays as evidence
        with removed=True and is filtered out of every reader. A card written and then
        decided against has to be able to come back off the board.
        """
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            if card.get('removed'):
                raise BoardError('Board card is already removed')
            card['removed'] = True
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, None, None)

    def file_card(self, card_id, project, *, actor=None, expected_version=None):
        """File a card under a project, or unfiled with project=''.

        Filing is not ownership: a card can be filed by its claimant or by the operator,
        and a project manager files work orders without doing the work.
        """
        if not isinstance(project, str) or len(project) > 256:
            raise BoardError('Invalid board project')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            projects, _ = self._load_projects(repair=True)
            # Filing is shared organization. It does not claim or execute the work.
            card = self._owned(cards, card_id, None, expected_version)
            if project and project not in projects:
                raise BoardError('Board project does not exist')
            card['project'] = project
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, None, None)

    # --- Projects -------------------------------------------------------------
    # Shared workspaces group cards. Planning projects carry intent and ordered steps;
    # subjects collect related issues and healing work with an optional approach.

    def create_project(self, title, session_slug, *, description='', manager='', project_id=None, note='', kind='planning'):
        self._require_enabled()
        title = self._text(title)
        for field in (description, note):
            if not isinstance(field, str) or len(field.encode()) > self.max_text_bytes:
                raise BoardError('Board project text is empty or exceeds configured byte limit')
        valid_name(session_slug)
        manager = manager if manager is not None else ''
        if manager and not _valid_owner(manager):
            raise BoardError('Invalid project manager')
        project_id = uuid.uuid4().hex if project_id is None else project_id
        if not isinstance(project_id, str) or not project_id or len(project_id) > 256:
            raise BoardError('Invalid board project id')
        request = {'title': title, 'session_slug': session_slug, 'description': description,
                   'manager': manager, 'note': note}
        if kind not in ('planning', 'subject'):
            raise BoardError('Unknown workspace kind')
        if kind == 'subject':
            request['kind'] = kind
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            if project_id in projects:
                if projects[project_id]['creation'] != request:
                    raise BoardError('Board project id was used for another request')
                return deepcopy(projects[project_id])
            stamp = self.clock()
            project = {'id': project_id, **request, 'creation': request, 'status': 'active', 'version': 1,
                       'steps': [], 'created_at': stamp, 'updated_at': stamp, 'kind': kind}
            return self._append_project(project, sequence)

    def list_projects(self):
        if not self.enabled:
            return []
        projects, _ = self._load_projects()
        return deepcopy(list(projects.values()))

    def get_project(self, project_id):
        self._require_enabled()
        projects, _ = self._load_projects()
        if project_id not in projects:
            raise BoardError('Board project does not exist')
        result = deepcopy(projects[project_id])
        result['canvas'], _ = self._load_canvas(project_id)
        return result

    def _project(self, projects, project_id, expected_version):
        if not isinstance(project_id, str) or project_id not in projects:
            raise BoardError('Board project does not exist')
        project = projects[project_id]
        if expected_version is not None:
            if not safe_integer(expected_version) or project['version'] != expected_version:
                raise BoardError('Board project version is stale')
        return project

    def _finish_project(self, project, sequence):
        project['version'] = project['version'] + 1
        project['updated_at'] = self.clock()
        return self._append_project(project, sequence)

    def update_project(self, project_id, *, title=None, description=None, note=None, manager=None,
                       status=None, expected_version=None, kind=None):
        """Revise a project's intent, hand it to another manager, or close it."""
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            project = self._project(projects, project_id, expected_version)
            if kind is not None:
                if kind not in ('planning', 'subject'):
                    raise BoardError('Unknown workspace kind')
                project['kind'] = kind
            if title is not None:
                project['title'] = self._text(title)
            for key, value in (('description', description), ('note', note)):
                if value is not None:
                    if not isinstance(value, str) or len(value.encode()) > self.max_text_bytes:
                        raise BoardError('Board project text is empty or exceeds configured byte limit')
                    project[key] = value
            if manager is not None:
                if manager and not _valid_owner(manager):
                    raise BoardError('Invalid project manager')
                project['manager'] = manager
            if status is not None:
                if not isinstance(status, str) or status not in PROJECT_STATES:
                    raise BoardError('Unknown project status')
                project['status'] = status
            return self._finish_project(project, sequence)

    def set_steps(self, project_id, steps, *, expected_version=None):
        """Replace the ordered plan. Each step is {'text': str, 'done': bool}.

        Replacing is the whole plan at once so a step cannot drift out of order
        between two sessions editing the same project.
        """
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise BoardError('Project steps must be a list of at most %d items' % MAX_STEPS)
        cleaned = []
        for step in steps:
            if not isinstance(step, dict):
                raise BoardError('Invalid project step')
            text = self._text(step.get('text'))
            done = step.get('done', False)
            if type(done) is not bool:
                raise BoardError('Invalid project step state')
            cleaned.append({'id': step.get('id') if isinstance(step.get('id'), str) and step['id']
                            else uuid.uuid4().hex, 'text': text, 'done': done})
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            project = self._project(projects, project_id, expected_version)
            project['steps'] = cleaned
            return self._finish_project(project, sequence)

    def update_canvas(self, project_id, changes, *, validate_media=None):
        from .project_canvas import apply_changes
        with self._locked():
            projects, _ = self._load_projects(repair=True)
            project = self._project(projects, project_id, None)
            cards, _ = self._load_cards(repair=True)
            owned = set(cards)
            steps = {step['id'] for step in project['steps']}
            current, sequence = self._load_canvas(project_id, repair=True)
            canvas = apply_changes(current, changes, cards=owned, steps=steps)
            if validate_media:
                media_changes = set()
                for patch in changes.get('nodes', []):
                    if any(key in patch for key in ('kind', 'session_slug', 'attachment_id')):
                        media_changes.add(patch['id'])
                for node in canvas['nodes']:
                    if node['id'] in media_changes and node['kind'] in ('image', 'document'):
                        validate_media(node)
            self._append_record(self.canvas_path, {'project_id': project_id, 'canvas_version': canvas['version'],
                                                   'changes': deepcopy(changes)}, sequence)
            result = deepcopy(project)
            result['canvas'] = canvas
            return result

    def append_project_note(self, project_id, note):
        """Append to the latest project record while holding its journal lock."""
        note = self._text(note)
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            project = self._project(projects, project_id, None)
            project['note'] = self._appended_note(project['note'], note)
            return self._finish_project(project, sequence)

    def add_step(self, project_id, text):
        """Add a plan step without replacing contributions from other callers."""
        text = self._text(text)
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            project = self._project(projects, project_id, None)
            if len(project['steps']) >= MAX_STEPS:
                raise BoardError('Project plan exceeds step limit')
            project['steps'].append({'id': uuid.uuid4().hex, 'text': text, 'done': False})
            return self._finish_project(project, sequence)

    def check_step(self, project_id, step_id, done, *, expected_version=None):
        """Tick or untick one step without rewriting the rest of the plan."""
        if type(done) is not bool:
            raise BoardError('Invalid project step state')
        with self._locked():
            projects, sequence = self._load_projects(repair=True)
            project = self._project(projects, project_id, expected_version)
            for step in project['steps']:
                if step['id'] == step_id:
                    step['done'] = done
                    return self._finish_project(project, sequence)
            raise BoardError('Project step does not exist')

    def attach_artifact(self, card_id, reference, *, actor=None, expected_version=None, log_start=None, log_end=None):
        reference = self._text(reference)
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            if reference in card['artifacts']:
                return deepcopy(card)
            if len(card['artifacts']) + len(card.get('evidence', [])) >= self.max_artifacts:
                raise BoardError('Board artifact collection limit reached')
            card['artifacts'].append(reference)
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, log_start, log_end)

    def ask_human(self, card_id, question, *, pending=True, gate=None, actor=None, expected_version=None,
                  log_start=None, log_end=None):
        question = self._text(question)
        if type(pending) is not bool:
            raise BoardError('Board question pending flag must be boolean')
        gate = gate or {'ask_human': 1, 'needs_authority': 1, 'source': 'deterministic'}
        saved_gate = {}
        for key in ('ask_human', 'needs_authority', 'source'):
            saved_gate[key] = gate[key]
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            if len(card['questions']) >= self.max_questions:
                raise BoardError('Board question collection limit reached')
            card['questions'].append({'id': uuid.uuid4().hex, 'text': question, 'pending': pending, 'gate': saved_gate})
            if pending:
                card['status'] = 'blocked'
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, log_start, log_end)

    def claim(self, card_id, owner, *, actor=None, expected_version=None):
        """Take an unclaimed card, or hand it to another session. Releasing uses owner=''."""
        if not isinstance(owner, str) or (owner and not _valid_owner(owner)):
            raise BoardError('Invalid board owner')
        self._known_owner(owner)
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            current = card.get('owner') or ''
            # actor=None is the operator, who can release or reassign any claim.
            if current and owner and current != owner and actor is not None and current != actor:
                raise BoardError('Board card is claimed by another session')
            if not owner and current and actor is not None and current != actor:
                raise BoardError('Only the owner or the operator can release a claim')
            card['owner'] = owner
            if owner:
                card['updated_by'] = owner
            elif card['status'] == 'running':
                # Nobody is running a released card, so it returns to the open pool.
                card['status'] = 'queued'
            card['updated_at'] = self.clock()
            card['version'] = card['version'] + 1
            return self._append_card(card, sequence)

    def answer_human(self, card_id, question_id, answer, *, actor=None, expected_version=None):
        """Record a person's answer to a card question and clear its pending flag.

        The answer is appended to the card's note, so the reason the work unblocked stays
        readable on the card itself. A card that was blocked only by this question returns
        to the open pool: the answer is what unblocks it, not a separate status move.
        """
        answer = self._text(answer)
        if not isinstance(question_id, str) or not question_id:
            raise BoardError('Invalid board question id')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            found = None
            for question in card['questions']:
                if question['id'] == question_id:
                    found = question
                    break
            if found is None:
                raise BoardError('Board card has no such question')
            if not found['pending']:
                raise BoardError('Board question is already answered')
            found['pending'] = False
            found['answer'] = answer
            previous = card.get('note') or ''
            card['note'] = self._appended_note(previous, 'Answered: ' + answer)
            if card['status'] == 'blocked' and not any(q['pending'] for q in card['questions']):
                card['status'] = 'queued'
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, None, None)

    def revise(self, card_id, *, title=None, note=None, actor=None, expected_version=None,
               log_start=None, log_end=None, priority=_MISSING):
        """Correct a card's title, note or priority without moving its status."""
        if title is None and note is None and priority is _MISSING:
            raise BoardError('Board revise needs a title, a note or a priority')
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            if title is not None:
                card['title'] = self._text(title)
            if priority is not _MISSING:
                if priority is None:
                    card.pop('priority', None)
                else:
                    card['priority'] = _priority_value(priority)
            if note is not None:
                # A full replace is the person's correction, so an empty note is refused
                # but an oversized one names the measured size instead of guessing.
                if not isinstance(note, str) or not note.strip():
                    raise BoardSizeError('Board note is empty')
                if len(note.encode()) > self.max_text_bytes:
                    raise BoardSizeError('Board note is %d bytes; the limit is %d bytes'
                                         % (len(note.encode()), self.max_text_bytes))
                card['note'] = note
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, log_start, log_end)

    def append_note(self, card_id, text, *, actor=None, expected_version=None,
                    log_start=None, log_end=None):
        """Add one progress line to a card's note, trimming old history when full.

        Separate from revise() because the two mean different things: revise replaces the
        whole note, append adds to it. Routing an append through replace drops every
        new line once the note is full.
        """
        self._note_room(text)
        with self._locked():
            cards, sequence = self._load_cards(repair=True)
            card = self._owned(cards, card_id, actor, expected_version)
            card['note'] = self._appended_note(card.get('note') or '', text)
            if actor is not None:
                card['updated_by'] = actor
            return self._finish(card, sequence, log_start, log_end)
