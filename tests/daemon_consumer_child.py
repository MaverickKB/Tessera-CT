"""Test-only real daemon process with FakeBackend and forbidden-capability guards."""

import atexit
import builtins
import os
from pathlib import Path
import re
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fake_backend import FakeBackend
from tessera.aethermind import AetherMindClient
from tessera.backend import Backend
from tessera.config import Config
from tessera.credentials import CredentialResolver


attempts = []


def reject(*args, **kwargs):
    attempts.append(True)
    raise AssertionError("Daemon fixture forbids listeners, external providers and native helpers")


def finish():
    if attempts:
        sys.stderr.write("Forbidden capability attempted in daemon fixture\n")
        sys.stderr.flush()
        os._exit(75)


def guarded_import(name, *args, **kwargs):
    if name == "tessera.server" or name.split(".")[0] in {"AppKit", "Foundation", "Cocoa", "objc"}:
        reject()
    return original_import(name, *args, **kwargs)


def answer(messages, tools):
    if messages[-1]["role"] == "tool":
        output = messages[-1]["content"]
        if output.startswith(("Blocked:", "Tool error")):
            return {"content": "Tool not run: approval denied"}
        return {"content": "Approved fixture complete"}
    text = messages[-1].get("content", "")
    if "write_file" in text:
        return {"tool_calls": [{"id": "write-placeholder", "name": "write_file",
                                 "arguments": {"path": "approved-fixture.txt", "text": "fixture write"}}]}
    if text == "Recover the earlier fixture fact from saved context":
        for message in messages[:-1]:
            match = re.search(r"acceptance-fact:[a-f0-9]{32}", str(message.get("content", "")))
            if match:
                return {"content": "Recovered " + match.group()}
        raise AssertionError("Saved context did not reach the backend after restart")
    return {"content": "Recorded " + text}


def backend_factory(*args):
    return FakeBackend([answer] * 20)


if __name__ == "__main__":
    atexit.register(finish)
    original_import = builtins.__import__
    builtins.__import__ = guarded_import
    for method in ("connect", "connect_ex", "bind", "listen", "sendto"):
        setattr(socket.socket, method, reject)
    socket.getaddrinfo = reject
    CredentialResolver.resolve = reject
    AetherMindClient.__init__ = reject
    Backend.__init__ = reject

    from tessera.daemon import Daemon
    from tessera.rpc import serve
    from local_memory import test_local_memory

    home = Path(sys.argv[1])
    home.mkdir(parents=True, exist_ok=True)
    with (home / "daemon-pids-fixture.txt").open("a") as stream:
        stream.write(str(os.getpid()) + "\n")
    config = Config(home=home, cwd=home)
    config.jev.enabled = False
    config.tools.approval = "ask"
    # The guard above rejects a real AetherMind client. This stand-in keeps the
    # turn attached to memory without opening a socket or spawning an engine.
    with test_local_memory():
        serve(Daemon(config, backend_factory=backend_factory))
