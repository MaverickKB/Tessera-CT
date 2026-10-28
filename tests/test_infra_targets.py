"""Prometheus targets endpoint against a loopback fake Prometheus."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from tessera.config import load_config
from tessera.server import create_server
from test_server import call


class Fake(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'status': 'success', 'data': {'activeTargets': [
            {'labels': {'job': 'node', 'instance': 'a:9100'}, 'health': 'up', 'lastError': '', 'lastScrape': 't'},
            {'labels': {'job': 'node', 'instance': 'b:9100'}, 'health': 'down', 'lastError': 'refused', 'lastScrape': 't'}]}}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_targets_are_listed_down_first(tmp_path):
    prom = HTTPServer(('127.0.0.1', 0), Fake)
    threading.Thread(target=prom.serve_forever, daemon=True).start()
    config = load_config(tmp_path)
    config.infrastructure.prometheus_url = f'http://127.0.0.1:{prom.server_port}'
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, body = call(server, 'GET', '/api/infrastructure/targets')
        assert code == 200 and body['up'] == 1 and body['total'] == 2
        assert body['targets'][0]['instance'] == 'b:9100' and body['targets'][0]['error'] == 'refused'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        prom.shutdown()
