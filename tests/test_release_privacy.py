"""The release privacy scan refuses private data and allows a copyright line.

These tests drive the same scanner the release build runs. A pattern added for
one path is therefore enforced by the other.
"""

import json
from pathlib import Path
import sys

from release_privacy import load_scanner, privacy_hits


ROOT = Path(__file__).resolve().parents[1]
# The shape of an absolute macOS user home, split so this file carries none.
USER_HOME = '/' + 'Users/'


def write_config(tmp_path, **overrides):
    config = json.loads((ROOT / 'release' / 'privacy_patterns.json').read_text(encoding='utf-8'))
    config.update(overrides)
    path = tmp_path / 'privacy_patterns.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    return path


def scan(tmp_path, text, terms=''):
    """Run the scanner over one file. Returns the privacy hit labels."""
    scanner = load_scanner()
    config = scanner.load_config(write_config(tmp_path))
    terms_path = tmp_path / scanner.TERMS_NAME
    if terms:
        terms_path.write_text(terms, encoding='utf-8')
    loaded_terms = scanner.load_terms(terms_path)
    patterns = scanner.compile_patterns(config)
    copyright_line = scanner.compile_copyright(config)
    labels = []
    for line in text.splitlines():
        labels.extend(scanner.scan_line(line, patterns, loaded_terms, config, copyright_line))
    return labels


def test_a_user_home_path_is_reported(tmp_path):
    assert scan(tmp_path, 'export PATH=' + USER_HOME + 'someone-real/bin\n') == ['user home path']


def test_private_and_tailnet_addresses_are_reported(tmp_path):
    assert 'private IPv4 address' in scan(tmp_path, 'host = ' + '192.' + '168.' + '1.20\n')
    assert 'tailnet IPv4 address' in scan(tmp_path, 'host = ' + '100.' + '101.7.9\n')


def test_a_local_hostname_is_reported(tmp_path):
    assert scan(tmp_path, 'reach ' + 'build-box' + '.local for the logs\n') == ['mDNS hostname']


def test_a_personal_email_is_reported_and_an_example_address_is_not(tmp_path):
    assert scan(tmp_path, 'mail ' + 'person' + '@' + 'real-domain.test\n') == ['email address']
    assert scan(tmp_path, 'mail user:secret@' + 'example.com' + '\n') == []


def test_a_local_terms_file_reports_its_words(tmp_path):
    assert scan(tmp_path, 'the private word appears here\n', terms='exampleterm\n') == []
    assert scan(tmp_path, 'exampleterm appears here\n', terms='exampleterm\n') == ['private term']


def test_a_copyright_line_may_carry_the_holder_and_the_class_patterns_still_run(tmp_path):
    holder = 'Copyright 2026 ' + 'Example Holder' + '\n'
    assert scan(tmp_path, holder) == []
    assert scan(tmp_path, holder + 'see ' + '192.' + '168.' + '9.9 for details\n') == ['private IPv4 address']


def test_the_repository_ships_no_private_data_outside_the_excluded_paths():
    """A private word or address in a copied file fails the release build."""
    sys.path.insert(0, str(ROOT / 'scripts'))
    from build_release import load_patterns, path_excluded

    scanner = load_scanner()
    config = scanner.load_config(ROOT / 'release' / 'privacy_patterns.json')
    terms = scanner.load_terms(ROOT / 'release' / scanner.TERMS_NAME)
    patterns = scanner.compile_patterns(config)
    copyright_line = scanner.compile_copyright(config)
    excludes = load_patterns(ROOT / 'release' / 'exclude.txt')
    # These files are gitignored on purpose and never part of a release copy.
    local_only = {scanner.TERMS_NAME, '.tessera-hardwire-patterns.local', '.tessera-private-hosts.local'}
    hits = []
    for relative, text in scanner.iter_text_files(ROOT, config['skip_suffixes']):
        if Path(relative).name in local_only or path_excluded(relative, excludes):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            labels = scanner.scan_line(line, patterns, terms, config, copyright_line)
            for label in labels:
                hits.append('%s:%d: %s' % (relative, number, label))
    assert hits == []
