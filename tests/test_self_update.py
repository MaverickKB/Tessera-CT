"""A running core must install an update and serve fresh imports at the same URL."""

import json
from importlib import metadata
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from test_updates import git
from tessera import updates
from tessera.config import Config
from tessera.server import create_server


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def update_server(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.voice.enabled = False
    with create_server(config) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_failed_update_keeps_the_current_core_available(update_server, monkeypatch):
    monkeypatch.setattr(updates, 'apply', lambda: {'updated': False, 'reason': 'fetch failed'})
    before = request(update_server.url, '/api/status?shape=connection')['generation']

    result = request(update_server.url, '/api/update/apply', {})

    assert result == {'updated': False, 'reason': 'fetch failed', 'restarting': False}
    assert request(update_server.url, '/api/status?shape=connection')['generation'] == before


def test_overlapping_update_requests_do_not_run_two_installers(update_server, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []

    def install():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {'updated': False, 'reason': 'test refusal'}

    monkeypatch.setattr(updates, 'apply', install)
    first = threading.Thread(target=lambda: results.append(
        request(update_server.url, '/api/update/apply', {})))
    first.start()
    try:
        assert entered.wait(5)
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as refused:
            request(update_server.url, '/api/update/apply', {})
        assert refused.value.code == 409
        assert 'already in progress' in refused.value.read().decode()
        assert len(calls) == 1
    finally:
        release.set()
        first.join(timeout=5)
    assert results and results[0]['updated'] is False


def test_unowned_core_does_not_stop_after_installing_an_update(update_server, monkeypatch):
    monkeypatch.setattr(updates, 'apply', lambda: {'updated': True, 'reason': 'installed'})
    result = request(update_server.url, '/api/update/apply', {})
    assert result['updated'] is True and result['restarting'] is False
    assert request(update_server.url, '/api/status?shape=connection')['generation']


def test_successful_update_restarts_even_if_the_client_disconnects(update_server, monkeypatch):
    import http.client
    from tessera.server import Handler

    update_server.self_supervised = True
    monkeypatch.setattr(updates, 'apply', lambda: {'updated': True, 'reason': 'installed'})
    original = Handler._json

    def disconnected(handler, status, body):
        if body.get('updated'):
            raise BrokenPipeError('browser disconnected')
        return original(handler, status, body)

    monkeypatch.setattr(Handler, '_json', disconnected)
    with pytest.raises((OSError, http.client.RemoteDisconnected)):
        request(update_server.url, '/api/update/apply', {})
    assert update_server.stopping.wait(5)
    assert update_server._restarting


def commit_all(root, message):
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Update Test', '-c', 'user.email=updates@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', message)
    return git(root, 'rev-parse', 'HEAD')


def provision_local_build_tools(python, source):
    """Populate a test venv from trusted installed distributions, without an index.

    ensurepip may bundle setuptools too old for this project's PEP 639 metadata.
    A nested venv also does not inherit its invoking venv's site-packages, so
    --system-site-packages alone cannot supply the already-installed build tools.
    """
    from packaging.requirements import Requirement
    required = tomllib.loads((source / 'pyproject.toml').read_text())['build-system']['requires']
    specifications = [Requirement('pip'), *(Requirement(item) for item in required)]
    destination = Path(subprocess.check_output(
        [str(python), '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
    for specification in specifications:
        distribution = metadata.distribution(specification.name)
        assert distribution.version in specification.specifier, specification
        assert distribution.files, 'The installed build distribution must have a file manifest'
        for entry in distribution.files:
            # Console scripts outside site-packages are unnecessary: this fixture
            # invokes `python -m pip`. Keep every copy within the isolated venv.
            if '..' in entry.parts or entry.is_absolute():
                continue
            installed = distribution.locate_file(entry)
            if not installed.is_file():
                continue
            target = destination / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(installed, target)


def request(url, path, body=None, timeout=5):
    req = Request(url + path, data=None if body is None else json.dumps(body).encode(),
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def ready_url(process, mode):
    lines = Queue()
    def read():
        for line in iter(process.stdout.readline, b''):
            lines.put(line)
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.2)
        except Empty:
            assert process.poll() is None, 'core exited before readiness'
            continue
        assert line is not None, 'core exited before readiness'
        if mode.startswith('desktop') and line.startswith(b'{'):
            return json.loads(line)['url']
        if line.startswith(b'Serving Tessera at '):
            return line.decode().split()[-1]
    pytest.fail('core did not report readiness')


def test_owned_desktop_propagates_worker_startup_failure(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('this is not valid TOML = [')
    script = ('from tessera.config import Config; from tessera.desktop import _run_owned; '
              'from pathlib import Path; import sys; '
              '_run_owned(Config(home=Path(sys.argv[1]), cwd=Path.cwd()))')
    env = {name: value for name, value in os.environ.items() if not name.startswith('TESSERA_')}
    env['PYTHONPATH'] = str(ROOT)
    process = subprocess.Popen([sys.executable, '-u', '-c', script, str(home)], cwd=ROOT, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        # Keep owner stdin open: a real startup failure must exit, not wait for
        # the native shell to close its pipe or retry forever behind its back.
        assert process.wait(timeout=15) == 1
        assert process.stdout.read() == b''
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX subprocess pipe readiness and exec lifecycle')
@pytest.mark.parametrize('mode', ['start', 'serve', 'desktop'])
def test_update_restart_is_bounded_when_a_model_ignores_interruption(tmp_path, mode):
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(
        '[server]\nport = 0\nresume_turns = false\n[voice]\nenabled = false\n')
    script = '''
import runpy, sys, threading
sys.path.insert(0, 'tests')
from fake_backend import FakeBackend
from local_memory import test_local_memory
from tessera import server, updates

original = server.create_server
entered = threading.Event()
def stuck(messages, tools):
    entered.set()
    threading.Event().wait()
def create(config):
    config.server.drain_seconds = .05
    config.server.interrupt_grace_seconds = .05
    core = original(config, backend_factory=lambda *args: FakeBackend([stuck]))
    slug = core.manager.create('update-recovery')['slug']
    core.manager.submit(slug, 'running')
    assert entered.wait(5)
    core.manager.submit(slug, 'queued')
    return core
server.create_server = create
updates.apply = lambda: {'updated': True, 'reason': 'installed'}
sys.argv = ['tessera', *sys.argv[1:]]
with test_local_memory():
    runpy.run_module('tessera', run_name='__main__')
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith('TESSERA_')}
    env['PYTHONPATH'] = str(ROOT)
    command = [sys.executable, '-u', '-c', script, '--home', str(home), mode, '--port', '0']
    if mode == 'start':
        command.append('--no-browser')
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = ready_url(process, mode)
        before = request(url, '/api/status?shape=connection')['generation']
        assert request(url, '/api/update/apply', {})['restarting']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                if request(url, '/api/status?shape=connection')['generation'] != before:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail('the update waited indefinitely for an interrupted model')
        records = [json.loads(line) for line in
                   (home / 'sessions/update-recovery/turns.jsonl').read_text().splitlines()]
        queued = next(row['turn']['id'] for row in records
                      if row['op'] == 'enqueue' and row['turn']['text'] == 'queued')
        assert not any(row.get('id') == queued for row in records), 'queued work must survive the restart'
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX subprocess pipe readiness and exec lifecycle')
def test_git_installed_harness_reinstalls_itself_and_loads_new_code(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    source, origin = tmp_path / 'source', tmp_path / 'origin.git'
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(
        '.git', '__pycache__', '*.pyc', '.pytest_cache', '*.egg-info', 'build', '.venv'))
    git(source, 'init', '--initial-branch=main')
    commit_all(source, 'initial installable harness')
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(origin))
    git(source, 'remote', 'add', 'origin', str(origin))
    git(source, 'push', '-u', 'origin', 'main')
    # pip records the original VCS URL in direct_url.json; Git redirects it
    # to a local bare repository so this test needs no GitHub or index access.
    remote = 'https://github.com/update-test/tessera.git'
    git_config = tmp_path / 'gitconfig'
    git_config.write_text('[url "' + str(origin) + '"]\n    insteadOf = ' + remote + '\n')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('TESSERA_') and key != 'PYTHONPATH'}
    env.update(GIT_CONFIG_GLOBAL=str(git_config), GIT_CONFIG_NOSYSTEM='1',
               PIP_CONFIG_FILE=os.devnull, PIP_NO_INDEX='1', PIP_NO_BUILD_ISOLATION='0')
    venv = tmp_path / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', '--system-site-packages', '--without-pip', str(venv)],
                   check=True, capture_output=True, env=env)
    python = venv / 'bin/python'
    provision_local_build_tools(python, source)
    installed = subprocess.run(
        [str(python), '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', 'git+' + remote + '@main'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('[server]\nport = 0\n[voice]\nenabled = false\n')
    process = subprocess.Popen(
        [str(python), '-u', '-m', 'tessera', '--home', str(home), 'start', '--no-browser', '--port', '0'],
        cwd=tmp_path, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = ready_url(process, 'start')
        before = request(url, '/api/status?shape=connection')['generation']
        server = source / 'tessera/server.py'
        server.write_text(server.read_text().replace(
            'self.generation = uuid.uuid4().hex',
            "self.generation = 'reinstalled-' + uuid.uuid4().hex"))
        latest = commit_all(source, 'updated installed implementation')
        git(source, 'push', 'origin', 'main')
        result = request(url, '/api/update/apply', {}, timeout=60)
        assert result['updated'] and result['restarting'], result
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                status = request(url, '/api/status?shape=connection')
                if status['generation'].startswith('reinstalled-'):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail('the installed harness did not load its newly installed Python code')
        assert status['generation'] != before
        report = subprocess.run(
            [str(python), '-c', 'import json; from tessera.updates import install_report; print(json.dumps(install_report()))'],
            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
        installed_origin = json.loads(report.stdout)
        assert installed_origin['kind'] == 'git-install'
        assert installed_origin['commit'] == latest
        process.terminate()
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize('mode', [
    pytest.param('start', marks=pytest.mark.skipif(os.name == 'nt', reason='POSIX exec lifecycle')),
    pytest.param('serve', marks=pytest.mark.skipif(os.name == 'nt', reason='POSIX exec lifecycle')),
    'desktop',
    pytest.param('desktop-owner', marks=pytest.mark.skipif(os.name == 'nt', reason='Owner is already used by native Windows desktop')),
])
def test_update_installs_and_reexecutes_at_the_same_origin(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    source, origin, clone = (tmp_path / name for name in ('source', 'origin.git', 'clone'))
    source.mkdir()
    for name in ('tessera', 'web', 'seeds', 'docs/packaging'):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copyfile(ROOT / 'config.example.toml', source / 'config.example.toml')
    git(source, 'init', '--initial-branch=main')
    commit_all(source, 'initial runnable harness')
    git(tmp_path, 'init', '--bare', '--initial-branch=main', str(origin))
    git(source, 'remote', 'add', 'origin', str(origin))
    git(source, 'push', '-u', 'origin', 'main')
    git(tmp_path, 'clone', str(origin), str(clone))
    # The complete check/install/restart path runs against a local bare
    # repository. No installer, update result or respawn is mocked.

    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text('[server]\nport = 0\n[voice]\nenabled = false\n')
    sentinel = home / 'keep-me.txt'
    sentinel.write_text('operator data')
    env = {key: value for key, value in os.environ.items() if not key.startswith('TESSERA_')}
    env['PYTHONPATH'] = str(clone)
    command = [sys.executable, '-u', '-m', 'tessera', '--home', str(home), mode, '--port', '0']
    if mode == 'desktop-owner':
        # Exercise the Windows owner strategy with real subprocesses on POSIX too.
        script = ('from tessera.config import load_config; from tessera.desktop import _run_owned; '
                  'import sys; config=load_config(sys.argv[1]); config.server.port=0; _run_owned(config)')
        command = [sys.executable, '-u', '-c', script, str(home)]
    if mode == 'start':
        command.append('--no-browser')
    process = subprocess.Popen(command, cwd=clone, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = ready_url(process, mode)
        before = request(url, '/api/status?shape=connection')['generation']
        # A changed generation alone could be a reconstructed server in the
        # old interpreter. This prefix proves newly imported Python code ran.
        server = source / 'tessera/server.py'
        server.write_text(server.read_text().replace(
            'self.generation = uuid.uuid4().hex',
            "self.generation = 'updated-' + uuid.uuid4().hex"))
        latest = commit_all(source, 'updated Python implementation')
        git(source, 'push', 'origin', 'main')
        report = request(url, '/api/update/check')
        assert report['available'] and report['behind'] == 1, report

        result = request(url, '/api/update/apply', {})
        assert result['updated'] and result['restarting'], result
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.poll() is None, 'an owned core must survive its update'
            try:
                status = request(url, '/api/status?shape=connection')
                if status['generation'].startswith('updated-'):
                    break
            except (OSError, URLError):
                pass
            time.sleep(0.1)
        else:
            pytest.fail('updated Python code never served at the original URL')
        assert status['generation'] != before
        assert git(clone, 'rev-parse', 'HEAD') == latest
        assert request(url, '/api/update/check')['behind'] == 0
        assert sentinel.read_text() == 'operator data'
        if mode.startswith('desktop'):
            process.stdin.close()
            process.stdin = None
        else:
            process.terminate()
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
