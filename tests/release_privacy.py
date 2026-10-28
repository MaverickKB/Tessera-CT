"""Shared helpers for the release privacy scan.

The tests that guard a shipped template or seed pack use the same scanner the
release build uses, so a pattern added for one is enforced by the other. The
private terms file is gitignored and optional: without it the scanner still
applies every class pattern (paths, addresses, hostnames, email addresses).
"""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_scanner():
    path = ROOT / 'scripts' / 'check_release_privacy.py'
    spec = importlib.util.spec_from_file_location('check_release_privacy', path)
    if spec is None or spec.loader is None:
        raise AssertionError('could not load the privacy scanner at ' + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def privacy_hits(target):
    """Privacy hits for one file or a directory tree, as (path, line, label)."""
    scanner = load_scanner()
    config = scanner.load_config(ROOT / 'release' / 'privacy_patterns.json')
    terms = scanner.load_terms(ROOT / 'release' / 'privacy_terms.local.txt')
    path = Path(target)
    if not path.is_file():
        hits, _scanned = scanner.scan_tree(path, config, terms)
        return hits
    patterns = scanner.compile_patterns(config)
    copyright_line = scanner.compile_copyright(config)
    hits = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        for label in scanner.scan_line(line, patterns, terms, config, copyright_line):
            hits.append((str(path), number, label))
    return hits


def copyright_notice(root=ROOT):
    """The copyright line LICENSE carries, so tests never restate the name."""
    for line in (Path(root) / 'LICENSE').read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.startswith('Copyright '):
            return stripped
    raise AssertionError('LICENSE has no copyright line')
