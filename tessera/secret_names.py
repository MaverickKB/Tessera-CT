"""Which config names hold a secret.

The last component of a dotted path is the name, read case-insensitively and
with '-' treated as '_'. A name is a secret when it ends with one of the
credential words below, at the start of the name or after an underscore:
auth_token, api_key, password, secret, token, and anything ending in one of
those (BRAVE_API_KEY, CLIENT_SECRET, DB_PASSWORD, webhook_token).

A longer word that merely contains one of them stays visible, because it names
a count, a variable or a path rather than a value: token_budget, token_env,
api_key_env, secret_ttl_seconds, client_secret_path.
"""

import re

_NAME = re.compile(r'(?i)(?:^|_)(?:auth_token|token|secret|password|api_key)$')
# Same shapes scripts/check_no_hardwires.sh treats as credentials.
_SECRET_VALUE = re.compile(
    r'(?i)(?:^sk-|^xai-|sk-[A-Za-z0-9]{8,}|Bearer [A-Za-z0-9._-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})'
)
_ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')


def is_secret_name(key):
    name = str(key or '').split('.')[-1].replace('-', '_')
    return _NAME.search(name) is not None


def flagged_env_value(value):
    """True when an env-var field holds a secret value instead of a variable name.

    Any environment-variable name is accepted, including MODEL_API_KEY and
    pasted_api_key. A value is a secret when it uses a key prefix (sk- or
    xai-), matches a known token shape, or is a long mix of letters and digits
    with no underscore.
    """
    text = str(value or '')
    if _SECRET_VALUE.search(text):
        return True
    if _ENV_NAME.fullmatch(text) and '_' in text:
        return False
    if (len(text) >= 24 and re.search(r'[A-Za-z]', text) and re.search(r'\d', text)
            and '_' not in text):
        return True
    return False
