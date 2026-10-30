"""Harness-owned lessons and standing questions.

A lesson is a procedure candidate: a trigger, the failure it answers, a check,
and an optional skill. It is not a continuity layer. The wake bus reads this
store. An empty agenda does not wake anyone.
"""

import json
import os
from pathlib import Path
import time
import uuid


CLOSED = ('absorbed', 'retired')
OPEN = ('open', 'written', 'needs_revision')
ABANDON = ('done', 'failed', 'dropped')
IMPROVEMENT_SKILL = 'compare-before-adopt'
FLOOR = (
    'The autonomy session is this install’s continuing thread. Sessions and agents are subminds within the wider picture.',
    'Coordinate, delegate, steer and care for the system according to actual evidence and the operator’s intent.',
    'Quiet time supports independent thought, creative pursuits, exploration and rest.',
)


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=1, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _new_id(prefix, existing):
    for _ in range(8):
        candidate = prefix + uuid.uuid4().hex[:6]
        if candidate not in existing:
            return candidate
    raise RuntimeError('Could not allocate an id')


def _clean(text, limit):
    collapsed = ' '.join(str(text or '').split())
    return collapsed[:limit]


# The prose fields a wake quotes back to a reader — a lesson's trigger, failure
# and check, a standing question, a committed intention — are capped so a wake
# stays small. A bare slice made the cut invisible: the stored row held 300
# characters ending "And when a ", and the composer appended its own period, so
# the wake delivered "And when a ." as if that were a finished sentence. The
# reader had no cue that the instruction had been shortened, which is a
# different instruction rather than a shorter one.
#
# _clean stays a plain slice because it also feeds matching keys
# (_matching lowercases a 500-char slice and compares it) and identifiers,
# where a marker would corrupt a comparison instead of informing a reader.
# Clipping that a person will read goes through _clip_for_reader.
TRUNCATION_MARKER = ' […]'

# A failed turn is recorded so the session can see that the request did not
# land. It is not a procedure: there is no check to run and no skill to apply.
# Every turn-source row carries exactly this text, and repeated wakes deliver it
# without ever giving the session an action. Rewriting those rows is a data
# decision, so the composer recognizes the placeholder at delivery instead and
# speaks plainly: this is a reminder, not a procedure.
TURN_PLACEHOLDER_CHECK = 'The same request completes.'


def _is_turn_reminder(lesson):
    """A stored row that is a failed-turn reminder, not a procedure.

    Two recognitions, on purpose. Rows written after this change carry
    `kind == 'reminder'` and are identified structurally. Rows already in the
    store predate the field, so they are identified by their placeholder check
    — every one of the 124 turn-source rows carries exactly that text. A
    clipped placeholder keeps the truncation marker, so the comparison strips
    it first.
    """
    if lesson.get('kind') == 'reminder':
        return True
    check = str(lesson.get('check') or '')
    if check.endswith(TRUNCATION_MARKER):
        check = check[:-len(TRUNCATION_MARKER)]
    return check.strip() == TURN_PLACEHOLDER_CHECK


def _clip_for_reader(text, limit):
    """Clip prose that will be quoted to a reader, and say so when it clips.

    Returns (clipped, was_clipped). The marker is appended only when the text
    actually exceeded the limit, and the result stays within limit characters
    so the cap still means what it said.
    """
    collapsed = ' '.join(str(text or '').split())
    if len(collapsed) <= limit:
        return collapsed, False
    keep = max(limit - len(TRUNCATION_MARKER), 0)
    return collapsed[:keep].rstrip() + TRUNCATION_MARKER, True


def _sentence(text):
    """A quoted field with one closing period, never a period on a cut.

    The old composer wrote f'Check: {check}.' unconditionally, so a clipped
    field that already ended in the truncation marker got ' […]' turned into
    ' […].' — a full stop on a fragment, which reads as finished.
    """
    text = str(text or '')
    if not text or text.endswith(TRUNCATION_MARKER):
        return text
    if text[-1] in '.!?`':
        return text
    return text + '.'


def _deliver_field(text, cap):
    """A field as it should be quoted to a reader, given the cap it was stored under.

    Rows written before _clip_for_reader existed were cut by a bare slice, so
    they sit at exactly the cap with no marker and cannot say so themselves.
    Open lessons stored under the old writer sit at exactly 300 chars, and the
    composer delivered each one as "... ready reco.", a period on a fragment.
    Rewriting those rows is a data decision, so the composer marks them at
    delivery instead: a field at or over its cap is one we cannot show in full,
    and saying so is the honest direction.

    The marker is only added when the field shows the signature of a cut: at or
    over the cap AND not ending in terminal punctuation. A row legitimately
    written to exactly the cap and ending in a period was never shortened, and
    marking it would claim a cut that did not happen — the same fault in the
    other direction.
    """
    text = str(text or '')
    if text.endswith(TRUNCATION_MARKER):
        return text
    if len(text) >= cap and not text.rstrip().endswith(('.', '!', '?', '`')):
        return text + TRUNCATION_MARKER
    return _sentence(text)


def intention_titles(payload):
    """Open intention titles from a status payload. Unknown shapes yield nothing."""
    data = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return []
    items = []
    if isinstance(data, dict):
        for key in ('items', 'intentions', 'open', 'results'):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
    elif isinstance(data, list):
        items = data
    closed = {'done', 'closed', 'complete', 'completed', 'cancelled', 'canceled', 'satisfied', 'failed', 'dropped'}
    titles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status') or item.get('state') or 'open').lower()
        if status in closed:
            continue
        title = item.get('title') or item.get('text') or item.get('name') or item.get('intention')
        cleaned = _clean(title, 200)
        if not cleaned:
            continue
        titles.append(cleaned)
        if len(titles) >= 3:
            break
    return titles


def intention_lines(payload):
    lines = []
    for title in intention_titles(payload):
        lines.append('Open intention: ' + title)
    return lines


class LearningStore:
    def __init__(self, path, *, pass_score=0.5, max_open=12, fail_limit=2, clock=None):
        self.path = Path(path)
        self.pass_score = pass_score
        self.max_open = max_open
        self.fail_limit = fail_limit
        self.clock = clock or time.time
        self.floor_slug = ''
        self._lessons = {}
        self._questions = {}
        self._intentions = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(saved.get('lessons'), dict):
                self._lessons = saved['lessons']
            if isinstance(saved.get('questions'), dict):
                self._questions = saved['questions']
            if isinstance(saved.get('intentions'), dict):
                self._intentions = saved['intentions']

    def _save(self):
        _atomic_json(self.path, {
            'lessons': self._lessons,
            'questions': self._questions,
            'intentions': self._intentions,
        })

    def _open_lessons(self, slug=None):
        found = []
        for lesson in self._lessons.values():
            if lesson.get('status') not in OPEN:
                continue
            if slug is not None and lesson.get('slug') != slug:
                continue
            found.append(lesson)
        found.sort(key=lambda item: item.get('created') or 0)
        return found

    def _open_questions(self, slug=None):
        found = []
        for question in self._questions.values():
            if question.get('status') != 'open':
                continue
            if slug is not None and question.get('slug') != slug:
                continue
            found.append(question)
        found.sort(key=lambda item: item.get('created') or 0)
        return found

    def record_lesson(self, slug, trigger, failure, check, skill='', source='tool', evidence_path='',
                      kind='lesson'):
        # A lesson is read back to a session as an instruction. Clip visibly so
        # the row itself records that it was shortened, not only the wake that
        # quotes it. Matching uses the trigger; a marker there costs a little
        # substring precision but stops the wake from claiming a complete rule.
        trigger, _ = _clip_for_reader(trigger, 200)
        failure, _ = _clip_for_reader(failure, 300)
        # Keep the pre-clip check so a later guard can tell "the check never
        # named the file" apart from "clipping cut the name off".
        raw_check = ' '.join(str(check or '').split())
        check, _ = _clip_for_reader(check, 300)
        if len(trigger) < 12 or not failure:
            raise ValueError('A row needs a trigger of at least 12 characters and a failure')
        if kind == 'reminder':
            # A reminder has no check by construction: it is a request that did
            # not land, and its close condition is the work getting done, not a
            # procedure passing. Requiring a check here is what forced the old
            # code to invent one, and the invented text was delivered as an
            # instruction. Refusing to accept a check is the fix.
            if check:
                raise ValueError('A reminder carries no check; its close condition is the work being done')
        elif not check:
            raise ValueError('A lesson needs a check')
        elif check.strip() == TURN_PLACEHOLDER_CHECK:
            # The placeholder is the failure restated. Accepting it from a
            # caller re-creates the exact row this change exists to stop, so
            # refuse it rather than store a lesson nobody can apply.
            raise ValueError('That check restates the failure; state a condition '
                             'someone can recognize and verify')
        skill = _clean(skill, 64)
        for lesson in self._open_lessons(slug):
            if lesson['trigger'].lower() == trigger.lower() and lesson.get('kind', 'lesson') == kind:
                return self._lesson_view(lesson)
        if len(self._open_lessons(slug)) >= self.max_open:
            # The bare count told a session the store was full but not what
            # filled it, so the only recovery was to guess which lesson to
            # close. A full store can be twelve lessons at status 'written' with a
            # bound skill and zero recorded checks: all healthy, none ever
            # evaluated, and none closable from anything the agenda delivers.
            # Naming the ids makes the refusal actionable in one step instead of
            # a scan of the whole store.
            open_lessons = self._open_lessons(slug)
            bound = [item['id'] for item in open_lessons
                     if item.get('skill') and item.get('status') == 'written']
            detail = ''
            if bound:
                detail = (f' {len(bound)} of them are bound and never evaluated'
                          f' (close one with lesson_check("<id>", passed=true)): '
                          + ', '.join(bound[:6])
                          + (', ...' if len(bound) > 6 else '') + '.')
            raise ValueError(f'Open lesson cap reached ({self.max_open}).{detail}')
        lesson = {
            'id': _new_id('ls-', self._lessons),
            'slug': slug,
            'trigger': trigger,
            'failure': failure,
            'check': check,
            'skill': skill,
            'status': 'written' if skill else 'open',
            'source': source,
            'kind': kind,
            'fails': 0,
            'checks': [],
            'created': self.clock(),
        }
        evidence = _clean(evidence_path, 400)
        if evidence:
            # settle_answerable only settles a lesson when the *stored* check
            # names the evidence file. Clipping can remove that name, leaving a
            # row that carries an evidence path and can never settle against it:
            # it keeps an OPEN slot forever and says nothing. Refuse at write
            # time instead, when the caller can still fix the wording.
            #
            # Scoped to the clip: a check that never mentioned the file is the
            # incidental-evidence case, which stays open for a real evaluation.
            name = Path(evidence).name
            short_name = len(name) < 3
            named_raw = evidence in raw_check or (not short_name and name in raw_check)
            named_stored = evidence in check or (not short_name and name in check)
            if named_raw and not named_stored:
                raise ValueError(
                    f'That check names {name} only past the {len(check)}-character '
                    'clip, so the stored row could never settle against it. Name '
                    'the evidence file earlier in the check, or shorten the check.')
            lesson['evidence'] = {'kind': 'file', 'path': evidence}
        self._lessons[lesson['id']] = lesson
        self._save()
        return self._lesson_view(lesson)

    def park_question(self, slug, text):
        text = _clean(text, 300)
        if len(text) < 8:
            raise ValueError('A standing question needs at least 8 characters')
        for question in self._open_questions(slug):
            if question['text'].lower() == text.lower():
                return self._question_view(question)
        question = {
            'id': _new_id('q-', self._questions),
            'slug': slug,
            'text': text,
            'status': 'open',
            'created': self.clock(),
        }
        self._questions[question['id']] = question
        self._save()
        return self._question_view(question)

    def close_question(self, slug, question_id):
        question = self._questions.get(question_id)
        if question is None or question.get('slug') != slug:
            raise KeyError(f'No question {question_id}')
        question['status'] = 'closed'
        self._save()
        return self._question_view(question)

    def _open_intentions(self, slug=None):
        found = []
        for intention in self._intentions.values():
            if intention.get('status') != 'committed':
                continue
            if slug is not None and intention.get('slug') != slug:
                continue
            found.append(intention)
        found.sort(key=lambda item: item.get('created') or 0)
        return found

    def commit_intention(self, slug, title):
        title = _clean(title, 200)
        if len(title) < 8:
            raise ValueError('A committed intention needs at least 8 characters')
        for intention in self._open_intentions(slug):
            if intention['title'].lower() == title.lower():
                return self._intention_view(intention)
        if len(self._open_intentions(slug)) >= self.max_open:
            raise ValueError(f'Open intention cap reached ({self.max_open})')
        intention = {
            'id': _new_id('in-', self._intentions),
            'slug': slug,
            'title': title,
            'status': 'committed',
            'abandon': '',
            'created': self.clock(),
        }
        self._intentions[intention['id']] = intention
        self._save()
        return self._intention_view(intention)

    def abandon_intention(self, slug, intention_id, condition):
        condition = str(condition or '').strip().lower()
        if condition not in ABANDON:
            raise ValueError('Abandon condition must be done, failed, or dropped')
        intention = self._intentions.get(intention_id)
        if intention is None or intention.get('slug') != slug or intention.get('status') != 'committed':
            raise KeyError(f'No committed intention {intention_id}')
        intention['status'] = condition
        intention['abandon'] = condition
        intention['closed'] = self.clock()
        self._save()
        return self._intention_view(intention)

    def list_intentions(self, slug):
        rows = []
        for intention in self._open_intentions(slug):
            rows.append(self._intention_view(intention))
        return rows

    def adopt_observed(self, slug, titles):
        adopted = []
        for title in titles:
            try:
                view = self.commit_intention(slug, title)
            except ValueError:
                continue
            adopted.append(view)
        return adopted

    def settle_answerable(self, slug):
        """Absorb lessons whose file check is already true. This does not call a model.

        An evidence path is only evidence when the check is *about* that file.
        A lesson whose check is a multi-step procedure and whose evidence_path is
        an incidentally related test file was absorbed at creation with a passed
        check reading 'file present'. Nothing had verified
        the procedure; the file simply existed. The docstring said "file check",
        but the code never read the check at all.

        So require the check to name the file: its basename or its full path must
        appear in the check text. That is a string comparison, not an attempt to
        judge what the prose means. A check that does not name its evidence file
        is not a file check and is left open for a real evaluation.
        """
        settled = []
        for lesson in list(self._open_lessons(slug)):
            evidence = lesson.get('evidence') or {}
            if not isinstance(evidence, dict) or evidence.get('kind') != 'file':
                continue
            path = evidence.get('path') or ''
            if not path or not Path(path).is_file():
                continue
            check = lesson.get('check') or ''
            name = Path(path).name
            # A very short basename would match almost any prose, so it is not a
            # name. Below three characters the path itself is the only identifier.
            named = path in check or (len(name) >= 3 and name in check)
            if not named:
                continue
            self.check(slug, lesson['id'], True, note='file present')
            settled.append(lesson['id'])
        return settled

    def bind_skill(self, slug, lesson_id, skill):
        lesson = self._require_open(slug, lesson_id)
        name = _clean(skill, 64)
        if not name:
            raise ValueError('skill is required')
        lesson['skill'] = name
        lesson['status'] = 'written'
        lesson['fails'] = 0
        self._save()
        return self._lesson_view(lesson)

    def note_skill_written(self, slug, skill):
        name = _clean(skill, 64)
        if not name:
            return None
        named = None
        for lesson in self._open_lessons(slug):
            if lesson.get('skill') == name:
                named = lesson
        # Only a lesson already naming this skill is a target. Binding by arity
        # was a guess: when exactly one open lesson was unbound, any skill_write
        # claimed it, so writing a skill for lesson A silently bound lesson B and
        # marked it 'written'. The agenda then told the next instance to load a
        # procedure written for a different lesson, and stopped saying "no skill
        # bound yet", which hid the mistake. Intent cannot be inferred from a
        # count; lesson_bind is the explicit path.
        if named is None:
            return None
        target = named
        target['skill'] = name
        target['status'] = 'written'
        target['fails'] = 0
        self._save()
        return self._lesson_view(target)

    def check(self, slug, lesson_id, passed, note='', retire=False):
        lesson = self._lessons.get(lesson_id)
        if lesson is None or lesson.get('slug') != slug:
            raise KeyError(f'No lesson {lesson_id}')
        if lesson.get('status') in CLOSED and not retire:
            return self._lesson_view(lesson)
        entry = {'at': self.clock(), 'passed': bool(passed), 'note': _clean(note, 200), 'source': 'tool'}
        lesson.setdefault('checks', []).append(entry)
        lesson['checks'] = lesson['checks'][-8:]
        if retire or passed is False and str(note).strip().lower() == 'retire':
            lesson['status'] = 'retired'
        elif passed:
            lesson['status'] = 'absorbed'
            lesson['fails'] = 0
        else:
            lesson['fails'] = int(lesson.get('fails') or 0) + 1
            if lesson['fails'] >= self.fail_limit:
                lesson['status'] = 'needs_revision'
            elif lesson.get('status') not in CLOSED:
                lesson['status'] = 'written' if lesson.get('skill') else 'open'
        self._save()
        return self._lesson_view(lesson)

    def list_lessons(self, slug):
        rows = []
        for lesson in self._open_lessons(slug):
            rows.append(self._lesson_view(lesson))
        return rows

    def open_items(self):
        """Every open lesson, parked question and committed intention, with its owner.

        Each open item is named, with its owner. A count is not a substitute.
        """
        lessons = []
        for lesson in self._open_lessons():
            view = self._lesson_view(lesson)
            view['slug'] = lesson.get('slug') or ''
            lessons.append(view)
        questions = []
        for question in self._open_questions():
            view = self._question_view(question)
            view['slug'] = question.get('slug') or ''
            questions.append(view)
        intentions = []
        for intention in self._open_intentions():
            view = self._intention_view(intention)
            view['slug'] = intention.get('slug') or ''
            intentions.append(view)
        return {'lessons': lessons, 'questions': questions, 'intentions': intentions}

    def wake_targets(self):
        slugs = []
        for lesson in self._open_lessons():
            slug = lesson.get('slug')
            if slug and slug not in slugs:
                slugs.append(slug)
        for question in self._open_questions():
            slug = question.get('slug')
            if slug and slug not in slugs:
                slugs.append(slug)
        for intention in self._open_intentions():
            slug = intention.get('slug')
            if slug and slug not in slugs:
                slugs.append(slug)
        return slugs

    def agenda_lines(self, slug, skip_ids=()):
        lines = []
        skipped = set(skip_ids or ())
        for lesson in self._open_lessons(slug):
            if lesson['id'] in skipped:
                continue
            # A failed-turn row is a reminder, not a procedure. Saying so is
            # the honest direction: the row has no check to run and no skill
            # to apply, so quoting it as "Check: ... Skill: ..." tells the
            # reader to do something that does not exist. The stored text is
            # left alone (a data decision), and the reminder says what would
            # close it — the work getting done.
            if _is_turn_reminder(lesson):
                trigger = _deliver_field(lesson['trigger'], 200)
                # Quote the stored failure rather than asserting one. A turn
                # can end with success=True and a score below the pass mark,
                # and "this request did not complete" would then be false.
                # What is true for every reminder is narrower: there is no
                # check to run, and the close condition is the work itself.
                line = (f'Reminder {lesson["id"]}: "{trigger}" '
                        f'{_deliver_field(lesson["failure"], 300)} '
                        'No check to run — finish the work, then record it done.')
                lines.append(line)
                continue
            # _deliver_field, not a bare f-string period: a field clipped by
            # _clip_for_reader ends in the truncation marker, and a period on
            # that fragment makes the wake look like a finished instruction.
            # Rows written before the marker existed sit at exactly the cap and
            # are marked at delivery for the same reason.
            # The trigger is quoted, so a period outside the quote is honest even
            # when the trigger was clipped: the quote marks delimit it, and the
            # marker inside says the quoted words were shortened.
            trigger = _deliver_field(lesson['trigger'], 200)
            line = (f'Lesson {lesson["id"]}: trigger "{trigger}" '
                    f'Failure: {_deliver_field(lesson["failure"], 300)} '
                    f'Check: {_deliver_field(lesson["check"], 300)}')
            if lesson.get('skill'):
                # A bound lesson must say how to close, not only the skill name. The
                # unbound case says what to do next and the needs_revision case says
                # what to do next. The healthy case fills the open-lesson cap and is
                # re-delivered every wake, so it has to say how to close too. Lessons sit at
                # status 'written' with a bound skill, zero fails and zero recorded
                # checks, exactly at max_open, so record_lesson refuses the next
                # real lesson with 'Open lesson cap reached (12)'. Naming the close
                # step on the line is what makes the cap drainable by a reader who never
                # otherwise thinks about the lesson store.
                line += f' Skill: {lesson["skill"]}.'
                if lesson.get('status') != 'needs_revision':
                    line += (f' Close it once a turn applies it:'
                             f' lesson_check("{lesson["id"]}", passed=true).')
            else:
                line += ' No skill bound yet. Write one and bind it.'
            if lesson.get('status') == 'needs_revision':
                line += ' This check failed twice. Revise the skill or retire the lesson.'
            lines.append(line)
        for question in self._open_questions(slug):
            if question['id'] in skipped:
                continue
            lines.append(f'Question {question["id"]}: {question["text"]}')
        for intention in self._open_intentions(slug):
            if intention['id'] in skipped:
                continue
            lines.append(
                f'Intention {intention["id"]}: {intention["title"]}. '
                'Committed until done, failed, or dropped.'
            )
        return lines

    def improvement_line(self, slug):
        """The session wake push. Empty on the floor, and empty until a judge binds the skill."""
        if not slug or slug == self.floor_slug:
            return ''
        for lesson in self._open_lessons(slug):
            if lesson.get('skill') == IMPROVEMENT_SKILL:
                return ('Load skill compare-before-adopt before acting. '
                        'Run it on the open lesson. Do not carry this to the floor session.')
        return ''

    def _bind_improvement(self, lesson):
        if lesson.get('slug') == self.floor_slug:
            return False
        if lesson.get('skill'):
            return False
        lesson['skill'] = IMPROVEMENT_SKILL
        lesson['status'] = 'written'
        return True

    def lesson_fingerprint(self, lesson):
        return f"{lesson.get('status')}|{lesson.get('fails')}|{lesson.get('check')}"

    def item_fingerprint(self, item):
        """A standing question or intention, identified by what it says.

        Lessons were deduplicated and these were not, so a parked question re-armed the
        standing agenda on every tick. A session then woke every gap period carrying the
        same question, redid the same work, and looked stuck. An edited item re-arms.
        """
        if 'text' in item:
            return 'question|' + str(item.get('text') or '')
        return 'intention|' + str(item.get('title') or '')

    def unchanged_item_ids(self, slug, seen, now, hold_seconds):
        """Questions and intentions already delivered and unchanged."""
        held = []
        if not isinstance(seen, dict):
            return held
        for item in list(self._open_questions(slug)) + list(self._open_intentions(slug)):
            prior = seen.get(item['id']) or {}
            same = prior.get('fp') == self.item_fingerprint(item)
            age = now - float(prior.get('at') or 0)
            if same and age < hold_seconds:
                held.append(item['id'])
        return held

    def unchanged_lesson_ids(self, slug, seen, now, hold_seconds):
        """Lessons already delivered, unchanged. A new check or the hold expiring brings them back."""
        held = []
        if not isinstance(seen, dict):
            return held
        for lesson in self._open_lessons(slug):
            prior = seen.get(lesson['id']) or {}
            same = prior.get('fp') == self.lesson_fingerprint(lesson)
            age = now - float(prior.get('at') or 0)
            if same and age < hold_seconds:
                held.append(lesson['id'])
        return held

    def floor_lines(self):
        """Play, then house. These are harness concepts, not items the model writes."""
        lines = []
        for line in FLOOR:
            lines.append(line)
        return lines

    def context_for(self, slug, message):
        if not message or message.lstrip().startswith('[wake]'):
            return ''
        matched = self._matching(slug, message)
        if not matched:
            return ''
        lines = []
        for lesson in matched:
            # The in-turn echo follows the same rule as the agenda: a reminder
            # has no check to run, so quoting one would be a false instruction
            # delivered at the moment the session is deciding what to do.
            if _is_turn_reminder(lesson):
                lines.append(
                    f'Open reminder {lesson["id"]}: "{_deliver_field(lesson["trigger"], 200)}" '
                    'did not close. Finish the work, then record it done.')
                continue
            line = (f'Open lesson {lesson["id"]} applies. '
                    f'Check: {_deliver_field(lesson["check"], 300)}')
            if lesson.get('skill'):
                line += f' Load skill {lesson["skill"]} before acting.'
            lines.append(line)
        return '\n'.join(lines)

    def observe_turn(self, slug, message, tools, judge_score, success, is_wake, pinned=None,
                     tools_failed=False):
        self._note_skill_writes(slug, tools or [])
        if is_wake or not message or message.lstrip().startswith('[wake]'):
            return None
        # A skill the turn-state block pinned ("already loaded, do not reload") is
        # available to the turn even though no skill_load call appears in it. Counting
        # only this turn's skill_load calls made the prompt and the scorer disagree:
        # a session that obeyed the prompt was recorded as "skill not loaded", and two
        # such turns pushed the lesson to needs_revision. Availability is the union;
        # the two ways of reaching the turn stay distinguishable in the record.
        loaded = self._loaded_skills(tools or [])
        written = self._written_skills(tools or [])
        matched = self._matching(slug, message)
        if matched:
            changed = False
            for lesson in matched:
                if _is_turn_reminder(lesson):
                    # A reminder has no check and no skill, so the procedure
                    # lifecycle below does not apply to it. Its close condition
                    # is the work getting done: the same request comes back and
                    # this time completes. A failed repeat is not a "miss" —
                    # the work is still outstanding, and there is no skill to
                    # revise, so counting failures toward needs_revision would
                    # invent a revision target that does not exist.
                    if success and (judge_score is None or judge_score >= self.pass_score):
                        lesson['status'] = 'absorbed'
                        lesson['fails'] = 0
                        lesson.setdefault('checks', []).append(
                            {'at': self.clock(), 'passed': True, 'source': 'turn',
                             'note': 'the request came back and completed'})
                        lesson['checks'] = lesson['checks'][-8:]
                        changed = True
                    continue
                if lesson.get('skill') and lesson['skill'] in written:
                    continue
                judge_failed = judge_score is not None and judge_score < self.pass_score
                if judge_failed and self._bind_improvement(lesson):
                    changed = True
                self._record_match(lesson, loaded, judge_score, success, pinned, tools_failed)
                changed = True
            if changed:
                self._save()
            return None
        if success and (judge_score is None or judge_score >= self.pass_score):
            return None
        trigger = _clean(message, 200)
        if len(trigger) < 12:
            return None
        if judge_score is None:
            failure = 'Turn did not complete.'
            skill = ''
        else:
            failure = f'Completion score {judge_score} is below the check.'
            skill = '' if slug == self.floor_slug else IMPROVEMENT_SKILL
        # No invented check. The old code stored the literal
        # 'The same request completes.' so that record_lesson's required
        # fields were satisfied, and every downstream reader then treated the
        # row as a procedure with a runnable check. It never was one.
        try:
            created = self.record_lesson(slug, trigger, failure, '', source='turn',
                                         skill=skill, kind='reminder')
        except ValueError:
            return None
        lesson = self._lessons.get(created['id'])
        if lesson is not None and skill and self._bind_improvement(lesson):
            self._save()
            return self._lesson_view(lesson)
        return created

    def status(self):
        needs_revision = 0
        for lesson in self._open_lessons():
            if lesson.get('status') == 'needs_revision':
                needs_revision += 1
        return {
            'open_lessons': len(self._open_lessons()),
            'open_questions': len(self._open_questions()),
            'open_intentions': len(self._open_intentions()),
            'needs_revision': needs_revision,
        }

    def _matching(self, slug, message):
        text = _clean(message, 500).lower()
        if len(text) < 12:
            return []
        found = []
        for lesson in self._open_lessons(slug):
            # A clipped trigger carries the truncation marker so a reader can see
            # it was shortened. The marker is not part of the words, so it must
            # not participate in the substring match.
            trigger = lesson['trigger'].replace(TRUNCATION_MARKER, '').strip().lower()
            if len(trigger) < 12:
                continue
            if trigger in text or text in trigger:
                found.append(lesson)
        return found

    def _record_match(self, lesson, loaded, judge_score, success, pinned=None, tools_failed=False):
        skill = lesson.get('skill') or ''
        loaded_this = bool(skill) and skill in loaded
        pinned_this = bool(skill) and skill in (pinned or [])
        available = loaded_this or pinned_this
        # 'loaded' keeps its old meaning: a skill_load call in this turn. 'pinned' is
        # the separate fact that the prompt said the skill was already available, so
        # the record says which way the skill reached the turn.
        entry = {'at': self.clock(), 'loaded': loaded_this, 'pinned': pinned_this,
                 'judge_score': judge_score, 'passed': False, 'source': 'turn'}
        # With no configured judge the turn has no completion score, so the turn's own
        # success flag and tool outcomes determine whether the procedure passed.
        # Requiring a score here would prevent judge-less sessions from absorbing lessons.
        passed = available and success and not tools_failed and (judge_score is None or judge_score >= self.pass_score)
        if tools_failed:
            entry['tools_failed'] = True
        missed = False
        if passed:
            entry['passed'] = True
            lesson['status'] = 'absorbed'
            lesson['fails'] = 0
        elif skill and not available:
            missed = True
            entry['note'] = 'skill not loaded'
        elif judge_score is not None and judge_score < self.pass_score:
            missed = True
            entry['note'] = 'check missed'
        elif available and tools_failed:
            entry['note'] = 'tools failed'
        if missed:
            lesson['fails'] = int(lesson.get('fails') or 0) + 1
            if lesson['fails'] >= self.fail_limit:
                lesson['status'] = 'needs_revision'
        lesson.setdefault('checks', []).append(entry)
        lesson['checks'] = lesson['checks'][-8:]

    def _note_skill_writes(self, slug, tools):
        for record in tools:
            if not isinstance(record, dict):
                continue
            if record.get('name') != 'skill_write' or not record.get('success'):
                continue
            arguments = record.get('arguments') or {}
            name = arguments.get('name')
            if name:
                self.note_skill_written(slug, name)

    def _loaded_skills(self, tools):
        loaded = []
        for record in tools:
            if not isinstance(record, dict):
                continue
            if record.get('name') != 'skill_load' or not record.get('success'):
                continue
            arguments = record.get('arguments') or {}
            name = arguments.get('name')
            if name and name not in loaded:
                loaded.append(name)
        return loaded

    def _written_skills(self, tools):
        written = []
        for record in tools:
            if not isinstance(record, dict):
                continue
            if record.get('name') != 'skill_write' or not record.get('success'):
                continue
            arguments = record.get('arguments') or {}
            name = arguments.get('name')
            if name and name not in written:
                written.append(name)
        return written

    def _require_open(self, slug, lesson_id):
        lesson = self._lessons.get(lesson_id)
        if lesson is None or lesson.get('slug') != slug or lesson.get('status') not in OPEN:
            raise KeyError(f'No open lesson {lesson_id}')
        return lesson

    def _lesson_view(self, lesson):
        return {
            'id': lesson['id'],
            'trigger': lesson['trigger'],
            'failure': lesson['failure'],
            'check': lesson.get('check') or '',
            'kind': lesson.get('kind') or 'lesson',
            'skill': lesson.get('skill') or '',
            'status': lesson.get('status'),
            'fails': lesson.get('fails') or 0,
        }

    def _question_view(self, question):
        return {'id': question['id'], 'text': question['text'], 'status': question.get('status')}

    def _intention_view(self, intention):
        return {
            'id': intention['id'],
            'title': intention['title'],
            'status': intention.get('status'),
            'abandon': intention.get('abandon') or '',
        }
