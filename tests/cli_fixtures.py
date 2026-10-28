"""Keep synthetic CLI startup separate from the call's transport deadline."""

from pathlib import Path
import base64
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from tessera import cli_worker


def synthetic_codex_auth(directory):
    """Write a future-dated synthetic access JWT and return its path.

    Codex chat reads this file when a test passes it as ``auth_path``. The
    grant precheck must not open the developer home's ``~/.codex/auth.json``.
    """
    expires = int(time.time()) + 3600

    def encode(value):
        raw = json.dumps(value).encode('utf-8')
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')

    token = encode({'alg': 'none', 'typ': 'JWT'}) + '.' + encode({'exp': expires}) + '.synthetic'
    path = Path(directory) / 'codex-auth.json'
    path.write_text(json.dumps({'tokens': {'access_token': token}}), encoding='utf-8')
    return str(path)


@pytest.fixture(params=[False, True], ids=["normal-start", "contended-start"])
def cli_dispatch(monkeypatch, request):
    """Model a scheduler pause before the child's first pipe event, without sleeping."""
    if not request.param:
        return
    clock = time.monotonic
    selector = cli_worker.selectors.DefaultSelector
    displacement = [0.0]

    class ContendedSelector(selector):
        def select(self, timeout=None):
            if not displacement[0]:
                displacement[0] = 4.0
            return super().select(timeout)

    monkeypatch.setattr(cli_worker, "time", SimpleNamespace(monotonic=lambda: clock() + displacement[0]))
    monkeypatch.setattr(cli_worker, "selectors", SimpleNamespace(
        DefaultSelector=ContendedSelector, EVENT_READ=cli_worker.selectors.EVENT_READ,
        EVENT_WRITE=cli_worker.selectors.EVENT_WRITE))


def synchronize_cli_start(monkeypatch, command):
    """Consume a ready byte after imports, then run the unmodified collector."""
    path = Path(command)
    source = path.read_text()
    assert source.startswith("#!" + sys.executable + "\n")
    # Both generated fixtures explicitly read their task from stdin.
    reader = next(line for line in source.splitlines() if line.startswith(("prompt=", "prompt =")))
    assert "sys.stdin.read()" in reader
    ready = 'sys.stderr.buffer.write(b"\\0")\nsys.stderr.buffer.flush()\n'
    path.write_text(source.replace(reader, ready + reader, 1))
    collect = cli_worker.CliWorker._collect

    def after_ready(worker, process, prompt):
        if worker.settings.command == str(command):
            with cli_worker.selectors.DefaultSelector() as selector:
                selector.register(process.stderr, cli_worker.selectors.EVENT_READ)
                assert selector.select(10), "Synthetic CLI did not become ready"
                assert os.read(process.stderr.fileno(), 1) == b"\0", "Synthetic CLI readiness failed"
        return collect(worker, process, prompt)

    monkeypatch.setattr(cli_worker.CliWorker, "_collect", after_ready)
