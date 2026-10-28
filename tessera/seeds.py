"""Install the packaged seed pack into a store, from an installed wheel.

The seed pack ships inside the package (`tessera/_assets/seeds/`), but the
installer that writes it lived only in the source tree at
`scripts/install-seed-layers.py`. An installed Tessera therefore carried the
rules and had no command to place them, so a stranger's first install booted
with no judgment at all. This module is the packaged half: it reads the pack
from the asset directory and writes it through the bundled engine, which is
stdlib-only, so no venv and no MCP server are needed to bootstrap.

Two properties are load-bearing and both are tested:

- A rerun never rewrites a body already in the store. The pack is a floor, and
  the operator's own version of a rule wins over the seed.
- A skill directory that already exists on the shelf is left untouched, for the
  same reason.

The pack names no person, host, or companion; that is guarded separately in
tests/test_seed_pack.py.
"""

import hashlib
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from .assets import asset_path

SEED_AUTHOR = 'seed'
SEED_MARKERS = ['seed']
TEXTURE_MARKERS = ['seed']
SKILL_DIR = 'skills'
ENGINE_ASSET = 'aethermind-engine'


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
    return json.loads(path.read_text(encoding='utf-8'))


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
    store = Path(store_root).expanduser() / '.aethermind' / 'layers.aem'
    if not store.is_file():
        return set()
    with store.open('rb') as stream:
        doc = tomllib.load(stream)
    return {hashlib.sha256(str(row.get('body') or '').encode()).hexdigest()
            for row in doc.get('layer') or []}


def present_texture_hashes(store_root):
    """Texture entries are plain text blocks separated by a bare `---` line.

    The separator is punctuation, not part of the body, so it must not enter
    the hash or a rerun would append every seed texture a second time.
    """
    store = Path(store_root).expanduser() / '.aethermind' / 'texture.aem'
    if not store.is_file():
        return set()
    bodies = set()
    lines = store.read_text(encoding='utf-8').splitlines()
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
        raise RuntimeError(
            'the seed pack cannot be written because this install carries no AetherMind '
            'engine. Two remedies: (1) set TESSERA_SEED_ENGINE to an unpacked engine '
            'directory, meaning a folder holding lib/aethermind/cli.py (the engine tree '
            'is what the provenance manifest in docs/packaging pins); or (2) install a '
            'wheel that was built with the engine bundled, which carries '
            'tessera/_assets/aethermind-engine. A release wheel vendors that engine and '
            'refuses to build when it does not match the manifest in docs/packaging.')

    known_layers = present_layer_hashes(store_root)
    known_textures = present_texture_hashes(store_root)
    aether = _aethermind(store_root, lib_dir)
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
    if result['dry_run']:
        return ('seed pack (dry run): %d layers and %d textures would be written, '
                '%d layers and %d textures already present; %d skills would be staged, '
                '%d kept' % (result['layers_written'], result['textures_written'],
                             result['layers_skipped'], result['textures_skipped'],
                             result['skills_staged'], result['skills_kept']))
    return ('seed pack: %d layers and %d textures written, %d layers and %d textures '
            'already present; %d skills staged, %d kept from the shelf'
            % (result['layers_written'], result['textures_written'],
               result['layers_skipped'], result['textures_skipped'],
               result['skills_staged'], result['skills_kept']))
