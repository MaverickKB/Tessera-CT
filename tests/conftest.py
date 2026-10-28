"""Offline pytest: permit local fake servers, block external socket traffic."""

import base64
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import _socket
import socket
import sys
import time
from urllib.parse import urlsplit

import pytest


_originals = {}
_native_originals = {}
_attempts = []


def local_host(host):
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def reject(operation):
    # Never retain the attempted address, which may contain private host data.
    _attempts.append(operation)
    raise AssertionError("External networking is disabled in offline pytest")


def check_address(sock, address, operation):
    if sock.family == socket.AF_UNIX:
        return
    if not isinstance(address, tuple) or not address or not local_host(address[0]):
        reject(operation)


def guarded_connect(sock, address):
    check_address(sock, address, "connect")
    return _originals["connect"](sock, address)


def guarded_connect_ex(sock, address):
    check_address(sock, address, "connect_ex")
    return _originals["connect_ex"](sock, address)


def guarded_sendto(sock, data, *args):
    check_address(sock, args[-1], "sendto")
    return _originals["sendto"](sock, data, *args)


def guarded_sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
    if address is None:
        return _originals["sendmsg"](sock, buffers, ancdata, flags)
    check_address(sock, address, "sendmsg")
    return _originals["sendmsg"](sock, buffers, ancdata, flags, address)


def local_ipv4(host, operation):
    if not local_host(host):
        reject(operation)
    address = "127.0.0.1" if host == "localhost" else host
    if ipaddress.ip_address(address).version != 4:
        raise socket.gaierror("Offline IPv4 lookup requires an IPv4 loopback address")
    return address


def guarded_gethostbyname(host):
    return local_ipv4(host, "gethostbyname")


def guarded_gethostbyname_ex(host):
    return host, [], [local_ipv4(host, "gethostbyname_ex")]


def guarded_gethostbyaddr(host):
    if not local_host(host):
        reject("gethostbyaddr")
    address = "127.0.0.1" if host == "localhost" else host
    return "localhost", [], [address]


def guarded_getnameinfo(address, flags):
    if not isinstance(address, tuple) or not address or not local_host(address[0]):
        reject("getnameinfo")
    return _originals["getnameinfo"](address, flags | socket.NI_NUMERICHOST)


def guarded_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host is not None and host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            reject("getaddrinfo")
        # Resolving numeric bind addresses emits no DNS; outbound checks still
        # reject every non-loopback destination before a physical socket call.
        flags |= socket.AI_NUMERICHOST
    return _native_originals["getaddrinfo"](host, port, family, type, proto, flags)


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'aethermind_unavailable: do not attach the test-local memory engine',
    )
    _attempts.clear()
    methods = {"connect": guarded_connect, "connect_ex": guarded_connect_ex, "sendto": guarded_sendto}
    if hasattr(socket.socket, "sendmsg"):
        methods["sendmsg"] = guarded_sendmsg
    for name in methods:
        _originals[name] = getattr(socket.socket, name)
    functions = {"getaddrinfo": guarded_getaddrinfo, "gethostbyname": guarded_gethostbyname,
                 "gethostbyname_ex": guarded_gethostbyname_ex, "gethostbyaddr": guarded_gethostbyaddr,
                 "getnameinfo": guarded_getnameinfo}
    for name in functions:
        _originals[name] = getattr(socket, name)
        _native_originals[name] = getattr(_socket, name)
    for name, guard in methods.items():
        setattr(socket.socket, name, guard)
    for name, guard in functions.items():
        setattr(socket, name, guard)
        setattr(_socket, name, guard)


def pytest_sessionfinish(session, exitstatus):
    if _attempts:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "External networking blocked: " + str(len(_attempts)) + " attempted operations")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_unconfigure(config):
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if name in _originals:
            setattr(socket.socket, name, _originals[name])
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        if name in _originals:
            setattr(socket, name, _originals[name])
            setattr(_socket, name, _native_originals[name])


# load_config() lets any TESSERA_* env var override the config file, so a
# suite run from a session that inherited TESSERA_SERVER_HOST, PORT, or
# ALLOWED_HOSTS would read that deployment into tests that wrote port=0
# into tmp_path. Isolation is per test, so a test that wants one of them
# sets it with monkeypatch.
@pytest.fixture(autouse=True)
def _settings_probe_skips_remote_hosts(monkeypatch):
    """Settings tests inject the model-list probe. Production does not look at pytest.

    A non-loopback probe would be recorded as external networking even when the
    caller catches the error. Loopback probes still hit the real function.
    """
    from tessera import settings_model
    real = settings_model._probe_models

    def probe(url):
        host = (urlsplit(url or '').hostname or '').strip('[]').lower()
        if host not in ('localhost', '127.0.0.1', '::1'):
            return 'Unreachable', ''
        return real(url)

    monkeypatch.setattr(settings_model, '_probe_models', probe)


@pytest.fixture(autouse=True)
def _clear_aethermind_choice_cache():
    """choose_memory must not keep another test's URL for the life of the process."""
    from tessera.bundled_engine import clear_memory_choices
    clear_memory_choices()
    yield
    clear_memory_choices()


def _load_local_memory():
    """Load the stand-in that sits next to this plugin.

    A fresh pytest can import this plugin by name without keeping this
    directory on the path a later import searches. Reading the sibling file
    does not depend on that path.
    """
    path = Path(__file__).resolve().with_name('local_memory.py')
    loaded = sys.modules.get('local_memory')
    if loaded is not None:
        loaded_file = getattr(loaded, '__file__', None)
        if loaded_file is not None and Path(loaded_file).resolve() == path:
            return loaded
        raise ImportError('local_memory resolved to ' + str(loaded_file) + ' instead of ' + str(path))
    spec = importlib.util.spec_from_file_location('local_memory', path)
    if spec is None or spec.loader is None:
        raise ImportError('test-local memory stand-in is missing at ' + str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules['local_memory'] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop('local_memory', None)
        raise
    return module


@pytest.fixture(autouse=True)
def _test_local_aethermind(request, monkeypatch):
    """Turns need a working AetherMind. This stand-in is not an off switch.

    Tests that measure a missing engine opt out with aethermind_unavailable.
    A configured URL, endpoint list, or bundled_path is left to the real launcher.
    The patch goes through monkeypatch so a test that replaces the launcher
    cannot leave this stand-in installed for the next test.
    """
    if request.node.get_closest_marker('aethermind_unavailable'):
        yield
        return
    _load_local_memory().install_test_local_memory(monkeypatch)
    yield


@pytest.fixture(autouse=True)
def _isolate_deployment_env(monkeypatch):
    for name in [key for key in os.environ if key.startswith('TESSERA_')]:
        monkeypatch.delenv(name, raising=False)


def _synthetic_jwt(claims):
    def encode(value):
        raw = json.dumps(value).encode('utf-8')
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    return encode({'alg': 'HS256', 'typ': 'JWT'}) + '.' + encode(claims) + '.synthetic'


@pytest.fixture(autouse=True)
def _codex_grant_when_this_host_has_none(monkeypatch, tmp_path_factory):
    """Codex CLI tests spawn a fake binary. They still pass the grant check first.

    A developer machine with a live ~/.codex/auth.json keeps that file. A host
    with no grant, including CI, gets a synthetic unexpired JWT so the check
    does not pretend the fake CLI failed to start. An explicit auth path is
    left alone, and a default path that already resolves stays the one read.
    """
    from tessera.codex_grant import grant_state
    if grant_state()['state'] == 'ok':
        yield
        return
    home = tmp_path_factory.mktemp('codex-grant')
    directory = home / '.codex'
    directory.mkdir()
    token = _synthetic_jwt({'exp': int(time.time()) + 3600})
    path = directory / 'auth.json'
    path.write_text(json.dumps({'tokens': {'access_token': token}}), encoding='utf-8')
    real = grant_state

    def wrapped(auth_path=None, *, now=None):
        state = real(auth_path, now=now)
        if auth_path is None and state['state'] != 'ok':
            return real(path, now=now)
        return state

    monkeypatch.setattr('tessera.codex_grant.grant_state', wrapped)
    yield
