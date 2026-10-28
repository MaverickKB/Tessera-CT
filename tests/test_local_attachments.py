"""Host-local drops retain paths; only verified bytes reach consumers."""
import base64
import hashlib
import io
import json
import os

import pytest

from tessera import attachments
from tessera.server import ApiError, Handler
from tessera.sessions import SessionManager
from tessera.config import Config
from fake_backend import FakeBackend


PNG = b'\x89PNG\r\n\x1a\n' + b'image payload'


def test_local_reference_does_not_copy_payload(tmp_path):
    source = tmp_path / 'large.txt'
    data = b'original content\n' * 100000
    source.write_bytes(data)
    session = tmp_path / 'session'
    ref = attachments.reference(session, source)
    stored = list((session / 'attachments').iterdir())
    assert len(stored) == 1 and stored[0].suffix == '.ref'
    assert sum(path.stat().st_size for path in stored) < len(data) // 100
    assert json.loads(stored[0].read_text()) == ref
    assert ref['path'] == str(source.resolve())
    assert ref['bytes'] == len(data)
    assert ref['mtime_ns'] == source.stat().st_mtime_ns
    assert ref['sha256'] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize('name,data,media', [('note.txt', b'hello at use time', 'text/plain'),
                                           ('image.png', PNG, 'image/png'),
                                           ('report.pdf', b'%PDF-1.4\nreport', 'application/pdf')])
def test_local_reference_resolves_for_model_and_path_consumers(tmp_path, name, data, media):
    source = tmp_path / name
    source.write_bytes(data)
    session = tmp_path / 'session'
    ref = attachments.reference(session, source)
    # Deliberately resolve from disk with only the ID, as a resumed turn does.
    assert attachments.read(session, ref['id']) == (source.resolve(), media, data)
    assert attachments.resolve(session, ref['id']) == (source.resolve(), media)
    notes, images = attachments.describe(session, [ref])
    if media.startswith('image/'):
        assert images[0]['data_url'] == 'data:image/png;base64,' + base64.b64encode(data).decode()
    elif media == 'text/plain':
        assert data.decode() in notes[0]
    else:
        assert str(source.resolve()) in notes[0]


@pytest.mark.parametrize('change', ['content', 'mtime', 'missing', 'same_size_and_mtime'])
def test_changed_or_missing_reference_is_reported(tmp_path, change):
    source = tmp_path / 'note.txt'
    source.write_bytes(b'original')
    session = tmp_path / 'session'
    ref = attachments.reference(session, source)
    if change == 'missing':
        source.unlink()
    elif change == 'mtime':
        os.utime(source, ns=(ref['mtime_ns'], ref['mtime_ns'] + 1000000000))
    else:
        source.write_bytes(b'changed!' if change == 'same_size_and_mtime' else b'other')
        if change == 'same_size_and_mtime':
            os.utime(source, ns=(ref['mtime_ns'], ref['mtime_ns']))
    with pytest.raises(ValueError, match='missing|changed'):
        attachments.describe(session, [ref])
    with pytest.raises(ValueError, match='missing|changed'):
        attachments.resolve(session, ref['id'])


def test_local_reference_limit_applies_when_read(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, 'MAX_BYTES', 32)
    source = tmp_path / 'large.txt'
    source.write_bytes(b'x' * 1000)
    session = tmp_path / 'session'
    ref = attachments.reference(session, source)
    assert ref['bytes'] == 1000
    with pytest.raises(ValueError, match='exceeds 32 bytes at use time'):
        attachments.read(session, ref['id'])


def test_uploads_still_store_bytes_and_enforce_limit(tmp_path, monkeypatch):
    ref = attachments.store(tmp_path, PNG, 'remote.png', 'image/png')
    assert 'path' not in ref
    assert (tmp_path / 'attachments' / (ref['id'] + '.png')).read_bytes() == PNG
    assert attachments.read(tmp_path, ref['id'])[2] == PNG
    monkeypatch.setattr(attachments, 'MAX_BYTES', 1)
    with pytest.raises(ValueError, match='exceeds'):
        attachments.store(tmp_path, PNG, 'remote.png', 'image/png')


def manager_for(tmp_path, script):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.jev.enabled = False
    return SessionManager(config, backend_factory=lambda *args: FakeBackend(script))


def test_local_attached_turn_hands_verified_content_to_model_and_reports_change(tmp_path):
    source = tmp_path / 'note.txt'
    source.write_bytes(b'local evidence')
    seen = []
    def answer(messages, tools):
        seen.append(json.dumps(messages))
        return 'Received'
    with manager_for(tmp_path, [answer, answer]) as manager:
        slug = manager.create('Local drop')['slug']
        ref = manager.add_local_attachment(slug, source)
        manager.submit(slug, 'Read the local note', attachments=[ref])
        assert manager.wait(slug, 10)
        assert 'local evidence' in seen[0]
        source.unlink()
        manager.submit(slug, 'Read it again', attachments=[ref])
        assert manager.wait(slug, 10)
        assert 'Local attachment missing or unreadable' in seen[1]


def test_http_local_reference_registration_and_checked_download_without_socket(tmp_path):
    source = tmp_path / 'note.txt'
    source.write_bytes(b'download content')
    with manager_for(tmp_path, ['OK']) as manager:
        slug = manager.create('Local HTTP')['slug']
        handler = object.__new__(Handler)
        handler._body = lambda *args: {'path': str(source.resolve())}
        from types import SimpleNamespace
        handler.server = SimpleNamespace(manager=manager)
        registered = {}
        handler._json = lambda status, body: registered.update(status=status, body=body)
        handler._api('POST', f'/api/sessions/{slug}/attachment-references', {})
        assert registered['status'] == 201
        ref = registered['body']
        handler.wfile = io.BytesIO()
        headers = {}
        handler.send_response = lambda status: headers.update(status=status)
        handler.send_header = lambda key, value: headers.update({key: value})
        handler.end_headers = lambda: None
        handler._send_attachment(manager, slug, {'id': [ref['id']]})
        assert handler.wfile.getvalue() == b'download content'
        assert headers['Cache-Control'] == 'no-store'
        source.write_bytes(b'substituted')
        handler.wfile = io.BytesIO()
        with pytest.raises(ApiError, match='changed'):
            handler._send_attachment(manager, slug, {'id': [ref['id']]})
        assert handler.wfile.getvalue() == b''


@pytest.mark.parametrize('tool,filename,data', [('read_file', 'note.txt', b'local text'),
                                              ('read_bytes', 'note.pdf', b'%PDF-1.4'),
                                              ('read_image', 'note.png', PNG)])
def test_later_model_file_tools_revalidate_local_reference(tmp_path, tool, filename, data):
    source = tmp_path / filename
    source.write_bytes(data)
    with manager_for(tmp_path, ['OK']) as manager:
        slug = manager.create('Tool inspection')['slug']
        manager.add_local_attachment(slug, source)
        registry = manager.runtime(slug).session.tools
        handler = registry._tools[tool].handler
        assert handler(str(source))
        source.write_bytes(b'changed content')
        with pytest.raises(ValueError, match='changed'):
            handler(str(source))
        manager.add_local_attachment(slug, source)
        assert handler(str(source))


def test_large_artifact_can_be_placed_without_copying_or_reading_at_placement(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, 'MAX_BYTES', 32)
    source = tmp_path / 'large.txt'
    source.write_bytes(b'x' * 1000)
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    with SessionManager(config, backend_factory=lambda *args: FakeBackend(['OK'])) as manager:
        slug = manager.create('Artifact')['slug']
        project = manager.board.create_project('Files', slug)
        handler = manager.runtime(slug).session.tools._tools['board_project_artifact'].handler
        result = handler(project['id'], str(source))
        assert result['attachment']['path'] == str(source.resolve())
        assert result['workspace']['canvas']['nodes'][0]['attachment_id'] == result['attachment']['id']
        with pytest.raises(ValueError, match='exceeds 32 bytes at use time'):
            manager.attachment_content(slug, result['attachment']['id'])
