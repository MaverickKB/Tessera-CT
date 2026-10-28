"""wait_idle must not return in the window between the reply and turn completion.

The reply is appended before turn_end fires, and the runtime's active flag clears
only after the turn_end hooks finish. A helper that returns on the reply alone
lets a caller act on a session the manager still calls busy. test_session_delete
hit exactly that: DELETE refused with 409 'stop it before deleting' on a session
whose turn had visibly answered.

The window is made deterministic here with a turn_end hook that sleeps, so this
test measures the contract rather than the host's load.
"""

import threading
import time

import pytest

from test_message_feedback import wait_idle
from test_server import running_server, call

# Long enough that a reply-only helper cannot help but return inside it.
HOOK_SECONDS = 0.6


def test_wait_idle_returns_only_after_the_turn_end_hooks_finish(tmp_path):
    with running_server(tmp_path) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Hooked'})[1]['slug']
        runtime = server.manager._runtimes.get(slug)
        if runtime is None:
            runtime = server.manager.runtime(slug)

        entered = threading.Event()
        finished = threading.Event()

        def slow_hook(payload):
            entered.set()
            time.sleep(HOOK_SECONDS)
            finished.set()

        runtime.session.hooks.register('turn_end', slow_hook)

        call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'hello'})
        messages = wait_idle(server, slug)

        # The reply is what the caller came for.
        assert any(m['kind'] == 'agent' and m['text'] for m in messages)
        # The point of the test: the hook that runs after the reply had finished.
        assert entered.is_set(), 'the turn_end hook never ran'
        assert finished.is_set(), (
            'wait_idle returned before the turn_end hook finished, so the caller '
            'is acting on a session the manager still calls busy')
        assert runtime.idle(), 'wait_idle returned while the runtime was not idle'


def test_delete_after_wait_idle_is_accepted_when_a_slow_hook_runs(tmp_path):
    """The end-to-end shape of the flake: wait, then delete, and it must succeed."""
    with running_server(tmp_path) as server:
        slug = call(server, 'POST', '/api/sessions', {'title': 'Throwaway'})[1]['slug']
        runtime = server.manager._runtimes.get(slug)
        if runtime is None:
            runtime = server.manager.runtime(slug)
        runtime.session.hooks.register('turn_end', lambda payload: time.sleep(HOOK_SECONDS))

        call(server, 'POST', f'/api/sessions/{slug}/messages', {'text': 'hello'})
        wait_idle(server, slug)

        assert call(server, 'DELETE', f'/api/sessions/{slug}') == (200, {'deleted': slug})
        assert not (tmp_path / 'sessions' / slug).exists()
