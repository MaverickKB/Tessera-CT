"""Read-only Grafana transport contracts never use live credentials or accounts."""
import io
import json
from unittest.mock import Mock

import pytest

from tessera.config import Config, GrafanaConfig
from tessera.credentials import CredentialResolver


def client_for(monkeypatch, responses):
    from tessera.grafana import GrafanaClient
    monkeypatch.setenv('EXAMPLE_GRAFANA_TOKEN', 'synthetic-grafana-secret')
    config = GrafanaConfig(enabled=True, base_url='https://metrics.example.com/subpath', token_env='EXAMPLE_GRAFANA_TOKEN',
                           allowed_proxy_paths=['api/v1/query'])
    requests = []
    def send(request, timeout):
        requests.append(request)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())
    return GrafanaClient(config, opener=send), requests


def test_disabled_grafana_never_resolves_constructs_transport_or_reads_env():
    from tessera.grafana import GrafanaClient, GrafanaError
    resolver = Mock()
    send = Mock()
    client = GrafanaClient(GrafanaConfig(), credentials=resolver, opener=send)
    assert client.status() == {'enabled': False, 'configured': False, 'health': 'unknown'}
    for call in (client.list_datasources, client.list_dashboards, lambda: client.query_proxy('uid','api/v1/query',{})):
        with pytest.raises(GrafanaError):
            call()
    resolver.resolve.assert_not_called()
    send.assert_not_called()


def test_lists_proxy_query_and_token_free_embed_are_complete(monkeypatch):
    client, requests = client_for(monkeypatch, [
        [{'uid':'source-fixture','name':'private-source','type':'prometheus','url':'private-address','secureJsonFields':{'token':True}}],
        [{'uid':'dashboard-fixture','title':'private-dashboard','type':'dash-db','url':'private-path'}],
        {'result':[{'value':12,'label':'synthetic-grafana-secret'}]}
    ])
    assert client.list_datasources() == [{'uid':'source-fixture','name':'private-source','type':'prometheus'}]
    assert client.list_dashboards() == [{'uid':'dashboard-fixture','title':'private-dashboard'}]
    assert client.query_proxy('source-fixture','api/v1/query',{'query':'up'})['result'][0]['label'] == '[REDACTED]'
    assert [r.get_method() for r in requests] == ['GET']*3
    assert requests[0].full_url == 'https://metrics.example.com/subpath/api/datasources'
    assert '/api/search?' in requests[1].full_url and 'type=dash-db' in requests[1].full_url
    assert requests[2].full_url.endswith('/api/datasources/proxy/uid/source-fixture/api/v1/query?query=up')
    expected_authorization = 'Bearer ' + 'synthetic-grafana-secret'
    assert all(r.get_header('Authorization') == expected_authorization for r in requests)
    url = client.embed_url('dashboard-fixture', 'example-panel', panel_id=2, from_time='now-1h', to_time='now')
    assert '/subpath/d-solo/dashboard-fixture/example-panel?' in url and 'panelId=2' in url
    assert 'secret' not in url and 'token' not in url
    assert client.status() == {'enabled':True,'configured':True,'health':'healthy','datasource_count':1,'dashboard_count':1}


def test_proxy_path_escape_and_mutations_are_rejected_before_credentials(monkeypatch):
    from tessera.grafana import GrafanaError
    client, requests = client_for(monkeypatch, [])
    resolver = Mock()
    client.credentials = resolver
    for path in ('../admin','/api/v1/query','api/%2e%2e/query','api/v1/query?x=1','api/v1/delete_series','https://other.example.com'):
        with pytest.raises(GrafanaError):
            client.query_proxy('source-fixture',path,{})
    for uid in ('../admin', 'a/b', 'a%2fb'):
        with pytest.raises(GrafanaError):
            client.query_proxy(uid,'api/v1/query',{})
    resolver.resolve.assert_not_called()
    assert requests == []


def test_bounded_responses_fixed_errors_and_missing_credentials(monkeypatch):
    from tessera.grafana import GrafanaError
    import urllib.error
    client, requests = client_for(monkeypatch, [])
    client.settings.max_response_bytes = 10
    client.opener = lambda *args, **kwargs: io.BytesIO(b'x'*11)
    with pytest.raises(GrafanaError) as error:
        client.list_datasources()
    assert error.value.category == 'invalid_response'
    assert client.status()['health'] == 'unavailable'
    client.opener = lambda *args, **kwargs: (_ for _ in ()).throw(
        urllib.error.HTTPError('https://private.example.com',403,'private secret',{},io.BytesIO(b'private-body')))
    with pytest.raises(GrafanaError) as error:
        client.list_datasources()
    assert error.value.category == 'access' and 'private' not in str(error.value)
    monkeypatch.delenv('EXAMPLE_GRAFANA_TOKEN')
    send = Mock()
    client.opener = send
    with pytest.raises(GrafanaError) as error:
        client.list_datasources()
    assert error.value.category == 'credential'
    send.assert_not_called()


def test_reference_credentials_and_route_settings_are_used_lazily(monkeypatch):
    from tessera.grafana import GrafanaClient
    from tessera.credentials import CredentialReference
    monkeypatch.setenv('EXAMPLE_GRAFANA_KEY', 'fixture-secret')
    resolver = CredentialResolver({'metrics':CredentialReference(env='EXAMPLE_GRAFANA_KEY')})
    settings = GrafanaConfig(enabled=True,base_url='https://metrics.example.com',credential_ref='metrics',
                             datasources_path='/configured/datasources')
    send = Mock(return_value=io.BytesIO(b'[]'))
    client = GrafanaClient(settings, credentials=resolver, opener=send)
    assert client.status()['configured'] and not send.called
    assert client.list_datasources() == []
    assert send.call_args.args[0].full_url.endswith('/configured/datasources')


def test_unexpected_transport_errors_are_sanitized_and_default_redirects_do_not_forward_tokens(monkeypatch):
    from tessera.grafana import GrafanaClient, GrafanaError
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    client, _ = client_for(monkeypatch, [])
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic-grafana-secret private-route')
    client.opener = fail
    with pytest.raises(GrafanaError) as error:
        client.list_datasources()
    assert error.value.category == 'unavailable' and 'secret' not in str(error.value)
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            seen.append(self.path)
            self.send_response(302)
            self.send_header('Location','/must-not-follow')
            self.send_header('Content-Length','0')
            self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        client=GrafanaClient(GrafanaConfig(enabled=True,base_url='http://127.0.0.1:'+str(server.server_address[1]),
                                          token_env='EXAMPLE_GRAFANA_TOKEN'))
        with pytest.raises(GrafanaError) as error:
            client.list_datasources()
        assert error.value.category == 'http' and seen == ['/api/datasources']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_datasource_rows_are_bounded_and_say_how_many_the_bound_left_out(monkeypatch):
    client, requests = client_for(monkeypatch, [
        [{'uid': 'source-%02d' % i, 'name': 'private-%02d' % i, 'type': 'prometheus'} for i in range(1, 4)],
        [{'uid': 'source-01', 'name': 'private-01', 'type': 'prometheus'}],
    ])
    client.settings.list_limit = 2
    assert [row['uid'] for row in client.list_datasources()] == ['source-01', 'source-02']
    assert client.status()['datasource_count'] == 2
    assert client.status()['datasource_omitted'] == 1
    # A later read that fits inside the bound must drop the stale count, or the page
    # keeps claiming rows are missing after nothing is.
    assert [row['uid'] for row in client.list_datasources()] == ['source-01']
    assert client.status()['datasource_count'] == 1
    assert 'datasource_omitted' not in client.status()
    assert len(requests) == 2
