"""A private git worktree and branch for one spawned writer.

A writer gets its own checkout, so two workers never share an index or edit
the parent's tree. The native CLI sandbox can write inside the worktree but not
the repository's .git directory, so the worker may be unable to commit. When the
worker ends, for any reason, anything it left uncommitted is committed here so
the work survives and the parent can integrate the branch.
"""

import os
from pathlib import Path
import re
import subprocess
import uuid


def _git(root, timeout, *args, check=True):
    environment = dict(os.environ)
    environment['GIT_TERMINAL_PROMPT'] = '0'
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                                timeout=timeout, env=environment)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f'git {args[0]} could not run: {exc}') from None
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f'git {args[0]} failed: {detail[-400:]}')
    return result


def repository_root(path, timeout):
    """The top-level directory of the git checkout containing path, or None."""
    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        return None
    try:
        result = _git(candidate, timeout, 'rev-parse', '--show-toplevel', check=False)
    except ValueError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    if not top:
        return None
    return Path(top).resolve()


class WorkerBranch:
    def __init__(self, repo, path, branch, base, timeout):
        self.timeout = timeout
        self.repo = Path(repo)
        self.path = Path(path)
        self.branch = branch
        self.base = base

    @classmethod
    def create(cls, repo, worktrees_root, worker_name, timeout):
        """Add a worktree on a new branch from the repository's current HEAD."""
        token = uuid.uuid4().hex[:8]
        safe_name = re.sub(r'[^A-Za-z0-9_-]', '-', worker_name)[:48]
        branch = f'tessera-agent/{safe_name}-{token}'
        root = Path(worktrees_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f'{safe_name}-{token}'
        base = _git(repo, timeout, 'rev-parse', 'HEAD').stdout.strip()
        _git(repo, timeout, 'worktree', 'add', '-b', branch, str(path), base)
        return cls(repo, path, branch, base, timeout)

    def writable_git_paths(self):
        """The repository git paths a commit from this worktree writes to.

        A native sandbox refuses a writable root named .git, but accepts these
        subdirectories. Granting them lets the worker commit its own branch.
        """
        common = Path(_git(self.path, self.timeout, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
        private = Path(_git(self.path, self.timeout, 'rev-parse', '--path-format=absolute', '--git-dir').stdout.strip())
        paths = [str(private)]
        for name in ('objects', 'refs', 'logs'):
            candidate = common / name
            if candidate.is_dir():
                paths.append(str(candidate))
        return paths

    def task_preface(self):
        return ('You are working in a private git worktree created for you.\n'
                f'Worktree: {self.path}\nBranch: {self.branch}\nBase commit: {self.base}\n'
                'Make your edits inside this worktree only. Commit when your change is complete '
                'and verified. If git cannot write (a native sandbox may block the repository '
                '.git directory), leave the changes in place: the harness commits everything left '
                'in this worktree when you finish, and the parent integrates the branch.\n\n')

    def commit_leftovers(self, worker_name):
        """Commit uncommitted work so it survives. Returns a short note, or '' when clean."""
        status = _git(self.path, self.timeout, 'status', '--porcelain').stdout
        if not status.strip():
            return ''
        _git(self.path, self.timeout, 'add', '-A')
        message = (f'Work left uncommitted by agent {worker_name}\n\n'
                   'Committed by the harness when the agent ended. Repository hooks were not run; '
                   'verify before merging.')
        # --no-verify: this commit preserves the work; verification belongs to the merge.
        _git(self.path, self.timeout, 'commit', '--no-verify', '-q', '-m', message)
        return 'uncommitted work was committed by the harness (hooks not run)'

    def discard(self):
        """Remove an unused worktree and its branch (the worker never started)."""
        _git(self.repo, self.timeout, 'worktree', 'remove', '--force', str(self.path), check=False)
        _git(self.repo, self.timeout, 'branch', '-D', self.branch, check=False)

    def summary(self):
        head = _git(self.path, self.timeout, 'rev-parse', 'HEAD').stdout.strip()
        count_text = _git(self.path, self.timeout, 'rev-list', '--count', f'{self.base}..{head}').stdout.strip()
        files_text = _git(self.path, self.timeout, 'diff', '--name-only', self.base, head).stdout
        changed = []
        for line in files_text.splitlines():
            if line.strip():
                changed.append(line.strip())
        return {'worktree': str(self.path), 'branch': self.branch, 'base': self.base, 'head': head,
                'commits': int(count_text or 0), 'files': changed}
