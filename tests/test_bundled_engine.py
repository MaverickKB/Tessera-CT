"""Bundled runtime interpreter selection across supported platform layouts."""

import io
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera import bundled_engine


@pytest.mark.parametrize('platform,relative', [
    ('nt', 'Scripts/python.exe'),
    ('posix', 'bin/python'),
])
def test_provision_reuses_platform_interpreter(tmp_path, monkeypatch, platform, relative):
    interpreter = tmp_path / '.venv' / relative
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    monkeypatch.setattr(bundled_engine, '_venv_python',
                        lambda venv: bundled_engine_path(venv, platform))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bundled_engine.subprocess, 'run', run)
    assert bundled_engine._provision(tmp_path) == str(interpreter)
    assert calls == [[str(interpreter), '-c', 'import mcp']]


def test_provision_installs_the_tessera_lock_when_mcp_is_absent(tmp_path, monkeypatch):
    interpreter = tmp_path / '.venv' / 'bin' / 'python'
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    lock = tmp_path / 'aethermind-mcp-requirements.lock'
    lock.write_text('mcp==1.27.1\n', encoding='utf-8')
    monkeypatch.setattr(bundled_engine, 'mcp_requirements_lock', lambda: lock)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1 if '-c' in args else 0, stdout='')

    monkeypatch.setattr(bundled_engine.subprocess, 'run', run)
    assert bundled_engine._provision(tmp_path) == str(interpreter)
    assert calls[0] == [str(interpreter), '-c', 'import mcp']
    assert calls[1] == [str(interpreter), '-m', 'pip', 'install', '--require-hashes', '-r', str(lock)]
    assert 'requirements-mcp.lock' not in ' '.join(calls[1])


def _fake_interpreter(path, *, fail_install=False):
    """A real executable the provisioner can spawn. Not a stub of subprocess.run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    status = '1' if fail_install else '0'
    path.write_text(
        '#!' + sys.executable + '\n'
        'import sys\n'
        'if "-c" in sys.argv:\n'
        '    raise SystemExit(1)\n'
        'sys.stdout.write("Collecting not-json-noise\\n")\n'
        'sys.stdout.write("Downloading not-json-noise\\n")\n'
        'sys.stderr.write("pip-stderr-marker\\n")\n'
        f'raise SystemExit({status})\n',
        encoding='utf-8')
    path.chmod(0o755)


def test_provision_pip_output_stays_off_stdout(tmp_path, monkeypatch, capsys):
    interpreter = tmp_path / '.venv' / 'bin' / 'python'
    _fake_interpreter(interpreter)
    lock = tmp_path / 'aethermind-mcp-requirements.lock'
    lock.write_text('mcp==1.27.1\n', encoding='utf-8')
    monkeypatch.setattr(bundled_engine, 'mcp_requirements_lock', lambda: lock)
    assert bundled_engine._provision(tmp_path) == str(interpreter)
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Collecting not-json-noise' in captured.err
    assert 'Downloading not-json-noise' in captured.err
    assert 'pip-stderr-marker' in captured.err


def test_provision_failure_reports_pip_output_and_spares_stdout(tmp_path, monkeypatch, capsys):
    interpreter = tmp_path / '.venv' / 'bin' / 'python'
    _fake_interpreter(interpreter, fail_install=True)
    lock = tmp_path / 'aethermind-mcp-requirements.lock'
    lock.write_text('mcp==1.27.1\n', encoding='utf-8')
    monkeypatch.setattr(bundled_engine, 'mcp_requirements_lock', lambda: lock)
    with pytest.raises(RuntimeError) as caught:
        bundled_engine._provision(tmp_path)
    message = str(caught.value)
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'Collecting not-json-noise' in message
    assert 'Downloading not-json-noise' in message
    assert 'failed' in message
    assert 'Collecting not-json-noise' in captured.err
    assert 'Collecting not-json-noise' in (tmp_path / 'engine.log').read_text(encoding='utf-8')


def test_provision_names_the_missing_tessera_lock(tmp_path, monkeypatch):
    interpreter = tmp_path / '.venv' / 'bin' / 'python'
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    missing = tmp_path / 'absent' / 'aethermind-mcp-requirements.lock'
    monkeypatch.setattr(bundled_engine, 'mcp_requirements_lock', lambda: missing)

    def run(args, **kwargs):
        return SimpleNamespace(returncode=1 if '-c' in args else 0)

    monkeypatch.setattr(bundled_engine.subprocess, 'run', run)
    with pytest.raises(RuntimeError) as caught:
        bundled_engine._provision(tmp_path)
    message = str(caught.value)
    assert 'aethermind-mcp-requirements.lock' in message
    assert 'docs/packaging/aethermind-mcp-requirements.lock' in message
    assert 'mcp/requirements-mcp.lock' in message
    assert 'TESSERA_BUNDLE_SOURCE' in message
    assert 'bundled engine is missing its requirements lock' not in message


bundled_engine_path = bundled_engine._venv_python


def packaged_assets(tmp_path, monkeypatch):
    """A package layout as an install has it: engine bytes and the seed pack.

    The pack is part of a startable install, not an optional extra. A fixture
    without one describes a wheel that could never have come from this source,
    and the engine now refuses to start against a store it cannot seed.
    """
    from tessera import assets, seeds
    package = tmp_path / 'site' / 'tessera'
    root = package / '_assets' / 'aethermind-engine'
    (root / 'mcp').mkdir(parents=True)
    (root / 'mcp' / 'aethermind_mcp.py').touch()
    (root / 'build-receipt.json').write_text('{}')
    # A vendored engine carries its stdlib lib/ tree; that tree is what writes
    # the seed pack into a store, so a fixture without it is not an install.
    real = Path(bundled_engine.__file__).resolve().parent / '_assets' / 'aethermind-engine' / 'lib'
    shutil.copytree(real, root / 'lib')
    staged = package / '_assets' / 'seeds'
    staged.mkdir(parents=True)
    source = seeds.pack_path().parent
    shutil.copytree(source, staged, dirs_exist_ok=True)
    monkeypatch.setattr(assets, '__file__', str(package / 'assets.py'))
    return root


def test_packaged_discovery_ignores_a_stored_enabled_key(tmp_path, monkeypatch):
    from tessera.config import load_config
    root = packaged_assets(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    home.mkdir()
    config = load_config(home)
    assert not hasattr(config.aethermind, 'enabled')
    assert bundled_engine._engine_root(config) == root
    assert not (home / 'runtime').exists()
    monkeypatch.setenv('TESSERA_AETHERMIND_ENABLED', 'false')
    stored = load_config(home)
    assert stored.retired_settings['aethermind.enabled'] is False
    assert bundled_engine._engine_root(stored) == root
    monkeypatch.setenv('TESSERA_AETHERMIND_ENABLED', 'true')
    overridden = load_config(home)
    assert overridden.retired_settings['aethermind.enabled'] is True
    assert bundled_engine._engine_root(overridden) == root


@pytest.mark.parametrize('setting', ['url', 'endpoints'])
def test_a_configured_service_never_provisions_until_asked(tmp_path, monkeypatch, setting):
    from tessera.config import Config
    packaged_assets(tmp_path, monkeypatch)
    config = Config(home=tmp_path)
    if setting == 'url':
        config.aethermind.url = 'https://example.invalid/mcp'
    if setting == 'endpoints':
        config.aethermind.endpoints = ['https://example.invalid/mcp']
    monkeypatch.setattr(bundled_engine, '_provision', lambda *a: pytest.fail('must not provision'))
    assert bundled_engine.ensure_bundled_engine(config) is None


def test_a_stored_false_flag_still_provisions_the_packaged_engine(tmp_path, monkeypatch):
    from tessera.config import Config
    packaged_assets(tmp_path, monkeypatch)
    config = Config(home=tmp_path)
    calls = []

    def provision(root, runtime=None):
        calls.append('provision')
        runtime.mkdir(parents=True, exist_ok=True)
        return 'python'

    class Proc:
        def __init__(self):
            self.stdout = io.BytesIO(b'')

        def poll(self):
            return None

        def terminate(self):
            calls.append('terminate')

    monkeypatch.setattr(bundled_engine, '_provision', provision)
    monkeypatch.setattr(bundled_engine, '_pick_port', lambda: 9)
    monkeypatch.setattr(bundled_engine, '_wait_for_engine', lambda *args, **kwargs: True)
    monkeypatch.setattr(bundled_engine.subprocess, 'Popen', lambda *args, **kwargs: Proc())
    url = bundled_engine.ensure_bundled_engine(config)
    assert calls == ['provision']
    assert url == 'http://127.0.0.1:9/mcp'


def test_engine_log_is_truncated_on_launch_and_capped(tmp_path, monkeypatch):
    from tessera.config import Config
    root = tmp_path / 'engine'
    (root / 'mcp').mkdir(parents=True)
    (root / 'mcp' / 'aethermind_mcp.py').write_text(
        'import sys\n'
        'sys.stderr.write("OLD-SHOULD-BE-GONE\\n")\n'
        'sys.stderr.write("x" * 4000)\n'
        'sys.stderr.write("\\nTAIL\\n")\n'
        'raise SystemExit(1)\n',
        encoding='utf-8')
    (root / 'build-receipt.json').write_text('{}\n', encoding='utf-8')
    home = tmp_path / 'home'
    config = Config(home=home)
    config.home.mkdir()
    config.aethermind.bundled_path = str(root)
    config.aethermind.engine_log_max_bytes = 500
    monkeypatch.setattr(bundled_engine, '_provision', lambda *args, **kwargs: sys.executable)
    runtime = bundled_engine._runtime_root(config, root)
    runtime.mkdir(parents=True)
    (runtime / 'engine.log').write_text('OLD-SHOULD-BE-GONE\n', encoding='utf-8')
    with pytest.raises(RuntimeError, match='TAIL'):
        bundled_engine.ensure_bundled_engine(config)
    text = (runtime / 'engine.log').read_text(encoding='utf-8')
    assert 'OLD-SHOULD-BE-GONE' not in text
    assert 'TAIL' in text
    assert len(text.encode('utf-8')) <= 500


def test_embedded_lock_backend_reads_the_vendored_engine_report(tmp_path):
    from tessera.config import Config
    from tessera.engine_lock import embedded_lock_backend
    report = embedded_lock_backend(Config(home=tmp_path, cwd=tmp_path))
    assert report == ('unavailable' if sys.platform == 'win32' else 'fcntl')


def test_lock_report_follows_the_session_and_labels_an_import_error(tmp_path):
    from types import SimpleNamespace
    from tessera.config import Config
    from tessera.engine_lock import EmbeddedEngineLockError, embedded_lock_backend
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.url = 'https://example.invalid/mcp'
    expected = 'unavailable' if sys.platform == 'win32' else 'fcntl'
    assert embedded_lock_backend(config) == expected
    assert embedded_lock_backend(config, SimpleNamespace(uses_embedded_engine=False)) is None
    assert embedded_lock_backend(config, SimpleNamespace(uses_embedded_engine=True)) == expected
    root = tmp_path / 'broken-engine'
    core = root / 'lib' / 'aethermind' / 'core.py'
    core.parent.mkdir(parents=True)
    core.write_text('raise RuntimeError("secret engine internals")\n', encoding='utf-8')
    config.aethermind.bundled_path = str(root)
    with pytest.raises(EmbeddedEngineLockError, match=r'embedded AetherMind lock report could not be read \(RuntimeError\)') as caught:
        embedded_lock_backend(config)
    assert 'secret engine internals' not in str(caught.value)
    assert not (core.parent / '__pycache__').exists()


def test_runtime_is_scoped_to_home_and_receipt(tmp_path, monkeypatch):
    from tessera.config import Config
    root = packaged_assets(tmp_path, monkeypatch)
    config = Config(home=tmp_path / 'one')
    first = bundled_engine._runtime_root(config, root)
    assert first.is_relative_to(config.home)
    config.home = tmp_path / 'two'
    assert bundled_engine._runtime_root(config, root) != first
    config.home = tmp_path / 'one'
    (root / 'build-receipt.json').write_text('{"revision": 2}')
    assert bundled_engine._runtime_root(config, root) != first


def test_readiness_waits_for_mcp_tools_not_only_a_listening_socket(monkeypatch):
    import contextlib
    import time
    from types import SimpleNamespace
    from tessera.mcp_client import McpClient, McpError

    answers = iter([McpError('initializing'), [],
                    [{'name': 'read_layers'}, {'name': 'write_layer'}]])
    probes = []

    def tools(client):
        probes.append(client.url)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(bundled_engine.socket, 'create_connection',
                        lambda *args, **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(McpClient, 'list_tools', tools)
    assert bundled_engine._wait_for_engine(
        12345, SimpleNamespace(poll=lambda: None), time.monotonic() + 2, poll=0)
    assert len(probes) == 3


@pytest.mark.parametrize('incomplete', [[None], ['initializing'], [{'name': []}], [{'description': 'not ready'}]])
def test_readiness_retries_incomplete_tool_descriptors_until_valid(monkeypatch, incomplete):
    import contextlib
    import time
    from tessera.mcp_client import McpClient

    responses = iter([incomplete, [{'name': 'read_layers'}, {'name': 'write_layer'}]])
    probes = []

    def tools(client):
        probes.append(client.url)
        return next(responses)

    monkeypatch.setattr(bundled_engine.socket, 'create_connection',
                        lambda *args, **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(McpClient, 'list_tools', tools)
    assert bundled_engine._wait_for_engine(
        12345, SimpleNamespace(poll=lambda: None), time.monotonic() + 2, poll=0)
    assert len(probes) == 2
