"""A built wheel must name the commit it came from.

A wheel with no commit stamp cannot be attached to a sha. The build writes
tessera/_assets/build-stamp.json, and these tests keep that
promise honest in both directions: a checkout resolves a real sha, and an
export with no repository says "unknown" instead of inventing one.
"""

import json
import subprocess

from build_support import source_commit


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def test_a_checkout_stamps_its_own_head(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Fixture base')

    assert source_commit(repo) == git(repo, 'rev-parse', 'HEAD')


def test_a_tree_without_a_repository_says_unknown(tmp_path):
    plain = tmp_path / 'export'
    plain.mkdir()
    (plain / 'base.txt').write_text('export\n')

    # An export carries no .git. Inventing a sha here would be worse than
    # admitting there is none, so 'unknown' is the contract.
    assert source_commit(plain) == 'unknown'


def test_an_explicit_environment_commit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv('TESSERA_BUILD_COMMIT', 'deadbeef')

    assert source_commit(tmp_path) == 'deadbeef'


def test_the_stamp_file_is_valid_json_naming_a_commit(tmp_path, monkeypatch):
    monkeypatch.setenv('TESSERA_BUILD_COMMIT', 'cafebabe')
    stamp = tmp_path / 'build-stamp.json'
    stamp.write_text(json.dumps({'commit': source_commit(tmp_path)}) + '\n')

    assert json.loads(stamp.read_text()) == {'commit': 'cafebabe'}
