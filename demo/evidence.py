"""Publication redaction for demo evidence; never resolve credentials or probe hosts."""

import html
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote


REDACTED = '[REDACTED]'
URL = re.compile(r'''(?i)(?:\b[a-z][a-z0-9+.-]*:[\\/]+|[\\/]{2})[^\s<>"']+''')
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?![\w.])')
IPV6 = re.compile(r'(?<!\w)\[?([0-9a-fA-F]*(?::[0-9a-fA-F.]*){2,}(?:%[\w.-]+)?)\]?(?::\d+)?')
HOST = re.compile(r'(?i)(?<![\w./-])(?:[\w.+%-]+@)?(?:[\w-]+\.)+[^\W\d_][\w-]*\.?(?::\d+)?')
HOST_PORT = re.compile(r'(?<![\w:/.-])[a-zA-Z][\w-]*:\d+(?!\d)')
PATH = re.compile(r'''(?<![\w:/>])/(?!/)[^\s<>"']+|\b[A-Za-z]:[\\/][^\s<>"']+''')
AUTH = re.compile(r'''(?i)\b(?:Bearer|Basic)\s+[^\s<>"']+''')
ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2}))')
PRIVATE_FIELDS = frozenset({
    'account', 'address', 'api_key', 'api_key_env', 'authorization', 'backend',
    'client_secret', 'config_ref', 'credential_ref', 'cwd', 'endpoint', 'home',
    'host', 'hostname', 'job_id', 'keychain_account', 'keychain_service', 'model',
    'model_id', 'path', 'personal_project', 'port', 'project', 'project_id',
    'project_root', 'refresh_token', 'requested_model', 'secret', 'session_id',
    'token', 'token_env', 'url', 'access_token',
})


def _unescape(match):
    digits = match.group(1) or match.group(2)
    number = int(digits, 16)
    if 0xD800 <= number <= 0xDFFF:
        return match.group()
    return chr(number)


def _plain_text(text):
    # Decode common transport wrappers before looking for private locations.
    # Deeply wrapped strings fail closed instead of hiding another encoded host.
    for _ in range(8):
        decoded = unquote(text)
        decoded = html.unescape(decoded)
        decoded = decoded.replace('\\/', '/')
        decoded = ESCAPE.sub(_unescape, decoded)
        if decoded == text:
            return text
        text = decoded
    return REDACTED


def _redact_ipv6(match):
    address = match.group(1).split('%', 1)[0]
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return match.group()
    return REDACTED


def redact_text(text, replacements=()):
    """Strip all network locators, absolute paths and explicit caller secrets.

    Pass additional private strings as (value, public marker) pairs. No env,
    keychain, filesystem or network discovery happens here. Domain-like filenames
    may also be removed; public evidence favors redaction over retaining a label.
    """
    if not isinstance(text, str):
        raise ValueError('Evidence text must be a string')
    selected = []
    for private, marker in replacements:
        if not isinstance(private, (str, Path)) or not isinstance(marker, str):
            raise ValueError('Evidence replacements must be strings or paths with public string markers')
        if private:
            selected.append((_plain_text(str(private)), marker))
    selected.sort(key=lambda pair: len(pair[0]), reverse=True)
    text = _plain_text(text)
    for private, marker in selected:
        text = re.sub(re.escape(private), lambda match: marker, text, flags=re.IGNORECASE)
    text = URL.sub(REDACTED, text)
    text = AUTH.sub(REDACTED, text)
    text = IPV6.sub(_redact_ipv6, text)
    text = IPV4.sub(REDACTED, text)
    text = PATH.sub(REDACTED, text)
    text = HOST_PORT.sub(REDACTED, text)
    return HOST.sub(REDACTED, text)


def sanitize_evidence(value, replacements=()):
    """Copy JSON evidence, retaining observations while removing private metadata."""
    replacements = tuple(replacements)
    return _sanitize(value, replacements, 0)


def _sanitize(value, replacements, depth):
    if depth > 64:
        raise ValueError('Evidence nesting is too deep or cyclic')
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Evidence numbers must be finite')
        return value
    if isinstance(value, str):
        return redact_text(value, replacements)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('Evidence keys must be strings')
            cleaned_key = redact_text(key, replacements)
            if cleaned_key in result:
                raise ValueError('Evidence keys collide after redaction')
            field = cleaned_key.casefold()
            field = field.replace('-', '_')
            if field in PRIVATE_FIELDS or field.endswith(('_url', '_host', '_endpoint', '_path')):
                cleaned = None if item is None else REDACTED
            else:
                cleaned = _sanitize(item, replacements, depth + 1)
            result[cleaned_key] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.append(_sanitize(item, replacements, depth + 1))
        return result
    raise ValueError('Evidence must contain only JSON values')


def write_json(path, value, replacements=()):
    """Validate and redact before an atomic owner-only publication."""
    cleaned = sanitize_evidence(value, replacements)
    serialized = json.dumps(cleaned, indent=2, allow_nan=False) + '\n'
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.evidence-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
