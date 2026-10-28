"""Independent local-git consumers of self-build isolation and landing gates."""

from pathlib import Path
import json
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from tessera.agent_worktrees import AgentWorktrees
from tessera.agent_edits import apply_edits
from tessera.config import Config


def git(root, *arguments):
    command = ['git', '-C', str(root), *arguments]
    return subprocess.check_output(command, text=True).strip()


@pytest.fixture(autouse=True)
def no_external_provider(monkeypatch):
    guards = []
    for target in ('urllib.request.urlopen', 'urllib.request.OpenerDirector.open',
                   'tessera.credentials.CredentialResolver.resolve', 'tessera.backend.Backend.chat'):
        guard = Mock(side_effect=AssertionError('Local worktree checks cannot call a provider'))
        monkeypatch.setattr(target, guard)
        guards.append(guard)
    yield
    for guard in guards:
        guard.assert_not_called()


def workspace_fixture(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    git(source, 'init', '-q', '--template=')
    git(source, 'config', 'user.name', 'Fixture')
    git(source, 'config', 'user.email', 'fixture@example.invalid')
    (source / 'base.txt').write_text('Synthetic base\n')
    git(source, 'add', 'base.txt')
    git(source, 'commit', '-qm', 'Synthetic base')
    (source / 'operator.txt').write_text('Preserve uncommitted fixture work\n')
    config = Config(home=tmp_path / 'home', cwd=source)
    config.agents.worktrees_enabled = True
    config.agents.repo_root = str(source)
    config.agents.validation_commands = [[sys.executable, '-c', 'pass']]
    run = {'id': 'e' * 32, 'deadline': time.time() + 60,
           'workers': [{'threadId': 'fixture-child', 'scope': ['piece.py', 'tests'],
                        'objective': 'Implement synthetic piece', 'artifact': None}]}
    manager = AgentWorktrees(config)
    manager.ensure(run, lambda: None)
    worker = run['workers'][0]
    child = Path(worker['workspace']['path'])
    parent = Path(run['workspace']['parent']['path'])
    return config, source, run, manager, worker, child, parent


def commit_fixture(child, *, tests=True):
    (child / 'piece.py').write_text('VALUE = 42\n')
    if tests:
        (child / 'tests').mkdir()
        (child / 'tests' / 'test_piece.py').write_text('from piece import VALUE\n\ndef test_value():\n    assert VALUE == 42\n')
    git(child, 'add', '.')
    git(child, 'commit', '-qm', 'Synthetic child implementation')


def test_disabled_worktree_ensure_is_inert_before_any_process_or_state(tmp_path, monkeypatch):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    forbidden = Mock(side_effect=AssertionError('Disabled worktrees must not start git'))
    monkeypatch.setattr('subprocess.run', forbidden)
    save = Mock(side_effect=AssertionError('Disabled worktrees must not save intent'))
    manager = AgentWorktrees(config)
    run = {'id': 'e' * 32, 'workers': []}
    manager.ensure(run, save)
    assert run == {'id': 'e' * 32, 'workers': []}
    assert not config.home.exists()
    forbidden.assert_not_called()
    save.assert_not_called()


def test_prospective_integration_test_failure_rolls_back_without_judging(tmp_path):
    config, source, run, manager, worker, child, parent = workspace_fixture(tmp_path)
    commit_fixture(child)
    # The same validation passes in the child and fails in the prospective
    # integration tree. Passing child tests alone must never authorize landing.
    counter = tmp_path / 'validation-count.txt'
    script = '''from pathlib import Path
import sys
counter = Path(sys.argv[1])
count = 1
if counter.exists():
    count = int(counter.read_text()) + 1
counter.write_text(str(count))
if count > 1:
    raise SystemExit(9)
'''
    config.agents.validation_commands = [[sys.executable, '-c', script, str(counter)]]
    judge = Mock(side_effect=AssertionError('Failing integration tests must precede Jev'))
    before = git(parent, 'rev-parse', 'HEAD')
    source_head = git(source, 'rev-parse', 'HEAD')
    source_status = git(source, 'status', '--porcelain')
    with pytest.raises(ValueError, match='validation'):
        manager.land(run, worker, judge)
    assert counter.read_text() == '2'
    assert git(parent, 'rev-parse', 'HEAD') == before
    assert not git(parent, 'status', '--porcelain')
    assert not (parent / 'piece.py').exists()
    assert git(source, 'rev-parse', 'HEAD') == source_head
    assert git(source, 'status', '--porcelain') == source_status
    assert (source / 'operator.txt').read_text() == 'Preserve uncommitted fixture work\n'
    assert not worker.get('landing')
    judge.assert_not_called()


def test_required_changed_tests_cannot_be_replaced_by_an_empty_selection(tmp_path):
    config, source, run, manager, worker, child, parent = workspace_fixture(tmp_path)
    commit_fixture(child, tests=False)
    config.agents.validation_commands = [[sys.executable, '-m', 'pytest', '-q', '{tests}']]
    judge = Mock(side_effect=AssertionError('Missing required child tests cannot reach Jev'))
    before = git(parent, 'rev-parse', 'HEAD')
    with pytest.raises(ValueError, match='tests'):
        manager.land(run, worker, judge)
    assert git(parent, 'rev-parse', 'HEAD') == before
    assert not git(parent, 'status', '--porcelain')
    assert not worker.get('landing')
    judge.assert_not_called()


def test_saved_child_branch_rebinding_rejects_without_repair_or_validation(tmp_path):
    config, source, run, manager, worker, child, parent = workspace_fixture(tmp_path)
    commit_fixture(child)
    git(child, 'checkout', '-qb', 'fixture-rebound')
    source_status = git(source, 'status', '--porcelain')
    child_head = git(child, 'rev-parse', 'HEAD')
    parent_head = git(parent, 'rev-parse', 'HEAD')
    save = Mock(side_effect=AssertionError('Rebinding must not repair saved worktree identities'))
    with pytest.raises(ValueError, match='branch'):
        manager.ensure(run, save)
    assert git(child, 'branch', '--show-current') == 'fixture-rebound'
    assert git(child, 'rev-parse', 'HEAD') == child_head
    assert git(parent, 'rev-parse', 'HEAD') == parent_head
    assert git(source, 'status', '--porcelain') == source_status
    save.assert_not_called()


def test_invalid_later_model_edit_cannot_partially_apply_an_earlier_valid_edit(tmp_path):
    root = tmp_path / 'child'
    root.mkdir()
    first = root / 'first.py'
    second = root / 'second.py'
    first.write_text('FIRST = 1\n')
    second.write_text('SECOND = 2\n')
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    config = Config(home=tmp_path / 'home', cwd=root)
    worker = {'scope': ['first.py', 'second.py'], 'workspace': {'path': str(root)}}
    packet = {'edits': [
        {'path': 'first.py', 'old': 'FIRST = 1', 'new': 'FIRST = 42'},
        {'path': 'second.py', 'old': 'A stale anchor', 'new': 'SECOND = 49'},
    ]}
    with pytest.raises(ValueError, match='anchor'):
        apply_edits(config, worker, json.dumps(packet))
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    assert not config.home.exists()
