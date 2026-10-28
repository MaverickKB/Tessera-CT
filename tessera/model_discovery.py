"""Ask a provider which models it actually serves, instead of guessing names.

Auto-discovery is the missing half of the Models section: without it a person
has to type a model name they got from somewhere else, and a typo only shows up
as a failed turn. This module asks the endpoint it is
configured with for its own model list and returns plain names.

Two entry points, because the two questions are different:

- `discover(config, name)` asks about a backend already in config.
- `discover_unsaved(config, base_url, kind, credential, api_key_env)` asks about
  an endpoint being typed into the Add-model form, before it is saved. That is
  the case that matters most: you want to know what is there *before* you name
  a model.

Credentials are resolved through the same CredentialResolver the transport
uses, so an OAuth grant, keychain entry or env var behaves identically here and
in a real turn. Nothing is sent anywhere except the configured base URL.
"""

from __future__ import annotations

import json
from urllib import error, request
from urllib.parse import quote, urlsplit

from .credentials import CredentialError, CredentialResolver
from .codex_headers import codex_headers, official_codex_url
from .settings_model import OAUTH_MODELS

# Providers that speak a model-list dialect we can read. A CLI worker has no
# list endpoint at all -- it is a local process, not a service -- so it is
# reported as such rather than probed.
HTTP_KINDS = ('openai_chat', 'openai_responses', 'anthropic_messages')

# A list request is cheap and a person is waiting on the button, so it does not
# inherit a long generation timeout.
DISCOVERY_TIMEOUT = 20.0

LIST_PATHS = {
    'openai_chat': '/models',
    'openai_responses': '/models',
    # Anthropic's list endpoint lives at the same path and needs the version header.
    'anthropic_messages': '/models',
}


class DiscoveryError(Exception):
    """A discovery attempt that could not produce a list, with a plain reason."""

    def __init__(self, message, category='provider'):
        super().__init__(message)
        self.category = category


def _headers(kind, secret, base_url):
    headers = {'Accept': 'application/json'}
    if kind == 'anthropic_messages':
        headers['anthropic-version'] = '2023-06-01'
        if secret:
            headers['x-api-key'] = secret
    elif secret:
        headers['Authorization'] = 'Bearer ' + secret
    if official_codex_url(base_url):
        headers.update(codex_headers(secret))
    return headers


def _read_names(kind, payload):
    """Pull model names out of the provider's own answer, whatever shape it uses."""
    if isinstance(payload, dict):
        if isinstance(payload.get('data'), list):
            items = payload['data']
        elif isinstance(payload.get('models'), list):
            items = payload['models']
        elif isinstance(payload.get('data'), dict) and isinstance(payload['data'].get('models'), list):
            items = payload['data']['models']
        else:
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    names = []
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for key in ('slug', 'id', 'name', 'model'):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
                    break
    # Stable order, no duplicates: two entries naming the same model are one model.
    seen = set()
    unique = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return sorted(unique)


def _fetch(base_url, kind, secret, api_key_env, timeout):
    if kind not in HTTP_KINDS:
        raise DiscoveryError(
            'A CLI worker has no model list; it runs a local program and reports '
            'models itself. Set the model in its arguments instead.', 'configuration')
    parsed = urlsplit(base_url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
        raise DiscoveryError('Base URL must be an http or https address without credentials',
                             'configuration')
    path = LIST_PATHS[kind]
    url = base_url.rstrip('/') + path
    headers = _headers(kind, secret, base_url)
    req = request.Request(url, headers=headers, method='GET')
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            raise DiscoveryError('The provider refused the credential for this endpoint '
                                 '(HTTP %d)' % code, 'authentication') from None
        if code == 404:
            raise DiscoveryError('This endpoint does not publish a model list (HTTP 404). '
                                 'Type the model name instead.', 'configuration') from None
        raise DiscoveryError('The endpoint answered HTTP %d' % code, 'provider') from None
    except error.URLError as exc:
        raise DiscoveryError('Could not reach %s (%s)' % (url, exc.reason), 'connection') from None
    except (TimeoutError, OSError) as exc:
        raise DiscoveryError('Could not reach %s (%s)' % (url, exc), 'connection') from None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise DiscoveryError('The endpoint answered with something that is not JSON. '
                             'Check the base URL and protocol.', 'provider') from None
    names = _read_names(kind, payload)
    if not names:
        raise DiscoveryError('The endpoint answered but named no models.', 'provider')
    return names


def discover_unsaved(config, base_url, kind, credential='', api_key_env=''):
    """Ask an endpoint being typed into the form, before it is saved."""
    resolver = CredentialResolver(config.credentials, oauth_home=str(config.home))
    secret = ''
    try:
        secret = resolver.resolve(credential, env=api_key_env).value
    except CredentialError as exc:
        raise DiscoveryError(str(exc), 'authentication') from None
    return _fetch(base_url, kind, secret, api_key_env, DISCOVERY_TIMEOUT)


def _codex_slugs(payload):
    """Visible Codex catalog slugs. supported_in_api is not a reason to drop one."""
    if isinstance(payload, dict):
        entries = payload.get('models') if isinstance(payload.get('models'), list) else payload.get('data')
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = None
    if not isinstance(entries, list):
        entries = []
    ranked = []
    for item in entries:
        if isinstance(item, str) and item.strip():
            ranked.append((10000, item.strip()))
            continue
        if not isinstance(item, dict):
            continue
        visibility = item.get('visibility')
        if isinstance(visibility, str) and visibility.strip().lower() in ('hide', 'hidden'):
            continue
        slug = item.get('slug') or item.get('id') or item.get('name')
        if not isinstance(slug, str) or not slug.strip():
            continue
        priority = item.get('priority')
        rank = int(priority) if isinstance(priority, (int, float)) and not isinstance(priority, bool) else 10000
        ranked.append((rank, slug.strip()))
    ranked.sort()
    seen = set()
    names = []
    for _rank, slug in ranked:
        if slug not in seen:
            seen.add(slug)
            names.append(slug)
    return names


def _fetch_url(url, secret, timeout, *, parse, codex=False):
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
        raise DiscoveryError('Model list URL must be an http or https address without credentials',
                             'configuration')
    headers = _headers('openai_responses', secret, url)
    if codex:
        headers.update(codex_headers(secret))
    req = request.Request(url, headers=headers, method='GET')
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except error.HTTPError as exc:
        code = exc.code
        if code in (401, 403):
            raise DiscoveryError('The provider refused the credential for this endpoint '
                                 '(HTTP %d)' % code, 'authentication') from None
        if code == 404:
            raise DiscoveryError('This endpoint does not publish a model list (HTTP 404). '
                                 'Type the model name instead.', 'configuration') from None
        raise DiscoveryError('The endpoint answered HTTP %d' % code, 'provider') from None
    except error.URLError as exc:
        raise DiscoveryError('Could not reach %s (%s)' % (url, exc.reason), 'connection') from None
    except (TimeoutError, OSError) as exc:
        raise DiscoveryError('Could not reach %s (%s)' % (url, exc), 'connection') from None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise DiscoveryError('The endpoint answered with something that is not JSON. '
                             'Check the base URL and protocol.', 'provider') from None
    names = parse(payload)
    if not names:
        raise DiscoveryError('The endpoint answered but named no models. Type the model id instead.',
                             'provider')
    return names


def _signin_token(config, provider):
    """The grant /api/oauth stores. A missing credential row is not required."""
    from .account_oauth import OAuthError, access_token
    try:
        return access_token(str(config.home), provider)
    except OAuthError as exc:
        raise DiscoveryError(str(exc), 'authentication') from None


def codex_request_url(config):
    """The catalog request, from the two settings. No network."""
    catalog = getattr(config, 'codex', None)
    endpoint = (getattr(catalog, 'models_url', '') or '').strip()
    version = (getattr(catalog, 'client_version', '') or '').strip()
    if not endpoint:
        raise DiscoveryError('Codex model list URL is not set', 'configuration')
    if not version or any(char in version for char in ' \t\r\n&#'):
        raise DiscoveryError('Codex client version must be a version name', 'configuration')
    join = '&' if '?' in endpoint else '?'
    return endpoint + join + 'client_version=' + quote(version, safe='')


def discover_codex_models(config):
    """List Codex models with the stored sign-in token.

    The catalog URL and client version are the settings values. A failure names
    the error and leaves the model id typeable.
    """
    url = codex_request_url(config)
    token = _signin_token(config, 'openai-codex')
    return _fetch_url(url, token, DISCOVERY_TIMEOUT, parse=_codex_slugs, codex=True)


def discover_account(config, account):
    """Ask a sign-in provider, using the stored grant rather than a model row."""
    spec = OAUTH_MODELS.get(account)
    if spec is None:
        raise DiscoveryError('Unknown sign-in provider', 'configuration')
    if account == 'openai-codex':
        return discover_codex_models(config)
    token = _signin_token(config, spec['oauth']) if spec.get('oauth') else ''
    return _fetch(spec['base_url'], spec['kind'], token, '', DISCOVERY_TIMEOUT)


def discover(config, name):
    """Ask about a backend already in config, using its stored wiring."""
    backend = config.backends.get(name)
    if backend is None:
        raise DiscoveryError('No such backend', 'configuration')
    kind = backend.provider_kind or 'openai_chat'
    reference = backend.credential_ref
    base_url = backend.base_url
    api_key_env = backend.api_key_env
    if backend.account and backend.account in config.accounts:
        account = config.accounts[backend.account]
        kind = account.provider_kind
        reference = account.credential_ref
    resolver = CredentialResolver(config.credentials, oauth_home=str(config.home))
    secret = ''
    try:
        secret = resolver.resolve(reference, env=api_key_env).value
    except CredentialError as exc:
        raise DiscoveryError(str(exc), 'authentication') from None
    # A list request is cheap; a person is waiting on the button. Do not inherit a
    # long generation timeout here.
    return _fetch(base_url, kind, secret, api_key_env, DISCOVERY_TIMEOUT)
