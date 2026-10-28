"""Real Python desktop child lifecycle with empty config and outbound tripwires."""

from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CHILD = r"""
import atexit
import ipaddress
import os
import runpy
import socket
import sys
from tessera.credentials import CredentialResolver

attempts = []
def reject(*args, **kwargs):
    attempts.append(True)
    raise AssertionError('Desktop startup cannot resolve credentials or make outbound calls')

def check_attempts():
    if attempts:
        sys.stderr.write('Desktop startup attempted a forbidden operation\n')
        sys.stderr.flush()
        os._exit(75)

atexit.register(check_attempts)
lookup = socket.getaddrinfo
def loopback_lookup(host, *args, **kwargs):
    try:
        local = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        reject()
    return lookup(host, *args, **kwargs)

def loopback_reverse(host):
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local:
        reject()
    return 'localhost', [], [host]

CredentialResolver.resolve = reject
socket.socket.connect = reject
socket.socket.connect_ex = reject
socket.socket.sendto = reject
socket.getaddrinfo = loopback_lookup
socket.gethostbyname = reject
socket.gethostbyname_ex = reject
socket.gethostbyaddr = loopback_reverse
sys.argv = ['tessera', *sys.argv[1:]]
runpy.run_module('tessera', run_name='__main__')
"""


@contextmanager
def desktop_child(tmp_path, *, host="127.0.0.1"):
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("TESSERA_") and name not in {
                       "AETHERMIND_TOKEN", "TYPESAFE_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                       "http_proxy", "https_proxy", "all_proxy",
                   }}
    # An obsolete configured HTTP key must neither be needed nor printed.
    environment["DESKTOP_KEY_PLACEHOLDER"] = "synthetic-desktop-key-placeholder"
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[server]\ntoken_env = "DESKTOP_KEY_PLACEHOLDER"\n')
    command = [sys.executable, "-u", "-c", CHILD, "--home", str(home), "desktop", "--host", host, "--port", "0"]
    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def ready_record(process):
    pending = b""
    deadline = time.monotonic() + 30
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while b"\n" not in pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Desktop core did not emit its ready record"
            assert selector.select(remaining), "Desktop core did not emit its ready record"
            chunk = os.read(process.stdout.fileno(), 4096)
            assert chunk, "Desktop core exited before readiness"
            pending += chunk
            assert len(pending) <= 65536, "Desktop core readiness must remain bounded"
    line, remainder = pending.split(b"\n", 1)
    assert remainder == b"", "Only the readiness record belongs on the launch protocol"
    return json.loads(line)


def test_desktop_cli_ready_api_and_parent_pipe_shutdown_without_a_bearer(tmp_path):
    with desktop_child(tmp_path) as process:
        report = ready_record(process)
        assert set(report) == {"type", "version", "transport", "url", "pid"}
        assert report["type"] == "tessera.ready" and report["version"] == 1
        assert report["transport"] == "loopback-http" and report["pid"] == process.pid
        assert "synthetic-desktop-key-placeholder" not in json.dumps(report)
        assert str(tmp_path) not in json.dumps(report)
        address = urlsplit(report["url"])
        assert address.scheme == "http" and address.hostname == "127.0.0.1"
        assert address.port and not address.username and not address.password
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            connection.request("GET", "/api/status", headers={"Origin": f"http://{address.hostname}:{address.port}"})
            response = connection.getresponse()
            assert response.status == 200
            assert "sessions" in json.loads(response.read())
        finally:
            connection.close()
        process.stdin.close()
        process.stdin = None
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0
        assert output == b"" and error == b""
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex((address.hostname, address.port)) != 0


def test_desktop_cli_refuses_remote_binding_even_with_an_old_server_key(tmp_path):
    with desktop_child(tmp_path, host="0.0.0.0") as process:
        output, error = process.communicate(timeout=10)
        assert process.returncode != 0
        assert b"tessera.ready" not in output
        assert b"loopback" in error.lower()
        assert b"synthetic-desktop-key-placeholder" not in output + error
        assert str(tmp_path).encode() not in output + error
