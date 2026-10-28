import os
from pathlib import Path
import subprocess
import sys

from fake_backend import fake_http_server
from integration_helpers import FakeMcpServer


ROOT = Path(__file__).resolve().parents[1]


def cli_env(home):
    """Run the CLI against the package by PYTHONPATH, never by cwd.

    cwd must not be the real checkout. A turn that delegates spawns a worker,
    and that worker's worktree is registered in whatever repository the cwd sits
    inside. A test run would otherwise leave a registered worktree in the
    developer's own git directory.
    Pointing cwd at the test's own tmp_path and importing by PYTHONPATH keeps the
    registration inside the throwaway tree.
    """
    return dict(os.environ, TESSERA_HOME=str(home),
                PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get('PYTHONPATH', ''),
                TESSERA_JEV_ENABLED='false')


def memory_table(url):
    """A loopback AetherMind the CLI subprocess can actually use.

    A checkout has no build receipt, so a turn with no endpoint refuses.
    The stand-in is a configured service, not an off switch.
    """
    return ('[aethermind]\n'
            f'endpoints = ["{url}"]\n'
            'probe_timeout = 0.5\n'
            'probe_interval = 0\n')


def run_cli(home, *args):
    return subprocess.run([sys.executable, '-m', 'tessera', *args], cwd=str(home),
                          env=cli_env(home), capture_output=True, text=True, timeout=15)


def test_cli_one_shot_persists_reply_and_status(tmp_path):
    response = {'choices': [{'message': {'content': 'Hello from the HTTP model.'}}]}
    with FakeMcpServer(dialect='path') as memory, fake_http_server([(200, response)]) as (url, received):
        (tmp_path / 'config.toml').write_text(
            f'''[backends.test]\nbase_url="{url}"\nmodel="test-model"\n[roles]\nmain="test"\n'''
            + memory_table(memory.url))
        result = run_cli(tmp_path, 'run', 'Hello')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'Hello from the HTTP model.'
    status = run_cli(tmp_path, 'status')
    assert status.returncode == 0, status.stderr
    assert 'write_debt' in status.stdout
    assert 'test' in status.stdout
    zoom = run_cli(tmp_path, 'zoom', '0', '1')
    assert zoom.returncode == 0, zoom.stderr
    assert zoom.stdout.strip() == 'Hello'


def test_init_does_not_overwrite_config_and_missing_backend_is_actionable(tmp_path):
    first = run_cli(tmp_path, 'init')
    assert first.returncode == 0, first.stderr
    original = (tmp_path / 'config.toml').read_bytes()
    second = run_cli(tmp_path, 'init')
    assert second.returncode != 0
    assert (tmp_path / 'config.toml').read_bytes() == original
    (tmp_path / 'config.toml').write_text('')
    missing = run_cli(tmp_path, 'run', 'Hello')
    assert missing.returncode != 0
    assert 'backend' in missing.stderr.lower()


def test_cli_delegation_prints_completed_background_report(tmp_path):
    response = {'choices': [{'message': {'content': 'Actual delegated result: alpha-123'}}]}
    with FakeMcpServer(dialect='path') as memory, fake_http_server([(200, response)]) as (url, received):
        # Safe mode allows one request on a local model and the chat holds it.
        # The hand-set 2 leaves a slot for the delegated worker.
        (tmp_path / 'config.toml').write_text(
            f'[backends.test]\nbase_url="{url}"\nmodel="test-model"\n'
            'max_concurrent_requests = 2\n[roles]\nmain="test"\n'
            + memory_table(memory.url))
        result = run_cli(tmp_path, 'run', 'Delegate an independent task to inspect a note')
    assert result.returncode == 0, result.stderr
    assert '[turn-1] Actual delegated result: alpha-123' in result.stdout


def test_cli_chat_two_turns_and_clean_exit(tmp_path):
    replies = []
    for text in ('Hello from chat.', 'You are welcome.'):
        replies.append((200, {'choices': [{'message': {'content': text}}]}))
    with FakeMcpServer(dialect='path') as memory, fake_http_server(replies) as (url, received):
        (tmp_path / 'config.toml').write_text(
            f'[backends.test]\nbase_url="{url}"\nmodel="test-model"\n[roles]\nmain="test"\n'
            + memory_table(memory.url))
        result = subprocess.run([sys.executable, '-m', 'tessera', 'chat'], input='Hello\nThanks\n/quit\n', cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'Hello from chat.' in result.stdout
    assert 'You are welcome.' in result.stdout
    assert received[0]['body']['messages'][0] == received[1]['body']['messages'][0]


def test_cli_named_chat_reuses_persistent_subject(tmp_path):
    response = {'choices': [{'message': {'content': 'Persistent answer'}}]}
    with FakeMcpServer(dialect='path') as memory, fake_http_server([(200, response), (200, response)]) as (url, received):
        (tmp_path / 'config.toml').write_text(
            f'[backends.test]\nbase_url="{url}"\nmodel="test-model"\n[roles]\nmain="test"\n'
            + memory_table(memory.url))
        for prompt in ('Hello', 'Continue'):
            result = subprocess.run([sys.executable, '-m', 'tessera', 'chat', '--session', 'subject'], input=prompt + '\n/quit\n', cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            assert 'Persistent answer' in result.stdout
    files = list((tmp_path / 'sessions' / 'subject' / 'log').glob('*.jsonl'))
    rows = []
    for path in files:
        rows.extend(__import__('json').loads(line) for line in path.read_text().splitlines())
    assert [row['text'] for row in rows] == ['Hello', 'Persistent answer', 'Continue', 'Persistent answer']


def test_init_says_an_install_cannot_answer_instead_of_reporting_plain_success(tmp_path):
    """The defect this replaces: init wrote a placeholder config and said only 'Created'.

    Measured before the change: `tessera --home X init` printed one line, "Replace
    placeholders and set credential environment variables", wrote base_url
    https://models.example.com/v1 (a host that does not resolve) and api_key_env
    MODEL_API_KEY (unset), and returned 0. The install loaded cleanly and then failed at
    the first turn with nothing pointing at the placeholder as the cause.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ('MODEL_API_KEY', 'TESSERA_HOME')}
    result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init'],
                            cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'Created' in result.stdout
    combined = result.stdout + result.stderr
    # It must name the two things that are actually missing, not just say "placeholders".
    assert 'models.example.com' in combined, combined
    assert 'MODEL_API_KEY' in combined, combined
    assert 'cannot answer' in combined, combined


def test_init_check_reports_gaps_without_writing_or_calling_out(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ('MODEL_API_KEY', 'TESSERA_HOME')}
    # No config yet: --check must say so rather than create one.
    missing = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init', '--check'],
                             cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert missing.returncode == 1
    assert not (tmp_path / 'config.toml').exists()
    # Write one, then check: gaps are named and the exit code is non-zero.
    subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init'],
                   cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    gaps = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init', '--check'],
                          cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert gaps.returncode == 1
    assert 'models.example.com' in gaps.stderr


def _packaged_engine(home):
    """A stand-in bundle so a backend check is a complete install."""
    root = home / 'engine'
    (root / 'mcp').mkdir(parents=True)
    (root / 'mcp' / 'aethermind_mcp.py').write_text('# packaged engine stand-in\n')
    (root / 'build-receipt.json').write_text('{}\n')


def test_init_check_passes_when_a_role_resolves_to_a_usable_backend(tmp_path):
    """The clean case must be clean: no false alarm on a correctly configured install."""
    _packaged_engine(tmp_path)
    (tmp_path / 'config.toml').write_text(
        '[backends.primary]\nbase_url="https://api.x.ai/v1"\nmodel="grok-4.7"\n'
        'api_key_env="MODEL_API_KEY"\nallow_api_key=true\n[roles]\nmain="primary"\n'
        '[aethermind]\nbundled_path="engine"\n')
    env = dict(cli_env(tmp_path), MODEL_API_KEY='a-real-looking-key')
    result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init', '--check'],
                            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'can answer' in result.stdout


def test_init_check_treats_a_credential_ref_as_satisfied(tmp_path):
    """A [credentials] table is a legitimate way to supply a key; do not cry about the env."""
    _packaged_engine(tmp_path)
    (tmp_path / 'config.toml').write_text(
        '[backends.primary]\nbase_url="https://api.x.ai/v1"\nmodel="grok-4.7"\n'
        'credential_ref="xai-oauth"\nallow_api_key=false\n'
        '[credentials.xai-oauth]\noauth="xai"\n[roles]\nmain="primary"\n'
        '[aethermind]\nbundled_path="engine"\n')
    result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init', '--check'],
                            cwd=str(tmp_path), env=cli_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
