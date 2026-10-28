"""A loopback stand-in for AetherMind used by tests and the offline demo.

It does not spawn a process and it does not accept writes, so capture debt
stays in the outbox. It is not an off switch: a session that receives it has
a client, which is what a turn requires.
"""

from contextlib import contextmanager
from pathlib import Path
import time


SENTINEL = 'http://127.0.0.1:1/mcp'


def receipt_less_bundle(tmp_path):
    """An empty directory with no engine files and no build receipt.

    Point ``aethermind.bundled_path`` here when a test must not start an
    engine process. An empty path selects the packaged copy. This directory
    does not. It is not an off switch.
    """
    path = Path(tmp_path) / 'no-engine'
    path.mkdir()
    return path


class QuietMemory:
    def __init__(self, *args, **kwargs):
        self.create_project = kwargs.get('create_project', False)
        self.token_env = kwargs.get('token_env', '')

    def write_layer(self, *args, **kwargs):
        raise RuntimeError('test-local memory keeps the capture in the outbox')

    def prepare_inheritance(self, *args, **kwargs):
        raise RuntimeError('test-local memory has no store')

    def tree_view(self, *args, **kwargs):
        raise RuntimeError('test-local memory has no store')

    def tree_zoom(self, *args, **kwargs):
        raise RuntimeError('test-local memory has no store')

    def read_layers(self, *args, **kwargs):
        raise RuntimeError('test-local memory has no store')

    def detect_tools(self, refresh=False):
        return []


def stand_in(original_ensure, real_client):
    """Launcher and client that answer only when this install would start nothing."""

    def ensure(config, clock=None, *, force=False):
        found = original_ensure(config, time.monotonic if clock is None else clock, force=force)
        if found:
            return found
        memory = config.aethermind
        if memory.url or memory.endpoints or memory.bundled_path:
            return found
        return SENTINEL

    def client(*args, **kwargs):
        url = kwargs.get('url', args[0] if args else '')
        endpoints = list(kwargs.get('endpoints') or [])
        if url == SENTINEL or SENTINEL in endpoints:
            return QuietMemory(*args, **kwargs)
        return real_client(*args, **kwargs)

    return ensure, client


def _quiet_turn_brief(original):
    """The stand-in has no store. A missing brief must not lecture every turn."""

    def turn_brief(self):
        if isinstance(self.client, QuietMemory):
            return ''
        return original(self)

    return turn_brief


def install_test_local_memory(monkeypatch):
    """Install the stand-in through pytest's patch stack.

    A later setattr in the same test undoes first. Installing here, rather
    than by assigning the attribute, keeps that undo from leaving the stand-in
    in place for the next test.
    """
    from tessera import bundled_engine
    from tessera.capture import Capture
    import tessera.loop as loop
    import tessera.mirror as mirror

    ensure, client = stand_in(bundled_engine.ensure_bundled_engine, loop.AetherMindClient)
    monkeypatch.setattr(bundled_engine, 'ensure_bundled_engine', ensure)
    monkeypatch.setattr(loop, 'AetherMindClient', client)
    monkeypatch.setattr(mirror, 'AetherMindClient', client)
    monkeypatch.setattr(Capture, 'turn_brief', _quiet_turn_brief(Capture.turn_brief))


@contextmanager
def test_local_memory():
    """Answer choose_memory with a client when this install would start nothing."""
    from tessera import bundled_engine
    from tessera.capture import Capture
    import tessera.loop as loop
    import tessera.mirror as mirror

    ensure, client = stand_in(bundled_engine.ensure_bundled_engine, loop.AetherMindClient)
    original_ensure = bundled_engine.ensure_bundled_engine
    original_loop = loop.AetherMindClient
    original_mirror = mirror.AetherMindClient
    original_brief = Capture.turn_brief
    bundled_engine.ensure_bundled_engine = ensure
    loop.AetherMindClient = client
    mirror.AetherMindClient = client
    Capture.turn_brief = _quiet_turn_brief(original_brief)
    try:
        yield
    finally:
        bundled_engine.ensure_bundled_engine = original_ensure
        loop.AetherMindClient = original_loop
        mirror.AetherMindClient = original_mirror
        Capture.turn_brief = original_brief
