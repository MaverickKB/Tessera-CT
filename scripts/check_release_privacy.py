#!/usr/bin/env python3
"""Refuse a release copy that carries personal data.

Patterns come from release/privacy_patterns.json. That file is public input:
it describes classes of private data (home paths, private addresses, local
hostnames, email addresses) and never a private value.

An optional terms file adds literal words that must not ship, such as an
owner's name or a machine name. It is gitignored, one term or `re:` regex per
line, and it sits beside the pattern file by default. A build on a machine
without that file still runs every generic pattern; the terms file extends the
check rather than enabling it.

A copyright line may carry the holder's name. That allowance covers the name
only: the generic patterns still run on the same line, so a private address
written next to a copyright notice is still reported.

Hits print as path:line:label and never repeat the matched text, so a log of
this check cannot become the leak it reports.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


PATTERNS_NAME = 'privacy_patterns.json'
TERMS_NAME = 'privacy_terms.local.txt'
EMAIL_LABEL = 'email address'
TERM_LABEL = 'private term'
# Generated trees are not release input. A copy built by build_release.py has
# none of these, and scanning a working checkout should not read them either.
SKIP_DIRS = {'.git', '.venv', '__pycache__', 'node_modules', 'target', '.mypy_cache', '.pytest_cache',
             '.ruff_cache', '.hypothesis', 'build', 'dist'}


class PrivacyCheckError(Exception):
    """The check could not run. Reported as exit status 2, never as clean."""


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / 'release' / PATTERNS_NAME


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise PrivacyCheckError('pattern file not found: ' + str(path))
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivacyCheckError('pattern file is unreadable: ' + str(path)) from error
    if not isinstance(data, dict):
        raise PrivacyCheckError('pattern file is not a JSON object: ' + str(path))
    for key in ('copyright_line', 'patterns', 'skip_suffixes'):
        if key not in data:
            raise PrivacyCheckError('pattern file has no %r: %s' % (key, path))
    return data


def load_terms(path: Path) -> list:
    """Literal terms and `re:` regexes from the optional private terms file."""
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        raise PrivacyCheckError('terms file is unreadable: ' + str(path)) from error
    terms = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('re:'):
            expression = line[3:].strip()
            if not expression:
                continue
            terms.append((TERM_LABEL, re.compile(expression, re.IGNORECASE)))
            continue
        # The boundaries stop a short term matching inside a longer word or a
        # token. A term that begins or ends with punctuation still works, which a
        # bare \b would not allow.
        expression = r'(?<![A-Za-z0-9])' + re.escape(line) + r'(?![A-Za-z0-9])'
        terms.append((TERM_LABEL, re.compile(expression, re.IGNORECASE)))
    return terms


def compile_patterns(config: dict) -> list:
    compiled = []
    for entry in config['patterns']:
        if not isinstance(entry, dict) or 'label' not in entry or 'regex' not in entry:
            raise PrivacyCheckError('pattern entry needs a label and a regex')
        try:
            compiled.append((str(entry['label']), re.compile(entry['regex'])))
        except re.error as error:
            raise PrivacyCheckError('pattern regex does not compile: ' + str(entry['label'])) from error
    return compiled


def compile_copyright(config: dict):
    try:
        return re.compile(config['copyright_line'])
    except re.error as error:
        raise PrivacyCheckError('copyright line regex does not compile') from error


def match_starts_with_allowed(line: str, start: int, allowed: list) -> bool:
    for value in allowed:
        if not line.startswith(value, start):
            continue
        # A placeholder that ends in a name character allows its exact token,
        # not a longer name that starts with it, because the longer name is a
        # real home path. A placeholder that ends in punctuation describes a
        # shape and may be followed by prose.
        if value[-1].isalnum():
            after = line[start + len(value):start + len(value) + 1]
            if after and (after.isalnum() or after in '._-'):
                continue
        return True
    return False


def email_is_allowed(value: str, config: dict) -> bool:
    _local, _at, domain = value.partition('@')
    domain = domain.lower()
    allowed_domains = [str(item).lower() for item in config.get('allowed_email_domains', [])]
    for allowed in allowed_domains:
        if domain == allowed or domain.endswith('.' + allowed):
            return True
    # An icon set names itself 128x128@2x.png. A file name split at '@' is not
    # an address.
    lowered = value.lower()
    for suffix in config['skip_suffixes']:
        if lowered.endswith(str(suffix).lower()):
            return True
    return False


def scan_line(line: str, patterns: list, terms: list, config: dict, copyright_re) -> list:
    """Labels for one line. The matched text is never returned."""
    allowed = [str(item) for item in config.get('allowed_matches', [])]
    labels = []
    if copyright_re.match(line) is None:
        for label, regex in terms:
            if regex.search(line):
                labels.append(label)
    for label, regex in patterns:
        for match in regex.finditer(line):
            if match_starts_with_allowed(line, match.start(), allowed):
                continue
            if label == EMAIL_LABEL and email_is_allowed(match.group(0), config):
                continue
            labels.append(label)
            break
    return labels


def iter_text_files(root: Path, skip_suffixes: list):
    """Every file whose text can be read. A file that will not decode is an error."""
    skip = {str(suffix).lower() for suffix in skip_suffixes}
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if path.suffix.lower() in skip:
            continue
        try:
            yield relative, path.read_text(encoding='utf-8')
        except UnicodeDecodeError as error:
            raise PrivacyCheckError('file will not decode as text: ' + relative) from error


def scan_tree(root: Path, config: dict, terms: list) -> tuple:
    """Return (hits, scanned). A hit is (path, line number, label)."""
    patterns = compile_patterns(config)
    copyright_re = compile_copyright(config)
    hits = []
    scanned = 0
    for relative, text in iter_text_files(root, config['skip_suffixes']):
        scanned += 1
        for number, line in enumerate(text.splitlines(), 1):
            for label in scan_line(line, patterns, terms, config, copyright_re):
                hits.append((relative, number, label))
    return hits, scanned


def terms_path_for(config_path: Path) -> Path:
    return config_path.parent / TERMS_NAME


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description='Refuse a release copy that carries personal data')
    parser.add_argument('--root', required=True, help='directory to scan')
    parser.add_argument('--config', default='', help='pattern file (default release/privacy_patterns.json)')
    parser.add_argument('--terms', default='', help='optional private terms file')
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print('Release privacy check could not run: no such directory: ' + str(root), file=sys.stderr)
        return 2
    config_path = Path(args.config).resolve() if args.config else default_config_path()
    terms_path = Path(args.terms).resolve() if args.terms else terms_path_for(config_path)
    try:
        config = load_config(config_path)
        terms = load_terms(terms_path)
        hits, scanned = scan_tree(root, config, terms)
    except PrivacyCheckError as error:
        print('Release privacy check could not run: ' + str(error), file=sys.stderr)
        return 2

    if hits:
        print('Release privacy check failed:', file=sys.stderr)
        for relative, number, label in hits:
            print('%s:%d: %s' % (relative, number, label), file=sys.stderr)
        return 1
    note = ' with %d private term(s)' % len(terms) if terms else ' with no private terms file'
    print('Release privacy check passed: %d files%s.' % (scanned, note))
    return 0


if __name__ == '__main__':
    sys.exit(main())
