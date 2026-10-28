"""Standalone, purpose-bearing systems registry and generated world view.

Per-system signals replace the literals currently in autonomy.py:50-79:
a health transition, an unhealthy host on first sight, and disk free < 10%.
This module declares and validates those settings; wiring consumers is separate.
Notes are documentation only and have no functional meaning.
Addresses remain strings (URLs, SSH selectors, or command text); this module
does not connect to systems, resolve credentials, or execute commands.
"""

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math


SYSTEM_KINDS = ('host', 'remote', 'mcp', 'credential', 'account', 'backend', 'endpoint', 'dashboard')
SYSTEM_OPTIONS = {'id', 'name', 'kind', 'label', 'role', 'address', 'auth', 'watch', 'signals', 'notes'}
SIGNALS = {'disk_free_percent_below', 'stale_after_seconds', 'wake_on', 'max_wakes_per_hour', 'managed',
           'poll_seconds'}
WAKE_EVENTS = {'health_change', 'disk_low', 'unhealthy'}

# How often a watched endpoint is probed when the record does not say. The operator
# sets this in [autonomy] endpoint_poll_seconds; it lives here only so the registry
# can answer "when is this due" without being handed a config object. A declared
# per-record interval always wins, so one slow service cannot set the pace for
# everything else.
DEFAULT_ENDPOINT_POLL_SECONDS = 60.0


@dataclass
class SystemRecord:
    id: str
    kind: str
    label: str
    role: str
    address: str = ''
    auth: str = ''
    watch: bool = False
    signals: dict = field(default_factory=dict)
    notes: str = ''


def _validate_signals(system_id, signals):
    if not isinstance(signals, dict):
        raise ValueError(f'System {system_id} signals must be a table')
    for key, value in signals.items():
        if key not in SIGNALS:
            raise ValueError(f'Unknown signal: {key}')
        valid = False
        if key == 'wake_on':
            if isinstance(value, list):
                valid = all(isinstance(event, str) and event in WAKE_EVENTS for event in value)
        elif key == 'managed':
            # Whether this system is ours to repair. A display label is not
            # that decision, so the value has to be an explicit boolean.
            valid = type(value) is bool
        elif key == 'max_wakes_per_hour':
            valid = type(value) is int and value > 0
        elif type(value) in (int, float):
            finite = type(value) is int or math.isfinite(value)
            if key == 'disk_free_percent_below':
                valid = finite and 0 <= value <= 100
            else:
                valid = finite and value > 0
        if not valid:
            raise ValueError(f'Invalid signal value for {key}')


def validate_systems(raw, credentials=None) -> dict[str, SystemRecord]:
    """Validate parsed systems tables without mutating or retaining their values.

An optional record name or id must equal its table key. Credential membership
is checked only when a collection of known credential ids is supplied.
"""
    if not isinstance(raw, dict):
        raise ValueError('Systems must be a table')
    records = {}
    for system_id, options in raw.items():
        if (not isinstance(system_id, str) or not system_id
                or any(char.isspace() or char == '\0' for char in system_id)):
            raise ValueError('Invalid system id')
        if not isinstance(options, dict):
            raise ValueError(f'System {system_id} must be a table')
        for key in options:
            if key not in SYSTEM_OPTIONS:
                raise ValueError(f'Unknown system option: {key}')
        for key in ('id', 'name'):
            if key in options and options[key] != system_id:
                raise ValueError('Invalid system id')
        kind = options.get('kind', '')
        if kind not in SYSTEM_KINDS:
            raise ValueError(f'Unknown system kind: {kind}')
        role = options.get('role', '')
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f'System {system_id} needs a role: what it is for')
        label = options.get('label', '')
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f'System {system_id} needs a label')
        for key in ('address', 'auth', 'notes'):
            if not isinstance(options.get(key, ''), str):
                raise ValueError(f'System {system_id} {key} must be text')
        watch = options.get('watch', False)
        if type(watch) is not bool:
            raise ValueError(f'System {system_id} watch must be a boolean')
        signals = options.get('signals', {})
        _validate_signals(system_id, signals)
        auth = options.get('auth', '')
        if auth and credentials is not None and auth not in credentials:
            raise ValueError(f'System {system_id} names credential {auth}, which is not configured')
        address = options.get('address', '')
        # A watched endpoint is probed at its address, so the address is the one
        # field that cannot be empty when polling is what the record claims to do.
        if kind == 'endpoint' and watch and not address.strip():
            raise ValueError(f'System {system_id} is a watched endpoint and needs an address to poll')
        records[system_id] = SystemRecord(
            id=system_id, kind=kind, label=label, role=role,
            address=address, auth=auth, watch=watch,
            signals=deepcopy(signals), notes=options.get('notes', ''),
        )
    return records


def render_world_view(records) -> str:
    """Render validated records in fixed kind order, then id order within kinds."""
    lines = []
    counts = []
    for kind in SYSTEM_KINDS:
        group = []
        for system_id in sorted(records):
            record = records[system_id]
            if record.kind == kind:
                group.append(record)
        counts.append(f'{kind}s: {len(group)}')
        if not group:
            continue
        lines.append(f'## {kind}s')
        for record in group:
            lines.append(f'- **{record.label}** (`{record.id}`) — {record.role}')
            if record.address:
                lines.append(f'  - address: {record.address}')
            elif kind == 'dashboard':
                lines.append('  - declared, not configured')
            if record.auth:
                lines.append(f'  - auth: {record.auth}')
            watched = 'yes' if record.watch else 'no'
            lines.append(f'  - watched: {watched}')
            if record.signals:
                signals = []
                for key in sorted(record.signals):
                    value = json.dumps(record.signals[key], ensure_ascii=False)
                    signals.append(f'{key}={value}')
                lines.append('  - signals: ' + ', '.join(signals))
        lines.append('')
    lines.append(f'System counts ({len(records)} total): ' + ', '.join(counts) + '.')
    return '\n'.join(lines) + '\n'


def load_systems(config_dict, credentials=None) -> tuple[dict[str, SystemRecord], str]:
    """Load only the registry, using config credential keys unless overridden."""
    if credentials is None:
        credentials = config_dict.get('credentials', {})
    records = validate_systems(config_dict.get('systems', {}), credentials)
    return records, render_world_view(records)


def host_thresholds(records, host_name) -> dict:
    """Per-host floor thresholds from the registry, keyed by the host it names.

    A registry record joins a host row by id: a system whose id is the host name
    is about that host. Only `watch` records contribute; an unwatched record is
    documentation. Returns an empty dict when nothing is declared, so callers
    keep their existing default rather than inventing a threshold.
    """
    record = records.get(host_name) if isinstance(records, dict) else None
    if record is None or not record.watch:
        return {}
    signals = record.signals if isinstance(record.signals, dict) else {}
    out = {}
    for key in ('disk_free_percent_below', 'stale_after_seconds',
                'max_wakes_per_hour', 'managed', 'wake_on'):
        if key in signals:
            out[key] = signals[key]
    return out


def thresholds_by_ordinal(records, host_names) -> dict:
    """Resolve the registry against host names into ordinal-keyed thresholds.

    Host rows deliberately carry no name: a name is a private selector and the
    status payload is a public projection (test_infrastructure_status asserts a
    host name never appears in it). So the join happens here, on the server, and
    the floor receives thresholds keyed by the ordinal the row does carry.
    """
    out = {}
    if not isinstance(records, dict):
        return out
    for ordinal, name in enumerate(host_names, 1):
        declared = host_thresholds(records, name)
        if declared:
            out[ordinal] = declared
    return out


def endpoint_polls(records, default_interval=None) -> dict:
    """The watched endpoints to poll, with the interval each one asked for.

    Kept here rather than in the poller so the schedule is a fact about the
    registry a test can read without a clock, a socket or an autonomy object.
    A record is pollable when its kind is `endpoint`, it is watched, and it
    names an address; `poll_seconds` is per record, so a slow service sets its
    own pace instead of one global interval deciding for everything.
    """
    out = {}
    if default_interval is None:
        default_interval = DEFAULT_ENDPOINT_POLL_SECONDS
    if not isinstance(records, dict):
        return out
    for system_id, record in records.items():
        if record.kind != 'endpoint' or not record.watch or not record.address:
            continue
        signals = record.signals if isinstance(record.signals, dict) else {}
        interval = signals.get('poll_seconds', default_interval)
        if type(interval) not in (int, float) or not interval > 0:
            # Validation refuses a bad value at load, so this is the belt to that
            # brace: a record assembled by hand cannot make the loop poll forever.
            continue
        out[system_id] = {'address': record.address, 'role': record.role,
                          'label': record.label, 'interval_seconds': interval}
    return out


def due_endpoints(records, last_checked, now, default_interval=None) -> dict:
    """Which watched endpoints are due at `now`, given when each was last polled.

    `last_checked` is a mapping of system id to the moment of its last probe;
    an endpoint never polled is due immediately, so the loop learns its current
    answer instead of waiting a full interval to find out. That first answer sets
    the baseline and is not announced as a change.
    """
    due = {}
    last_checked = last_checked if isinstance(last_checked, dict) else {}
    for system_id, poll in endpoint_polls(records, default_interval).items():
        previous = last_checked.get(system_id)
        if type(previous) in (int, float) and now - previous < poll['interval_seconds']:
            continue
        due[system_id] = poll
    return due
