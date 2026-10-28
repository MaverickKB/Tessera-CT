#!/usr/bin/env python3
"""Copy one commit's tree, omitting release/exclude.txt paths.

The manifest is one path or glob per line. Blank lines and lines that start
with # are ignored. After the copy, tests, README files, and documentation
links are checked for a reference to an omitted path.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import posixpath
import re
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


LINK = re.compile(r'\[[^\]]*\]\(([^)]+)\)')
PATH_TOKEN = re.compile(r'(?:(?:\.{1,2}/)|(?:[A-Za-z0-9_.-]+/))+[A-Za-z0-9_.-]+')
README_NAMES = {'readme', 'readme.md'}
DOC_SUFFIXES = {'.md', '.markdown'}
TEST_SUFFIXES = {'.py', '.js', '.mjs', '.md'}


def load_patterns(path: Path) -> list[str]:
    patterns = []
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        patterns.append(line.strip().lstrip('./'))
    # An empty list is a real state, not a mistake: a public copy can carry the
    # whole tree. The path itself is still checked by the caller, so a typo in
    # the manifest's location fails loudly; only "exclude nothing" is allowed.
    return patterns


def normalize_rel(name: str) -> str:
    """Drop a leading ./ only. A leading dot is part of the name."""
    name = name.strip().replace('\\', '/')
    while name.startswith('./'):
        name = name[2:]
    return name


def path_excluded(rel: str, patterns: list[str]) -> bool:
    rel = normalize_rel(rel)
    if not rel or rel == '.':
        return False
    for pattern in patterns:
        if _match(rel, pattern):
            return True
    return False


def _match(rel: str, pattern: str) -> bool:
    if pattern.endswith('/**'):
        base = pattern[:-3].rstrip('/')
        return rel == base or rel.startswith(base + '/')
    if any(ch in pattern for ch in '*?['):
        return fnmatch.fnmatch(rel, pattern)
    return rel == pattern


def _safe_target(dest: Path, name: str) -> Path:
    if name.startswith('/') or posixpath.isabs(name):
        raise SystemExit('build_release: refusing absolute path: ' + name)
    parts = PurePosixPath(name).parts
    if '..' in parts:
        raise SystemExit('build_release: refusing parent path: ' + name)
    target = dest.joinpath(*parts)
    return target


def copy_commit(repo: Path, commit: str, dest: Path, patterns: list[str]) -> int:
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit('build_release: output is not empty: ' + str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ['git', 'archive', '--format=tar', commit],
        cwd=repo, check=True, capture_output=True,
    )
    copied = 0
    payload = io.BytesIO(archive.stdout)
    with tarfile.open(fileobj=payload, mode='r|') as tar:
        for member in tar:
            name = normalize_rel(member.name)
            if not name or name.endswith('/'):
                continue
            if path_excluded(name, patterns):
                continue
            target = _safe_target(dest, name)
            if member.issym() or member.islnk():
                link = member.linkname
                if path_excluded(posixpath.normpath(posixpath.join(posixpath.dirname(name), link)), patterns):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(link)
                copied += 1
                continue
            if not member.isreg():
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                raise SystemExit('build_release: could not read ' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
            mode = member.mode & 0o777
            if mode:
                target.chmod(mode)
            copied += 1
    return copied


def _scan_files(root: Path, patterns: list[str]) -> list[Path]:
    found = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path_excluded(rel, patterns):
            continue
        name = path.name.lower()
        if name in README_NAMES or name.startswith('readme.'):
            found.append(path)
            continue
        if path.name == 'DEPLOY.md':
            found.append(path)
            continue
        if rel.startswith('docs/') and path.suffix.lower() in DOC_SUFFIXES:
            found.append(path)
            continue
        if (rel.startswith('tests/') or rel.startswith('web/tests/')) and path.suffix.lower() in TEST_SUFFIXES:
            found.append(path)
    return found


def _tokens(line: str) -> list[str]:
    found = []
    for match in LINK.finditer(line):
        found.append(match.group(1).strip())
    for match in PATH_TOKEN.finditer(line):
        found.append(match.group(0).strip())
    return found


def _clean_token(token: str) -> str:
    token = token.strip().strip('\'"<>')
    if not token or '://' in token or token.startswith('mailto:') or token.startswith('#'):
        return ''
    token = token.split('#', 1)[0].split('?', 1)[0].strip()
    if not token or token.startswith('/'):
        return ''
    return token


def _candidates(source_rel: str, token: str, *, resolve: bool) -> list[str]:
    raw = token.strip()
    token = _clean_token(token)
    if not token:
        return []
    found = []
    plain = posixpath.normpath(normalize_rel(token))
    if plain and not plain.startswith('..'):
        found.append(plain)
    # Join with the source directory for a markdown link or an explicit
    # ./ or ../ reference. A slash in ordinary prose is not a repo path.
    if resolve or raw.startswith('./') or raw.startswith('../'):
        parent = str(PurePosixPath(source_rel).parent)
        if parent == '.':
            parent = ''
        joined = posixpath.normpath(posixpath.join(parent, token))
        if joined and not joined.startswith('..') and joined not in found:
            found.append(joined)
    return found


def reference_hits(root: Path, patterns: list[str]) -> list[str]:
    hits = []
    seen = set()
    for path in _scan_files(root, patterns):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            linked = {match.group(1).strip() for match in LINK.finditer(line)}
            for token in _tokens(line):
                for candidate in _candidates(rel, token, resolve=token in linked):
                    if path_excluded(candidate, patterns):
                        key = (rel, number, candidate)
                        if key not in seen:
                            seen.add(key)
                            hits.append('%s:%d: %s' % (rel, number, candidate))
                        break
    return hits


def privacy_check(repo: Path, dest: Path) -> int:
    """Run the personal-data scan over the built copy."""
    script = Path(__file__).resolve().parent / 'check_release_privacy.py'
    config = repo / 'release' / 'privacy_patterns.json'
    result = subprocess.run(
        [sys.executable, str(script), '--root', str(dest), '--config', str(config)],
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Copy a commit tree minus release excludes')
    parser.add_argument('--output', required=True, help='directory to write')
    parser.add_argument('--commit', default='HEAD', help='commit to copy (default HEAD)')
    parser.add_argument('--repo', default='.', help='git repository (default .)')
    parser.add_argument('--exclude', default='', help='exclude manifest (default release/exclude.txt)')
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    dest = Path(args.output).resolve()
    exclude = Path(args.exclude) if args.exclude else repo / 'release' / 'exclude.txt'
    if not exclude.is_file():
        raise SystemExit('build_release: exclude manifest not found: ' + str(exclude))
    patterns = load_patterns(exclude)
    copied = copy_commit(repo, args.commit, dest, patterns)
    print('Release copy: %d files in %s' % (copied, dest))
    privacy_status = privacy_check(repo, dest)
    if privacy_status != 0:
        if privacy_status == 2:
            print('Release privacy check could not run.', file=sys.stderr)
        return 1
    hits = reference_hits(dest, patterns)
    if hits:
        print('Release reference check failed:', file=sys.stderr)
        for hit in hits:
            print(hit, file=sys.stderr)
        return 1
    print('Release reference check passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
