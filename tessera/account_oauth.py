"""Tessera-owned account sign-in for xAI and OpenAI.

The protocol matches the public device-code flows those accounts use.
This module speaks HTTP itself. It does not call another program, and it
does not read another program's auth file. Tokens stay in this home.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlencode, urlsplit


XAI_CLIENT_ID = 'b1a00492-073a-47ea-816f-4c329264a828'
XAI_SCOPE = 'openid profile email offline_access grok-cli:access api:access'
XAI_DEVICE_URL = 'https://auth.x.ai/oauth2/device/code'
XAI_DISCOVERY_URL = 'https://auth.x.ai/.well-known/openid-configuration'
XAI_BASE_URL = 'https://api.x.ai/v1'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'

CODEX_CLIENT_ID = 'app_EMoamEEZ73f0CkXaXp7hrann'
CODEX_ISSUER = 'https://auth.openai.com'
CODEX_TOKEN_URL = 'https://auth.openai.com/oauth/token'
CODEX_USER_AGENT = 'codex-cli'

SIGN_IN_HOSTS = {'auth.x.ai', 'accounts.x.ai', 'auth.openai.com'}
PENDING = {'authorization_pending', 'slow_down'}


class OAuthError(RuntimeError):
    pass


def grant_path(home, provider):
    return Path(home) / 'oauth' / (provider + '.json')


def grant_report(home, provider, now=None):
    """Describe a saved grant without returning the tokens."""
    if now is None:
        now = time.time()
    path = grant_path(home, provider)
    if not path.is_file():
        return {'state': 'missing', 'has_refresh': False, 'expires_at': 0, 'status': 'Not signed in'}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {'state': 'missing', 'has_refresh': False, 'expires_at': 0, 'status': 'Not signed in'}
    if not isinstance(payload, dict):
        return {'state': 'missing', 'has_refresh': False, 'expires_at': 0, 'status': 'Not signed in'}
    has_access = bool(str(payload.get('access_token') or '').strip())
    has_refresh = bool(str(payload.get('refresh_token') or '').strip())
    expires_at = payload.get('expires_at') or 0
    if type(expires_at) is not int:
        expires_at = 0
    if not has_access and not has_refresh:
        return {'state': 'missing', 'has_refresh': False, 'expires_at': 0, 'status': 'Not signed in'}
    expired = expires_at > 0 and expires_at <= int(now)
    if expired or not has_access:
        state = 'expired'
        status = 'Access expired. Refresh or sign in again.'
    else:
        state = 'current'
        if expires_at > 0:
            when = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(expires_at))
            status = 'Signed in until ' + when
        else:
            status = 'Signed in'
    return {'state': state, 'has_refresh': has_refresh, 'expires_at': expires_at, 'status': status}


def signed_in(home, provider):
    return grant_report(home, provider)['state'] == 'current'


def save_grant(home, provider, tokens):
    path = grant_path(home, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(tokens), encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def load_grant(home, provider):
    path = grant_path(home, provider)
    if not path.is_file():
        raise OAuthError('Not signed in. Use Sign in on the settings page.')
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        raise OAuthError('The saved sign-in could not be read. Sign in again.') from None
    if not isinstance(payload, dict) or not str(payload.get('access_token') or '').strip():
        raise OAuthError('Not signed in. Use Sign in on the settings page.')
    return payload


def open_sign_in_page(url, runner=None):
    parsed = urlsplit(url or '')
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or host not in SIGN_IN_HOSTS:
        raise OAuthError('That address is not a sign-in page')
    if runner is None:
        _open_url(url)
        return
    runner(url)


def _open_url(url):
    if sys.platform == 'win32':
        try:
            os.startfile(url)  # noqa: Windows-only; absent on other platforms
        except OSError as error:
            raise OAuthError(f'The sign-in page could not be opened ({type(error).__name__}).') from None
        return
    command = '/usr/bin/open' if sys.platform == 'darwin' else 'xdg-open'
    subprocess.run([command, url], check=False)


def _form(fields):
    return urlencode(fields).encode('utf-8')


def _json_body(fields):
    return json.dumps(fields).encode('utf-8')


def _urllib_post(url, body, headers):
    req = request.Request(url, data=body, headers=headers, method='POST')
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read()
            status = response.status
    except error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if not raw:
        return status, {}
    try:
        payload = json.loads(raw.decode('utf-8'))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return status, payload


def _urllib_get(url, headers):
    req = request.Request(url, headers=headers, method='GET')
    with request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode('utf-8'))
    if not isinstance(payload, dict):
        raise OAuthError('Sign-in discovery did not return an object')
    return payload


def _post(url, body, headers, poster):
    status, payload = poster(url, body, headers)
    return status, payload


def _xai_token_endpoint(getter):
    discovered = getter(XAI_DISCOVERY_URL, {'Accept': 'application/json'})
    endpoint = str(discovered.get('token_endpoint') or '')
    if not endpoint.startswith('https://auth.x.ai/'):
        raise OAuthError('xAI discovery did not return its own token endpoint')
    return endpoint


def _write_status(path, status, url='', code='', message=''):
    payload = {
        'status': status,
        'verification_url': url,
        'user_code': code,
        'message': message,
    }
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(payload), encoding='utf-8')
    os.replace(temporary, path)


def _save_tokens(home, provider, payload, refresh_fallback, token_endpoint):
    access = str(payload.get('access_token') or '').strip()
    refresh = str(payload.get('refresh_token') or refresh_fallback or '').strip()
    if not access or not refresh:
        raise OAuthError('Sign-in did not return a usable grant')
    expires_in = payload.get('expires_in')
    expires_at = 0
    if isinstance(expires_in, int) and expires_in > 0:
        expires_at = int(time.time()) + expires_in
    save_grant(home, provider, {
        'access_token': access,
        'refresh_token': refresh,
        'expires_at': expires_at,
        'token_endpoint': token_endpoint,
    })


def sign_in_xai(home, status_path, poster=None, getter=None, sleep=time.sleep, now=time.time, opener=None):
    poster = poster or _urllib_post
    getter = getter or _urllib_get
    status, device = _post(
        XAI_DEVICE_URL,
        _form({'client_id': XAI_CLIENT_ID, 'scope': XAI_SCOPE}),
        {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
        poster,
    )
    if status != 200 or not device.get('user_code') or not device.get('device_code'):
        raise OAuthError('xAI did not issue a sign-in code')
    url = str(device.get('verification_uri_complete') or device.get('verification_uri') or '')
    code = str(device['user_code'])
    _write_status(status_path, 'waiting', url, code, 'Approve this code in the browser.')
    if opener is not False:
        open_sign_in_page(url, runner=opener)
    endpoint = _xai_token_endpoint(getter)
    interval = max(1, int(device.get('interval') or 5))
    deadline = now() + int(device.get('expires_in') or 600)
    while now() < deadline:
        sleep(interval)
        token_status, payload = _post(
            endpoint,
            _form({
                'grant_type': DEVICE_GRANT,
                'client_id': XAI_CLIENT_ID,
                'device_code': str(device['device_code']),
            }),
            {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            poster,
        )
        if token_status == 200 and payload.get('access_token'):
            _save_tokens(home, 'xai', payload, '', endpoint)
            _write_status(status_path, 'signed_in', url, code, 'Signed in.')
            return
        oauth_error = str(payload.get('error') or '')
        if oauth_error == 'slow_down':
            interval += 5
            continue
        if oauth_error in PENDING:
            continue
        if oauth_error:
            raise OAuthError('xAI sign-in was declined or expired')
        raise OAuthError('xAI sign-in failed')
    raise OAuthError('xAI sign-in timed out')


def _openai_headers():
    return {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'User-Agent': CODEX_USER_AGENT,
    }


def _openai_failure(status, payload, fallback):
    detail = fallback
    if isinstance(payload, dict):
        err = payload.get('error')
        if isinstance(err, dict) and err.get('message'):
            detail = str(err.get('message'))[:180]
        elif err:
            detail = str(err)[:180]
    return OAuthError('OpenAI sign-in failed (HTTP ' + str(status) + '). ' + detail)


def sign_in_openai(home, status_path, poster=None, sleep=time.sleep, now=time.time, opener=None):
    poster = poster or _urllib_post
    status = 0
    device = {}
    for attempt in range(4):
        status, device = _post(
            CODEX_ISSUER + '/api/accounts/deviceauth/usercode',
            _json_body({'client_id': CODEX_CLIENT_ID}),
            _openai_headers(),
            poster,
        )
        if status != 429:
            break
        sleep(min(2 ** (attempt + 1), 8))
    if status != 200 or not device.get('user_code') or not device.get('device_auth_id'):
        raise _openai_failure(status, device, 'OpenAI did not issue a sign-in code')
    url = CODEX_ISSUER + '/codex/device'
    code = str(device['user_code'])
    _write_status(status_path, 'waiting', url, code, 'Open the page and enter this code.')
    if opener is not False:
        open_sign_in_page(url, runner=opener)
    interval = max(3, int(device.get('interval') or 5))
    deadline = now() + 15 * 60
    exchanged = None
    while now() < deadline:
        sleep(interval)
        poll_status, payload = _post(
            CODEX_ISSUER + '/api/accounts/deviceauth/token',
            _json_body({'device_auth_id': device['device_auth_id'], 'user_code': code}),
            _openai_headers(),
            poster,
        )
        if poll_status == 200 and payload.get('authorization_code'):
            exchanged = payload
            break
        if poll_status in (403, 404):
            continue
        raise OAuthError('OpenAI sign-in failed')
    if exchanged is None:
        raise OAuthError('OpenAI sign-in timed out')
    token_status, tokens = _post(
        CODEX_TOKEN_URL,
        _form({
            'grant_type': 'authorization_code',
            'code': exchanged.get('authorization_code') or '',
            'redirect_uri': CODEX_ISSUER + '/deviceauth/callback',
            'client_id': CODEX_CLIENT_ID,
            'code_verifier': exchanged.get('code_verifier') or '',
        }),
        {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'User-Agent': CODEX_USER_AGENT},
        poster,
    )
    if token_status != 200:
        raise _openai_failure(token_status, tokens, 'OpenAI did not return a usable grant')
    _save_tokens(home, 'openai-codex', tokens, '', CODEX_TOKEN_URL)
    _write_status(status_path, 'signed_in', url, code, 'Signed in.')


def refresh(home, provider, poster=None, now=time.time):
    poster = poster or _urllib_post
    grant = load_grant(home, provider)
    expires_at = grant.get('expires_at') or 0
    if isinstance(expires_at, int) and expires_at > int(now()) + 120:
        return grant['access_token']
    refresh_token = str(grant.get('refresh_token') or '')
    if provider == 'xai':
        endpoint = str(grant.get('token_endpoint') or '')
        if not endpoint.startswith('https://auth.x.ai/'):
            raise OAuthError('Saved xAI sign-in has no token endpoint. Sign in again.')
        status, payload = _post(
            endpoint,
            _form({
                'grant_type': 'refresh_token',
                'client_id': XAI_CLIENT_ID,
                'refresh_token': refresh_token,
            }),
            {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
            poster,
        )
    elif provider == 'openai-codex':
        status, payload = _post(
            CODEX_TOKEN_URL,
            _form({
                'grant_type': 'refresh_token',
                'client_id': CODEX_CLIENT_ID,
                'refresh_token': refresh_token,
            }),
            {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'User-Agent': CODEX_USER_AGENT},
            poster,
        )
    else:
        raise OAuthError('Unknown account')
    if status != 200:
        raise OAuthError('The saved sign-in expired. Sign in again.')
    _save_tokens(home, provider, payload, refresh_token, str(grant.get('token_endpoint') or ''))
    return str(payload.get('access_token') or '')


def access_token(home, provider, poster=None):
    if provider not in ('xai', 'openai-codex'):
        raise OAuthError('Unknown account')
    if not home:
        raise OAuthError('Not signed in. Use Sign in on the settings page.')
    return refresh(home, provider, poster=poster)
