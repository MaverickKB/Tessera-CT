"""A session has a serialized identity beside its slug, and both address it.

Each session also has a unique ID. Before this, the code used
session names, but we should probably serialize those somehow to make coordination
easier for you."

The id already existed in every session's meta.json (15 sessions on the live home,
measured this turn: zero missing, zero duplicates). What did not exist was a way to
use it. Ownership, senders and dispatch all keyed on the slug, which is a label
derived from the title.
"""

import pytest

from tessera.config import Config
from tessera.sessions import SessionManager


def manager(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    return SessionManager(config)


def test_a_session_carries_an_id_beside_its_slug(tmp_path):
    with manager(tmp_path) as sessions:
        sessions.create("Watch the core")
        row = sessions.list_sessions()[0]
        assert row["slug"] == "watch-the-core"
        assert isinstance(row["id"], str) and len(row["id"]) == 32
        assert row["id"] != row["slug"]
        # The same handle is on the status payload a coordinator polls.
        live = sessions.status()["sessions"]["watch-the-core"]
        assert live["id"] == row["id"]


def test_two_sessions_with_the_same_title_have_different_ids(tmp_path):
    """The slug is a label, so a second 'Review' becomes 'review-2'. The id is not."""
    with manager(tmp_path) as sessions:
        sessions.create("Review")
        sessions.create("Review")
        rows = {row["slug"]: row for row in sessions.list_sessions()}
        assert set(rows) == {"review", "review-2"}
        assert rows["review"]["id"] != rows["review-2"]["id"]


def test_a_session_can_be_resolved_by_either_handle(tmp_path):
    with manager(tmp_path) as sessions:
        sessions.create("Dispatch")
        row = sessions.list_sessions()[0]
        assert sessions.resolve_session(row["slug"]) == row["slug"]
        assert sessions.resolve_session(row["id"]) == row["slug"]
        assert sessions.resolve_session("no-such-session") is None
        assert sessions.resolve_session("") is None
        assert sessions.resolve_session(None) is None


def test_a_card_can_be_claimed_and_noted_by_session_id(tmp_path):
    """The point of the handle: coordination names the session, not its title."""
    with manager(tmp_path) as sessions:
        sessions.create("Claiming session")
        sessions.create("Other session")
        row = sessions.list_sessions()[0]
        card = sessions.board.create("Work to claim", row["slug"], 0, 0)
        view = sessions.board_view()
        ordinal = next(c["ordinal"] for c in view["cards"] if c["card_id"] == card["id"])

        claimed = sessions.board_claim(ordinal, view["revision"], owner=row["id"])
        by_id = {c["card_id"]: c for c in claimed["cards"]}
        # Stored as the slug: storage, comparison and wake routing all still key on it.
        assert by_id[card["id"]]["owner"] == row["slug"]

        noted = sessions.board_note(ordinal, claimed["revision"], "a line", actor=row["id"])
        assert "a line" in {c["card_id"]: c for c in noted["cards"]}[card["id"]]["note"]


def test_a_non_session_identity_is_left_alone(tmp_path):
    """The operator is an identity on the board too, and is not a session."""
    with manager(tmp_path) as sessions:
        sessions.create("Worker")
        row = sessions.list_sessions()[0]
        card = sessions.board.create("Operator work", row["slug"], 0, 0)
        view = sessions.board_view()
        ordinal = next(c["ordinal"] for c in view["cards"] if c["card_id"] == card["id"])
        claimed = sessions.board_claim(ordinal, view["revision"], owner="operator")
        assert {c["card_id"]: c for c in claimed["cards"]}[card["id"]]["owner"] == "operator"


def test_an_unknown_handle_is_stored_verbatim_exactly_as_an_unknown_slug_was(tmp_path):
    """Not a refusal, and not a new hazard: this is what an unknown slug already did.

    An owner string that matches the name pattern is accepted whether or not a session
    answers to it, so a typo claims a card for a name nobody holds. That is the state
    before this change and it is preserved deliberately, because tightening it here
    would refuse the operator identity and any person named as a manager.

    Pinned rather than fixed so the next person meets the behavior knowingly: a card
    owned by an unknown handle wakes nobody, which is the failure mode to close when
    the permission layer lands.
    """
    with manager(tmp_path) as sessions:
        sessions.create("Worker")
        row = sessions.list_sessions()[0]
        card = sessions.board.create("Work", row["slug"], 0, 0)
        view = sessions.board_view()
        ordinal = next(c["ordinal"] for c in view["cards"] if c["card_id"] == card["id"])
        claimed = sessions.board_claim(ordinal, view["revision"], owner="00000000000000000000000000000000")
        assert {c["card_id"]: c for c in claimed["cards"]}[card["id"]]["owner"] == "00000000000000000000000000000000"


def test_a_daemon_thread_id_stays_the_slug_while_the_serialized_id_travels_beside_it(tmp_path):
    """The wire contract this change nearly broke.

    A daemon caller does `slug = thread/start()['thread']['id']` and hands that value
    back as `threadId`. Exposing the serialized id on the session row made the old
    `{'id': row['slug'], **row}` spread replace the slug with the uuid, so every
    threadId a caller had been given addressed nothing. Measured: 59 daemon and
    terminal tests failed before the daemon was fixed to keep `id` meaning the thread.
    """
    from tessera.daemon import Daemon
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    daemon = Daemon(config)
    try:
        started = daemon.dispatch("thread/start", {"title": "Wire check"})["thread"]
        assert started["id"] == "wire-check"
        assert started["session_id"] != started["id"]
        # The id the caller was handed is the handle that resolves the thread.
        assert daemon.dispatch("thread/read", {"threadId": started["id"]})["thread"]["id"] == "wire-check"
        listed = daemon.dispatch("thread/list", {})["data"]
        assert [row["id"] for row in listed] == ["wire-check"]
        assert all(row["session_id"] for row in listed)
    finally:
        daemon.close()
