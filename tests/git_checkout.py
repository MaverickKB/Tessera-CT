"""Skip a check that needs this tree to be its own git checkout.

A release copy is a plain directory with no history. A test that asks git which
files are tracked, or archives HEAD, cannot answer there. It skips with that
reason instead of failing, so the release copy still reports an honest result.
"""

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def require_git_checkout():
    """Skip unless this tree root is the top of its own git checkout."""
    probe = subprocess.run(
        ['git', 'rev-parse', '--show-toplevel'],
        cwd=ROOT, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip('release copy: this tree is not a git checkout')
    top = Path(probe.stdout.strip()).resolve()
    if top != ROOT:
        pytest.skip('release copy: the enclosing git checkout is not this tree')
