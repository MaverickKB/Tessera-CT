"""Read the Tessera-owned xAI grant. This does not call another program."""

from .account_oauth import OAuthError, access_token
from .credentials import CredentialError


def resolve_access_token(home):
    try:
        return access_token(home, 'xai')
    except OAuthError as error:
        raise CredentialError(str(error)) from None
