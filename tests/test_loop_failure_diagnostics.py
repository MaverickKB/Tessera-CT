import pytest

from fake_backend import FakeBackend
from tessera.backend import BackendError
from tessera.loop import Session, run_turn

from test_loop import session_config


def test_agent_failure_log_carries_backend_error_category_and_kind(tmp_path):
    """A worker that dies mid-turn must not lose its error category.

    A worker that dies mid-turn must not leave a transcript that records only
    "Turn failed (BackendError)": category and kind were computed, then thrown
    away. sessions.py keeps them for session turns; the loop's own log line
    must carry the same diagnostics so the category is recoverable later.
    """
    backend = FakeBackend([
        BackendError('upstream died mid-stream', category='connection', kind='http',
                    http_status=502, detail='worker stderr tail'),
        BackendError('died again', category='timeout', kind='timeout',
                     detail='stderr showed sk-live-secret-token at the end'),
    ])
    with Session(session_config(tmp_path), backend=backend) as session:
        session.log.add_secrets(['sk-live-secret-token'])
        with pytest.raises(BackendError):
            session.run_turn('Do the work')
        failure = [m for m in session.log.iter() if m.kind == 'agent' and m.text.startswith('Turn failed')][-1]
        assert failure.meta.get('error_category') == 'connection'
        assert failure.meta.get('error_kind') == 'http'
        assert failure.meta.get('http_status') == 502
        # The CLI worker's stderr can carry credential fragments: it must be
        # redacted on the way into the persisted log, as sessions.py does.
        assert failure.meta.get('error_detail') == 'worker stderr tail'
        # And a registered secret inside the detail is redacted, not persisted.
        with pytest.raises(BackendError):
            session.run_turn('Do more work')
        failure2 = [m for m in session.log.iter() if m.kind == 'agent' and m.text.startswith('Turn failed')][-1]
        assert failure2.meta.get('error_detail') == 'stderr showed [REDACTED] at the end'

