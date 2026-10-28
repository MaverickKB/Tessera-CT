from copy import deepcopy
import re

import pytest

from tessera.systems import (SYSTEM_KINDS, SystemRecord, host_thresholds, load_systems,
                             render_world_view, thresholds_by_ordinal, validate_systems)


def system(**options):
    return {'kind': 'host', 'label': 'Compute node', 'role': 'Runs model inference.', **options}


def assert_rejected(raw, message, credentials=None):
    before = deepcopy(raw)
    credentials_before = deepcopy(credentials)
    with pytest.raises(ValueError, match='^' + re.escape(message) + '$'):
        validate_systems(raw, credentials)
    assert raw == before
    assert credentials == credentials_before


def test_valid_record_round_trips_without_mutating_input():
    raw = {'compute': system(address='ssh compute:22', watch=True,
                             signals={'wake_on': ['disk_low'], 'disk_free_percent_below': 10},
                             notes='Documentation only.')}
    before = deepcopy(raw)
    records = validate_systems(raw)
    assert records['compute'] == SystemRecord(
        id='compute', kind='host', label='Compute node', role='Runs model inference.',
        address='ssh compute:22', watch=True,
        signals={'wake_on': ['disk_low'], 'disk_free_percent_below': 10}, notes='Documentation only.')
    markdown = render_world_view(records)
    assert '**Compute node** (`compute`) — Runs model inference.' in markdown
    assert 'address: ssh compute:22' in markdown
    assert 'signals: disk_free_percent_below=10, wake_on=["disk_low"]' in markdown
    assert 'Documentation only.' not in markdown
    assert raw == before
    records['compute'].signals['wake_on'].append('unhealthy')
    assert raw == before


@pytest.mark.parametrize('role', [None, '', '   ', 42])
def test_empty_role_is_rejected(role):
    assert_rejected({'compute': system(role=role)}, 'System compute needs a role: what it is for')


def test_missing_role_is_rejected():
    options = system()
    del options['role']
    assert_rejected({'compute': options}, 'System compute needs a role: what it is for')


@pytest.mark.parametrize('label', [None, '', '   ', 42])
def test_empty_label_is_rejected(label):
    assert_rejected({'compute': system(label=label)}, 'System compute needs a label')


def test_unknown_kind_is_rejected():
    assert_rejected({'compute': system(kind='mystery')}, 'Unknown system kind: mystery')


@pytest.mark.parametrize('system_id', ['', 'two words', 'node\tname', 'node\0name', 42])
def test_invalid_id_is_rejected(system_id):
    assert_rejected({system_id: system()}, 'Invalid system id')


@pytest.mark.parametrize('field', ['id', 'name'])
def test_explicit_identity_must_match_table_key(field):
    assert_rejected({'compute': system(**{field: 'other'})}, 'Invalid system id')
    assert validate_systems({'compute': system(**{field: 'compute'})})['compute'].id == 'compute'


def test_unknown_option_is_rejected():
    assert_rejected({'compute': system(typo=True)}, 'Unknown system option: typo')


def test_signals_must_be_a_table():
    assert_rejected({'compute': system(signals=[])}, 'System compute signals must be a table')


def test_unknown_signal_is_rejected():
    assert_rejected({'compute': system(signals={'disk_limit': 10})}, 'Unknown signal: disk_limit')


@pytest.mark.parametrize(('key', 'value'), [
    ('disk_free_percent_below', -1), ('disk_free_percent_below', 101),
    ('disk_free_percent_below', True), ('disk_free_percent_below', '10'),
    ('disk_free_percent_below', float('inf')),
    ('stale_after_seconds', 0), ('stale_after_seconds', -1),
    ('stale_after_seconds', False), ('stale_after_seconds', float('inf')),
    ('wake_on', 'disk_low'), ('wake_on', ['unknown']), ('wake_on', [1]), ('wake_on', [{}]),
    ('max_wakes_per_hour', 0), ('max_wakes_per_hour', -1),
    ('max_wakes_per_hour', 1.5), ('max_wakes_per_hour', True),
])
def test_invalid_signal_values_are_rejected(key, value):
    assert_rejected({'compute': system(signals={key: value})}, f'Invalid signal value for {key}')


@pytest.mark.parametrize('percent', [0, 100, 10.5])
def test_valid_signal_bounds(percent):
    signals = {'disk_free_percent_below': percent, 'stale_after_seconds': 0.5,
               'wake_on': ['health_change', 'disk_low', 'unhealthy'], 'max_wakes_per_hour': 1}
    assert validate_systems({'compute': system(signals=signals)})['compute'].signals == signals


def test_nan_signal_is_rejected_without_mutation():
    signals = {'stale_after_seconds': float('nan')}
    raw = {'compute': system(signals=signals)}
    before = deepcopy(raw)
    with pytest.raises(ValueError, match='^Invalid signal value for stale_after_seconds$'):
        validate_systems(raw)
    assert raw == before
    assert raw['compute']['signals'] is signals


def test_auth_must_name_a_known_credential_when_supplied():
    raw = {'compute': system(auth='ssh-key')}
    assert_rejected(raw, 'System compute names credential ssh-key, which is not configured', {})
    assert validate_systems(raw, {'ssh-key': {}})['compute'].auth == 'ssh-key'
    assert validate_systems(raw)['compute'].auth == 'ssh-key'


@pytest.mark.parametrize(('options', 'expected'), [({}, 'no'), ({'watch': False}, 'no'), ({'watch': True}, 'yes')])
def test_watch_rendering(options, expected):
    markdown = render_world_view(validate_systems({'compute': system(**options)}))
    assert f'watched: {expected}' in markdown
    assert '  - address:' not in markdown
    assert '  - auth:' not in markdown
    assert '  - signals:' not in markdown


def test_rendering_is_deterministic_across_record_and_signal_order():
    raw = {'z': system(signals={'wake_on': [], 'stale_after_seconds': 10}),
           'a': system(), 'remote': system(kind='remote'), 'mcp': system(kind='mcp')}
    reordered = dict(reversed(list(raw.items())))
    reordered = deepcopy(reordered)
    reordered['z']['signals'] = {'stale_after_seconds': 10, 'wake_on': []}
    markdown = render_world_view(validate_systems(raw))
    assert markdown == render_world_view(validate_systems(reordered))
    assert markdown.index('(`a`)') < markdown.index('(`z`)')
    assert markdown.index('## hosts') < markdown.index('## remotes') < markdown.index('## mcps')


def test_all_kinds_render_in_declared_order_with_counts():
    raw = {}
    for kind in reversed(SYSTEM_KINDS):
        raw[kind] = system(kind=kind)
    markdown = render_world_view(validate_systems(raw))
    headings = [line for line in markdown.splitlines() if line.startswith('## ')]
    assert headings == [f'## {kind}s' for kind in SYSTEM_KINDS]
    for kind in SYSTEM_KINDS:
        assert f'{kind}s: 1' in markdown.splitlines()[-1]


def test_dashboard_without_address_is_declared_not_configured():
    raw = {'metrics': system(kind='dashboard', label='Metrics', role='Displays system metrics.')}
    markdown = render_world_view(validate_systems(raw))
    assert '**Metrics** (`metrics`) — Displays system metrics.\n  - declared, not configured' in markdown
    raw['metrics']['address'] = 'https://metrics.example'
    assert 'declared, not configured' not in render_world_view(validate_systems(raw))


def test_load_without_registry_returns_empty_records_and_zero_counts():
    records, markdown = load_systems({})
    assert records == {}
    assert markdown.startswith('System counts (0 total): ')
    for kind in SYSTEM_KINDS:
        assert f'{kind}s: 0' in markdown


def test_load_uses_config_credentials_and_allows_override():
    config = {'systems': {'compute': system(auth='ssh-key')}, 'credentials': {'ssh-key': {}}}
    before = deepcopy(config)
    records, markdown = load_systems(config)
    assert records['compute'].auth == 'ssh-key'
    assert 'auth: ssh-key' in markdown
    with pytest.raises(ValueError, match='names credential ssh-key, which is not configured'):
        load_systems(config, credentials={})
    assert config == before
    config['credentials'] = {}
    assert load_systems(config, credentials={'ssh-key'})[0] == records


@pytest.mark.parametrize(('options', 'message'), [
    ({'watch': ''}, 'System compute watch must be a boolean'),
    ({'address': []}, 'System compute address must be text'),
    ({'auth': 1}, 'System compute auth must be text'),
    ({'notes': []}, 'System compute notes must be text'),
])
def test_other_fields_keep_their_declared_types(options, message):
    assert_rejected({'compute': system(**options)}, message)


def test_registry_and_records_must_be_tables():
    assert_rejected([], 'Systems must be a table')
    assert_rejected({'compute': []}, 'System compute must be a table')


def test_host_thresholds_join_a_host_by_id_and_only_when_watched():
    records, _ = load_systems({'systems': {
        'mac': system(watch=True, signals={'disk_free_percent_below': 25, 'managed': True}),
        'spark-19cd': system(watch=False, signals={'disk_free_percent_below': 5}),
    }})
    # A watched record contributes the thresholds it declares.
    assert host_thresholds(records, 'mac') == {'disk_free_percent_below': 25, 'managed': True}
    # An unwatched record is documentation: it contributes nothing.
    assert host_thresholds(records, 'spark-19cd') == {}
    # A host with no record keeps the caller's default rather than inventing one.
    assert host_thresholds(records, 'alpha') == {}


def test_managed_is_a_declared_signal_not_a_label_substring():
    # "Ours to repair" is the declared boolean, not a label substring.
    assert_rejected({'compute': system(signals={'managed': 'yes'})}, 'Invalid signal value for managed')
    records, _ = load_systems({'systems': {'compute': system(watch=True, signals={'managed': False})}})
    assert host_thresholds(records, 'compute') == {'managed': False}


def test_the_floor_threshold_comes_from_the_registry_not_a_literal():
    from tessera.autonomy import pressure_lines

    def report(free, total):
        # Host rows carry an ordinal and no name: a name is a private selector and
        # never appears in the status payload.
        return {'hosts': [{'ordinal': 1, 'label': 'Mac', 'health': 'healthy',
                           'metrics': {'disk_free_bytes': free, 'disk_total_bytes': total}}]}

    # 20% free is above the default 10% floor, so the reading is steady.
    assert pressure_lines(report(20, 100), {}, 0) == [
        'Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']

    # Declare 25% for this host and the same reading is now pressure.
    records, _ = load_systems({'systems': {'mac': system(watch=True,
                                                         signals={'disk_free_percent_below': 25})}})
    lines = pressure_lines(report(20, 100), {}, 0,
                           thresholds=thresholds_by_ordinal(records, ['mac']))
    assert lines == ['Pressure: Mac disk free is 20 percent. Name the largest disposable path. Do not delete it.']

    # A host with no record is unchanged: still the old 10% behaviour.
    assert pressure_lines(report(20, 100), {}, 0, thresholds={}) == [
        'Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']


def test_a_declared_managed_system_repairs_without_being_named_kiosk():
    from tessera.autonomy import pressure_lines
    records, _ = load_systems({'systems': {'spark-19cd': system(watch=True, signals={'managed': True})}})
    report = {'hosts': [{'ordinal': 1, 'label': 'Spark', 'health': 'unreachable'}]}
    lines = pressure_lines(report, {}, 0, thresholds=thresholds_by_ordinal(records, ['spark-19cd']))
    assert 'Restart the service if that is the fix' in lines[0]
    # Without the declaration the same host is only named, never restarted.
    lines = pressure_lines(report, {}, 0, thresholds={})
    assert 'Do not invent a repair' in lines[0]
