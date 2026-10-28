"""Speech-only rendering of a committed agent message in its owning session."""

import json
import threading
import urllib.error
import urllib.request

from .credentials import CredentialError, CredentialResolver
from .voice import _NoRedirect


class SpeechError(RuntimeError):
    pass


class SessionSpeech:
    _slots = threading.BoundedSemaphore(2)
    sample_rate = 24000
    max_audio_bytes = sample_rate * 2 * 180

    def __init__(self, manager, *, opener=None, credentials=None):
        self.manager = manager
        self.opener = opener
        self.credentials = credentials or CredentialResolver(manager.config.credentials)

    def render(self, slug, message_i, offset=0, limit=300):
        if type(offset) is not int or offset < 0:
            raise ValueError('Speech offset must be a non-negative integer')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Speech chunk limit must be between 1 and 1000')
        message = self.manager._message(slug, message_i)
        if message.kind != 'agent' or message.meta.get('voice_transcript') or message.meta.get('provenance') == 'client_reported':
            raise ValueError('Only agent replies can be spoken')
        text = message.text
        if not text or offset >= len(text):
            raise ValueError('No reply text at this speech offset')
        end = min(offset + limit, len(text))
        if end < len(text):
            split = text.rfind(' ', offset + limit // 2, end)
            if split > offset:
                end = split + 1
        spoken = text[offset:end]
        if not self._slots.acquire(blocking=False):
            raise SpeechError('Speech output is busy')
        try:
            audio = self._synthesize(spoken)
        finally:
            self._slots.release()
        next_offset = end if end < len(text) else -1
        return audio, next_offset

    def _synthesize(self, text):
        settings = self.manager.config.voice
        try:
            credential = self.credentials.resolve(settings.credential_ref, env=settings.api_key_env)
            if not credential.value or credential.source == 'oauth':
                raise SpeechError('Speech media credential unavailable')
            body = {
                'text': text,
                'voice_id': 'rex',
                'language': 'en',
                'output_format': {'codec': 'pcm', 'sample_rate': self.sample_rate},
            }
            # This adapter has one media endpoint; it never sends a prompt or tools.
            request = urllib.request.Request('https://api.x.ai/v1/tts',
                data=json.dumps(body).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + credential.value}, method='POST')
            send = self.opener or urllib.request.build_opener(_NoRedirect()).open
            with send(request, timeout=60) as response:
                audio = response.read(self.max_audio_bytes + 1)
            if not audio or len(audio) > self.max_audio_bytes or len(audio) % 2:
                raise SpeechError('Speech output has an invalid audio format')
            return audio
        except CredentialError:
            raise SpeechError('Speech media credential unavailable') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SpeechError('Speech provider unavailable') from None
