"""Cancellation for owned HTTP requests, including blocked headers and SSE reads."""
import http.client
import socket
import threading
from urllib import request


class CancelledIO(RuntimeError):
    pass


def _host_and_port(host, default_port):
    """Split the ``name:port`` string http.client hands the transport."""
    if host.startswith('['):
        end = host.find(']')
        name = host[1:end]
        rest = host[end + 1:]
        if rest.startswith(':'):
            return name, int(rest[1:])
        return name, default_port
    if ':' in host:
        name, _, text = host.rpartition(':')
        return name, int(text)
    return host, default_port


def _pinned_connector(address, port):
    """A create_connection stand-in that only ever dials the vetted address."""
    def create_connection(ignored_address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        return socket.create_connection((address, port), timeout, source_address)
    return create_connection


class _CancelHTTP(request.HTTPHandler):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def http_open(self, req):
        return self.do_open(self.transport.http_connection, req)


class _CancelHTTPS(request.HTTPSHandler):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def https_open(self, req):
        return self.do_open(self.transport.https_connection, req, context=self._context)


class CancellableHTTP:
    def __init__(self, *handlers, vet_host=None):
        self._cancelled = threading.Event()
        self.cancel_event = None
        # Optional callable(name, port) -> address. It must raise ValueError to refuse.
        self.vet_host = vet_host
        self.response = None
        self._connections = set()
        self._connection_lock = threading.Lock()
        self.opener = request.build_opener(*handlers, _CancelHTTP(self), _CancelHTTPS(self))

    def reset(self):
        self._cancelled.clear()

    def check_cancelled(self):
        if self._cancelled.is_set() or (self.cancel_event is not None and self.cancel_event.is_set()):
            raise CancelledIO('Request cancelled by operator')

    def _connection(self, kind, host, **options):
        self.check_cancelled()
        pinned = None
        if self.vet_host is not None:
            name, port = _host_and_port(host, kind.default_port)
            pinned = self.vet_host(name, port)
        connection = kind(host, **options)
        if pinned is not None:
            # The connection still carries the real hostname for the Host header
            # and for TLS server_hostname; only the socket address is pinned.
            connection._create_connection = _pinned_connector(pinned, connection.port)
        original_connect = connection.connect

        def connect():
            self.check_cancelled()
            original_connect()
            self.check_cancelled()

        connection.connect = connect
        with self._connection_lock:
            self.check_cancelled()
            self._connections.add(connection)
        return connection

    def http_connection(self, host, **options):
        return self._connection(http.client.HTTPConnection, host, **options)

    def https_connection(self, host, **options):
        return self._connection(http.client.HTTPSConnection, host, **options)

    def cancel(self):
        self._cancelled.set()
        with self._connection_lock:
            connections = list(self._connections)
        # shutdown wakes a reader blocked in headers or an SSE line. close alone
        # can wait for that reader's buffer lock and leave Stop stuck behind it.
        for connection in connections:
            sock = connection.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        response = self.response
        try:
            sock = response.fp.raw._sock if response is not None and response.fp is not None else None
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        return True

    def finish(self):
        with self._connection_lock:
            connections = list(self._connections)
            self._connections.clear()
            self.response = None
        for connection in connections:
            connection.close()
