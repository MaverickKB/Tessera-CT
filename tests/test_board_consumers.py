"""Independent offline consumers of the optional work-in-progress board."""

import io
import json
import os
from unittest.mock import Mock

import pytest

from tessera.backend import Backend
from tessera.board import BoardStore
from tessera.config import Config
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from tessera.sessions import SessionManager
from fake_backend import FakeBackend
from test_server import call
from test_status_projection_consumers import disk_snapshot, forbid_active_work, status_server


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    tripwires = []
    for owner, name in ((CredentialResolver, "resolve"), (Backend, "__init__"),
                        (JudgeKit, "_evaluate_batch")):
        tripwire = Mock(side_effect=AssertionError("Board consumers must remain offline"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
        tripwire = Mock(side_effect=AssertionError("Board consumers must not contact or spawn integrations"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


def forbid_artifact_reads(monkeypatch, artifact):
    """Allow board storage, but reject opening or inspecting the artifact reference."""
    expected = os.fspath(artifact)
    inspected = []

    def guard(function):
        def checked(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path) == expected:
                inspected.append(expected)
                raise AssertionError("Artifact references must not be resolved")
            return function(path, *args, **kwargs)
        return checked

    monkeypatch.setattr(io, "open", guard(io.open))
    monkeypatch.setattr("builtins.open", guard(open))
    monkeypatch.setattr(os, "open", guard(os.open))
    monkeypatch.setattr(os, "stat", guard(os.stat))
    monkeypatch.setattr(os, "lstat", guard(os.lstat))
    return inspected


@pytest.mark.parametrize("operation,args", [
    ("create", ("Inspect synthetic source", "one", 0, 1)),
    ("update_status", ("card-placeholder", "running")),
    ("attach_artifact", ("card-placeholder", "artifact-placeholder")),
    ("ask_human", ("card-placeholder", "Choose the synthetic source?")),
])
def test_disabled_board_mutations_are_inert_before_time_or_storage(tmp_path, operation, args):
    from tessera.board import BoardError, BoardStore

    def unused_clock():
        raise AssertionError("Disabled board must not consult time")

    root = tmp_path / "absent"
    board = BoardStore(root / "board.jsonl", clock=unused_clock)
    assert board.list() == []
    with pytest.raises(BoardError, match="disabled"):
        getattr(board, operation)(*args)
    assert not root.exists()


def test_card_versions_fence_stale_cross_instance_writes_without_losing_state(tmp_path):
    from tessera.board import BoardError, BoardStore

    path = tmp_path / "board.jsonl"
    left = BoardStore(path, enabled=True)
    card = left.create("Inspect synthetic source", "one", 2, 4, card_id="card-placeholder")
    right = BoardStore(path, enabled=True)
    stale = right.get(card["id"])
    updated = left.update_status(card["id"], "running", attempt=1, actor="one", expected_version=1)
    assert updated["version"] == 2
    before = path.read_bytes()
    with pytest.raises(BoardError):
        right.attach_artifact(card["id"], "stale-artifact", actor="one", expected_version=stale["version"])
    with pytest.raises(BoardError):
        right.update_status(card["id"], "succeeded", actor="two", expected_version=2)
    assert path.read_bytes() == before
    restored = BoardStore(path, enabled=True).get(card["id"])
    assert (restored["status"], restored["attempt"], restored["version"]) == ("running", 1, 2)
    assert restored["artifacts"] == []


def test_artifact_refs_remain_opaque_local_and_url_values_across_restart(tmp_path, monkeypatch):
    from tessera.board import BoardStore

    artifact = tmp_path / "artifact-placeholder.txt"
    artifact.write_text("synthetic-artifact-body-must-stay-local")
    board = BoardStore(tmp_path / "board.jsonl", enabled=True)
    card = board.create("Inspect artifact", "one", 0, 1)
    references = [str(artifact), "https://artifacts.example.com/opaque-placeholder"]
    with monkeypatch.context() as guarded:
        inspected = forbid_artifact_reads(guarded, artifact)
        for reference in references:
            board.attach_artifact(card["id"], reference, actor="one")
        restored = BoardStore(board.path, enabled=True).get(card["id"])
        assert restored["artifacts"] == references
        assert inspected == []
    assert "synthetic-artifact-body-must-stay-local" not in board.path.read_text()


def test_offline_human_questions_are_durable_and_pending_controls_blocking(tmp_path):
    from tessera.board import BoardStore

    board = BoardStore(tmp_path / "board.jsonl", enabled=True)
    card = board.create("Inspect synthetic source", "one", 0, 2)
    board.update_status(card["id"], "running", actor="one")
    advisory = board.ask_human(card["id"], "Which source is preferable?", pending=False, actor="one")
    assert advisory["status"] == "running"
    assert [(question["text"], question["pending"]) for question in advisory["questions"]] == [
        ("Which source is preferable?", False)]
    blocked = board.ask_human(card["id"], "May I change the synthetic source?", pending=True, actor="one")
    identities = [question["id"] for question in blocked["questions"]]
    blocked["questions"][0]["text"] = "caller mutation"
    restored = BoardStore(board.path, enabled=True).get(card["id"])
    assert restored["status"] == "blocked"
    assert [(question["text"], question["pending"]) for question in restored["questions"]] == [
        ("Which source is preferable?", False), ("May I change the synthetic source?", True),
    ]
    assert [question["id"] for question in restored["questions"]] == identities
    assert len(set(identities)) == 2 and all(isinstance(identity, str) and identity for identity in identities)
    for question in restored["questions"]:
        assert question["gate"] == {"ask_human": 1, "needs_authority": 1, "source": "deterministic"}


def board_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.board.journal_file = "state/board.jsonl"
    config.jev.enabled = False
    return config


def forbid_board_execution(monkeypatch):
    tripwires = []
    for name in ("_load_cards", "_load_projects", "get", "get_project", "_append_card", "_append_project",
                 "create", "create_project", "update_status", "attach_artifact", "ask_human"):
        tripwire = Mock(side_effect=AssertionError("Board reads must not load or mutate execution storage"))
        monkeypatch.setattr(BoardStore, name, tripwire)
        tripwires.append(tripwire)
    return tripwires


def test_http_board_projection_omits_private_data_and_never_resolves_artifacts(
        tmp_path, monkeypatch, forbid_active_work):
    config = board_config(tmp_path)
    key = "synthetic-board-key"
    monkeypatch.setenv("EXAMPLE_BOARD_KEY", key)
    config.jev.api_key_env = "EXAMPLE_BOARD_KEY"
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    artifact = tmp_path / "private-artifact-placeholder.txt"
    artifact.write_text("synthetic-artifact-body-must-stay-local")
    with status_server(config) as server:
        server.manager.create("private-subject-placeholder")
        card = server.manager.board.create("private-title-placeholder " + key, "private-subject-placeholder",
                                           3, 5, attempt=2, card_id="private-card-placeholder")
        server.manager.board.attach_artifact(card["id"], str(artifact))
        server.manager.board.ask_human(card["id"], "private-question-placeholder " + key)
        forbid_active_work()
        tripwires = forbid_board_execution(monkeypatch)
        before = disk_snapshot(tmp_path)
        with monkeypatch.context() as guarded:
            inspected = forbid_artifact_reads(guarded, artifact)
            code, projection = call(server, "GET", "/api/board")
            assert code == 200 and projection["enabled"] is True and projection["configured"] is True
            assert projection["cards"] == [{"ordinal": 1, "status": "blocked", "attempt": 2,
                "artifact_count": 1, "question_pending": True, "session_ordinal": 1, "log_start": 3, "log_end": 5}]
            assert isinstance(projection["revision"], str) and projection["revision"]
            assert call(server, "GET", "/api/status")[1]["board_status"] == projection
            assert call(server, "GET", "/api/board", headers={
                "If-None-Match": '"' + projection["revision"] + '"'}) == (304, None)
            assert inspected == []
        for private in (key, "private-title-placeholder", "private-subject-placeholder", "private-card-placeholder",
                        "private-question-placeholder", str(artifact), "synthetic-artifact-body-must-stay-local",
                        "judge-placeholder", "judge.example.com", "EXAMPLE_BOARD_KEY"):
            assert private not in json.dumps(projection)
        assert disk_snapshot(tmp_path) == before


def test_http_claim_and_note_let_the_operator_and_sessions_share_one_board(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Shared board smoke", "one", 0, 0, card_id="shared-card")
        view = call(server, "GET", "/api/board/view")[1]
        assert view["cards"][0]["owner"] == "one" and view["cards"][0]["claimable"] is False
        # Release the author's claim so the card is open to anyone.
        code, view = call(server, "POST", "/api/board/cards/1/claim", {"revision": view["revision"], "owner": ""})
        assert code == 200 and view["cards"][0]["owner"] == ""
        # The operator claims it by name.
        code, view = call(server, "POST", "/api/board/cards/1/claim", {"revision": view["revision"], "owner": "operator"})
        assert code == 200 and view["cards"][0]["owner"] == "operator"
        code, view = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "note": "Operator: this is the shared board now."})
        assert code == 200 and view["cards"][0]["note"] == "Operator: this is the shared board now."
        # Add note appends. Replacing the whole note would destroy what was already there.
        code, view = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "note": "Second line."})
        assert code == 200 and view["cards"][0]["note"] == "Operator: this is the shared board now.\nSecond line."
        code, view = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "title": "Shared board, renamed"})
        assert code == 200 and view["cards"][0]["title"] == "Shared board, renamed"
        assert view["cards"][0]["note"] == "Operator: this is the shared board now.\nSecond line."
        # A stale revision is refused, not silently applied.
        stale = view["revision"]
        call(server, "POST", "/api/board/cards/1/status", {"revision": stale, "status": "running"})
        assert call(server, "POST", "/api/board/cards/1/claim", {"revision": stale, "owner": "two"})[0] == 409
        assert call(server, "GET", "/api/board/view")[1]["cards"][0]["owner"] == "operator"


def test_http_project_routes_carry_the_plan_and_file_cards_under_it(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        code, created = call(server, "POST", "/api/board/projects",
                             {"title": "Shared board", "description": "One board, many threads"})
        assert code == 200
        project_id = created["project_id"]
        assert created["view"]["projects"][0]["title"] == "Shared board"
        code, view = call(server, "POST", "/api/board/projects/" + project_id + "/steps",
                          {"steps": [{"text": "Store the plan"}, {"text": "Render it"}]})
        assert code == 200
        assert view["projects"][0]["steps_total"] == 2
        step_id = view["projects"][0]["steps"][0]["id"]
        code, view = call(server, "POST", "/api/board/projects/" + project_id + "/step",
                          {"step_id": step_id, "done": True})
        assert code == 200
        assert view["projects"][0]["steps_done"] == 1
        # File the card through the store: this consumer must stay offline and never
        # construct a backend just to create a card.
        server.manager.board.create("Render the plan", "one", 0, 0)
        code, view = call(server, "GET", "/api/board/view")
        revision = view["revision"]
        code, view = call(server, "POST", "/api/board/cards/1/file",
                          {"revision": revision, "project": project_id})
        assert code == 200
        assert view["cards"][0]["project"] == project_id
        assert view["projects"][0]["cards_total"] == 1
        # A stale revision is refused, same as every other board mutation.
        assert call(server, "POST", "/api/board/cards/1/file", {"revision": "0" * 64, "project": project_id})[0] == 409
        # Closing a project keeps its plan and cards; it only stops being active.
        code, view = call(server, "POST", "/api/board/projects/" + project_id + "/update", {"status": "done"})
        assert code == 200
        assert view["projects"][0]["status"] == "done"
        assert view["projects"][0]["cards_total"] == 1


def test_disabled_board_api_is_inert_and_has_no_mutation_route(tmp_path, monkeypatch, forbid_active_work):
    config = Config(home=tmp_path, cwd=tmp_path)
    with status_server(config) as server:
        forbid_active_work()
        tripwires = forbid_board_execution(monkeypatch)
        before = disk_snapshot(tmp_path)
        assert call(server, "GET", "/api/board") == (200, {"enabled": False, "configured": True})
        assert call(server, "POST", "/api/board", {"title": "Synthetic title"})[0] == 404
        assert call(server, "GET", "/api/board/cards/1/link?revision=placeholder")[0] == 409
        assert disk_snapshot(tmp_path) == before
        for tripwire in tripwires:
            tripwire.assert_not_called()


def test_snapshot_revision_fences_ordinal_mapping_change(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.create("two")
        server.manager.board.create("First synthetic card", "one", 0, 1)
        first = call(server, "GET", "/api/board")[1]
        assert call(server, "GET", "/api/board/cards/1/link?revision=" + first["revision"]) == (
            200, {"session_slug": "one", "log_start": 0, "log_end": 1})
        server.manager.board.create("Second synthetic card", "two", 2, 4)
        before = disk_snapshot(tmp_path)
        code, rejection = call(server, "GET", "/api/board/cards/1/link?revision=" + first["revision"])
        assert code == 409 and "session_slug" not in rejection
        code, second = call(server, "GET", "/api/board", headers={"If-None-Match": '"' + first["revision"] + '"'})
        assert code == 200 and second["revision"] != first["revision"]
        assert len(second["cards"]) == 2
        assert [card["ordinal"] for card in second["cards"]] == [1, 2]
        assert call(server, "GET", "/api/board/cards/1/link?revision=" + second["revision"]) == (
            200, {"session_slug": "one", "log_start": 0, "log_end": 1})
        assert call(server, "GET", "/api/board/cards/2/link?revision=" + second["revision"]) == (
            200, {"session_slug": "two", "log_start": 2, "log_end": 4})
        assert disk_snapshot(tmp_path) == before


def test_board_reads_preserve_torn_tail_and_omit_corrupt_complete_snapshots(tmp_path, monkeypatch, forbid_active_work):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Inspect synthetic source", "one", 0, 1)
        first = call(server, "GET", "/api/board")[1]
        with server.manager.board.path.open("ab") as stream:
            stream.write(b'{"private-uncommitted')
        forbid_active_work()
        tripwires = forbid_board_execution(monkeypatch)
        before = disk_snapshot(tmp_path)
        assert call(server, "GET", "/api/board") == (200, first)
        assert disk_snapshot(tmp_path) == before
        with server.manager.board.path.open("ab") as stream:
            stream.write(b"\n")
        before = disk_snapshot(tmp_path)
        # A blank board is not an empty board. The route names the unreadable line so a
        # reader learns the board is broken and where, instead of seeing zero cards and
        # having to guess. This state never self-heals: repair only heals a torn tail.
        code, blank = call(server, "GET", "/api/board")
        assert code == 200
        assert blank["enabled"] is True and blank["configured"] is True
        assert "cards" not in blank and "revision" not in blank
        assert "unreadable" in blank and "not valid JSON" in blank["unreadable"]
        assert call(server, "GET", "/api/board/cards/1/link?revision=" + first["revision"])[0] == 409
        assert disk_snapshot(tmp_path) == before
        for tripwire in tripwires:
            tripwire.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_board_tool_registration_is_opt_in_and_independent_of_channels(tmp_path, enabled):
    config = board_config(tmp_path)
    config.board.enabled = enabled
    backend = FakeBackend([])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create("one")
        session = manager.get("one")
        names = {entry["function"]["name"] for entry in session.tools.schemas()}
        board_names = {"board_create_card", "board_update_status", "board_attach_artifact", "board_ask_human"}
        assert names.intersection(board_names) == (board_names if enabled else set())
        assert "send_to_session" not in names
        assert backend.calls == [] and not manager.board.path.exists()


@pytest.mark.parametrize("required,source", [(False, "heuristic"), (True, "deterministic")])
def test_board_human_tool_records_offline_authority_boundary_with_redacted_fields(
        tmp_path, monkeypatch, required, source):
    config = board_config(tmp_path)
    config.board.judge_asks = True
    config.jev.enabled = required
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    config.jev.api_key_env = "EXAMPLE_BOARD_KEY"
    key = "synthetic-board-key"
    monkeypatch.setenv("EXAMPLE_BOARD_KEY", key)
    artifact = tmp_path / "artifact-placeholder.txt"
    artifact.write_text("synthetic-artifact-body-must-stay-local")
    backend = FakeBackend([])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.create("one")
        session = manager.get("one")
        session.log.append("user", "Synthetic source evidence")
        card = session.tools.execute("board_create_card", {"title": "Inspect " + key})
        with monkeypatch.context() as guarded:
            inspected = forbid_artifact_reads(guarded, artifact)
            attached = session.tools.execute("board_attach_artifact", {"card_id": card["card_id"],
                "reference": str(artifact), "expected_version": 1})
            assert attached["artifact_count"] == 1 and inspected == []
        result = session.tools.execute("board_ask_human", {"card_id": card["card_id"],
            "question": "Need permission to use " + key, "required": required, "expected_version": 2})
        assert result["status"] == "blocked" and result["question_pending"] is True
        assert result["human_gate"]["source"] == source
        assert result["human_gate"]["needs_authority"] > .7
        restored = BoardStore(manager.board.path, enabled=True).get(card["card_id"])
        assert restored["title"] == "Inspect [REDACTED]"
        assert restored["artifacts"] == [str(artifact)]
        assert restored["questions"][0]["text"] == "Need permission to use [REDACTED]"
        assert restored["session_slug"] == "one" and (restored["log_start"], restored["log_end"]) == (0, 0)
        assert key not in manager.board.path.read_text() and backend.calls == []
        assert "synthetic-artifact-body-must-stay-local" not in manager.board.path.read_text()


def test_board_tools_cannot_claim_another_session_or_future_log_evidence(tmp_path, monkeypatch):
    config = board_config(tmp_path)
    config.board.judge_asks = True
    config.jev.enabled = True
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    config.jev.api_key_env = "EXAMPLE_BOARD_KEY"
    monkeypatch.setenv("EXAMPLE_BOARD_KEY", "synthetic-board-key")
    backend = FakeBackend([])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        for name in ("one", "two"):
            manager.create(name)
            manager.get(name).log.append("user", "Synthetic source evidence")
        first = manager.get("one")
        second = manager.get("two")
        card = first.tools.execute("board_create_card", {"title": "Inspect source"})
        before = manager.board.path.read_bytes()
        with pytest.raises(ValueError, match="another session"):
            second.tools.execute("board_ask_human", {"card_id": card["card_id"], "question": "Need permission?"})
        with pytest.raises(ValueError, match="another session"):
            second.tools.execute("board_attach_artifact", {"card_id": card["card_id"], "reference": "artifact-placeholder"})
        with pytest.raises(ValueError, match="exceeds"):
            first.tools.execute("board_create_card", {"title": "Future evidence", "log_start": 0, "log_end": 10})
        assert manager.board.path.read_bytes() == before and backend.calls == []


def test_redactor_handles_structured_board_results_without_failing_after_a_write(tmp_path, monkeypatch):
    # Board RPC helpers redact whole result dicts after the card is durable; text-only redaction raised
    # there and reported a failure for a card that was already saved.
    config = board_config(tmp_path)
    config.jev.api_key_env = "EXAMPLE_BOARD_KEY"
    key = "synthetic-board-key"
    monkeypatch.setenv("EXAMPLE_BOARD_KEY", key)
    with SessionManager(config, backend_factory=lambda *args: FakeBackend([])) as manager:
        value = {"card": {"card_id": "c-" + key, "version": 1, "done": False, "note": None,
                          "artifacts": ["see " + key, 3.5], key: "key"}}
        redacted = manager.messaging.redact_value(value)
        assert key not in json.dumps(redacted)
        assert redacted["card"]["version"] == 1 and redacted["card"]["done"] is False
        assert redacted["card"]["artifacts"][1] == 3.5 and value["card"]["card_id"] == "c-" + key
        with pytest.raises(Exception):
            manager.messaging.redact_value({"card": object()})


def test_board_tool_rejects_non_text_before_any_card_is_saved(tmp_path):
    with SessionManager(board_config(tmp_path), backend_factory=lambda *args: FakeBackend([])) as manager:
        manager.create("one")
        session = manager.get("one")
        session.log.append("user", "Synthetic source evidence")
        with pytest.raises(Exception):
            session.tools.execute("board_create_card", {"title": {"text": "structured"}}, )
        assert not manager.board.path.exists() or manager.board.list() == []


def test_http_a_person_writes_work_and_answers_a_card(tmp_path):
    """The two lanes the page was missing.

    Before this, the page could claim, note and move a card, but there was no route to
    create one and no way to answer a card that asked a person something. A board a person
    cannot write to is a read-only report, not a board.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        # A person writes work. It starts unclaimed, so any session that wakes can take it.
        code, created = call(server, "POST", "/api/board/cards",
                             {"title": "Check the NAS tailscaled", "note": "The operator noticed it."})
        assert code == 200
        card_id = created["card_id"]
        view = created["view"]
        assert view["cards"][0]["title"] == "Check the NAS tailscaled"
        assert view["cards"][0]["note"] == "The operator noticed it."
        assert view["cards"][0]["owner"] == ""
        assert view["cards"][0]["claimable"] is True
        assert view["cards"][0]["status"] == "queued"

        # A card that asks a person something carries the question, and the answer is
        # what unblocks it. The card returns to the open pool by itself.
        store = server.manager.board
        card = store.get(card_id)
        store.ask_human(card_id, "Which NAS share holds the repo?", pending=True)
        view = call(server, "GET", "/api/board/view")[1]
        row = view["cards"][0]
        assert row["status"] == "blocked" and row["question_pending"] is True
        assert len(row["questions"]) == 1
        question_id = row["questions"][0]["id"]
        assert row["questions"][0]["text"] == "Which NAS share holds the repo?"

        code, view = call(server, "POST", "/api/board/cards/1/answer",
                          {"revision": view["revision"], "question_id": question_id,
                           "answer": "/srv/example-share/repos"})
        assert code == 200
        answered = view["cards"][0]
        assert answered["status"] == "queued"
        assert answered["question_pending"] is False
        assert answered["questions"][0]["answer"] == "/srv/example-share/repos"
        assert "Answered: /srv/example-share/repos" in answered["note"]

        # Answering the same question twice is refused, not silently duplicated.
        assert call(server, "POST", "/api/board/cards/1/answer",
                    {"revision": view["revision"], "question_id": question_id, "answer": "again"})[0] == 409
        # A stale revision is refused.
        assert call(server, "POST", "/api/board/cards/1/answer",
                    {"revision": "stale", "question_id": question_id, "answer": "x"})[0] == 409


def test_http_a_person_cannot_create_a_card_in_a_missing_project(tmp_path):
    """A card may only be filed under a project that exists, same as the session tool."""
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        code, body = call(server, "POST", "/api/board/cards",
                          {"title": "Filed nowhere", "project": "no-such-project"})
        assert code == 409
        assert call(server, "GET", "/api/board/view")[1]["cards"] == []


def test_http_remove_takes_a_card_off_the_board_for_every_reader(tmp_path):
    """The operator could not remove it. The page now has a Remove control, and
    the route it calls has to actually take the card off the board for both readers.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Keep this", "one", 0, 0, card_id="keep-card")
        server.manager.board.create("No longer needed", "one", 0, 0, card_id="gone-card")
        view = call(server, "GET", "/api/board/view")[1]
        assert [card["title"] for card in view["cards"]] == ["Keep this", "No longer needed"]

        # A missing revision is refused, so the route cannot be called blind.
        assert call(server, "POST", "/api/board/cards/2/remove", {})[0] == 400
        code, view = call(server, "POST", "/api/board/cards/2/remove", {"revision": view["revision"]})
        assert code == 200
        assert [card["title"] for card in view["cards"]] == ["Keep this"]

        # The passive status reader agrees with the operator view.
        assert [card["status"] for card in call(server, "GET", "/api/board")[1]["cards"]] == ["queued"]
        # The record survives: the store still knows the card, flagged.
        assert server.manager.board.get("gone-card")["removed"] is True
        # A second remove of the same card is a refusal, not a silent second removal.
        assert call(server, "POST", "/api/board/cards/2/remove", {"revision": view["revision"]})[0] == 409


def test_an_oversized_note_is_refused_with_its_measured_size_not_a_version_conflict(tmp_path):
    """A card's note filled the field on the board page and the
    page told him "This card changed. Refresh the board and try again." The card had not
    changed; the text did not fit. Size is its own fact and says so.
    """
    config = board_config(tmp_path)
    config.board.max_text_bytes = 120
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Long work", "one", 0, 0, card_id="long-card")
        view = call(server, "GET", "/api/board/view")[1]
        code, rejection = call(server, "POST", "/api/board/cards/1/note",
                               {"revision": view["revision"], "note": "x" * 200})
        assert code == 413, rejection
        assert "200 bytes" in rejection["error"] and "limit is 120 bytes" in rejection["error"]
        # The card itself is untouched, so a shorter line still lands on the same revision.
        code, view = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "note": "A line that fits."})
        assert code == 200 and view["cards"][0]["note"] == "A line that fits."


def test_a_write_naming_a_card_lands_on_that_card_not_its_old_position(tmp_path):
    """A note that names a card lands on that card, not on whatever now sits at the old position.

    The page re-reads the board revision at click time, so a row drawn before the board
    changed posts a fresh revision with a stale position. The revision fence therefore
    passes, and `cards[ordinal - 1]` would resolve to whichever card now sits at that
    position.

    Two cards exist; the note is addressed to the position of the first while naming the
    identity of the second. Refusing that outright stops the misdelivery and also refuses
    a correct write. The named card is the card that was meant, so the note lands there
    and the position is ignored.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        store = server.manager.board
        first = store.create("first card", "one", 0, 0, card_id="card-first")
        second = store.create("second card", "one", 0, 0, card_id="card-second")
        view = call(server, "GET", "/api/board/view")[1]
        revision = view["revision"]
        ordinal_first = next(row["ordinal"] for row in view["cards"] if row["card_id"] == "card-first")
        assert next(row["ordinal"] for row in view["cards"] if row["card_id"] == "card-second") != ordinal_first

        # The position is the first card's, the identity is the second's.
        code, body = call(server, "POST", "/api/board/cards/%d/note" % ordinal_first,
                          {"revision": revision, "note": "meant for the second card",
                           "card_id": "card-second"})
        assert code == 200, body
        after = call(server, "GET", "/api/board/view")[1]
        by_id = {row["card_id"]: row for row in after["cards"]}
        # The named card got the words. The card that happened to sit at that position
        # did not.
        assert "meant for the second card" not in by_id["card-first"]["note"]
        assert "meant for the second card" in by_id["card-second"]["note"]

        # The same write, naming the card it is actually addressed to, lands.
        revision = after["revision"]
        code, body = call(server, "POST", "/api/board/cards/%d/note" % ordinal_first,
                          {"revision": revision, "note": "meant for the first card",
                           "card_id": "card-first"})
        assert code == 200, body
        after = call(server, "GET", "/api/board/view")[1]
        by_id = {row["card_id"]: row for row in after["cards"]}
        assert "meant for the first card" in by_id["card-first"]["note"]
        assert "meant for the first card" not in by_id["card-second"]["note"]

        # Every sibling write route resolves by identity too, not only note. Each one
        # below names card-second while pointing at card-first's position, and each has
        # to land on card-second.
        current = call(server, "GET", "/api/board/view")[1]
        revision = current["revision"]
        ordinal_first = next(row["ordinal"] for row in current["cards"] if row["card_id"] == "card-first")
        code, body = call(server, "POST", "/api/board/cards/%d/claim" % ordinal_first,
                          {"revision": revision, "owner": "operator", "card_id": "card-second"})
        assert code == 200, body
        by_id = {row["card_id"]: row for row in body["cards"]}
        assert by_id["card-second"]["owner"] == "operator"
        assert by_id["card-first"]["owner"] != "operator"

        revision = body["revision"]
        code, body = call(server, "POST", "/api/board/cards/%d/status" % ordinal_first,
                          {"revision": revision, "status": "running", "card_id": "card-second"})
        assert code == 200, body
        by_id = {row["card_id"]: row for row in body["cards"]}
        assert by_id["card-second"]["status"] == "running"
        assert by_id["card-first"]["status"] != "running"

        # A named card that is absent from the board is still a refusal, because there
        # is nothing left to write to. That is the one case where refreshing is honest.
        revision = body["revision"]
        code, body = call(server, "POST", "/api/board/cards/%d/note" % ordinal_first,
                          {"revision": revision, "note": "to a card that never existed",
                           "card_id": "card-absent"})
        assert code == 409, body
        assert "no longer on the board" in body["error"]
        still = call(server, "GET", "/api/board/view")[1]
        assert len(still["cards"]) == 2, "a refused write must not remove or move a card"


def test_a_write_without_a_card_identity_still_works_for_existing_callers(tmp_path):
    """The identity is optional so a caller that only knows the position still works.

    Every session tool and every already-open page posts without it. Requiring it would
    break them; the guard is for callers that DO name a card, which is what makes a
    mismatch detectable rather than silent.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("only card", "one", 0, 0, card_id="card-only")
        view = call(server, "GET", "/api/board/view")[1]
        code, body = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "note": "position only"})
        assert code == 200, body
        after = call(server, "GET", "/api/board/view")[1]
        assert "position only" in after["cards"][0]["note"]


def test_start_moves_an_owned_card_to_running_with_the_session(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Start smoke", "one", 0, 0, card_id="start-card")
        view = call(server, "GET", "/api/board/view")[1]
        code, view = call(server, "POST", "/api/board/cards/1/start",
                          {"revision": view["revision"], "session": "one",
                           "receipt": "working the slice", "card_id": "start-card"})
        assert code == 200
        card = view["cards"][0]
        assert card["status"] == "running"
        assert card["execution"]["session"] == "one"
        assert card["execution"]["receipt"] == "working the slice"
        assert card["execution"]["since"] > 0


def test_start_refuses_an_unclaimed_or_foreign_card(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Start guards", "one", 0, 0, card_id="guard-card")
        view = call(server, "GET", "/api/board/view")[1]
        code, view = call(server, "POST", "/api/board/cards/1/claim",
                          {"revision": view["revision"], "owner": "", "card_id": "guard-card"})
        assert code == 200 and view["cards"][0]["owner"] == ""
        code, body = call(server, "POST", "/api/board/cards/1/start",
                          {"revision": view["revision"], "session": "one",
                           "card_id": "guard-card"})
        assert code == 409 and "unclaimed" in str(body)
        view = call(server, "GET", "/api/board/view")[1]
        code, view = call(server, "POST", "/api/board/cards/1/claim",
                          {"revision": view["revision"], "owner": "operator", "card_id": "guard-card"})
        assert code == 200
        code, body = call(server, "POST", "/api/board/cards/1/start",
                          {"revision": view["revision"], "session": "one",
                           "card_id": "guard-card"})
        assert code == 409 and "another session" in str(body)


def test_terminal_move_keeps_the_latest_execution_record(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Finish keeps evidence", "one", 0, 0, card_id="finish-card")
        view = call(server, "GET", "/api/board/view")[1]
        code, view = call(server, "POST", "/api/board/cards/1/start",
                          {"revision": view["revision"], "session": "one",
                           "receipt": "slice a", "card_id": "finish-card"})
        assert code == 200 and view["cards"][0]["execution"]["receipt"] == "slice a"
        code, view = call(server, "POST", "/api/board/cards/1/status",
                          {"revision": view["revision"], "status": "succeeded", "card_id": "finish-card"})
        assert code == 200
        card = view["cards"][0]
        assert card["status"] == "succeeded"
        assert card["execution"]["session"] == "one"
        assert card["execution"]["receipt"] == "slice a"


def test_http_acting_session_is_guarded_like_the_tool_path(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.create("two")
        server.manager.board.create("Guarded claim", "one", 0, 0, card_id="guarded-claim")
        view = call(server, "GET", "/api/board/view")[1]
        # Another session naming itself as the actor cannot take the card.
        code, body = call(server, "POST", "/api/board/cards/1/claim",
                          {"revision": view["revision"], "owner": "two", "session": "two",
                           "card_id": "guarded-claim"})
        assert code == 409, body
        view = call(server, "GET", "/api/board/view")[1]
        code, body = call(server, "POST", "/api/board/cards/1/note",
                          {"revision": view["revision"], "note": "intruder writes",
                           "session": "two", "card_id": "guarded-claim"})
        assert code == 409, body
        # Nothing changed on the card.
        view = call(server, "GET", "/api/board/view")[1]
        assert view["cards"][0]["owner"] == "one"
        assert "intruder writes" not in view["cards"][0]["note"]


def test_http_writes_without_acting_session_stay_operator(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("Operator still works", "one", 0, 0, card_id="op-card")
        view = call(server, "GET", "/api/board/view")[1]
        code, view = call(server, "POST", "/api/board/cards/1/claim",
                          {"revision": view["revision"], "owner": "operator", "card_id": "op-card"})
        assert code == 200 and view["cards"][0]["owner"] == "operator"
        code, body = call(server, "POST", "/api/board/cards/1/status",
                          {"revision": view["revision"], "status": "running", "card_id": "op-card"})
        assert code == 200, body
        view = call(server, "GET", "/api/board/view")[1]
        assert view["cards"][0]["status"] == "running"


def test_http_remove_with_foreign_acting_session_is_refused(tmp_path):
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.create("two")
        server.manager.board.create("Guarded removal", "one", 0, 0, card_id="guarded-rm")
        view = call(server, "GET", "/api/board/view")[1]
        code, body = call(server, "POST", "/api/board/cards/1/remove",
                          {"revision": view["revision"], "session": "two", "card_id": "guarded-rm"})
        assert code == 409, body
        view = call(server, "GET", "/api/board/view")[1]
        assert view["cards"][0]["status"] == "queued"


def test_a_project_can_be_handed_to_a_named_session(tmp_path):
    """A project can be handed to a named session from the project surface.

    The update route accepts a manager. This pins the contract the chooser depends
    on: assign a live session, read it back, and refuse a name that is not a session
    name, so the page's control cannot drift from the route that serves it.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.create("two")
        code, created = call(server, "POST", "/api/board/projects", {"title": "Hand me over"})
        assert code == 200
        project_id = created["project_id"]
        assert created["view"]["projects"][0]["manager"] == ""

        code, view = call(server, "POST", "/api/board/projects/" + project_id + "/update",
                          {"manager": "two"})
        assert code == 200
        assert view["projects"][0]["manager"] == "two"

        # Clearing it is a real choice, not an omission.
        code, view = call(server, "POST", "/api/board/projects/" + project_id + "/update",
                          {"manager": ""})
        assert code == 200
        assert view["projects"][0]["manager"] == ""

        # A name that is not a session name is refused rather than stored.
        assert call(server, "POST", "/api/board/projects/" + project_id + "/update",
                    {"manager": "Not A Session"})[0] == 409
        assert call(server, "GET", "/api/board/view")[1]["projects"][0]["manager"] == ""

def test_a_note_named_by_card_lands_even_when_the_page_is_older_than_the_board(tmp_path):
    """A note named by card lands even when the page is older than the board.

    A board with several writers moves its revision while a person is still typing,
    so the posted page is older than the board. Fencing that write as "the card
    changed" reads as a dead Submit button. Naming the card is what makes the write
    safe, so a named write is not fenced.
    """
    config = board_config(tmp_path)
    with status_server(config) as server:
        server.manager.create("one")
        server.manager.board.create("First", "one", 0, 0, card_id="card-first")
        server.manager.board.create("Second", "one", 0, 0, card_id="card-second")
        view = call(server, "GET", "/api/board/view")[1]
        page_revision = view["revision"]
        ordinal_first = next(r["ordinal"] for r in view["cards"] if r["card_id"] == "card-first")

        # A session writes while the person is typing. The revision moves.
        server.manager.board.update_status("card-second", "running", attempt=1)
        now = call(server, "GET", "/api/board/view")[1]
        assert now["revision"] != page_revision

        # The person presses Submit on the note form they opened before that write.
        # They named the card; the position and revision are from their older page.
        code, body = call(server, "POST", "/api/board/cards/%d/note" % ordinal_first,
                          {"revision": page_revision, "note": "typed over twenty seconds",
                           "card_id": "card-first"})
        assert code == 200, body
        after = call(server, "GET", "/api/board/view")[1]
        by_id = {r["card_id"]: r for r in after["cards"]}
        assert "typed over twenty seconds" in by_id["card-first"]["note"]
        assert "typed over twenty seconds" not in by_id["card-second"]["note"]
