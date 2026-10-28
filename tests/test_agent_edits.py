"""Structured model edits stay exact, scoped and confined to owned worktrees."""

import json
from pathlib import Path

import pytest

from test_agent_worktrees import setup
from tessera.agent_worktrees import AgentWorktrees
from tessera.agent_edits import apply_edits, edit_prompt


def test_context_and_exact_model_edit_create_real_scoped_code(tmp_path):
    cfg, repo, run = setup(tmp_path)
    cfg.agents.worktree_context_files = ['base.txt']
    helper = AgentWorktrees(cfg)
    helper.ensure(run, lambda: None)
    worker = run['workers'][0]
    prompt = edit_prompt(cfg, worker)
    assert 'base' in prompt and 'piece.py' in prompt
    apply_edits(cfg, worker, json.dumps({'edits': [{'path': 'piece.py', 'old': '', 'new': 'VALUE = 3\n'}]}))
    assert (Path(worker['workspace']['path']) / 'piece.py').read_text() == 'VALUE = 3\n'
    assert not (repo / 'piece.py').exists()


@pytest.mark.parametrize('case', ['outside', 'absolute', 'mismatch', 'duplicate', 'symlink'])
def test_invalid_model_edit_leaves_owned_source_untouched(tmp_path, case):
    cfg, repo, run = setup(tmp_path)
    AgentWorktrees(cfg).ensure(run, lambda: None)
    worker = run['workers'][0]
    root = Path(worker['workspace']['path'])
    edit = {'path': 'piece.py', 'old': '', 'new': 'VALUE = 3\n'}
    if case == 'outside':edit['path'] = '../outside.py'
    elif case == 'absolute':edit['path'] = str(tmp_path / 'outside.py')
    elif case == 'mismatch':edit['old'] = 'does not exist'
    elif case == 'symlink':(root / 'piece.py').symlink_to(repo / 'base.txt')
    edits = [edit, edit] if case == 'duplicate' else [edit]
    with pytest.raises(ValueError):
        apply_edits(cfg, worker, json.dumps({'edits': edits}))
    assert (repo / 'base.txt').read_text() == 'base\n'
    if not (root / 'piece.py').is_symlink():assert not (root / 'piece.py').exists()
