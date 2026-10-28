#!/usr/bin/env python3
"""Generate the Settings consumer map without loading an operator's config.

The map must never read a host config, credentials or runtime paths: it is
generated from a bare Config, so the JSON and markdown in docs/reference/
are install-neutral and can ship in an open-source release. Regenerate from
the repository root:

    .venv/bin/python scripts/gen-settings-consumers.py
"""

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tessera.config import Config
from tessera.settings_consumers import annotate_counts, build_map


def render_counts(mapping):
    counts = mapping['counts']
    consumed = counts.get('CONSUMED', 0)
    no_reader = counts.get('NO-READER', 0)
    unresolved = counts.get('UNRESOLVED', 0)
    total = sum(counts.values())
    return total, consumed, no_reader, unresolved


def render_markdown(mapping):
    total, consumed, no_reader, unresolved = render_counts(mapping)
    lines = [
        '# Settings consumer map', '',
        'Generated from `tessera/settings_model.py` (the `catalog` projection plus the',
        'record-row specs) and `tessera/settings_consumers.py`. The machine-readable',
        'map is `settings-consumers.json` beside this file. Regenerate both from the',
        'repository root:', '',
        '```sh', '.venv/bin/python scripts/gen-settings-consumers.py', '```', '',
        'Do not edit either by hand.', '',
        '## Why this exists', '',
        'Every settings field has to actually have function, not just capture text that',
        'reads well and is never used. A Settings',
        'field with no reader is a lie the page tells, and a first-run wizard that wrote',
        'one would move the lie from the page into setup. This map is the inventory the',
        "wizard's doctor (`tessera init --check`) consults: every field the page serves,",
        'and the code site that reads it — or an explicit NO-READER.', '',
        '## Evidence classes', '',
        '- The **catalog scalars** are flip-tested in `tests/test_settings_consumers.py`:',
        'each value is set to two variants, a production reader is called, and the output',
        'must move. That test is the proof; the citation in the JSON is the pointer.',
        '- The **record-row fields** (providers, accounts, credentials, mcp_servers,',
        'hosts, identities, oauth_accounts, service_accounts) have no flip test at HEAD.',
        'They are classified by named reader: a production code site, verified by',
        'reading the code, whose behaviour changes with the value. Writing,',
        'type-checking and rendering the value do not count as reads.', '',
        '## Summary at HEAD', '',
        f'{total} fields: {consumed} CONSUMED, {no_reader} NO-READER, {unresolved} UNRESOLVED.',
        '',
    ]
    if no_reader == 1 and unresolved == 0 and any(e['id'] == 'service_accounts' and e['status'] == 'NO-READER'
                                                   for e in mapping['fields']):
        lines.extend([
            'The single NO-READER is `service_accounts`, a record-container placeholder',
            'that serves an empty list with no actions; the page hint already says no',
            'third-party service account records exist yet. It is page furniture, not a',
            'config value, so `init --check` cannot flag it against any config.', '',
        ])
    lines.extend([
        '## Partial findings, recorded as debt', '',
        'Both live in `tests/settings_consumers_baseline.txt` and fail the gate if',
        'they change without the debt being fixed or re-recorded:', '',
        '- `computer_use.provider` — the dropdown\'s "Default" and "Native" options',
        '  resolve to the same backend on macOS. An `mcp:` name moves it, so the',
        '  field is live; the first option is not. The baseline line is tagged',
        '  `platform=darwin` because Linux Default selects the local desktop tools.',
        '- `mcp_servers.*.env` — an HTTP MCP server drops `env` (mcp_servers.py:31);',
        '  only stdio servers receive it (mcp_servers.py:32). The field hint says so.', '',
        '## The resolved contradiction', '',
        '`autonomy.world_view` is not a readerless value. The floor reads it.',
        'The label names that reader. This map records the same fact:',
        '`tessera/autonomy.py:1700`, inside `Autonomy._queue_floor_wake`, reads',
        '`config.autonomy.world_view`, trims it, caps it at 2000 characters and appends',
        'it to the floor wake as "What matters to you, in your own words". Treat that',
        'audit as a field-name inventory only.', '',
        '## The doctor', '',
        '`tessera init --check` keeps its existing placeholder-host, credential and DNS',
        'behaviour, and adds the consumer map: a config that SETS a NO-READER field is',
        'named and the exit code is nonzero. The map-coverage test fails first when a',
        'catalog spec appears without a reader entry, so a dead scalar cannot ship', '',
        '## Fields', '',
        '| Field | Status | Reader |',
        '| --- | --- | --- |',
    ])
    for entry in mapping['fields']:
        readers = '; '.join(reader['site'] or '(none)' for reader in entry['readers'])
        lines.append(f"| `{entry['id']}` | {entry['status']} | {readers} |")
    lines.extend(['', '## NO-READER and UNRESOLVED fields', ''])
    flagged = [entry for entry in mapping['fields'] if entry['status'] != 'CONSUMED']
    if not flagged:
        lines.append('None at HEAD.')
    for entry in flagged:
        lines.append(f"- `{entry['id']}` ({entry['status']}): {entry.get('note', '')}")
    lines.append('')
    return '\n'.join(lines)


def main():
    home = Path(tempfile.mkdtemp())
    config = Config(home=home, cwd=home)
    mapping = annotate_counts(build_map(config, {}))
    reference = ROOT / 'docs' / 'reference'
    reference.mkdir(parents=True, exist_ok=True)
    (reference / 'settings-consumers.json').write_text(
        json.dumps(mapping, indent=2, sort_keys=False, ensure_ascii=False) + '\n', encoding='utf-8')
    (reference / 'settings-consumers.md').write_text(render_markdown(mapping), encoding='utf-8')
    total, consumed, no_reader, unresolved = render_counts(mapping)
    print(f'{total} fields: {consumed} CONSUMED, {no_reader} NO-READER, {unresolved} UNRESOLVED')
    for entry in mapping['fields']:
        if entry['status'] != 'CONSUMED':
            print(f"  {entry['status']}: {entry['id']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
