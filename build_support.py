"""Ship the source template, static UI, and vendored AetherMind engine."""

from pathlib import Path
import importlib.util
import json
import os
import subprocess

from setuptools.command.build_py import build_py


def _engine_manifest_module(root):
    """Load the export's checker, not whatever tessera is already installed."""
    path = Path(root) / 'tessera' / 'engine_manifest.py'
    spec = importlib.util.spec_from_file_location('tessera_engine_manifest_for_build', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(
            'Release wheel build cannot load tessera/engine_manifest.py. '
            'TESSERA_BUNDLE_SOURCE is an optional override and cannot replace that checker.')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_commit(root):
    """The commit this tree was built from, or 'unknown'.

    Evidence files keep having to say "the wheel carries no commit stamp", which
    makes a clean-machine result impossible to attach to a sha. Reading it at
    build time costs one git call and removes that whole class of ambiguity.
    """
    if os.environ.get('TESSERA_BUILD_COMMIT'):
        return os.environ['TESSERA_BUILD_COMMIT'].strip()
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=10)
    except Exception:
        return 'unknown'
    if result.returncode != 0:
        return 'unknown'
    return result.stdout.strip() or 'unknown'


def write_build_receipt(dest, root=None, *, override=False):
    """The receipt a wheel build writes next to the vendored engine.

    The receipt is a build product. It is not one of the engine's own files.
    ``dest`` is the engine root that will hold ``build-receipt.json``.
    """
    root = Path(__file__).resolve().parent if root is None else Path(root)
    module = _engine_manifest_module(root)
    manifest = module.load_manifest(root)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    payload = {
        'engine': 'bundled',
        'source_repo': manifest['source_repo'],
        'commit': manifest['commit'],
        'manifest': f'docs/packaging/{module.MANIFEST_NAME}',
        'files': len(manifest['files']),
        'source': 'TESSERA_BUNDLE_SOURCE' if override else 'vendored',
        'tessera_commit': source_commit(root),
    }
    receipt = dest / 'build-receipt.json'
    receipt.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    return receipt


class BuildPy(build_py):
    def stamp_files(self):
        root = Path(__file__).resolve().parent
        stamp = Path(self.build_lib) / 'tessera' / '_assets' / 'build-stamp.json'
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(json.dumps({'commit': source_commit(root)}, indent=2) + '\n')
        return [(str(stamp), None)]

    def runtime_files(self):
        root = Path(__file__).resolve().parent
        sources = [root / 'config.example.toml']
        sources.extend(path for path in (root / 'web').iterdir()
                       if path.is_file() and path.suffix in {'.html', '.css', '.js', '.webmanifest'})
        sources.extend(path for path in (root / 'web' / 'brand').rglob('*')
                       if path.is_file() and path.suffix in {'.png', '.svg'})
        # The seed pack is what a fresh install boots with, so it ships beside
        # the example config and the static UI rather than only in the source
        # tree. Without this an installed wheel has no seeds at all.
        seeds = root / 'seeds'
        if seeds.is_dir():
            sources.extend(path for path in sorted(seeds.rglob('*')) if path.is_file())
        for source in sorted(sources):
            target = Path(self.build_lib) / 'tessera' / '_assets' / source.relative_to(root)
            yield str(target), str(source)

    def _engine_plan(self):
        """Verify the vendored engine, or the optional TESSERA_BUNDLE_SOURCE override.

        Editable installs keep the files that already live in the checkout and
        do not run this check. A wheel build fails here instead of shipping a
        tree that does not match the manifest.
        """
        if self.editable_mode:
            return None
        root = Path(__file__).resolve().parent
        module = _engine_manifest_module(root)
        manifest = module.load_manifest(root)
        source, override = module.engine_source(root)
        module.verify_engine_tree(source, manifest, override=override)
        return module, root, manifest, source, override

    def engine_files(self):
        plan = self._engine_plan()
        if plan is None:
            return
        _module, _root, manifest, source, _override = plan
        dest = Path(self.build_lib) / 'tessera' / '_assets' / 'aethermind-engine'
        for name in manifest['files']:
            yield str(dest / name), str(source / name)

    def engine_metadata_files(self):
        """Receipt and provenance are build products. They are not engine bytes."""
        plan = self._engine_plan()
        if plan is None:
            return []
        module, root, _manifest, _source, override = plan
        dest = Path(self.build_lib) / 'tessera' / '_assets' / 'aethermind-engine'
        receipt = write_build_receipt(dest, root, override=override)
        provenance = dest / 'provenance.json'
        provenance.write_bytes(module.manifest_file(root).read_bytes())
        return [str(receipt), str(provenance)]

    def mcp_lock_files(self):
        """Copy Tessera's MCP lock beside the engine. It is not an engine byte."""
        if self.editable_mode:
            return []
        root = Path(__file__).resolve().parent
        module = _engine_manifest_module(root)
        source = module.verify_mcp_requirements_lock(root)
        target = Path(self.build_lib) / 'tessera' / '_assets' / module.MCP_REQUIREMENTS_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return [str(target)]

    def run(self):
        super().run()
        if not self.editable_mode:
            self.stamp_files()
            for target, source in [*self.runtime_files(), *self.engine_files()]:
                self.mkpath(str(Path(target).parent))
                self.copy_file(source, target)
            # After the copy, so a generated receipt cannot be mistaken for an
            # upstream file and cannot be skipped when the copy list is empty.
            self.engine_metadata_files()
            self.mcp_lock_files()

    def get_outputs(self, include_bytecode=True):
        outputs = super().get_outputs(include_bytecode)
        extra = {target for target, _ in self.stamp_files()}
        extra.update(self.engine_metadata_files())
        extra.update(self.mcp_lock_files())
        extra.update(target for target, _ in self.runtime_files())
        extra.update(target for target, _ in self.engine_files())
        return sorted(set(outputs) | extra)

    def get_output_mapping(self):
        mapping = super().get_output_mapping()
        if self.editable_mode:
            return {**mapping, **dict(self.runtime_files())}
        return {**mapping, **dict(self.runtime_files()), **dict(self.engine_files())}
