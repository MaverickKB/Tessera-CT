"""Configured MCP servers ([mcp_servers.<name>]) exposed as session tools.

Each server is an HTTP(S) URL or a stdio argv. Tools are listed from the live server
(cached per process), filtered by include/exclude, and registered as
``<prefix><tool>``. Every tool is risky (approval policy applies) unless it is named in
``safe_tools``. Routes do not filter these tools: a server with ``session_tools`` is
on every route with the same schemas.

A computer-use provider (``[computer_use] provider = "mcp:<name>"``) is registered
once, under ``computer_use.tool_prefix``, and Tessera's own computer-use tools stay
hidden. ``list_apps`` and ``get_app_state`` are read-only. Screenshot image content
is returned for the model; accessibility text is size-capped.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import threading
import time

from .mcp_client import McpClient, McpError
from .mcp_stdio import StdioMcpClient
from .tools import clip_output

_NAME = re.compile(r'[^A-Za-z0-9_-]+')


def open_server(server, timeout=None):
    if server.url:
        return McpClient(server.url, token_env=server.token_env, timeout=timeout or server.timeout)
    return StdioMcpClient(server.command, env=server.env, cwd=server.cwd or None, timeout=timeout or server.timeout)


def resolve_endpoint_factory(config):
    """Client factory for companions: 'mcp:<name>' endpoints use the named server."""
    def factory(endpoint, token_env='', timeout=15):
        if endpoint.startswith('mcp:'):
            # Shared, long-lived client per server (a stdio child is not spawned per session).
            return CATALOG.client(config.mcp_servers[endpoint[4:]])
        return McpClient(endpoint, token_env=token_env, timeout=timeout)
    return factory


# Codex computer use observes and acts in the background. These two never need approval.
READ_ONLY_COMPUTER_TOOLS = frozenset({'list_apps', 'get_app_state'})


def session_plugin_servers(config):
    """MCP servers whose tools belong on a depth-0 session right now.

    ``enabled`` and ``session_tools`` are manual switches. Turning one off keeps
    that plugin's tools off in Safe and in Unleashed. Unleashed does not turn a plugin
    back on. A server selected as the computer-use backend is registered by that
    path instead, under its own prefix.
    """
    servers = {name: server for name, server in config.mcp_servers.items()
               if server.enabled and server.session_tools}
    provider = getattr(getattr(config, 'computer_use', None), 'provider', '') or ''
    if provider.startswith('mcp:') and computer_backend(config) == 'mcp':
        servers.pop(provider.split(':', 1)[1], None)
    return servers


def computer_backend(config):
    """fake, linux, mac, mcp, native, or unsupported.

    An explicit computer_use.backend wins. ``provider = "native"`` selects Tessera's
    background helper, including when an older cua.backend is set. An ``mcp:`` provider
    selects that server, except on Linux where auto keeps the local desktop tools.
    cua.backend then pins fake, linux, or mac for tests. On macOS, auto with an empty
    provider is native. On Windows and any other operating system, auto is
    ``unsupported``: the tools report that they cannot act, they do not pretend to.
    """
    settings = getattr(config, 'computer_use', None)
    choice = settings.backend if settings is not None else 'auto'
    provider = settings.provider if settings is not None else ''
    if choice != 'auto':
        return choice
    linux = sys.platform.startswith('linux')
    if provider == 'native':
        return 'native'
    if provider.startswith('mcp:') and not linux:
        return 'mcp'
    legacy = config.cua.backend
    if legacy != 'auto':
        return legacy
    if provider.startswith('mcp:'):
        return 'linux'
    if sys.platform == 'darwin':
        return 'native'
    if linux:
        return 'linux'
    return 'unsupported'


def tool_name(server, tool, prefix=None):
    if prefix is None:
        prefix = server.tool_prefix if server.tool_prefix else server.name + '__'
    return _NAME.sub('_', prefix + tool)[:64]


def selected(server, tools):
    """Apply include and exclude. An excluded name stays out in Safe and in Unleashed."""
    picked = []
    for tool in tools:
        name = tool.get('name') if isinstance(tool, dict) else None
        if not isinstance(name, str) or not name:
            continue
        if server.include and name not in server.include:
            continue
        if name in server.exclude:
            continue
        picked.append(tool)
    return picked


def memory_endpoint(config, name):
    """Return ``mcp:<name>`` when AetherMind memory uses this server.

    ``resolve_endpoint_factory`` opens that server whether or not ``enabled``
    is set. AetherMind has no off switch, so memory keeps using the server
    when Enabled is off.
    """
    if not isinstance(name, str) or not name or config is None:
        return ''
    endpoints = getattr(getattr(config, 'aethermind', None), 'endpoints', None) or ()
    marker = 'mcp:' + name
    for endpoint in endpoints:
        if endpoint == marker:
            return marker
    return ''


def memory_server_names(*configs):
    """Server names named by any ``aethermind.endpoints`` entry of the form ``mcp:<name>``."""
    names = set()
    for config in configs:
        if config is None:
            continue
        endpoints = getattr(getattr(config, 'aethermind', None), 'endpoints', None) or ()
        for endpoint in endpoints:
            if isinstance(endpoint, str) and endpoint.startswith('mcp:') and endpoint[4:]:
                names.add(endpoint[4:])
    return names


class Catalog:
    """Process-wide tool lists and long-lived clients per server."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lists = {}
        self._clients = {}
        self._lock = threading.RLock()
        self._ready = threading.Condition(self._lock)
        self._loading = set()
        self._errors = {}

    def client(self, server):
        key = (server.name, server.url, tuple(server.command), tuple(sorted(server.env.items())), server.cwd)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = open_server(server)
            return self._clients[key]

    def tools(self, server):
        with self._ready:
            while server.name in self._loading:
                self._ready.wait()
            cached = self._lists.get(server.name)
            if cached and self.clock() - cached[0] < (server.list_ttl if cached[1] is not None else 60):
                return cached[1]
            self._loading.add(server.name)
        try:
            failure = ''
            try:
                tools = selected(server, self.client(server).list_tools())
            except (McpError, OSError, ValueError) as error:
                tools = None
                failure = str(error).strip() or type(error).__name__
            with self._ready:
                self._lists[server.name] = (self.clock(), tools)
                if failure:
                    self._errors[server.name] = failure
                else:
                    self._errors.pop(server.name, None)
            return tools
        finally:
            with self._ready:
                self._loading.remove(server.name)
                self._ready.notify_all()

    def last_error(self, name):
        """The last list failure for one server, or '' when the last list succeeded.

        A missing entry means this process has not asked that server yet. That is
        not the same as a successful empty list.
        """
        with self._lock:
            return self._errors.get(name, '')

    def forget(self, name=None):
        with self._lock:
            if name is None:
                self._lists.clear()
                self._errors.clear()
            else:
                self._lists.pop(name, None)
                self._errors.pop(name, None)

    def close(self, server):
        key = (server.name, server.url, tuple(server.command), tuple(sorted(server.env.items())), server.cwd)
        with self._lock:
            client = self._clients.pop(key, None)
            self._lists.pop(server.name, None)
        self._close_client(client)

    def close_named(self, name):
        """Drop every cached client for one server name, including a command that changed."""
        with self._lock:
            keys = [key for key in self._clients if key and key[0] == name]
            clients = [self._clients.pop(key) for key in keys]
            self._lists.pop(name, None)
        for client in clients:
            self._close_client(client)

    def cached_client_count(self, name):
        """How many live clients this process still holds for one server name."""
        with self._lock:
            return sum(1 for key in self._clients if key and key[0] == name)

    def close_all(self):
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._lists.clear()
        for client in clients:
            self._close_client(client)

    @staticmethod
    def _close_client(client):
        closer = getattr(client, 'close', None)
        if closer is None:
            return
        try:
            closer()
        except (OSError, RuntimeError) as error:
            text = str(error).replace('\n', ' ').strip()
            label = type(error).__name__
            message = f'{label}: {text}' if text and text != label else label
            print(f'mcp: closing a plugin client failed ({message[:300]})',
                  file=sys.stderr, flush=True)

    def inspect(self, server, *, keep=False):
        """List tools from the live server. Returns ``(tools, error)``.

        ``error`` is the exception type and message. It is not a success, and a
        failure is not cached as an empty tool list. ``keep`` leaves the client
        running. A server named by ``aethermind.endpoints`` uses that, so Check
        and edit leave the memory process running.
        """
        if not keep:
            self.close_named(server.name)
        try:
            tools = self.client(server).list_tools()
        except (McpError, OSError, ValueError) as error:
            if not keep:
                self.close_named(server.name)
            return None, f'{type(error).__name__}: {error}'[:300]
        if not isinstance(tools, list):
            return None, 'MCP tools/list returned an invalid tool list'
        return tools, None


CATALOG = Catalog()


def _data_url(item):
    data = item.get('data')
    mime = item.get('mimeType') or item.get('mime_type') or 'image/png'
    source = item.get('source')
    if not isinstance(data, str) or not data.strip():
        if isinstance(source, dict):
            data = source.get('data')
            mime = source.get('media_type') or mime
    if not isinstance(data, str):
        return None
    data = ''.join(data.split())
    if not data:
        return None
    if data.startswith('data:'):
        return data
    if not isinstance(mime, str) or not mime:
        mime = 'image/png'
    return f'data:{mime};base64,{data}'


def project_mcp_result(result, text_limit):
    """Text plus at most the images the server returned. Text is capped; images are not flattened away."""
    texts = []
    images = []
    if isinstance(result, dict):
        for item in result.get('content') or []:
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'text' and isinstance(item.get('text'), str):
                texts.append(item['text'])
            elif item.get('type') == 'image':
                url = _data_url(item)
                if url:
                    images.append({'caption': 'Computer use screenshot', 'data_url': url})
        if not texts and 'structuredContent' in result:
            texts.append(json.dumps(result['structuredContent'], ensure_ascii=False, sort_keys=True))
    text = '\n'.join(texts)
    if text_limit is not None:
        text = clip_output(text, text_limit)
    if not images:
        return text
    payload = {'text': text, 'image': images[0]}
    if len(images) > 1:
        payload['images'] = images[1:]
    return payload


def _handler(catalog, server, remote_tool, clip_chars, text_limit=None, registry=None):
    limit = clip_chars if text_limit is None else text_limit

    def handler(**arguments):
        try:
            client = catalog.client(server)
            cancellable = getattr(client, 'call_tool_cancellable', None)
            if registry is not None and callable(cancellable):
                result = cancellable(remote_tool, arguments, registry)
            else:
                result = client.call_tool(remote_tool, arguments)
        except McpError as error:
            return {'error': f'{server.name}: {error}'}
        return project_mcp_result(result, limit)
    return handler


def register_mcp_server_tools(registry, servers, clip_chars, catalog=None, listed=None):
    catalog = catalog or CATALOG
    registered = []
    enabled = [server for server in servers.values() if server.enabled]
    if not enabled:
        return registered
    # A name already inspected (including an empty list) is not fetched again.
    # The rest are independent, so they are discovered together and then
    # registered in configured order.
    known = {}
    discover = []
    for server in enabled:
        if listed is not None and server.name in listed:
            raw = listed[server.name]
            known[server.name] = selected(server, raw) if raw else []
        else:
            discover.append(server)
    if discover:
        with ThreadPoolExecutor(max_workers=len(discover)) as discovery:
            found = list(discovery.map(catalog.tools, discover))
        for server, tools in zip(discover, found):
            known[server.name] = tools
    for server in enabled:
        tools = known.get(server.name) or []
        for tool in tools or []:
            name = tool_name(server, tool['name'])
            schema = tool.get('inputSchema') if isinstance(tool.get('inputSchema'), dict) else {}
            schema = {**schema, 'type': 'object'}
            schema.setdefault('properties', {})
            description = (tool.get('description') or tool['name']).strip().split('\n')[0][:300]

            handler = _handler(catalog, server, tool['name'], clip_chars, registry=registry)

            try:
                registry.register(name, f'[{server.name}] {description}', schema, handler,
                                  risky=tool['name'] not in server.safe_tools, origin='mcp')
            except ValueError:
                continue  # Name collision with a built-in tool: the built-in wins.
            registered.append(name)
    return registered


def reload_session_plugins(session, catalog=None, prepared=None, close_names=(), *, pending_close=None):
    """Replace one session's plugin tools from its current config.

    ``prepared`` maps a server name to a tool list already fetched. Passing it
    keeps the registry update off the network. ``close_names`` are servers whose
    old process should be dropped. A name present in ``prepared`` with a tool
    list keeps the client inspect just opened.

    Closing a stdio client waits on the lock the running call holds. Pass
    ``pending_close`` to record those names and close them after releasing the
    manager lock, so Stop can cancel the call. Returns the registered names.
    """
    if getattr(session, 'depth', 0) != 0:
        return []
    registry = getattr(session, 'tools', None)
    if registry is None:
        return []
    catalog = catalog or CATALOG
    for name in list(getattr(session, '_plugin_tools', ())):
        registry._tools.pop(name, None)
    prepared = prepared or {}
    closing = [name for name in close_names if not prepared.get(name)]
    if pending_close is not None:
        pending_close.extend(closing)
    else:
        for name in closing:
            catalog.close_named(name)
    servers = session_plugin_servers(session.config)
    registered = register_mcp_server_tools(
        registry, servers, session.config.tools.clip_chars, catalog, listed=prepared)
    session._plugin_tools = tuple(registered)
    session._plugin_servers = tuple(session.config.mcp_servers)
    session.tool_ceiling = set(registry.names())
    return registered


def reload_installed_plugins(manager, catalog=None, only=None):
    """Refresh plugin tools on open depth-0 sessions.

    Tool lists for every server ``apply`` will register are fetched before the
    manager lock is held, including servers outside ``only``. ``only`` limits
    which servers are inspected for a status report. The locked section only
    updates registries. Stdio shutdown waits on the client lock a running call
    holds, and Stop needs the manager lock to cancel that call, so the
    processes are closed after the lock is released. A server named by
    ``aethermind.endpoints`` is left running: memory has no off switch, and
    closing it would only make the next memory call start it again. Returns
    ``(statuses, error)``. ``statuses`` covers the servers in ``only`` (or
    every offered server when ``only`` is omitted). ``error`` is set when a
    session registry could not be updated.
    """
    catalog = catalog or CATALOG
    lock = getattr(manager, '_lock', None)

    def snapshot():
        sessions = []
        for runtime in getattr(manager, '_runtimes', {}).values():
            session = getattr(runtime, 'session', None)
            if session is not None and getattr(session, 'depth', 0) == 0 and getattr(session, 'tools', None) is not None:
                sessions.append(session)
        return sessions

    if lock is None:
        sessions = snapshot()
    else:
        with lock:
            sessions = snapshot()
    memory_names = memory_server_names(
        getattr(manager, 'config', None),
        *[getattr(session, 'config', None) for session in sessions])
    prepared = {}
    statuses = {}
    for session in sessions:
        for server in session_plugin_servers(session.config).values():
            if server.name in prepared:
                continue
            if only is not None and server.name not in only:
                continue
            tools, error = catalog.inspect(server, keep=server.name in memory_names)
            prepared[server.name] = tools
            picked = selected(server, tools or [])
            registered = [tool_name(server, tool['name']) for tool in picked
                          if isinstance(tool, dict) and isinstance(tool.get('name'), str)]
            if error:
                statuses[server.name] = {'ok': False, 'state': 'error', 'detail': error, 'last_error': error,
                                         'tools': 0, 'registered': []}
            else:
                detail = f'Connected, {len(registered)} tools.'
                statuses[server.name] = {'ok': True, 'state': 'connected', 'detail': detail, 'last_error': '',
                                         'tools': len(registered), 'registered': registered}
    # apply() re-registers every enabled session plugin, not only `only`.
    # Discover the rest here. catalog.tools() can spawn stdio, and it must not
    # run while the manager lock is held.
    pending = []
    seen_pending = set(prepared)
    for session in sessions:
        for server in session_plugin_servers(session.config).values():
            if server.name in seen_pending:
                continue
            seen_pending.add(server.name)
            pending.append(server)
    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as discovery:
            found = list(discovery.map(catalog.tools, pending))
        for server, tools in zip(pending, found):
            prepared[server.name] = tools
    # Servers configured but not offered to sessions still need a fresh answer
    # when something asks. Callers probe those themselves.
    failures = []
    pending_close = []

    def apply():
        for session in sessions:
            try:
                previous = set(getattr(session, '_plugin_servers', ()))
                current = set(getattr(session.config, 'mcp_servers', {}))
                focus = set(only) if only is not None else current
                # A memory server stays in the catalog. Closing it drops the
                # process AetherMind already holds, and the next memory call
                # starts that same client again.
                closing = ((previous - current) | focus) - memory_names
                reload_session_plugins(session, catalog, prepared, closing, pending_close=pending_close)
            except (RuntimeError, ValueError, KeyError) as error:
                failures.append(f'{type(error).__name__}: {error}'[:200])

    if lock is None:
        apply()
    else:
        with lock:
            apply()
    seen = set()
    for name in pending_close:
        if name in seen:
            continue
        seen.add(name)
        catalog.close_named(name)
    return statuses, '; '.join(failures)


def register_computer_use_tools(registry, config, clip_chars, catalog=None):
    """Expose the designated MCP computer-use server under one prefix.

    ``session_tools`` does not hide this server: the provider setting is the switch.
    Read-only tools run without an approval card. The rest follow Safe or Unleashed.
    """
    provider = config.computer_use.provider
    if not provider.startswith('mcp:'):
        return []
    server = config.mcp_servers.get(provider.split(':', 1)[1])
    if server is None or not server.enabled:
        return []
    catalog = catalog or CATALOG
    prefix = config.computer_use.tool_prefix
    text_limit = min(config.computer_use.text_chars, max(64, clip_chars - 80))
    registered = []
    for tool in catalog.tools(server) or []:
        remote = tool['name']
        name = tool_name(server, remote, prefix)
        schema = tool.get('inputSchema') if isinstance(tool.get('inputSchema'), dict) else {}
        schema = {**schema, 'type': 'object'}
        schema.setdefault('properties', {})
        description = (tool.get('description') or remote).strip().split('\n')[0][:300]
        handler = _handler(catalog, server, remote, clip_chars, text_limit=text_limit, registry=registry)
        risky = remote not in READ_ONLY_COMPUTER_TOOLS and remote not in server.safe_tools
        try:
            registry.register(name, f'[computer use] {description}', schema, handler, risky=risky, origin='mcp')
        except ValueError:
            continue
        registered.append(name)
    return registered
