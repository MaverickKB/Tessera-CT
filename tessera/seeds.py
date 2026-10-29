"""Install the packaged seed pack into a store, from an installed wheel.

The seed pack ships inside the package (`tessera/_assets/seeds/`), but the
installer that writes it lived only in the source tree at
`scripts/install-seed-layers.py`. An installed Tessera therefore carried the
rules and had no command to place them, so a stranger's first install booted
with no judgment at all. This module is the packaged half: it reads the pack
from the asset directory and writes it through the bundled engine, which is
stdlib-only, so no venv and no MCP server are needed to bootstrap.

The guarantee this module makes is about boot, not about a command: a memory
store Tessera starts against is never empty. `ensure` is the call the engine
lifecycle makes before it serves, so the first session of an install reads the
pack even when nobody ran a setup verb. A store that cannot take the pack is an
error the caller must report, never a warning beside an empty store.

Three properties are load-bearing and all are tested:

- A rerun never rewrites a body already in the store. The pack is a floor, and
  the operator's own version of a rule wins over the seed.
- A skill directory that already exists on the shelf is left untouched, for the
  same reason.
- A store the engine cannot parse is moved aside intact, then the pack is
  written into a fresh one. A hand-written or foreign record must not leave an
  install memoryless, and the bytes that made it unreadable are kept as the
  rollback.

The pack names no person, host, or companion; that is guarded separately in
tests/test_seed_pack.py.
"""

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
import tomllib
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

from .assets import asset_path
from .config import resolve_store_path

SEED_AUTHOR = 'seed'
SEED_MARKERS = ['seed']
TEXTURE_MARKERS = ['seed']
SKILL_DIR = 'skills'
ENGINE_ASSET = 'aethermind-engine'
CORRUPT_SUFFIX = '.corrupt-'

# Read by whoever sees the refusal, so it names the state and the way out.
RECOVERY_HINT = (
    'Nothing was served against an empty store. To recover by hand: run '
    '"tessera seed", which writes the packaged pack into this store, or name a '
    'different [aethermind] project in config.toml. If the store itself is '
    'unreadable, move its .aethermind directory aside and run that command again.'
)


class SeedUnavailable(RuntimeError):
    """The pack, or the engine that writes it, is missing from this install."""


def store_path(config):
    """The store a session addresses, resolved once for every caller.

    The configured memory project wins. A relative name sits beside the Tessera
    home, which is how the config editor heals a placeholder; an empty project
    means the home, which is where a bundled engine keeps its own store. Start,
    the seed command, and the engine lifecycle all resolve through this, so a
    seed can never land in a sibling of the store the session opens.
    """
    home = Path(getattr(config, 'home', '') or '~').expanduser()
    memory = getattr(config, 'aethermind', None)
    configured = str(getattr(memory, 'project', '') or '').strip()
    if not configured:
        return home
    # Same resolver the loader uses, so a config built by hand in a test and one
    # loaded from config.toml seed the same directory.
    return Path(resolve_store_path(configured, home))


def shelf_path(config):
    """The skill shelf the pack stages onto. Empty means `<home>/skills`."""
    home = Path(getattr(config, 'home', '') or '~').expanduser()
    configured = str(getattr(getattr(config, 'skills', None), 'shelf', '') or '').strip()
    return Path(configured).expanduser() if configured else home / SKILL_DIR


def ensure(config):
    """Write the pack into the store a session addresses. Never returns empty.

    Idempotent by hash, so this is safe on every engine start. Raises
    SeedUnavailable when the pack or the bundled engine is absent: a caller that
    cannot seed must not go on to serve a memoryless session.
    """
    return install(store_path(config), skills_dir=shelf_path(config))


def pack_path():
    """The pack inside the package, falling back to the source tree."""
    return asset_path('seeds/tessera-seed-layers.json')


def seed_ctx(value):
    """Seed contexts live under one `seed/` namespace, named once."""
    text = str(value or 'generic').strip().strip('/')
    if text.startswith('seed/'):
        return text
    return 'seed/' + text


def load_pack(path=None):
    path = Path(path or pack_path())
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        # A wheel that shipped no pack cannot start a session with judgment.
        # Naming the missing file is the difference between a five-second fix
        # and an install that looks like it works while answering from nothing.
        raise SeedUnavailable(
            'the seed pack is missing from this install: ' + str(path) + '. '
            'It ships as tessera/_assets/seeds/tessera-seed-layers.json inside any '
            'wheel built from this source, so either reinstall such a wheel, or set '
            'TESSERA_SEED_ENGINE and run from a source checkout that holds seeds/.'
        ) from error


def normalize(pack):
    """Turn the pack into engine payloads; skip any body that is empty."""
    layers = []
    for layer in pack.get('layers') or []:
        body = str(layer.get('body') or '').strip()
        if not body:
            continue
        conf = layer.get('conf')
        layers.append({
            'type': str(layer.get('type') or 'load-bearing'),
            'body': body,
            'ctx': seed_ctx(layer.get('ctx')),
            'conf': float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 1.0,
            'author': SEED_AUTHOR,
            'markers': list(SEED_MARKERS),
            'primitive': 'layer',
        })
    textures = []
    for texture in pack.get('textures') or []:
        body = str(texture.get('body') or '').strip()
        if not body:
            continue
        textures.append({
            'ctx': seed_ctx(texture.get('ctx')),
            'body': body,
            'author': SEED_AUTHOR,
        })
    return layers, textures


def present_layer_hashes(store_root):
    """Bodies already in the store, or none when the store cannot be read.

    An unreadable store is not this guard's verdict to make. Returning nothing
    lets the engine raise its own parse error, which is the one place that can
    say what is wrong with the record and whether to set the store aside.
    """
    store = Path(store_root).expanduser() / '.aethermind' / 'layers.aem'
    if not store.is_file():
        return set()
    try:
        with store.open('rb') as stream:
            doc = tomllib.load(stream)
    except (tomllib.TOMLDecodeError, OSError, ValueError):
        return set()
    rows = doc.get('layer')
    if not isinstance(rows, list):
        return set()
    return {hashlib.sha256(str(row.get('body') or '').encode()).hexdigest()
            for row in rows if isinstance(row, dict)}


def present_texture_hashes(store_root):
    """Texture entries are plain text blocks separated by a bare `---` line.

    The separator is punctuation, not part of the body, so it must not enter
    the hash or a rerun would append every seed texture a second time.
    """
    store = Path(store_root).expanduser() / '.aethermind' / 'texture.aem'
    if not store.is_file():
        return set()
    bodies = set()
    try:
        lines = store.read_text(encoding='utf-8').splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    index = 0
    while index < len(lines):
        header = lines[index]
        if header.startswith('20') and '[' in header and header.rstrip().endswith(']'):
            index += 1
            block = []
            while index < len(lines) and not (lines[index].startswith('20') and
                                              '[' in lines[index] and
                                              lines[index].rstrip().endswith(']')):
                block.append(lines[index])
                index += 1
            while block and block[-1].strip() == '---':
                block.pop()
            body = '\n'.join(block).strip()
            if body:
                bodies.add(hashlib.sha256(body.encode()).hexdigest())
            continue
        index += 1
    return bodies


def engine_lib_dir():
    """The bundled engine's importable lib directory, or None when absent.

    A source checkout has no `_assets` directory, so a developer can point
    TESSERA_SEED_ENGINE at an unpacked engine instead of building a wheel.
    """
    raw = os.environ.get('TESSERA_SEED_ENGINE', '')
    root = Path(raw).expanduser() if raw else asset_path(ENGINE_ASSET)
    lib = root / 'lib'
    return lib if (lib / 'aethermind' / 'cli.py').is_file() else None


def _aethermind(store_root, lib_dir):
    sys.path.insert(0, str(lib_dir))
    try:
        from aethermind import AetherMind
    finally:
        sys.path.pop(0)
    return AetherMind(str(Path(store_root).expanduser()), create=True)


@contextlib.contextmanager
def _seed_lock(store_root):
    """Serialize the hash-check-and-write, within and across processes.

    The guard is a read of the store followed by appends, so two installs
    starting at the same time would both see an empty store and both write the
    pack. The lock file is beside `.aethermind`, never inside it, so a store the
    engine owns is not given a file it did not write. A platform without fcntl
    runs unlocked: the engine refuses writes there anyway, so the lock is not
    what protects it.
    """
    if fcntl is None:
        yield
        return
    root = Path(store_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / '.aethermind-seed.lock').open('a')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _engine_error_types(lib_dir):
    """The engine's own store exceptions, imported from its tree."""
    sys.path.insert(0, str(lib_dir))
    try:
        from aethermind.core import AetherMindParseError, LockUnavailableError
    finally:
        sys.path.pop(0)
    return AetherMindParseError, LockUnavailableError


def lock_refusal_sentence():
    """The sentence this product uses when the platform cannot lock a store.

    Imported lazily: the sentence lives with the client that reports the lock
    backend, and that module should not be pulled in just to write a seed.
    """
    from .aethermind import EMBEDDED_LOCK_REFUSAL
    return EMBEDDED_LOCK_REFUSAL


def _quarantine(store_root):
    """Move an unreadable `.aethermind` directory aside and return where it went.

    Nothing is deleted: the directory keeps its bytes under a timestamped name,
    so the record that made the store unreadable is still there to inspect and
    is the rollback for this repair. Returns None when there was nothing to move.
    """
    store = Path(store_root).expanduser() / '.aethermind'
    if not store.exists():
        return None
    stamp = time.strftime('%Y%m%dT%H%M%S')
    target = store.with_name(store.name + CORRUPT_SUFFIX + stamp)
    counter = 1
    while target.exists():
        target = store.with_name(store.name + CORRUPT_SUFFIX + stamp + '-' + str(counter))
        counter += 1
    store.rename(target)
    return target


def install(store_root, skills_dir=None, pack=None, dry_run=False):
    """Write the pack into `store_root`; returns a count dict.

    `store_root` is the AetherMind project root, the same path the engine
    takes as `project_root`. `skills_dir` defaults to the store root's
    sibling `skills` directory, which is what an empty `[skills] shelf` means.
    """
    layers, textures = normalize(pack or load_pack())
    skills_source = Path(pack_path()).parent / SKILL_DIR
    result = {'layers_written': 0, 'layers_skipped': 0, 'textures_written': 0,
              'textures_skipped': 0, 'skills_staged': 0, 'skills_kept': 0,
              'dry_run': bool(dry_run)}
    if dry_run:
        known_layers = present_layer_hashes(store_root)
        known_textures = present_texture_hashes(store_root)
        result['layers_written'] = sum(
            1 for layer in layers
            if hashlib.sha256(layer['body'].encode()).hexdigest() not in known_layers)
        result['layers_skipped'] = len(layers) - result['layers_written']
        result['textures_written'] = sum(
            1 for texture in textures
            if hashlib.sha256(texture['body'].encode()).hexdigest() not in known_textures)
        result['textures_skipped'] = len(textures) - result['textures_written']
        shelf = Path(skills_dir or Path(store_root).expanduser() / SKILL_DIR)
        if skills_source.is_dir():
            for child in sorted(skills_source.iterdir()):
                if child.is_dir() and (child / 'SKILL.md').is_file():
                    if (shelf / child.name).exists():
                        result['skills_kept'] += 1
                    else:
                        result['skills_staged'] += 1
        return result

    lib_dir = engine_lib_dir()
    if lib_dir is None:
        # This refusal is read by whoever downloaded the wheel, so it must name a
        # remedy they can act on. "Build the wheel" was not one: a person holding a
        # wheel has already built or been given one. What they can do is point at an
        # engine tree they have, or fetch a wheel that carries one.
        raise SeedUnavailable(
            'the seed pack cannot be written because this install carries no AetherMind '
            'engine. Two remedies: (1) set TESSERA_SEED_ENGINE to an unpacked engine '
            'directory, meaning a folder holding lib/aethermind/cli.py (the engine tree '
            'is what the provenance manifest in docs/packaging pins); or (2) install a '
            'wheel that was built with the engine bundled, which carries '
            'tessera/_assets/aethermind-engine. A release wheel vendors that engine and '
            'refuses to build when it does not match the manifest in docs/packaging.')

    corrupt, lock_unavailable = _engine_error_types(lib_dir)
    with _seed_lock(store_root):
        try:
            _write(store_root, layers, textures, skills_dir, result)
        except lock_unavailable as error:
            # Native Windows has no fcntl, so the engine refuses every write.
            # Seeding is impossible there by design, and this is not the place
            # to change that: the engine still serves reads and the session's
            # own lock refusal is the sentence the operator already gets. Report
            # it as a labeled state so the caller does not read it as success.
            result['unavailable'] = True
            result['unavailable_reason'] = str(error).strip() or type(error).__name__
            return result
        except corrupt as error:
            # A store the engine cannot parse would leave every session
            # memoryless. Move it aside intact, then write the pack into a
            # fresh one. The quarantine directory is the rollback, so nothing
            # is destroyed.
            moved = _quarantine(store_root)
            if moved is None:
                raise
            for key in ('layers_written', 'layers_skipped', 'textures_written',
                        'textures_skipped', 'skills_staged', 'skills_kept'):
                result[key] = 0
            result['quarantined'] = str(moved)
            result['quarantined_because'] = str(error)
            _write(store_root, layers, textures, skills_dir, result)
    return result


def _write(store_root, layers, textures, skills_dir, result):
    """Append the payloads that are not present yet, then stage the skills."""
    skills_source = Path(pack_path()).parent / SKILL_DIR
    known_layers = present_layer_hashes(store_root)
    known_textures = present_texture_hashes(store_root)
    aether = _aethermind(store_root, engine_lib_dir())
    for layer in layers:
        if hashlib.sha256(layer['body'].encode()).hexdigest() in known_layers:
            result['layers_skipped'] += 1
            continue
        aether.write_layer(type=layer['type'], body=layer['body'], ctx=layer['ctx'],
                           conf=layer['conf'], markers=layer['markers'],
                           author=layer['author'], primitive=layer['primitive'])
        known_layers.add(hashlib.sha256(layer['body'].encode()).hexdigest())
        result['layers_written'] += 1
    for texture in textures:
        if hashlib.sha256(texture['body'].encode()).hexdigest() in known_textures:
            result['textures_skipped'] += 1
            continue
        aether.write_texture(body=texture['body'], ctx=texture['ctx'],
                             author=texture['author'])
        known_textures.add(hashlib.sha256(texture['body'].encode()).hexdigest())
        result['textures_written'] += 1

    shelf = Path(skills_dir or Path(store_root).expanduser() / SKILL_DIR).expanduser()
    if skills_source.is_dir():
        shelf.mkdir(parents=True, exist_ok=True)
        for child in sorted(skills_source.iterdir()):
            if not child.is_dir() or not (child / 'SKILL.md').is_file():
                continue
            destination = shelf / child.name
            if destination.exists():
                # The operator's own version of this skill wins; a seed is a
                # floor, so a rerun must not overwrite a customized shelf.
                result['skills_kept'] += 1
                continue
            shutil.copytree(child, destination)
            result['skills_staged'] += 1
    return result


def describe(result):
    if result.get('unavailable'):
        return ('seed pack: not written here: ' + lock_refusal_sentence()
                + ' (' + result.get('unavailable_reason', '') + ')')
    if result['dry_run']:
        return ('seed pack (dry run): %d layers and %d textures would be written, '
                '%d layers and %d textures already present; %d skills would be staged, '
                '%d kept' % (result['layers_written'], result['textures_written'],
                             result['layers_skipped'], result['textures_skipped'],
                             result['skills_staged'], result['skills_kept']))
    text = ('seed pack: %d layers and %d textures written, %d layers and %d textures '
            'already present; %d skills staged, %d kept from the shelf'
            % (result['layers_written'], result['textures_written'],
               result['layers_skipped'], result['textures_skipped'],
               result['skills_staged'], result['skills_kept']))
    if result.get('quarantined'):
        text += ('; the previous store could not be parsed and was moved aside '
                 'intact to ' + result['quarantined'])
    return text
