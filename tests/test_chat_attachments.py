"""A person can attach a screenshot or a text file to a Tessera chat turn."""

import time

from test_server import call, running_server


PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000001000000010802000000907753'
    'de0000000c4944415408d763f8ffff3f0005fe02fe4c8c2f190000000049454e44ae426082'
)


def _wait_user(server, slug, count):
    deadline = time.time() + 10
    while time.time() < deadline:
        rows = call(server, 'GET', f'/api/sessions/{slug}/messages')[1]
        users = [row for row in rows if row['kind'] == 'user']
        if len(users) >= count:
            return users
        time.sleep(0.05)
    raise AssertionError('attached turn did not land')


def test_a_screenshot_is_stored_shown_and_handed_to_the_model(tmp_path):
    seen = {}

    def answer(messages, tools):
        seen['messages'] = messages
        return {'content': 'I can see it.'}

    with running_server(tmp_path, [answer]) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Shots'})[1]['slug']
        connection = __import__('http.client', fromlist=['HTTPConnection']).HTTPConnection(*server.server_address, timeout=5)
        connection.request('POST', f'/api/sessions/{slug}/attachments', body=PNG, headers={
            'Content-Type': 'image/png',
            'Content-Length': str(len(PNG)),
            'X-File-Name': 'screen%20shot.png',
        })
        response = connection.getresponse()
        stored = __import__('json').loads(response.read())
        assert response.status == 201
        connection.close()
        assert stored['media_type'] == 'image/png'
        assert stored['name'] == 'screen shot.png'
        assert stored['bytes'] == len(PNG)

        status, queued = call(server, 'POST', f'/api/sessions/{slug}/messages', {
            'text': 'What is in this shot?',
            'attachments': [stored],
        })
        assert status == 202 and queued['turn_id']
        user = _wait_user(server, slug, 1)[-1]
        assert user['text'] == 'What is in this shot?'
        assert user['meta']['attachments'][0]['id'] == stored['id']
        assert 'base64' not in user['text']

        fetch = __import__('http.client', fromlist=['HTTPConnection']).HTTPConnection(*server.server_address, timeout=5)
        fetch.request('GET', f'/api/sessions/{slug}/attachments?id={stored["id"]}')
        fetched = fetch.getresponse()
        body = fetched.read()
        assert fetched.status == 200
        assert fetched.getheader('Content-Type') == 'image/png'
        assert body == PNG
        fetch.close()

        deadline = time.time() + 10
        while time.time() < deadline and 'messages' not in seen:
            time.sleep(0.05)
        handed = seen['messages']
        image_parts = [part for message in handed if isinstance(message.get('content'), list) for part in message['content']]
        assert any(part.get('type') == 'image_url' and part['image_url']['url'].startswith('data:image/png;base64,') for part in image_parts)
        assert any('What is in this shot?' in (message.get('content') or '') for message in handed if isinstance(message.get('content'), str))


def test_a_text_file_is_inlined_and_a_binary_is_refused(tmp_path):
    with running_server(tmp_path, ['Noted.']) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Files'})[1]['slug']
        connection = __import__('http.client', fromlist=['HTTPConnection']).HTTPConnection(*server.server_address, timeout=5)
        note = b'hello from the file\n'
        connection.request('POST', f'/api/sessions/{slug}/attachments', body=note, headers={
            'Content-Type': 'text/plain',
            'Content-Length': str(len(note)),
            'X-File-Name': 'note.txt',
        })
        response = connection.getresponse()
        stored = __import__('json').loads(response.read())
        assert response.status == 201 and stored['media_type'] == 'text/plain'
        connection.close()
        assert call(server, 'POST', f'/api/sessions/{slug}/messages', {
            'text': 'Read this.',
            'attachments': [stored],
        })[0] == 202
        refused = __import__('http.client', fromlist=['HTTPConnection']).HTTPConnection(*server.server_address, timeout=5)
        blob = b'\x00\x01not-an-image'
        refused.request('POST', f'/api/sessions/{slug}/attachments', body=blob, headers={
            'Content-Type': 'application/octet-stream',
            'Content-Length': str(len(blob)),
            'X-File-Name': 'blob.bin',
        })
        rejected = refused.getresponse()
        assert rejected.status == 400
        refused.close()
