"""Explicit local git worktrees and validated, per-piece gated integration merges."""

import os
from pathlib import Path
import subprocess
import time

from .passive_status import configured_path


class AgentWorktrees:
    def __init__(self, config):
        self.config = config
        self.settings = config.agents
        self.root = configured_path(config.home, self.settings.worktrees_dir)
        self.repo = Path(self.settings.repo_root).expanduser()

    def _git(self, root, *args, check=True):
        hooks = self.root / 'empty-hooks'
        hooks.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run([self.settings.git_command, '-c', 'core.hooksPath=' + str(hooks),
                '-c', 'commit.gpgSign=false', '-C', str(root), *args], capture_output=True,
                text=True, timeout=self.settings.worktree_timeout, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Agent git command unavailable or timed out') from None
        if check and result.returncode:
            raise ValueError('Agent git operation failed; inspect the private worktree')
        return result

    def ensure(self, run, save):
        if not self.settings.worktrees_enabled:
            return
        if self.root.is_symlink() or not self.repo.is_dir():
            raise ValueError('Invalid configured agent worktree root or repository')
        base = self._git(self.repo, 'rev-parse', '--show-toplevel').stdout.strip()
        if Path(base).resolve() != self.repo.resolve():
            raise ValueError('Agent repository must be its git top-level directory')
        if 'workspace' not in run:
            commit = self._git(self.repo, 'rev-parse', 'HEAD').stdout.strip()
            prefix = 'agent-' + run['id']
            run['workspace'] = {'base': commit, 'repo': str(self.repo.resolve()), 'parent': {
                'path': str(self.root / run['id'] / 'parent'), 'branch': prefix + '-integration'}}
            for index, worker in enumerate(run['workers']):
                worker['workspace'] = {'path': str(self.root / run['id'] / str(index)),
                                        'branch': prefix + '-' + str(index), 'base': commit}
            save()  # Intent/identities precede all worktree mutations.
        if run['workspace']['repo'] != str(self.repo.resolve()):
            raise ValueError('Agent repository binding changed')
        workspaces = [run['workspace']['parent'], *[w['workspace'] for w in run['workers']]]
        for workspace in workspaces:
            path = Path(workspace['path'])
            if path.parent.resolve() != (self.root / run['id']).resolve() or path.is_symlink():
                raise ValueError('Agent worktree escaped its configured root')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                self._git(self.repo, 'worktree', 'add', '-b', workspace['branch'], str(path), run['workspace']['base'])
            branch = self._git(path, 'branch', '--show-current').stdout.strip()
            common = self._git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            original = self._git(self.repo, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            if branch != workspace['branch'] or common != original:
                raise ValueError('Agent worktree belongs to another branch or repository')

    def finish(self, run, worker):
        root = Path(worker['workspace']['path'])
        if self._git(root, 'status', '--porcelain').stdout.strip() and self.settings.commit_children:
            changed = self._git(root, 'diff', '--name-only', '-z', 'HEAD').stdout.split('\0')
            changed += self._git(root, 'ls-files', '--others', '--exclude-standard', '-z').stdout.split('\0')
            changed = sorted(set(p for p in changed if p))
            for path in changed:
                if not any(path == scope.rstrip('/') or path.startswith(scope.rstrip('/') + '/') for scope in worker['scope']):
                    raise ValueError('Uncommitted child work is outside its admitted scope')
            proof = {'tests': [p for p in changed if p.startswith('tests/') and p.endswith('.py') and (root / p).is_file()]}
            self.validate(run, root, proof, worker['threadId'] + '-precommit')
            self._git(root, 'add', '--', *changed)
            self._git(root, 'commit', '-m', 'Tessera child implementation ' + worker['threadId'])
            worker['commit_attribution'] = 'model_authored_engine_committed'
        return self.inspect(run, worker)

    def inspect(self, run, worker):
        workspace = worker['workspace']
        root = Path(workspace['path'])
        if self._git(root, 'status', '--porcelain').stdout.strip():
            raise ValueError('Child must commit its work before validation')
        head = self._git(root, 'rev-parse', 'HEAD').stdout.strip()
        base = run['workspace']['base']
        if head == base or self._git(root, 'merge-base', '--is-ancestor', base, head, check=False).returncode:
            raise ValueError('Child needs a new commit descended from its assigned base')
        if self._git(root, 'branch', '--show-current').stdout.strip() != workspace['branch']:
            raise ValueError('Child branch identity changed')
        files = self._git(root, 'diff', '--no-renames', '--name-only', '-z', base, head).stdout.split('\0')
        files = [p for p in files if p]
        for path in files:
            if not any(path == scope.rstrip('/') or path.startswith(scope.rstrip('/') + '/') for scope in worker['scope']):
                raise ValueError('Child commit changes files outside its admitted scope')
        if not files:
            raise ValueError('Child commit contains no implementation changes')
        tests = [p for p in files if p.startswith('tests/') and p.endswith('.py') and (root / p).is_file()]
        return {'commit': head, 'files': files, 'tests': tests}

    def validate(self, run, root, proof, label):
        if not self.settings.validation_commands:
            raise ValueError('Worktree merges require configured validation commands')
        output = self.root / run['id'] / 'evidence'
        output.mkdir(parents=True, exist_ok=True)
        checks = []
        for index, command in enumerate(self.settings.validation_commands):
            argv = []
            for arg in command:
                if arg == '{tests}':
                    if not proof['tests']:
                        raise ValueError('Changed child tests are required by the validation contract')
                    argv.extend(proof['tests'])
                else:
                    argv.append(arg)
            timeout = min(self.settings.validation_timeout, run['deadline'] - time.time())
            if timeout <= 0:
                raise ValueError('Agent validation wall budget exhausted')
            with (output / (label + '-' + str(index) + '.stdout')).open('wb') as stdout, (output / (label + '-' + str(index) + '.stderr')).open('wb') as stderr:
                try:
                    result = subprocess.run(argv, cwd=root, stdout=stdout, stderr=stderr, timeout=timeout,
                        env={**os.environ, 'PYTHONPATH': str(root), 'GIT_TERMINAL_PROMPT': '0'})
                except (OSError, subprocess.TimeoutExpired):
                    raise ValueError('Agent validation unavailable or timed out') from None
            checks.append({'command_ordinal': index + 1, 'exit_code': result.returncode})
            if result.returncode:
                raise ValueError('Agent validation failed; no merge was committed')
        return checks

    def land(self, run, worker, judge):
        target = Path(run['workspace']['parent']['path'])
        previous = worker.get('landing')
        if previous and previous['status'] == 'merged':
            if self._git(target, 'merge-base', '--is-ancestor', previous['commit'], 'HEAD', check=False).returncode:
                raise ValueError('Recorded integration commit no longer matches its branch')
            return previous
        proof = self.inspect(run, worker)
        pinned = worker.get('implementation')
        if pinned is not None and pinned != proof:
            raise ValueError('Child implementation changed after its result was recorded')
        child_checks = self.validate(run, Path(worker['workspace']['path']), proof, worker['threadId'] + '-child')
        if self._git(target, 'status', '--porcelain').stdout.strip():
            raise ValueError('Integration worktree is dirty or interrupted; inspect before continuing')
        merged = self._git(target, 'merge', '--no-ff', '--no-commit', proof['commit'], check=False)
        if merged.returncode:
            self._git(target, 'merge', '--abort', check=False)
            raise ValueError('Integration conflict; no merge was committed')
        try:
            target_checks = self.validate(run, target, proof, worker['threadId'] + '-integration')
            diff = self._git(target, 'diff', '--cached', '--no-ext-diff', '--no-textconv').stdout
            if len(diff.encode()) > self.config.swarm.max_artifact_bytes:
                raise ValueError('Candidate diff exceeds the configured artifact bound')
            checks = {'child_tests_passed': bool(child_checks), 'integration_tests_passed': bool(target_checks),
                      'scope_verified': True, 'committed_child': True}
            gate = judge(proof, diff, checks)
            if not gate['allowed']:
                self._git(target, 'merge', '--abort')
                return {'status': 'blocked', 'child_commit': proof['commit'], 'gate': gate,
                        'child_checks': child_checks, 'integration_checks': target_checks}
            self._git(target, 'commit', '-m', 'Tessera gated merge ' + worker['threadId'])
            receipt = {'status': 'merged', 'child_commit': proof['commit'], 'gate': gate,
                       'commit': self._git(target, 'rev-parse', 'HEAD').stdout.strip(),
                       'child_checks': child_checks, 'integration_checks': target_checks}
            worker['landing'] = receipt
            return receipt
        except BaseException:
            self._git(target, 'merge', '--abort', check=False)
            raise
