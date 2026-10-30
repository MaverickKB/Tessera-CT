"""Self-update for a running install: report, upstream check, apply.

An installed Tessera knows where it came from. pip records the git URL and
exact commit in the package's dist-info `direct_url.json`, and a checkout
carries its own `.git`. This module reads that record, asks the repository
how far ahead upstream is, and reinstalls from the same URL on request.

The check touches the network only when asked, and only the repository:
no telemetry, no version server, no download until the operator clicks
update. What changed comes from the repository's own compare API, the same
information `git log upstream` would show.

A checkout resolves, fetches, and validates one update branch through a
single resolver that Check and Apply both use, so Check cannot advertise a
branch that Apply will not pull.
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
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                commit = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                branch = subprocess.run(['git', '-C', str(root), 'rev-parse', '--abbrev-ref', 'HEAD'],
                                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                url = (remote.stdout or '').strip()
                if url.endswith('.git'):
                    url = url[:-4]
                url = url.replace(':', '/').replace('git@', 'https://') if url.startswith('git@') else url
                return {'url': url, 'commit': (commit.stdout or '').strip(),
                        'branch': (branch.stdout or '').strip() or 'main', 'kind': 'checkout',
                        'root': str(root)}
            except (OSError, subprocess.SubprocessError):
                return None
        root = root.parent
    return None


def install_report():
    """Where this install came from, or None when it cannot be updated in place."""
    return _direct_url() or _checkout_origin()


def restart_process(config, command, port):
    """Replace an owned core with fresh imports, retaining its home and origin."""
    if command in ('desktop', 'serve'):
        from .desktop import owned_worker, _RESTART_EXIT
        if owned_worker():
            # The persistent owner retains the native shell's monitored handle.
            # Exit all worker threads after the bounded drain; the owner then
            # launches fresh imports at the original ready-record address.
            os._exit(_RESTART_EXIT)
    os.execv(sys.executable, [
        sys.executable, '-m', 'tessera', '--home', str(config.home), command,
        '--host', config.server.host, '--port', str(port),
    ])


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


def _token():
    """A GitHub token for a private repository, if this machine has one.

    The unauthenticated compare API answers 404 for a private repository, which
    is why Check said "Upstream could not be reached" on an install whose
    remote was fine. `gh auth token` is the same credential git already uses.
    """
    for name in ('GH_TOKEN', 'GITHUB_TOKEN'):
        value = os.environ.get(name)
        if value:
            return value
    try:
        result = subprocess.run(['gh', 'auth', 'token'], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (result.stdout or '').strip() if result.returncode == 0 else ''


def _git(root, *args, timeout):
    # A desktop core's stdin is its native owner's lifetime pipe. Updates are
    # HTTP transactions and cannot answer prompts or borrow that shared handle.
    return subprocess.run(['git', '-C', root, *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)


def _resolve_update_branch(root, fallback, timeout):
    """Fetch and validate the same update branch for Check and Apply.

    A deleted upstream can leave a stale origin ref in the checkout. Both
    operations must fall back together, rather than Check advertising the
    default branch while Apply still tries to pull the deleted upstream.
    """
    if not root:
        return None, 'The checkout has no root to resolve an update branch.'
    try:
        tracking = _git(root, 'rev-parse', '--abbrev-ref', '--symbolic-full-name',
                        '@{u}', timeout=timeout)
        tracked_name = (tracking.stdout or '').strip()
        head = _git(root, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD',
                    timeout=timeout)
        default_name = (head.stdout or '').strip()
    except (OSError, subprocess.SubprocessError) as error:
        return None, f'The checkout could not resolve an update branch: {type(error).__name__}.'

    default_branch = None
    if head.returncode == 0 and default_name.startswith('origin/'):
        default_branch = default_name[len('origin/'):]
    if tracking.returncode == 0 and tracked_name.startswith('origin/'):
        branch = tracked_name[len('origin/'):]
    elif default_branch:
        branch = default_branch
    else:
        branch = fallback

    candidates = [branch]
    if default_branch and default_branch != branch:
        candidates.append(default_branch)
    reason = 'The checkout has no valid update branch on origin.'
    for candidate in candidates:
        remote_ref = 'refs/remotes/origin/' + candidate
        # An explicit destination also updates the ref with a narrow fetch config.
        refspec = '+refs/heads/' + candidate + ':' + remote_ref
        try:
            fetch = _git(root, 'fetch', '--quiet', 'origin', refspec, timeout=timeout)
            if fetch.returncode != 0:
                errors = (fetch.stderr or '').strip().splitlines()
                detail = 'git fetch failed'
                if errors:
                    detail = errors[-1][:200]
                reason = 'The checkout could not fetch an update branch from origin: ' + detail
                continue
            verified = _git(root, 'show-ref', '--verify', '--quiet', remote_ref,
                            timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            reason = f'The checkout could not validate an update branch: {type(error).__name__}.'
            continue
        if verified.returncode != 0:
            reason = 'The checkout could not verify ' + remote_ref + ' after fetching.'
            continue
        return candidate, None
    return None, reason


def _checkout_check(origin, timeout):
    """Ask the checkout's own git, which already holds the credentials."""
    root = origin.get('root') or ''
    fallback = origin.get('branch') or 'main'
    branch, reason = _resolve_update_branch(root, fallback, timeout)
    if branch is None:
        return {'available': False, 'origin': origin, 'slug': _repo_slug(origin['url']),
                'reason': reason}
    remote = 'refs/remotes/origin/' + branch
    try:
        count = _git(root, 'rev-list', '--count', 'HEAD..' + remote, timeout=timeout)
        log = _git(root, 'log', '--oneline', '--no-decorate', '-n', '30', 'HEAD..' + remote, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {'available': False, 'origin': origin, 'slug': _repo_slug(origin['url']),
                'reason': 'The checkout could not compare the validated update branch.'}
    if count.returncode != 0 or log.returncode != 0:
        return {'available': False, 'origin': origin, 'slug': _repo_slug(origin['url']),
                'reason': 'The checkout could not compare the validated update branch.'}
    commits = []
    for line in (log.stdout or '').strip().splitlines():
        sha, _space, message = line.partition(' ')
        commits.append({'sha': sha[:7], 'message': message})
    behind = int((count.stdout or '0').strip() or 0)
    return {'available': True, 'origin': origin, 'slug': _repo_slug(origin['url']), 'branch': branch,
            'current': origin['commit'], 'behind': behind, 'commits': commits}


def check(timeout=10):
    """Compare this install's commit with the branch it tracks.

    A checkout asks git, which already holds the credentials the remote needs.
    A pip install from a git URL asks the GitHub compare API, with a token when
    this machine has one: the unauthenticated API cannot see a private
    repository. A repository that cannot be reached is reported as a reason,
    never as zero-behind.
    """
    origin = install_report()
    if origin is None:
        return {'available': False,
                'reason': 'This install did not come from a git URL or a checkout, so upstream cannot be compared. Reinstall with pip from the repository to enable updates.'}
    slug = _repo_slug(origin['url'])
    if origin.get('kind') == 'checkout':
        return _checkout_check(origin, timeout)
    if slug is None:
        return {'available': False, 'reason': f'The install origin {origin["url"]!r} is not a GitHub repository.'}
    branch = origin.get('branch') or 'main'
    api = f'https://api.github.com/repos/{slug}/compare/{origin["commit"]}...{branch}'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'tessera-update-check'}
    token = _token()
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request = urllib.request.Request(api, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {'available': False, 'origin': origin, 'slug': slug,
                    'reason': ('The repository is private, or that commit is not on it. '
                               'Sign the GitHub CLI in on this machine (gh auth login) so Check can read it.')}
        return {'available': False, 'origin': origin, 'slug': slug,
                'reason': f'Upstream refused the check: HTTP {error.code}.'}
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
    """Update in place: fast-forward a checkout, reinstall a pip install.

    A checkout is the tree the running core imports, so it moves with git. A
    pip install from a git URL is replaced with pip. The caller restarts the
    core either way.
    """
    origin = install_report()
    if origin is None:
        return {'updated': False, 'reason': 'No install origin to update from.'}
    if origin.get('kind') == 'checkout':
        root = origin.get('root') or ''
        fallback = origin.get('branch') or 'main'
        branch, reason = _resolve_update_branch(root, fallback, timeout)
        if branch is None:
            return {'updated': False, 'reason': reason}
        try:
            result = _git(root, 'pull', '--ff-only', 'origin', branch, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as error:
            return {'updated': False, 'reason': f'The checkout could not update: {type(error).__name__}.'}
        if result.returncode != 0:
            tail = ((result.stderr or result.stdout or '').strip().splitlines()[-1:] or [''])[0]
            return {'updated': False,
                    'reason': 'git pull reported: ' + tail[:200]
                              + ' The checkout has local commits or changes; update it by hand.'}
        return {'updated': True, 'reason': 'Fast-forwarded ' + root + ' to origin/' + branch}
    # Reinstall the same ref the user chose, not the repository default: an
    # install from a branch or tag keeps tracking that ref across updates.
    ref = origin.get('branch') or ''
    spec = 'git+' + origin['url'] + ('@' + ref if ref else '')
    command = [sys.executable, '-m', 'pip', 'install', '--force-reinstall', '--no-deps', spec]
    env = dict(os.environ)
    env['GIT_TERMINAL_PROMPT'] = '0'
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return {'updated': False, 'reason': f'The reinstall could not run: {type(error).__name__}.'}
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or '').strip().splitlines()[-1:] or ['']
        return {'updated': False, 'reason': 'pip reported: ' + tail[0][:200]}
    return {'updated': True, 'reason': 'Reinstalled from ' + origin['url']}
