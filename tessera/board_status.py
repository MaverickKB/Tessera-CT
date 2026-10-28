"""Passive board metadata and revision-fenced links, without execution constructors."""

import hashlib
import json
import time

from .board import CARD_STATES, CLAIMABLE_STATES, PROJECT_STATES, BoardError, _MISSING, replay_cards, replay_projects
from .passive_status import configured_path, read_snapshot, read_snapshot_reasoned


class BoardRevisionError(BoardError):
    pass


def _unreadable(name, reason):
    """One sentence naming the file and the wall it hit.

    The reasons from read_snapshot_reasoned are clauses ("is 4202752 bytes and the
    reader cap is 4194304 bytes"), so they follow the name. Replay errors are full
    sentences ("Invalid board journal sequence at record 2"), so they stand alone.
    Both read correctly without the caller guessing which kind it has.
    """
    if reason[:1].isupper():
        return reason
    return '%s %s' % (name, reason)


def _card_at(cards, ordinal, card_id=None):
    """Resolve a card by position, refusing when the caller named a different card.

    The ordinal is a position. It changes whenever any session writes to the
    board. A caller that knows the card passes card_id, and that identity is
    the card written. The position is not consulted. A named card that is
    absent is a refusal, because there is nothing left to write.

    The revision moves while a person is typing. Naming the card keeps the
    note on the card they meant.
    """
    if card_id is not None:
        if not isinstance(card_id, str) or not card_id:
            raise BoardError('Invalid board card identity')
        for card in cards:
            if card['id'] == card_id:
                return card
        raise BoardRevisionError('That card is no longer on the board; refresh to see the current one')
    if type(ordinal) is not int or ordinal <= 0 or ordinal > len(cards):
        raise BoardError('Invalid board card ordinal')
    return cards[ordinal - 1]


def board_snapshot(config, session_slugs):
    flags = {'enabled': config.board.enabled, 'configured': bool(config.board.journal_file)}
    if not flags['enabled'] or not flags['configured']:
        return flags, [], []
    path = configured_path(config.home, config.board.journal_file)
    _, records, _, _, reason = read_snapshot_reasoned(path, config.status)
    if records is None:
        # A blank board must say why it is blank. Returning only {enabled, configured}
        # reads as an empty board, which is what a journal that outgrew its cap looked
        # like, and what a corrupt line was once misread as. The reason is a fact about
        # the file, not a guess about the data.
        return {**flags, 'unreadable': _unreadable('the board journal', reason)}, [], []
    try:
        cards, sequence = replay_cards(records)
    except BoardError as error:
        # Replay is a version-sequenced fold, so a bad record cannot be skipped: dropping
        # it would invent a card state that never existed. It is named instead, and it
        # never self-heals — repair only heals a torn tail, not a corrupt complete line.
        return {**flags, 'unreadable': str(error)}, [], []
    # Projects are a separate journal. A missing or unreadable project file leaves the
    # cards readable; it does not take the board down with it.
    projects = {}
    project_path = path.with_name(path.stem + '.projects' + path.suffix)
    _, project_records, _, _, project_reason = read_snapshot_reasoned(project_path, config.status)
    if project_records is not None:
        try:
            projects, project_sequence = replay_projects(project_records)
            sequence = max(sequence, project_sequence)
        except BoardError as error:
            projects = {}
            flags = {**flags, 'projects_unreadable': str(error)}
    elif project_reason is not None:
        # Absent is fine (no projects yet). Unreadable is a fact worth saying.
        flags = {**flags, 'projects_unreadable': _unreadable('the projects journal', project_reason)}
    sessions = {}
    for ordinal, slug in enumerate(session_slugs, 1):
        sessions[slug] = ordinal
    # A removed card is evidence, not work. It stays in the journal and is dropped here,
    # so every reader sees the same board the person took the card off.
    live = [card for card in cards.values() if not card.get('removed')]
    rows = []
    identities = []
    for ordinal, card in enumerate(live, 1):
        pending = False
        for question in card['questions']:
            pending |= question['pending']
        row = {'ordinal': ordinal, 'status': card['status'], 'attempt': card['attempt'],
               'artifact_count': len(card['artifacts']) + len(card.get('evidence', [])), 'question_pending': pending}
        if card['session_slug'] in sessions:
            row.update(session_ordinal=sessions[card['session_slug']], log_start=card['log_start'], log_end=card['log_end'])
        rows.append(row)
        identities.append([card['id'], card['version']])
    report = {**flags, 'cards': rows}
    # No prompts, refs, paths or titles are used in the public revision digest. Project
    # identities are included so ticking a step or closing a project changes the revision
    # the page is fenced to; without them the plan would never redraw.
    project_identities = sorted([pid, project['version']] for pid, project in projects.items())
    stamp = {'sequence': sequence, 'identities': identities, 'projects': project_identities,
             'report': report, 'sessions': session_slugs}
    report['revision'] = hashlib.sha256(json.dumps(stamp, sort_keys=True).encode()).hexdigest()
    return report, live, list(projects.values())


def board_status(config, session_slugs):
    return board_snapshot(config, session_slugs)[0]


def board_view(config, session_slugs):
    """Operator board. Titles and timestamps stay off the passive status projection."""
    report, cards, projects = board_snapshot(config, session_slugs)
    if 'cards' not in report:
        return report
    rows = []
    for row, card in zip(report['cards'], cards):
        # A card that asked a person something has to carry the question itself, or the
        # page can only say "input requested" with no way to answer it.
        questions = []
        for question in card['questions']:
            questions.append({'id': question['id'], 'text': question['text'],
                              'pending': question['pending'], 'answer': question.get('answer', '')})
        rows.append({**row, 'title': card['title'], 'created_at': card['created_at'], 'updated_at': card['updated_at'],
                     **({'evidence': card['evidence']} if card.get('evidence') else {}),
                     'owner': card.get('owner', ''), 'note': card.get('note', ''),
                     'updated_by': card.get('updated_by', ''),
                     'project': card.get('project', ''),
                     # The session that wrote the card. Project history needs it: a card
                     # carries its creator, and the last writer sits in updated_by, so a
                     # project can say which threads touched its work without reading
                     # another session's log.
                     'session_slug': card['session_slug'],
                     # The operator view names the card so it can be acted on. Without this a
                     # reader sees a card it cannot claim, note or file: every board_* tool
                     # addresses cards by id, and the passive status projection carries none.
                     'card_id': card['id'],
                     # Priority is a field on the card. Absence means unset, which is
                     # not the same as a number guessed from the title.
                     **({'priority': card['priority']} if isinstance(card.get('priority'), int)
                        and not isinstance(card.get('priority'), bool) else {}),
                     # The offer fingerprint needs a stable identity for the card that
                     # changes only when the card actually changes. Age is not that.
                     'version': card['version'],
                     'questions': questions,
                     'claimable': not card.get('owner') and card['status'] in CLAIMABLE_STATES,
                     'execution': card.get('execution'),
                     **({'acceptances': card['acceptances']} if card.get('acceptances') else {}),
                     **({'intake': card['intake']} if card.get('intake') else {}),
                     **({'ignored': card['ignored']} if card.get('ignored') else {})})
    view = {**report, 'cards': rows, 'projects': project_rows(projects, rows)}
    return _describe_owners(config, view)


def _describe_owners(config, view):
    """Name the identity behind each owner, when identity records are configured.

    The registry is attached to the config by the session manager. Absent or disabled,
    this returns the view untouched, so an install that has not turned identity records
    on keeps the exact payload its page already parses.

    An owner that resolves to nothing is left as written with ``owner_known`` false. That
    is the honest reading: the card was written before this layer existed, or by a handle
    nobody holds. Rewriting it to something tidier would erase the evidence that a card
    is owned by a name that wakes nobody — which is the failure this layer exists to
    surface, not to hide.
    """
    registry = getattr(config, 'identity_registry', None)
    if registry is None or not registry.enabled:
        return view
    for row in view.get('cards') or []:
        described = registry.describe(row.get('owner') or '')
        row['owner_id'] = described['owner_id']
        row['owner_known'] = described['owner_known']
        row['owner_kind'] = described['owner_kind']
    for row in view.get('projects') or []:
        described = registry.describe(row.get('manager') or '')
        row['manager_id'] = described['owner_id']
        row['manager_known'] = described['owner_known']
        row['manager_kind'] = described['owner_kind']
    return view


def project_rows(projects, cards):
    """Projects with their plan and the work filed under each.

    Plan checks are bookkeeping, not evidence of delivered work. cards_settled
    counts succeeded, failed and cancelled cards; settlement does not imply success.
    plan_ahead_of_work is a ratio heuristic, not a check of prose or dependencies.
    With no filed cards, the settled share is zero.

    sessions names the threads that touched this project, so one project surface can
    show its own history instead of only its open work. It is derived from what the
    board already records: the project's creator, its manager, each filed card's
    creator (session_slug, which replay fixes for the card's life) and each card's
    last writer (updated_by). That is a floor, not a complete log. Compaction keeps
    one record per card, so a session that wrote a card and was later superseded by
    another writer is no longer named, and a session that only read or commented
    without writing the card was never recorded at all. Absence here means "not
    evidenced by the board", not "never participated".
    """
    by_project = {}
    for card in cards:
        key = card.get('project') or ''
        if key:
            by_project.setdefault(key, []).append(card)
    rows = []
    for project in projects:
        filed = by_project.get(project['id'], [])
        steps = project.get('steps') or []
        done_steps = sum(1 for step in steps if step['done'])
        counts = {state: 0 for state in sorted(CARD_STATES)}
        for card in filed:
            counts[card['status']] = counts.get(card['status'], 0) + 1
        settled = sum(counts[state] for state in ('succeeded', 'failed', 'cancelled'))
        # Cross multiplication avoids rounding; an empty card set has zero delivery.
        plan_ahead = bool(steps) and done_steps * max(len(filed), 1) > settled * len(steps)
        sessions = {}
        def touch(slug, card=None):
            if not slug:
                return
            row = sessions.setdefault(slug, {'slug': slug, 'cards': set(), 'last_at': 0})
            if card is not None:
                # A card's creator and its last writer are often the same session, and
                # one session touching one card twice is still one card.
                row['cards'].add(card.get('card_id') or card.get('id') or id(card))
                row['last_at'] = max(row['last_at'], card.get('updated_at') or 0)
        # The project itself is a session's record too: whoever opened it and whoever
        # manages it now both count as having touched this work.
        touch(project.get('session_slug'))
        touch(project.get('manager'))
        for card in filed:
            touch(card.get('session_slug'), card)
            touch(card.get('updated_by'), card)
        participants = [{'slug': row['slug'], 'cards': len(row['cards']), 'last_at': row['last_at']}
                        for row in sessions.values()]
        rows.append({**project,
                     'steps_done': done_steps, 'steps_total': len(steps),
                     'cards_settled': settled, 'plan_ahead_of_work': plan_ahead,
                     'cards_open': sum(1 for card in filed if card['status'] in ('queued', 'running', 'blocked')),
                     'cards_total': len(filed), 'card_counts': counts,
                     'unclaimed': sum(1 for card in filed if card['claimable']),
                     'sessions': sorted(participants, key=lambda row: (-row['last_at'], row['slug']))})
    rows.sort(key=lambda row: (row['status'] != 'active', -row['updated_at']))
    return rows


def board_brief(config, session_slugs, *, now=None):
    """The board an agent reads: who owns what, what waits, and what nobody has taken.

    The operator view and the agent view are the same facts. The brief adds the counts a
    session needs to decide whether to claim work, and names the unclaimed items outright
    so a wake does not have to infer them from an empty owner field.
    """
    view = board_view(config, session_slugs)
    if 'cards' not in view:
        return view
    stamp = time.time() if now is None else now
    counts = {state: 0 for state in sorted(CARD_STATES)}
    unclaimed = []
    unfiled = []
    mine = []
    cards = []
    for row in view['cards']:
        counts[row['status']] = counts.get(row['status'], 0) + 1
        age = max(0, int(stamp - row['updated_at']))
        item = {**row, 'age_seconds': age}
        cards.append(item)
        if row['claimable']:
            unclaimed.append({'ordinal': row['ordinal'], 'card_id': row['card_id'], 'title': row['title'], 'status': row['status'],
                              'age_seconds': age, 'note': row['note'], 'project': row.get('project', '')})
        # The model decides which board and project a card belongs to; a person cannot
        # create boards or projects. Filing is the model's job, so a session reading the
        # board must be told which cards still need filing.
        # Naming them here is the mechanism: the pressure line says a card is unfiled,
        # this says which one, which is what a session needs in order to file it.
        if row['status'] in ('queued', 'running', 'blocked') and not row.get('project'):
            unfiled.append({'ordinal': row['ordinal'], 'card_id': row['card_id'], 'title': row['title'], 'status': row['status'],
                            'age_seconds': age, 'note': row['note']})
    # The live session list travels with the brief so a reader can tell a card that is
    # genuinely in flight from one whose executing session has ended. Without it the
    # `running` state is an unfalsifiable claim.
    return {**view, 'cards': cards, 'counts': counts, 'unclaimed': unclaimed, 'unfiled': unfiled,
            'sessions': sorted(session_slugs),
            'filing_guidance': 'Tessera files cards. Reuse a suitable group; create or revise kind=subject for a granular area, recurring symptom or healing effort. Use kind=planning for an intentional collaborative project. Subjects can hold related fixes and lessons without requiring a plan. Review unfiled cards and read their evidence before filing; do not create one category per card by default.',
            'open': sum(1 for row in cards if row['status'] in ('queued', 'running', 'blocked'))}


def board_move(store, config, session_slugs, ordinal, revision, status, actor=None, card_id=None):
    if not isinstance(status, str) or status not in CARD_STATES:
        raise BoardError('Unknown board status')
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.update_status(card['id'], status, actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_start(store, config, session_slugs, ordinal, revision, session, receipt, card_id=None):
    """Mark a claimed card as actively executed by the named session."""
    if not isinstance(session, str) or not session:
        raise BoardError('Invalid board session')
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.start_card(card['id'], session, receipt, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_link(config, session_slugs, ordinal, revision):
    if type(ordinal) is not int or ordinal <= 0:
        raise BoardError('Invalid board card ordinal')
    snapshot, cards, _ = board_snapshot(config, session_slugs)
    if not isinstance(revision, str) or not revision or snapshot.get('revision') != revision:
        raise BoardRevisionError('Board snapshot changed or is unavailable; refresh before opening a card')
    if ordinal > len(cards):
        raise BoardError('Board card ordinal is outside this snapshot')
    card = cards[ordinal - 1]
    if card['session_slug'] not in session_slugs:
        raise BoardError('Board session is unavailable')
    return {'session_slug': card['session_slug'], 'log_start': card['log_start'], 'log_end': card['log_end']}


def _fenced_cards(config, session_slugs, revision):
    snapshot, cards, _ = board_snapshot(config, session_slugs)
    if not isinstance(revision, str) or not revision or snapshot.get('revision') != revision:
        raise BoardRevisionError('Board snapshot changed or is unavailable; refresh before changing a card')
    return cards


def _cards_for_write(config, session_slugs, revision, card_id=None):
    """The snapshot a write resolves against.

    A caller that names the card it means is not fenced by the revision: the write
    lands on that card by identity in the current snapshot, so a page older than the
    board still writes to the right card. A caller that names only a position keeps
    the fence, because position alone is not evidence about which card was meant.
    """
    if card_id is not None:
        snapshot, cards, _ = board_snapshot(config, session_slugs)
        if 'cards' not in snapshot:
            # Unreadable is not the same as "your card is gone". Name the real wall.
            raise BoardRevisionError(snapshot.get('unreadable') or 'The board is unavailable; refresh before changing a card')
        return cards
    return _fenced_cards(config, session_slugs, revision)


def board_claim(store, config, session_slugs, ordinal, revision, owner, *, actor=None, card_id=None):
    """Claim an unclaimed card, hand it to a session, or release it with owner=''."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.claim(card['id'], owner, actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_revise(store, config, session_slugs, ordinal, revision, *, title=None, note=None, actor=None, card_id=None,
                 priority=_MISSING):
    """Correct the title, note or priority of a card in the current snapshot."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.revise(card['id'], title=title, note=note, actor=actor, expected_version=card['version'],
                 priority=priority)
    return board_view(config, session_slugs)


def board_note(store, config, session_slugs, ordinal, revision, note, *, actor=None, card_id=None):
    """Append progress to a card's note, newest text last, without rewriting its history."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.append_note(card['id'], note, actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_file(store, config, session_slugs, ordinal, revision, project, *, actor=None, card_id=None):
    """File a card under a project (or unfiled with project='') from the current snapshot."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.file_card(card['id'], project, actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_remove(store, config, session_slugs, ordinal, revision, *, actor=None, card_id=None):
    """Take a card off the board from the current snapshot. The record is kept, flagged."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.remove(card['id'], actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_project_create(store, config, session_slugs, title, *, description='', manager='', note='', actor=None, kind='planning'):
    """Open a project. The creating session is the manager unless one is named."""
    project = store.create_project(title, actor, description=description, manager=manager or '', note=note, kind=kind)
    return board_view(config, session_slugs), project['id']


def board_create(store, config, session_slugs, title, *, note='', project='', session_slug='', owner='',
                 actor=None, evidence=None):
    """Write a card from the operator's page.

    A person's observation is work the board did not have before. It is created unclaimed
    by default. An unclaimed card wakes nobody: a session wakes only for cards it owns,
    and the floor wakes for house pressure, not for board work. The card is filed under
    the floor thread when no session is named, because a note is not evidence in any
    session's log and a card still needs an owning thread to be readable.
    """
    if not isinstance(project, str) or len(project) > 256:
        raise BoardError('Invalid board project')
    if project:
        store.get_project(project)
    if not session_slug:
        # A person's note must not depend on one particular thread existing. Prefer the
        # floor thread, then any live session. A card needs an owning thread to be
        # readable, not a specific one.
        floor = config.autonomy.infinity_slug
        if floor in session_slugs:
            session_slug = floor
        elif session_slugs:
            session_slug = session_slugs[0]
        else:
            raise BoardError('No session is available to own this card yet')
    if session_slug not in session_slugs:
        raise BoardError('Board session is unavailable')
    if owner and not isinstance(owner, str):
        raise BoardError('Invalid board owner')
    card = store.create(title, session_slug, 0, 0, note=note, owner=owner, project=project, evidence=evidence)
    return board_view(config, session_slugs), card['id']


def board_answer(store, config, session_slugs, ordinal, revision, question_id, answer, *, actor=None, card_id=None):
    """Answer a card's question from the page and unblock the work that waited on it."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.answer_human(card['id'], question_id, answer, actor=actor, expected_version=card['version'])
    return board_view(config, session_slugs)


def accept_board_card(store, config, session_slugs, ordinal, revision, by, evidence='', note='', card_id=None):
    """Record an acceptance from the page, fenced on the same revision as every write."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.accept(card['id'], by, evidence=evidence, note=note, expected_version=card['version'])
    return board_view(config, session_slugs)


def triage_intake_card(store, config, session_slugs, ordinal, revision, subject=None, compatibility=None,
                       summary=None, *, actor=None, card_id=None):
    """Record what an intake reviewer found, fenced on the same revision as every write."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.triage_intake(card['id'], subject=subject, compatibility=compatibility, summary=summary,
                        expected_version=card['version'])
    return board_view(config, session_slugs)


def accept_intake_card(store, config, session_slugs, ordinal, revision, by, note='', card_id=None):
    """Accept a triaged intake card as work."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.accept_intake(card['id'], by, note=note, expected_version=card['version'])
    return board_view(config, session_slugs)


def ignore_intake_card(store, config, session_slugs, ordinal, revision, by, reason, card_id=None):
    """Decline an intake card as work, keeping it as evidence."""
    cards = _cards_for_write(config, session_slugs, revision, card_id)
    card = _card_at(cards, ordinal, card_id)
    store.ignore_intake(card['id'], by, reason=reason, expected_version=card['version'])
    return board_view(config, session_slugs)


def board_project_update(store, config, session_slugs, project_id, *, title=None, description=None, note=None,
                         manager=None, status=None, kind=None):
    store.update_project(project_id, title=title, description=description, note=note, manager=manager, status=status, kind=kind)
    return board_view(config, session_slugs)


def board_steps(store, config, session_slugs, project_id, steps):
    store.set_steps(project_id, steps)
    return board_view(config, session_slugs)


def board_step_check(store, config, session_slugs, project_id, step_id, done):
    store.check_step(project_id, step_id, done)
    return board_view(config, session_slugs)
