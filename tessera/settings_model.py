"""Typed settings registry. The page renders this projection and does not invent rows."""

from dataclasses import asdict, fields as dataclass_fields, replace
import json
import math
import os
import re
import threading
from urllib.parse import urlsplit

from .account_oauth import grant_report
from .config import PROVIDER_KINDS, Config, load_config
from .credentials import CredentialResolver, KeychainStore
from .floor_definition import FloorDefinitionError, peek_world_view, save_definition, staged_world_view
from .systems import SYSTEM_KINDS
from .settings_ladder import ladder, credential_source, binding_source
from .speech_services import (DUPLEX_PROVIDERS, SpeechProbeError, connection_for,
                              duplex_connections, probe_models, probe_voices)
from .voice import pipes as voice_pipes, selected_pipe as voice_selected_pipe
from .agent_selection import backend_catalog
from .secret_names import flagged_env_value, is_secret_name

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
SECTIONS = (
    ('providers', 'Providers'),
    ('models', 'Models'),
    ('credentials', 'Credentials'),
    ('identity', 'Identity'),
    ('accounts', 'Accounts'),
    ('integrations', 'Integrations'),
    ('hosts', 'Hosts'),
    ('systems', 'Systems'),
    ('plugins', 'Plugins'),
    ('computer', 'Computer use'),
    ('voice', 'Voice'),
    ('appearance', 'Appearance'),
    ('permissions', 'Permissions'),
    ('autonomy', 'Autonomy'),
    ('judge', 'Session judge (Jev)'),
)
# Keep setup guidance beside the registry so every client sees the same starting point.
SECTION_START = {
    'identity': 'Start here: choose your companion name and how it addresses you.',
    'accounts': 'Start here: accounts are for services that are not model hosts. Sign-ins are on Providers.',
    'providers': 'Start here: connect a provider first. Sign in, add an API key, or add a local model server.',
    'models': 'Start here: pick the chat model from a connected provider. Failover is optional.',
    'credentials': 'Start here: saved credentials. A value is never shown. A model uses its provider connection.',
    'hosts': ('Start here: list the machines the floor watches. A host row is what the floor reads; '
              'its thresholds live in [systems.<host>].'),
    'systems': ('Start here: declare what each system is FOR and the thresholds the floor should use '
                'for it. This is the registry the autonomy floor reads, so a threshold set here '
                'changes when the floor wakes, without editing the application.'),
    'integrations': ('Start here: set a service URL or endpoints when you have an external AetherMind. '
                     'The project root is here too. Which engine is running is on the Plugins row.'),
    'plugins': ('Start here: add an MCP plugin by URL or local command, then Check it. '
                'Enabled, Session tools, and an excluded tool stay off when you turn them off, in Unleashed too. '
                'Unleashed only drops approvals, warnings, and default limits.'),
    'computer': 'Start here: choose a provider, then configure screen questions and their credential if needed.',
    'voice': ('Start here: connect a duplex account, or add a speech-to-text and a '
              'text-to-speech service. A pipe choice appears only when both are ready.'),
    'appearance': 'Start here: choose a theme and an icon and logo set; the remaining display preferences are optional.',
    'permissions': 'Start here: choose the approval policy for tool actions.',
    'autonomy': 'Start here: choose whether autonomy runs and describe what matters in World view.',
    'judge': 'Start here: review the judge and action gate switches, then supply the judge credential.',
}
SAFE_FACT = {'ask': 'Safe asks', 'deny_risky': 'Safe blocks', 'never': 'Safe allows'}
_TIER_RANK = {'required': 0, 'optional': 1, 'advanced': 2}

# The config table each record editor writes. A named-entry editor reaches its whole
# table, so an attribute listed here has a control even though no catalog Spec stores
# a path into it. Without this, the coverage measurement called `backends` and
# `accounts` unreachable while Providers and Worker pools were editing them.
EDITOR_TABLES = {
    'providers': 'backends',
    'named_accounts': 'accounts',
    'mcp_servers': 'mcp_servers',
    'hosts': 'hosts',
    'systems': 'systems',
    'credentials': 'credentials',
}

# Every top-level Config attribute with no control on the page, and the reason it has
# none. The page publishes this as its own section and the disclosure test reads the
# same table, so a person learns the extent of Settings from Settings rather than from
# a doc. `file_only()` refuses to answer when an attribute has no reason here, which is
# what keeps a newly added table from becoming an undisclosed gap.
# Tables whose live values the page shows even though it does not edit them.
BEHAVIOR_TABLES = ('agents', 'prompt', 'compactor', 'tools', 'swarm', 'board', 'server')

FILE_ONLY_REASONS = {
    'home': 'Chosen before the core starts, by --home or TESSERA_HOME.',
    'cwd': 'The launch directory; relative paths resolve from it.',
    'config_missing': 'Derived at load: true when no config file was found.',
    'workspace': 'The directory turns run in. It follows --home and the launch directory.',
    'user_instructions': 'Standing instructions for every turn; they live in the identity file.',
    'compactor': 'Limits for compacting a long session. Tuning knobs, not setup.',
    'view': 'How much of a session the prompt carries. Tuning knobs.',
    'tools': 'Approval is on this page as Mode. The rest are tool limits and policy.',
    'hooks': 'Shell commands run on session events.',
    'agents': 'Worker concurrency, depth, budget and the worker backend policy.',
    'server': 'Listener host, port, auth and restart timing.',
    'daemon': 'IPC frame limits and the approval timeout.',
    'swarm': 'Parallel evidence jobs: concurrency, retries and leases.',
    'status': 'Size limits for the status payload.',
    'channels': 'Messaging between sessions on one home.',
    'board': 'Board limits and the acceptance-gate policy.',
    'intake': 'Limits for gathering pull requests onto the board.',
    'remotes': 'Remote machines for shell and computer use.',
    'infrastructure': 'Prometheus and host collection.',
    'grafana': 'Dashboard proxy paths and its credential.',
    'companions': 'Companion endpoints.',
    'inspector': 'The debug inspector socket.',
    'inspector_error': 'Derived: why the inspector is unavailable.',
    'retired_settings': 'Older keys a file still carries. Each one is shown on this page and never applied.',
    'cua': 'Computer-use binaries, paths and limits.',
    'prompt': 'The system-prompt sections and the per-turn nudge.',
    'skills': 'The skills shelf path and its limits.',
    'identity_file': 'Paths the identity layer reads directly.',
    'identity_max_bytes': 'A cap the identity layer reads directly.',
}
PROVIDER_KEYS = {'name', 'kind', 'account', 'base_url', 'model', 'credential', 'chat', 'enabled', 'vendor', 'command',
                 'cwd', 'args', 'allow_api_key_fallback', 'fallback_env', 'fallback_credential',
                 'api_key_env', 'max_concurrent_requests', 'throttle_requests_per_minute', 'throttle_tokens_per_minute'}
CONCURRENCY_HINT = (
    'How many requests may run at once on this model. A number you set by hand is enforced in Safe mode and in Unleashed mode. '
    'max_concurrent_requests on this model is the cap, and the chat holds one slot. '
    'Leave this empty and Safe mode allows 1 request at a time on a local model and no cap on a cloud provider, and it does not refuse a subagent. '
    'Unleashed mode drops that default and does not add a number. Empty does not mean zero requests, and this is not a swarm-wide limit.'
)
THROTTLE_HINT = (
    'Optional pace for this one model endpoint. A number you set by hand is enforced in Safe mode and in Unleashed mode. '
    'Leave this empty and it stays unlimited. Safe mode does not fill in a number, and Unleashed mode does not add one. '
    'Empty does not mean zero. This paces this endpoint only. It does not cap the swarm, and it is not the concurrency limit above. '
    'Token pace counts the usage the request reports when it finishes.'
)
JOB_TASKS_HINT = (
    'How many workers one swarm job may list. A number you set by hand is enforced in Safe mode and in Unleashed mode. '
    'Leave this empty and Safe mode allows 8. Unleashed mode does not add that cap. '
    'Empty does not mean zero. This bounds the job record. It is not how many requests run at once, and it is not a swarm-wide limit.'
)
DISTINCT_HINT = (
    'A guard on one job so its workers can be independent evidence. '
    'On, each worker needs its own backend and its own account. '
    'This does not require a judge to differ from a worker. Overlapping scopes are refused either way. '
    'Leave this unset and Safe mode requires distinct backends and accounts. Unleashed mode allows a shared backend. '
    'A choice you save, on or off, holds in Safe mode and in Unleashed mode.'
)
OAUTH_MODELS = {
    'xai': {
        'label': 'xAI OAuth',
        'kind': 'openai_chat',
        'base_url': 'https://api.x.ai/v1',
        'credential': 'xai-oauth',
        'oauth': 'xai',
    },
    'openai-codex': {
        'label': 'OpenAI Codex',
        'kind': 'openai_responses',
        'base_url': 'https://chatgpt.com/backend-api/codex',
        'credential': 'openai-codex',
        'oauth': 'openai-codex',
    },
}
ACCOUNT_KEYS = {'name', 'provider_kind', 'credential_ref'}
RETIRED_ACCOUNT_LIMITS = ('max_concurrent', 'initial_concurrency')
CREDENTIAL_KEYS = {'name', 'keychain_service', 'keychain_account', 'env', 'secret'}
MCP_KEYS = {'name', 'enabled', 'url', 'command', 'args', 'env', 'token_env', 'session_tools'}
VENDORS = ('codex', 'claude', 'grok')


class Spec:
    def __init__(self, id, section, type, label, apply, path=(), *, hint='', write='set', options=(),
                 minimum=None, maximum=None, status='', lines=False, empty_removes=False, inline=False, key='',
                 inert=False, tier='required'):
        self.id = id
        self.section = section
        self.type = type
        self.label = label
        self.apply = apply
        self.path = tuple(path)
        self.hint = hint
        self.write = write
        self.options = tuple(options)
        self.minimum = minimum
        self.maximum = maximum
        self.status = status
        self.lines = lines
        self.empty_removes = empty_removes
        self.inline = inline
        self.key = key or id.rsplit('.', 1)[-1]
        # A field the core refuses to save right now, because another setting
        # supersedes it. The page must be able to tell that structurally: `status`
        # is prose and some live fields carry one while staying editable
        # (permissions.mode reports "Safe blocks" and is the switch), so the
        # renderer cannot infer inertness from the presence of a status string.
        self.inert = inert
        # How a field ranks for a reader with no prior knowledge: 'required' is what
        # makes the thing work at all, 'optional' is worth setting, 'advanced' is for
        # a proxy or a custom endpoint. A field required for the thing to work at all
        # comes first, and that ordering has to be visible. Default is 'required' so an
        # unranked field is never pushed out of sight.
        self.tier = tier


def _options(pairs):
    return [{'value': value, 'label': label} for value, label in pairs]


def retired_memory_label(config):
    """What Settings shows for a stored aethermind.enabled key. The value is never applied."""
    stored = getattr(config, 'retired_settings', None)
    if not isinstance(stored, dict) or 'aethermind.enabled' not in stored:
        return 'not stored'
    return 'ignored (' + repr(stored['aethermind.enabled']) + ')'


def dump_field(spec, value):
    payload = {
        'id': spec.id, 'section': spec.section, 'type': spec.type, 'label': spec.label,
        'value': value, 'write': spec.write, 'apply': spec.apply, 'key': spec.key, 'tier': spec.tier,
    }


    if spec.hint:
        payload['hint'] = spec.hint
    if spec.status:
        payload['status'] = spec.status
    if spec.inert:
        payload['inert'] = True
    if spec.options:
        payload['options'] = _options(spec.options)
    if spec.inline:
        payload['inline'] = True
    constraints = {}
    if spec.minimum is not None:
        constraints['min'] = spec.minimum
    if spec.maximum is not None:
        constraints['max'] = spec.maximum
    if spec.lines:
        constraints['lines'] = True
    if constraints:
        payload['constraints'] = constraints
    return payload


def worker_policy_field(config):
    records = []
    for row in backend_catalog(config):
        name = row['backend']
        fields = [
            dump_field(Spec('worker_policy.' + name + '.capability', 'models', 'enum',
                            'Capability', 'live', key='capability',
                            options=tuple((x, x) for x in ('routine', 'standard', 'complex'))), row['capability']),
            dump_field(Spec('worker_policy.' + name + '.cost_rank', 'models', 'float',
                            'Cost rank', 'live', minimum=0, key='cost_rank'), row['cost_rank']),
        ]
        records.append({'name': name, 'title': name, 'fields': fields, 'actions': ['edit'],
                        'status': row['capability'] + ' · rank ' + str(row['cost_rank'])})
    return {'id': 'worker_policy', 'section': 'models', 'type': 'list-of-records',
            'label': 'Worker tiers', 'records': records, 'value': records, 'actions': [],
            'blank': [], 'write': 'records', 'apply': 'live', 'tier': 'advanced',
            'hint': 'The dispatcher chooses the cheapest enabled, permitted backend meeting the requested tier. Ranks are preferences, not prices.'}


def _backend_label(config, name):
    backend = config.backends[name]
    cli = (backend.extra or {}).get('cli') or {}
    if cli.get('vendor'):
        return name + ' · ' + str(cli.get('vendor')) + ' CLI'
    model = backend.model or name
    # The page is a model page, so lead with the model id and keep the backend
    # name as the qualifier. Two backends can serve the same model id (grok and
    # grok-chat both run grok-4.7), so the name still has to appear to tell the
    # options apart.
    if model != name:
        return model + ' · ' + name
    return name


def _kind(config, backend):
    if backend.account and backend.account in config.accounts:
        return config.accounts[backend.account].provider_kind
    return backend.provider_kind or 'openai_chat'


def _credential_options(config, include_empty=True):
    pairs = [('', 'None')] if include_empty else []
    for name in config.credentials:
        pairs.append((name, name))
    return tuple(pairs)


def _voice_account_options(connections, current):
    """Accounts the page offers, plus whatever the file already names."""
    pairs = [('', 'No account')]
    for item in connections:
        pairs.append((item['reference'], item['label']))
    known = [value for value, _label in pairs]
    if current and current not in known:
        pairs.append((current, current + ' (not a duplex account)'))
    return tuple(pairs)


def _voice_model_options(connection, current):
    """Models the chosen account offers, plus whatever the file already names."""
    pairs = []
    if connection is not None:
        for model in connection['models']:
            pairs.append((model, model))
    known = [value for value, _label in pairs]
    if current and current not in known:
        pairs.append((current, current + ' (not offered by this account)'))
    return tuple(pairs)


def _voice_status(config, connection):
    """The one line the Voice row shows: ready, off, or what is missing."""
    voice = config.voice
    chosen = voice_selected_pipe(voice)
    if not voice.enabled:
        return 'Voice is off.'
    if chosen == 'local':
        return 'Configured: local speech. Hold to talk on the Voice page.'
    if chosen == 'duplex':
        if connection is None:
            return 'Not ready: the configured credential is not a duplex account. Choose an account.'
        if not connection['connected']:
            return 'Not ready: ' + connection['status']
        return 'Ready: duplex voice over ' + voice.websocket_url + '.'
    return 'Not ready: connect a duplex account, or add a speech-to-text and a text-to-speech service.'


def _voice_account_status(connection):
    if connection is None:
        return 'No connected account offers a duplex model. Sign in under Providers.'
    return connection['status']


def _speech_specs(config, service_id, label):
    """Endpoint and model for one local speech service.

    The model is a list the endpoint reported, never a typed name. Until the
    endpoint has been saved and listed its models there is nothing to choose.
    """
    service = getattr(config.voice, service_id)
    path = ('voice', service_id)
    endpoint = Spec('voice.' + service_id + '.base_url', 'voice', 'string', label + ' endpoint',
                    'live', path + ('base_url',), key='base_url', tier='optional',
                    hint='The base URL of a local server that speaks the OpenAI audio API. '
                         'Saving it lists the models the server serves.')
    options = []
    for model in service.models:
        if isinstance(model, str) and model:
            options.append((model, model))
    known = [value for value, _option_label in options]
    if service.model and service.model not in known:
        options.append((service.model, service.model))
    if options:
        count = str(len(options)) + ' model' + ('' if len(options) == 1 else 's') + ' listed.'
        model = Spec('voice.' + service_id + '.model', 'voice', 'enum', label + ' model',
                     'live', path + ('model',), key='model', options=tuple(options), tier='optional',
                     status=count, hint='A model this endpoint reported. Nothing is typed here.')
        return [(endpoint, service.base_url), (model, service.model)]
    model = Spec('voice.' + service_id + '.model', 'voice', 'display', label + ' model',
                 'live', path + ('model',), write='display', inert=True, tier='optional',
                 status='Save the endpoint to list the models it serves.')
    return [(endpoint, service.base_url), (model, service.model or 'Not chosen')]


def _voice_specs(config):
    """The Voice section: accounts and models, never credentials.

    A duplex account is a sign-in or a provider the app already holds; the
    provider profile supplies the socket, token service and protocol prefix.
    Local speech services list the models their endpoints report.
    """
    voice = config.voice
    connections = duplex_connections(config)
    connection = connection_for(config, voice.credential_ref)
    account_options = _voice_account_options(connections, voice.credential_ref)
    model_options = _voice_model_options(connection, voice.model)
    if connection is None:
        model_spec = Spec('voice.model', 'voice', 'display', 'Duplex model', 'live', ('voice', 'model'),
                          write='display', inert=True, status='Choose an account first.')
        model_value = voice.model or 'Not chosen'
    else:
        model_spec = Spec('voice.model', 'voice', 'enum', 'Duplex model', 'live', ('voice', 'model'),
                          options=model_options, key='model',
                          hint='A full-duplex model this account offers. The model carries the voice.')
        model_value = voice.model
    specs = [
        (Spec('voice.enabled', 'voice', 'bool', 'Voice', 'live', ('voice', 'enabled'), key='enabled',
              status=_voice_status(config, connection)), voice.enabled),
        (Spec('voice.account', 'voice', 'enum', 'Account', 'live', ('voice', 'credential_ref'),
              options=account_options, key='credential_ref', status=_voice_account_status(connection),
              hint='The account that mints the voice session. Sign-ins and providers live under Providers.'),
         voice.credential_ref),
        (model_spec, model_value),
        # The endpoint is the account profile's, not a field to type.
        (Spec('voice.websocket_url', 'voice', 'display', 'Realtime endpoint', 'live', ('voice', 'websocket_url'),
              write='display', inert=True,
              status='Set by the account. Voice connects here.' if voice.websocket_url
                     else 'Set when an account is chosen.'),
         voice.websocket_url),
    ]
    state = voice_pipes(voice)
    if state['duplex'] and state['local']:
        pipe_spec = Spec('voice.pipe', 'voice', 'enum', 'Pipe', 'live', ('voice', 'pipe'),
                         options=(('duplex', 'Duplex'), ('local', 'Local speech')), key='pipe',
                         hint='Duplex uses one full-duplex model. Local speech uses the speech-to-text '
                              'and text-to-speech services below. Duplex is the default.')
        pipe_value = voice_selected_pipe(voice)
    elif state['duplex'] or state['local']:
        only = 'Duplex' if state['duplex'] else 'Local speech'
        pipe_spec = Spec('voice.pipe', 'voice', 'display', 'Pipe', 'live', ('voice', 'pipe'),
                         write='display', inert=True,
                         status=only + ' is the only configured pipe, so it runs and no choice is offered.')
        pipe_value = only
    else:
        pipe_spec = Spec('voice.pipe', 'voice', 'display', 'Pipe', 'live', ('voice', 'pipe'),
                         write='display', inert=True, status='No pipe is configured yet.')
        pipe_value = 'None'
    specs.append((pipe_spec, pipe_value))
    specs.extend(_speech_specs(config, 'stt', 'Speech-to-text'))
    specs.extend(_speech_specs(config, 'tts', 'Text-to-speech'))
    return specs


# The stored values are wire names the config validator accepts; the labels are the
# words a person uses for the same thing. Provider kinds are named as standard terms,
# not as guessed names a reader has to decode.
PROVIDER_KIND_LABELS = (
    ('cli_worker', 'Vendor CLI login (Codex, Claude, Grok)'),
    ('openai_chat', 'OpenAI-compatible chat API'),
    ('openai_responses', 'OpenAI-compatible responses API'),
    ('anthropic_messages', 'Anthropic messages API'),
)


def _kind_options():
    return tuple((value, label) for value, label in PROVIDER_KIND_LABELS
                 if value in PROVIDER_KINDS)


def _job_tasks_shown(config):
    explicit = config.swarm.max_job_tasks
    return '' if explicit is None else str(explicit)


def _job_tasks_status(config):
    from .endpoint_limit import yolo_mode
    if config.swarm.max_job_tasks is not None:
        return 'You set this by hand, so it is enforced in Safe mode and in Unleashed mode.'
    if yolo_mode(config):
        return 'Empty means no cap. Unleashed mode does not add the Safe default of 8.'
    return ('Empty means 8 workers in one job in Safe mode. Unleashed mode does not add that cap. '
            'A number you set by hand is enforced in both modes.')


def _distinct_shown(config):
    explicit = config.swarm.distinct_workers
    if explicit is None:
        return ''
    return 'true' if explicit else 'false'


def _distinct_status(config):
    from .endpoint_limit import yolo_mode
    explicit = config.swarm.distinct_workers
    if explicit is True:
        return 'You saved this on, so each worker needs its own backend and account in Safe mode and in Unleashed mode.'
    if explicit is False:
        return 'You saved this off, so one job may share a backend in Safe mode and in Unleashed mode.'
    if yolo_mode(config):
        return 'Unset. Unleashed mode allows workers in one job to share a backend. Safe mode would require distinct backends and accounts.'
    return 'Unset. Safe mode requires distinct backends and accounts. Unleashed mode allows a shared backend. A choice you save holds in both modes.'


def _saved_job_tasks(value):
    if value == '':
        return None
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError('Workers in one swarm job must be an integer of at least 2, or empty')
    number = int(value)
    if number < 2:
        raise ValueError('Workers in one swarm job must be an integer of at least 2, or empty')
    return number


def catalog(config, approval):
    """Every scalar the page can set, with the live value beside its spec."""
    chat = [(name, _backend_label(config, name)) for name, item in config.backends.items()
            if item.chat and item.enabled and _kind(config, item) != 'cli_worker']
    items = [
        # Identity is what makes a companion this install's own rather than a
        # copy of someone else's. Names have working defaults; the identity
        # files themselves are edited on disk.
        (Spec('agent_name', 'identity', 'string', 'Companion name', 'restart', ('agent_name',),
              tier='optional',
              hint='The name this companion answers to. It is yours to choose; nothing here assumes one.'),
         config.agent_name),
        (Spec('operator_name', 'identity', 'string', 'Your name', 'restart', ('operator_name',),
              tier='optional',
              hint='How the companion addresses you in prompts, notes and wakes. Leave it empty and it says "the operator".',
              empty_removes=True), config.operator_name),
        (Spec('autonomy.floor_name', 'identity', 'string', 'Floor display name', 'restart',
              ('autonomy', 'floor_name'), tier='advanced',
              hint='The name shown for the autonomy session. Its address is a separate setting: Floor session slug. Empty means "Autonomy".',
              empty_removes=True), config.autonomy.floor_name),
        (Spec('identity.enabled', 'identity', 'bool', 'Identity records', 'restart',
              ('identity', 'enabled'), tier='optional',
              hint='Record who a card belongs to, so an owner resolves to a person or a session instead of a free string. Off, ownership accepts any name-shaped value and a typo wakes nobody. On, an owner must name someone real, and the board shows which kind it is. This is the record layer; sign-in comes later.'),
         config.identity.enabled),
        (Spec('swarm.pressure_hold_enabled', 'models', 'bool', 'Hold local workers under pressure', 'restart',
              ('swarm', 'pressure_hold_enabled'), tier='advanced',
              hint='Off unless you turn it on. On applies in Safe mode and in Unleashed mode. Only models in infrastructure.pressure_engines; unknown readings hold until a valid sample.'),
         config.swarm.pressure_hold_enabled),
        (Spec('swarm.pressure_soft_threshold', 'models', 'float', 'Local engine soft occupancy threshold', 'restart',
              ('swarm', 'pressure_soft_threshold'), minimum=0.01, maximum=1.0, tier='advanced',
              hint='Fraction of advertised slots, counting running, waiting and warming.'),
         config.swarm.pressure_soft_threshold),
        (Spec('swarm.max_job_tasks', 'models', 'string', 'Workers in one swarm job', 'restart',
              ('swarm', 'max_job_tasks'), tier='advanced', empty_removes=True,
              hint=JOB_TASKS_HINT, status=_job_tasks_status(config)),
         _job_tasks_shown(config)),
        (Spec('swarm.distinct_workers', 'models', 'enum', 'Distinct workers in one job', 'restart',
              ('swarm', 'distinct_workers'), tier='advanced', empty_removes=True,
              options=(('false', 'Allow a shared backend'),
                       ('true', 'Require distinct backends and accounts'),
                       ('', 'Unset')),
              hint=DISTINCT_HINT, status=_distinct_status(config)),
         _distinct_shown(config)),
        (Spec('roles.main', 'models', 'enum', 'Chat model', 'live', ('roles', 'main'), options=tuple(chat),
              hint='Cloud providers are supported but will burn budgets and run API costs fast; local models are the recommended workhorse. A cloud choice is your call, made here.'),
         config.roles.get('main', '')),
        (Spec('roles.fast', 'models', 'enum', 'Fast model', 'live', ('roles', 'fast'),
              tier='optional',
              options=(('', 'Use chat model'), *chat),
              hint='Direct-routed turns and fallback judging. Empty uses the chat model.',
              empty_removes=True),
         config.roles.get('fast', '') or ''),
        (Spec('roles.compactor', 'models', 'enum', 'Compactor model', 'live', ('roles', 'compactor'),
              tier='optional',
              options=(('', 'Use chat model'), *chat),
              hint='Background summaries for long sessions. Empty uses the chat model.',
              empty_removes=True),
         config.roles.get('compactor', '') or ''),
        (Spec('roles.failover', 'models', 'enum', 'Failover', 'live', ('roles', 'failover'),
              tier='optional',
              options=(('', 'Off'), *chat), hint='Used when the chat model is unreachable.', empty_removes=True),
         config.roles.get('failover', '') or ''),
        (Spec('codex.models_url', 'models', 'string', 'Codex model list URL', 'live',
              ('codex', 'models_url'), tier='advanced',
              hint='Used after ChatGPT or Codex sign-in to list models.'),
         config.codex.models_url),
        (Spec('codex.client_version', 'models', 'string', 'Codex client version', 'live',
              ('codex', 'client_version'), tier='advanced',
              hint='Sent with the model list request. Change it here if the service expects another version.'),
         config.codex.client_version),
    ]
    providers = [('', 'Default'), ('native', 'Native')]
    for server_name in config.mcp_servers:
        providers.append((f'mcp:{server_name}', server_name))
    current_provider = config.computer_use.provider or ''
    if current_provider and current_provider not in {value for value, _label in providers}:
        providers.append((current_provider, current_provider))
    jev = config.computer_use.jev
    items.extend([
        (Spec('computer_use.provider', 'computer', 'enum', 'Provider', 'restart', ('computer_use', 'provider'),
              options=tuple(providers)), current_provider),
        (Spec('computer_use.timeout', 'computer', 'float', 'Timeout', 'restart', ('computer_use', 'timeout'),
              minimum=0.1, key='timeout', tier='advanced'), config.computer_use.timeout),
        (Spec('computer_use.jev.enabled', 'computer', 'bool', 'Use Jev for screen questions', 'restart', ('computer_use', 'jev', 'enabled'),
              hint='Native computer use asks Jev to find screen elements and check results. Session judge (Jev) configures its separate action-judging role.',
              key='enabled'), jev.enabled),
        (Spec('computer_use.jev.credential_ref', 'computer', 'string', 'Credential in use', 'restart',
              ('computer_use', 'jev', 'credential_ref'), write='display', inert=True, key='credential_ref',
              hint='Edited in config.toml. Screen questions use this credential.'),
         binding_source(config, jev.credential_ref, jev.api_key_env, oauth_clears_env=False)['label']),
        (Spec('computer_use.jev.base_url', 'computer', 'string', 'Jev base URL', 'restart',
              ('computer_use', 'jev', 'base_url'), key='base_url', tier='advanced'), jev.base_url),
        (Spec('computer_use.jev.model', 'computer', 'string', 'Jev model', 'restart',
              ('computer_use', 'jev', 'model'), key='model', tier='advanced'), jev.model),
        (Spec('computer_use.jev.timeout', 'computer', 'float', 'Jev timeout', 'restart',
              ('computer_use', 'jev', 'timeout'), minimum=0.1, key='timeout', tier='advanced'), jev.timeout),
        (Spec('computer_use.jev.min_confidence', 'computer', 'float', 'Minimum confidence', 'restart',
              ('computer_use', 'jev', 'min_confidence'), minimum=0, maximum=1, key='min_confidence', tier='optional'),
         jev.min_confidence),
    ])
    memory = config.aethermind
    from .aethermind import EMBEDDED_LOCK_REFUSAL
    from .engine_lock import EmbeddedEngineLockError, embedded_lock_backend
    try:
        reported_lock = embedded_lock_backend(config)
    except EmbeddedEngineLockError as error:
        lock_status = str(error)
    else:
        lock_status = EMBEDDED_LOCK_REFUSAL if reported_lock == 'unavailable' else ''
    requested = memory.source if memory.source in ('embedded', 'external') else (
        'external' if (memory.url or memory.endpoints) else 'embedded')
    items.extend([
        (Spec('aethermind.source', 'integrations', 'enum', 'AetherMind', 'restart',
              ('aethermind', 'source'),
              options=(('embedded', 'Embedded'), ('external', 'External')),
              hint='Embedded is the packaged engine. External uses the URL or the endpoint list only after that service is reachable and accepts its auth. If that service fails, the packaged engine keeps running and the error is shown here.',
              status=lock_status),
         requested),
        (Spec('aethermind.url', 'integrations', 'string', 'AetherMind service URL', 'restart',
              ('aethermind', 'url'),
              hint='External HTTP(S) MCP endpoint. Used only after it is reachable and accepts its auth. Otherwise the packaged engine keeps running and the failure is shown here. Ignored when ordered endpoints are set below.',
              # Name the way out, not just the conflict. "Not used" alone leaves a person
              # looking at a disabled box with no stated path back to a working one.
              status=(f'Not used: {len(memory.endpoints)} ordered endpoint(s) are set below and take '
                      'precedence. Clear that list to use this field.'
                      if memory.endpoints else ''),
              inert=bool(memory.endpoints)), memory.url),
        (Spec('aethermind.endpoints', 'integrations', 'list', 'Ordered endpoints', 'restart',
              ('aethermind', 'endpoints'), lines=True, tier='optional',
              hint='One MCP endpoint per line, in the order they are tried. When this list is non-empty it overrides the URL above, and the URL field refuses a save. Leave empty to use the URL.'), '\n'.join(memory.endpoints or [])),
        (Spec('aethermind.project', 'integrations', 'string', 'Memory project root', 'restart',
              ('aethermind', 'project'),
              hint='Project root on the AetherMind service host. Existing projects are used without creating a store.'), memory.project),
        (Spec('aethermind.bundled_path', 'integrations', 'string', 'Packaged engine path', 'restart',
              ('aethermind', 'bundled_path'), tier='advanced',
              hint='Path to a self-contained AetherMind engine copy. This is the memory service unless an external URL or endpoint list validates. Relative paths sit beside the home directory.'), memory.bundled_path),
        (Spec('aethermind.engine_log_max_bytes', 'integrations', 'int', 'Engine log cap', 'restart',
              ('aethermind', 'engine_log_max_bytes'), minimum=1, tier='advanced',
              hint='Bytes kept in the packaged engine log. The log view shows this cap. The newest bytes stay.'),
         memory.engine_log_max_bytes),
    ])
    stored = getattr(config, 'retired_settings', None)
    if isinstance(stored, dict) and 'aethermind.enabled' in stored:
        items.append((Spec('aethermind.enabled', 'integrations', 'string', 'Retired memory switch', 'restart',
                           ('aethermind', 'enabled'), tier='optional', inert=True, write='display',
                           hint='Retired. A value left in an older config.toml or in TESSERA_AETHERMIND_ENABLED is shown here and ignored. It cannot turn memory off.'),
                      retired_memory_label(config)))
    autonomy = config.autonomy
    world_status = ''
    try:
        stored_world_view = peek_world_view(config)
    except FloorDefinitionError as exc:
        stored_world_view = None
        world_status = str(exc)
    items.extend([
        (Spec('autonomy.enabled', 'autonomy', 'bool', 'Autonomy scheduler and tools', 'restart',
              ('autonomy', 'enabled'),
              hint='Enable background wakes and autonomy tools. Off leaves existing sessions intact and usable. Restart to apply.'), autonomy.enabled),
        (Spec('autonomy.infinity_slug', 'autonomy', 'string', 'Floor session slug', 'restart',
              ('autonomy', 'infinity_slug'), tier='advanced',
              hint='The session address: the name its state and cockpit lane are keyed under, not its display name.'), autonomy.infinity_slug),
        (Spec('autonomy.world_view', 'autonomy', 'string', 'Autonomy priorities', 'restart',
              ('autonomy', 'world_view'), lines=True, status=world_status,
              hint='Text in each floor wake. Its character limit is a Floor definition setting, not a fixed 2,000. Not a world-state connection.'),
         stored_world_view if stored_world_view is not None else autonomy.world_view),
        (Spec('autonomy.min_wake_gap_seconds', 'autonomy', 'float', 'Minimum wake gap', 'restart',
              ('autonomy', 'min_wake_gap_seconds'), minimum=0, tier='advanced',
              hint='Minimum seconds between delivered autonomy wakes across all sessions. Pending events wait; zero removes this throttle.'), autonomy.min_wake_gap_seconds),
        (Spec('autonomy.self_prompt_gap_seconds', 'autonomy', 'float', 'Self-prompt gap', 'restart',
              ('autonomy', 'self_prompt_gap_seconds'), minimum=0, tier='advanced',
              hint='With self-prompting enabled, seconds between eligible agenda/floor prompts and intention refreshes. Zero removes the delay.'), autonomy.self_prompt_gap_seconds),
        (Spec('autonomy.oversight_policy', 'autonomy', 'enum', 'Oversight report mode',
              'restart', ('autonomy', 'oversight_policy'), tier='optional',
              options=(('findings', 'Findings only'),
                       ('approve', 'Findings and proposals (default)'),
                       ('auto', 'Findings and trial status')),
              hint='Choose what the read-only oversight report adds to findings. This setting does not gate changes or start trials.'), autonomy.oversight_policy),
        (Spec('autonomy.trial_poll_seconds', 'autonomy', 'float', 'Trial re-read interval', 'restart',
              ('autonomy', 'trial_poll_seconds'), minimum=0, tier='advanced',
              hint='Minimum seconds between trial checks, shared by autonomy ticks and turn completions. Zero checks on every call; it does not start a timer.'), autonomy.trial_poll_seconds),
    ])
    jev = config.jev
    items.extend([
        (Spec('jev.enabled', 'judge', 'bool', 'Judge session actions', 'restart',
              ('jev', 'enabled'),
              hint='Run the Jev judge: it rates turns and supplies the judgment the action gate consults. '
                   'Off means no judge and no turn scoring. It does not by itself stop the action gate; '
                   'that is the Action gate switch below.'), jev.enabled),
        (Spec('jev.action_gate_enabled', 'judge', 'bool', 'Action gate', 'live',
              ('jev', 'action_gate_enabled'),
              hint='Gate mutating tool actions through the judge and refuse the ones it says would '
                   'interrupt this session\'s serving or reasoning. Off means mutating tools run without '
                   'an action gate, whatever jev.enabled says.'), jev.action_gate_enabled),
        (Spec('jev.url', 'judge', 'string', 'Jev service URL', 'restart',
              ('jev', 'url'),
              hint='HTTP(S) endpoint of the Jev judge service. Leave empty to run without one. '
                   'The public service is the usual answer; change this only for a proxy or a '
                   'local model that speaks the same API.',
              tier='advanced'), jev.url),
        (Spec('jev.model', 'judge', 'string', 'Jev model', 'restart',
              ('jev', 'model'),
              hint='Model the judge service should use, when the service takes a model name.',
              tier='advanced'), jev.model),
        (Spec('jev.api_key_env', 'judge', 'string', 'Jev API key env var', 'restart',
              ('jev', 'api_key_env'),
              hint='Name of the environment variable holding the Jev service key. Prefer a configured credential.'), jev.api_key_env),
        (Spec('jev.credential_ref', 'judge', 'string', 'Jev credential', 'restart',
              ('jev', 'credential_ref'),
              hint='Name of a configured credential to use for the Jev service instead of an env var.',
              tier='advanced'), jev.credential_ref),
        (Spec('jev.timeout', 'judge', 'float', 'Judge timeout', 'restart',
              ('jev', 'timeout'), minimum=0.1,
              hint='Seconds one judge call may take before the session falls back to its default policy.',
              tier='optional'), jev.timeout),
        (Spec('jev.gate_threshold', 'judge', 'float', 'Session gate bar', 'restart',
              ('jev', 'gate_threshold'), minimum=0, maximum=1,
              hint='Bar for the session, merge and completion gates; the contextual action gate uses its own fixed policy constants (0.7 deny / 0.20 soft floor) and is not tuned here.',
              tier='optional'), jev.gate_threshold),
        (Spec('jev.goal_threshold', 'judge', 'float', 'Complete-or-improve bar', 'restart',
              ('jev', 'goal_threshold'), minimum=0, maximum=1,
              hint='Minimum confidence that a turn improved or completed the goal before the loop considers it done. Raise it to demand more certainty.',
              tier='optional'), jev.goal_threshold),
        (Spec('jev.risk_threshold', 'judge', 'float', 'Risk bar', 'restart',
              ('jev', 'risk_threshold'), minimum=0, maximum=1,
              hint='Applies to session-tool risk enforcement, not the contextual action gate, whose thresholds are fixed (0.7 deny / 0.20 soft floor).',
              tier='optional'), jev.risk_threshold),
        (Spec('jev.loop_window', 'judge', 'int', 'Judge window', 'restart',
              ('jev', 'loop_window'), minimum=2,
              hint='How many recent turns the judge weighs when scoring the loop.',
              tier='advanced'), jev.loop_window),
        (Spec('jev.batch_items', 'judge', 'int', 'Judge batch size', 'restart',
              ('jev', 'batch_items'), minimum=1,
              hint='How many actions the judge can review in one batch.',
              tier='advanced'), jev.batch_items),
        (Spec('jev.human_gate', 'judge', 'bool', 'Hand uncertain calls to a person', 'restart',
              ('jev', 'human_gate'),
              hint='When the judge cannot decide confidently, ask the operator instead of defaulting one way.',
              tier='optional'), jev.human_gate),
        (Spec('jev.session_gates_enabled', 'judge', 'bool', 'Gate session tools', 'restart',
              ('jev', 'session_gates_enabled'),
              hint='Run session tools through the judge gate. Off means the approval policy alone decides.',
              tier='optional'), jev.session_gates_enabled),
    ])
    voice = config.voice
    voices = [item for item in voice.voices if isinstance(item, str) and item]
    current_voice = str((voice.session_options or {}).get('voice') or '')
    if voices:
        voice_options = [(item, item) for item in voices]
        if current_voice and current_voice not in voices:
            voice_options.append((current_voice, current_voice))
        voice_spec = Spec('voice.voice', 'voice', 'enum', 'Voice', 'live', ('voice', 'session_options', 'voice'),
                          options=tuple(voice_options), key='voice', tier='optional',
                          hint='Used on the next voice start. An explicit Voice page choice overrides it for that tab until this setting changes.')
    else:
        voice_spec = Spec('voice.voice', 'voice', 'string', 'Voice', 'live', ('voice', 'session_options', 'voice'),
                          key='voice', tier='optional',
                          hint='Used on the next voice start. An explicit Voice page choice overrides it for that tab until this setting changes.')
    items.extend(_voice_specs(config))
    items.extend([
        (voice_spec, current_voice),
        (Spec('voice.max_session_seconds', 'voice', 'int', 'Session seconds', 'live', ('voice', 'max_session_seconds'),
              minimum=1, maximum=86400, key='max_session_seconds', tier='advanced'), voice.max_session_seconds),
        # The dedicated voice session: its own name and its own reasoning posture.
        # A spoken turn is answered in a sentence or two, so a deep reasoning pass
        # there is latency the speaker hears. Reconcile after a core restart and
        # on each voice start, applying only changed Settings values.
        (Spec('voice.dedicated_title', 'voice', 'string', 'Voice session name', 'restart',
              ('voice', 'dedicated_title'), tier='optional',
              hint='After the core restarts, applies the name to the existing voice session in the sidebar and on the Voice page.'), voice.dedicated_title),
        (Spec('voice.dedicated_reasoning', 'voice', 'enum', 'Voice reasoning', 'restart',
              ('voice', 'dedicated_reasoning'), tier='advanced', options=(('off', 'Off'), ('low', 'Low'), ('medium', 'Medium'), ('high', 'High')),
              hint='After the core restarts, applies changed Settings to the existing voice session. Low keeps spoken answers quick.'), voice.dedicated_reasoning),
        (Spec('voice.dedicated_thinking_budget', 'voice', 'int', 'Voice thinking budget', 'restart',
              ('voice', 'dedicated_thinking_budget'), minimum=0, tier='advanced',
              hint='After the core restarts, applies changed Settings to the existing voice session. 0 uses the engine default; used only with reasoning on.'), voice.dedicated_thinking_budget),
    ])
    ui = config.ui
    items.extend([
        (Spec('ui.theme', 'appearance', 'enum', 'Theme', 'live', ('ui', 'theme'),
              tier='optional',
              options=(('system', 'System'), ('light', 'Light'), ('dark', 'Dark')), key='theme'), ui.theme),
        (Spec('ui.mark', 'appearance', 'enum', 'Icon and logo', 'live', ('ui', 'mark'),
              tier='optional',
              options=(('cairn', 'Tessera'), ('gremlin', 'Gremlin')),
              hint='Chooses the logo art and follows the theme. The default includes the Tessera-CT wordmark and the tree. The F-it button adds the gremlin favicon and the goblin crew.',
              status=('F-it button is on, so the gremlin logo, favicon and crew are showing.' if ui.easter_egg_f_it
                      else 'Showing the gremlin logo.' if ui.mark == 'gremlin'
                      else 'Showing the Tessera art with the Tessera-CT wordmark.'),
              key='mark'), ui.mark),
        (Spec('ui.skin', 'appearance', 'enum', 'Skin', 'live', ('ui', 'skin'),
              tier='optional',
              options=(('strata', 'Strata · warm stone'), ('basalt', 'Basalt · copper and mineral'), ('tide', 'Tide · deep blue and sea glass')),
              hint='Each skin follows your light, dark, or system theme.', key='skin'), ui.skin),
        (Spec('ui.accent_color', 'appearance', 'color', 'Accent color', 'live', ('ui', 'accent_color'),
              tier='optional',
              hint='Choose your own accent, or return to the skin color.', key='accent_color'), ui.accent_color),
        (Spec('ui.show_thoughts', 'appearance', 'bool', 'Show thinking', 'live', ('ui', 'show_thoughts'),
              tier='optional', key='show_thoughts'), ui.show_thoughts),
        (Spec('ui.yolo_banner', 'appearance', 'bool', 'Show Unleashed banner', 'live', ('ui', 'yolo_banner'),
              tier='optional', hint='Show the banner while Unleashed mode is on.', key='yolo_banner'), ui.yolo_banner),
        (Spec('ui.easter_egg_f_it', 'appearance', 'bool', 'F-it button', 'live', ('ui', 'easter_egg_f_it'),
              tier='optional',
              hint='When on, the button reads F-it and shows the cartoon, and the gremlin logo, favicon and goblin crew turn on. The stored mode stays yolo.',
              key='easter_egg_f_it'), ui.easter_egg_f_it),
        (Spec('ui.send_shortcut', 'appearance', 'enum', 'Send with', 'live', ('ui', 'send_shortcut'),
              tier='optional',
              options=(('enter', 'Enter'), ('modifier', 'Ctrl or Command + Enter')), key='send_shortcut'),
         ui.send_shortcut),
        (Spec('ui.config_journal_entries', 'appearance', 'int', 'Config change log entries', 'live',
              ('ui', 'config_journal_entries'), minimum=1, tier='advanced',
              hint='How many newest config changes this page lists. It says how many it is showing, and the log beside config.toml keeps only that many lines.'),
         ui.config_journal_entries),
    ])
    mode = 'yolo' if approval.get('yolo') else 'safe'
    # The switch is a runtime override kept in a state file; the policy in config.toml is
    # what Safe means. A page that shows only "Unleashed" hides the configured value, so when
    # the two disagree the field says so instead of leaving the reader to guess.
    configured = approval.get('configured', '')
    safe_mode = approval.get('safe_mode', '')
    hint = ''
    if configured and configured != approval.get('mode'):
        hint = (f'The switch is a runtime override: config.toml says {configured}, and Safe '
                f'would apply {SAFE_FACT.get(safe_mode, safe_mode)}. Flipping to Safe writes '
                f'the state file, not the config.')
    items.append((Spec('permissions.mode', 'permissions', 'enum', 'Mode', 'live', write='approval',
                       options=(('safe', 'Safe'), ('yolo', 'Unleashed')),
                       status=SAFE_FACT.get(safe_mode, ''), hint=hint, key='mode'), mode))
    return items


def _access_fact(config, name):
    """How this provider reaches a model: the credential a request would use."""
    return credential_source(config, name)['label']


def _concurrency_value(raw):
    if raw in ('', None):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError(
                'Max concurrent requests must be a positive integer, or empty to leave it unset. '
                'Empty is not zero requests. Safe mode then allows 1 on a local model and no cap on a cloud provider. '
                'Unleashed mode does not add a cap.')
        raw = int(text)
    if isinstance(raw, bool) or type(raw) is not int or raw < 1:
        raise ValueError(
            'Max concurrent requests must be a positive integer, or empty to leave it unset. '
            'Empty is not zero requests. Safe mode then allows 1 on a local model and no cap on a cloud provider. '
            'Unleashed mode does not add a cap.')
    return raw


def _optional_rate(raw, label):
    """Empty is unlimited. A hand-set positive integer is the pace in both modes."""
    if raw in ('', None):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if not text.isdigit():
            raise ValueError(
                f'{label} must be a positive integer, or empty to leave it unset. '
                'Empty is unlimited, not zero. A number you set by hand is enforced in Safe mode and in Unleashed mode.')
        raw = int(text)
    if isinstance(raw, bool) or type(raw) is not int or raw < 1:
        raise ValueError(
            f'{label} must be a positive integer, or empty to leave it unset. '
            'Empty is unlimited, not zero. A number you set by hand is enforced in Safe mode and in Unleashed mode.')
    return raw


def _append_throttle(edits, name, record):
    for key, label in (('throttle_requests_per_minute', 'Requests per minute'),
                       ('throttle_tokens_per_minute', 'Tokens per minute')):
        if key not in record:
            continue
        edits.append({'path': ['backends', name, key], 'value': _optional_rate(record.get(key), label)})


def _throttle_field(config, name, backend, key, label):
    explicit = getattr(backend, key)
    shown = '' if explicit is None else str(explicit)
    if explicit is not None:
        status = (f'In effect: {explicit} per minute. You set this by hand, so it is enforced in Safe mode and in Unleashed mode. '
                  'It paces this endpoint only and does not cap the swarm.')
    else:
        status = ('In effect: no throttle. You did not set a pace by hand. '
                  'Empty stays unlimited in Safe mode and in Unleashed mode. '
                  'This does not cap the swarm.')
    return dump_field(Spec(
        f'providers.{name}.{key}', 'providers', 'string', label, 'live',
        hint=THROTTLE_HINT, status=status, key=key), shown)


def _append_concurrency(edits, name, record):
    if 'max_concurrent_requests' not in record:
        return
    edits.append({'path': ['backends', name, 'max_concurrent_requests'],
                  'value': _concurrency_value(record.get('max_concurrent_requests'))})


def _concurrency_field(config, name, backend):
    from .endpoint_limit import LOCAL_MODEL_CONCURRENCY, _is_local, request_limit, yolo_mode
    explicit = backend.max_concurrent_requests
    shown = '' if explicit is None else str(explicit)
    limit = request_limit(config, name)
    local = _is_local(config, name)
    if explicit is not None and local:
        status = (f'In effect: {limit} at a time. You set this by hand, so it is enforced in Safe mode and in Unleashed mode. '
                  'It protects local hardware.')
    elif explicit is not None:
        status = (f'In effect: {limit} at a time. You set this by hand, so it is enforced in Safe mode and in Unleashed mode.')
    elif yolo_mode(config):
        status = 'In effect: no cap. You did not set a limit by hand. Unleashed mode drops the Safe-mode default and does not add one.'
    elif local:
        status = (f'In effect: {LOCAL_MODEL_CONCURRENCY} at a time in Safe mode. '
                  'This default protects local hardware. It is not saved until you enter a number. '
                  'Unleashed mode drops this default. A number you set by hand is enforced in both modes.')
    else:
        status = 'In effect: no cap. A cloud provider stays unlimited until you set a number by hand.'
    return dump_field(Spec(
        f'providers.{name}.max_concurrent_requests', 'providers', 'string', 'Max concurrent requests', 'live',
        hint=CONCURRENCY_HINT, status=status, key='max_concurrent_requests'), shown)


def _provider_fields(config, name, backend):
    kind = _kind(config, backend)
    cli = dict((backend.extra or {}).get('cli') or {})
    fields = [
        dump_field(Spec(f'providers.{name}.enabled', 'providers', 'bool', 'Enabled', 'live', inline=True, key='enabled'),
                   backend.enabled),
        dump_field(Spec(f'providers.{name}.chat', 'providers', 'bool', 'Use for chat', 'live', key='chat'), backend.chat),
    ]
    if kind == 'cli_worker':
        if cli.get('vendor') == 'codex':
            fields.append(dump_field(Spec(f'providers.{name}.model', 'providers', 'string',
                                          'Model', 'live', key='model'), backend.model))
        fields.extend([
            dump_field(Spec(f'providers.{name}.vendor', 'providers', 'enum', 'Vendor', 'live',
                            options=tuple((item, item) for item in VENDORS), key='vendor'), cli.get('vendor', '')),
            dump_field(Spec(f'providers.{name}.command', 'providers', 'string', 'Command', 'live', key='command'),
                       cli.get('command', '')),
            dump_field(Spec(f'providers.{name}.cwd', 'providers', 'string', 'Working directory', 'live', key='cwd'),
                       cli.get('cwd', '')),
            dump_field(Spec(f'providers.{name}.args', 'providers', 'string', 'Arguments', 'live', lines=True, key='args'),
                       '\n'.join(cli.get('args') or [])),
        ])
        if cli.get('vendor') == 'grok':
            fields.extend([
                dump_field(Spec(f'providers.{name}.allow_api_key_fallback', 'providers', 'bool',
                                'API key fallback', 'live',
                                inline=True, key='allow_api_key_fallback',
                                hint='Off, a CLI login never uses an API key. On, a failed login may use the fallback env var.'),
                           cli.get('allow_api_key_fallback') is True),
                dump_field(Spec(f'providers.{name}.fallback_env', 'providers', 'string', 'Fallback env var', 'live',
                                key='fallback_env'), cli.get('fallback_env', '')),
            ])
    else:
        fields.extend([
            dump_field(Spec(f'providers.{name}.model', 'providers', 'string', 'Model', 'live', key='model',
                            hint='The model id the server expects, such as llama-3.1-8b-demo.'), backend.model),
        ])
    fields.append(_concurrency_field(config, name, backend))
    fields.append(_throttle_field(config, name, backend, 'throttle_requests_per_minute', 'Requests per minute'))
    fields.append(_throttle_field(config, name, backend, 'throttle_tokens_per_minute', 'Tokens per minute'))
    return fields


def _provider_actions(config, name, backend):
    return ['edit', 'remove']


def _local_first_chat_backend(config, exclude):
    """Pick a remaining chat model, preferring one with no credential.

    A local model server carries no API credential, so it sorts first. CLI
    workers and cloud backends sort after it. The name breaks ties so the
    choice is stable.
    """
    choices = []
    for name, backend in config.backends.items():
        if name == exclude or not backend.enabled or not backend.chat:
            continue
        has_credential = bool(
            backend.api_key_env or backend.credential_ref or backend.account
            or backend.provider_kind == 'cli_worker')
        choices.append((has_credential, name))
    if not choices:
        return ''
    choices.sort()
    return choices[0][1]


def _oauth_actions(report):
    actions = ['sign-in']
    if report['has_refresh']:
        actions.append('refresh')
    if report.get('state') == 'current':
        actions.append('add-model')
    return actions


def _oauth_status(config, spec):
    return grant_report(config.home, spec['oauth'])['status']


def oauth_accounts_field(config):
    records = []
    for key, spec in OAUTH_MODELS.items():
        report = grant_report(config.home, spec['oauth'])
        records.append({
            'name': key,
            'title': 'xAI Grok' if key == 'xai' else 'ChatGPT or Codex',
            'status': _grant_status(report),
            'actions': _oauth_actions(report),
            'fields': [],
            'preset': {'account': key},
        })
    return {
        'id': 'oauth_accounts', 'section': 'providers', 'type': 'list-of-records',
        'label': 'Sign in to a provider',
        'value': records, 'write': 'records', 'apply': 'live', 'actions': [],
        'blank': [], 'records': records, 'tier': 'optional',
        'hint': 'Sign-in is one way to connect a provider. A local server or other endpoint also counts. The app holds the grant; no other program is needed.',
    }


def _account_specs(config, name):
    return [
        Spec(f'accounts.{name}.provider_kind', 'accounts', 'enum', 'Connection type', 'restart',
             ('accounts', name, 'provider_kind'), options=_kind_options(),
             hint='How this account reaches the model. A CLI login is the vendor program you '
                  'are already signed into; the API types need a credential.',
             key='provider_kind'),
        Spec(f'accounts.{name}.credential_ref', 'accounts', 'enum', 'Credential', 'restart',
             ('accounts', name, 'credential_ref'), options=_credential_options(config),
             hint='A credential configured on this page. A CLI login does not use one.',
             key='credential_ref'),
    ]


def accounts_field(config):
    records = []
    for name, account in config.accounts.items():
        values = {
            'provider_kind': account.provider_kind, 'credential_ref': account.credential_ref,
        }
        status = account.provider_kind
        records.append({
            'name': name, 'title': name, 'status': status, 'actions': ['edit', 'remove'],
            'fields': [dump_field(spec, values[spec.key]) for spec in _account_specs(config, name)],
        })
    defaults = {'provider_kind': 'cli_worker', 'credential_ref': ''}
    blank = [dump_field(Spec('accounts.new.name', 'accounts', 'string', 'Name', 'restart', key='name'), '')]
    blank.extend(dump_field(spec, defaults[spec.key]) for spec in _account_specs(config, 'new'))
    return {
        'id': 'named_accounts', 'section': 'integrations', 'type': 'list-of-records', 'label': 'Worker pools',
        'tier': 'advanced',
        'value': records, 'write': 'records', 'apply': 'restart', 'actions': ['add', 'edit', 'remove'],
        'blank': blank, 'records': records,
        'hint': 'A pool shares a credential and budgets. It does not cap how many agents run. '
                'Each model has its own max concurrent requests. This is not a sign-in.',
    }


def identities_field(config, manager=None):
    """People and live sessions, plus the owner strings that resolve to nobody.

    Only present when identity records are on. Off is the default, and off must leave
    the payload exactly as it was: an install that never opted in should not grow a
    row it cannot use.

    The unresolved list rides on the same field as ``unresolved`` rather than becoming
    rows: those strings are not records, they are evidence that a card is owned by a
    handle nobody holds, and letting them be edited as records would hide the defect
    the layer exists to surface.
    """
    registry = getattr(config, 'identity_registry', None)
    if registry is None or not registry.enabled:
        return None
    records = []
    for identity in registry.all():
        if identity['kind'] == 'person':
            status = 'person · v%d' % identity['version']
            actions = ['edit']
        else:
            # A session is read from the session manager, never stored here, so there
            # is nothing to rename: its name is its slug and its id is its meta.json.
            status = 'session'
            actions = []
        records.append({'name': identity['name'],
                        'title': identity.get('display_name') or identity['name'],
                        'status': status, 'actions': actions,
                        'fields': [dump_field(Spec('identities.name', 'identity', 'string', 'Handle',
                                                   'live', key='name'), identity['name']),
                                   dump_field(Spec('identities.display_name', 'identity', 'string',
                                                   'Display name', 'live', key='display_name'),
                                              identity.get('display_name') or '')]})
    unresolved = []
    view = None
    if manager is not None:
        try:
            from .identities import unresolved_owners
            view = manager.identities_view()
            unresolved = view.get('unresolved_owners') or []
        except Exception:
            # A settings page must not fail because the board is unreadable.
            unresolved = []
    field = {
        'id': 'identities', 'section': 'identity', 'type': 'list-of-records', 'label': 'People',
        'value': records, 'write': 'records', 'apply': 'live', 'actions': ['add', 'edit'],
        'blank': [dump_field(Spec('identities.new.name', 'identity', 'string', 'Name', 'live', key='name'), ''),
                  dump_field(Spec('identities.new.display_name', 'identity', 'string', 'Display name',
                                  'live', key='display_name'), '')],
        'records': records,
        'unresolved': unresolved,
        'record_errors': list(view.get('record_errors') or []) if isinstance(view, dict) else [],
        'hint': 'Who a card belongs to. A person is a record you can add here; a session is '
                'read live from its own meta.json. An owner that resolves to nobody is listed '
                'below rather than hidden: it is a card that wakes no one.',
    }
    return field


def _provider_group(config, name, backend):
    """The provider this model belongs to. A model is never its own provider."""
    kind = _kind(config, backend)
    cli = (backend.extra or {}).get('cli') or {}
    if kind == 'cli_worker':
        vendor = cli.get('vendor') if isinstance(cli.get('vendor'), str) and cli.get('vendor') else 'CLI'
        return vendor + ' CLI'
    reference = backend.credential_ref
    pool = config.accounts.get(backend.account) if backend.account else None
    if pool is not None:
        reference = pool.credential_ref or reference
    cred = config.credentials.get(reference) if reference else None
    oauth = (getattr(cred, 'oauth', '') or '') if cred is not None else ''
    if oauth == 'xai':
        return 'xAI Grok'
    if oauth == 'openai-codex':
        return 'ChatGPT or Codex'
    return name


def providers_field(config):
    records = []
    for name, backend in config.backends.items():
        source = credential_source(config, name)
        records.append({
            'name': name, 'title': backend.model or name, 'status': _access_fact(config, name),
            'actions': _provider_actions(config, name, backend),
            'fields': _provider_fields(config, backend=backend, name=name),
            'group': _provider_group(config, name, backend),
            'source': source['label'],
            'source_label': 'Credential in use',
            'source_kind': source['source'],
        })
    blank = [
        dump_field(Spec('providers.new.name', 'providers', 'string', 'Name', 'live', key='name',
                        hint='A short name. Spaces are saved as dashes, and the id is shown here before you save.'), ''),
        dump_field(Spec('providers.new.kind', 'providers', 'enum', 'HTTP protocol', 'live',
                        options=(('openai_chat', 'OpenAI-compatible chat'),
                                 ('openai_responses', 'OpenAI Responses')),
                        hint='For local servers, choose the protocol documented by your model server. Adding a model does not download or start it.',
                        key='kind'), 'openai_chat'),
        # Base URL before Model: discovery asks that endpoint what it serves, so the
        # field it needs has to be reachable before the field it fills.
        dump_field(Spec('providers.new.base_url', 'providers', 'string', 'Base URL', 'live', key='base_url',
                        hint='The server address, with http:// and the port. Example: http://127.0.0.1:8080/v1'), ''),
        dump_field(Spec('providers.new.model', 'providers', 'string', 'Model', 'live', key='model',
                        hint='The model id the server expects, such as llama-3.1-8b-demo.'), ''),
        dump_field(Spec('providers.new.api_key_env', 'providers', 'string', 'API key env var', 'live',
                        hint='The name of an environment variable. The value is never shown. A sign-in grant does not fall back to it.',
                        key='api_key_env'), ''),
        dump_field(Spec('providers.new.chat', 'providers', 'bool', 'Use for chat', 'live', key='chat'), True),
        dump_field(Spec('providers.new.max_concurrent_requests', 'providers', 'string', 'Max concurrent requests', 'live',
                        hint=CONCURRENCY_HINT,
                        status=('Leave this empty and Safe mode allows 1 request at a time on a local model, '
                                'which protects local hardware. A cloud provider has no cap until you set a number by hand. '
                                'Unleashed mode drops that default. A number you set by hand is enforced in both modes.'),
                        key='max_concurrent_requests'), ''),
        dump_field(Spec('providers.new.throttle_requests_per_minute', 'providers', 'string', 'Requests per minute', 'live',
                        hint=THROTTLE_HINT,
                        status=('Leave this empty and the endpoint stays unlimited in Safe mode and in Unleashed mode. '
                                'A number you set by hand is enforced in both. This does not cap the swarm.'),
                        key='throttle_requests_per_minute'), ''),
        dump_field(Spec('providers.new.throttle_tokens_per_minute', 'providers', 'string', 'Tokens per minute', 'live',
                        hint=THROTTLE_HINT,
                        status=('Leave this empty and the endpoint stays unlimited in Safe mode and in Unleashed mode. '
                                'A number you set by hand is enforced in both. This does not cap the swarm.'),
                        key='throttle_tokens_per_minute'), ''),
    ]
    return {
        'id': 'providers', 'section': 'providers', 'type': 'list-of-records', 'label': 'Models',
        'value': records, 'write': 'records', 'apply': 'live', 'actions': ['add', 'edit', 'remove'],
        'blank': blank, 'records': records,
        'hint': 'Each model shows the credential a request would use. A sign-in grant does not fall back to an API key. Saving configuration does not run inference.',
    }


def _credential_unavailable(text):
    return {
        'source_kind': 'unavailable', 'type_tag': 'Unavailable', 'stored': text,
        'added': 'Not recorded', 'pill': 'Not connected', 'status': text,
    }


def _grant_status(report):
    """Settings copy for a grant. Expired is named without refreshing the token."""
    if report.get('state') == 'expired':
        return 'Signed in, token expired'
    return report.get('status') or ''


def _oauth_presence(config, oauth):
    report = grant_report(config.home, oauth)
    status = _grant_status(report)
    who = 'xAI Grok sign-in' if oauth == 'xai' else ('ChatGPT or Codex sign-in' if oauth == 'openai-codex' else 'Sign-in')
    shown = '' if status == 'Not signed in' else status
    return {
        'source_kind': 'oauth', 'type_tag': 'Sign-in',
        'stored': who if not shown else who + '. ' + shown,
        'added': 'Not recorded',
        'pill': 'Connected' if report.get('state') == 'current' else 'Not connected',
        'status': status,
    }


def _projection_keychain(manager, keychain):
    if keychain is not None:
        return keychain
    settings = getattr(manager, 'settings', None) if manager is not None else None
    found = getattr(settings, 'keychain', None) if settings is not None else None
    return found if found is not None else KeychainStore()


def _keychain_phrase(service, account, keychain):
    """Existence only. The phrase names the item; the secret is not read."""
    store = keychain if keychain is not None else KeychainStore()
    try:
        state = store.exists(service, account)
    except ValueError:
        state = 'unavailable'
    if state == 'found':
        return 'Keychain item found'
    if state == 'missing':
        return 'Keychain item missing'
    return 'Keychain not available on this system'


def _credential_resolution(config, name, reference, keychain=None):
    """Same order as CredentialResolver.resolve, from presence only.

    An environment variable is set or not. A keychain row is an existence
    check that does not read the secret and does not prompt. A grant is the
    file on disk and its expiry. This does not refresh a token or write the grant.
    """
    seen = set()
    current_name = name
    current = reference
    while current_name:
        if current_name in seen:
            return _credential_unavailable('Credential config reference cycle')
        seen.add(current_name)
        if current is None:
            return _credential_unavailable('Credential config reference is not configured')
        oauth = (getattr(current, 'oauth', '') or '').strip()
        if oauth in ('xai', 'openai-codex'):
            return _oauth_presence(config, oauth)
        env = getattr(current, 'env', '') or ''
        if env and str(os.environ.get(env, '')).strip():
            return {
                'source_kind': 'env', 'type_tag': 'Env var',
                'stored': 'Environment variable ' + env,
                'added': 'Not recorded', 'pill': 'Configured', 'status': 'Env var ' + env,
            }
        service = getattr(current, 'keychain_service', '') or ''
        account = getattr(current, 'keychain_account', '') or ''
        if service and account:
            phrase = _keychain_phrase(service, account, keychain)
            return {
                'source_kind': 'keychain', 'type_tag': 'Keychain',
                'stored': phrase, 'added': 'Not recorded', 'pill': phrase, 'status': phrase,
            }
        nxt = getattr(current, 'config_ref', '') or ''
        if not nxt:
            if env:
                return _credential_unavailable(
                    'Credential unavailable: environment unset; keychain not configured; no config fallback')
            return {
                'source_kind': 'none', 'type_tag': 'Unset', 'stored': 'No source configured',
                'added': 'Not recorded', 'pill': 'Not connected', 'status': 'No key',
            }
        current_name = nxt
        current = config.credentials.get(nxt)
    return {
        'source_kind': 'none', 'type_tag': 'Unset', 'stored': 'No source configured',
        'added': 'Not recorded', 'pill': 'Not connected', 'status': 'No key',
    }


def _credential_line(name, fact, reference):
    """Kind of source, once. The status phrase stays on the pill."""
    kind = fact.get('source_kind')
    if kind == 'keychain':
        return 'Keychain item: ' + name
    if kind == 'env':
        env = (getattr(reference, 'env', '') or '').strip()
        return 'Env var: ' + (env or name)
    tag = (fact.get('type_tag') or '').strip()
    if tag:
        return tag + ': ' + name
    return fact.get('stored') or ''


def _dated(value):
    text = (value or '').strip()
    if text == 'Not recorded':
        return ''
    return text


def _credential_meta(config, name, reference, keychain=None):
    """Where a saved credential lives. The value itself is never included."""
    fact = _credential_resolution(config, name, reference, keychain)
    meta = {key: fact[key] for key in ('source_kind', 'type_tag', 'stored', 'added', 'pill')}
    meta['added'] = _dated(meta.get('added'))
    meta['line'] = _credential_line(name, meta, reference)
    meta['status'] = fact['status']
    return meta


def credentials_field(config, keychain=None):
    records = []
    for name, reference in config.credentials.items():
        meta = _credential_meta(config, name, reference, keychain)
        if (reference.oauth or '').strip():
            records.append({
                'name': name, 'title': name, 'status': meta['status'],
                'actions': ['remove'], 'fields': [], **meta,
            })
            continue
        records.append({
            'name': name, 'title': name, 'status': meta['status'],
            'actions': ['edit', 'remove'], **meta,
            'fields': [
                dump_field(Spec(f'credentials.{name}.keychain_service', 'credentials', 'string', 'Keychain service',
                                'live', key='keychain_service'), reference.keychain_service),
                dump_field(Spec(f'credentials.{name}.keychain_account', 'credentials', 'string', 'Keychain account',
                                'live', key='keychain_account'), reference.keychain_account),
                dump_field(Spec(f'credentials.{name}.env', 'credentials', 'string', 'Env var', 'live', key='env'),
                           reference.env),
                dump_field(Spec(f'credentials.{name}.secret', 'credentials', 'secret', 'Key', 'live', key='secret'), ''),
            ],
        })
    blank = [
        dump_field(Spec('credentials.new.name', 'credentials', 'string', 'Name', 'live', key='name'), ''),
        dump_field(Spec('credentials.new.keychain_service', 'credentials', 'string', 'Keychain service', 'live',
                        key='keychain_service'), ''),
        dump_field(Spec('credentials.new.keychain_account', 'credentials', 'string', 'Keychain account', 'live',
                        key='keychain_account'), ''),
        dump_field(Spec('credentials.new.env', 'credentials', 'string', 'Env var', 'live', key='env'), ''),
        dump_field(Spec('credentials.new.secret', 'credentials', 'secret', 'Key', 'live', key='secret'), ''),
    ]
    return {
        'id': 'credentials', 'section': 'credentials', 'type': 'list-of-records', 'label': 'Credentials',
        'value': records, 'write': 'records', 'apply': 'live', 'actions': ['add', 'edit', 'remove'],
        'blank': blank, 'records': records,
    }


# Plugins are the configured MCP servers ([mcp_servers.<name>]) plus the
# AetherMind memory row. The page does not invent a second store.
MCP_LIST_HINT = ('Each row is one MCP plugin from config.toml, plus the AetherMind memory row. '
                 'Fill in either a URL or a local command, never both. '
                 'Enable, disable, and remove on an MCP plugin are saved, and the tools on an open session are updated without restarting the core. '
                 'A computer-use provider is the exception: that row says a core restart is still required for those tools. '
                 'A plugin that fails to connect stays saved, the row shows the error, and its tools are not offered. '
                 'A subagent that is already running keeps the tools it started with. '
                 'Check asks the plugin and shows what it answers. A check is remembered until the core restarts. '
                 'An environment value is written into config.toml and passed to a local command as that text. '
                 'Safe mode notes that on the field. Unleashed does not warn and does not cap the length. '
                 'Calling a plugin tool follows the existing Safe or Unleashed switch. '
                 'Unleashed runs an offered tool with no approval and no extra gate. '
                 'Safe uses the existing approval policy. '
                 'Enabled, Session tools, and an excluded tool stay off when you turn them off, in Unleashed too. '
                 'Unleashed only drops approvals, warnings, and default limits. '
                 'A name in safe_tools in config.toml is one Safe does not treat as risky. This page adds no other gate. '
                 'Working directory, timeout, include, exclude, tool prefix, safe tools, and the tool-list cache stay in config.toml. '
                 'When one differs from its default, the row names it. This page does not change them. '
                 'The AetherMind row is read-only. It shows Embedded or External from the core status report. '
                 'When this core does not report which AetherMind is active, the row says so. '
                 'This page has no off switch for AetherMind and stores no token. '
                 'The URL, endpoints, project, and packaged engine path are under Integrations.')
MCP_NAME_HINT = 'Short identifier used as the tool prefix, e.g. wallclock. Letters, digits, dashes.'

# Guidance for the host rows. The floor works, but a person cannot see how or adjust
# it without editing the application. A host row is what the floor reads; these hints
# state the accepted shapes so a person can add one without knowing PromQL or the
# validator.
HOSTS_LIST_HINT = ('Each row is one machine the floor watches. Kind decides how it is read: '
                   '"prometheus" reads it through infrastructure.prometheus_url, "local" probes '
                   'it from this machine, "ssh" runs an argv command over ssh. Restart the core '
                   'to apply.')
HOST_NAME_HINT = 'Stable identifier used in config and thresholds, e.g. builder. Letters, digits, dash.'
HOST_LABEL_HINT = 'Name shown on the board. Empty shows "Host N".'
HOST_KIND_HINT = ('prometheus: metrics from the Prometheus URL. local: a probe_command run here. '
                  'ssh: an ssh_command run against ssh_alias. agent: an agent_url endpoint.')
HOST_ADDRESS_HINT = 'Address for a local probe, e.g. probe.example.com:8765. Empty when the kind does not need one.'
HOST_INSTANCE_HINT = 'Prometheus instance label, e.g. node-exporter:9100. Required for kind=prometheus.'
HOST_PROBE_HINT = 'Command to run for kind=local, one argument per line.'
HOST_SSH_HINT = 'Alias ssh uses, for kind=ssh.'
# Named for what it describes rather than for the field it sits beside: the tunable
# gate treats any module constant whose name carries a policy word as a config default,
# and this is prose shown next to the field, not a value anything reads.
HOST_DEADLINE_HINT = 'Seconds before this host is treated as unreachable.'

HOST_KINDS = ('', 'local', 'ssh', 'agent', 'prometheus')

# The systems registry (tessera/systems.py) is the floor's threshold source. The registry,
# not a prose box, is the shape: define the function, identify the owner, write the
# contract, build a configurable file. The contract exists and the floor reads it; what was
# missing was any way for a person to write one without hand-editing config.toml.
SYSTEMS_LIST_HINT = ('Each row is a system the floor knows about: what it is for, and the signals '
                     'that should wake you. A host row in [hosts] says WHERE a machine is; a row '
                     'here says what it MEANS and when it matters.')
SYSTEM_KIND_HINT = ('host: a machine. remote: reachable over ssh. mcp: a tool server. credential: a '
                    'stored credential. account: a provider login. backend: a model endpoint. '
                    'endpoint: an HTTP address the floor may poll. dashboard: a page, declared only.')
SYSTEM_ROLE_HINT = 'What this system is for, in a sentence. Required: a record with no purpose is not a record.'
SYSTEM_LABEL_HINT = 'Name shown in the world view.'
SYSTEM_ADDRESS_HINT = 'Address to reach it (URL, ssh selector or command text). Required for a watched endpoint.'
SYSTEM_AUTH_HINT = 'Name of a [credentials] entry this system uses, if any.'
SYSTEM_WATCH_HINT = 'Whether the floor should poll this system at all.'
SYSTEM_NOTES_HINT = 'Anything a reader should know. Documentation only; nothing reads it.'
SYSTEM_SIGNAL_KEYS = ('disk_free_percent_below', 'stale_after_seconds', 'wake_on', 'max_wakes_per_hour',
                      'managed')
# The form's fields. The signal keys are accepted as flat inputs and assembled into the
# `signals` table the loader validates, so a person sets a threshold without writing TOML.
SYSTEM_KEYS = {'id', 'name', 'kind', 'label', 'role', 'address', 'auth', 'watch', 'signals', 'notes',
               'disk_free_percent_below', 'stale_after_seconds', 'max_wakes_per_hour', 'managed'}


def _system_fields(system_id, record):
    signals = record.signals or {}
    return [
        dump_field(Spec(f'systems.{system_id}.label', 'systems', 'string', 'Label', 'restart',
                        key='label', hint=SYSTEM_LABEL_HINT), record.label),
        dump_field(Spec(f'systems.{system_id}.kind', 'systems', 'enum', 'Kind', 'restart', key='kind',
                        hint=SYSTEM_KIND_HINT,
                        options=tuple((kind, kind) for kind in SYSTEM_KINDS if kind)), record.kind),
        dump_field(Spec(f'systems.{system_id}.role', 'systems', 'string', 'Purpose', 'restart',
                        key='role', hint=SYSTEM_ROLE_HINT), record.role),
        dump_field(Spec(f'systems.{system_id}.address', 'systems', 'string', 'Address', 'restart',
                        key='address', hint=SYSTEM_ADDRESS_HINT), record.address),
        dump_field(Spec(f'systems.{system_id}.auth', 'systems', 'string', 'Credential', 'restart',
                        key='auth', hint=SYSTEM_AUTH_HINT), record.auth),
        dump_field(Spec(f'systems.{system_id}.watch', 'systems', 'bool', 'Watched', 'restart',
                        key='watch', hint=SYSTEM_WATCH_HINT), record.watch),
        dump_field(Spec(f'systems.{system_id}.notes', 'systems', 'string', 'Notes', 'restart',
                        key='notes', lines=True, hint=SYSTEM_NOTES_HINT), record.notes),
        # Signals are the thresholds the floor actually uses, so they get their own row rather
        # than being buried in prose. Each is shown with its current value, empty when unset.
        dump_field(Spec(f'systems.{system_id}.disk_free_percent_below', 'systems', 'string',
                        'Wake below disk %', 'restart', key='disk_free_percent_below',
                        hint='Wake when free disk falls below this percentage. Empty means the floor default.'),
                   '' if signals.get('disk_free_percent_below') is None else str(signals['disk_free_percent_below'])),
        dump_field(Spec(f'systems.{system_id}.stale_after_seconds', 'systems', 'string',
                        'Stale after (seconds)', 'restart', key='stale_after_seconds',
                        hint='How long a reading stays current. Empty means the floor default.'),
                   '' if signals.get('stale_after_seconds') is None else str(signals['stale_after_seconds'])),
        dump_field(Spec(f'systems.{system_id}.managed', 'systems', 'bool', 'Ours to repair', 'restart',
                        key='managed',
                        hint='Whether this system is ours to repair.'),
                   bool(signals.get('managed', False))),
        dump_field(Spec(f'systems.{system_id}.max_wakes_per_hour', 'systems', 'string',
                        'Max wakes per hour', 'restart', key='max_wakes_per_hour',
                        hint='Cap on wakes this system may cause. Empty means the floor default.'),
                   '' if signals.get('max_wakes_per_hour') is None else str(signals['max_wakes_per_hour'])),
    ]


def systems_field(config):
    """The purpose-bearing registry, as records a person can add and edit.

    The shape is explicit: a registry with a function, an owner, a contract and a
    configurable file, not a prose box. The contract and the file exist (tessera/systems.py,
    [systems.*]); the floor reads the thresholds. This publishes them so a person can change a
    threshold or declare a purpose without editing the application.
    """
    records = []
    for system_id, record in (config.systems or {}).items():
        records.append({
            'name': system_id, 'title': record.label or system_id,
            'status': record.kind or 'unset', 'actions': ['edit', 'remove'],
            'fields': _system_fields(system_id, record),
        })
    blank = [
        dump_field(Spec('systems.new.id', 'systems', 'string', 'Id', 'restart', key='id',
                        hint='Stable identifier, e.g. builder.'), ''),
        dump_field(Spec('systems.new.label', 'systems', 'string', 'Label', 'restart', key='label',
                        hint=SYSTEM_LABEL_HINT), ''),
        dump_field(Spec('systems.new.kind', 'systems', 'enum', 'Kind', 'restart', key='kind',
                        hint=SYSTEM_KIND_HINT,
                        options=tuple((kind, kind) for kind in SYSTEM_KINDS if kind)), ''),
        dump_field(Spec('systems.new.role', 'systems', 'string', 'Purpose', 'restart', key='role',
                        hint=SYSTEM_ROLE_HINT), ''),
        dump_field(Spec('systems.new.address', 'systems', 'string', 'Address', 'restart', key='address',
                        hint=SYSTEM_ADDRESS_HINT), ''),
        dump_field(Spec('systems.new.auth', 'systems', 'string', 'Credential', 'restart', key='auth',
                        hint=SYSTEM_AUTH_HINT), ''),
        dump_field(Spec('systems.new.watch', 'systems', 'bool', 'Watched', 'restart', key='watch',
                        hint=SYSTEM_WATCH_HINT), False),
        dump_field(Spec('systems.new.notes', 'systems', 'string', 'Notes', 'restart', key='notes',
                        lines=True, hint=SYSTEM_NOTES_HINT), ''),
    ]
    return {
        'id': 'systems', 'section': 'systems', 'type': 'list-of-records', 'label': 'Systems',
        'value': records, 'write': 'records', 'apply': 'restart', 'actions': ['add', 'edit', 'remove'],
        'blank': blank, 'records': records, 'hint': SYSTEMS_LIST_HINT,
    }


HOST_KEYS = {'name', 'enabled', 'kind', 'label', 'address', 'prometheus_instance', 'ssh_alias',
             'timeout', 'probe_command'}


def _host_fields(name, host):
    return [
        dump_field(Spec(f'hosts.{name}.enabled', 'hosts', 'bool', 'Watched', 'restart',
                        inline=True, key='enabled'), host.enabled),
        dump_field(Spec(f'hosts.{name}.label', 'hosts', 'string', 'Label', 'restart', key='label',
                        hint=HOST_LABEL_HINT), host.label),
        dump_field(Spec(f'hosts.{name}.kind', 'hosts', 'enum', 'Kind', 'restart', key='kind',
                        hint=HOST_KIND_HINT,
                        options=tuple((kind, kind or '(none)') for kind in HOST_KINDS)), host.kind),
        dump_field(Spec(f'hosts.{name}.address', 'hosts', 'string', 'Address', 'restart', key='address',
                        hint=HOST_ADDRESS_HINT), host.address),
        dump_field(Spec(f'hosts.{name}.prometheus_instance', 'hosts', 'string', 'Prometheus instance',
                        'restart', key='prometheus_instance', hint=HOST_INSTANCE_HINT),
                   host.prometheus_instance),
        dump_field(Spec(f'hosts.{name}.ssh_alias', 'hosts', 'string', 'SSH alias', 'restart',
                        key='ssh_alias', hint=HOST_SSH_HINT), host.ssh_alias),
        dump_field(Spec(f'hosts.{name}.probe_command', 'hosts', 'string', 'Probe command', 'restart',
                        key='probe_command', lines=True, hint=HOST_PROBE_HINT),
                   '\n'.join(host.probe_command or [])),
        dump_field(Spec(f'hosts.{name}.timeout', 'hosts', 'number', 'Timeout (seconds)', 'restart',
                        key='timeout', hint=HOST_DEADLINE_HINT), host.timeout),
    ]


def hosts_field(config):
    """The machines the floor watches, as records a person can add and edit.

    A host name is deliberately not shown on the status payload (a name is a private
    selector), so this list is the only place a person sees the mapping between a name
    and the row the board displays.
    """
    records = []
    for name, host in (config.hosts or {}).items():
        records.append({
            'name': name, 'title': host.label or f'Host {name}', 'status': host.kind or 'unset',
            'actions': ['edit', 'remove'], 'fields': _host_fields(name, host),
        })
    blank = [
        dump_field(Spec('hosts.new.name', 'hosts', 'string', 'Name', 'restart', key='name',
                        hint=HOST_NAME_HINT), ''),
        dump_field(Spec('hosts.new.enabled', 'hosts', 'bool', 'Watched', 'restart', key='enabled'), True),
        dump_field(Spec('hosts.new.label', 'hosts', 'string', 'Label', 'restart', key='label',
                        hint=HOST_LABEL_HINT), ''),
        dump_field(Spec('hosts.new.kind', 'hosts', 'enum', 'Kind', 'restart', key='kind',
                        hint=HOST_KIND_HINT,
                        options=tuple((kind, kind or '(none)') for kind in HOST_KINDS)), ''),
        dump_field(Spec('hosts.new.address', 'hosts', 'string', 'Address', 'restart', key='address',
                        hint=HOST_ADDRESS_HINT), ''),
        dump_field(Spec('hosts.new.prometheus_instance', 'hosts', 'string', 'Prometheus instance',
                        'restart', key='prometheus_instance', hint=HOST_INSTANCE_HINT), ''),
        dump_field(Spec('hosts.new.ssh_alias', 'hosts', 'string', 'SSH alias', 'restart',
                        key='ssh_alias', hint=HOST_SSH_HINT), ''),
        dump_field(Spec('hosts.new.probe_command', 'hosts', 'string', 'Probe command', 'restart',
                        key='probe_command', lines=True, hint=HOST_PROBE_HINT), ''),
        dump_field(Spec('hosts.new.timeout', 'hosts', 'number', 'Timeout (seconds)', 'restart',
                        key='timeout', hint=HOST_DEADLINE_HINT), 3.0),
    ]
    return {
        'id': 'hosts', 'section': 'hosts', 'type': 'list-of-records', 'label': 'Hosts',
        'value': records, 'write': 'records', 'apply': 'restart', 'actions': ['add', 'edit', 'remove'],
        'blank': blank, 'records': records, 'hint': HOSTS_LIST_HINT,
    }

MCP_URL_HINT = 'HTTP(S) endpoint of a server that is already running. Leave empty when using Command.'
MCP_COMMAND_HINT = ('Program to launch, as an absolute path to an executable or a command on PATH. '
                    'Leave empty when using URL.')
MCP_ARGS_HINT = 'One argument per line, passed to Command in order.'


def _unleashed(manager):
    """True when the operator switch is Unleashed.

    Settings names that switch ``permissions.mode``: ``safe`` or ``yolo``.
    ``approval_status()['yolo']`` is set when ``tools.approval`` is ``yolo`` or
    ``never`` (both auto-approve). Safe mode is ``ask`` or ``deny_risky``.
    """
    if manager is None:
        return False
    return bool(manager.approval_status().get('yolo'))


def _env_hint(literal):
    """How an environment line is stored.

    Both modes pass the value through to a local plugin process and write it in
    config.toml. They do not look it up. Safe mode warns that the file holds the
    value. Unleashed states the mechanism and does not warn.
    """
    text = ('KEY=value, one per line, for Command (stdio) servers only because a URL server is remote '
            'and receives no environment. Put the actual value on the right: it is passed as-is to the '
            'server process and stored in the config file. It is not looked up')
    if literal:
        return text + '.'
    return text + '; do not put secrets here.'
MCP_TOKEN_HINT = ('HTTP plugins only. The name of an environment variable that already holds a bearer token. '
                  'The token is read when the plugin is called and is never saved here. Leave empty for no token. A local command ignores this.')
MCP_ENABLED_HINT = 'Off keeps this plugin disconnected, including in Unleashed.'
MCP_SESSION_HINT = 'Off keeps this plugin\'s tools out of sessions, including in Unleashed. Check can still reach it.'


def _memory_plugin_note(name, endpoint, enabled):
    """What a plugin row says when AetherMind memory uses it.

    Memory keeps using that server. AetherMind has no off switch.
    """
    if enabled:
        switch = 'Enabled is locked on this row. '
    else:
        switch = 'Enabled is off in config.toml. Memory keeps using this plugin. '
    return (
        f'{name} serves AetherMind memory because aethermind.endpoints includes {endpoint}. '
        'AetherMind has no off switch. '
        + switch
        + 'Check and edit leave the live memory process running. '
        f'Change {endpoint} in aethermind.endpoints before this plugin can be disabled.'
    )


def _plugin_cache(manager):
    if manager is None:
        return {}, threading.Lock()
    cache = getattr(manager, '_plugin_status', None)
    if cache is None:
        cache = {}
        manager._plugin_status = cache
    lock = getattr(manager, '_plugin_status_lock', None)
    if lock is None:
        lock = threading.Lock()
        manager._plugin_status_lock = lock
    return cache, lock


def _public_connection(status):
    if not status:
        return {'ok': None, 'state': 'unknown', 'detail': 'Not checked yet.', 'last_error': ''}
    return {
        'ok': status.get('ok'),
        'state': status.get('state') or 'unknown',
        'detail': status.get('detail') or '',
        'last_error': status.get('last_error') or '',
    }


def _mcp_fields(name, server, env_hint, endpoint=''):
    command = list(server.command or [])
    program = command[0] if command else ''
    args = '\n'.join(command[1:])
    env = '\n'.join(f'{key}={value}' for key, value in (server.env or {}).items())
    enabled_hint = _memory_plugin_note(name, endpoint, server.enabled) if endpoint else MCP_ENABLED_HINT
    return [
        dump_field(Spec(f'mcp_servers.{name}.enabled', 'plugins', 'bool', 'Enabled', 'live',
                        inline=True, key='enabled', hint=enabled_hint,
                        inert=bool(endpoint) and server.enabled), server.enabled),
        dump_field(Spec(f'mcp_servers.{name}.url', 'plugins', 'string', 'URL', 'live', key='url',
                        hint=MCP_URL_HINT), server.url),
        dump_field(Spec(f'mcp_servers.{name}.command', 'plugins', 'string', 'Command', 'live', key='command',
                        hint=MCP_COMMAND_HINT), program),
        dump_field(Spec(f'mcp_servers.{name}.args', 'plugins', 'string', 'Arguments', 'live',
                        lines=True, hint=MCP_ARGS_HINT, key='args'), args),
        dump_field(Spec(f'mcp_servers.{name}.env', 'plugins', 'string', 'Environment', 'live',
                        lines=True, hint=env_hint, key='env'), env),
        dump_field(Spec(f'mcp_servers.{name}.token_env', 'plugins', 'string', 'Token env', 'live', key='token_env',
                        hint=MCP_TOKEN_HINT), server.token_env or ''),
        dump_field(Spec(f'mcp_servers.{name}.session_tools', 'plugins', 'bool', 'Session tools', 'live',
                        key='session_tools', hint=MCP_SESSION_HINT), server.session_tools),
    ]


def _plugin_summary(name, server, endpoint=''):
    if endpoint:
        state = f'Serves AetherMind memory ({endpoint})'
        if not server.enabled:
            state += '. Enabled is off in config.toml. Memory keeps using this plugin'
    else:
        state = 'Enabled' if server.enabled else 'Disabled'
    tools = 'Session tools on' if server.session_tools else 'Session tools off'
    token = (f'Token env {server.token_env} (the value is not stored here).'
             if server.token_env else 'No token env.')
    return f'{state}. {tools}. {token} From config.toml [mcp_servers.{name}].'


def _plain_number(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _config_only_note(server):
    """Name plugin settings this page shows but does not edit."""
    from .config import McpServerConfig
    parts = []
    if server.description:
        parts.append(server.description[:120])
    if server.cwd:
        parts.append(f'working directory {server.cwd}')
    if server.timeout != McpServerConfig.timeout:
        parts.append(f'timeout {_plain_number(server.timeout)}s')
    if server.list_ttl != McpServerConfig.list_ttl:
        parts.append(f'tool list cached for {_plain_number(server.list_ttl)}s')
    if server.include:
        parts.append('include ' + ', '.join(str(item) for item in server.include))
    if server.exclude:
        parts.append('exclude ' + ', '.join(str(item) for item in server.exclude))
    if server.safe_tools:
        parts.append('safe tools ' + ', '.join(str(item) for item in server.safe_tools))
    if server.tool_prefix:
        parts.append(f'tool prefix {server.tool_prefix}')
    if not parts:
        return ''
    return 'Also in config.toml, not edited on this page: ' + '; '.join(parts) + '.'


def _computer_use_note(config, name):
    provider = getattr(getattr(config, 'computer_use', None), 'provider', '') or ''
    if provider != f'mcp:{name}':
        return ''
    from .mcp_servers import computer_backend
    if computer_backend(config) != 'mcp':
        return ''
    return ('This server is the computer-use provider. Those tools are registered when a session starts. '
            'A new session uses the saved server. An open session keeps the computer-use tools it started with '
            'until the core restarts.')


_AETHERMIND_NOTE = (
    'This row shows Embedded or External from the core status report '
    '(aethermind.active and aethermind.reason). '
    'This row has no off switch, does not choose the engine, and stores no token. '
    'The service URL, endpoints, project, and packaged engine path are under Integrations.'
)
# status() failures we know how to name. Anything else stays an exception.
_STATUS_CALL_ERRORS = (OSError, ValueError, LookupError, TypeError, AttributeError, RuntimeError)


def _labeled_failure(error):
    text = str(error).replace('\n', ' ').strip()
    label = type(error).__name__
    message = f'{label}: {text}' if text and text != label else label
    return message[:300]


def _published_memory(manager):
    """Read the memory report. Do not choose an engine.

    Returns ``(report, error)``. ``error`` is set when ``status()`` fails with
    an expected error. A core whose status has no ``aethermind`` report returns
    an empty report and no error. This function reads that report and does not
    publish ``aethermind.active``.
    """
    status = getattr(manager, 'status', None)
    if not callable(status):
        return {}, ''
    try:
        body = status()
    except _STATUS_CALL_ERRORS as error:
        return {}, _labeled_failure(error)
    if not isinstance(body, dict) or not isinstance(body.get('aethermind'), dict):
        return {}, ''
    return body['aethermind'], ''


def _aethermind_record(config, manager):
    """Show Embedded or External from the core status report. Do not select an engine."""
    if 'aethermind' in config.mcp_servers:
        return None
    memory = config.aethermind
    report, failure = _published_memory(manager)
    if failure:
        if memory.endpoints:
            source = 'Ordered endpoints'
        elif memory.url:
            source = memory.url
        else:
            source = 'None saved'
        return {
            'name': 'aethermind',
            'title': 'AetherMind',
            'status': 'Error',
            'template': True,
            'summary': failure,
            'source_label': 'Saved address',
            'source': source,
            'connection': {'ok': False, 'state': 'error', 'detail': failure, 'last_error': failure},
            'note': _AETHERMIND_NOTE,
            'actions': [],
            'fields': [],
        }
    active = report.get('active')
    reason = report.get('reason') if isinstance(report.get('reason'), str) else ''
    error = report.get('external_error')
    if not isinstance(error, str) or not error.strip():
        error = report.get('last_error') if isinstance(report.get('last_error'), str) else ''
    if active == 'external':
        status = 'External'
        source = memory.url if memory.url and not memory.endpoints else (
            'Ordered endpoints' if memory.endpoints else 'External AetherMind')
        summary = reason or 'The core status report names an external AetherMind.'
        connection = {'ok': None, 'state': 'external', 'detail': '', 'last_error': ''}
    elif active == 'embedded':
        status = 'Embedded'
        source = 'Embedded AetherMind'
        summary = reason or 'The core status report names the embedded AetherMind.'
        if error.strip():
            connection = {'ok': False, 'state': 'error', 'detail': error, 'last_error': error}
        else:
            connection = {'ok': None, 'state': 'embedded', 'detail': '', 'last_error': ''}
    else:
        status = 'Unreported'
        if memory.endpoints:
            source = 'Ordered endpoints'
        elif memory.url:
            source = memory.url
        else:
            source = 'None saved'
        summary = 'This core does not report which AetherMind is active yet.'
        connection = {'ok': None, 'state': 'unreported', 'detail': '', 'last_error': ''}
    return {
        'name': 'aethermind',
        'title': 'AetherMind',
        'status': status,
        'template': True,
        'summary': summary,
        'source_label': 'Saved address' if active not in ('embedded', 'external') else 'Active',
        'source': source,
        'connection': connection,
        'note': _AETHERMIND_NOTE,
        'actions': [],
        'fields': [],
    }


def mcp_field(config, manager=None):
    from .mcp_servers import memory_endpoint
    cache, lock = _plugin_cache(manager)
    env_hint = _env_hint(_unleashed(manager))
    records = []
    for name, server in config.mcp_servers.items():
        endpoint = memory_endpoint(config, name)
        with lock:
            cached = dict(cache.get(name) or {})
        if endpoint and (not cached or cached.get('state') == 'disabled'):
            connection = {
                'ok': None, 'state': 'memory',
                'detail': f'Serves AetherMind memory ({endpoint}). The live memory process stays running.',
                'last_error': '',
            }
        elif not server.enabled and not cached:
            connection = {'ok': None, 'state': 'disabled', 'detail': 'Disabled. Not connected.', 'last_error': ''}
        elif not cached:
            connection = {'ok': None, 'state': 'unknown', 'detail': 'Not checked yet.', 'last_error': ''}
        else:
            connection = _public_connection(cached)
        label, source = ('URL', server.url) if server.url else ('Command', ' '.join(server.command))
        if connection['state'] == 'error':
            badge = 'Error'
        elif endpoint:
            badge = 'Memory'
        elif not server.enabled:
            badge = 'Disabled'
        elif server.url:
            badge = 'HTTP'
        else:
            badge = 'Command'
        parts = [_computer_use_note(config, name), _config_only_note(server)]
        if endpoint:
            parts.append(_memory_plugin_note(name, endpoint, server.enabled))
        note = ' '.join(part for part in parts if part)
        if name == 'aethermind':
            extra = 'AetherMind memory is a separate config.toml [aethermind] entry and is not this row.'
            note = f'{note} {extra}'.strip()
        records.append({
            'name': name,
            'title': name,
            'status': badge,
            'summary': _plugin_summary(name, server, endpoint),
            'source_label': label,
            'source': source,
            'connection': connection,
            'note': note,
            'actions': ['edit', 'check', 'remove'],
            'fields': _mcp_fields(name, server, env_hint, endpoint),
        })
    template = _aethermind_record(config, manager)
    if template is not None:
        records.append(template)
    blank = [
        dump_field(Spec('mcp_servers.new.name', 'plugins', 'string', 'Name', 'live', key='name',
                        hint=MCP_NAME_HINT), ''),
        dump_field(Spec('mcp_servers.new.enabled', 'plugins', 'bool', 'Enabled', 'live', key='enabled',
                        hint=MCP_ENABLED_HINT), True),
        dump_field(Spec('mcp_servers.new.url', 'plugins', 'string', 'URL', 'live', key='url',
                        hint=MCP_URL_HINT), ''),
        dump_field(Spec('mcp_servers.new.command', 'plugins', 'string', 'Command', 'live', key='command',
                        hint=MCP_COMMAND_HINT), ''),
        dump_field(Spec('mcp_servers.new.args', 'plugins', 'string', 'Arguments', 'live', lines=True,
                        hint=MCP_ARGS_HINT, key='args'), ''),
        dump_field(Spec('mcp_servers.new.env', 'plugins', 'string', 'Environment', 'live', lines=True,
                        hint=env_hint, key='env'), ''),
        dump_field(Spec('mcp_servers.new.token_env', 'plugins', 'string', 'Token env', 'live', key='token_env',
                        hint=MCP_TOKEN_HINT), ''),
        dump_field(Spec('mcp_servers.new.session_tools', 'plugins', 'bool', 'Session tools', 'live',
                        key='session_tools', hint=MCP_SESSION_HINT), True),
    ]
    return {
        'id': 'mcp_servers', 'section': 'plugins', 'type': 'list-of-records', 'label': 'Installed plugins',
        'value': records, 'write': 'records', 'apply': 'live', 'actions': ['add', 'edit', 'remove', 'check'],
        'blank': blank, 'records': records, 'hint': MCP_LIST_HINT,
    }


def _loopback_host(url):
    host = (urlsplit(url or '').hostname or '').strip('[]').lower()
    return host in ('localhost', '127.0.0.1', '::1')


def _provider_connected(config):
    signed = any(grant_report(config.home, spec['oauth'])['state'] == 'current' for spec in OAUTH_MODELS.values())
    return signed or bool(config.backends)


def _setup_steps(config):
    picked = bool((config.roles.get('main') or '').strip())
    return [
        {'n': 1, 'text': 'Connect a provider (required).', 'done': _provider_connected(config)},
        {'n': 2, 'text': 'Pick a chat model (required).', 'done': picked},
    ]


def _setup_line(config):
    return '1. Connect a provider (required). 2. Pick a chat model (required). Everything else is optional.'


def _endpoint_rows(config, groups, *, kind):
    rows = []
    for group in groups.values():
        names = [item['name'] for item in group['models']]
        source = credential_source(config, names[0]) if names else {'source': 'none', 'label': 'No credential'}
        protocol = group.get('protocol') or ''
        if protocol == 'openai_chat':
            protocol_label = 'OpenAI-compatible chat'
        elif protocol == 'openai_responses':
            protocol_label = 'OpenAI Responses'
        else:
            protocol_label = protocol
        count = len(group['models'])
        if len(names) == 1:
            group['name'] = names[0]
        url = group.get('base_url') or ''
        host = urlsplit(url).netloc or ''
        where = (host + ' · ') if host and host != group.get('name') else ''
        place = 'Local · ' if kind == 'local' else ''
        group['line'] = where + place + source['label'] + ' · ' + protocol_label + ' · ' + str(count) + (' model' if count == 1 else ' models')
        pill, reason = _probe_models(url)
        group['pill'] = pill
        if reason and pill != 'Connected':
            group['line'] += ' · ' + reason
        group['source'] = source['label']
        group['kind'] = kind
        group['removable'] = False
        group['actions'] = ['add-model']
        rows.append(group)
    return rows


def provider_pages(config, keychain=None):
    """Providers, split the way a person connects them. Models stay on Models."""
    signin = []
    for key, spec in OAUTH_MODELS.items():
        report = grant_report(config.home, spec['oauth'])
        connected = report['state'] == 'current'
        who = 'xAI Grok sign-in' if key == 'xai' else 'ChatGPT or Codex sign-in'
        status = _grant_status(report)
        shown = '' if status == 'Not signed in' else status
        signin.append({
            'id': key,
            'name': 'xAI Grok' if key == 'xai' else 'ChatGPT or Codex',
            'pill': 'Connected' if connected else 'Not connected',
            'line': who if not shown else who + '. ' + shown,
            'source': who if not shown else who + ' · ' + shown,
            'actions': _oauth_actions(report),
            'removable': False,
            'kind': 'signin',
            'preset': {'account': key},
            'status': shown,
        })
    keys = []
    for name, reference in config.credentials.items():
        if (reference.oauth or '').strip():
            continue
        meta = _credential_meta(config, name, reference, keychain)
        keys.append({
            'id': name, 'name': name, 'pill': meta['pill'], 'line': meta['line'],
            'stored': meta['stored'], 'added': meta['added'],
            'source_kind': meta['source_kind'],
            'removable': True, 'actions': ['edit', 'remove'], 'kind': 'key',
        })
    local_groups = {}
    custom_groups = {}
    cli_groups = {}
    for name, backend in config.backends.items():
        kind = _kind(config, backend)
        cli = (backend.extra or {}).get('cli') or {}
        source = credential_source(config, name)
        model_row = {
            'name': name, 'title': backend.model or name, 'source': source['label'],
            'removable': True, 'enabled': bool(backend.enabled),
        }
        if kind == 'cli_worker':
            vendor = cli.get('vendor') if isinstance(cli.get('vendor'), str) and cli.get('vendor') else 'CLI'
            bucket = cli_groups.setdefault(vendor, {
                'id': 'cli-' + vendor, 'name': vendor + ' CLI', 'models': [], 'kind': 'cli', 'actions': [],
            })
            bucket['models'].append(model_row)
            continue
        reference = backend.credential_ref
        pool = config.accounts.get(backend.account) if backend.account else None
        if pool is not None and pool.credential_ref:
            reference = pool.credential_ref
        cred = config.credentials.get(reference) if reference else None
        if cred is not None and (cred.oauth or '') in OAUTH_MODELS:
            continue
        url = backend.base_url or ''
        loopback = _loopback_host(url)
        target = local_groups if loopback else custom_groups
        group = target.setdefault(name, {
            'id': name,
            'name': name,
            'base_url': url,
            'protocol': kind,
            'models': [],
            'preset': {
                'account': 'other', 'base_url': url, 'kind': kind,
                'credential': reference or '', 'api_key_env': backend.api_key_env or '',
            },
        })
        group['models'].append(model_row)
    local_rows = _endpoint_rows(config, local_groups, kind='local')
    custom = _endpoint_rows(config, custom_groups, kind='custom')
    for group in cli_groups.values():
        enabled = any(item['enabled'] for item in group['models'])
        group['pill'] = 'Saved' if enabled else 'Not connected'
        group['line'] = group['models'][0]['source'] if group['models'] else 'CLI login'
        group['source'] = group['line']
        group['removable'] = False
        group['actions'] = []
        custom.append(group)
    return {
        'accounts': {
            'crumb': ['Settings', 'Providers', 'Accounts'],
            'title': 'Connect an account',
            'help': 'Sign in with the provider account you already use.',
            'api_key_link': 'Have an API key instead?',
            'local_row': {
                'name': 'Run models locally',
                'line': 'A model server on this machine.',
            },
            'connected': [row for row in signin if row['pill'] == 'Connected'],
            'available': [row for row in signin if row['pill'] != 'Connected'],
        },
        'keys': {
            'crumb': ['Settings', 'Providers', 'API keys'],
            'title': 'API keys',
            'help': 'A key is an environment variable name or a keychain item. The value is never shown.',
            'records': keys,
        },
        'endpoints': {
            'crumb': ['Settings', 'Providers', 'Endpoints'],
            'title': 'Endpoints',
            'help': 'A local server and any other address are both endpoints. A local one is on this machine.',
            'records': local_rows + custom,
        },
        'local': {
            'crumb': ['Settings', 'Providers', 'Endpoints'],
            'title': 'Endpoints',
            'help': 'A local server and any other address are both endpoints. A local one is on this machine.',
            'records': local_rows,
        },
    }


def project(config, approval, manager=None, keychain=None):
    keychain = _projection_keychain(manager, keychain)
    relationships = ladder(config)
    grouped = {section: [] for section, _label in SECTIONS}
    for spec, value in catalog(config, approval):
        grouped[spec.section].append(dump_field(spec, value))
    people = identities_field(config, manager)
    if people is not None:
        grouped['identity'].append(people)
    grouped['providers'].append(oauth_accounts_field(config))
    grouped['providers'].append(providers_field(config))
    grouped['models'].append(worker_policy_field(config))
    grouped['integrations'].append(accounts_field(config))
    grouped['accounts'].append({
        'id': 'service_accounts', 'section': 'accounts', 'type': 'list-of-records',
        'label': 'Accounts', 'value': [],
        'records': [],
        'write': 'records', 'apply': 'live', 'actions': [], 'blank': [],
        'hint': 'Accounts are for services that are not model hosts. None are listed yet. Sign-ins are on Providers. Keys are in Credentials.',
    })
    grouped['credentials'].append(credentials_field(config, keychain))
    grouped['hosts'].append(hosts_field(config))
    grouped['systems'].append(systems_field(config))
    grouped['plugins'].append(mcp_field(config, manager))
    sections = []
    for section, label in SECTIONS:
        fields = grouped[section]
        # Record collections bypass dump_field; publish their default tier too.
        for field in fields:
            field.setdefault('tier', 'required')
        # Rank by tier, keeping declaration order inside each rank. The fields which
        # make a thing work come first and the customizations sit below them; the order
        # WITHIN a tier is still the author's choice, so the sort must be stable rather
        # than alphabetical.
        fields = sorted(fields, key=lambda field: _TIER_RANK.get(field.get('tier', 'required'), 1))
        if fields:
            sections.append({'id': section, 'label': label, 'start_hint': SECTION_START[section], 'fields': fields})
    _annotate_memory_status(sections, manager)
    return {'sections': sections, 'file_only': file_only(config, approval), 'ladder': relationships,
            'setup': _setup_line(config), 'setup_steps': _setup_steps(config),
            'provider_pages': provider_pages(config, keychain)}


def _write_memory_status(sections, line):
    for section in sections:
        if section.get('id') != 'integrations':
            continue
        for field in section['fields']:
            field_id = field.get('id')
            existing = field.get('status') or ''
            if field_id == 'aethermind.source':
                # The live line stays first so a lock sentence does not hide it.
                field['status'] = (line + ' · ' + existing) if existing and line else (existing or line)
            elif field_id == 'aethermind.url':
                field['status'] = (existing + ' · ' + line) if existing else line


def _annotate_memory_status(sections, manager):
    """The AetherMind rows report the live connection. A status failure is shown."""
    if manager is None or not hasattr(manager, 'status'):
        return
    try:
        report = manager.status().get('aethermind') or {}
    except Exception as error:
        message = type(error).__name__
        detail = str(error).replace('\n', ' ').strip()
        if detail and detail != message:
            message += ': ' + detail[:200]
        _write_memory_status(sections, 'AetherMind status failed: ' + message)
        return
    line = _memory_truth(report)
    if not line:
        return
    _write_memory_status(sections, line)


def _status_error_text(value):
    """The error a person should see. A class name and a full message both stay."""
    if not isinstance(value, str):
        return ''
    return value.replace('\n', ' ').strip()


def _memory_truth(report):
    if not isinstance(report, dict):
        return ''
    active = report.get('active')
    reason = report.get('reason') if isinstance(report.get('reason'), str) else ''
    if active == 'external':
        line = 'External'
        if report.get('reachable') is True:
            line += ' · Reachable'
        elif report.get('reachable') is False:
            line += ' · Unreachable'
        elif reason:
            line += ' · ' + reason
        stop = report.get('embedded_stop')
        if isinstance(stop, str) and stop.strip() and stop.strip() not in line:
            line += ' · ' + stop.strip()
    elif active == 'embedded':
        line = 'Embedded'
        if reason:
            line += ' · ' + reason
        process = report.get('process')
        if isinstance(process, str) and process and process not in line:
            line += ' · ' + process
    elif reason:
        line = reason
    else:
        return ''
    last = _status_error_text(report.get('external_error')) or _status_error_text(report.get('last_error'))
    if last and active != 'external':
        line += ' · Last error: ' + last
        category = report.get('error_category')
        if isinstance(category, str) and category.strip():
            line += ' · ' + category.strip()
        target = report.get('external_target')
        if isinstance(target, str) and target.strip():
            line += ' · ' + target.strip()
    return line


def reachable_attributes(config, approval):
    """Every top-level Config attribute the page can change, measured not asserted.

    Two ways an attribute is reachable: a catalog Spec stores a path rooted at it, or a
    record editor writes its whole table. Both are read from the live catalog, so this
    answers for the page as it is now rather than for a list someone maintained by hand.
    """
    roots = {spec.path[0] for spec, _value in catalog(config, approval) if spec.path}
    return roots | set(EDITOR_TABLES.values())


def _covered_keys(config, approval):
    """(table, key) pairs a catalog spec edits. A longer path still covers that key."""
    covered = set()
    for spec, _value in catalog(config, approval):
        if len(spec.path) >= 2:
            covered.add((spec.path[0], spec.path[1]))
    return covered


def file_only(config, approval):
    """The attributes with no control on the page, each with the reason it has none.

    Raises when an attribute has neither a control nor a stated reason. That refusal is
    the point: a table added to Config and forgotten here would otherwise be a silent
    gap, and the person reading the page could not tell a decision from an oversight.

    A behavior table the page reaches only in part still lists the keys it does not
    edit, under ``uncovered``. Those keys are visible. They are not new controls.
    """
    reached = reachable_attributes(config, approval)
    covered = _covered_keys(config, approval)
    rows = []
    unstated = []
    for item in dataclass_fields(Config):
        if item.name in reached:
            continue
        reason = FILE_ONLY_REASONS.get(item.name, '')
        if not reason:
            unstated.append(item.name)
        row = {'name': item.name, 'reason': reason}
        if item.name in BEHAVIOR_TABLES:
            row['values'] = behavior_values(getattr(config, item.name))
        rows.append(row)
    uncovered = []
    for name in BEHAVIOR_TABLES:
        if name not in reached:
            continue
        values = [item for item in behavior_values(getattr(config, name))
                  if (name, item['key']) not in covered]
        if values:
            uncovered.append({'name': name, 'reason': FILE_ONLY_REASONS.get(name, ''), 'values': values})
    if unstated:
        raise ValueError('No Settings control and no stated reason for: ' + ', '.join(sorted(unstated)))
    return {'count': len(rows), 'reachable': len(reached), 'rows': rows, 'uncovered': uncovered}


def behavior_values(section):
    """Live values for one file-only behavior table. Secrets are presence, not contents."""
    rows = []
    for item in dataclass_fields(section):
        value = getattr(section, item.name)
        if is_secret_name(item.name):
            rows.append({'key': item.name, 'secret': True, 'set': bool(value)})
        else:
            rows.append({'key': item.name, 'secret': False, 'value': value})
    return rows


def _loaded_sessions(manager):
    found = []
    for runtime in list((getattr(manager, '_runtimes', {}) or {}).values()):
        session = getattr(runtime, 'session', None)
        if session is not None:
            found.append((runtime, session))
    return found


_MANUAL_SWITCH = ' An off switch applies in every mode, including Unleashed.'


def skill_effect(manager):
    """What a skills save actually changes. The sentence is the contract the page shows."""
    loaded = [(runtime, session) for runtime, session in _loaded_sessions(manager)
              if getattr(session, 'shelf', None) is not None]
    if not manager.config.skills.enabled:
        if loaded:
            return ('Saved. Sessions that already loaded the shelf will not offer skills on the next call. '
                    'A session that started with the shelf off has no skill tools until it is started again.'
                    + _MANUAL_SWITCH)
        return ('Saved. The shelf is off. A session that already started without it does not gain skill tools. '
                'A session started with the shelf on will use this.' + _MANUAL_SWITCH)
    if loaded:
        return ('Saved. The next skill call in a session that already loaded the shelf uses this.'
                + _MANUAL_SWITCH)
    if _loaded_sessions(manager):
        return ('Saved. Loaded sessions started with the shelf off and have no skill tools. '
                'A session started from now on will load the shelf.' + _MANUAL_SWITCH)
    return 'Saved. No session is loaded. A session started from now on will use this.' + _MANUAL_SWITCH


def tool_effect(manager):
    if _loaded_sessions(manager):
        return ('Saved. Every loaded session omits a disabled tool from the next turn and refuses a call to it.'
                + _MANUAL_SWITCH)
    return ('Saved. No session is loaded. A session started from now on will omit a disabled tool and refuse a call to it.'
            + _MANUAL_SWITCH)


def skills_view(config, cwd):
    """Every skill directory and every SKILL.md, including ones that fail to parse.

    A broken file stays in the list with its error. A disabled skill stays in the list
    with enabled false. Off means the loop is not offering it, in every approval mode
    including Unleashed, which is not the same as an empty shelf.
    """
    from .skills import Shelf, parse_skill_text
    shelf = Shelf(config.home, cwd, config.skills)
    disabled = {item for item in (config.skills.disabled or []) if isinstance(item, str)}
    directories = []
    rows = []
    for root, source in shelf.roots():
        error = ''
        found = 0
        if not root.exists():
            error = 'Directory does not exist'
        elif not root.is_dir():
            error = 'Not a directory'
        else:
            try:
                children = sorted(root.iterdir())
            except OSError as exc:
                error = str(exc)
                children = []
            for child in children:
                path = child / 'SKILL.md'
                if not path.is_file():
                    continue
                found += 1
                try:
                    parsed = parse_skill_text(path.read_text(encoding='utf-8'), child.name)
                    enabled = parsed['name'] not in disabled
                    rows.append({'name': parsed['name'], 'description': parsed['description'],
                                 'source': source, 'dir': str(child), 'error': '',
                                 'enabled': enabled,
                                 'offered': bool(config.skills.enabled and enabled)})
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    rows.append({'name': child.name, 'description': '', 'source': source,
                                 'dir': str(child), 'error': str(exc), 'enabled': False, 'offered': False})
        if error:
            status = error
        elif found == 1:
            status = '1 skill file'
        else:
            status = f'{found} skill files'
        directories.append({'path': str(root), 'source': source, 'error': error,
                            'status': status, 'removable': source == 'added'})
    rows.sort(key=lambda row: row['name'])
    yolo = getattr(config.tools, 'approval', '') == 'yolo'
    if config.skills.enabled:
        note = ''
    else:
        note = ('skills.enabled is off. The loop does not offer these skills. '
                'A session that already started without the shelf has no skill tools until it is started again. '
                'An off switch applies in every mode, including Unleashed.')
    for row in rows:
        if row['error']:
            row['offered'] = False
    return {'enabled': bool(config.skills.enabled), 'loaded': bool(config.skills.enabled),
            'yolo': yolo, 'count': len(rows), 'skills': rows, 'directories': directories, 'note': note,
            'workspace': str(cwd)}


def _tool_rows(registry, policy, tools_config=None, builtin_names=()):
    """What happens when each tool is called, in the registry's own terms.

    Three policy outcomes, not two. A risky tool runs freely under yolo/never or when an
    allow-rule names it; under `ask` it prompts; under `deny_risky` it is refused
    outright. A tool the operator turned off is a fourth outcome: the call is refused
    before that policy runs, and the row still names the policy it would follow when on.
    """
    from .tools import AUTO_APPROVE, approval_rule
    classes = getattr(tools_config, 'approval_classes', {}) or {}
    rules = getattr(tools_config, 'approval_rules', {}) or {}
    disabled = {item for item in (getattr(tools_config, 'disabled', ()) or []) if isinstance(item, str)}
    builtin = set(builtin_names)
    rows = []
    for name in registry.names():
        tool = registry._tools[name]
        if not tool.risky:
            policy_outcome = 'runs'
        elif policy in AUTO_APPROVE or approval_rule(name, classes, rules) == 'allow':
            policy_outcome = 'runs'
        elif policy == 'ask':
            policy_outcome = 'asks'
        else:
            policy_outcome = 'refused'
        marked = getattr(tool, 'origin', '') or ''
        if marked == 'mcp':
            origin = 'mcp'
        elif name in builtin:
            origin = 'builtin'
        else:
            origin = 'session'
        enabled = name not in disabled
        # The operator's off switch is enforced before the approval policy, including in Unleashed.
        enforced = not enabled
        rows.append({
            'name': name,
            'description': tool.description,
            'risky': bool(tool.risky),
            'outcome': 'disabled' if enforced else policy_outcome,
            'policy_outcome': policy_outcome,
            'asks': (not enforced) and policy_outcome == 'asks',
            'enabled': enabled,
            'origin': origin,
        })
    rows.sort(key=lambda row: row['name'])
    return rows


def _base_registry(config, policy):
    from .tools import build_registry
    # Pass the configured values rather than relying on build_registry's own defaults:
    # an unconfigured call makes those defaults newly reachable tunables, which the
    # hardwire check refuses. The loop passes exactly these two from config.tools.
    return build_registry(approval=policy, policy=config.tools,
                          clip_chars=config.tools.clip_chars, timeout=config.tools.timeout)


def _mcp_servers_for_sessions(config):
    """The servers a new depth-0 session would ask, in the loop's order."""
    from .mcp_servers import computer_backend
    servers = {key: value for key, value in config.mcp_servers.items() if value.session_tools}
    provider = getattr(config.computer_use, 'provider', '') or ''
    if computer_backend(config) == 'mcp' and provider.startswith('mcp:'):
        servers.pop(provider.split(':', 1)[1], None)
    return servers


def _ask_mcp(config):
    """Register MCP tools the way a new session does, into a registry used only for this read.

    This contacts configured servers through the same process catalog the loop uses.
    It does not start a session and it does not register desktop, board, or autonomy tools.
    """
    from .mcp_servers import CATALOG, computer_backend, register_computer_use_tools, register_mcp_server_tools
    registry = _base_registry(config, config.tools.approval)
    register_mcp_server_tools(registry, _mcp_servers_for_sessions(config), config.tools.clip_chars, CATALOG)
    if computer_backend(config) == 'mcp':
        register_computer_use_tools(registry, config, config.tools.clip_chars, CATALOG)
    return registry


def _mcp_errors(config):
    from .mcp_servers import CATALOG, computer_backend
    names = [name for name, server in config.mcp_servers.items() if server.enabled and server.session_tools]
    provider = getattr(config.computer_use, 'provider', '') or ''
    if computer_backend(config) == 'mcp' and provider.startswith('mcp:'):
        extra = provider.split(':', 1)[1]
        if extra not in names and extra in config.mcp_servers and config.mcp_servers[extra].enabled:
            names.append(extra)
    errors = []
    for name in names:
        text = CATALOG.last_error(name)
        if text:
            errors.append({'server': name, 'error': text})
    return errors


def _tool_payload(rows, policy, source, callable_now, gap, mcp_errors):
    return {'count': len(rows), 'risky': sum(1 for row in rows if row['risky']),
            'policy': policy, 'yolo': policy == 'yolo', 'source': source, 'callable': callable_now,
            'gap': gap, 'mcp_errors': mcp_errors, 'tools': rows}


def tools_view(config, approval, manager=None):
    """Tools the loop can call, or the set a new session would register when none is loaded.

    A live session registry is the list, because board, autonomy, companion and desktop
    tools register on that session and a second invented list would disagree with it.
    With no session, the base registry plus configured MCP servers is what a new session
    registers before those session-only tools attach. The gap text says which is which.
    """
    policy = approval.get('mode') or config.tools.approval
    builtin = set(_base_registry(config, policy).names())
    loaded = _loaded_sessions(manager) if manager is not None else []
    for runtime, session in loaded:
        registry = getattr(session, 'tools', None)
        if registry is None:
            continue
        others = [other.slug for other, _session in loaded
                  if getattr(other, 'slug', '') and other is not runtime]
        gap = ''
        if others:
            gap = 'Other loaded sessions are not shown: ' + ', '.join(others) + '.'
        return _tool_payload(_tool_rows(registry, policy, config.tools, builtin), policy,
                             f'live session {runtime.slug}', True, gap, _mcp_errors(config))
    registry = _ask_mcp(config)
    gap = ('No session is loaded. This list is the built-in registry plus configured MCP servers. '
           'Desktop, board, autonomy and companion tools register when a session loads and are not listed. '
           'Nothing here is callable until a session is loaded.')
    return _tool_payload(_tool_rows(registry, policy, config.tools, builtin), policy,
                         'base registry (no session loaded)', False, gap, _mcp_errors(config))


def _save_config(manager, edits):
    commit(manager, edits, restart=False)


def _replace_name(names, name, enabled):
    current = [item for item in names if isinstance(item, str)]
    if enabled:
        return [item for item in current if item != name]
    if name in current:
        return current
    return current + [name]


def set_tool_enabled(manager, name, enabled):
    if not isinstance(name, str) or not name or '\x00' in name or len(name) > 128:
        raise ValueError('Tool name is not valid')
    if type(enabled) is not bool:
        raise ValueError('enabled must be true or false')
    view = tools_view(manager.config, manager.approval_status(), manager)
    if name not in {row['name'] for row in view['tools']}:
        raise ValueError(f'Unknown tool: {name}')
    previous = [item for item in manager.config.tools.disabled if isinstance(item, str)]
    updated = _replace_name(previous, name, enabled)
    changed = updated != previous
    if changed:
        _save_config(manager, [{'path': ['tools', 'disabled'], 'value': updated}])
    body = tools_view(manager.config, manager.approval_status(), manager)
    body['effect'] = tool_effect(manager) if changed else 'Already set.'
    return body


def set_skill_enabled(manager, name, enabled, cwd):
    if not isinstance(name, str) or not name or '\x00' in name or len(name) > 128:
        raise ValueError('Skill name is not valid')
    if type(enabled) is not bool:
        raise ValueError('enabled must be true or false')
    view = skills_view(manager.config, cwd)
    row = next((item for item in view['skills'] if item['name'] == name), None)
    if row is None:
        raise ValueError(f'Unknown skill: {name}')
    if row['error']:
        raise ValueError(row['name'] + ' cannot be switched: ' + row['error'])
    previous = [item for item in manager.config.skills.disabled if isinstance(item, str)]
    updated = _replace_name(previous, name, enabled)
    changed = updated != previous
    if changed:
        _save_config(manager, [{'path': ['skills', 'disabled'], 'value': updated}])
    body = skills_view(manager.config, cwd)
    body['effect'] = skill_effect(manager) if changed else 'Already set.'
    return body


def set_shelf_enabled(manager, enabled, cwd):
    if type(enabled) is not bool:
        raise ValueError('enabled must be true or false')
    changed = manager.config.skills.enabled is not enabled
    if changed:
        _save_config(manager, [{'path': ['skills', 'enabled'], 'value': enabled}])
    body = skills_view(manager.config, cwd)
    body['effect'] = skill_effect(manager) if changed else 'Already set.'
    return body


def _directory_key(value):
    from pathlib import Path
    from .config import expand_config_path
    expanded = Path(expand_config_path(str(value))).expanduser()
    try:
        return str(expanded.resolve())
    except OSError:
        return str(expanded)


def _absolute_directory(value):
    from pathlib import Path
    from .config import expand_config_path
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 1024:
        raise ValueError('A skills directory must be an absolute path')
    expanded = Path(expand_config_path(value.strip())).expanduser()
    if not expanded.is_absolute():
        raise ValueError('A skills directory must be an absolute path')
    return expanded


def add_skill_directory(manager, raw, cwd):
    expanded = _absolute_directory(raw)
    key = _directory_key(expanded)
    for directory in skills_view(manager.config, cwd)['directories']:
        if _directory_key(directory['path']) != key:
            continue
        if directory['source'] == 'added':
            raise ValueError('That directory is already added')
        if directory['source'] == 'user':
            raise ValueError('That path is the user shelf. This page adds extra directories and does not replace the shelf.')
        raise ValueError('That directory is already on the shelf.')
    updated = [item for item in manager.config.skills.extra_dirs if isinstance(item, str)] + [str(expanded)]
    _save_config(manager, [{'path': ['skills', 'extra_dirs'], 'value': updated}])
    body = skills_view(manager.config, cwd)
    body['effect'] = skill_effect(manager)
    return body


def remove_skill_directory(manager, raw, cwd):
    expanded = _absolute_directory(raw)
    key = _directory_key(expanded)
    match = next((directory for directory in skills_view(manager.config, cwd)['directories']
                  if _directory_key(directory['path']) == key), None)
    if match is None:
        raise ValueError('That directory is not on the shelf.')
    if not match['removable']:
        if match['source'] == 'user':
            raise ValueError('That path is the user shelf. This page removes only directories that were added here.')
        raise ValueError('That directory comes from project settings. This page removes only directories that were added here.')
    updated = [item for item in manager.config.skills.extra_dirs
               if isinstance(item, str) and _directory_key(item) != key]
    _save_config(manager, [{'path': ['skills', 'extra_dirs'], 'value': updated}])
    body = skills_view(manager.config, cwd)
    body['effect'] = skill_effect(manager)
    return body


def find_spec(config, approval, ident):
    for spec, _value in catalog(config, approval):
        if spec.id == ident:
            return spec
    return None


def coerce(spec, value):
    if spec.type == 'color':
        if not isinstance(value, str) or (value and not re.fullmatch(r'#[0-9a-fA-F]{6}', value)):
            raise ValueError(f'{spec.label} must be a six-digit hex color or empty')
        return value.lower()
    if spec.type == 'bool':
        if not isinstance(value, bool):
            raise ValueError(f'{spec.label} must be true or false')
        return value
    if spec.type == 'enum':
        if not isinstance(value, str) or value not in {item for item, _label in spec.options}:
            raise ValueError(f'{spec.label} is not an allowed value')
        return value
    if spec.type == 'int':
        if type(value) is not int or isinstance(value, bool):
            raise ValueError(f'{spec.label} must be an integer')
        if spec.minimum is not None and value < spec.minimum or spec.maximum is not None and value > spec.maximum:
            raise ValueError(f'{spec.label} is outside its range')
        return value
    if spec.type == 'float':
        if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'{spec.label} must be a finite number')
        if spec.minimum is not None and value < spec.minimum or spec.maximum is not None and value > spec.maximum:
            raise ValueError(f'{spec.label} is outside its range')
        return float(value) if type(value) is not int else value
    if spec.type == 'list':
        return _list_value(spec, value)
    if spec.type == 'string':
        if not isinstance(value, str) or '\x00' in value or len(value) > 4000:
            raise ValueError(f'{spec.label} must be text')
        return value
    raise ValueError(f'{spec.label} cannot be set this way')


def _list_value(spec, value):
    """Coerce one ordered list of strings from the page.

    The page sends a textarea, so a newline-separated string is the normal shape;
    a list is accepted too because the raw config door and tests pass arrays. Each
    entry is validated here rather than left to config.validate(), so a rejected
    save never reaches the file.
    """
    if isinstance(value, str):
        items = value.splitlines()
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(f'{spec.label} must be a list of entries')
    cleaned = []
    for item in items:
        if not isinstance(item, str) or '\x00' in item or len(item) > 500:
            raise ValueError(f'{spec.label} must be text, one entry per line')
        entry = item.strip()
        if not entry:
            continue
        if spec.id == 'aethermind.endpoints':
            if entry.startswith('mcp:'):
                cleaned.append(entry)
                continue
            parsed = urlsplit(entry)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
                raise ValueError('AetherMind endpoints must be HTTP(S) URLs without credentials')
        cleaned.append(entry)
    return cleaned


def _require_name(value):
    if not isinstance(value, str) or not NAME.match(value):
        raise ValueError('Name must be letters, numbers, dash, or underscore')
    return value


def _lines(value):
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.splitlines()
    else:
        raise ValueError('Arguments must be text')
    cleaned = []
    for item in items:
        if not isinstance(item, str) or '\x00' in item or len(item) > 500:
            raise ValueError('Invalid argument')
        if item.strip():
            cleaned.append(item.strip())
    return cleaned


def _env_map(value, *, literal=False):
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, str):
        pairs = []
        for line in value.splitlines():
            if not line.strip():
                continue
            if '=' not in line:
                raise ValueError('Env entries are KEY=value lines')
            key, raw = line.split('=', 1)
            pairs.append((key.strip(), raw.strip()))
    elif value in (None, ''):
        return {}
    else:
        raise ValueError('Env entries are KEY=value lines')
    result = {}
    for key, raw in pairs:
        if not isinstance(key, str) or not ENV_NAME.fullmatch(key):
            raise ValueError('Env keys must be environment variable names')
        if not isinstance(raw, str) or not raw or any(char in raw for char in '\x00\r\n'):
            raise ValueError('Invalid env value')
        # Arguments already cap a line at 500 characters. Safe mode keeps that bound
        # on an environment value. Unleashed stores the value as written, with no length cap.
        if not literal and len(raw) > 500:
            raise ValueError('Invalid env value')
        result[key] = raw
    return result


def _refuse_retired_pool(record):
    if not isinstance(record, dict):
        return
    present = [key for key in RETIRED_ACCOUNT_LIMITS if key in record]
    if present:
        raise ValueError(
            'max_concurrent and initial_concurrency on an account are retired. '
            'They are not caps. Set max_concurrent_requests on the model.'
        )


def _check_keys(record, allowed):
    if not isinstance(record, dict):
        raise ValueError('Missing record')
    unknown = set(record) - allowed
    if unknown:
        raise ValueError('Unknown fields')


def credential_in_use(config, name):
    for backend in config.backends.values():
        if backend.credential_ref == name:
            return True
        cli = (backend.extra or {}).get('cli') or {}
        if cli.get('fallback_credential_ref') == name:
            return True
    for account in config.accounts.values():
        if account.credential_ref == name:
            return True
    return name in {
        config.voice.credential_ref, config.jev.credential_ref,
        config.computer_use.jev.credential_ref, config.grafana.credential_ref,
    }


def adopt_config(manager, config):
    """Swap in a reloaded config. The Safe/Unleashed switch stays where the operator left it."""
    config.tools.approval = manager.config.tools.approval
    manager.config = config
    # The identity registry is a live object owned by the manager, not a field of the
    # config object that happens to carry it. A reloaded config is a fresh Config with
    # nothing attached, so a save that adopted it silently dropped owner resolution:
    # a card row went from owner_known True to None after one settings write, with the
    # manager's registry still alive and enabled underneath.
    # Re-attaching keeps the live registry authoritative; a change to
    # identity.enabled still waits for the restart its spec declares.
    registry = getattr(manager, 'identities', None)
    if registry is not None:
        config.identity_registry = registry
        board = getattr(manager, 'board', None)
        if board is not None:
            board.identities = registry
    integrations = getattr(manager, 'integrations', None)
    if integrations is not None:
        integrations.config = config
    jobs = getattr(manager, 'swarm_jobs', None)
    if jobs is not None:
        jobs.config = config
        jobs.settings = config.swarm
        scheduler = getattr(jobs, '_scheduler', None)
        if scheduler is not None:
            scheduler.pressure_config = config
    voice = getattr(manager, 'voice', None)
    if voice is not None:
        voice.config = config
        voice.settings = config.voice
        client = getattr(voice, 'client', None)
        if client is not None:
            client.settings = config.voice
            client.credentials = CredentialResolver(config.credentials, oauth_home=str(config.home))
    runtimes = getattr(manager, '_runtimes', {})
    metadata = getattr(manager, '_metadata', {})

    def refresh():
        for runtime in list(runtimes.values()):
            session = getattr(runtime, 'session', None)
            if session is None or not hasattr(session, 'config'):
                continue
            roles = dict(config.roles)
            meta = metadata.get(getattr(runtime, 'slug', ''), {})
            chosen = meta.get('backend') if isinstance(meta, dict) else None
            if chosen and isinstance(meta, dict) and meta.get('backend_override', True) and chosen in config.backends:
                roles['main'] = chosen
                roles['fast'] = chosen
            session.config = replace(config, roles=roles)
            # The registry enforces whichever ToolsConfig it holds. The Unleashed switch
            # writes that same object. After a reload, point it at the new one so
            # the switch and the off-list keep using the live approval mode.
            registry = getattr(session, 'tools', None)
            if registry is not None and session.config.tools is not None:
                registry.policy = session.config.tools
            if hasattr(session, '_default_main'):
                session._default_main = config.roles.get('main')
            # Descendants do not get a copied ToolsConfig. Their registries
            # resolve policy_of through this session, whose config was just replaced.

    lock = getattr(manager, '_lock', None)
    if lock is None:
        refresh()
    else:
        with lock:
            refresh()


def _result(manager, reply, restart, detail=''):
    changed = bool(reply.get('changed'))
    body = {
        'changed': changed,
        'restart_required': bool(changed and restart),
        'applied': 'restart' if changed and restart else 'live',
        'settings': project(manager.config, manager.approval_status(), manager),
    }
    if reply.get('backup'):
        body['backup'] = reply['backup']
    if detail:
        body['detail'] = detail
    return body


def commit(manager, edits, restart):
    if not edits:
        return _result(manager, {'changed': False}, False)
    # A fresh install is already serving defaults. The first Settings save creates
    # config.toml the same way a missing file is an empty document for the editor.
    reply = manager.config_editor.apply(edits, label='settings')
    if reply.get('changed'):
        adopt_config(manager, load_config(manager.config.home))
    return _result(manager, reply, restart)


_MISSING = object()


def _read_path(config, path):
    """Follow one config path through dataclass sections and plain dict tables."""
    node = config
    for part in path:
        if isinstance(node, dict):
            if part not in node:
                return _MISSING
            node = node[part]
        else:
            if not hasattr(node, part):
                return _MISSING
            node = getattr(node, part)
    return node


def _row(config, path):
    value = _read_path(config, path)
    return {'value': None if value is _MISSING else value, 'present': value is not _MISSING}


def pending_edits(manager, edits):
    """For each edited path: what the file now says, and what the running core holds.

    Raw config writes do not adopt, so the running process keeps its old values
    until explicit adoption, a reload, or a settings save re-reads the whole file.
    This compares the two instead of assuming either one.
    """
    try:
        on_disk = load_config(manager.config.home)
    except (ValueError, TypeError, OSError):
        return None
    return _pending_edits(on_disk, manager.config, edits)


def _pending_edits(on_disk, running, edits):
    rows = []
    for edit in edits:
        path = list(edit['path'])
        file_side = _row(on_disk, path)
        running_side = _row(running, path)
        rows.append({
            'path': path,
            'file': file_side['value'], 'file_present': file_side['present'],
            'running': running_side['value'], 'running_present': running_side['present'],
        })
    return rows


def adopt_file_result(manager):
    """Re-read the file and report the values actually moved into the core."""
    try:
        if not (manager.config.home / 'config.toml').is_file():
            raise ValueError('Config file is missing')
        config = load_config(manager.config.home)
        if config.config_missing:
            raise ValueError('Config file is missing')
    except (ValueError, TypeError, OSError) as error:
        raise ValueError('Cannot adopt config.toml: the config file could not be re-read') from error
    previous = manager.config
    paths = []

    def collect(value, path):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                collect(child, path + [key])
        elif path not in paths:
            paths.append(path)

    collect(asdict(previous), [])
    collect(asdict(config), [])
    adopt_config(manager, config)
    rows = _pending_edits(manager.config, previous, [{'path': path} for path in paths])
    changed = [row['path'] for row in rows
               if row['file'] != row['running'] or row['file_present'] != row['running_present']]
    detail = "The running core re-read config.toml and now serves the file's values."
    if not changed:
        detail += ' No values changed.'
    return {'applied': 'live', 'changed': changed, 'detail': detail,
            'running_core': {'known': True, 'agrees': True, 'pending': []}}


def file_write_result(manager, edits, reply):
    """Report a raw config write honestly: the file changed, the core may not have.

    POST /api/settings adopts its edit into the running core. POST /api/config does
    not. The reply names that disagreement.
    """
    body = dict(reply)
    if not reply.get('changed'):
        body['applied'] = 'live'
        return body
    rows = pending_edits(manager, edits)
    if rows is None:
        body['applied'] = 'restart'
        body['running_core'] = {'known': False,
                                'detail': 'The config file could not be re-read, so what the running core holds is unknown.'}
        return body
    disagreeing = [row for row in rows
                   if row['file'] != row['running'] or row['file_present'] != row['running_present']]
    agrees = not disagreeing
    body['restart_required'] = not agrees
    body['applied'] = 'live' if agrees else 'restart'
    body['running_core'] = {'known': True, 'agrees': agrees, 'pending': disagreeing}
    if disagreeing:
        names = ', '.join('.'.join(row['path']) for row in disagreeing)
        body['detail'] = (f'The file now holds the new value for {names}, but the running core does not: '
                          'this route writes config.toml without adopting it. The change takes effect on the '
                          'next core reload, or when a settings save re-reads the file.')
    return body


def _plain_env_name(value):
    """A variable name. Empty is not a name; a secret-shaped value is not a name."""
    return isinstance(value, str) and ENV_NAME.fullmatch(value) is not None and not flagged_env_value(value)


def _env_var_name(value, label):
    """An env-var field stores a name. Empty clears it. A pasted secret is refused."""
    if value is None or value == '':
        return None
    if not _plain_env_name(value):
        raise ValueError(label + ' must be an environment variable name, not a secret value')
    return value


def _probe_models(url):
    """Ask the endpoint for its model list.

    Connected means the host answered with something other than 401 or 403.
    Those two are Reachable, key rejected. A network failure is Unreachable.
    """
    from urllib import error, request
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return 'Unreachable', 'No model list address'
    target = url.rstrip('/') + '/models'
    try:
        req = request.Request(target, method='GET')
        with request.urlopen(req, timeout=0.6) as response:
            response.read(32)
            code = getattr(response, 'status', 200) or 200
        if code in (401, 403):
            return 'Reachable, key rejected', ''
        return 'Connected', ''
    except error.HTTPError as exc:
        if exc.code in (401, 403):
            return 'Reachable, key rejected', ''
        return 'Connected', ''
    except Exception as exc:
        text = getattr(exc, 'reason', None) or str(exc) or type(exc).__name__
        return 'Unreachable', str(text)[:180]


class SettingsService:
    def __init__(self, manager, *, keychain=None):
        self.manager = manager
        self.keychain = keychain or KeychainStore()

    def view(self):
        """The settings projection, with every config-file read failure named.

        The backup list, the change report and the drift check each read the
        config directory. A failure in one of them is reported beside the rest
        of the page instead of shortening a list with no explanation.
        """
        editor = getattr(self.manager, 'config_editor', None)
        problems = []
        if editor is not None:
            try:
                editor.notice_drift()
            except OSError as error:
                problems.append(type(error).__name__ + ': ' + str(error))
        body = project(self.manager.config, self.manager.approval_status(), self.manager)
        body['supervised'] = bool(self.manager.config.server.supervised)
        if editor is not None:
            try:
                body['config_backups'] = editor.backups()
            except OSError as error:
                body['config_backups'] = []
                body['config_backups_error'] = type(error).__name__ + ': ' + str(error)
            report = editor.change_report(getattr(self.manager.config.ui, 'config_journal_entries', None))
            body['config_changes'] = report['entries']
            body['config_changes_shown'] = report['shown']
            body['config_changes_total'] = report['total']
            body['config_journal_entries'] = report['keep']
            if report.get('error'):
                problems.append(report['error'])
        self.manager.config_journal_error = '; '.join(problems)
        if problems:
            body['config_journal_error'] = self.manager.config_journal_error
        return body

    def restore_backup(self, name):
        """Put a ConfigEditor backup back, then re-read it into the running core."""
        editor = self.manager.config_editor
        reply = editor.restore(name)
        adopt_config(self.manager, load_config(self.manager.config.home))
        # applyResult reads result.settings. The projection at the root would
        # leave the page on the values from before this restore.
        return {
            'changed': True,
            'restart_required': True,
            'applied': 'restart',
            'backup': reply.get('backup'),
            'restored': reply.get('restored'),
            'detail': ('Restored ' + str(reply.get('restored'))
                       + '. Restart the core to apply listener and process settings.'),
            'settings': self.view(),
        }

    def apply(self, body):
        if not isinstance(body, dict) or not isinstance(body.get('id'), str):
            raise ValueError('Missing setting id')
        action = body.get('action') or 'set'
        record = body.get('record')
        secret = record.get('secret') if isinstance(record, dict) else None
        try:
            handler = {
                'set': self._set, 'add': self._add, 'edit': self._edit, 'remove': self._remove,
                'check': self._check,
            }.get(action)
            if handler is None:
                raise ValueError('Unknown setting action')
            result = handler(body)
        finally:
            if isinstance(record, dict):
                record.pop('secret', None)
        if isinstance(secret, str) and secret and secret in json.dumps(result):
            raise ValueError('Setting was not saved')
        return result

    def _set(self, body):
        if 'value' not in body:
            raise ValueError('Missing value')
        approval = self.manager.approval_status()
        spec = find_spec(self.manager.config, approval, body['id'])
        if spec is None:
            raise ValueError('Unknown setting')
        if spec.id == 'aethermind.enabled':
            raise ValueError('aethermind.enabled is retired and cannot turn memory off')
        if spec.write == 'display':
            raise ValueError('Edited in config.toml')
        if spec.write == 'approval':
            return self._approval(body)
        value = coerce(spec, body['value'])
        if spec.id == 'voice.account':
            return self._voice_account(spec, value)
        if spec.id in ('voice.stt.base_url', 'voice.tts.base_url'):
            return self._speech_endpoint(spec, value)
        if spec.id == 'aethermind.url':
            endpoints = list(self.manager.config.aethermind.endpoints or [])
            if endpoints:
                raise ValueError(
                    'AetherMind endpoints are already configured and take precedence, so saving a URL '
                    'here would change nothing. Clear the ordered endpoints first, or edit them instead.')
        if spec.id == 'voice.voice':
            options = dict(self.manager.config.voice.session_options or {})
            options['voice'] = value
            edits = [{'path': ['voice', 'session_options'], 'value': options}]
        else:
            if spec.id == 'swarm.max_job_tasks':
                value = _saved_job_tasks(value)
            elif spec.id == 'swarm.distinct_workers':
                value = None if value == '' else value == 'true'
            elif spec.empty_removes and value == '':
                value = None
            edits = [{'path': list(spec.path), 'value': value}]
        staged = None
        if spec.id == 'autonomy.world_view' and isinstance(value, str):
            try:
                staged = staged_world_view(self.manager.config, value)
            except FloorDefinitionError as exc:
                raise ValueError(str(exc)) from None
        result = commit(self.manager, edits, spec.apply == 'restart')
        if staged is not None:
            try:
                save_definition(self.manager.config, staged, 'operator')
            except FloorDefinitionError as exc:
                raise ValueError(str(exc)) from None
        return result

    def _voice_account(self, spec, value):
        """Bind duplex voice to an account, and materialize the provider profile.

        The page names an account, never a credential. Choosing one writes the
        credential the account already holds and the endpoint details the provider
        publishes, so no socket, token path or prefix is typed by hand.
        """
        config = self.manager.config
        connections = duplex_connections(config)
        chosen = next((item for item in connections if item['reference'] == value), None)
        if not value:
            edits = [
                {'path': ['voice', 'credential_ref'], 'value': ''},
                {'path': ['voice', 'websocket_url'], 'value': ''},
                {'path': ['voice', 'api_base_url'], 'value': ''},
                {'path': ['voice', 'client_secret_path'], 'value': ''},
                {'path': ['voice', 'client_protocol_prefix'], 'value': ''},
                {'path': ['voice', 'model'], 'value': ''},
                {'path': ['voice', 'voices'], 'value': []},
            ]
            result = commit(self.manager, edits, False)
            result['detail'] = 'Duplex voice has no account.'
            return result
        if chosen is None:
            raise ValueError('That account does not offer a duplex model')
        profile = DUPLEX_PROVIDERS[chosen['provider']]
        edits = []
        if chosen['reference'] not in config.credentials:
            edits.append({'path': ['credentials', chosen['reference'], 'oauth'], 'value': chosen['provider']})
        model = config.voice.model
        if model not in profile['models']:
            model = profile['models'][0]
        edits.extend([
            {'path': ['voice', 'credential_ref'], 'value': chosen['reference']},
            {'path': ['voice', 'websocket_url'], 'value': profile['websocket_url']},
            {'path': ['voice', 'api_base_url'], 'value': profile['api_base_url']},
            {'path': ['voice', 'client_secret_path'], 'value': profile['client_secret_path']},
            {'path': ['voice', 'client_protocol_prefix'], 'value': profile['client_protocol_prefix']},
            {'path': ['voice', 'model'], 'value': model},
        ])
        # Ask the provider which voices it offers. The answer becomes the voice
        # dropdown. A failed probe clears a stale list instead of leaving names
        # from another provider in place, and says why.
        note = ''
        try:
            voices = probe_voices(config, chosen)
        except SpeechProbeError as error:
            voices = []
            note = ' ' + str(error)
        edits.append({'path': ['voice', 'voices'], 'value': voices})
        if voices:
            current = str((config.voice.session_options or {}).get('voice') or '').lower()
            selected = current if current in voices else ('eve' if 'eve' in voices else voices[0])
            options = dict(config.voice.session_options or {})
            options['voice'] = selected
            edits.append({'path': ['voice', 'session_options'], 'value': options})
        result = commit(self.manager, edits, False)
        result['detail'] = 'Duplex account set to ' + chosen['label'] + '.' + note
        return result

    def _speech_endpoint(self, spec, value):
        """Save a local speech endpoint and list the models it reports.

        The model list is what the server answered, so the model field is a
        dropdown of real names. A failed probe keeps the endpoint and says why.
        """
        service_id = spec.path[1]
        service = getattr(self.manager.config.voice, service_id)
        base = ['voice', service_id]
        models = []
        detail = 'Endpoint cleared.'
        if value:
            try:
                models = probe_models(value, service.timeout)
                count = str(len(models)) + ' model' + ('' if len(models) == 1 else 's')
                detail = 'Endpoint saved. ' + count + ' listed.'
            except SpeechProbeError as error:
                detail = 'Endpoint saved. Could not list models: ' + str(error)
        edits = [{'path': list(spec.path), 'value': value},
                 {'path': base + ['models'], 'value': models}]
        if not value or (service.model and service.model not in models):
            edits.append({'path': base + ['model'], 'value': ''})
        result = commit(self.manager, edits, False)
        result['detail'] = detail
        return result

    def _approval(self, body):
        value = body.get('value')
        if value not in ('safe', 'yolo'):
            raise ValueError('Approval is Safe or Unleashed')
        status = self.manager.set_yolo(value == 'yolo')
        result = _result(self.manager, {'changed': True}, False)
        result['approval'] = status
        return result

    def _add(self, body):
        record = body.get('record')
        ident = body['id']
        if ident == 'identities':
            _check_keys(record, {'name', 'display_name'})
            name = record.get('name')
            if not isinstance(name, str) or not name.strip():
                raise ValueError('A person needs a name')
            created = self.manager.identity_create(name, record.get('display_name'))
            return _result(self.manager, {'changed': True}, False, 'Recorded %s' % created['display_name'])
        if ident == 'named_accounts':
            _refuse_retired_pool(record)
            _check_keys(record, ACCOUNT_KEYS)
            name = _require_name(record.get('name'))
            if name in self.manager.config.accounts:
                raise ValueError('Account already exists')
            values = {'provider_kind': 'cli_worker', 'credential_ref': '', **record}
            return commit(self.manager, self._account_edits(name, values), True)
        if ident == 'providers':
            _check_keys(record, PROVIDER_KEYS)
            return commit(self.manager, self._provider_add(record), False)
        if ident == 'credentials':
            _check_keys(record, CREDENTIAL_KEYS)
            return self._credential_add(record)
        if ident == 'mcp_servers':
            _check_keys(record, MCP_KEYS)
            edits = self._mcp_add(record)
            result = commit(self.manager, edits, False)
            return self._finish_plugins(result, [_require_name(record.get('name'))])
        if ident == 'hosts':
            _check_keys(record, HOST_KEYS)
            name = _require_name(record.get('name'))
            if name in (self.manager.config.hosts or {}):
                raise ValueError('Host already exists')
            edits = self._host_body(name, record, creating=True)
            if not edits:
                raise ValueError('A host needs at least a kind')
            return commit(self.manager, edits, True)
        if ident == 'systems':
            _check_keys(record, SYSTEM_KEYS)
            system_id = record.get('id')
            if not isinstance(system_id, str) or not system_id.strip():
                raise ValueError('A system needs an id')
            if system_id in (self.manager.config.systems or {}):
                raise ValueError('System already exists')
            edits = self._system_body(system_id, record, creating=True)
            return commit(self.manager, edits, True)
        raise ValueError('Unknown setting')

    def _edit(self, body):
        record = body.get('record')
        name = _require_name(body.get('name'))
        ident = body['id']
        if ident == 'worker_policy':
            _check_keys(record, {'capability', 'cost_rank'})
            rows = worker_policy_field(self.manager.config)['records']
            row = next((row for row in rows if row['name'] == name), None)
            if row is None:
                raise ValueError('Unknown worker policy')
            edits = []
            for key, value in record.items():
                if key == 'capability' and value not in ('routine', 'standard', 'complex'):
                    raise ValueError('Unknown capability')
                if key == 'cost_rank' and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError('Cost rank must be finite and nonnegative')
                edits.append({'path': ['agents', 'backend_policy', name, key], 'value': value})
            return commit(self.manager, edits, False)
        if ident == 'identities':
            _check_keys(record, {'name', 'display_name'})
            registry = getattr(self.manager.config, 'identity_registry', None)
            identity = registry.store.by_name(name) if registry is not None else None
            if identity is None:
                raise ValueError('Unknown identity')
            renamed = self.manager.identity_rename(identity['id'], record.get('name'),
                                                   record.get('display_name'))
            return _result(self.manager, {'changed': True}, False, 'Renamed %s' % renamed['display_name'])
        if ident == 'named_accounts':
            _refuse_retired_pool(record)
            _check_keys(record, ACCOUNT_KEYS - {'name'})
            if name not in self.manager.config.accounts:
                raise ValueError('Unknown account')
            return commit(self.manager, self._account_edits(name, record), True)
        if ident == 'providers':
            _check_keys(record, PROVIDER_KEYS - {'name'})
            return commit(self.manager, self._provider_edit(name, record), False)
        if ident == 'credentials':
            _check_keys(record, CREDENTIAL_KEYS - {'name'})
            return self._credential_edit(name, record)
        if ident == 'mcp_servers':
            if name == 'aethermind' and name not in self.manager.config.mcp_servers:
                self._refuse_aethermind_write()
            _check_keys(record, MCP_KEYS - {'name'})
            result = commit(self.manager, self._mcp_edit(name, record), False)
            return self._finish_plugins(result, [name])
        if ident == 'hosts':
            _check_keys(record, HOST_KEYS - {'name'})
            if name not in (self.manager.config.hosts or {}):
                raise ValueError('Unknown host')
            edits = self._host_body(name, record, creating=False)
            if not edits:
                raise ValueError('Nothing to change')
            return commit(self.manager, edits, True)
        if ident == 'systems':
            _check_keys(record, SYSTEM_KEYS - {'id'})
            if name not in (self.manager.config.systems or {}):
                raise ValueError('Unknown system')
            edits = self._system_body(name, record, creating=False)
            if not edits:
                raise ValueError('Nothing to change')
            return commit(self.manager, edits, True)
        raise ValueError('Unknown setting')

    def _remove(self, body):
        name = _require_name(body.get('name'))
        ident = body['id']
        config = self.manager.config
        if ident == 'named_accounts':
            if name not in config.accounts:
                raise ValueError('Unknown account')
            for backend_name, backend in config.backends.items():
                if backend.account == name:
                    raise ValueError(f'Account is used by backend {backend_name}')
            return commit(self.manager, [{'path': ['accounts', name], 'value': None}], True)
        if ident == 'providers':
            if name not in config.backends:
                raise ValueError('Unknown provider')
            replacement = _local_first_chat_backend(config, name)
            chat_roles = []
            other_roles = []
            for role, backend_name in config.roles.items():
                if backend_name != name:
                    continue
                if role in ('main', 'fast', 'compactor', 'failover'):
                    chat_roles.append(role)
                else:
                    other_roles.append(role)
            if chat_roles and not replacement:
                raise ValueError('Add another chat model before removing this provider')
            edits = [{'path': ['backends', name], 'value': None}]
            for role in chat_roles:
                edits.append({'path': ['roles', role], 'value': replacement})
            for role in other_roles:
                edits.append({'path': ['roles', role], 'value': None})
            result = commit(self.manager, edits, False)
            if chat_roles and replacement:
                result['detail'] = ('Removed %s. %s now uses %s.'
                                    % (name, ', '.join(chat_roles), replacement))
            return result
        if ident == 'credentials':
            if name not in config.credentials:
                raise ValueError('Unknown credential')
            if credential_in_use(config, name):
                raise ValueError('Credential is in use')
            reference = config.credentials[name]
            result = commit(self.manager, [{'path': ['credentials', name], 'value': None}], False)
            if reference.keychain_service and reference.keychain_account:
                try:
                    self.keychain.delete(reference.keychain_service, reference.keychain_account)
                except ValueError:
                    result['detail'] = 'Removed from config. The keychain item is still there.'
            return result
        if ident == 'hosts':
            if name not in (config.hosts or {}):
                raise ValueError('Unknown host')
            return commit(self.manager, [{'path': ['hosts', name], 'value': None}], True)
        if ident == 'systems':
            if name not in (config.systems or {}):
                raise ValueError('Unknown system')
            return commit(self.manager, [{'path': ['systems', name], 'value': None}], True)
        if ident == 'mcp_servers':
            if name == 'aethermind' and name not in config.mcp_servers:
                self._refuse_aethermind_write()
            if name not in config.mcp_servers:
                raise ValueError('Unknown plugin')
            from .mcp_servers import computer_backend, memory_endpoint
            endpoint = memory_endpoint(config, name)
            if endpoint:
                self._refuse_memory_plugin(name, endpoint, 'removing')
            computer_use = config.computer_use.provider == f'mcp:{name}' and computer_backend(config) == 'mcp'
            edits = [{'path': ['mcp_servers', name], 'value': None}]
            if config.computer_use.provider == f'mcp:{name}':
                edits.append({'path': ['computer_use', 'provider'], 'value': ''})
            result = commit(self.manager, edits, False)
            return self._finish_plugins(result, [name], computer_use=computer_use)
        raise ValueError('Unknown setting')

    def _account_edits(self, name, record):
        return [
            {'path': list(spec.path), 'value': coerce(spec, record[spec.key])}
            for spec in _account_specs(self.manager.config, name)
            if spec.key in record and not spec.inert
        ]

    def _host_body(self, name, record, *, creating):
        """Turn a host form into config edits, validating what the loader will check.

        The editor re-loads and validates the whole config after writing, so a bad row is
        refused at the door rather than left to fail at the next reload. This function
        only has to reject what would be ambiguous in a form: a kind the loader does not
        accept, a prometheus row with no instance (the loader requires one), and a
        non-positive timeout.
        """
        existing = None if creating else (self.manager.config.hosts or {}).get(name)
        if not creating and existing is None:
            raise ValueError('Unknown host')
        edits = []

        def put(field, value):
            edits.append({'path': ['hosts', name, field], 'value': value})

        if creating or 'enabled' in record:
            enabled = record.get('enabled', True)
            if not isinstance(enabled, bool):
                raise ValueError('Watched must be true or false')
            put('enabled', enabled)
        # The row never carries `name`: load_config builds HostConfig(name=<table key>,
        # **values), so a `name` key in the table is an unknown option and the load is
        # refused. The table key IS the name. Measured against the live config: no
        # [hosts.<id>] table carries a name key.
        for field in ('label', 'address', 'prometheus_instance', 'ssh_alias'):
            if field in record:
                value = record[field]
                if not isinstance(value, str):
                    raise ValueError(f'{field} must be text')
                put(field, value)
        if 'kind' in record:
            kind = record['kind']
            if kind not in HOST_KINDS:
                raise ValueError('Unknown host kind')
            put('kind', kind)
        if 'timeout' in record:
            timeout = record['timeout']
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError('Timeout must be a number')
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('Timeout must be positive and finite')
            put('timeout', float(timeout))
        if 'probe_command' in record:
            put('probe_command', _lines(record['probe_command']))

        # Cross-field check on the row as it will exist, not just the fields supplied.
        kind = record.get('kind', getattr(existing, 'kind', ''))
        instance = record.get('prometheus_instance', getattr(existing, 'prometheus_instance', ''))
        if kind == 'prometheus' and not instance:
            raise ValueError('A prometheus host needs a Prometheus instance label')
        if kind == 'prometheus' and not self.manager.config.infrastructure.prometheus_url:
            raise ValueError('A prometheus host needs infrastructure.prometheus_url set first')
        return edits

    def _system_body(self, system_id, record, *, creating):
        """Turn a systems form into config edits, validating what load_systems will check.

        The editor re-loads and validates the whole config after writing, so a bad record is
        refused at the door. This rejects what would be ambiguous in a form: an unknown kind,
        a missing role (a record with no purpose is not a record), a watched endpoint with no
        address, and a signal value the validator would reject.
        """
        existing = None if creating else (self.manager.config.systems or {}).get(system_id)
        if not creating and existing is None:
            raise ValueError('Unknown system')
        edits = []

        def put(key, value):
            edits.append({'path': ['systems', system_id, key], 'value': value})

        for key in ('label', 'role', 'address', 'auth', 'notes'):
            if key in record:
                value = record[key]
                if not isinstance(value, str):
                    raise ValueError(f'{key} must be text')
                put(key, value)
        if 'kind' in record:
            kind = record['kind']
            if kind not in SYSTEM_KINDS:
                raise ValueError('Unknown system kind')
            put('kind', kind)
        if 'watch' in record:
            if type(record['watch']) is not bool:
                raise ValueError('Watched must be true or false')
            put('watch', record['watch'])

        signals = dict(getattr(existing, 'signals', {}) or {})
        for key in ('disk_free_percent_below', 'stale_after_seconds', 'max_wakes_per_hour'):
            if key in record:
                raw = record[key]
                if raw == '' or raw is None:
                    signals.pop(key, None)
                    continue
                try:
                    number = float(raw)
                except (TypeError, ValueError):
                    raise ValueError(f'{key} must be a number') from None
                if key == 'disk_free_percent_below':
                    if not 0 <= number <= 100:
                        raise ValueError('disk_free_percent_below must be between 0 and 100')
                elif number <= 0:
                    raise ValueError(f'{key} must be positive')
                signals[key] = int(number) if key != 'stale_after_seconds' else number
        if 'managed' in record:
            if type(record['managed']) is not bool:
                raise ValueError('Ours to repair must be true or false')
            signals['managed'] = record['managed']
        if any(key in record for key in ('disk_free_percent_below', 'stale_after_seconds',
                                         'max_wakes_per_hour', 'managed')):
            put('signals', signals)

        # Cross-field checks on the row as it will exist, not just the fields supplied.
        role = record.get('role', getattr(existing, 'role', ''))
        if creating and not (role or '').strip():
            raise ValueError('A system needs a purpose: what it is for')
        kind = record.get('kind', getattr(existing, 'kind', ''))
        address = record.get('address', getattr(existing, 'address', ''))
        watch = record.get('watch', getattr(existing, 'watch', False))
        if kind == 'endpoint' and watch and not (address or '').strip():
            raise ValueError('A watched endpoint needs an address to poll')
        auth = record.get('auth', getattr(existing, 'auth', ''))
        if auth and auth not in self.manager.config.credentials:
            raise ValueError(f'Credential {auth} is not configured')
        return edits

    def _provider_add(self, record):
        name = _require_name(record.get('name'))
        config = self.manager.config
        if name in config.backends:
            raise ValueError('Provider already exists')
        kind = record.get('kind')
        account = record.get('account') or ''
        oauth = OAUTH_MODELS.get(account)
        if account and account != 'other' and oauth is None:
            raise ValueError('Unknown account')
        if oauth is not None:
            kind = oauth['kind']
        if kind not in PROVIDER_KINDS:
            raise ValueError('Unknown provider kind')
        chat = record.get('chat', True)
        if not isinstance(chat, bool):
            raise ValueError('Use for chat must be true or false')
        edits = [
            {'path': ['backends', name, 'provider_kind'], 'value': kind},
            {'path': ['backends', name, 'chat'], 'value': chat},
            {'path': ['backends', name, 'enabled'], 'value': True},
        ]
        if kind == 'cli_worker':
            vendor = record.get('vendor') or ''
            command = record.get('command') or ''
            cwd = record.get('cwd') or ''
            if vendor not in VENDORS or not isinstance(command, str) or not command or not isinstance(cwd, str) or not cwd:
                raise ValueError('A CLI provider needs a vendor, command, and working directory')
            edits.extend([
                {'path': ['backends', name, 'cli', 'vendor'], 'value': vendor},
                {'path': ['backends', name, 'cli', 'command'], 'value': command},
                {'path': ['backends', name, 'cli', 'cwd'], 'value': cwd},
            ])
        elif oauth is not None:
            model = record.get('model') or ''
            if not isinstance(model, str) or not model:
                raise ValueError('A model id is required')
            credential_name = oauth['credential']
            reference = config.credentials.get(credential_name)
            if reference is None:
                edits.append({'path': ['credentials', credential_name, 'oauth'], 'value': oauth['oauth']})
            elif reference.oauth != oauth['oauth']:
                raise ValueError('That credential name is already used for something else')
            edits.extend([
                {'path': ['backends', name, 'base_url'], 'value': oauth['base_url']},
                {'path': ['backends', name, 'model'], 'value': model},
                {'path': ['backends', name, 'credential_ref'], 'value': credential_name},
            ])
        else:
            base = record.get('base_url') or ''
            model = record.get('model') or ''
            if not isinstance(base, str) or not base or not isinstance(model, str) or not model:
                raise ValueError('A provider needs a base URL and a model')
            edits.extend([
                {'path': ['backends', name, 'base_url'], 'value': base},
                {'path': ['backends', name, 'model'], 'value': model},
            ])
            credential = record.get('credential') or ''
            if credential:
                if credential not in config.credentials:
                    raise ValueError('Unknown credential')
                edits.append({'path': ['backends', name, 'credential_ref'], 'value': credential})
            if 'api_key_env' in record:
                edits.append({'path': ['backends', name, 'api_key_env'],
                              'value': _env_var_name(record.get('api_key_env'), 'API key env var')})
        _append_concurrency(edits, name, record)
        _append_throttle(edits, name, record)
        if chat and kind != 'cli_worker' and not (config.roles.get('main') or '').strip():
            edits.append({'path': ['roles', 'main'], 'value': name})
        return edits

    def _provider_edit(self, name, record):
        config = self.manager.config
        backend = config.backends.get(name)
        if backend is None:
            raise ValueError('Unknown provider')
        kind = _kind(config, backend)
        edits = []
        if 'enabled' in record:
            if not isinstance(record['enabled'], bool):
                raise ValueError('Enabled must be true or false')
            if record['enabled'] is False and config.roles.get('main') == name:
                raise ValueError('Choose another chat model before disabling this provider')
            edits.append({'path': ['backends', name, 'enabled'], 'value': record['enabled']})
        if 'chat' in record:
            if not isinstance(record['chat'], bool):
                raise ValueError('Use for chat must be true or false')
            edits.append({'path': ['backends', name, 'chat'], 'value': record['chat']})
        if kind == 'cli_worker':
            if 'model' in record:
                if not isinstance(record['model'], str):
                    raise ValueError('Model must be text')
                edits.append({'path': ['backends', name, 'model'], 'value': record['model']})
            for key, toml_key in (('vendor', 'vendor'), ('command', 'command'), ('cwd', 'cwd')):
                if key in record:
                    if not isinstance(record[key], str) or not record[key]:
                        raise ValueError(f'{key} must be text')
                    edits.append({'path': ['backends', name, 'cli', toml_key], 'value': record[key]})
            if 'args' in record:
                edits.append({'path': ['backends', name, 'cli', 'args'], 'value': _lines(record['args'])})
            if 'allow_api_key_fallback' in record:
                if not isinstance(record['allow_api_key_fallback'], bool):
                    raise ValueError('API key fallback must be true or false')
                edits.append({'path': ['backends', name, 'cli', 'allow_api_key_fallback'],
                              'value': record['allow_api_key_fallback']})
            if 'fallback_env' in record:
                if not isinstance(record['fallback_env'], str) or (record['fallback_env'] and not _plain_env_name(record['fallback_env'])):
                    raise ValueError('Fallback env var must be a name')
                edits.append({'path': ['backends', name, 'cli', 'fallback_env'], 'value': record['fallback_env']})
            if 'fallback_credential' in record:
                credential = record['fallback_credential'] or ''
                if credential and credential not in config.credentials:
                    raise ValueError('Unknown credential')
                edits.append({'path': ['backends', name, 'cli', 'fallback_credential_ref'], 'value': credential})
        else:
            for key in ('base_url', 'model'):
                if key in record:
                    if not isinstance(record[key], str) or not record[key]:
                        raise ValueError(f'{key} must be text')
                    edits.append({'path': ['backends', name, key], 'value': record[key]})
            if 'credential' in record:
                credential = record['credential'] or ''
                if credential and credential not in config.credentials:
                    raise ValueError('Unknown credential')
                edits.append({'path': ['backends', name, 'credential_ref'], 'value': credential or None})
            if 'api_key_env' in record:
                edits.append({'path': ['backends', name, 'api_key_env'],
                              'value': _env_var_name(record.get('api_key_env'), 'API key env var')})
        _append_concurrency(edits, name, record)
        _append_throttle(edits, name, record)
        if not edits:
            raise ValueError('Nothing to change')
        return edits

    def _secret_target(self, record):
        """Read the secret and its destination without writing anything.

        The write happens only after the whole record validates. An add that is
        rejected for a bad env name must not have already replaced a working key.
        """
        secret = record.get('secret') or ''
        service = record.get('keychain_service') or ''
        account = record.get('keychain_account') or ''
        if not isinstance(secret, str):
            raise ValueError('Credential value is empty or too long')
        return secret, service, account

    def _secret_destination(self, name, record, *, creating):
        """Resolve where a new secret goes, before any mutation.

        A record that only carries the secret is the normal edit from the page, so
        an edit falls back to the slot the credential already declares. Validating
        here is what keeps a rejected save from having already overwritten a
        working key.
        """
        secret, service, account = self._secret_target(record)
        if secret and (not service or not account) and not creating:
            existing = self.manager.config.credentials.get(name)
            if existing is not None:
                service = service or existing.keychain_service
                account = account or existing.keychain_account
        if secret:
            KeychainStore._names(service, account)
        return secret, service, account

    def _destination_owner(self, service, account, *, allow=''):
        """Name the credential that already owns this keychain slot, if any.

        Two credentials pointing at one slot means one of them silently reads the
        other's secret, and deleting one destroys the other's. Refuse that before
        writing rather than discovering it later.
        """
        for name, reference in self.manager.config.credentials.items():
            if name == allow:
                continue
            if reference.keychain_service == service and reference.keychain_account == account:
                return name
        return ''

    def _store_secret(self, secret, service, account):
        prior, state = self.keychain.get(service, account)
        self.keychain.put(service, account, secret)
        return prior, state

    def _restore_secret(self, service, account, prior, state):
        """Put the slot back the way it was. A slot that was unavailable is left
        alone: we cannot know whether it held a value, and deleting on that guess
        is what broke a working credential."""
        if not service or not account:
            return
        try:
            if state == 'available' and prior:
                self.keychain.put(service, account, prior)
            elif state == 'missing':
                self.keychain.delete(service, account)
        except ValueError:
            pass

    def _credential_edits(self, name, record, *, creating):
        service = record.get('keychain_service', None)
        account = record.get('keychain_account', None)
        env = record.get('env', None)
        edits = []
        if service is not None:
            if not isinstance(service, str):
                raise ValueError('Keychain service must be text')
            edits.append({'path': ['credentials', name, 'keychain_service'], 'value': service or None})
        if account is not None:
            if not isinstance(account, str):
                raise ValueError('Keychain account must be text')
            edits.append({'path': ['credentials', name, 'keychain_account'], 'value': account or None})
        if env is not None:
            if not isinstance(env, str) or (env and not _plain_env_name(env)):
                raise ValueError('Env var must be a name')
            edits.append({'path': ['credentials', name, 'env'], 'value': env or None})
        if creating and not edits:
            raise ValueError('Add a key or an environment variable name')
        return edits

    def _credential_add(self, record):
        name = _require_name(record.get('name'))
        if name in self.manager.config.credentials:
            raise ValueError('Credential already exists')
        # Validate the whole record before any mutation. This is the ordering that
        # was wrong: the key was written first and the env name checked after, so a
        # 400 arrived with the destination already overwritten.
        secret, service, account = self._secret_destination(name, record, creating=True)
        edits = self._credential_edits(name, record, creating=True)
        if not secret and not (record.get('env') or ''):
            raise ValueError('Add a key or an environment variable name')
        if secret:
            owner = self._destination_owner(service, account)
            if owner:
                raise ValueError(f'Keychain slot is already used by {owner}')
        prior, state = ('', 'missing')
        if secret:
            prior, state = self._store_secret(secret, service, account)
        try:
            return commit(self.manager, edits, False)
        except Exception:
            if secret:
                self._restore_secret(service, account, prior, state)
            raise

    def _credential_edit(self, name, record):
        if name not in self.manager.config.credentials:
            raise ValueError('Unknown credential')
        secret, service, account = self._secret_destination(name, record, creating=False)
        edits = self._credential_edits(name, record, creating=False)
        if not edits and not secret:
            raise ValueError('Nothing to change')
        if secret:
            owner = self._destination_owner(service, account, allow=name)
            if owner:
                raise ValueError(f'Keychain slot is already used by {owner}')
        prior, state = ('', 'missing')
        if secret:
            prior, state = self._store_secret(secret, service, account)
        try:
            if edits:
                result = commit(self.manager, edits, False)
            else:
                result = _result(self.manager, {'changed': False}, False)
            if secret:
                result['changed'] = True
            return result
        except Exception:
            if secret:
                self._restore_secret(service, account, prior, state)
            raise

    def _mcp_add(self, record):
        name = _require_name(record.get('name'))
        if name in self.manager.config.mcp_servers:
            raise ValueError('Server already exists')
        return self._mcp_body(name, record, creating=True)

    def _mcp_edit(self, name, record):
        if name not in self.manager.config.mcp_servers:
            raise ValueError('Unknown server')
        edits = self._mcp_body(name, record, creating=False)
        if not edits:
            raise ValueError('Nothing to change')
        return edits

    def _mcp_body(self, name, record, *, creating):
        edits = []
        existing = None if creating else self.manager.config.mcp_servers[name]
        if 'enabled' in record or creating:
            enabled = record.get('enabled', True)
            if not isinstance(enabled, bool):
                raise ValueError('Enabled must be true or false')
            from .mcp_servers import memory_endpoint
            endpoint = memory_endpoint(self.manager.config, name)
            turning_off = enabled is False and (creating or existing is None or existing.enabled)
            if endpoint and turning_off:
                self._refuse_memory_plugin(name, endpoint, 'disabling')
            edits.append({'path': ['mcp_servers', name, 'enabled'], 'value': enabled})
        touching = creating or 'url' in record or 'command' in record or 'args' in record
        if touching:
            if 'url' in record and not isinstance(record.get('url'), str):
                raise ValueError('URL must be text')
            if 'command' in record and not isinstance(record.get('command'), str):
                raise ValueError('Command must be text')
            url_text = record.get('url') if 'url' in record else ('' if creating or existing is None else existing.url)
            if 'command' in record:
                program = record.get('command') or ''
            elif existing is not None and existing.command:
                program = existing.command[0]
            else:
                program = ''
            if 'args' in record or creating:
                args = _lines(record.get('args') or '')
            elif existing is not None:
                args = list(existing.command[1:])
            else:
                args = []
            argv = ([program] if program else []) + args
            if bool(url_text) == bool(argv):
                raise ValueError('Set a URL or a command, not both')
            if url_text:
                edits.append({'path': ['mcp_servers', name, 'url'], 'value': url_text})
                edits.append({'path': ['mcp_servers', name, 'command'], 'value': None})
            else:
                edits.append({'path': ['mcp_servers', name, 'command'], 'value': argv})
                edits.append({'path': ['mcp_servers', name, 'url'], 'value': None})
        if 'env' in record or creating:
            edits.append({'path': ['mcp_servers', name, 'env'],
                          'value': _env_map(record.get('env') or '', literal=_unleashed(self.manager))})
        if 'token_env' in record or creating:
            token_env = record.get('token_env') or ''
            if not isinstance(token_env, str) or (token_env and not _plain_env_name(token_env)):
                raise ValueError('Token env must be an environment variable name')
            edits.append({'path': ['mcp_servers', name, 'token_env'], 'value': token_env})
        if 'session_tools' in record or creating:
            session_tools = record.get('session_tools', True)
            if not isinstance(session_tools, bool):
                raise ValueError('Session tools must be true or false')
            edits.append({'path': ['mcp_servers', name, 'session_tools'], 'value': session_tools})
        return edits

    def _check(self, body):
        if body.get('id') != 'mcp_servers':
            raise ValueError('Unknown setting')
        name = body.get('name')
        if not isinstance(name, str) or not name:
            raise ValueError('Name must be letters, numbers, dash, or underscore')
        if name == 'aethermind' and name not in self.manager.config.mcp_servers:
            self._refuse_aethermind_write()
        name = _require_name(name)
        if name not in self.manager.config.mcp_servers:
            raise ValueError('Unknown plugin')
        result = _result(self.manager, {'changed': False}, False)
        return self._finish_plugins(result, [name], saved=False)

    def _open_plugin_sessions(self):
        sessions = []
        for runtime in getattr(self.manager, '_runtimes', {}).values():
            session = getattr(runtime, 'session', None)
            if session is not None and getattr(session, 'depth', 0) == 0 and getattr(session, 'tools', None) is not None:
                sessions.append(session)
        return sessions

    def _remember_plugin(self, name, status):
        cache, lock = _plugin_cache(self.manager)
        with lock:
            prior = cache.get(name) or {}
            stored = dict(status)
            if stored.get('state') == 'disabled':
                last = prior.get('last_error') or (prior.get('detail') if prior.get('state') == 'error' else '')
                stored['last_error'] = last
            elif stored.get('state') == 'error':
                stored['last_error'] = stored.get('detail') or ''
            elif stored.get('ok'):
                stored['last_error'] = ''
            cache[name] = stored
            return dict(stored)

    def _annotate_plugin(self, server, status, sessions, endpoint=''):
        from .mcp_servers import session_plugin_servers
        status = dict(status)

        def finish():
            if endpoint:
                status['detail'] = self._with_memory_sentence(status.get('detail') or '', endpoint)
            return status

        if status.get('state') != 'connected':
            return finish()
        count = status.get('tools', 0)
        if endpoint and not server.enabled:
            status['detail'] = (f'Connected, {count} tools. Enabled is off in config.toml. '
                                'Memory keeps using this plugin.')
            return finish()
        if not server.session_tools:
            status['detail'] = f'Connected, {count} tools, none added to sessions (session tools is off).'
            return finish()
        if server.name not in session_plugin_servers(self.manager.config):
            status['detail'] = (f'Connected, {count} tools. This server is the computer-use provider, '
                                'so these tools are not added as generic session tools.')
            return finish()
        if not sessions:
            status['detail'] = f'Connected, {count} tools. No session is open; a new session will offer them.'
            return finish()
        expected = status.get('registered') or []
        if count == 0 or not expected:
            status['detail'] = f'Connected, {count} tools, offered in {len(sessions)} open session(s).'
            return finish()
        for session in sessions:
            have = set(getattr(session, '_plugin_tools', ()))
            if any(name not in have for name in expected):
                status['ok'] = False
                status['state'] = 'error'
                status['detail'] = f'The server listed {count} tools, but an open session did not register them.'
                return finish()
        status['detail'] = f'Connected, {count} tools, offered in {len(sessions)} open session(s).'
        return finish()

    def _with_memory_sentence(self, detail, endpoint):
        sentence = (f'This plugin serves AetherMind memory ({endpoint}). '
                    'The live memory process stays running.')
        if sentence in (detail or ''):
            return detail or ''
        return ((detail or '').rstrip() + ' ' + sentence).strip()

    def _refuse_memory_plugin(self, name, endpoint, verb):
        raise ValueError(
            f'{name} serves AetherMind memory. aethermind.endpoints includes {endpoint}. '
            'AetherMind has no off switch. '
            f'Change that endpoints entry before {verb} this plugin.')

    def _note_live_memory(self, result, names):
        """Say when a save or check left a memory process running."""
        from .mcp_servers import CATALOG, memory_endpoint
        config = self.manager.config
        split = []
        serving = False
        for name in names:
            endpoint = memory_endpoint(config, name)
            if not endpoint:
                continue
            serving = True
            if CATALOG.cached_client_count(name) > 1:
                split.append(
                    f'{endpoint} keeps the memory process it already opened. '
                    'The saved command is a separate connection.')
        if split:
            addition = ' '.join(split)
        elif serving:
            addition = 'The live memory process stays running.'
        else:
            return result
        detail = result.get('detail') or ''
        if addition not in detail:
            result['detail'] = (detail.rstrip() + ' ' + addition).strip()
        return result

    def _finish_plugins(self, result, names, *, saved=True, computer_use=False):
        from .mcp_servers import CATALOG, memory_endpoint, reload_installed_plugins, selected, tool_name
        statuses, reload_error = reload_installed_plugins(self.manager, only=set(names))
        sessions = self._open_plugin_sessions()
        config = self.manager.config
        computer = []
        for name in names:
            server = config.mcp_servers.get(name)
            endpoint = memory_endpoint(config, name)
            if server is None:
                cache, lock = _plugin_cache(self.manager)
                with lock:
                    cache.pop(name, None)
                # A removed memory server is refused before this point. Any other
                # removal drops the client Check may have started with no session open.
                if not endpoint:
                    CATALOG.close_named(name)
                continue
            if not server.enabled and not endpoint:
                self._remember_plugin(name, {'ok': None, 'state': 'disabled', 'detail': 'Disabled. Not connected.', 'tools': 0})
                # Reload only closes clients attached to an open session. A Check
                # or add before any session exists leaves the inspected process
                # cached here, so disable drops that client itself.
                CATALOG.close_named(name)
                continue
            status = dict(statuses.get(name) or {})
            if not status:
                tools, error = CATALOG.inspect(server, keep=bool(endpoint))
                picked = selected(server, tools or [])
                registered = [tool_name(server, tool['name']) for tool in picked
                              if isinstance(tool, dict) and isinstance(tool.get('name'), str)]
                if error:
                    status = {'ok': False, 'state': 'error', 'detail': error, 'tools': 0, 'registered': []}
                else:
                    status = {'ok': True, 'state': 'connected', 'detail': f'Connected, {len(registered)} tools.',
                              'tools': len(registered), 'registered': registered}
            status = self._annotate_plugin(server, status, sessions, endpoint)
            self._remember_plugin(name, status)
            provider = config.computer_use.provider
            from .mcp_servers import computer_backend
            if provider == f'mcp:{name}' and computer_backend(config) == 'mcp':
                computer.append(name)
        if computer_use and not computer:
            computer.append(names[0] if names else 'plugin')
        result['settings'] = project(self.manager.config, self.manager.approval_status(), self.manager)
        if not saved:
            shown = None
            if names:
                cache, lock = _plugin_cache(self.manager)
                with lock:
                    shown = dict(cache.get(names[0]) or {})
            result['changed'] = False
            result['restart_required'] = False
            result['applied'] = 'live'
            result['detail'] = (shown or {}).get('detail') or 'Not checked.'
            return self._note_live_memory(result, names)
        if reload_error:
            result['restart_required'] = True
            result['applied'] = 'restart'
            result['detail'] = ('Saved in config.toml. Open sessions could not be updated (' + reload_error + '). '
                                'A new session reads the saved config. Restart the core if an open session must change.')
            return self._note_live_memory(result, names)
        if computer:
            result['restart_required'] = True
            result['applied'] = 'restart'
            result['detail'] = ('Saved in config.toml. Generic session tools were reloaded. '
                                + ', '.join(computer) + ' is the computer-use provider: an open session keeps those tools '
                                'until the core restarts. A new session uses the saved server.')
            return self._note_live_memory(result, names)
        result['restart_required'] = False
        result['applied'] = 'live'
        result['detail'] = self._saved_plugin_detail(names, sessions)
        return self._note_live_memory(result, names)

    def _saved_plugin_detail(self, names, sessions):
        """Say what the save actually did. A failed or disabled plugin is not in use."""
        config = self.manager.config
        removed = [name for name in names if name not in config.mcp_servers]
        cache, lock = _plugin_cache(self.manager)
        with lock:
            held = [(name, dict(cache[name])) for name in names if name in cache]
        blocked = []
        withheld = []
        for _name, item in held:
            detail = item.get('detail') or ''
            state = item.get('state')
            if state in ('error', 'disabled') or item.get('ok') is False:
                blocked.append(detail or 'The plugin is not available, so its tools are not offered.')
            elif 'none added to sessions' in detail or 'not added as generic session tools' in detail:
                withheld.append(detail)
        if removed and not held:
            target = ', '.join(removed)
            if sessions:
                return f'Saved in config.toml. {target} was removed from open sessions.'
            return f'Saved in config.toml. {target} was removed. No session is open.'
        if blocked:
            sentence = 'Saved in config.toml. ' + ' '.join(blocked)
            if sessions:
                sentence += ' Open sessions do not offer its tools.'
            return sentence
        if withheld:
            return 'Saved in config.toml. ' + ' '.join(withheld)
        if sessions:
            return 'Saved in config.toml. Open sessions and a new session use these plugins.'
        return 'Saved in config.toml. No session is open. A new session will use these plugins.'

    def _refuse_aethermind_write(self):
        raise ValueError(
            'AetherMind is read-only on this page. It shows Embedded or External from the core status report. '
            'The service URL, endpoints, project, and packaged engine path are under Integrations.')
