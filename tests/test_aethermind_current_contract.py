"""Offline checks against the checked-in current MCP argument schemas.

These verify Tessera's wire arguments, not the live runtime or bundled engine.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient
from tessera.mcp_client import McpError


def test_current_inheritance_binding_and_view_window_reach_wire():
    with FakeMcpServer(dialect='path') as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        client.prepare_inheritance('/synthetic-project', anchor='tessera', budget=1200,
                                   surface='alpha-test', session_id='synthetic-session')
        client.tree_view('/synthetic-project', budget_chars=900, recent_window=12)
        calls = [request['params'] for request, _ in server.requests
                 if request['method'] == 'tools/call']
        assert calls == [
            {'name': 'capabilities', 'arguments': {}},
            {'name': 'prepare_inheritance', 'arguments': {
                'project_root': '/synthetic-project', 'anchor': 'tessera', 'budget': 1200,
                'surface': 'alpha-test', 'session_id': 'synthetic-session'}},
            {'name': 'view', 'arguments': {
                'project_root': '/synthetic-project', 'budget_chars': 900, 'recent_window': 12}},
        ]
        assert server.native_write_calls == 0


@pytest.mark.parametrize('capabilities', [
    {}, {'success': False}, {'success': True, 'runtime': {}},
    {'success': True, 'runtime': {'runtime_id': 'aethermind-python', 'contract_version': 'legacy'}},
    {'success': True, 'runtime': {'runtime_id': 'other', 'contract_version': 'adapter-v1'}},
])
def test_invalid_capabilities_never_publish_or_read(capabilities):
    with FakeMcpServer(dialect='path', tool_results={'capabilities': capabilities}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1)
        with pytest.raises(McpError):
            client.tree_view('/synthetic-project')
        assert client._tools is None
        assert client._toolsets == client._layer_types == {}
        assert all(request.get('params', {}).get('name') in (None, 'capabilities')
                   for request, _ in server.requests)


def test_legacy_endpoint_is_rejected_before_fallback_is_published():
    with FakeMcpServer(contract='legacy') as legacy, FakeMcpServer(dialect='path') as current:
        client = AetherMindClient(endpoints=[legacy.url, current.url], timeout=1)
        assert client.tree_view('/synthetic-project')['view'] == 'personal history'
        assert client.last_endpoint == current.url
        assert legacy.url not in client._toolsets
        assert set(client._toolsets) == {current.url}
        assert not any(request['method'] == 'tools/call' for request, _ in legacy.requests)


def test_failed_refresh_invalidates_only_its_endpoint():
    with FakeMcpServer(dialect='path') as first, FakeMcpServer(dialect='path') as second:
        client = AetherMindClient(endpoints=[first.url, second.url], timeout=1)
        client.tree_view('/synthetic-project')
        client._probe(second.url, 1)
        second_tools = client._toolsets[second.url]
        first.tool_results['capabilities'] = {'success': True, 'runtime': {}}
        assert client.detect_tools(refresh=True)['overview'] == 'view'
        assert client.last_endpoint == second.url
        assert first.url not in client._toolsets
        assert first.url not in client._layer_types
        assert client._toolsets[second.url] is not second_tools
        assert client._tools is client._toolsets[second.url]


@pytest.mark.parametrize('mutation', ['missing', 'missing_capabilities', 'required', 'type', 'duplicate'])
def test_schema_rejection_precedes_capabilities_and_publication(mutation):
    reference = Path(__file__).resolve().parents[1] / 'docs/reference/aethermind_server_tools.json'
    tools = deepcopy(json.loads(reference.read_text()))
    view = next(tool for tool in tools if tool['name'] == 'view')
    if mutation == 'missing':
        tools.remove(view)
    elif mutation == 'missing_capabilities':
        tools = [tool for tool in tools if tool['name'] != 'capabilities']
    elif mutation == 'required':
        view['inputSchema']['required'].append('unknown')
    elif mutation == 'type':
        view['inputSchema']['properties']['recent_window'] = {'type': 'string'}
    else:
        tools.append(deepcopy(view))
    client = AetherMindClient('http://127.0.0.1:1/mcp')
    client.mcp.list_tools = lambda: tools
    def unexpected_call(*args):
        pytest.fail('Invalid schemas must fail before capabilities')
    client.mcp.call_tool = unexpected_call
    with pytest.raises(McpError):
        client._probe(client._current, .2)
    assert client._tools is None and client._toolsets == {}
    assert client.mcp.timeout == 15


def test_a_missing_store_has_no_capture_to_reconcile():
    client = AetherMindClient('http://127.0.0.1:9/mcp')
    client.read_layers = lambda *args, **kwargs: {
        'success': False,
        'error': 'No AetherMind store exists and creation was not authorized.',
        'error_code': 'AUTO_INIT_BLOCKED',
        'error_type': 'AutoInitBlocked',
    }
    assert client._native_duplicate('/memory', 'req-1', {'ctx': 'tessera/turn', 'body': 'hello'}) is None


def test_a_broken_store_keeps_its_reconcile_error():
    client = AetherMindClient('http://127.0.0.1:9/mcp')
    client.read_layers = lambda *args, **kwargs: {
        'success': False,
        'error': 'store corrupt',
        'error_code': 'STORE_CORRUPT',
    }
    with pytest.raises(McpError, match='Cannot reconcile the native capture reference: store corrupt'):
        client._native_duplicate('/memory', 'req-1', {'ctx': 'tessera/turn', 'body': 'hello'})
