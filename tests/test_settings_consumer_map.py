"""The consumer map is generated from settings_model, and covers what the page serves.

Three guarantees:

1. The map derives its field inventory from the live ``catalog`` projection,
   so a spec without a map entry fails here, not in a shipped wizard.
2. A NO-READER classification is honest: the field named NO-READER is a record
   container with no consumer, and the one named in the stale audit
   (autonomy.world_view) really has the reader the map cites.
3. ``init --check`` consults the map and still keeps its existing placeholder,
   credential and DNS behaviour.
"""

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
from tessera.config import Config, BackendConfig
from tessera.settings_model import catalog
from tessera.settings_consumers import build_map, annotate_counts, unread_set_fields


def bare(tmp_path):
    return Config(home=tmp_path, cwd=tmp_path)


def mapping(tmp_path):
    return annotate_counts(build_map(bare(tmp_path), {}))


def test_map_covers_every_served_catalog_spec(tmp_path):
    """Coverage ties to the live Spec count, not a typed number: every catalog
    id appears in the map exactly once, and nothing is silently UNRESOLVED."""
    config = bare(tmp_path)
    served = [spec.id for spec, _value in catalog(config, {})]
    assert served, 'catalog is empty; the map would be vacuously complete'
    entries = mapping(tmp_path)['fields']
    by_id = {entry['id']: entry for entry in entries}
    missing = [field_id for field_id in served if by_id.get(field_id, {}).get('status') == 'UNRESOLVED']
    assert not missing, f'catalog specs with no map entry: {missing}'
    # The record-row classes are in the map too; the total is the served count plus them.
    record_rows = [entry['id'] for entry in entries
                   if not any(entry['id'] == field_id for field_id in served)]
    assert len(entries) == len(served) + len(record_rows)


def test_checked_in_json_matches_the_generated_map(tmp_path):
    """docs/reference/settings-consumers.json must not drift from the source."""
    checked_in = json.loads((ROOT / 'docs' / 'reference' / 'settings-consumers.json').read_text())
    generated = mapping(tmp_path)
    assert [entry['id'] for entry in checked_in['fields']] == [entry['id'] for entry in generated['fields']]
    assert checked_in['counts'] == generated['counts']


def test_no_catalog_field_is_left_without_a_reader(tmp_path):
    """The page used to carry one placeholder container with no consumer.

    That container became the real accounts registry, so the map has no dead
    field left. A new dead field still fails: ``test_doctor_names_a_set_but_unread_field``
    plants one and asserts the doctor names it.
    """
    entries = {entry['id']: entry for entry in mapping(tmp_path)['fields']}
    no_readers = [field_id for field_id, entry in entries.items() if entry['status'] == 'NO-READER']
    assert no_readers == [], f'unexpected NO-READER set: {no_readers}'


def _cited_lines(site):
    match = re.match(r'(\S+?):(\d+(?:,:\d+)*)', site)
    assert match, site
    numbers = [int(part) for part in match.group(2).split(',:')]
    source = (ROOT / match.group(1)).read_text(encoding='utf-8').splitlines()
    return [source[number - 1] for number in numbers]


def test_every_mcp_field_settings_accepts_is_in_the_consumer_map(tmp_path):
    """A Plugins field with no consumer row is a setting the page can save and nothing reads.

    ``name`` is the table key, not a field. Every other key in ``MCP_KEYS`` is
    editable on the row and must be CONSUMED, with a citation that points at a
    line that actually mentions the value.
    """
    from tessera.settings_model import MCP_KEYS
    entries = {entry['id']: entry for entry in mapping(tmp_path)['fields']}
    missing = []
    for key in sorted(MCP_KEYS - {'name'}):
        field_id = f'mcp_servers.*.{key}'
        entry = entries.get(field_id)
        readers = (entry or {}).get('readers') or []
        sites = [reader.get('site') or '' for reader in readers]
        if not entry or entry['status'] != 'CONSUMED' or not any(sites):
            missing.append(field_id)
            continue
        # args is the tail of server.command, so the launch line names command.
        if key == 'args':
            continue
        cited = '\n'.join(line for site in sites for line in _cited_lines(site))
        assert key in cited, (field_id, cited)
    assert missing == [], missing


def test_mcp_enabled_citation_points_at_the_gates(tmp_path):
    """enabled cites the session offer and the registration gate, both of which read the switch."""
    entries = {entry['id']: entry for entry in mapping(tmp_path)['fields']}
    sites = [reader['site'] for reader in entries['mcp_servers.*.enabled']['readers']]
    lines = [line for site in sites for line in _cited_lines(site)]
    assert any('server.enabled and server.session_tools' in line for line in lines), lines
    assert any('servers.values() if server.enabled' in line for line in lines), lines
    assert all('served only when enabled' not in site for site in sites)
    session = [line for reader in entries['mcp_servers.*.session_tools']['readers']
               for line in _cited_lines(reader['site'])]
    assert any('server.session_tools' in line for line in session), session
    token = [line for reader in entries['mcp_servers.*.token_env']['readers']
             for line in _cited_lines(reader['site'])]
    assert any('token_env' in line for line in token), token


def test_the_stale_audit_finding_is_resolved_with_a_real_reader(tmp_path):
    """The audit said autonomy.world_view was dead. The map must cite the reader,
    and the cited line must actually read the attribute."""
    entries = {entry['id']: entry for entry in mapping(tmp_path)['fields']}
    assert entries['autonomy.world_view']['status'] == 'CONSUMED'
    site = entries['autonomy.world_view']['readers'][0]['site']
    path_part, _, rest = site.partition(':')
    line_no = int(rest.split(' ', 1)[0])
    source = (ROOT / path_part).read_text(encoding='utf-8').splitlines()
    assert 'autonomy.world_view' in source[line_no - 1], source[line_no - 1]


def run_cli(home, extra_env=None):
    env = {k: v for k, v in __import__('os').environ.items()
           if k not in ('MODEL_API_KEY', 'TESSERA_HOME')}
    env.update(extra_env or {})
    return subprocess.run([sys.executable, '-m', 'tessera', '--home', str(home), 'init', '--check'],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)


def test_doctor_exits_zero_for_a_valid_config(tmp_path):
    engine = tmp_path / 'engine'
    (engine / 'mcp').mkdir(parents=True)
    (engine / 'mcp' / 'aethermind_mcp.py').write_text('# packaged engine stand-in\n')
    (engine / 'build-receipt.json').write_text('{}\n')
    (tmp_path / 'config.toml').write_text(
        '[backends.primary]\nbase_url="http://127.0.0.1:8761/v1"\nmodel="grok-4.7"\n'
        'api_key_env="MODEL_API_KEY"\nallow_api_key=true\n[roles]\nmain="primary"\n'
        '[aethermind]\nbundled_path="engine"\n')
    result = run_cli(tmp_path, {'MODEL_API_KEY': 'a-real-looking-key', 'TESSERA_HOME': str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'can answer' in result.stdout


def test_doctor_still_exits_one_naming_a_placeholder_host(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'tessera', '--home', str(tmp_path), 'init'],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    checked = run_cli(tmp_path)
    assert checked.returncode == 1
    assert 'models.example.com' in checked.stderr


def test_doctor_exits_one_naming_an_unresolvable_host(tmp_path):
    """An unresolvable host fails with the resolve message; the valid twin passes."""
    (tmp_path / 'config.toml').write_text(
        '[backends.primary]\nbase_url="http://card123-nonexistent.invalid/v1"\n'
        'model="test"\n[roles]\nmain="primary"\n')
    result = run_cli(tmp_path)
    assert result.returncode == 1
    assert 'cannot resolve endpoint host' in result.stderr, result.stderr


def test_doctor_names_a_set_but_unread_field(tmp_path, monkeypatch):
    """The new behaviour: a SET scalar with NO-READER in the map is named and
    the exit code is nonzero. At HEAD no scalar is NO-READER, so the test
    plants one -- the same way a future dead field would arrive -- and asserts
    the wiring, not the current inventory.
    """
    import tessera.settings_consumers as consumers

    original = consumers.build_map

    def planted(config, approval):
        return _planted_map(config, approval, consumers, original)

    monkeypatch.setattr(consumers, 'build_map', planted)
    monkeypatch.setitem(consumers.__dict__, 'build_map', planted)
    config = bare(tmp_path)
    config.autonomy.world_view = 'matters to someone'
    import tessera.cli as cli
    gaps = cli._unread_settings(config, {'yolo': False})
    assert any('autonomy.world_view' in gap and 'no code reads it' in gap for gap in gaps), gaps


def _planted_map(config, approval, consumers, original):
    """A build_map whose world_view entry is forced to NO-READER.

    Reuses the real generator for everything else, so the counts and record
    rows still come from source, and flips exactly one entry -- the way a
    future dead scalar would arrive in the map for real.
    """
    base = consumers.annotate_counts(original(config, approval))
    for entry in base['fields']:
        if entry['id'] == 'autonomy.world_view':
            entry['status'] = 'NO-READER'
            entry['readers'] = []
            entry['note'] = 'planted for the test: no reader'
    counts = {}
    for entry in base['fields']:
        counts[entry['status']] = counts.get(entry['status'], 0) + 1
    base['counts'] = counts
    return base
