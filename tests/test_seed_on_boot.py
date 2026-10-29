"""A memory store Tessera starts against is never empty.

The pack reaching a store used to depend on the operator running `tessera start`
or `tessera seed`. A session that started the engine cold got a running service
with no rules in it, which reads exactly like a working install until the model
answers without any of them. These tests hold the boot guarantee:

- the store a session addresses is the store that gets seeded;
- the embedded engine writes the pack before it answers;
- a store the engine cannot parse is moved aside intact, never left memoryless;
- a seed that cannot be written is a refusal, not a warning beside an empty store.
"""

import io
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tessera import seeds as seed_pack
from tessera.config import Config, load_config

# What a fresh install must never fall below. Additions are fine; a pack that
# loses rules is the regression this file exists to catch.
MINIMUM_LAYERS = 31
MINIMUM_TEXTURES = 6
MINIMUM_SKILLS = 5


def _config(tmp_path, project=None, shelf=''):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.aethermind.project = str(tmp_path / 'store') if project is None else project
    config.skills.shelf = shelf
    return config


def _layer_count(store):
    text = (Path(store) / '.aethermind' / 'layers.aem').read_text(encoding='utf-8')
    return text.count('[[layer]]')


def _texture_count(store):
    text = (Path(store) / '.aethermind' / 'texture.aem').read_text(encoding='utf-8')
    return sum(1 for line in text.splitlines()
               if line.startswith('20') and '[' in line and line.rstrip().endswith(']'))


def test_the_pack_is_still_complete():
    """The shipped pack is the original floor, not a reduced copy of it."""
    pack = seed_pack.load_pack()
    layers, textures = seed_pack.normalize(pack)
    assert len(layers) >= MINIMUM_LAYERS, len(layers)
    assert len(textures) >= MINIMUM_TEXTURES, len(textures)
    assert len(pack.get('skills') or []) >= MINIMUM_SKILLS
    assert {layer['ctx'].split('/')[1] for layer in layers} >= {
        'safety', 'operating', 'delivery', 'board', 'continuity', 'first-run'}


def test_the_store_a_session_addresses_is_the_store_that_is_seeded(tmp_path):
    """An absolute project, a relative one, and an empty one all resolve."""
    absolute = _config(tmp_path)
    assert seed_pack.store_path(absolute) == tmp_path / 'store'

    relative = _config(tmp_path, project='projects/default')
    assert seed_pack.store_path(relative) == tmp_path / 'projects' / 'default'

    empty = _config(tmp_path, project='')
    assert seed_pack.store_path(empty) == tmp_path

    assert seed_pack.shelf_path(empty) == tmp_path / 'skills'
    assert seed_pack.shelf_path(_config(tmp_path, shelf=str(tmp_path / 'mine'))) == tmp_path / 'mine'


def test_a_relative_project_is_resolved_before_any_reader_sees_it(tmp_path):
    """A relative name in config.toml is an absolute store after load.

    Sessions pass that absolute path. The live check that the engine itself
    can still be asked for the relative name is
    test_a_relative_project_is_readable_after_boot_without_a_setup_verb.
    """
    relative_home = tmp_path / 'relative'
    relative_home.mkdir()
    (relative_home / 'config.toml').write_text(
        '[aethermind]\nproject = "projects/default"\nnote_project = "notes"\n', encoding='utf-8')
    config = load_config(relative_home)
    home = relative_home.resolve()
    assert config.aethermind.project == str(home / 'projects' / 'default')
    assert config.aethermind.note_project == str(home / 'notes')
    assert seed_pack.store_path(config) == home / 'projects' / 'default'

    absolute = _config(tmp_path, project=str(tmp_path / 'store'))
    assert seed_pack.store_path(absolute) == tmp_path / 'store'

    example_home = tmp_path / 'example'
    example_home.mkdir()
    (example_home / 'config.toml').write_text(
        '[aethermind]\nproject = "your-project-id"\n', encoding='utf-8')
    example = load_config(example_home)
    # Still resolved, so nothing downstream can see a relative address. `start`
    # recognizes the name and heals it to the default store.
    example_stem = example_home.resolve()
    assert example.aethermind.project == str(example_stem / 'your-project-id')
    assert seed_pack.store_path(example).is_absolute()


def test_ensure_writes_the_pack_into_the_configured_store(tmp_path):
    config = _config(tmp_path)
    result = seed_pack.ensure(config)
    store = seed_pack.store_path(config)
    assert result['layers_written'] == len(seed_pack.load_pack()['layers'])
    assert _layer_count(store) == result['layers_written']
    assert _texture_count(store) == result['textures_written']
    assert (tmp_path / 'skills' / 'first-contact' / 'SKILL.md').is_file()


def test_a_second_boot_adds_nothing_and_keeps_an_operator_rule(tmp_path):
    """The pack is a floor: a rerun skips its own bodies and the operator's."""
    config = _config(tmp_path)
    seed_pack.ensure(config)
    store = seed_pack.store_path(config)
    before = (store / '.aethermind' / 'layers.aem').read_text(encoding='utf-8')
    again = seed_pack.ensure(config)
    assert again['layers_written'] == 0 and again['textures_written'] == 0
    assert again['layers_skipped'] >= MINIMUM_LAYERS
    assert (store / '.aethermind' / 'layers.aem').read_text(encoding='utf-8') == before


def test_a_store_the_engine_cannot_parse_is_set_aside_and_the_pack_lands(tmp_path):
    """A foreign record must not leave the install memoryless, and is not lost."""
    config = _config(tmp_path)
    store = seed_pack.store_path(config)
    store.mkdir(parents=True)
    (store / '.aethermind').mkdir()
    poison = ('version = 1\n\n[[layer]]\nid = "1"\nauthor = "hand-written"\n'
              'body = "Rule: a record written outside the engine."\nctx = "local/mesh"\n')
    (store / '.aethermind' / 'layers.aem').write_text(poison, encoding='utf-8')

    result = seed_pack.ensure(config)

    assert result.get('quarantined'), result
    moved = Path(result['quarantined'])
    assert moved.is_dir()
    assert (moved / 'layers.aem').read_text(encoding='utf-8') == poison
    assert _layer_count(store) >= MINIMUM_LAYERS
    assert (store / '.aethermind' / 'layers.aem').read_text(encoding='utf-8') != poison


class _FakeProc:
    def __init__(self):
        self.stdout = io.BytesIO()
        self.exited = None

    def poll(self):
        return self.exited

    def terminate(self):
        self.exited = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.exited = -9


def _fake_engine_root(tmp_path):
    """A directory shaped like a bundle: entry point plus build receipt."""
    root = tmp_path / 'engine'
    (root / 'mcp').mkdir(parents=True, exist_ok=True)
    (root / 'mcp' / 'aethermind_mcp.py').write_text('# entry\n', encoding='utf-8')
    (root / 'build-receipt.json').write_text(json.dumps({'engine': 'bundled'}), encoding='utf-8')
    return root


def test_the_engine_seeds_the_store_before_it_answers(tmp_path, monkeypatch):
    """A cold session start gets the pack, with no setup verb run first."""
    from tessera import bundled_engine

    config = _config(tmp_path)
    root = _fake_engine_root(tmp_path)
    started = []
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda checked: root)
    monkeypatch.setattr(bundled_engine, '_provision', lambda checked, runtime: 'python')
    monkeypatch.setattr(bundled_engine, '_wait_for_engine', lambda port, process, deadline: True)

    def popen(args, **kwargs):
        started.append(args)
        return _FakeProc()

    monkeypatch.setattr(bundled_engine.subprocess, 'Popen', popen)
    try:
        url = bundled_engine.ensure_bundled_engine(config, force=True)
        store = seed_pack.store_path(config)
        assert started, 'the engine was never launched'
        assert _layer_count(store) >= MINIMUM_LAYERS
        assert url and url.startswith('http://127.0.0.1:')
    finally:
        bundled_engine._REGISTRY.clear()


def test_the_engine_refuses_to_serve_an_empty_store(tmp_path, monkeypatch):
    """A seed that cannot be written stops the launch, by name."""
    from tessera import bundled_engine

    config = _config(tmp_path)
    root = _fake_engine_root(tmp_path)
    launched = []
    monkeypatch.setattr(bundled_engine, '_engine_root', lambda checked: root)

    def refuse(checked):
        raise seed_pack.SeedUnavailable('the seed pack cannot be written because this '
                                        'install carries no AetherMind engine')

    monkeypatch.setattr(seed_pack, 'ensure', refuse)
    monkeypatch.setattr(bundled_engine.subprocess, 'Popen',
                        lambda args, **kwargs: launched.append(args))
    try:
        with pytest.raises(RuntimeError) as refused:
            bundled_engine.ensure_bundled_engine(config, force=True)
    finally:
        bundled_engine._REGISTRY.clear()
    message = str(refused.value)
    assert str(seed_pack.store_path(config)) in message
    assert 'could not be seeded' in message
    assert 'carries no AetherMind engine' in message
    assert launched == [], 'an engine was launched against a store with no seeds'


class _NoLockBackend(Exception):
    """Stands in for the engine's LockUnavailableError on a platform without fcntl."""


def test_a_platform_without_file_locking_skips_the_seed_and_still_serves(tmp_path, monkeypatch):
    """Native Windows refuses every store write, and that is not a launch failure.

    Blocking the engine there would replace the product's own labeled lock
    refusal with a seed error and stop a turn the harness can still run. The
    seed reports itself unavailable, and the engine starts.
    """
    from tessera import bundled_engine

    config = _config(tmp_path)

    def write_that_cannot_lock(store_root, layers, textures, skills_dir, result):
        raise _NoLockBackend('no safe local file locking backend is available; refusing to write')

    monkeypatch.setattr(seed_pack, '_engine_error_types',
                        lambda lib_dir: (ValueError, _NoLockBackend))
    monkeypatch.setattr(seed_pack, '_write', write_that_cannot_lock)
    result = seed_pack.ensure(config)
    assert result['unavailable'] is True
    assert 'no safe local file locking backend' in result['unavailable_reason']
    assert 'not written here' in seed_pack.describe(result)

    monkeypatch.setattr(seed_pack, 'ensure', lambda checked: {'unavailable': True})
    assert bundled_engine._seed_store(config)['unavailable'] is True


def test_a_real_write_failure_still_raises(tmp_path, monkeypatch):
    """Only the platform lock state is tolerated. Anything else stops the launch."""
    from tessera import bundled_engine

    config = _config(tmp_path)

    def boom(checked):
        raise seed_pack.SeedUnavailable('disk is read-only')

    monkeypatch.setattr(seed_pack, 'ensure', boom)
    with pytest.raises(RuntimeError, match='could not be seeded'):
        bundled_engine._seed_store(config)


def test_a_bare_project_name_reaches_the_configured_store(tmp_path):
    """A model that writes `default` must reach the default store, not a sibling.

    This is the VM transcript: the model called memory_tree with project_id
    `default`, the engine resolved it against its own runtime directory, and the
    answer came back "no layers" while the store held 31.
    """
    from tessera.capture import Capture

    class Client:
        def __init__(self):
            self.seen = []

        def tree_view(self, project_id, budget_chars=2000):
            self.seen.append(project_id)
            return {'success': True, 'tree': ['a node']}

        def read_layers(self, project_id, **filters):
            self.seen.append(project_id)
            return {'layers': []}

    store = str(tmp_path / 'projects' / 'default')
    personal = str(tmp_path / 'projects' / 'personal')
    client = Client()
    capture = Capture(client, tmp_path / 'outbox', store, personal, 'operator')

    capture.memory_tree(project_id='default')
    assert client.seen[-1] == store
    capture.memory_tree(project_id='personal')
    assert client.seen[-1] == personal
    capture.memory_tree()
    assert client.seen[-1] == store
    capture.memory_tree(project_id=str(tmp_path / 'other'))
    assert client.seen[-1] == str(tmp_path / 'other')

    with pytest.raises(ValueError) as refused:
        capture.memory_tree(project_id='somewhere-else')
    assert 'not a configured store' in str(refused.value)
    assert store in str(refused.value)


def test_a_bare_project_name_in_recall_is_refused_before_it_dirty_health(tmp_path):
    """A wrong name is a bad argument, not a memory service failure."""
    from tessera.capture import Capture

    class Client:
        def read_layers(self, project_id, **filters):
            raise AssertionError('a refused name must not reach the service')

    capture = Capture(Client(), tmp_path / 'outbox', str(tmp_path / 'projects' / 'default'))
    with pytest.raises(ValueError):
        capture.memory_recall(query='anything', project_id='default-ish')
    assert capture.service_error is None


def test_the_prompt_names_the_store_path_and_never_a_bare_default(tmp_path):
    """The guidance must not hand the model a name that cannot resolve."""
    from tessera.config import PromptConfig

    guidance = PromptConfig().memory_guidance
    assert 'Pass project_id exactly as written above' in guidance
    assert 'a bare name is not a store' in guidance


def test_start_refuses_instead_of_serving_an_unseeded_store(tmp_path, monkeypatch, capsys):
    """start returns a failure, and says which store, instead of serving."""
    from tessera import cli, bundled_engine

    config = _config(tmp_path)
    monkeypatch.setattr(bundled_engine, 'ensure_local_engine_bundle', lambda checked: None)

    def refuse(checked):
        raise seed_pack.SeedUnavailable('no engine')

    monkeypatch.setattr(seed_pack, 'ensure', refuse)
    args = SimpleNamespace(install_launcher=False, no_browser=True, command='start')
    assert cli._start(config, args) == 1
    errors = capsys.readouterr().err
    # The example config ships a placeholder project; start heals it to a real
    # store and refuses there, so the name in the refusal is the store it would
    # have opened.
    healed = tmp_path / 'projects' / 'default'
    assert str(healed) in errors
    assert 'could not be seeded' in errors
    assert '"tessera seed"' in errors


def test_start_gives_a_first_run_a_real_project_store(tmp_path):
    """A heal makes the first store exist by name, so it can be seeded."""
    from tessera import cli, bundled_engine

    config = _config(tmp_path, project='your-project-id')
    original = bundled_engine.ensure_local_engine_bundle
    try:
        bundled_engine.ensure_local_engine_bundle = lambda checked: None
        # Stop before serving: the assertion is about config and the store.
        config.server.port = 0
        import tessera.server as server_module
        original_server = server_module.create_server
        server_module.create_server = lambda checked: (_ for _ in ()).throw(SystemExit('served'))
        try:
            with pytest.raises(SystemExit, match='served'):
                cli._start(config, SimpleNamespace(install_launcher=False, no_browser=True))
        finally:
            server_module.create_server = original_server
    finally:
        bundled_engine.ensure_local_engine_bundle = original

    text = (tmp_path / 'config.toml').read_text(encoding='utf-8')
    assert 'your-project-id' not in text
    assert (tmp_path / 'projects' / 'default' / '.aethermind' / 'layers.aem').is_file()


def _receipt_bundle(home):
    """A startable copy of the vendored engine.

    A source checkout has no build receipt, and the test fixture refuses to
    materialize one. The copy is what the child executes. The receipt is not
    written into the vendored tree.
    """
    from tessera.assets import asset_path
    bundle = home / 'engine-bundle'
    shutil.copytree(asset_path('aethermind-engine'), bundle)
    (bundle / 'build-receipt.json').write_text(
        json.dumps({'engine': 'test-boot'}) + '\n', encoding='utf-8')
    return bundle


def test_a_relative_project_is_readable_after_boot_without_a_setup_verb(tmp_path, monkeypatch):
    """Boot with project = "projects/default" and read that relative path.

    No `tessera init` and no `tessera seed`. The engine launch is what writes
    the pack. The reader then asks for the relative name the config file
    contains. Zero layers is the blank load: the seeder and that reader opened
    different directories.

    The embedded server imports mcp. A fresh ``.[test]`` install does not
    include it, and this suite stays offline, so the proof runs only when
    that import is already possible. It does not download mcp.
    """
    pytest.importorskip(
        'mcp.server.fastmcp',
        reason='embedded boot proof needs mcp already installed; the suite does not download it',
    )
    from tessera import bundled_engine
    from tessera.aethermind import AetherMindClient

    home = tmp_path / 'home'
    home.mkdir()
    bundle = _receipt_bundle(home)
    (home / 'config.toml').write_text(
        '[aethermind]\n'
        'project = "projects/default"\n'
        f'bundled_path = {json.dumps(str(bundle))}\n',
        encoding='utf-8')
    config = load_config(home)
    # The packaged launch builds a venv and pip-installs the MCP lock. This
    # proof uses the interpreter that already imported mcp, and does not download.
    monkeypatch.setattr(bundled_engine, '_provision', lambda root, runtime=None: sys.executable)
    seeded = home.resolve() / 'projects' / 'default'
    try:
        url = bundled_engine.ensure_bundled_engine(config, force=True)
        assert url and url.startswith('http://127.0.0.1:') and not url.endswith(':1/mcp'), url
        assert (seeded / '.aethermind' / 'layers.aem').is_file()
        assert _layer_count(seeded) >= MINIMUM_LAYERS
        client = AetherMindClient(url, timeout=30)
        # The string in config.toml, not the absolute path the loader rewrote.
        relative = client.read_layers('projects/default')
        layers = relative.get('layers') if isinstance(relative, dict) else None
        assert isinstance(layers, list) and len(layers) >= MINIMUM_LAYERS, relative
        assert Path(relative['store']) == seeded, relative
        absolute = client.read_layers(config.aethermind.project)
        absolute_layers = absolute.get('layers') if isinstance(absolute, dict) else None
        assert isinstance(absolute_layers, list) and len(absolute_layers) >= MINIMUM_LAYERS, absolute
        assert Path(absolute['store']) == seeded, absolute
    finally:
        bundled_engine.stop_bundled_engine(config)
