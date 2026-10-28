import re

def pressure_lines(report, previous, now, thresholds=None):
    """Live house pressure. A change or a fight wakes. A steady fact does not.

    `thresholds` is the systems registry, keyed by system id. A host row joins it
    by name. Anything not declared falls back to the previous fixed behaviour, so
    an unconfigured home behaves exactly as it did before the registry existed.
    """
    lines = []
    hosts = report.get('hosts') if isinstance(report, dict) else None
    if not isinstance(hosts, list) or not hosts:
        return ['Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']
    previous = previous or {}
    registry = thresholds if isinstance(thresholds, dict) else {}
    for host in hosts:
        if not isinstance(host, dict):
            continue
        name = host.get('label') or ('host ' + str(host.get('ordinal')))
        # Thresholds are keyed by the row's ordinal, resolved on the server from
        # config.hosts: a host name is a private selector and never appears in the
        # status payload, so the join cannot happen from the row itself.
        declared = {}
        ordinal = host.get('ordinal')
        if ordinal is not None and isinstance(registry, dict):
            try:
                declared = registry.get(ordinal) or {}
            except Exception:
                declared = {}
        health = host.get('health') or 'unknown'
        prior = previous.get(name) or {}
        prior_health = prior.get('health')
        # Managed is a declared boolean. A display label is not that decision.
        managed = declared.get('managed')
        if managed is None:
            managed = 'kiosk' in name.lower()
        if prior_health and prior_health != health:
            if managed:
                lines.append(f'Pressure: {name} changed {prior_health} -> {health}. Repair it. Restart the service if that is the fix. Do not replace the kiosk.')
            else:
                lines.append(f'Pressure: {name} changed {prior_health} -> {health}. Name what changed. Do not restart it.')
        elif health not in ('healthy', 'disabled') and prior_health != health:
            if managed:
                lines.append(f'Pressure: {name} is {health}. Repair it. Restart the service if that is the fix. Do not replace the kiosk.')
            else:
                lines.append(f'Pressure: {name} is {health}. Name it. Do not invent a repair.')
        metrics = host.get('metrics') if isinstance(host.get('metrics'), dict) else {}
        free = metrics.get('disk_free_bytes')
        total = metrics.get('disk_total_bytes')
        if free is not None and total:
            ratio = free / total
            limit = declared.get('disk_free_percent_below')
            if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                limit = 10
            if ratio * 100 < limit and not prior.get('disk_low'):
                lines.append(f'Pressure: {name} disk free is {int(ratio * 100)} percent. Name the largest disposable path. Do not delete it.')
    if not lines:
        lines.append('Pressure: hosts are steady. Do not invent work. Do not continue the last wake.')
    return lines


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

    A newline left in the excerpt delivers a stale fragment above a freshly
    rebuilt card line. Collapsing whitespace keeps the excerpt's words and
    keeps it one line.
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


def board_pressure_lines(brief):
    """Work the board is holding that nothing is acting on.

    The floor only ever read host health, so an unfiled card was invisible to the system
    that decides what matters. Filing is the operator's rule, encoded here: filing is the
    model's job, so a card nobody filed is pressure, not a person's homework. A card
    that is old and still queued is the same fact from the other side: work is not
    moving and no session owns it.

    These lines are never latched: they show for as long as the fact is true, because
    they are not a wake reason and a person looking at the page should see the real
    state of the board, not the state it had when something last judged it.
    """
    lines = []
    if not isinstance(brief, dict) or 'cards' not in brief:
        return lines
    # A `running` card whose executing session is gone is the mirror image of the
    # queued-card lie: the board says work is in flight when nobody holds it. This
    # Skipping `running` outright would hide a card whose session is gone.
    # The only way a card leaves `running` is its own session moving it, so a
    # session that ends mid-work leaves the card running, and this loop has to
    # say so.
    #
    # The judgement needs the live session list. When the brief does not carry one,
    # the check stays silent rather than inventing a verdict from absence.
    live_sessions = brief.get('sessions')
    known = set(live_sessions) if isinstance(live_sessions, (list, tuple, set)) else None
    unfiled = []
    stale = []
    abandoned = []
    for card in brief.get('cards') or []:
        if not isinstance(card, dict):
            continue
        if card.get('status') == 'running' and known is not None:
            execution = card.get('execution')
            holder = execution.get('session') if isinstance(execution, dict) else None
            if not holder or holder not in known:
                abandoned.append(card)
            continue
        if card.get('status') not in ('queued', 'blocked'):
            continue
        if not card.get('project'):
            unfiled.append(card)
        elif card.get('age_seconds', 0) >= 6 * 3600:
            stale.append(card)
    if unfiled:
        names = ', '.join(str(card.get('ordinal')) for card in unfiled[:5])
        lines.append(f'Pressure: {len(unfiled)} board card(s) unfiled ({names}). File them under a project. Do not ask a person to file them.')
    if stale:
        names = ', '.join(str(card.get('ordinal')) for card in stale[:5])
        lines.append(f'Pressure: {len(stale)} filed board card(s) older than six hours ({names}). Claim one and move it.')
    if abandoned:
        names = ', '.join(str(card.get('ordinal')) for card in abandoned[:5])
        lines.append(f'Pressure: {len(abandoned)} board card(s) read running but the session working them is gone ({names}). Release or take them over.')
    return lines


def _coordination_fingerprint(card):
    """A card's offer identity. The version moves when the card actually changes.

    The card's id lives in the record's key, not in here. The position is left out
    on purpose: an ordinal is assigned by enumeration at read time, and compaction
    renumbers every card while resetting versions, so a position in the fingerprint
    makes a card look changed whenever another card is removed above it.
    """
    return f'{card.get("status")}:{card.get("version")}'


def coordination_offers(brief, limit, seen=None, *, now=None, hold_seconds=6 * 3600):
    """Unclaimed work is the floor session's coordination agenda, highest priority first.

    Two facts starve this agenda if they are left in place.

    First, no memory. Sorting the same unowned cards on every wake and returning
    the first `limit` offers the same three forever, and the rest stay unreachable.
    The session's own-card lane already keeps a seen record keyed on
    ordinal:status:version (`_own_board_lines`), so coordination uses the same
    identity here.

    Second, ranking by title alone. Cards can tie at the fallback priority 100,
    so the order is pure ordinal and filing has no effect. A card
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
    seen = seen or {}
    candidates = []
    for card in brief['cards']:
        if not isinstance(card, dict) or card.get('owner') or card.get('status') != 'queued':
            continue
        ordinal = card.get('ordinal', 0)
        card_key = card.get('card_id') or str(ordinal)
        fingerprint = _coordination_fingerprint(card)
        previous = seen.get(card_key) or {}
        if previous.get('fp') == fingerprint:
            age = now - previous.get('at', 0) if now is not None else 0
            if age < hold_seconds:
                continue
        title = card.get('title') or ''
        match = re.search(r'(?i)\bP([0-9]+)\b', title)
        priority = int(match.group(1)) if match else 100
        unfiled = 0 if card.get('project') else 1
        candidates.append((priority, unfiled, ordinal, fingerprint, card))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    lines = []
    record = {}
    for _, _, ordinal, fingerprint, card in candidates[:limit]:
        lines.append(f"Work coordination: #{ordinal} {card.get('title', '')} "
                     f"(card_id={card.get('card_id', '')}).")
        record[card.get('card_id') or str(ordinal)] = {'fp': fingerprint, 'at': now}
    return lines, record


def wip_coordination_lines(brief, limit):
    """The coordination agenda as plain lines, for readers that do not wake anyone."""
    return coordination_offers(brief, limit)[0]


SALIENCE_WAKE = 1.0


def score_pressure(line):
    """A number for a measured change. Energy-class churn stays under the wake line."""
    text = line.lower()
    # Board work is reported, never a wake reason: a session wakes for its own cards
    # and the floor wakes for house pressure. That property has to hold for every
    # board line, so it is stated here rather than left to depend on wording. It was
    # accidental before: board lines scored 0.2 only because none happened to contain
    # " is ", and a new line ("the session working them is gone") would have scored
    # 1.4 and become a wake reason. All board pressure lines name board card(s).
    if 'board card(s)' in text:
        return 0.2
    if 'changed' in text and 'unreachable' in text:
        return 1.8
    if 'changed' in text:
        return 1.5
    if text.startswith('pressure:') and ' is ' in text and 'healthy' not in text:
        return 1.4
    if 'disk free' in text:
        return 1.2
    return 0.2


def rank_pressure(lines):
    """Highest salience first. The floor names one change, not a menu."""
    ranked = []
    for line in lines:
        if line.startswith('Pressure: house is steady'):
            continue
        ranked.append((score_pressure(line), line))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def pressure_seen(report, now):
    """The facts this reading already judged, so the next reading only fires on a change."""
    seen = {}
    hosts = report.get('hosts') if isinstance(report, dict) else []
    for host in hosts or []:
        if not isinstance(host, dict):
            continue
        name = host.get('label') or ('host ' + str(host.get('ordinal')))
        metrics = host.get('metrics') if isinstance(host.get('metrics'), dict) else {}
        free = metrics.get('disk_free_bytes')
        total = metrics.get('disk_total_bytes')
        disk_low = bool(free is not None and total and free / total < 0.1)
        seen[name] = {'health': host.get('health') or 'unknown', 'disk_low': disk_low, 'at': now}
    return seen
