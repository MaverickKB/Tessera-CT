"""Start a sign-in from the settings page and report only what the user must see.

The flow runs in this process. It does not start another program. Tokens are
written to this home by account_oauth and are not part of the status.
"""

import json
import threading
from pathlib import Path

from .account_oauth import OAuthError, sign_in_openai, sign_in_xai

PROVIDERS = ('xai', 'openai-codex')


class LoginError(RuntimeError):
    pass


class LoginDesk:
    def __init__(self, status_dir, home=None):
        self.status_dir = Path(status_dir)
        self.home = Path(home) if home is not None else self.status_dir.parent
        self._lock = threading.Lock()
        self._running = {}

    def start(self, provider, config=None):
        if provider not in PROVIDERS:
            raise LoginError('Unknown account')
        home = getattr(config, 'home', None) or self.home
        self.status_dir.mkdir(parents=True, exist_ok=True)
        status_path = self.status_dir / (provider + '.json')
        if status_path.exists():
            status_path.unlink()
        thread = threading.Thread(
            target=self._run,
            args=(provider, home, status_path),
            daemon=True,
        )
        with self._lock:
            self._running[provider] = thread
        thread.start()
        return {
            'status': 'starting',
            'verification_url': '',
            'user_code': '',
            'message': 'Requesting a sign-in code.',
        }

    def _run(self, provider, home, status_path):
        try:
            if provider == 'xai':
                sign_in_xai(home, status_path)
            else:
                sign_in_openai(home, status_path)
        except OAuthError as error:
            _write_error(status_path, str(error))
        except Exception:
            _write_error(status_path, 'Sign-in failed')

    def status(self, provider):
        if provider not in PROVIDERS:
            raise LoginError('Unknown account')
        status_path = self.status_dir / (provider + '.json')
        payload = {'status': 'idle', 'verification_url': '', 'user_code': '', 'message': ''}
        if status_path.is_file():
            try:
                loaded = json.loads(status_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                payload['status'] = str(loaded.get('status') or 'idle')
                payload['verification_url'] = str(loaded.get('verification_url') or '')
                payload['user_code'] = str(loaded.get('user_code') or '')
                payload['message'] = str(loaded.get('message') or '')
        return payload


def _write_error(path, message):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps({
        'status': 'error',
        'verification_url': '',
        'user_code': '',
        'message': message[:240],
    }), encoding='utf-8')
    temporary.replace(path)
