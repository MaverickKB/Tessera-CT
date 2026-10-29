#!/usr/bin/env python3
"""Generate the configuration reference without loading an operator's config."""

import argparse
import ast
from dataclasses import MISSING, fields, is_dataclass
import inspect
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tessera import config
from tessera.credentials import CredentialReference
from tessera.settings_model import catalog


RETIRED = {
    'tools.direct_route_tools': (
        'every route offers the full registry; this list does not hide tools'
    ),
    'aethermind.enabled': (
        'memory is required; the packaged engine runs unless an external service validates'
    ),
    'swarm.job_workers': 'not a cap; each model uses max_concurrent_requests',
    'swarm.max_concurrent': 'not a cap; each model uses max_concurrent_requests',
    'swarm.backend_limits': 'not a cap; each model uses max_concurrent_requests',
    'swarm.default_backend_limit': 'not a cap; each model uses max_concurrent_requests',
    'swarm.initial_concurrency': 'nothing reads it',
    'swarm.aimd_successes': 'nothing reads it',
    'swarm.aimd_add': 'nothing reads it',
    'swarm.aimd_factor': 'nothing reads it',
    'accounts.*.limits.max_concurrent': 'not a cap; set max_concurrent_requests on the model',
    'accounts.*.limits.initial_concurrency': 'nothing reads it',
}


NAMED_TABLES = {
    'accounts': config.AccountConfig,
    'backends': config.BackendConfig,
    'credentials': CredentialReference,
    'hosts': config.HostConfig,
    'systems': config.SystemConfig,
    'remotes': config.RemoteConfig,
    'mcp_servers': config.McpServerConfig,
}


def loader_names():
    """Read only the loader's literal declarations, including nested companions."""
    tree = ast.parse(inspect.getsource(config.load_config))
    sections = None
    scalars = None
    tables = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == 'sections':
                    sections = ast.literal_eval(node.value)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if node.target.id == 'name' and isinstance(node.iter, ast.Tuple):
                scalars = ast.literal_eval(node.iter)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == 'data' and node.func.attr == 'get':
                if node.args and isinstance(node.args[0], ast.Constant):
                    tables.add(node.args[0].value)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple):
            if not isinstance(node.iter, ast.Tuple):
                continue
            nested_companion = False
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    base = child.func.value
                    if isinstance(base, ast.Name) and base.id == 'companions' and child.func.attr == 'get':
                        nested_companion = True
            for entry in node.iter.elts:
                if isinstance(entry, ast.Tuple) and len(entry.elts) == 2:
                    name = ast.literal_eval(entry.elts[0])
                    if not nested_companion:
                        tables.add(name)
    if sections is None or scalars is None:
        raise ValueError('Loader declarations changed; update reference generation')
    tables.update(sections)
    tables.difference_update(scalars)
    return tables, scalars, sections


def default_value(item):
    if item.default is not MISSING:
        return item.default
    if item.default_factory is not MISSING:
        return item.default_factory()
    raise ValueError('Missing default for ' + item.name)


def display(value):
    if isinstance(value, Path):
        raise ValueError('Runtime paths must never enter the reference')
    return json.dumps(value, ensure_ascii=False, sort_keys=True).replace('|', '&#124;').replace('`', '&#96;')


def env_name(path, sections):
    parts = path.split('.')
    if parts[-1] in ('token_env', 'api_key_env'):
        return '— (credential selector; configure in TOML)'
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 2 and parts[0] in sections:
        name = '_'.join(parts)
    else:
        name = '__'.join(parts)
    return '`TESSERA_' + name.upper() + '`'


# Per-record editors are not catalog() specs. The Providers page saves these.
RECORD_SETTINGS = {
    'backends.*.max_concurrent_requests',
    'backends.*.throttle_requests_per_minute',
    'backends.*.throttle_tokens_per_minute',
}


def editor_paths():
    """Named-entry fields Settings accepts that are not catalog Spec paths.

    The Plugins editor writes these MCP server keys. ``name`` is the table key.
    ``args`` is stored as the tail of ``command``, so it has no key of its own.
    Other named-entry editors stay outside this set.
    """
    from tessera.config import McpServerConfig
    from tessera.settings_model import MCP_KEYS
    present = {item.name for item in fields(McpServerConfig)}
    return {f'mcp_servers.*.{key}' for key in MCP_KEYS if key in present and key != 'name'}


def settings_paths():
    """Read catalog paths using declared defaults, never an operator config."""
    paths = set(RECORD_SETTINGS)
    for spec, _value in catalog(config.Config(), {}):
        if spec.path:
            paths.add('.'.join(spec.path))
    paths.update(editor_paths())
    return paths


def availability(path, settings):
    notes = []
    retired = path in RETIRED
    if retired:
        notes.append('retired: ' + RETIRED[path] + ' (accepted for compatibility; ignored)')
    if path not in settings:
        # Every key with no Settings row says so; that is a structural claim the reference
        # tests pin. For a retired key, drop only the instruction to set it, because
        # "accepted and ignored" and "set this in config.toml" cannot both be true.
        notes.append('file-only: no Settings row' if retired
                     else 'file-only: no Settings row — set this in config.toml')
    text = '; '.join(notes) or 'Settings row'
    extra = VALUE_NOTES.get(path)
    if extra:
        text += '; ' + extra
    return text


VALUE_NOTES = {
    'ui.mark': (
        'values: cairn (default art with the Tessera-CT wordmark and the tree), '
        'gremlin (sticker logo and header crew; shown only while ui.easter_egg_f_it is on)'
    ),
}


MANUAL_SWITCHES = {
    'aethermind': (
        '`aethermind.project`, `aethermind.personal_project`, and `aethermind.note_project` '
        'are store addresses. A relative name is relative to the Tessera home, and the loader '
        'resolves every non-empty value to an absolute path before any session sees it, because '
        'the embedded engine runs with its own working directory and would otherwise open a '
        'different store than the one that was seeded.'
    ),
    'skills': (
        'Operator switches (`skills.enabled` off, and names in `skills.disabled`) apply in every '
        'approval mode, including Unleashed. Search and load honor them. Unleashed removes approval prompts '
        'and other guards the system adds; it does not turn a shelf or a skill back on. '
        'Unleashed is the label for config value `yolo`.'
    ),
    'tools': (
        'Names in `tools.disabled` are operator switches. They apply in every approval mode, '
        'including Unleashed: the next turn omits the tool and a call is refused. Unleashed removes '
        'approval prompts for tools that stay on.'
    ),
    'agents': (
        '`agents.local_serving_slots` is retired. A config that still sets it loads, records a '
        'migration note naming the saved value, and does not apply it. Local agent lanes use '
        'each model\'s `max_concurrent_requests`. A positive `agents.max_concurrent` remains an '
        'operator ceiling over every lane in both modes. Zero leaves local lanes to the endpoint.'
    ),
    'swarm': (
        '`swarm.max_job_tasks` omitted: Safe mode allows 8 workers in one job; Unleashed mode does not '
        'add that cap. A saved integer holds in both modes. `swarm.distinct_workers` omitted: Safe '
        'mode requires each worker\'s own backend and account so the evidence is independent. That '
        'is a guard, not a rule that a judge must differ from a worker. Unleashed mode drops it. A saved '
        'true or false holds in both modes. `job_workers`, `max_concurrent`, `backend_limits`, '
        '`default_backend_limit`, `initial_concurrency` and the AIMD settings are retired: a file '
        'that still sets them loads, records a migration note, and does not apply them. '
        'A model\'s cap is `max_concurrent_requests`. `pressure_hold_enabled` off is the default; '
        'turning it on holds in Safe mode and in Unleashed mode.'
    ),
    'ui': (
        '`ui.mark` is `cairn` or `gremlin`, both shown in Settings. `cairn` is the default: '
        'the Tessera art with the Tessera-CT wordmark and the header tree. `gremlin` is shown only '
        'while `ui.easter_egg_f_it` is on. There is no separate gremlin switch. The logo in the '
        'header and `/favicon.ico` is `web/brand/gremlin-logo.svg`. The F-it button keeps '
        '`web/brand/f-it-gremlin.svg`. The open page updates its header and icon links. '
        'The web app manifest, the macOS dock icon, and the iOS app icon stay the Tessera art. '
        'This release does not install alternate native icons. '
        'While gremlin is showing, the header trail replaces the tree with the crew. '
        'The tree returns when the mark is cairn. The crew reads signals the page already has, '
        'and only while the page is connected, the same gate that speeds the tree. '
        'Nothing running: one goblin napping. Load adds the open session busy flag, '
        'that session\'s `active_agents`, its `queued` turns, swarm jobs whose status is '
        '`running`, and board cards whose status is `running`. One goblin is hauling at load 1, '
        'two at 2 or 3, and three at 4 or more. One goblin tumbles when the open session error '
        'string is set, the live turn ended with an error, a shown job is `failed` or `blocked`, '
        'or a shown board card is `failed` or `blocked`. Up to two others keep hauling when load '
        'is above zero. While the page is not connected the crew naps and ignores those counts. '
        'Movement follows `prefers-reduced-motion` the same way the tree does. The trail stays '
        'hidden below 1020px. The iOS example has no native trail; its embedded page is this '
        'header, and a phone-width view already hides it. No separate iOS crew was added. '
        'Skins remain `strata`, `basalt`, and `tide`. A way to add a custom skin is a '
        'future update and is not part of this release.'
    ),
}


def row(path, value, sections, settings):
    return (f'| `{path}` | `{display(value)}` | {env_name(path, sections)} | '
            f'{availability(path, settings)} |')


def dataclass_rows(cls, prefix, sections, settings, *, named=False):
    lines = []
    for item in fields(cls):
        if named and item.name == 'name':
            continue  # The loader supplies this from the table label.
        value = default_value(item)
        path = prefix + '.' + item.name
        if is_dataclass(value):
            lines.extend(dataclass_rows(type(value), path, sections, settings))
        else:
            lines.append(row(path, value, sections, settings))
    return lines


def render():
    tables, scalars, sections = loader_names()
    settings = settings_paths()
    config_fields = {}
    for item in fields(config.Config):
        config_fields[item.name] = item
    lines = [
        '# Configuration reference', '',
        'Generated from `tessera/config.py` dataclasses and loader declarations, plus',
        '`CredentialReference`, with availability from `settings_model.catalog` and explicit retired-key annotations.',
        'Do not edit by hand. Regenerate from the repository root:', '',
        '```sh', '.venv/bin/python scripts/gen-config-reference.py', '```', '',
        f'{len(tables)} top-level tables and {len(scalars)} top-level scalar keys.', '',
        'Defaults below are declared defaults, before validation or runtime discovery.',
        'No host config, environment values, credential values or runtime paths are read.',
        '`cwd` defaults to the launch directory. Named tables default to `{}`; `*` is',
        'a caller-chosen name, supplied as the dataclass name by the loader.', '',
        'Env values use TOML types when parseable, otherwise text. The loader recognizes',
        'single-underscore section prefixes only for: ' + ', '.join(f'`{name}`' for name in sections) + '.',
        'Double underscores separate every nested path component (including `*`).',
        '`TESSERA_HOME` selects the home; it is not a table override. Credential selector',
        'fields ending in `token_env` or `api_key_env` name secret variables; configure',
        'these selectors in TOML. The variables they name are excluded from overrides,',
        'as are other configured credential source variables and `TESSERA_SERVER_TOKEN`.', '',
        'Availability is derived from stored paths in the Settings catalog, from',
        'record-row editors named in this generator, and from the MCP server fields',
        'the Plugins editor accepts. ``name`` is the entry key, not a field. A',
        'file-only row has no catalog Spec, no named record-row entry, and none of',
        'those editor fields for that exact path. The environment override still',
        'applies where listed.',
        'Retired keys remain accepted for compatibility but are ignored.', '',
        '## Top-level scalars', '', '| Key | Default | Environment override | Availability |',
        '| --- | --- | --- | --- |',
    ]
    for name in scalars:
        if name == 'cwd':
            lines.append('| `cwd` | launch directory | `TESSERA_CWD` | ' + availability('cwd', settings) + ' |')
        else:
            lines.append(row(name, default_value(config_fields[name]), sections, settings))
    for name in sorted(tables):
        lines.extend(['', f'## `{name}`', ''])
        if name in NAMED_TABLES:
            lines.extend(['Default: `{}`. Entries live under `' + name + '.*`.', '',
                          '| Key | Default | Environment override | Availability |', '| --- | --- | --- | --- |'])
            body = dataclass_rows(NAMED_TABLES[name], name + '.*', sections, settings, named=True)
            lines.extend(body)
            present = {line.split('`')[1] for line in body if line.startswith('| `')}
            for path in sorted(RETIRED):
                if path.startswith(name + '.') and path not in present:
                    lines.append(row(path, 'ignored', sections, settings))
            if name == 'backends':
                lines.extend(['', 'Additional backend fields are retained in `extra`; `extra` accepts provider-specific',
                              'request options, including `parameters`. Names and defaults depend on the provider.',
                              '',
                              '`max_concurrent_requests` is how many requests may run at once on that one model.',
                              '`null` means unset. A positive integer you set by hand is the cap in Safe mode and in Unleashed mode.',
                              'Safe mode, when the field is omitted and the model has no credential, allows 1',
                              'request at a time. That default is not saved. Unleashed mode drops it and does not add a number.',
                              'A model that names a credential or a vendor CLI stays',
                              'unlimited until you set a number by hand. That follows the credential, not the host name',
                              'or the model id. A hand-set number is also the',
                              'local agent lane, and the chat holds one slot. An omitted number does not',
                              'refuse a local subagent. A cloud lane stays unbounded unless `[agents]` sets',
                              '`max_concurrent` or `concurrency_by_kind`. `agents.local_serving_slots`',
                              'is retired: a file that still sets it is noted at load and ignored.',
                              '',
                              '`throttle_requests_per_minute` and `throttle_tokens_per_minute` pace that one endpoint.',
                              'Both default to `null`, which is unlimited in Safe mode and in Unleashed mode.',
                              'There is no system default. A positive integer you set by hand is enforced in both modes.',
                              'Unleashed drops only defaults the system adds, and these fields add none.',
                              'The pace is not a swarm-wide cap. Token pace counts usage reported when the request finishes.',
                              'Environment: `TESSERA_BACKENDS__<NAME>__THROTTLE_REQUESTS_PER_MINUTE` and',
                              '`TESSERA_BACKENDS__<NAME>__THROTTLE_TOKENS_PER_MINUTE`.'])
        elif name == 'hooks':
            lines.extend(['| Key | Default | Environment override | Availability |', '| --- | --- | --- | --- |',
                          row('hooks.shell', [], sections, settings), '',
                          '`shell` is a list of hook objects requiring `event` and `command` strings;',
                          'additional hook options (such as `timeout`) belong to the hook consumer.'])
        elif name == 'roles':
            lines.extend(['Default: `{}`. Maps role names to configured backend names.', '',
                          '| Key | Default | Environment override | Availability |', '| --- | --- | --- | --- |'])
            for path in ('roles.main', 'roles.fast', 'roles.compactor', 'roles.failover'):
                lines.append(row(path, '', sections, settings))
            lines.append(row('roles.*', '', sections, settings))
        else:
            cls = config_fields[name].default_factory
            if not is_dataclass(cls):
                raise ValueError('No dataclass schema for ' + name)
            if name == 'companions':
                for child in fields(cls):
                    lines.extend([f'### `companions.{child.name}`', '',
                                  '| Key | Default | Environment override | Availability |', '| --- | --- | --- | --- |'])
                    lines.extend(dataclass_rows(child.default_factory, 'companions.' + child.name, sections, settings))
                    lines.append('')
            else:
                lines.extend(['| Key | Default | Environment override | Availability |', '| --- | --- | --- | --- |'])
                body = dataclass_rows(cls, name, sections, settings)
                lines.extend(body)
                present = set()
                for line in body:
                    if line.startswith('| `'):
                        present.add(line.split('`')[1])
                for path in sorted(RETIRED):
                    if path.startswith(name + '.') and path not in present:
                        lines.append(row(path, 'ignored', sections, settings))
        note = MANUAL_SWITCHES.get(name)
        if note:
            lines.extend(['', note])
    return '\n'.join(lines).rstrip() + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/reference/CONFIG.md')
    args = parser.parse_args()
    content = render()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding='utf-8')


if __name__ == '__main__':
    main()
