"""Gather artifacts from outside into the board as intake cards.

A gathered pull request or issue becomes a card. It does not automatically become work:
it is assigned to an intake desk that reviews the change, notes the compatibility and the
subject matter, and submits it for acceptance.

A gathered artifact becomes a card in the `intake` state, which is not claimable work. The
collector is explicit about which provider and which repository it reads: it never guesses
a remote and never scrapes a page. A reference is handed in (or listed from one named
repository), and the card carries the reference, the source and the summary the reviewer
will read.

Nothing here merges, rebases, or touches the code. Acceptance is the board's own gate
(tessera/board.py accept_intake), so this module only produces the card.
"""

from dataclasses import dataclass
import json
import subprocess


PROVIDERS = ('github',)


class IntakeError(ValueError):
    pass


@dataclass
class GatherSpec:
    """Which provider, which repository, and how to reach them.

    A repository is named explicitly as ``owner/name``. There is no default and no
    inference from the local checkout: a collector that guessed would gather artifacts from
    whatever repository happened to be lying around, which is the ambiguity this module
    rules out by requiring the provider and repository to be stated.

    ``limit`` and ``timeout`` come from [intake] when the caller does not state them, so how
    much to gather and how long to wait are install decisions rather than constants here.
    """
    provider: str
    repository: str
    limit: int = None
    timeout: float = None

    def resolved(self, config):
        """Fill the unstated bounds from config, and validate the result."""
        settings = getattr(config, 'intake', None)
        if self.limit is None:
            self.limit = getattr(settings, 'limit', None)
        if self.timeout is None:
            self.timeout = getattr(settings, 'timeout_seconds', None)
        if self.limit is None or self.timeout is None:
            raise IntakeError('Intake bounds are not configured; set [intake] limit and timeout_seconds')
        return self.validate()

    def validate(self):
        if self.provider not in PROVIDERS:
            raise IntakeError('Intake provider must be one of: ' + ', '.join(PROVIDERS))
        if not isinstance(self.repository, str) or self.repository.count('/') != 1 \
                or not all(part.strip() for part in self.repository.split('/')):
            raise IntakeError('Intake repository must be named owner/name')
        if type(self.limit) is not int or self.limit <= 0:
            raise IntakeError('Intake limit must be a positive integer')
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or self.timeout <= 0:
            raise IntakeError('Intake timeout must be a positive number')
        return self


def _gh(*args, timeout=None):
    """Run the GitHub CLI and return parsed JSON. Absence of the CLI is a named failure."""
    if timeout is None:
        raise IntakeError('A provider timeout must be supplied; it comes from [intake] timeout_seconds')
    try:
        result = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise IntakeError('The GitHub CLI (gh) is not installed; the intake collector needs it '
                          'to read pull requests from a named repository') from None
    except subprocess.TimeoutExpired:
        raise IntakeError('The GitHub CLI timed out reading the repository') from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        raise IntakeError('GitHub CLI refused: ' + (detail[0] if detail else 'unknown error'))
    try:
        return json.loads(result.stdout or '[]')
    except json.JSONDecodeError:
        raise IntakeError('GitHub CLI returned something that is not JSON') from None


def list_pull_requests(spec, config=None):
    """List open pull requests for one named repository as gather records.

    Read-only. Each record carries what the intake card needs to be reviewed without the
    reviewer going back to the provider: the number, the title, who opened it, and whether
    it reports as mergeable.

    ``config`` is a parameter, not a module global. The limit and the provider
    timeout are install settings, and this function reads them from that argument.
    """
    if config is not None:
        spec.resolved(config)
    else:
        spec.validate()
    if spec.provider != 'github':
        raise IntakeError('Unsupported intake provider: ' + spec.provider)
    rows = _gh('pr', 'list', '--repo', spec.repository, '--state', 'open',
               '--limit', str(spec.limit),
               '--json', 'number,title,author,mergeable,headRefName,url,body', timeout=spec.timeout)
    gathered = []
    for row in rows:
        number = row.get('number')
        if type(number) is not int:
            continue
        gathered.append({
            'source': 'github:' + spec.repository,
            'reference': f'{spec.repository}#{number}',
            'title': (row.get('title') or '').strip(),
            'author': (row.get('author') or {}).get('login', '') if isinstance(row.get('author'), dict) else '',
            'branch': row.get('headRefName') or '',
            'url': row.get('url') or '',
            'mergeable': row.get('mergeable') or '',
            'summary': (row.get('body') or '').strip()[:2000],
        })
    return gathered


def gather_record(spec, number, config=None):
    """Read one pull request by number from one named repository.

    Resolve the unstated bounds first, for the same reason list_pull_requests does: a
    caller that names a number and leaves the timeout to [intake] would otherwise be told
    its timeout "must be a positive number" when the truth is that it was never set.
    """
    if config is not None:
        spec.resolved(config)
    else:
        spec.validate()
    if type(number) is not int or number <= 0:
        raise IntakeError('A pull request number must be a positive integer')
    rows = _gh('pr', 'view', str(number), '--repo', spec.repository,
               '--json', 'number,title,author,mergeable,headRefName,url,body', timeout=spec.timeout)
    if not isinstance(rows, dict):
        raise IntakeError('GitHub CLI returned an unexpected shape for one pull request')
    return {
        'source': 'github:' + spec.repository,
        'reference': f'{spec.repository}#{rows.get("number")}',
        'title': (rows.get('title') or '').strip(),
        'author': (rows.get('author') or {}).get('login', '') if isinstance(rows.get('author'), dict) else '',
        'branch': rows.get('headRefName') or '',
        'url': rows.get('url') or '',
        'mergeable': rows.get('mergeable') or '',
        'summary': (rows.get('body') or '').strip()[:2000],
    }


def intake_record(record):
    """The board's intake field for one gathered record: the fields acceptance reads."""
    if not isinstance(record, dict) or not record.get('reference'):
        raise IntakeError('A gathered record needs a reference')
    return {
        'source': record.get('source', ''),
        'reference': record['reference'],
        'subject': '',
        'compatibility': '',
        'summary': record.get('summary', ''),
    }


def gather_into_board(board, spec, session_slug, *, numbers=None, project='', config=None):
    """Create intake cards for open pull requests (or the named numbers) on the board.

    Each card starts in `intake`: not claimable, not work, and not offered to any worker.
    Re-running is idempotent per reference because the card id is derived from the
    reference, so a collector that polls does not multiply cards for one pull request.
    """
    # Resolve BEFORE validating. The bounds a caller leaves unstated come from [intake];
    # validating first rejected every ordinary gather with "Intake timeout must be a
    # positive number", which names the wrong problem: the value was unset, not invalid.
    if config is not None:
        spec.resolved(config)
    else:
        spec.validate()
    if numbers:
        records = [gather_record(spec, number, config=config) for number in numbers]
    else:
        records = list_pull_requests(spec, config)
    created = []
    for record in records:
        title = record.get('title') or record['reference']
        note = '\n'.join(part for part in (
            f'Gathered from {record.get("source", "")}',
            f'Branch: {record["branch"]}' if record.get('branch') else '',
            f'Opened by: {record["author"]}' if record.get('author') else '',
            f'Mergeable: {record["mergeable"]}' if record.get('mergeable') else '',
            f'URL: {record["url"]}' if record.get('url') else '',
        ) if part)
        card = board.create(title, session_slug, 0, 0,
                            card_id=_card_id_for(record['reference']),
                            note=note, owner='', project=project,
                            intake=intake_record(record))
        created.append(card)
    return created


def _card_id_for(reference):
    """A stable card id per gathered reference, so polling does not duplicate cards."""
    import hashlib
    return 'intake-' + hashlib.sha256(reference.encode('utf-8')).hexdigest()[:24]
