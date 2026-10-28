"""Identity headers the Codex endpoint expects on the Tessera-owned grant.

The account id and residency come from the OAuth token itself. A bad token
drops the headers instead of raising, so the request fails as auth, not as a
crash. The token is never logged.
"""

import base64
import json
from urllib.parse import urlsplit


def official_codex_url(base_url):
    parsed = urlsplit(base_url or "")
    path = parsed.path.rstrip("/")
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return False
    if parsed.port not in (None, 443):
        return False
    return path == "/backend-api/codex" or path.startswith("/backend-api/codex/")


def codex_headers(access_token):
    headers = {
        "User-Agent": "codex-cli",
        "originator": "tessera",
        "Accept": "application/json",
    }
    if not isinstance(access_token, str) or not access_token.strip():
        return headers
    parts = access_token.split(".")
    if len(parts) < 2:
        return headers
    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        raw = base64.urlsafe_b64decode(payload)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return headers
    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        return headers
    account_id = auth.get("chatgpt_account_id")
    if isinstance(account_id, str) and account_id:
        headers["ChatGPT-Account-ID"] = account_id
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    if isinstance(residency, str) and residency.strip():
        headers["x-openai-internal-codex-residency"] = residency.strip()
    return headers
