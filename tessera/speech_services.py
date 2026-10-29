"""Speech services for Voice: duplex accounts and the provider profiles behind them.

The Voice page names accounts and models, never credentials. A duplex account is
something the app already holds: a sign-in grant, or a provider row pointed at a
duplex endpoint. The provider profile supplies the socket, the token service and
the protocol prefix, so nobody types them into the page.
"""

from urllib.parse import urlsplit
import json
import urllib.error
import urllib.request

from .account_oauth import grant_report
from .credentials import CredentialError, CredentialResolver


# Every provider that speaks a full-duplex realtime protocol Tessera can drive.
# The values are the provider's published endpoints and models, not a guess.
DUPLEX_PROVIDERS = {
    'xai': {
        'label': 'xAI Grok',
        'websocket_url': 'wss://api.x.ai/v1/realtime',
        'api_base_url': 'https://api.x.ai/v1',
        'client_secret_path': '/realtime/client_secrets',
        'client_protocol_prefix': 'xai-client-secret.',
        'client_protocol_extra': (),
        'session_shape': 'xai',
        'transcribe_model': 'grok-transcribe',
        'models': ('grok-voice-latest', 'grok-voice-3.0'),
        'default_voice': 'eve',
    },
    # OpenAI's Realtime API, bound to the Codex sign-in Tessera already holds.
    # The browser pairs two subprotocols, the session carries its audio under
    # audio.input/audio.output, and the voice lives at audio.output.voice.
    # Published models and voices come from the Realtime API reference.
    'openai-codex': {
        'label': 'OpenAI Codex',
        'websocket_url': 'wss://api.openai.com/v1/realtime',
        'api_base_url': 'https://api.openai.com/v1',
        'client_secret_path': '/realtime/client_secrets',
        'client_protocol_prefix': 'openai-insecure-api-key.',
        'client_protocol_extra': ('realtime',),
        'session_shape': 'openai',
        'transcribe_model': 'gpt-4o-transcribe',
        'models': ('gpt-realtime-2.1', 'gpt-realtime-2.1-mini', 'gpt-realtime-2',
                   'gpt-realtime-1.5', 'gpt-realtime', 'gpt-realtime-mini'),
        'voices': ('marin', 'cedar', 'alloy', 'ash', 'ballad', 'coral', 'echo',
                   'sage', 'shimmer', 'verse'),
        'default_voice': 'marin',
    },
}

# The credential name the Providers page uses for each sign-in.
SIGNIN_CREDENTIALS = {'xai': 'xai-oauth', 'openai-codex': 'openai-codex'}


def _host(url):
    return (urlsplit(url or '').hostname or '').lower()


def provider_for_endpoint(url):
    """The duplex provider an endpoint belongs to, or empty when none matches."""
    host = _host(url)
    if not host:
        return ''
    for provider, profile in DUPLEX_PROVIDERS.items():
        if host == _host(profile['websocket_url']):
            return provider
    return ''


def duplex_connections(config):
    """Accounts that can mint a duplex voice session, as the page names them."""
    connections = []
    for provider, profile in DUPLEX_PROVIDERS.items():
        report = grant_report(config.home, provider)
        if report['state'] == 'missing':
            continue
        reference = SIGNIN_CREDENTIALS.get(provider, provider + '-oauth')
        connections.append({
            'reference': reference,
            'label': profile['label'] + ' sign-in',
            'provider': provider,
            'status': report['status'],
            'connected': report['state'] == 'current',
            'models': profile['models'],
        })
    for name, backend in config.backends.items():
        provider = provider_for_endpoint(backend.base_url)
        if not provider:
            continue
        reference = backend.credential_ref
        if not reference and backend.account:
            pool = config.accounts.get(backend.account)
            if pool is not None:
                reference = pool.credential_ref
        if not reference:
            continue
        connections.append({
            'reference': reference,
            'label': name,
            'provider': provider,
            'status': 'Uses the credential configured on this model',
            'connected': True,
            'models': DUPLEX_PROVIDERS[provider]['models'],
        })
    return connections


def connection_for(config, reference):
    """The connection a credential reference belongs to, or None."""
    for connection in duplex_connections(config):
        if connection['reference'] == reference:
            return connection
    return None


class SpeechProbeError(ValueError):
    """An endpoint that could not name its models, with a plain reason."""


def _model_names(payload):
    """Model names from the shapes speech servers actually answer with."""
    if isinstance(payload, dict):
        items = payload.get('data')
        if not isinstance(items, list):
            items = payload.get('models')
        if not isinstance(items, list):
            items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    names = []
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        name = item.get('id') or item.get('name') or item.get('model')
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique


def _voice_names(payload):
    """Voice ids from the shape a duplex provider answers with."""
    items = payload.get('voices') if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        items = []
    names = []
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip().lower())
            continue
        if not isinstance(item, dict):
            continue
        name = item.get('voice_id') or item.get('id') or item.get('name')
        if isinstance(name, str) and name.strip():
            names.append(name.strip().lower())
    unique = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return unique


def probe_voices(config, connection):
    """Ask a duplex provider which voices its speech-to-speech API offers.

    The account's own credential is resolved here, so an OAuth sign-in works the
    same as a provider row. The answer becomes the voice dropdown, never a guess.
    """
    if not isinstance(connection, dict):
        raise SpeechProbeError('That account does not name a duplex provider.')
    provider = str(connection.get('provider') or '')
    profile = DUPLEX_PROVIDERS.get(provider)
    if profile is None:
        raise SpeechProbeError('That account does not name a duplex provider.')
    # A provider with a published roster answers from that roster. OpenAI has no
    # voice-list endpoint; its Realtime API reference names the supported voices.
    published = profile.get('voices')
    if published:
        return list(published)
    reference = connection.get('reference') or ''
    try:
        token = CredentialResolver(config.credentials, oauth_home=str(config.home)).resolve(reference).value
    except CredentialError as error:
        raise SpeechProbeError('The account credential is not available (%s).' % error) from None
    if not token:
        raise SpeechProbeError('The account credential is not available.')
    url = profile['api_base_url'].rstrip('/') + '/tts/voices'
    ask = urllib.request.Request(url, headers={'Accept': 'application/json', 'Authorization': 'Bearer ' + token}, method='GET')
    try:
        with urllib.request.urlopen(ask, timeout=config.voice.timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        raise SpeechProbeError('The provider answered HTTP %d for its voice list.' % error.code) from None
    except urllib.error.URLError as error:
        raise SpeechProbeError('Could not reach the provider (%s).' % error.reason) from None
    except (TimeoutError, OSError) as error:
        raise SpeechProbeError('Could not reach the provider (%s).' % error) from None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise SpeechProbeError('The provider answered with something that is not JSON.') from None
    names = _voice_names(payload)
    if not names:
        raise SpeechProbeError('The provider answered but named no voices.')
    return names


def probe_models(base_url, timeout):
    """Ask a local speech endpoint which models it serves. No credential is sent."""
    parsed = urlsplit(base_url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
        raise SpeechProbeError('The endpoint must be an http or https address without credentials.')
    url = base_url.rstrip('/') + '/models'
    ask = urllib.request.Request(url, headers={'Accept': 'application/json'}, method='GET')
    try:
        with urllib.request.urlopen(ask, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        raise SpeechProbeError('The endpoint answered HTTP %d for its model list.' % error.code) from None
    except urllib.error.URLError as error:
        raise SpeechProbeError('Could not reach the endpoint (%s).' % error.reason) from None
    except (TimeoutError, OSError) as error:
        raise SpeechProbeError('Could not reach the endpoint (%s).' % error) from None
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise SpeechProbeError('The endpoint answered with something that is not JSON.') from None
    names = _model_names(payload)
    if not names:
        raise SpeechProbeError('The endpoint answered but named no models.')
    return names
