"""Read stored Codex OAuth expiry without verifying signatures or refreshing grants.

An ``ok`` result means only that the stored access expiry is in the future.
It does not prove that the provider will accept the grant.
"""

import base64
import binascii
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time


def _jwt_exp(token):
    if not isinstance(token, str):
        return None
    parts = token.split('.')
    if len(parts) != 3 or not all(parts):
        return None
    try:
        payload = parts[1] + '=' * (-len(parts[1]) % 4)
        decoded = base64.b64decode(payload, altchars=b'-_', validate=True)
        claims = json.loads(decoded)
        if not isinstance(claims, dict):
            return None
        expiry = claims.get('exp')
        if type(expiry) not in (int, float):
            return None
        expiry = float(expiry)
        if not math.isfinite(expiry):
            return None
        # Ensure the expiry can also be represented in the diagnostic date.
        datetime.fromtimestamp(expiry, timezone.utc)
        return expiry
    except (ValueError, TypeError, OverflowError, OSError, binascii.Error):
        return None


def grant_state(auth_path=None, *, now=None) -> dict:
    """Read one auth file and return expiry metadata, never credential values.

    ``now`` is a Unix timestamp in seconds. Malformed/missing access JWT expiry
    is ``no_oauth``; file/JSON errors are ``unreadable``. No exceptions escape
    for invalid file contents, paths or timestamps.
    """
    result = {'state': 'unreadable', 'access_exp': None, 'id_exp': None,
              'last_refresh': None, 'detail': 'Codex auth file is unreadable or invalid JSON.'}
    try:
        path = Path.home() / '.codex' / 'auth.json' if auth_path is None else Path(auth_path)
        auth = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        result.update(state='missing', detail='Codex auth file is missing.')
        return result
    except (OSError, ValueError, TypeError, RuntimeError):
        return result
    if not isinstance(auth, dict):
        return result
    last_refresh = auth.get('last_refresh')
    if isinstance(last_refresh, str):
        result['last_refresh'] = last_refresh
    tokens = auth.get('tokens')
    if not isinstance(tokens, dict):
        tokens = {}
    access_exp = _jwt_exp(tokens.get('access_token'))
    result['access_exp'] = access_exp
    result['id_exp'] = _jwt_exp(tokens.get('id_token'))
    if access_exp is None:
        result.update(state='no_oauth', detail='No usable Codex OAuth access JWT expiry; API-key auth is not checked.')
        return result
    try:
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError('Non-finite timestamp')
    except (ValueError, TypeError, OverflowError):
        result['detail'] = 'Codex grant check requires a finite Unix timestamp.'
        return result
    expiry_date = datetime.fromtimestamp(access_exp, timezone.utc).isoformat().replace('+00:00', 'Z')
    if access_exp <= current:
        elapsed = current - access_exp
        result.update(state='expired', detail=f'Codex OAuth access grant expired at {expiry_date} ({elapsed:.0f} seconds ago).')
    else:
        result.update(state='ok', detail=f'Codex OAuth access grant expires at {expiry_date}.')
    return result


def precheck(auth_path=None, *, now=None) -> tuple[bool, str]:
    """Accept only a stored OAuth access grant with a future expiry."""
    result = grant_state(auth_path, now=now)
    return result['state'] == 'ok', result['detail']
