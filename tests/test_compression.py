import gzip
import http.client
import json
import zlib

from test_server import running_server, call


def fetch(server, path, encoding):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request('GET', path, headers={'Accept-Encoding': encoding} if encoding else {})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


def test_json_and_static_are_compressed_only_when_accepted(tmp_path):
    with running_server(tmp_path) as server:
        for n in range(30):
            call(server, 'POST', '/api/sessions', {'title': f'Session number {n}'})
        plain_response, plain = fetch(server, '/api/sessions', None)
        assert plain_response.getheader('Content-Encoding') is None and len(json.loads(plain)) == 30
        response, data = fetch(server, '/api/sessions', 'gzip, deflate, br')
        assert response.getheader('Content-Encoding') == 'gzip' and response.getheader('Vary') == 'Accept-Encoding'
        assert gzip.decompress(data) == plain and int(response.getheader('Content-Length')) == len(data) < len(plain)
        response, data = fetch(server, '/api/sessions', 'deflate')
        assert response.getheader('Content-Encoding') == 'deflate' and zlib.decompress(data) == plain
        response, _ = fetch(server, '/api/sessions', 'gzip;q=0')
        assert response.getheader('Content-Encoding') is None
        response, data = fetch(server, '/static/app.js', 'gzip')
        assert response.getheader('Content-Encoding') == 'gzip' and b'TesseraItems' in gzip.decompress(data)
        response, data = fetch(server, '/api/status', 'gzip')
        small = fetch(server, '/api/sessions/session-number-0/feedback', 'gzip')[0]
        assert small.getheader('Content-Encoding') is None  # under the threshold
        server.manager.config.server.compress_min_bytes = 0
        assert fetch(server, '/api/sessions', 'gzip')[0].getheader('Content-Encoding') is None
