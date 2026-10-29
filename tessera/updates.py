"""Self-update for a running install: report, upstream check, apply.

An installed Tessera knows where it came from. pip records the git URL and
exact commit in the package's dist-info `direct_url.json`, and a checkout
carries its own `.git`. This module reads that record, asks the repository
how far ahead upstream is, and reinstalls from the same URL on request.

The check touches the network only when asked, and only the repository:
no telemetry, no version server, no download until the operator clicks
update. What changed comes from the repository's own compare API, the same
information `git log upstream` would show.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _dist_info_dirs():
    """The installed tessera dist-info directories, if this is an install."""
    package = Path(__file__).resolve().parent
    for parent in (package.parent, *package.parents):
        for child in sorted(parent.glob('tessera-*.dist-info')):
            yield child


def _direct_url():
    for dist_info in _dist_info_dirs():
        record = dist_info / 'direct_url.json'
        if not record.is_file():
            continue
        try:
            data = json.loads(record.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        url = data.get('url') or ''
        commit = ((data.get('vcs_info') or {}).get('commit_id')) or ''
        requested = ((data.get('vcs_info') or {}).get('requested_revision')) or ''
        if url.startswith('git+'):
            url = url[4:]
        if 'github.com' in url:
            return {'url': url, 'commit': commit, 'branch': requested, 'kind': 'git-install'}
    return None


def _checkout_origin():
    package = Path(__file__).resolve().parent
    root = package
    for _ in range(4):
        if (root / '.git').exists():
            try:
                remote = subprocess.run(['git', '-C', str(root), 'remote', 'get-url', 'origin'],
                                        capture_output=True, text=True, timeout=10)
                commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                        capture_output=True, text=True, timeout=10)
                branch = subprocess.run(['git', '-C', str(root), 'rev-parse', '--abbrev-ref', 'HEAD'],
                                         capture_output=True, text=True, timeout=10)
                url = (remote.stdout or '').strip()
                if url.endswith('.git'):
                    url = url[:-4]
                url = url.replace(':', '/').replace('git@', 'https://') if url.startswith('git@') else url
                return {'url': url, 'commit': (commit.stdout or '').strip(),
                        'branch': (branch.stdout or '').strip() or 'main', 'kind': 'checkout'}
            except (OSError, subprocess.SubprocessError):
                return None
        root = root.parent
    return None


def install_report():
    """Where this install came from, or None when it cannot be updated in place."""
    return _direct_url() or _checkout_origin()


def _repo_slug(url):
    """owner/name from an https github URL, or None."""
    marker = 'github.com/'
    if marker not in url:
        return None
    tail = url.split(marker, 1)[1].strip('/')
    tail = tail.removesuffix('.git')
    parts = tail.split('/')
    if len(parts) < 2:
        return None
    return '/'.join(parts[:2])


def check(timeout=10):
    """Compare this install's commit with the repository's default branch.

    Returns what the update section shows: the current commit, the upstream
    head, how many commits behind, and the messages of those commits. Only
    the repository is contacted, read-only, unauthenticated for public
    repositories. An install whose origin cannot be named, or a repository
    that cannot be reached, is reported as a reason, never as zero-behind.
    """
    origin = install_report()
    if origin is None:
        return {'available': False,
                'reason': 'This install did not come from a git URL or a checkout, so upstream cannot be compared. Reinstall with pip from the repository to enable updates.'}
    slug = _repo_slug(origin['url'])
    if slug is None:
        return {'available': False, 'reason': f'The install origin {origin["url"]!r} is not a GitHub repository.'}
    branch = origin.get('branch') or 'main'
    api = f'https://api.github.com/repos/{slug}/compare/{origin["commit"]}...{branch}'
    request = urllib.request.Request(api, headers={'Accept': 'application/vnd.github+json',
                                                    'User-Agent': 'tessera-update-check'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except (OSError, ValueError) as error:
        return {'available': False, 'origin': origin, 'slug': slug,
                'reason': f'Upstream could not be reached: {type(error).__name__}.'}
    # The comparison is local...upstream, so upstream-only commits — the ones
    # this install is missing — are always in ahead_by, whether the history is
    # ahead (the normal outdated case) or diverged. behind_by counts the local
    # commits upstream lacks, which is not what "how far behind" means.
    behind = int(data.get('ahead_by') or 0)
    commits = []
    for commit in (data.get('commits') or [])[:30]:
        message = (commit.get('commit') or {}).get('message') or ''
        commits.append({'sha': (commit.get('sha') or '')[:7],
                        'message': message.splitlines()[0] if message else ''})
    return {'available': True, 'origin': origin, 'slug': slug, 'branch': branch,
            'current': origin['commit'], 'behind': behind, 'commits': commits}


def apply(timeout=600):
    """Reinstall from the same git URL, so the running home keeps its settings.

    The install is replaced in place with pip; the core is restarted by the
    caller after this returns, so the new code is what serves the next page.
    """
    origin = install_report()
    if origin is None:
        return {'updated': False, 'reason': 'No install origin to update from.'}
    # Reinstall the same ref the user chose, not the repository default: an
    # install from a branch or tag keeps tracking that ref across updates.
    ref = origin.get('branch') or ''
    spec = 'git+' + origin['url'] + ('@' + ref if ref else '')
    command = [sys.executable, '-m', 'pip', 'install', '--force-reinstall', '--no-deps', spec]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        return {'updated': False, 'reason': f'The reinstall could not run: {type(error).__name__}.'}
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or '').strip().splitlines()[-1:] or ['']
        return {'updated': False, 'reason': 'pip reported: ' + tail[0][:200]}
    return {'updated': True, 'reason': 'Reinstalled from ' + origin['url']}
