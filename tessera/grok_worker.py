"""Official CLI-owned OAuth; the per-use API-key fallback runs only when explicitly opted in."""

import json
from pathlib import Path
import tempfile
import time

from .credentials import CredentialError, CredentialResolver, redact_secret
from .cli_worker import WorkerError


def chat(worker, messages):
    deadline = time.monotonic() + worker.settings.timeout
    environment = worker.environment()
    environment.pop('XAI_API_KEY', None)  # Vendor protocol key; OAuth is attempted first.
    worker.auth_path = ''
    with tempfile.TemporaryDirectory(prefix='tessera-grok-') as directory:
        prompt = Path(directory) / 'prompt.txt'
        prompt.write_text('Task input (the caller owns this task and its scope):\n' + json.dumps(messages, ensure_ascii=False))
        prompt.chmod(0o600)
        argv = [str(prompt) if arg == '{prompt_file}' else arg for arg in worker._argv()]
        try:
            result = worker._invoke(argv, environment, b'')
            worker.auth_path = 'cli_login'
            return result
        except WorkerError as error:
            if error.category != 'authentication':
                raise
            settings = worker.settings
            if (not settings.allow_api_key_fallback or not settings.fallback_env
                    or not (settings.fallback_credential_ref or settings.fallback_api_key_env)):
                raise WorkerError('Official Grok login unavailable; run grok login or grok login --device-auth', 'authentication') from None
            try:
                credential = CredentialResolver(worker.credential_sources).resolve(
                    settings.fallback_credential_ref, env=settings.fallback_api_key_env)
            except CredentialError:
                raise WorkerError('Configured Grok API-key fallback is unavailable', 'configuration') from None
            if not credential.value:
                raise WorkerError('Configured Grok API-key fallback is missing', 'configuration')
            environment[settings.fallback_env] = credential.value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError('Grok authentication exhausted the worker deadline', 'timeout')
            original = settings.timeout
            settings.timeout = remaining
            try:
                result = worker._invoke(argv, environment, b'')
                worker.auth_path = 'api_key_fallback'
                return redact_secret(result, credential.value)
            finally:
                settings.timeout = original
