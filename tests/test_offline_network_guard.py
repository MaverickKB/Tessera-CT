"""The pytest guard fails a run even if application code catches its rejection."""

import os
from pathlib import Path
import subprocess
import sys
import socket
from types import SimpleNamespace

import pytest


def test_numeric_wildcard_bind_resolution_is_local_and_stays_available():
    addresses = socket.getaddrinfo("0.0.0.0", 0, type=socket.SOCK_STREAM)
    assert addresses and all(address[4][0] == "0.0.0.0" for address in addresses)


def test_loopback_forward_and_reverse_lookups_need_no_os_dns():
    assert socket.gethostbyname("localhost") == "127.0.0.1"
    assert socket.gethostbyname_ex("localhost")[2] == ["127.0.0.1"]
    assert socket.gethostbyaddr("127.0.0.1")[0] == "localhost"
    assert socket.getnameinfo(("127.0.0.1", 0), socket.NI_NUMERICSERV) == ("127.0.0.1", "0")


def test_inet_loopback_guard_does_not_require_unix_socket_support(monkeypatch):
    import conftest
    monkeypatch.delattr(socket, 'AF_UNIX', raising=False)
    conftest.check_address(SimpleNamespace(family=socket.AF_INET), ('127.0.0.1', 80), 'connect')
    conftest.check_address(SimpleNamespace(family=socket.AF_INET6), ('::1', 80, 0, 0), 'connect')


@pytest.mark.parametrize('missing_unix', [False, True])
@pytest.mark.parametrize("operation, host", [
    ("getaddrinfo", "blocked.example.com"), ("connect", "blocked.example.com"),
    ("connect_ex", "blocked.example.com"), ("sendto", "blocked.example.com"),
    ("connect", "0.0.0.0"),
    ("connect", "203.0.113.1"), ("connect", "2001:db8::1"),
    ("gethostbyname", "blocked.example.com"), ("gethostbyname_ex", "blocked.example.com"),
    ("gethostbyaddr", "blocked.example.com"), ("getnameinfo", "blocked.example.com"),
    pytest.param("sendmsg", "blocked.example.com", marks=pytest.mark.skipif(
        not hasattr(socket.socket, "sendmsg"), reason="sendmsg is platform dependent")),
    ("native_getaddrinfo", "blocked.example.com"),
])
def test_caught_external_attempt_still_fails_the_offline_session(tmp_path, operation, host, missing_unix):
    test = tmp_path / "test_blocked_fixture.py"
    test.write_text('''import socket
import _socket
from unittest.mock import Mock
import conftest

if ''' + repr(missing_unix) + ''' and hasattr(socket, 'AF_UNIX'):
    del socket.AF_UNIX

def test_caught_attempt():
    operation = ''' + repr(operation) + '''
    host = ''' + repr(host) + '''
    # A broken guard must still never reach an actual resolver or socket.
    fallback = Mock(side_effect=AssertionError("Physical networking must never execute"))
    original_name = operation.removeprefix("native_")
    conftest._originals[original_name] = fallback
    if original_name in conftest._native_originals:
        conftest._native_originals[original_name] = fallback
    try:
        if operation == "getaddrinfo":
            assert socket.getaddrinfo.__module__ == "conftest"
            socket.getaddrinfo(host, 0)
        elif operation == "native_getaddrinfo":
            assert _socket.getaddrinfo.__module__ == "conftest"
            _socket.getaddrinfo(host, 0)
        elif operation == "getnameinfo":
            socket.getnameinfo((host, 0), 0)
        elif operation in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            getattr(socket, operation)(host)
        else:
            assert getattr(socket.socket, operation).__module__ == "conftest"
            with socket.socket() as sock:
                if operation == "sendto":
                    sock.sendto(b"synthetic", (host, 0))
                elif operation == "sendmsg":
                    sock.sendmsg([b"synthetic"], [], 0, (host, 0))
                else:
                    getattr(sock, operation)((host, 0))
    except AssertionError:
        pass
    fallback.assert_not_called()
''')
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parent)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "conftest",
                             "--confcutdir", str(tmp_path), str(test)], cwd=tmp_path,
                            env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "1 passed" in result.stdout
    assert "External networking blocked: 1 attempted operations" in result.stdout
    assert "blocked.example.com" not in result.stdout + result.stderr
