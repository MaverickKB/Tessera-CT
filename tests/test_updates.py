"""Check and Apply must validate and follow the same origin branch."""

import subprocess

import pytest

from tessera import updates


def git(root, *args):
    result = subprocess.run(
        ['git', '-C', str(root), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit(root, message):
    (root / 'history.txt').write_text(message + '\n', encoding='utf-8')
    git(root, 'add', 'history.txt')
    git(root, '-c', 'user.name=Update Test', '-c', 'user.email=updates@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    # Keep personal Git settings, signing, and hooks out of these local repos.
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    origin = tmp_path / 'origin.git'
    source = tmp_path / 'source'
    clone = tmp_path / 'clone'
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(origin))
    git(tmp_path, 'init', '--initial-branch=main', str(source))
    commit(source, 'initial')
    git(source, 'remote', 'add', 'origin', str(origin))
    git(source, 'push', '-u', 'origin', 'main')
    git(tmp_path, 'clone', str(origin), str(clone))

    def install_report():
        return {
            'kind': 'checkout',
            'url': 'https://github.com/example/repo',
            'commit': git(clone, 'rev-parse', 'HEAD'),
            'branch': git(clone, 'branch', '--show-current'),
            'root': str(clone),
        }

    monkeypatch.setattr(updates, 'install_report', install_report)
    return origin, source, clone


def track_deleted_feature(source, clone):
    git(source, 'branch', 'feature')
    git(source, 'push', 'origin', 'feature')
    git(clone, 'fetch', 'origin')
    git(clone, 'checkout', '-b', 'work', '--track', 'origin/feature')
    git(source, 'push', 'origin', '--delete', 'feature')
    # Leave the stale tracking ref in the clone to reproduce the bug.
    assert git(clone, 'rev-parse', '--abbrev-ref', '@{u}') == 'origin/feature'


def test_deleted_upstream_checks_and_applies_default_branch(checkout):
    origin, source, clone = checkout
    track_deleted_feature(source, clone)
    latest = commit(source, 'new main commit')
    git(source, 'push', 'origin', 'main')

    report = updates.check()
    assert report['available'] is True
    assert report['branch'] == 'main'
    assert report['behind'] == 1
    assert report['commits'][0]['sha'] == latest[:7]

    result = updates.apply()
    assert result['updated'] is True, result
    assert git(clone, 'rev-parse', 'HEAD') == latest
    assert git(clone, 'branch', '--show-current') == 'work'


def test_differently_named_upstream_checks_and_applies_release(checkout):
    origin, source, clone = checkout
    git(source, 'checkout', '-b', 'release')
    git(source, 'push', '-u', 'origin', 'release')
    git(clone, 'fetch', 'origin')
    git(clone, 'checkout', '-b', 'work', '--track', 'origin/release')
    latest = commit(source, 'new release commit')
    git(source, 'push', 'origin', 'release')

    report = updates.check()
    assert report['available'] is True
    assert report['branch'] == 'release'
    assert report['behind'] == 1
    assert report['commits'][0]['sha'] == latest[:7]

    result = updates.apply()
    assert result['updated'] is True, result
    assert git(clone, 'rev-parse', 'HEAD') == latest
    assert git(clone, 'branch', '--show-current') == 'work'


def test_deleted_upstream_without_default_fails_closed(checkout):
    origin, source, clone = checkout
    track_deleted_feature(source, clone)
    git(clone, 'symbolic-ref', '--delete', 'refs/remotes/origin/HEAD')
    before = git(clone, 'rev-parse', 'HEAD')

    report = updates.check()
    result = updates.apply()

    assert report['available'] is False
    assert result['updated'] is False
    assert report['reason']
    assert result['reason'] == report['reason']
    assert 'branch' not in report
    assert 'branch' not in result
    assert git(clone, 'rev-parse', 'HEAD') == before


def test_diverged_checkout_preserves_local_work_and_reports_failure(checkout):
    origin, source, clone = checkout
    before = commit(clone, 'local work')
    commit(source, 'upstream work')
    git(source, 'push', 'origin', 'main')

    assert updates.check()['behind'] == 1
    result = updates.apply()

    assert result['updated'] is False
    assert 'by hand' in result['reason']
    assert git(clone, 'rev-parse', 'HEAD') == before
    assert (clone / 'history.txt').read_text() == 'local work\n'


@pytest.mark.parametrize('code', [0, 1])
def test_git_install_reinstalls_with_its_own_interpreter_and_ref(monkeypatch, code):
    from types import SimpleNamespace
    import sys

    monkeypatch.setattr(updates, 'install_report', lambda: {
        'kind': 'git-install', 'url': 'https://github.com/example/repo',
        'branch': 'release', 'commit': 'old',
    })
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['stdin'] == subprocess.DEVNULL
        assert kwargs['env']['GIT_TERMINAL_PROMPT'] == '0'
        return SimpleNamespace(returncode=code, stderr='installation refused', stdout='')

    monkeypatch.setattr(updates.subprocess, 'run', run)
    result = updates.apply()
    assert calls == [[sys.executable, '-m', 'pip', 'install', '--force-reinstall',
                      '--no-deps', 'git+https://github.com/example/repo@release']]
    assert result['updated'] is (code == 0)
    if code:
        assert 'installation refused' in result['reason']


def test_checkout_origin_subprocesses_do_not_borrow_the_owner_lifetime_pipe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / '.git').mkdir()
    package = tmp_path / 'tessera'
    package.mkdir()
    monkeypatch.setattr(updates, '__file__', str(package / 'updates.py'))
    replies = iter(['https://github.com/example/repo.git', 'fixture-commit', 'main'])
    calls = []
    def run(command, **kwargs):
        assert kwargs['stdin'] == subprocess.DEVNULL
        calls.append(command)
        return SimpleNamespace(stdout=next(replies), returncode=0)
    monkeypatch.setattr(updates.subprocess, 'run', run)
    report = updates._checkout_origin()
    assert report['commit'] == 'fixture-commit'
    assert report['branch'] == 'main'
    assert len(calls) == 3


def test_git_and_github_auth_subprocesses_use_no_interactive_stdin(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.delenv('GH_TOKEN', raising=False)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    calls = []
    def run(command, **kwargs):
        assert kwargs['stdin'] == subprocess.DEVNULL
        calls.append(command)
        return SimpleNamespace(stdout='fixture-token', returncode=0)
    monkeypatch.setattr(updates.subprocess, 'run', run)
    updates._git('fixture-root', 'fetch', 'origin', timeout=10)
    assert updates._token() == 'fixture-token'
    assert calls == [['git', '-C', 'fixture-root', 'fetch', 'origin'], ['gh', 'auth', 'token']]


def test_git_install_check_compares_the_installed_commit_to_its_requested_ref(monkeypatch):
    import io
    import json

    monkeypatch.setattr(updates, 'install_report', lambda: {
        'kind': 'git-install', 'url': 'https://github.com/example/repo',
        'branch': 'release', 'commit': 'installed-commit',
    })
    monkeypatch.setattr(updates, '_token', lambda: '')
    requests = []

    def compare(request, **kwargs):
        requests.append(request.full_url)
        return io.BytesIO(json.dumps({
            'ahead_by': 3, 'behind_by': 9,
            'commits': [{'sha': '123456789', 'commit': {'message': 'A fix\nDetails'}}],
        }).encode())

    monkeypatch.setattr(updates.urllib.request, 'urlopen', compare)
    result = updates.check()
    assert requests == ['https://api.github.com/repos/example/repo/compare/installed-commit...release']
    assert result['available'] is True and result['behind'] == 3
    assert result['commits'] == [{'sha': '1234567', 'message': 'A fix'}]
