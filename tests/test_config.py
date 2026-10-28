from pathlib import Path

import pytest

from tessera.config import Config, load_config


def test_named_backends_roles_and_environment_overrides(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('''
[backends.local]
base_url = "http://localhost:9000/v1"
model = "small"
api_key_env = "LOCAL_KEY"
[roles]
main = "local"
fast = "local"
compactor = "local"
[tools]
approval = "deny_risky"
[view]
high_bytes = 4000
low_bytes = 2000
''')
    monkeypatch.setenv('TESSERA_HOME', str(tmp_path))
    monkeypatch.setenv('TESSERA_BACKENDS__LOCAL__MODEL', 'override')
    monkeypatch.setenv('TESSERA_AETHERMIND_ENABLED', 'false')
    config = load_config()
    assert config.home == tmp_path
    assert config.backend_for('main').model == 'override'
    assert config.tools.approval == 'deny_risky'
    assert config.retired_settings['aethermind.enabled'] is False
    assert not hasattr(config.aethermind, 'enabled')
    assert config.view.low_bytes == 2000


def test_invalid_policy_and_missing_backend_fail_early(tmp_path):
    (tmp_path / 'config.toml').write_text('[tools]\napproval="maybe"\n')
    with pytest.raises(ValueError, match='approval'):
        load_config(tmp_path)
    (tmp_path / 'config.toml').write_text('[roles]\nmain="missing"\n')
    with pytest.raises(ValueError, match='backend'):
        load_config(tmp_path)


def test_defaults_are_offline_and_configurable(tmp_path):
    config = load_config(tmp_path)
    assert config.home == tmp_path
    assert config.backends == {}
    assert config.tools.approval == 'ask'
    assert config.agents.max_depth >= 1
    assert isinstance(config.cwd, Path)


@pytest.mark.parametrize('toml', [
    '[view]\nnode_bytes = "512"',
    '[agents]\nmax_depth = true',
    '[jev]\nenabled = "false"',
    '[tools]\ntimeout = -1',
    '[backends.local]\nbase_url = "https://models.example.com/v1"',
])
def test_invalid_configuration_types_and_incomplete_backends_are_actionable(tmp_path, toml):
    (tmp_path / 'config.toml').write_text(toml)
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_removed_server_token_settings_are_ignored_not_config_overrides(tmp_path, monkeypatch):
    (tmp_path / 'config.toml').write_text('[server]\ntoken_env="TESSERA_SERVER_TOKEN"\nport=0\n')
    monkeypatch.setenv('TESSERA_SERVER_TOKEN', 'not-a-config-value')
    config = load_config(tmp_path)
    assert not hasattr(config.server, 'token_env') and not hasattr(config.server, 'token')
    assert config.server.port == 0
    monkeypatch.setenv('TESSERA_SERVER_TOKEN_ENV', 'TESSERA_CUSTOM_BEARER')
    monkeypatch.setenv('TESSERA_CUSTOM_BEARER', 'also-not-config')
    config = load_config(tmp_path)
    assert not hasattr(config.server, 'token_env')
    assert 'not-a-config-value' not in repr(config) and 'also-not-config' not in repr(config)


def test_load_config_reports_that_no_config_file_was_found(tmp_path):
    """A typo'd --home must be distinguishable from an intentionally default home."""
    config = load_config(tmp_path / 'not-the-right-home')
    assert config.config_missing is True
    # The silent-fallback half of the fault: nothing raised, defaults came back.
    assert config.tools.approval == 'ask'

    (tmp_path / 'home').mkdir()
    (tmp_path / 'home' / 'config.toml').write_text('[tools]\napproval = "deny_risky"\n')
    present = load_config(tmp_path / 'home')
    assert present.config_missing is False
    assert present.tools.approval == 'deny_risky'


def test_serve_on_a_missing_home_names_the_fallback_on_stderr(tmp_path, monkeypatch, capsys):
    """The operator-facing half: starting on defaults says so, without touching stdout.

    stdout carries a contract here — desktop emits its JSON ready record as the
    first line and serve prints 'Serving Tessera at <url>'. The diagnostic goes to
    stderr so neither consumer sees an extra line.
    """
    from tessera import cli, server as server_module

    started = {}

    def fake_serve(config):
        started['config'] = config
        print(f'Serving Tessera at {config.server.host}:{config.server.port}', flush=True)

    monkeypatch.setattr(server_module, 'serve', fake_serve)
    assert cli.main(['--home', str(tmp_path / 'typo'), 'serve']) == 0
    assert started['config'].config_missing is True
    captured = capsys.readouterr()
    assert 'no config.toml' in captured.err
    assert str(tmp_path / 'typo') in captured.err
    # stdout stays exactly the serve line; nothing was added to it.
    assert captured.out.strip().startswith('Serving Tessera at ')

    started.clear()
    (tmp_path / 'real').mkdir()
    (tmp_path / 'real' / 'config.toml').write_text('[tools]\napproval = "deny_risky"\n')
    assert cli.main(['--home', str(tmp_path / 'real'), 'serve']) == 0
    assert started['config'].config_missing is False
    captured = capsys.readouterr()
    assert 'no config.toml' not in captured.err
    assert started['config'].tools.approval == 'deny_risky'


def test_desktop_stays_silent_about_a_missing_home(tmp_path, monkeypatch, capsys):
    """The desktop shell owns its pipes: a ready record and an empty stderr.

    Its home is supplied by the app, not typed by an operator, so the fallback
    note belongs on `serve` only. Caught by the full suite: the first draft put
    the note on both commands and broke test_desktop_child_handles_parent_termination.
    """
    from tessera import cli, desktop as desktop_module

    started = {}
    monkeypatch.setattr(desktop_module, 'run', lambda config: started.setdefault('config', config))
    assert cli.main(['--home', str(tmp_path / 'typo'), 'desktop']) == 0
    assert started['config'].config_missing is True
    captured = capsys.readouterr()
    assert captured.out == '' and captured.err == ''


def test_bundled_engine_path_is_a_pure_config_signal(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    engine = home / 'aethermind-engine'
    (engine / 'mcp').mkdir(parents=True)
    (engine / 'mcp' / 'aethermind_mcp.py').write_text('# bundled engine entry\n')
    (engine / 'build-receipt.json').write_text('{}')

    (home / 'config.toml').write_text(
        '[aethermind]\nbundled_path = "aethermind-engine"\n')
    config = load_config(home)
    # Config load must stay pure: no server registration, no endpoint injection,
    # no spawned processes. The launcher owns all of that at session time.
    assert 'aethermind-bundled' not in config.mcp_servers
    assert config.aethermind.endpoints == []
    assert config.aethermind.bundled_path == 'aethermind-engine'

    (home / 'config.toml').write_text(
        '[aethermind]\nbundled_path = "aethermind-engine"\nurl = "https://example.invalid/mcp"\n')
    config = load_config(home)
    # A configured service wins by validation rules; the path remains a signal.
    assert 'aethermind-bundled' not in config.mcp_servers
    assert config.aethermind.url == 'https://example.invalid/mcp'


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_unlimited_agent_limits_validate(tmp_path, value):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.agents.max_concurrent = value
    if value == 0 and type(value) is int:
        config.validate()
    else:
        with pytest.raises(ValueError):
            config.validate()


def test_retired_swarm_caps_load_as_notes_and_are_not_applied(tmp_path):
    (tmp_path / 'config.toml').write_text(
        '[swarm]\njob_workers = 8\nmax_concurrent = 8\ndefault_backend_limit = 4\n'
        'backend_limits = { "*" = 4 }\ninitial_concurrency = 2\naimd_successes = 4\n'
        'aimd_add = 1\naimd_factor = 0.5\n'
        '[accounts.pool]\nprovider_kind = "cli_worker"\n'
        '[accounts.pool.limits]\nmax_concurrent = 24\ninitial_concurrency = 24\n'
    )
    loaded = load_config(tmp_path)
    for name in ('job_workers', 'max_concurrent', 'default_backend_limit', 'backend_limits',
                 'initial_concurrency', 'aimd_successes', 'aimd_add', 'aimd_factor'):
        assert not hasattr(loaded.swarm, name)
        assert any(f'swarm.{name}' in note and 'retired' in note for note in loaded.migration_notes)
    assert 'max_concurrent' not in loaded.accounts['pool'].limits
    assert 'initial_concurrency' not in loaded.accounts['pool'].limits
    assert any('accounts.pool.limits.max_concurrent' in note for note in loaded.migration_notes)
