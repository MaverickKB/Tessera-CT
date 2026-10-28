"""A worker's worktree must never be rooted in an ancestor repository.

A session whose workspace is a subdirectory of a git checkout must not spawn
writers from that ancestor repo. The reports would name a base that does not
exist in the project the session is working in, and the edits would land away
from the intended tree.

The contract is that the parent's workspace is used when it is a git checkout.
Walking up to an enclosing repo is a silent guess, so the spawn refuses and
asks for repo=.
"""

import subprocess

import pytest

from tessera.agents import AgentManager


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def make_repo(path):
    path.mkdir()
    git(path, 'init', '-q')
    git(path, 'config', 'user.name', 'Fixture')
    git(path, 'config', 'user.email', 'fixture@example.invalid')
    (path / 'base.txt').write_text('base\n')
    git(path, 'add', '.')
    git(path, 'commit', '-qm', 'Fixture base')
    return path


class Workspace:
    def __init__(self, path):
        self.path = str(path)


class Session:
    """The minimum an AgentManager reads when deciding where to root a worker."""

    def __init__(self, workspace, config, depth=0):
        self.workspace = Workspace(workspace)
        self.config = config
        self.depth = depth
        self._closed = False


class Config:
    class agents:
        worktree_timeout = 30
        worktrees_dir = 'spawn'

    home = None


def manager_for(workspace, home):
    config = Config()
    config.home = str(home)
    return AgentManager(Session(workspace, config))


def test_writer_in_a_subdirectory_of_a_checkout_is_refused(tmp_path):
    """The parent's workspace is inside a repo but is not its root -> refuse."""
    outer = make_repo(tmp_path / 'outer')
    inner = outer / 'some' / 'project'
    inner.mkdir(parents=True)
    (inner / 'work.txt').write_text('work\n')

    manager = manager_for(inner, tmp_path / 'home')

    # This is the measured failure: worktrees were created from `outer`.
    with pytest.raises(ValueError) as caught:
        manager._writer_branch('probe', tools={'shell'}, ceiling={'shell'}, repo=None)

    message = str(caught.value)
    assert 'repo=' in message
    assert 'not its root' in message


def test_writer_at_the_root_of_a_checkout_still_gets_a_worktree(tmp_path):
    """The documented fallback is intact: workspace IS the checkout root."""
    repo = make_repo(tmp_path / 'source')

    manager = manager_for(repo, tmp_path / 'home')
    branch = manager._writer_branch('probe', tools={'shell'}, ceiling={'shell'}, repo=None)

    assert branch is not None
    assert branch.repo == repo.resolve()
    assert branch.path.exists()
    assert git(branch.path, 'rev-parse', 'HEAD') == git(repo, 'rev-parse', 'HEAD')


def test_writer_in_a_plain_directory_outside_git_gets_no_worktree(tmp_path):
    """No enclosing repo at all keeps the existing behaviour: no worktree."""
    plain = tmp_path / 'plain'
    plain.mkdir()

    manager = manager_for(plain, tmp_path / 'home')

    assert manager._writer_branch('probe', tools={'shell'}, ceiling={'shell'}, repo=None) is None


def test_explicit_repo_still_names_the_checkout(tmp_path):
    """repo= is the escape hatch the refusal message points at."""
    repo = make_repo(tmp_path / 'source')
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()

    manager = manager_for(elsewhere, tmp_path / 'home')
    branch = manager._writer_branch('probe', tools={'shell'}, ceiling={'shell'}, repo=str(repo))

    assert branch is not None
    assert branch.repo == repo.resolve()
