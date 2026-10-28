"""Delegated actions use the operator's current policy and owning approval UI."""

import time

import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.sessions import SessionManager


def _wait_for_content(path, expected, timeout=5):
    """Wait until `path` holds `expected`, not merely until it exists.

    `resolve()` runs the approved tool on a daemon thread and `write_text`
    creates the inode before it writes the text, so existence is not
    completion. Returns False on timeout so the caller's assert carries the
    real reason instead of an unhandled timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if path.read_text() == expected:
                return True
        except FileNotFoundError:
            pass
        time.sleep(.01)
    return False


@pytest.mark.parametrize('mode,decision', [('deny_risky', True), ('deny_risky', False),
                                          ('ask', True), ('yolo', None)])
def test_child_write_inherits_the_live_parent_policy_and_approval_channel(tmp_path, mode, decision):
    config = session_config(tmp_path)
    config.tools.approval = 'ask' if mode == 'ask' else 'deny_risky'
    config.tools.yolo_switch = True
    config.autonomy.enabled = True
    config.autonomy.tick_seconds = 3600
    backend = FakeBackend([
        {'tool_calls': [{'id': 'write', 'name': 'write_file',
                         'arguments': {'path': 'child.txt', 'text': 'child wrote this'}}]},
        'Tool outcome recorded.',
    ])
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.autonomy.close()
        if mode == 'yolo':
            manager.set_yolo(True)
        slug = manager.create('parent')['slug']
        parent = manager.runtime(slug).session
        parent.agent_backend_factory = lambda: backend
        parent.agents.spawn('writer', 'Write child.txt', tools=['write_file'])
        assert parent.agents.wait(5)
        pending = manager.autonomy.status()['approvals']
        if mode == 'yolo':
            assert not pending
            assert (tmp_path / 'child.txt').read_text() == 'child wrote this'
        else:
            assert not (tmp_path / 'child.txt').exists()
            assert len(pending) == 1
            assert pending[0]['slug'] == slug
            assert pending[0]['tool'] == 'write_file'
            manager.autonomy.resolve(pending[0]['id'], decision, source='card')
            if decision:
                # Wait for the CONTENT, not for the path. `resolve()` runs the
                # approved tool on a daemon thread, and `Path.write_text` creates
                # the inode before it writes the text, so a contended machine
                # leaves a window where the file exists and reads ''.
                assert _wait_for_content(tmp_path / 'child.txt', 'child wrote this')
            else:
                assert not (tmp_path / 'child.txt').exists()
        assert manager.config.tools.approval == mode


def test_the_approval_wait_survives_a_write_that_exists_before_it_has_content(tmp_path, monkeypatch):
    """The wait must cross the window the full-suite run actually hit.

    Forces the window instead of hoping for load: the approved write creates the
    file, leaves it empty, then writes the text. Existence arrives before content
    does, so a wait on `exists()` reads '' -- the exact failure measured in the
    full-suite run at HEAD 8851616 (`assert '' == 'child wrote this'`).

    Two claims, both measured here rather than assumed:
    1. the window really opens (the file exists and reads empty), so this test
       cannot pass trivially if the monkeypatch stops taking effect;
    2. the real test body, run while the window is open, still passes.
    """
    from pathlib import Path

    target = (tmp_path / 'child.txt').resolve()
    opened = []
    original_write_text = Path.write_text

    def slow_write_text(self, data, *args, **kwargs):
        if Path(self).resolve() == target and data:
            original_write_text(self, '', *args, **kwargs)
            opened.append(self.read_text() if Path(self).exists() else None)
            time.sleep(0.3)
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', slow_write_text)

    # Claim 2: the real body, with the window forced open.
    test_child_write_inherits_the_live_parent_policy_and_approval_channel(
        tmp_path, 'ask', True)

    # Claim 1: the window actually opened. Checked after the body ran, so a
    # monkeypatch that silently stopped applying fails this test instead of
    # letting it pass on the unmodified write path.
    assert opened, 'the forced window never opened; this test proves nothing'
    assert opened[0] == '', 'the file already held its text at the window: %r' % (opened[0],)


def test_current_operator_switch_wins_over_the_router_startup_snapshot(tmp_path):
    config = session_config(tmp_path)
    config.tools.approval = 'deny_risky'
    config.tools.yolo_switch = True
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        parent = manager.runtime(manager.create('parent')['slug']).session
        assert parent.router.approval == 'deny_risky'
        manager.set_yolo(True)
        _, record, _ = parent._call_tool({'id': 'write', 'name': 'write_file',
                                         'arguments': {'path': 'live.txt', 'text': 'current mode'}})
        assert record['success']
        assert (tmp_path / 'live.txt').read_text() == 'current mode'
