"""Passive account/provider/model relationships for Settings. No secret resolution."""

import os
from urllib.parse import urlsplit

from .account_oauth import grant_report
from .agent_selection import LEVELS, backend_catalog, select_backend


def credential_kind(config, name):
    seen = set()
    while name and name not in seen:
        seen.add(name)
        reference = config.credentials.get(name)
        if reference is None:
            return 'unknown'
        if reference.oauth:
            return reference.oauth
        if reference.env or reference.keychain_service:
            return 'api_key'
        name = reference.config_ref
    return 'unknown'


def credential_fits(config, name, kind, endpoint):
    if not name:
        return True
    source = credential_kind(config, name)
    if kind == 'cli_worker':
        return False
    host = (urlsplit(endpoint).hostname or '').lower()
    if source == 'xai':
        return kind == 'openai_chat' and host == 'api.x.ai'
    if source == 'openai-codex':
        return (kind == 'openai_responses' and host == 'chatgpt.com'
                and urlsplit(endpoint).path.rstrip('/') == '/backend-api/codex')
    # Generic key references carry no issuer metadata. Protocol compatibility
    # is all that can be established without sending a credential.
    return source == 'api_key' and kind in (
        'openai_chat', 'openai_responses', 'anthropic_messages')


def ladder(config):
    accounts = {}
    providers = []
    for name in config.credentials:
        kind = credential_kind(config, name)
        report = grant_report(config.home, kind) if kind in ('xai', 'openai-codex') else None
        accounts['credential:' + name] = {
            'id': 'credential:' + name, 'label': name, 'kind': kind,
            'status': report['status'] if report else 'Reference configured; validity not checked',
            'state': report['state'] if report else 'unknown', 'providers': [], 'limits': [],
        }
    for name, backend in config.backends.items():
        pool = config.accounts.get(backend.account)
        kind = pool.provider_kind if pool else backend.provider_kind or 'openai_chat'
        reference = pool.credential_ref if pool else backend.credential_ref
        cli = (backend.extra or {}).get('cli') or {}
        if kind == 'cli_worker':
            # A pool is not proof of a login identity. Keep the configured CLI
            # context explicit; do not assert two programs share a grant.
            account_id = 'cli:' + (backend.account or name)
            label = (backend.account or name) + ' CLI login'
            account_kind = 'cli_worker'
            status = 'CLI-owned login; validity not checked'
        elif backend.api_key_env:
            account_id = 'env:' + backend.api_key_env
            label = backend.api_key_env
            account_kind = 'environment'
            status = 'Environment variable is used when set, unless the credential is a sign-in; validity not checked'
        elif reference:
            account_id = 'credential:' + reference
            label, account_kind, status = reference, credential_kind(config, reference), 'Unknown reference'
        else:
            account_id = 'none:' + name
            label, account_kind, status = 'No credential · ' + name, 'none', 'No credential configured'
        account = accounts.setdefault(account_id, {
            'id': account_id, 'label': label, 'kind': account_kind, 'status': status,
            'state': 'unknown', 'providers': [], 'limits': [],
        })
        account['providers'].append(name)
        if pool and not any(item['pool'] == backend.account for item in account['limits']):
            account['limits'].append({'pool': backend.account, **pool.limits})
        providers.append({
            'id': name, 'account': account_id, 'kind': kind,
            'endpoint': cli.get('command', '') if kind == 'cli_worker' else backend.base_url,
            'models': [backend.model] if backend.model else [],
            'model_source': 'configured; availability not probed',
            'enabled': backend.enabled,
            'source': credential_source(config, name),
            'compatible_accounts': ['credential:' + key for key in config.credentials
                                    if credential_fits(config, key, kind, backend.base_url)],
        })
    workers = backend_catalog(config)
    roles = []
    for role in ('main', 'failover'):
        selected = config.roles.get(role, '')
        provider = next((p for p in providers if p['id'] == selected), None)
        account = accounts.get(provider['account']) if provider else None
        warning = account['status'] if account and account['state'] in ('expired', 'missing') else ''
        roles.append({'id': role, 'provider': selected, 'warning': warning})
    for tier in LEVELS:
        try:
            selected = select_backend(config, capability=tier) or ''
        except ValueError:
            selected = ''
        roles.append({'id': 'worker:' + tier, 'provider': selected,
                      'eligible': [row['backend'] for row in workers
                                   if row['enabled']
                                   and LEVELS[row['capability']] >= LEVELS[tier]]})
    for role, settings, endpoint in (('judge', config.jev, config.jev.url),
                                      ('voice', config.voice, config.voice.websocket_url)):
        account = accounts.get('credential:' + settings.credential_ref)
        warning = account['status'] if account and account['state'] in ('expired', 'missing') else ''
        roles.append({'id': role, 'model': settings.model, 'endpoint': endpoint,
                      'account': 'credential:' + settings.credential_ref if settings.credential_ref else '',
                      'enabled': settings.enabled, 'warning': warning, 'configuration_section': role})
    return {'accounts': list(accounts.values()), 'providers': providers,
            'roles': roles, 'worker_backends': workers,
            'note': 'Models are configured IDs, not a verified service inventory. Generic keys have no issuer metadata; compatibility does not establish authorization.'}


def _walk_credential(config, reference, environ):
    first = reference
    seen = set()
    while reference:
        if reference in seen:
            return {'source': 'unavailable', 'label': 'Credential reference cycle'}
        seen.add(reference)
        settings = config.credentials.get(reference)
        if settings is None:
            return {'source': 'unavailable', 'label': 'Credential ' + reference + ' is not configured'}
        oauth = getattr(settings, 'oauth', '') or ''
        if oauth:
            report = grant_report(config.home, oauth)
            if oauth == 'xai':
                who = 'xAI Grok sign-in'
            elif oauth == 'openai-codex':
                who = 'ChatGPT or Codex sign-in'
            else:
                who = 'Sign-in'
            return {'source': 'oauth', 'label': who + ' · ' + report['status']}
        env = getattr(settings, 'env', '') or ''
        if env and str(environ.get(env, '')).strip():
            return {'source': 'env', 'label': 'Env var ' + env + ' via credential ' + reference}
        service = getattr(settings, 'keychain_service', '') or ''
        account = getattr(settings, 'keychain_account', '') or ''
        if service:
            label = ('Keychain item ' + service + ' / ' + account + ' via credential ' + reference
                     + ' (not read here)')
            fallback = getattr(settings, 'config_ref', '') or ''
            if fallback:
                label += '; if missing, falls back to ' + fallback
            return {'source': 'keychain', 'label': label}
        reference = getattr(settings, 'config_ref', '') or ''
    return {'source': 'unavailable', 'label': 'Credential ' + first + ' has no available source'}


def binding_source(config, reference='', api_key_env='', *, oauth_clears_env=True, environ=None):
    """Name the source a resolve() call would use. Never a secret value."""
    environ = os.environ if environ is None else environ
    env_name = api_key_env or ''
    cred = config.credentials.get(reference) if reference else None
    if oauth_clears_env and cred is not None and getattr(cred, 'oauth', ''):
        env_name = ''
    if env_name and str(environ.get(env_name, '')).strip():
        label = 'Env var ' + env_name
        if reference:
            label += ' (overrides credential ' + reference + ')'
        return {'source': 'env', 'label': label}
    if not reference:
        if env_name:
            return {'source': 'unavailable', 'label': 'Env var ' + env_name + ' is not set'}
        return {'source': 'none', 'label': 'No credential'}
    return _walk_credential(config, reference, environ)


def credential_source(config, name, environ=None):
    """The source a request to backend `name` would use. Names only, never values."""
    backend = config.backends.get(name)
    if backend is None:
        return {'source': 'unavailable', 'label': 'No such model'}
    pool = config.accounts.get(backend.account) if backend.account else None
    kind = pool.provider_kind if pool else (backend.provider_kind or 'openai_chat')
    reference = pool.credential_ref if pool else backend.credential_ref
    if kind == 'cli_worker':
        cli = (backend.extra or {}).get('cli') or {}
        vendor = cli.get('vendor') if isinstance(cli.get('vendor'), str) and cli.get('vendor') else 'CLI'
        label = vendor + ' CLI login (held by the CLI; not checked)'
        if cli.get('allow_api_key_fallback') is True:
            label += ' · API key fallback'
        return {'source': 'cli', 'label': label}
    return binding_source(config, reference, backend.api_key_env or '', environ=environ)


def no_credential(config, name):
    """True when this backend names no credential, API-key variable, or vendor CLI."""
    return credential_source(config, name)['source'] == 'none'
