#!/usr/bin/env python3
"""Ask Jev to find an element and check a question against a saved accessibility tree.

Usage:
  python scripts/jev_probe.py <tree.txt> <description> <check question>

The key comes from computer_use.jev.credential_ref, resolved like the other
Tessera credentials, or from the environment variable named by api_key_env
when that variable is set. The script prints the chosen index, probability,
and latency in milliseconds. It does not print the key.
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tessera.config import load_config
from tessera.cua_jev import api_key, check_tree, find_in_tree, redact


def main(argv):
    if len(argv) != 4:
        print('usage: python scripts/jev_probe.py <tree.txt> <description> <check question>', file=sys.stderr)
        return 2
    tree = Path(argv[1]).read_text(encoding='utf-8')
    config = load_config()
    settings = config.computer_use.jev
    key = api_key(settings, config.credentials)
    try:
        started = time.perf_counter()
        found = find_in_tree(tree, argv[2], settings, key)
        find_ms = int(round((time.perf_counter() - started) * 1000))
        started = time.perf_counter()
        checked = check_tree(tree, argv[3], settings, key)
        check_ms = int(round((time.perf_counter() - started) * 1000))
    except ValueError as error:
        print(redact(str(error), key), file=sys.stderr)
        return 1
    sure = 'not_sure' if found['unsure'] else 'sure'
    print(f"find index {found['index']} probability {found['probability']:.3f} {sure} latency_ms {find_ms}")
    print(f"check {checked['answer']} probability {checked['probability']:.3f} latency_ms {check_ms}")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main(sys.argv))
    except ValueError as error:
        print(redact(str(error), ''), file=sys.stderr)
        raise SystemExit(1) from None
