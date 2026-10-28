"""Hash contract for the vendored AetherMind engine.

The files under tessera/_assets/aethermind-engine are one public commit, copied
unchanged. This module reads them and the manifest. It does not import the
engine and it does not write those files.
"""

from pathlib import Path
import hashlib
import json
import os


MANIFEST_NAME = 'aethermind-engine-manifest.json'
VENDORED_DIR_NAME = 'aethermind-engine'
SOURCE_REPO = 'https://github.com/MaverickKB/aethermind-engine'
MCP_REQUIREMENTS_NAME = 'aethermind-mcp-requirements.lock'


class EngineBundleError(RuntimeError):
    """A wheel build or a tree check refused an unverified AetherMind engine."""


def repo_root():
    return Path(__file__).resolve().parents[1]


def manifest_file(root=None):
    root = repo_root() if root is None else Path(root)
    return root / 'docs' / 'packaging' / MANIFEST_NAME


def vendored_dir(root=None):
    root = repo_root() if root is None else Path(root)
    return root / 'tessera' / '_assets' / VENDORED_DIR_NAME


def load_manifest(root=None):
    path = manifest_file(root)
    relative = f'docs/packaging/{MANIFEST_NAME}'
    if not path.is_file():
        raise EngineBundleError(
            f'The engine manifest {relative} is missing. '
            'TESSERA_BUNDLE_SOURCE is an optional override and cannot replace the manifest. '
            'A release wheel will not ship an unverified AetherMind engine.')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise EngineBundleError(f'The engine manifest {relative} is not valid JSON.') from error
    files = data.get('files')
    commit = data.get('commit')
    if data.get('algorithm') != 'sha256' or not isinstance(files, dict) or not files:
        raise EngineBundleError(f'The engine manifest {relative} has no sha256 file map.')
    if not isinstance(commit, str) or len(commit) != 40 or any(ch not in '0123456789abcdef' for ch in commit):
        raise EngineBundleError(f'The engine manifest {relative} does not record a full commit SHA.')
    if not data.get('source_repo'):
        raise EngineBundleError(f'The engine manifest {relative} does not record a source repo.')
    return data


def engine_source(root):
    """The tree to hash-check. An empty override means the vendored copy."""
    raw = os.environ.get('TESSERA_BUNDLE_SOURCE', '').strip()
    if raw:
        return Path(raw).expanduser().resolve(), True
    return vendored_dir(root), False


# Compiled bytecode and OS metadata are local build residue, never engine bytes.
# A test run writes __pycache__ into the vendored tree, and a wheel copies only
# the manifest's files, so an extra .pyc must not read as tampering when the
# release flow is "run the tests, then build the wheel".
_IGNORED_DIRECTORIES = {'__pycache__'}
_IGNORED_SUFFIXES = {'.pyc', '.pyo'}
_IGNORED_NAMES = {'.DS_Store'}


def _tree_files(source):
    found = {}
    for path in sorted(source.rglob('*')):
        if not (path.is_symlink() or path.is_file()):
            continue
        relative = path.relative_to(source)
        if any(part in _IGNORED_DIRECTORIES for part in relative.parts[:-1]):
            continue
        if path.name in _IGNORED_NAMES or path.suffix in _IGNORED_SUFFIXES:
            continue
        found[relative.as_posix()] = path
    return found


def verify_engine_tree(source, manifest, *, override=False):
    """Raise EngineBundleError unless `source` is exactly the manifest's bytes."""
    source = Path(source)
    relative = f'docs/packaging/{MANIFEST_NAME}'
    repo = manifest.get('source_repo') or ''
    commit = manifest.get('commit') or ''
    if override:
        where = 'TESSERA_BUNDLE_SOURCE'
        role = 'TESSERA_BUNDLE_SOURCE is an optional override, never a requirement. '
    else:
        where = 'the vendored tree tessera/_assets/aethermind-engine'
        role = 'TESSERA_BUNDLE_SOURCE is an optional override and is not set. '
    intro = f'{where} does not match {relative} (source repo {repo}, commit {commit}). {role}'
    if not source.is_dir():
        raise EngineBundleError(
            intro + 'The engine source directory is missing. '
            'A release wheel will not ship without the embedded AetherMind engine.')
    files = manifest['files']
    present = _tree_files(source)
    missing = []
    mismatched = []
    for name, expected in files.items():
        path = present.get(name)
        if path is None or path.is_symlink() or not path.is_file():
            missing.append(name)
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatched.append(name)
    extra = sorted(set(present) - set(files))
    if not missing and not mismatched and not extra:
        return
    parts = []
    if missing:
        parts.append('missing: ' + ', '.join(missing))
    if mismatched:
        parts.append('hash mismatch: ' + ', '.join(mismatched))
    if extra:
        parts.append('unexpected: ' + ', '.join(extra))
    raise EngineBundleError(
        intro + '; '.join(parts) + '. A release wheel will not ship an unverified engine.')


def source_mcp_requirements_lock(root=None):
    """Tessera-owned lock. It is not a file of the vendored engine commit."""
    root = repo_root() if root is None else Path(root)
    return root / 'docs' / 'packaging' / MCP_REQUIREMENTS_NAME


def vendored_mcp_requirement(root=None):
    """The exact mcp extra declared by the vendored engine pyproject."""
    root = repo_root() if root is None else Path(root)
    pyproject = vendored_dir(root) / 'pyproject.toml'
    if not pyproject.is_file():
        raise EngineBundleError(
            'The vendored engine pyproject.toml is missing, so Tessera cannot read '
            'the MCP server requirement. TESSERA_BUNDLE_SOURCE does not supply it.')
    import tomllib
    data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    reqs = ((data.get('project') or {}).get('optional-dependencies') or {}).get('mcp')
    if not isinstance(reqs, list) or len(reqs) != 1 or not isinstance(reqs[0], str):
        raise EngineBundleError(
            'The vendored engine pyproject.toml does not declare exactly one mcp extra requirement.')
    requirement = reqs[0].strip()
    if not requirement.startswith('mcp==') or any(ch.isspace() for ch in requirement):
        raise EngineBundleError(
            f'The vendored engine mcp extra must be an exact pin, not {requirement!r}.')
    return requirement


def parse_hashed_lock(text):
    """Return (requirement, hashes) from a pip requirements file.

    Every requirement must carry at least one sha256 hash. A hash with no
    requirement, or a requirement with none, is an error.
    """
    items = []
    pending = None
    hashes = []

    def close():
        nonlocal pending, hashes
        if pending is None and not hashes:
            return
        if pending is None:
            raise EngineBundleError(
                'The MCP requirements lock has a hash with no requirement.')
        if not hashes:
            raise EngineBundleError(
                f'The MCP requirements lock requirement {pending} has no sha256 hash.')
        for item in hashes:
            digest = item[len('--hash=sha256:'):]
            if not item.startswith('--hash=sha256:') or len(digest) != 64 or any(
                    ch not in '0123456789abcdef' for ch in digest):
                raise EngineBundleError(
                    f'The MCP requirements lock requirement {pending} has a hash that is not sha256.')
        items.append((pending, list(hashes)))
        pending = None
        hashes = []

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.endswith('\\'):
            stripped = stripped[:-1].strip()
        if stripped.startswith('--hash='):
            hashes.append(stripped)
            continue
        close()
        pending = stripped
    close()
    return items


def verify_mcp_requirements_lock(root=None):
    """The checkout lock pins the vendored mcp extra and hashes every requirement."""
    root = repo_root() if root is None else Path(root)
    path = source_mcp_requirements_lock(root)
    relative = f'docs/packaging/{MCP_REQUIREMENTS_NAME}'
    if not path.is_file():
        raise EngineBundleError(
            f'Tessera is missing its pinned AetherMind MCP requirements lock at {relative}. '
            'The vendored engine does not ship mcp/requirements-mcp.lock. '
            'TESSERA_BUNDLE_SOURCE does not supply this lock. '
            'A release wheel will not ship an embedded engine that cannot provision.')
    pin = vendored_mcp_requirement(root)
    items = parse_hashed_lock(path.read_text(encoding='utf-8'))
    names = [item[0].split(';', 1)[0].strip() for item in items]
    if pin not in names:
        raise EngineBundleError(
            f'{relative} does not pin the vendored engine mcp extra {pin}. '
            'Build the lock from that extra. TESSERA_BUNDLE_SOURCE does not supply it.')
    return path


def vendored_status(root=None):
    """'match', 'mismatch', or 'absent' for the in-repo engine tree.

    An installed wheel keeps the engine under the package and does not carry
    this manifest. A missing manifest there is 'absent'; the build receipt is
    what tells an install the bytes were checked.
    """
    root = repo_root() if root is None else Path(root)
    if not manifest_file(root).is_file():
        return 'absent'
    try:
        verify_engine_tree(vendored_dir(root), load_manifest(root), override=False)
    except EngineBundleError:
        return 'mismatch'
    return 'match'


def engine_packaging(config):
    """How this process relates to the embedded engine.

    'bundled' — the configured engine can start: a wheel build receipt, an
    explicit bundled_path with a receipt, or a home-local verified bundle
    created from a source checkout by ensure_local_engine_bundle.
    'present' — this checkout's vendored tree matches the manifest, but no
    home-local bundle has been materialized yet. The wheel build is not the
    only way to start the engine.
    'missing' — neither of those is true.
    """
    from .bundled_engine import _engine_root
    if _engine_root(config) is not None:
        return 'bundled'
    if vendored_status() == 'match':
        return 'present'
    return 'missing'
