"""Typed readers for the floor definition.

Each provider turns live state into rows of plain fields. A provider does not
decide thresholds, wording, or whether the floor wakes. Those live on the
rules that read the rows. Field names are the contract in SIGNALS; the
evaluator rejects a rule that names a field the signal does not offer.
"""

SIGNALS = {
    'host': {
        'label': 'Host health and disk',
        'summary': 'One row per infrastructure host the floor can see.',
        'fields': {
            'ordinal': 'Position of the host in the infrastructure report.',
            'label': 'Display label, or "host {ordinal}" when the row has none.',
            'label_lower': 'The display label in lower case, for a rule that wants to test the words in it.',
            'health': 'Current health word from the infrastructure report.',
            'prior_health': 'Health recorded at the previous floor wake, or empty on the first reading.',
            'health_changed': 'True when a previous health exists and differs from the current one.',
            'disk_free_bytes': 'Free disk bytes, or null when the report has no disk reading.',
            'disk_total_bytes': 'Total disk bytes, or null when the report has no disk reading.',
            'disk_free_percent': 'Free disk as a percent, or null when it cannot be computed.',
            'prior_disk_low': 'True when the previous wake already recorded this host as low on disk.',
            'declared_managed': 'True, false, or null. Null means the systems registry did not say.',
            'declared_disk_free_percent_below': 'Per-host disk threshold from the systems registry, or null.',
        },
    },
    'board_card': {
        'label': 'Board card',
        'summary': 'One row per card on the board brief.',
        'fields': {
            'ordinal': 'Card position in the current brief.',
            'title': 'Card title.',
            'status': 'Card status.',
            'project': 'Project id, or empty when the card is unfiled.',
            'age_seconds': 'Seconds since the card was last updated. Missing ages read as 0.',
            'owner': 'Owning session slug, or empty when unclaimed.',
            'card_id': 'Stable card id.',
            'card_key': 'card_id, or the ordinal as text when the card has no id.',
            'version': 'Card version.',
            'note': 'Card note.',
            'pending_question': 'Text of the latest pending question, or empty.',
            'session_known': 'True when the brief included the live session list.',
            'holder': 'Session named on a running card, or empty.',
            'holder_live': 'True when that session is in the live list.',
            'priority': 'Integer priority stored on the card, or null when the card has none.',
            'unfiled': '0 when the card has a project, 1 when it does not.',
            'title_one_line': 'The title with line breaks collapsed.',
            'owner_is_floor': 'True when the owner is the floor session.',
            'fingerprint': 'status:version, the identity coordination uses to tell a card changed.',
        },
    },
    'notice': {
        'label': 'Floor error',
        'summary': 'One row per reading the floor failed to take. The row is the exception, not an empty success.',
        'fields': {
            'source': 'Which reading failed, such as systems-registry or intention-tool.',
            'error_type': 'Exception type name.',
            'message': 'Exception message.',
        },
    },
    'session': {
        'label': 'Session',
        'summary': 'One row per session coordination_status reports.',
        'fields': {
            'slug': 'Session slug.',
            'busy': 'True when the session has an active turn.',
            'is_floor': 'True when the slug is the floor session.',
            'turn_created': 'Created timestamp of the active turn, or empty.',
            'activity': 'Activity status of the active turn, or empty.',
            'voice': 'True when the session is a voice thread.',
        },
    },
    'engine': {
        'label': 'Engine occupancy',
        'summary': 'One row per configured infrastructure.pressure_engines entry.',
        'fields': {
            'host': 'Engine host from configuration.',
            'port': 'Engine port from configuration.',
            'model': 'Engine model name from configuration.',
            'slots_total': 'Reported slot ceiling, or the configured fallback.',
            'running': 'Running requests, or null when the engine could not be read.',
            'waiting': 'Waiting requests, or null when the engine could not be read.',
            'warming': 'True when the engine is warming, or null when it could not be read.',
            'free': 'Free slots, or null when the engine could not be read.',
            'reachable': 'True when health and metrics were read.',
            'error': 'Read error text, or null when the reading succeeded.',
        },
    },
    'endpoint': {
        'label': 'Watched endpoint',
        'summary': 'One row per endpoint answer the floor has already probed.',
        'fields': {
            'id': 'Probe key.',
            'label': 'Probe key. The poll stores the answer, not a second display name.',
            'state': 'Latest answer: up, down, or unknown.',
        },
    },
    'timer': {
        'label': 'Timer',
        'summary': 'One row per autonomy timer.',
        'fields': {
            'id': 'Timer id.',
            'slug': 'Session the timer belongs to.',
            'note': 'Timer note.',
            'due': 'Due time in epoch seconds.',
            'repeat': 'Repeat interval in seconds, or null.',
            'created': 'Created time in epoch seconds.',
            'late_seconds': 'How far past due the timer is. Zero until due.',
        },
    },
    'agenda_item': {
        'label': 'Session agenda line',
        'summary': 'One row per lesson, question, or intention line a session would be shown.',
        'fields': {
            'slug': 'Session slug.',
            'voice': 'True when that session is a voice thread.',
            'id': 'Lesson, question, or intention id when the line names one.',
            'text': 'The agenda line as the learning store delivers it.',
            'fingerprint': 'Identity of the item, when the store has one.',
            'kind': 'lesson, reminder, question, or intention.',
        },
    },
    'session_card': {
        'label': 'Owned board offer',
        'summary': 'One row per board offer a session has not already been shown.',
        'fields': {
            'slug': 'Session that owns the card.',
            'text': 'The offer line.',
            'voice': 'True when that session is a voice thread.',
            'ordinal': 'Card position.',
            'title': 'Card title.',
            'status': 'Card status.',
            'note': 'Card note.',
            'pending_question': 'Latest pending question, or empty.',
        },
    },
}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def host_rows(report, previous, thresholds=None, on_error=None):
    """Host rows for one infrastructure report and the previous wake's memory.

    An unhashable ordinal cannot look up a declaration. Pass `on_error` to
    record that and keep the other hosts. With no caller, the lookup raises.
    """
    hosts = report.get('hosts') if isinstance(report, dict) else None
    if not isinstance(hosts, list):
        return []
    previous = previous or {}
    thresholds = thresholds if isinstance(thresholds, dict) else {}
    rows = []
    failed = False
    for host in hosts:
        if not isinstance(host, dict):
            continue
        ordinal = host.get('ordinal')
        label = host.get('label') or ('host ' + str(ordinal))
        health = host.get('health') or 'unknown'
        prior = previous.get(label) or {}
        if not isinstance(prior, dict):
            prior = {}
        prior_health = prior.get('health') or ''
        if not isinstance(prior_health, str):
            prior_health = str(prior_health)
        declared = {}
        if ordinal is not None:
            try:
                declared = thresholds.get(ordinal) or {}
            except TypeError as exc:
                declared = {}
                failed = True
                labeled = ValueError('unhashable ordinal %r (%s)' % (ordinal, exc))
                if on_error is None:
                    raise labeled from None
                on_error(labeled)
        if not isinstance(declared, dict):
            declared = {}
        metrics = host.get('metrics') if isinstance(host.get('metrics'), dict) else {}
        free = _number(metrics.get('disk_free_bytes'))
        total = _number(metrics.get('disk_total_bytes'))
        percent = None
        if free is not None and total:
            percent = (free / total) * 100
        managed = declared.get('managed') if isinstance(declared.get('managed'), bool) else None
        disk_limit = _number(declared.get('disk_free_percent_below'))
        rows.append({
            'ordinal': ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else 0,
            'label': str(label),
            'label_lower': str(label).lower(),
            'health': str(health),
            'prior_health': prior_health,
            'health_changed': bool(prior_health) and prior_health != health,
            'disk_free_bytes': free,
            'disk_total_bytes': total,
            'disk_free_percent': percent,
            'prior_disk_low': bool(prior.get('disk_low')),
            'declared_managed': managed,
            'declared_disk_free_percent_below': disk_limit,
        })
    if on_error is not None and not failed:
        on_error(None)
    return rows


def board_rows(brief, floor_slug=''):
    """Card rows. An unreadable brief is an empty list, not a guess."""
    if not isinstance(brief, dict) or 'cards' not in brief:
        return []
    cards = brief.get('cards')
    if not isinstance(cards, list):
        return []
    live = brief.get('sessions')
    known = set(live) if isinstance(live, (list, tuple, set)) else None
    rows = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        execution = card.get('execution')
        holder = ''
        if isinstance(execution, dict) and execution.get('session'):
            holder = str(execution.get('session'))
        pending = ''
        questions = card.get('questions') or []
        if isinstance(questions, list):
            waiting = [item for item in questions
                       if isinstance(item, dict) and item.get('pending') and item.get('text')]
            if waiting:
                pending = str(waiting[-1]['text'])
        ordinal = card.get('ordinal', 0)
        card_id = str(card.get('card_id') or '')
        age = card.get('age_seconds', 0)
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            age = 0
        priority = card.get('priority')
        if isinstance(priority, bool) or not isinstance(priority, int):
            priority = None
        title = str(card.get('title') or '')
        owner = str(card.get('owner') or '')
        status = str(card.get('status') or '')
        rows.append({
            'ordinal': ordinal,
            'title': title,
            'status': status,
            'project': str(card.get('project') or ''),
            'age_seconds': age,
            'owner': owner,
            'card_id': card_id,
            'card_key': card_id or str(ordinal),
            'version': card.get('version'),
            'note': str(card.get('note') or ''),
            'pending_question': pending,
            'session_known': known is not None,
            'holder': holder,
            'holder_live': bool(known is not None and holder and holder in known),
            'priority': priority,
            'unfiled': 0 if card.get('project') else 1,
            'title_one_line': ' '.join(title.split()),
            'owner_is_floor': bool(floor_slug) and owner == floor_slug,
            'fingerprint': '%s:%s' % (card.get('status'), card.get('version')),
        })
    return rows


def offer_fields(item):
    """Fields the owned-card line template reads off one card."""
    rows = board_rows({'cards': [item], 'sessions': []})
    row = dict(rows[0]) if rows else {}
    row['voice'] = False
    row['slug'] = str(item.get('owner') or '') if isinstance(item, dict) else ''
    row['text'] = ''
    return row


def session_rows(rows, floor_slug):
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        turn = row.get('turn') if isinstance(row.get('turn'), dict) else {}
        activity = row.get('activity') if isinstance(row.get('activity'), dict) else {}
        slug = str(row.get('slug') or '')
        out.append({
            'slug': slug,
            'busy': bool(row.get('busy')),
            'is_floor': slug == floor_slug,
            'turn_created': str(turn.get('created') or ''),
            'activity': str(activity.get('status') or ''),
            'voice': bool(row.get('voice')),
        })
    return out


def engine_rows(config, transport=None, on_error=None):
    """Configured engines only. An empty list does not open a connection.

    A snapshot failure is handed to on_error and contributes no rows. Success,
    and a home with no configured engines, clears a previous failure.
    """
    infrastructure = getattr(config, 'infrastructure', None)
    engines = getattr(infrastructure, 'pressure_engines', None) or []
    if not engines:
        if on_error is not None:
            on_error(None)
        return []
    from .engine_pressure import snapshot
    try:
        report = snapshot(infrastructure, transport)
    except Exception as exc:
        if on_error is not None:
            on_error(exc)
        return []
    if on_error is not None:
        on_error(None)
    rows = []
    for reading in report.get('engines') or []:
        if not isinstance(reading, dict):
            continue
        rows.append({key: reading.get(key) for key in SIGNALS['engine']['fields']})
    return rows


def endpoint_rows(state):
    health = state.get('endpoint_health') if isinstance(state, dict) else None
    if not isinstance(health, dict):
        return []
    rows = []
    for key, value in health.items():
        rows.append({'id': str(key), 'label': str(key), 'state': str(value or '')})
    return rows


def agenda_rows(learning, voice_of=None):
    """One row per open lesson, question, or intention the learning store would show."""
    from .learning import _is_turn_reminder
    voice_of = voice_of or (lambda _slug: False)
    rows = []
    for slug in learning.wake_targets():
        voice = bool(voice_of(slug))
        for lesson in learning._open_lessons(slug):
            kind = 'reminder' if _is_turn_reminder(lesson) else 'lesson'
            rows.append({
                'slug': str(slug),
                'voice': voice,
                'id': str(lesson.get('id') or ''),
                'text': str(lesson.get('trigger') or ''),
                'fingerprint': str(learning.lesson_fingerprint(lesson)),
                'kind': kind,
            })
        for question in learning._open_questions(slug):
            rows.append({
                'slug': str(slug),
                'voice': voice,
                'id': str(question.get('id') or ''),
                'text': str(question.get('text') or ''),
                'fingerprint': str(learning.item_fingerprint(question)),
                'kind': 'question',
            })
        for intention in learning._open_intentions(slug):
            rows.append({
                'slug': str(slug),
                'voice': voice,
                'id': str(intention.get('id') or ''),
                'text': str(intention.get('title') or ''),
                'fingerprint': str(learning.item_fingerprint(intention)),
                'kind': 'intention',
            })
    return rows


def session_card_rows(brief, floor_slug='', line_for=None):
    """One row per owned card that still needs action, using the offer fields."""
    if not isinstance(brief, dict):
        return []
    cards = brief.get('cards')
    if not isinstance(cards, list):
        return []
    claimable = ('queued', 'blocked', 'failed')
    rows = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        owner = str(card.get('owner') or '')
        if not owner or (floor_slug and owner == floor_slug):
            continue
        if str(card.get('status') or '') not in claimable:
            continue
        row = offer_fields(card)
        row['slug'] = owner
        row['text'] = line_for(card) if line_for else ''
        row['voice'] = bool(card.get('voice'))
        rows.append(row)
    return rows


def probe_row(signal):
    """One row of this signal's own types, so a condition is executed at save time.

    Strings stay strings and numbers stay numbers. A comparison the type rejects,
    such as a title against a number, fails here even when the live board is empty.
    """
    samples = {
        'host': {
            'ordinal': 0, 'label': 'probe', 'label_lower': 'probe', 'health': 'healthy',
            'prior_health': 'healthy', 'health_changed': False, 'disk_free_bytes': None,
            'disk_total_bytes': None, 'disk_free_percent': None, 'prior_disk_low': False,
            'declared_managed': None, 'declared_disk_free_percent_below': None,
        },
        'board_card': {
            'ordinal': 0, 'title': 'probe', 'status': 'queued', 'project': 'probe',
            'age_seconds': 0, 'owner': 'probe', 'card_id': 'probe', 'card_key': 'probe',
            'version': 0, 'note': '', 'pending_question': '', 'session_known': False,
            'holder': '', 'holder_live': False, 'priority': None, 'unfiled': 0,
            'title_one_line': 'probe', 'owner_is_floor': False, 'fingerprint': 'queued:0',
        },
        'notice': {'source': 'probe', 'error_type': 'probe', 'message': 'probe'},
        'session': {
            'slug': 'probe', 'busy': False, 'is_floor': False, 'turn_created': '',
            'activity': '', 'voice': False,
        },
        'engine': {
            'host': 'probe', 'port': 0, 'model': 'probe', 'slots_total': 0,
            'running': None, 'waiting': None, 'warming': None, 'free': None,
            'reachable': False, 'error': None,
        },
        'endpoint': {'id': 'probe', 'label': 'probe', 'state': 'unknown'},
        'timer': {
            'id': 'probe', 'slug': 'probe', 'note': '', 'due': 0, 'repeat': None,
            'created': 0, 'late_seconds': 0,
        },
        'agenda_item': {
            'slug': 'probe', 'voice': False, 'id': 'probe', 'text': 'probe',
            'fingerprint': 'probe', 'kind': 'lesson',
        },
        'session_card': {
            'slug': 'probe', 'text': 'probe', 'voice': False, 'ordinal': 0,
            'title': 'probe', 'status': 'queued', 'note': '', 'pending_question': '',
        },
    }
    return dict(samples.get(signal) or {})


def timer_rows(timers, now):
    if not isinstance(timers, dict):
        return []
    rows = []
    for timer in timers.values():
        if not isinstance(timer, dict):
            continue
        due = timer.get('due')
        late = 0
        if now is not None and isinstance(due, (int, float)) and not isinstance(due, bool):
            late = now - due
        rows.append({
            'id': str(timer.get('id') or ''),
            'slug': str(timer.get('slug') or ''),
            'note': str(timer.get('note') or ''),
            'due': due,
            'repeat': timer.get('repeat'),
            'created': timer.get('created'),
            'late_seconds': late,
        })
    return rows
