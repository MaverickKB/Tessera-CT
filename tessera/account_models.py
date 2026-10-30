"""What each connected account can serve, and how a model becomes the choice.

The Models page asks this module instead of the provider directly. One account
that cannot answer is reported on its own row and does not blank the others,
because a failed discovery is information about that account, not about the
page.
"""

from .account_oauth import grant_report
from .model_discovery import DiscoveryError, discover_account
from .settings_model import OAUTH_MODELS

ACCOUNT_LABELS = {'xai': 'xAI Grok', 'openai-codex': 'ChatGPT or Codex'}
ROLES = ('main', 'fast', 'compactor', 'failover')


def credential_name(account):
    """The [credentials] entry an account's models use, or ''."""
    spec = OAUTH_MODELS.get(account)
    if spec is None:
        return ''
    return spec['credential']


def matching_backend(config, account, model):
    """The configured backend already wired to this account and model, or ''."""
    wanted = credential_name(account)
    if not wanted:
        return ''
    for name, backend in config.backends.items():
        if backend.model != model:
            continue
        if backend.credential_ref == wanted:
            return name
    return ''


def role_for(config, backend_name):
    """The role a backend holds, or '' when no role names it."""
    if not backend_name:
        return ''
    for role in ROLES:
        assigned = config.roles.get(role) or ''
        if assigned == backend_name:
            return role
    return ''


def account_rows(config):
    """One row per sign-in account: its state, its models, or why it failed."""
    rows = []
    for account, spec in OAUTH_MODELS.items():
        report = grant_report(config.home, spec['oauth'])
        row = {
            'id': account,
            'label': ACCOUNT_LABELS.get(account, account),
            'state': report['state'],
            'status': report['status'],
            'models': [],
            'error': '',
        }
        if report['state'] == 'current':
            try:
                names = discover_account(config, account)
            except DiscoveryError as error:
                row['error'] = str(error)
            else:
                for model in names:
                    backend_name = matching_backend(config, account, model)
                    backend = config.backends.get(backend_name) if backend_name else None
                    row['models'].append({
                        'id': model,
                        'backend': backend_name,
                        'chat': bool(backend is not None and backend.chat),
                        'enabled': bool(backend is not None and backend.enabled),
                        'role': role_for(config, backend_name),
                    })
        rows.append(row)
    return rows


def slug_name(text):
    """A backend-safe id from a model name: letters, digits and single dashes."""
    lowered = text.lower()
    characters = []
    for character in lowered:
        if character.isalnum():
            characters.append(character)
        else:
            characters.append('-')
    slug = ''.join(characters).strip('-')
    while '--' in slug:
        slug = slug.replace('--', '-')
    if not slug:
        slug = 'model'
    return slug[:48]


def account_prefix(account):
    """A short name for the account in a backend id."""
    if account == 'openai-codex':
        return 'codex'
    if account == 'xai':
        return 'grok'
    return slug_name(account)


def account_backend_name(config, account, model):
    """The backend id to use for this account and model.

    An existing backend wired to the same account and model is reused, so the
    same choice never creates a second row. Otherwise the name is derived from
    the account and model, with a counter when that id is already taken.
    """
    existing = matching_backend(config, account, model)
    if existing:
        return existing
    base = account_prefix(account) + '-' + slug_name(model)
    candidate = base
    index = 2
    while candidate in config.backends:
        candidate = base + '-' + str(index)
        index += 1
    return candidate
