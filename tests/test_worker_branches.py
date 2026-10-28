"""A spawned writer gets its own worktree and branch, and its work survives its end."""

from pathlib import Path
import subprocess

import pytest

from test_agent_selection import configured
from fake_backend import FakeBackend
from tessera.backend import Backend
from tessera.cli_worker import WorkerError
from tessera.loop import Session


def make_repo(path):
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'Test'], check=True)
    (path / 'README.md').write_text('base\n')
    subprocess.run(['git', '-C', str(path), 'add', 'README.md'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-q', '-m', 'base'], check=True)
    return path


def codex_config(tmp_path):
    config = configured(tmp_path)
    config.backends['large'].provider_kind = 'cli_worker'
    config.backends['large'].extra = {'cli': {'vendor': 'codex', 'command': 'codex', 'cwd': str(tmp_path),
        'args': ['exec', '--json', '--sandbox', 'read-only', '-']}}
    config.tools.approval = 'deny_risky'
    return config


def work_reports(session):
    reports = []
    for message in session.log.iter():
        if message.kind == 'work':
            reports.append(message)
    return reports


def test_writer_gets_a_private_worktree_with_a_writable_sandbox_under_deny_risky(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / 'repo')
    config = codex_config(tmp_path)
    seen = []
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)

    def chat(backend, messages, tools=None, **kwargs):
        argv = backend.worker._argv()
        cwd = Path(backend.worker.settings.cwd)
        seen.append({'argv': argv, 'cwd': cwd})
        # The worker edits but cannot commit (the native sandbox blocks .git).
        (cwd / 'feature.txt').write_text('done\n')
        return {'content': 'Implemented the feature.', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config, backend=FakeBackend([])) as parent:
        started = parent.agents.spawn('writer', 'Add feature.txt', backend='large', repo=str(repo))
        assert parent.agents.wait(10)

    assert 'worktree' in started
    argv = seen[0]['argv']
    cwd = seen[0]['cwd']
    assert argv[argv.index('--sandbox') + 1] == 'workspace-write'
    assert argv.count('--sandbox') == 1
    assert cwd != repo and cwd.is_dir()
    add_dirs = []
    for index, argument in enumerate(argv):
        if argument == '--add-dir':
            add_dirs.append(argv[index + 1])
    assert str(repo / '.git' / 'objects') in add_dirs
    # The parent's checkout is untouched; the work is a commit on the worker's branch.
    status = subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'], capture_output=True, text=True).stdout
    assert status == ''
    assert not (repo / 'feature.txt').exists()
    report = work_reports(parent)[0]
    assert report.meta['branch'].startswith('tessera-agent/writer-')
    assert '1 commit(s), 1 file(s) changed' in report.text
    shown = subprocess.run(['git', '-C', str(repo), 'show', '--name-only', '--format=%s', report.meta['head']],
                           capture_output=True, text=True).stdout
    assert 'feature.txt' in shown


def test_read_only_worker_gets_no_worktree_and_a_read_only_sandbox(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / 'repo')
    config = codex_config(tmp_path)
    seen = []
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)

    def chat(backend, messages, tools=None, **kwargs):
        seen.append(backend.worker._argv())
        return {'content': 'Read it.', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config, backend=FakeBackend([])) as parent:
        parent.agents.spawn('reader', 'Read README', tools=['read_file'], backend='large', repo=str(repo))
        assert parent.agents.wait(10)
    argv = seen[0]
    assert argv[argv.index('--sandbox') + 1] == 'read-only'
    assert '--add-dir' not in argv
    worktrees = subprocess.run(['git', '-C', str(repo), 'worktree', 'list'], capture_output=True, text=True).stdout
    assert len(worktrees.strip().splitlines()) == 1


def test_a_failed_writer_keeps_its_work_on_its_branch(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / 'repo')
    config = codex_config(tmp_path)
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)

    def chat(backend, messages, tools=None, **kwargs):
        cwd = Path(backend.worker.settings.cwd)
        (cwd / 'half.txt').write_text('partial\n')
        raise WorkerError('CLI worker timed out', 'timeout')

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config, backend=FakeBackend([])) as parent:
        parent.agents.spawn('slow', 'Do a long change', backend='large', repo=str(repo))
        assert parent.agents.wait(10)
    report = work_reports(parent)[0]
    assert report.meta['success'] is False
    assert 'timeout' in report.text
    assert 'committed by the harness' in report.text
    shown = subprocess.run(['git', '-C', str(repo), 'show', '--name-only', '--format=%s', report.meta['head']],
                           capture_output=True, text=True).stdout
    assert 'half.txt' in shown


def test_repo_outside_a_git_checkout_is_refused_before_any_worker_starts(tmp_path):
    config = codex_config(tmp_path)
    plain = tmp_path / 'plain'
    plain.mkdir()
    with Session(config, backend=FakeBackend([])) as parent:
        with pytest.raises(ValueError, match='not inside a git checkout'):
            parent.agents.spawn('writer', 'Edit', backend='large', repo=str(plain))
        assert parent.agents.active() == 0
        assert parent.agents.pool.running == 0


def test_cli_workers_have_no_engine_concurrency_limit_by_default(tmp_path, monkeypatch):
    import threading
    config = codex_config(tmp_path)
    # No advertised lane for a CLI worker means unbounded, which is the ruling.
    # The old default advertised an explicit 0 here; the key is now absent.
    assert config.agents.concurrency_by_kind.get('cli_worker', 0) == 0
    monkeypatch.setattr('tessera.cli_worker.shutil.which', lambda command: command)
    release = threading.Event()

    def chat(backend, messages, tools=None, **kwargs):
        assert release.wait(15)
        return {'content': 'Done', 'tool_calls': []}

    monkeypatch.setattr(Backend, 'chat', chat)
    with Session(config, backend=FakeBackend([])) as parent:
        try:
            for index in range(20):
                parent.agents.spawn(f'cli-{index}', 'Wait', tools=['read_file'], backend='large')
            assert parent.agents.pool.running_by_kind['cli_worker'] == 20
        finally:
            release.set()
            assert parent.agents.wait(15)
