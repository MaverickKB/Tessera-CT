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
