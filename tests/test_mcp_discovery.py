"""Independent integrations discover together; sessions share each discovery."""
from concurrent.futures import ThreadPoolExecutor
import threading

from tessera.config import McpServerConfig
from tessera.mcp_servers import Catalog, register_mcp_server_tools


def test_independent_servers_discover_concurrently_and_register_in_order():
    barrier = threading.Barrier(2)
    registered = []

    class LiveCatalog:
        def tools(self, server):
            barrier.wait(timeout=2)
            return [{'name': 'read', 'inputSchema': {}}]

    class Registry:
        def register(self, name, *args, **kwargs):
            registered.append(name)

    servers = {}
    for name in ('first', 'second'):
        servers[name] = McpServerConfig(name=name)
    names = register_mcp_server_tools(Registry(), servers, 1000, catalog=LiveCatalog())
    assert names == ['first__read', 'second__read']
    assert registered == names


def test_concurrent_sessions_share_the_same_discovery():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Client:
        def list_tools(self):
            calls.append('list')
            entered.set()
            assert release.wait(5)
            return [{'name': 'read'}]

    catalog = Catalog()
    catalog.client = lambda server: Client()
    server = McpServerConfig(name='shared')
    with ThreadPoolExecutor() as executor:
        first = executor.submit(catalog.tools, server)
        assert entered.wait(2)
        second = executor.submit(catalog.tools, server)
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2)
    assert calls == ['list']
