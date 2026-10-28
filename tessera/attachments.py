"""Session files a person attaches to a chat turn.

Uploads live beside the session log; local files keep an integrity-checked
path. The log keeps a short reference. The model sees images as data URLs for that turn only, the same
shape computer-use screenshots already use.
"""

import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from pathlib import Path


KINDS = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'text/plain': 'txt',
    'text/markdown': 'md',
    'application/pdf': 'pdf',
    'application/json': 'json',
}
MAX_BYTES = 8 * 1024 * 1024
MAX_PER_TURN = 6
TEXT_CHARS = 12000
NAME_LIMIT = 120


def image_kind(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'image/gif'
    if len(data) >= 12 and data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return ''


def _safe_name(name):
    raw = Path(str(name or 'file')).name
    cleaned = re.sub(r'[^A-Za-z0-9._ -]+', '', raw).strip(' .')
    return (cleaned or 'file')[:NAME_LIMIT]


def store(session_dir, data, name, claimed_type=''):
    """Write one file. Returns the reference the log and the model share."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError('Attachment is empty')
    if len(data) > MAX_BYTES:
        raise ValueError(f'Attachment exceeds {MAX_BYTES} bytes')
    return _write(session_dir, data, name, _media_type(data, claimed_type))


def _media_type(data, claimed_type):
    detected = image_kind(data)
    media = detected or (claimed_type if claimed_type in KINDS else '')
    if not media and _looks_like_text(data):
        return 'text/plain'
    if media not in KINDS:
        raise ValueError('Attachment must be a png, jpeg, gif, webp, text, markdown, json, or pdf')
    return media


def reference(session_dir, path):
    """Register a host-local file without retaining its payload in the store.

    Hash in bounded chunks, including files too large to inline. MAX_BYTES is
    enforced when content is requested, rather than limiting reference creation.
    """
    target = Path(path).expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    with target.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Attachment must be a regular file')
        if before.st_size == 0:
            raise ValueError('Attachment is empty')
        sample = stream.read(4096)
        media = _media_type(sample, mimetypes.guess_type(target.name)[0] or '')
        digest.update(sample)
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(target.stat()):
        raise ValueError('Local attachment changed while registering')
    ref = {
        'id': uuid.uuid4().hex,
        'name': _safe_name(target.name),
        'media_type': media,
        'bytes': before.st_size,
        'sha256': digest.hexdigest(),
        'path': str(target),
        'mtime_ns': before.st_mtime_ns,
    }
    directory = Path(session_dir) / 'attachments'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (ref['id'] + '.ref')).write_text(json.dumps(ref), encoding='utf-8')
    return ref


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _write(session_dir, data, name, media):
    file_id = uuid.uuid4().hex
    directory = Path(session_dir) / 'attachments'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (file_id + '.' + KINDS[media])
    target.write_bytes(bytes(data))
    return {
        'id': file_id,
        'name': _safe_name(name),
        'media_type': media,
        'bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }


def _looks_like_text(data):
    sample = data[:4096]
    if b'\x00' in sample:
        return False
    try:
        sample.decode('utf-8')
    except UnicodeDecodeError:
        return False
    return True


def _location(session_dir, file_id):
    if not isinstance(file_id, str) or not re.fullmatch(r'[0-9a-f]{32}', file_id):
        raise ValueError('Invalid attachment')
    directory = Path(session_dir) / 'attachments'
    matches = list(directory.glob(file_id + '.*')) if directory.is_dir() else []
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError('Attachment not found')
    target = matches[0].resolve()
    if not target.is_relative_to(directory.resolve()):
        raise ValueError('Attachment not found')
    if target.suffix == '.ref':
        ref = json.loads(target.read_text(encoding='utf-8'))
        return Path(ref['path']), ref['media_type'], ref
    suffix = target.suffix.lstrip('.')
    media = next((kind for kind, ext in KINDS.items() if ext == suffix), 'application/octet-stream')
    return target, media, None


def read(session_dir, file_id):
    """Return exactly the bounded, verified bytes used by HTTP and model input."""
    target, media, ref = _location(session_dir, file_id)
    try:
        with target.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('Attachment must be a regular file')
            if ref and (before.st_size != ref['bytes'] or before.st_mtime_ns != ref['mtime_ns']):
                raise ValueError('Local attachment changed since it was attached')
            if before.st_size > MAX_BYTES:
                raise ValueError(f'Attachment exceeds {MAX_BYTES} bytes at use time')
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(data) > MAX_BYTES:
            raise ValueError(f'Attachment exceeds {MAX_BYTES} bytes at use time')
        if ref:
            if (_identity(before) != _identity(after)
                    or _identity(after) != _identity(target.stat())
                    or hashlib.sha256(data).hexdigest() != ref['sha256']):
                raise ValueError('Local attachment changed since it was attached')
    except OSError as exc:
        raise ValueError('Local attachment missing or unreadable' if ref else 'Attachment not found') from exc
    return target, media, data


def location(session_dir, file_id):
    """Locate metadata for canvas placement without loading artifact content."""
    target, media, ref = _location(session_dir, file_id)
    try:
        info = target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Attachment must be a regular file')
        if ref and (info.st_size != ref['bytes'] or info.st_mtime_ns != ref['mtime_ns']):
            raise ValueError('Local attachment changed since it was attached')
    except OSError as exc:
        raise ValueError('Local attachment missing or unreadable' if ref else 'Attachment not found') from exc
    return target, media


def resolve(session_dir, file_id):
    """Compatibility path for board/canvas inspection; validate before exposing it."""
    target, media, _ = read(session_dir, file_id)
    return target, media


def read_path(session_dir, path):
    """Revalidate a referenced path when a model later uses a file-reading tool.

    An explicit reattachment replaces the earlier expectation for that path.
    Ordinary workspace files keep the tool's existing behavior.
    """
    target = Path(path).absolute()
    directory = Path(session_dir) / 'attachments'
    latest = None
    latest_time = -1
    for entry in directory.glob('*.ref'):
        ref = json.loads(entry.read_text(encoding='utf-8'))
        if Path(ref['path']) == target:
            registered = entry.stat().st_mtime_ns
            if registered > latest_time:
                latest = ref
                latest_time = registered
    if latest is not None:
        return read(session_dir, latest['id'])[2]
    return target.read_bytes()


def describe(session_dir, refs):
    """Text the model can read, plus images in the computer-use shape."""
    notes = []
    images = []
    for ref in refs:
        path, media, data = read(session_dir, ref['id'])
        label = f"{ref['name']} ({media}, {len(data)} bytes)"
        if media.startswith('image/'):
            import base64
            url = 'data:' + media + ';base64,' + base64.b64encode(data).decode('ascii')
            images.append({'caption': 'The operator attached ' + label, 'data_url': url})
            notes.append(label + ' — shown with this message.')
        elif media == 'application/pdf':
            notes.append(label + f' — saved at {path}. Use read_bytes if you need the file.')
        else:
            text = data.decode('utf-8', errors='replace')
            if len(text) > TEXT_CHARS:
                text = text[:TEXT_CHARS] + f'\n[truncated: {len(data)} bytes]'
            notes.append(label + ':\n' + text)
    return notes, images
