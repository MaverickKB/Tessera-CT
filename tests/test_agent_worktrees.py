"""Worktree isolation, validation and gated landing without providers."""

import subprocess
import sys
import time

import pytest

from tessera.config import Config
from tessera.agent_worktrees import AgentWorktrees


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def setup(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.name', 'Fixture')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    (repo / 'base.txt').write_text('base\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'Fixture base')
    (repo / 'dirty.txt').write_text('Preserve operator work\n')
    cfg = Config(home=tmp_path / 'home', cwd=repo)
    cfg.agents.worktrees_enabled = True
    cfg.agents.repo_root = str(repo)
    cfg.agents.validation_commands = [[sys.executable, '-c', 'print("Synthetic validation passed")']]
    run = {'id': 'c' * 32, 'deadline': time.time() + 30, 'workers': [
        {'threadId': 'agent-fixture', 'scope': ['piece.py'], 'objective': 'Implement a fixture', 'artifact': None}]}
    return cfg, repo, run


def commit_piece(cfg, run, name='piece.py'):
    root = __import__('pathlib').Path(run['workers'][0]['workspace']['path'])
    (root / name).write_text('VALUE = 1\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Model fixture implementation')
    return root


def test_worktrees_are_isolated_and_only_a_checked_gate_lands_a_commit(tmp_path):
    cfg, repo, run = setup(tmp_path)
    manager = AgentWorktrees(cfg)
    snapshots = []
    manager.ensure(run, lambda: snapshots.append(True))
    child = commit_piece(cfg, run)
    before = git(repo, 'rev-parse', 'HEAD')
    gates = []
    def judge(proof, diff, checks):
        gates.append((proof, diff, checks))
        assert 'VALUE = 1' in diff and all(checks.values())
        return {'allowed': True, 'source': 'jev'}
    result = manager.land(run, run['workers'][0], judge)
    assert result['status'] == 'merged' and result['gate']['source'] == 'jev'
    assert gates and snapshots
    target = __import__('pathlib').Path(run['workspace']['parent']['path'])
    assert (target / 'piece.py').read_text() == (child / 'piece.py').read_text()
    assert git(repo, 'rev-parse', 'HEAD') == before
    assert not (repo / 'piece.py').exists()
    assert (repo / 'dirty.txt').read_text() == 'Preserve operator work\n'
    assert manager.land(run, run['workers'][0], lambda *a: pytest.fail('Do not judge twice'))['commit'] == result['commit']


@pytest.mark.parametrize('failure', ['scope', 'validation', 'judge'])
def test_scope_failed_validation_and_denial_cannot_land(tmp_path, failure):
    cfg, repo, run = setup(tmp_path)
    if failure == 'validation':
        cfg.agents.validation_commands = [[sys.executable, '-c', 'raise SystemExit(3)']]
    manager = AgentWorktrees(cfg)
    manager.ensure(run, lambda: None)
    commit_piece(cfg, run, 'outside.py' if failure == 'scope' else 'piece.py')
    target = __import__('pathlib').Path(run['workspace']['parent']['path'])
    before = git(target, 'rev-parse', 'HEAD')
    def judge(*args):
        assert failure == 'judge', 'Scope/tests must precede semantic gates'
        return {'allowed': False, 'source': 'jev'}
    if failure == 'judge':
        assert manager.land(run, run['workers'][0], judge)['status'] == 'blocked'
    else:
        with pytest.raises(ValueError):
            manager.land(run, run['workers'][0], judge)
    assert git(target, 'rev-parse', 'HEAD') == before
    assert not git(target, 'status', '--porcelain')


def test_engine_can_validate_and_commit_model_authored_scoped_changes(tmp_path):
    cfg, repo, run = setup(tmp_path)
    cfg.agents.commit_children = True
    manager = AgentWorktrees(cfg)
    manager.ensure(run, lambda: None)
    worker = run['workers'][0]
    root = __import__('pathlib').Path(worker['workspace']['path'])
    (root / 'piece.py').write_text('VALUE = 2\n')
    proof = manager.finish(run, worker)
    assert proof['files'] == ['piece.py'] and not git(root, 'status', '--porcelain')
    assert worker['commit_attribution'] == 'model_authored_engine_committed'
    assert not (repo / 'piece.py').exists()


def test_engine_never_commits_out_of_scope_model_work(tmp_path):
    cfg, repo, run = setup(tmp_path)
    cfg.agents.commit_children = True
    manager = AgentWorktrees(cfg)
    manager.ensure(run, lambda: None)
    worker = run['workers'][0]
    root = __import__('pathlib').Path(worker['workspace']['path'])
    (root / 'outside.py').write_text('VALUE = 2\n')
    before = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(ValueError, match='scope'):
        manager.finish(run, worker)
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not worker.get('commit_attribution')
