"""The local speech pipe: push-to-talk through speech-to-text and text-to-speech.

The duplex pipe hands the browser a provider socket. This pipe keeps the audio on
the Tessera server: one recorded utterance goes to the speech-to-text endpoint,
the transcript becomes a normal turn in the session, and the reply is rendered by
the text-to-speech endpoint and returned as PCM for the page to play.

Both services speak the OpenAI audio API: ``POST /audio/transcriptions`` with a
multipart WAV and ``POST /audio/speech`` with JSON. Nothing is typed here; the
model names come from the settings the page saved.
"""

import base64
import json
import struct
import time
import urllib.error
import urllib.request
import uuid

from .voice import selected_pipe


class LocalVoiceError(ValueError):
    """A local speech step that could not finish, with a plain reason."""

    def __init__(self, category):
        self.category = category
        super().__init__('Local speech unavailable (' + category + ')')


class LocalVoice:
    """One push-to-talk request, shared by the HTTP route and the desktop IPC.

    The caller supplies base64 PCM16 and its sample rate; everything else comes
    from the saved [voice.stt] and [voice.tts] settings. The same bounds apply on
    both transports because they are checked here, once.
    """

    def __init__(self, manager):
        self.manager = manager

    def utterance(self, slug, audio_text, rate):
        settings = self.manager.config.voice
        if selected_pipe(settings) != 'local':
            raise LocalVoiceError('pipe')
        if not isinstance(audio_text, str) or type(rate) is not int:
            raise LocalVoiceError('request')
        try:
            audio = base64.b64decode(audio_text, validate=True)
        except (ValueError, TypeError):
            raise LocalVoiceError('encoding') from None
        if not audio or len(audio) > settings.local_max_audio_bytes:
            raise LocalVoiceError('size')
        if rate < 8000 or rate > 48000 or len(audio) % 2:
            raise LocalVoiceError('rate')
        return utterance(self.manager, slug, audio, rate, settings.local_reply_timeout)


def pcm_to_wav(pcm, rate, channels=1):
    """Wrap 16-bit mono or stereo PCM in a RIFF/WAVE header."""
    block_align = channels * 2
    byte_rate = rate * block_align
    header = (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE'
              + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, byte_rate, block_align, 16)
              + b'data' + struct.pack('<I', len(pcm)))
    return header + pcm


def wav_to_pcm(raw):
    """Return (pcm, rate, channels) from a PCM16 RIFF/WAVE body."""
    if len(raw) < 44 or raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise LocalVoiceError('audio_format')
    position = 12
    fmt = None
    while position + 8 <= len(raw):
        chunk_id = raw[position:position + 4]
        size = struct.unpack('<I', raw[position + 4:position + 8])[0]
        body = raw[position + 8:position + 8 + size]
        if chunk_id == b'fmt ' and len(body) >= 16:
            fmt = struct.unpack('<HHIIHH', body[:16])
        elif chunk_id == b'data':
            if fmt is None:
                raise LocalVoiceError('audio_format')
            format_tag, channels, rate, _byte_rate, _block_align, bits = fmt
            if format_tag != 1 or bits != 16 or channels not in (1, 2):
                raise LocalVoiceError('audio_format')
            if not body or len(body) % (channels * 2):
                raise LocalVoiceError('audio_format')
            return body, rate, channels
        position += 8 + size + (size & 1)
    raise LocalVoiceError('audio_format')


def _multipart(fields, files):
    """Build one multipart/form-data body without a client library."""
    boundary = 'tessera-' + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        head = ('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name
                + '"\r\n\r\n').encode('utf-8')
        parts.append(head + str(value).encode('utf-8') + b'\r\n')
    for name, filename, content_type, data in files:
        head = ('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name
                + '"; filename="' + filename + '"\r\nContent-Type: ' + content_type
                + '\r\n\r\n').encode('utf-8')
        parts.append(head + data + b'\r\n')
    parts.append(('--' + boundary + '--\r\n').encode('utf-8'))
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


def _post(request, timeout, limit, opener):
    send = opener or urllib.request.urlopen
    try:
        with send(request, timeout=timeout) as response:
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise LocalVoiceError('upstream') from None
    except urllib.error.URLError as error:
        raise LocalVoiceError('timeout' if isinstance(error.reason, TimeoutError) else 'connection') from None
    except TimeoutError:
        raise LocalVoiceError('timeout') from None
    except OSError:
        raise LocalVoiceError('connection') from None
    if len(raw) > limit:
        raise LocalVoiceError('invalid_response')
    return raw


def transcribe(settings, pcm, rate, opener=None):
    """Send one utterance to the speech-to-text service and return its text."""
    body, content_type = _multipart(
        {'model': settings.model},
        [('file', 'speech.wav', 'audio/wav', pcm_to_wav(pcm, rate))])
    url = settings.base_url.rstrip('/') + '/audio/transcriptions'
    ask = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': content_type}, method='POST')
    raw = _post(ask, settings.timeout, settings.max_response_bytes, opener)
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise LocalVoiceError('invalid_response') from None
    text = payload.get('text') if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise LocalVoiceError('transcript')
    return text.strip()


def synthesize(settings, text, opener=None):
    """Ask the text-to-speech service for one reply and return (pcm, rate, channels)."""
    body = {'model': settings.model, 'input': text, 'response_format': 'wav'}
    if settings.voice:
        body['voice'] = settings.voice
    url = settings.base_url.rstrip('/') + '/audio/speech'
    ask = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    raw = _post(ask, settings.timeout, settings.max_response_bytes, opener)
    return wav_to_pcm(raw)


def _wait_for_turn(manager, slug, turn_id, timeout):
    runtime = manager.runtime(slug)
    deadline = time.monotonic() + timeout
    with runtime.condition:
        while True:
            item = runtime.turns.get(turn_id)
            if item is None:
                raise LocalVoiceError('turn')
            if item.get('state') in ('done', 'error'):
                return item
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LocalVoiceError('turn_timeout')
            runtime.condition.wait(min(remaining, 0.5))


def utterance(manager, slug, audio, rate, timeout):
    """Run one push-to-talk utterance: speech in, session turn, speech out."""
    settings = manager.config.voice
    transcript = transcribe(settings.stt, audio, rate)
    before = manager.get(slug).log.count()
    turn_id = manager.submit(slug, transcript, source='voice')
    item = _wait_for_turn(manager, slug, turn_id, timeout)
    if item.get('state') != 'done':
        raise LocalVoiceError('turn')
    reply = ''
    for message in manager.get(slug).log.iter(before):
        if message.kind == 'agent':
            reply = message.text
    if not reply:
        raise LocalVoiceError('reply')
    spoken = reply[:settings.max_text_bytes]
    pcm, speech_rate, channels = synthesize(settings.tts, spoken)
    return {
        'transcript': transcript,
        'reply': reply,
        'audio': base64.b64encode(pcm).decode('ascii'),
        'rate': speech_rate,
        'channels': channels,
    }
