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
    """A relative name must reach the engine absolute.

    The embedded engine runs with its own working directory, so a bare
    `projects/default` sent to it addresses `<runtime>/projects/default` and not
    the store the seeder wrote. The loader resolves it once, for every reader.
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
