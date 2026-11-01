import json
import re
import socket

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient, CaptureRejected
from tessera.capture import Capture
from tessera.mcp_client import McpError


class Bus:
    def __init__(self):
        self.hooks = {}

    def register(self, event, fn):
        self.hooks.setdefault(event, []).append(fn)

    def emit(self, event, payload):
        results = []
        for hook in self.hooks.get(event, []):
            results.append(hook(payload))
        return results


def run_capture(bus, session="session-one"):
    bus.emit("session_start", {"session_id": session, "anchor": "work/task"})
    bus.emit("turn_start", {"message": "Keep my exact words: café, 日本語.\nSecond line."})
    bus.emit("pre_tool", {"name": "shell", "arguments": {"command": "false"}, "id": "c1"})
    bus.emit("post_tool", {"name": "shell", "result": {"exit_code": 1, "output": "failure"}, "id": "c1"})
    bus.emit("turn_end", {"reply": "The command failed. café 日本語", "failed": True})


def test_server_down_then_up_replays_stable_id_and_drains(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = AetherMindClient(f"http://127.0.0.1:{port}/mcp", timeout=.2)
    capture = Capture(client, tmp_path, "work", "personal", "tester")
    bus = Bus()
    capture.install(bus)
    run_capture(bus)
    debt = capture.debt()
    assert len(debt) == 1
    request_id = debt[0]["request_id"]
    assert re.fullmatch(r"session-one-t1-[0-9a-f]{32}", request_id)
    assert debt[0]["attempts"] == 1
    assert "Write-debt: 1 unsent captures (oldest " in capture.inheritance_block()
    with FakeMcpServer(port=port) as server:
        capture.retry_outbox()
        assert capture.debt() == []
        assert [item["ref"] for item in server.storage["layers"]] == [request_id]
        body = server.storage["layers"][0]["body"]
        assert "The command failed." in body
        assert "café" in body and "日本語" in body
        assert "Keep my exact words" not in body
        assert "Turn " not in body and "User:" not in body
        assert "shell: failed" in body
        assert "exit_code" not in body
        assert "The command failed." in body
        assert "Decision: failure" in body
        assert "Failure recorded" in body
        assert "personal history" in capture.inheritance_block()


def test_lost_ack_and_restart_keep_identical_payload(tmp_path):
    with FakeMcpServer() as server:
        client = AetherMindClient(server.url, timeout=1)
        server.lose_next_ack = True
        capture = Capture(client, tmp_path, "work", "personal", "tester")
        bus = Bus()
        capture.install(bus)
        run_capture(bus)
        assert len(capture.debt()) == 1
        original = {key: value for key, value in server.storage["layers"][0].items() if key != "id"}
        restored = Capture(client, tmp_path, "work", "personal", "tester")
        next_bus = Bus()
        restored.install(next_bus)
        next_bus.emit("session_start", {"session_id": "session-two"})
        assert restored.debt() == []
        writes = []
        for request, _ in server.requests:
            params = request.get("params", {})
            if params.get("name") == "write_layer":
                writes.append(params["arguments"])
        assert writes == [original]
        assert len(server.storage["layers"]) == 1


def test_no_client_captures_all_turns_session_and_explicit_note(tmp_path):
    capture = Capture(None, tmp_path, "work", "personal", "tester")
    bus = Bus()
    capture.install(bus)
    run_capture(bus)
    bus.emit("turn_start", {"message": "Another request"})
    bus.emit("turn_end", {"reply": "Another answer"})
    bus.emit("session_end", {})
    capture.memory_note("An explicit note")
    pending = capture.debt()
    ids = [item["request_id"] for item in pending]
    assert re.fullmatch(r"session-one-t1-[0-9a-f]{32}", ids[0])
    assert re.fullmatch(r"session-one-t2-[0-9a-f]{32}", ids[1])
    assert ids[2] == "session-one-session"
    assert len(pending) == 4
    assert all(item["attempts"] == 0 for item in pending)
    assert ids[0] in pending[2]["layer"]["body"]
    reopened = Capture(None, tmp_path, "work", "personal", "tester")
    assert len(reopened.debt()) == 4
    assert reopened.memory_recall(query="history")["error"] == "memory service unavailable"


def test_repeated_turn_end_does_not_enqueue_or_send_twice(tmp_path):
    with FakeMcpServer() as server:
        capture = Capture(AetherMindClient(server.url), tmp_path, "work", "", "tester")
        bus = Bus()
        capture.install(bus)
        run_capture(bus)
        bus.emit("turn_end", {"reply": "The command failed.", "failed": True})
        assert len(server.storage["layers"]) == 1
        calls = [r for r, _ in server.requests if r.get("params", {}).get("name") == "write_layer"]
        assert len(calls) == 1


def test_recover_torn_journal_tail_without_losing_pending_capture(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    run_capture(bus)
    journal = tmp_path / "outbox.jsonl"
    with journal.open("ab") as stream:
        stream.write(b'{"event":"enqueue"')
    recovered = Capture(None, tmp_path, "work", "", "tester")
    assert len(recovered.debt()) == 1
    recovered.memory_note("after recovery")
    for line in journal.read_text().splitlines():
        json.loads(line)
    assert len(recovered.debt()) == 2


def test_memory_tools_use_the_service_and_bound_inheritance(tmp_path):
    class Client:
        def prepare_inheritance(self, *args):
            return "project " * 3000

        def tree_view(self, *args):
            return "personal " * 3000

        def read_layers(self, project_id, **filters):
            return {"project": project_id, **filters}

        def tree_zoom(self, project_id, level, index):
            return {"level": level, "index": index}

    capture = Capture(Client(), tmp_path, "work", "personal", "tester", inheritance_budget=200, personal_budget=100)
    assert len(capture.inheritance_block()) < 500
    assert capture.memory_recall(query="prior", project_id="work") == {"project": "work", "query": "prior", "last_n": 10}
    # Without a project the search covers the work project and the personal store.
    assert capture.memory_recall(query="prior")["projects"] == ["work", "personal"]
    assert capture.memory_recall(level=1, index=2) == {"level": 1, "index": 2}
    with pytest.raises(ValueError):
        capture.memory_recall(level=1)


def test_resuming_session_never_reuses_a_completed_turn_id(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    run_capture(bus, session="resumed")
    restored = Capture(None, tmp_path, "work", "", "tester")
    resumed_bus = Bus()
    restored.install(resumed_bus)
    resumed_bus.emit("session_start", {"session_id": "resumed"})
    resumed_bus.emit("turn_start", {"message": "Continue after restart"})
    resumed_bus.emit("turn_end", {"reply": "Continued"})
    ids = [item["request_id"] for item in restored.debt()]
    assert ids[0] == capture.debt()[0]["request_id"]
    assert re.fullmatch(r"resumed-t1-[0-9a-f]{32}", ids[0])
    assert re.fullmatch(r"resumed-t2-[0-9a-f]{32}", ids[1])


def test_unsuccessful_turn_and_tool_are_marked_without_error_text(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("turn_start", {"message": "Run until limit"})
    bus.emit("post_tool", {"name": "shell", "result": "exit code 7", "success": False})
    bus.emit("turn_end", {"reply": "Stopped at limit", "success": False})
    assert "Failure" in capture.debt()[0]["layer"]["body"]


def test_ambiguous_ack_keeps_debt_and_middle_corruption_is_not_discarded(tmp_path):
    class Client:
        def write_layer(self, *args):
            return {}

    capture = Capture(Client(), tmp_path, "work", "", "tester")
    capture.memory_note("keep until acknowledged")
    assert len(capture.debt()) == 1
    with (tmp_path / "outbox.jsonl").open("ab") as stream:
        stream.write(b"not json\n")
    with pytest.raises(ValueError, match="Invalid outbox journal"):
        Capture(None, tmp_path, "work", "", "tester")


def test_capture_redacts_known_environment_credentials_before_journaling(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-value-for-capture-test")
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("turn_start", {"message": "Do not store secret-value-for-capture-test"})
    bus.emit("pre_tool", {"name": "shell", "arguments": {"command": "echo secret-value-for-capture-test"}})
    bus.emit("post_tool", {"name": "shell", "result": "secret-value-for-capture-test"})
    bus.emit("turn_end", {"reply": "secret-value-for-capture-test"})
    journal = (tmp_path / "outbox.jsonl").read_text()
    assert "secret-value-for-capture-test" not in journal
    assert "[REDACTED]" in journal


@pytest.mark.parametrize("dialect", ["id", "path"])
def test_outbox_flush_skips_interleaved_foreign_debt_and_persists_exact_items(tmp_path, dialect):
    outbox = tmp_path / "outbox"
    projects = ["previous-placeholder", "current-placeholder", "personal-placeholder",
                "previous-placeholder", "current-placeholder"]
    for index, project in enumerate(projects):
        capture = Capture(None, outbox, project)
        capture.memory_note(f"Synthetic capture {index}")
    original = Capture(None, outbox, "current-placeholder").debt()
    foreign = [item for item in original if item["project_id"] != "current-placeholder"]
    current_ids = {item["request_id"] for item in original if item["project_id"] == "current-placeholder"}

    with FakeMcpServer(dialect=dialect) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1,
                                  read_only_projects=["personal-placeholder"])
        capture = Capture(client, outbox, "current-placeholder", "personal-placeholder")
        capture.retry_outbox()
        assert capture.debt() == foreign
        assert "3 captures target a different project; retained without sending." in capture.inheritance_block()
        reopened = Capture(client, outbox, "current-placeholder", "personal-placeholder")
        reopened.retry_outbox()
        assert reopened.debt() == foreign
        writes = []
        for request, _ in server.requests:
            params = request.get("params", {})
            if params.get("name") == "write_layer":
                writes.append(params["arguments"])
        assert len(writes) == 2
        assert {item["ref"] for item in writes} == current_ids
        assert all(item["project_root"] == "current-placeholder" for item in writes)
    events = [json.loads(line) for line in (outbox / "outbox.jsonl").read_text().splitlines()]
    delivered = [event["request_id"] for event in events if event["event"] == "ack"]
    attempted = [event["request_id"] for event in events if event["event"] == "attempt"]
    assert set(delivered) == current_ids and len(delivered) == 2
    assert set(attempted) == current_ids and len(attempted) == 2
    assert Capture(None, outbox, "current-placeholder").debt() == foreign


def test_empty_current_project_never_flushes_preexisting_debt(tmp_path):
    capture = Capture(None, tmp_path, "previous-placeholder")
    capture.memory_note("Retain the original destination.")
    original = capture.debt()

    class Client:
        def write_layer(self, *args):
            pytest.fail("Empty project cannot authorize an outbox write")

    reopened = Capture(Client(), tmp_path, "")
    reopened.retry_outbox()
    assert reopened.debt() == original


def test_text_query_is_ranked_client_side_when_the_server_has_no_query_filter():
    from tessera.aethermind import rank_layers
    layers = [{"id": "1", "ts": "2026-10-16T10:00:00Z", "body": "unrelated"},
              {"id": "2", "ts": "2026-10-16T11:00:00Z", "body": "Ash Hart wrote TensorFold"},
              {"id": "3", "ts": "2026-10-16T12:00:00Z", "body": "TensorFold serve"}]
    ranked = rank_layers({"layers": layers}, "Who is Ash, TensorFold author?", 5)
    assert [layer["id"] for layer in ranked["layers"]] == ["2", "3"] and ranked["scanned"] == 3


def test_an_archived_layer_never_reaches_the_model_as_live_memory():
    """An archived layer never reaches the model as live memory.

    `read_layers` returns immutable history. A layer an explicit archive
    superseded comes back with no self-signal at all: markers, replacement and
    reason are all empty, and only a newer tombstone names it. Retired records
    must not be handed to the model as current memory.
    """
    from tessera.aethermind import drop_superseded
    layers = [
        {"id": "1093", "markers": [], "supersedes": [], "body": "probe debris"},
        {"id": "1094", "markers": ["!"], "supersedes": ["1093"], "body": "archived"},
        {"id": "1095", "markers": [], "supersedes": [], "body": "more debris"},
        {"id": "1096", "markers": ["!"], "supersedes": ["1095"], "body": "archived"},
        {"id": "1097", "markers": [], "supersedes": [], "body": "real work"},
    ]
    result = drop_superseded({"success": True, "layers": layers})
    assert [layer["id"] for layer in result["layers"]] == ["1094", "1096", "1097"]
    assert result["omitted_superseded"] == 2
    # The tombstone stays: it is history too, and it carries the reason.
    assert result["success"] is True


def test_a_load_bearing_marker_is_not_a_tombstone():
    """90 of 200 `!`-marked layers on the live store name no supersedes target.

    Treating the marker alone as a tombstone would drop live load-bearing
    records. Only a layer that names a target is a tombstone.
    """
    from tessera.aethermind import drop_superseded, superseded_ids
    layers = [{"id": "1", "markers": ["!"], "supersedes": []},
              {"id": "2", "markers": ["?", "!"], "supersedes": []},
              {"id": "3", "markers": ["!"], "supersedes": ["9"]}]
    assert superseded_ids(layers) == {"9"}
    assert [layer["id"] for layer in drop_superseded({"layers": layers})["layers"]] == ["1", "2", "3"]


def test_thread_replaced_history_is_kept_because_it_is_normal_recall():
    """1003 of 1005 inactive entries on the live store are `thread-replaced`.

    That reason is ordinary per-turn history, not retirement. Dropping it would
    gut recall, so the filter is deliberately narrow: only explicit archives.
    """
    from tessera.aethermind import drop_superseded
    layers = [{"id": "10", "markers": [], "supersedes": [], "body": "yesterday's note"}]
    result = drop_superseded({"layers": layers})
    assert [layer["id"] for layer in result["layers"]] == ["10"]
    assert "omitted_superseded" not in result


def test_a_superseded_target_cannot_take_a_models_recall_slot():
    """The scan is wider than the returned page, so filtering must happen first.

    `read_layers(query=...)` scans a recent window (400 by default) and returns
    only `last_n`. A superseded target can rank inside the page while its
    tombstone falls outside it. Computing the superseded set from the WHOLE scan
    before truncating is what makes the filter complete rather than lucky.
    """
    from tessera.aethermind import rank_layers
    layers = [{"id": "1", "ts": "2026-10-16T12:00:00Z", "body": "TensorFold probe"},
              {"id": "2", "ts": "2026-10-16T13:00:00Z", "markers": ["!"],
               "supersedes": ["1"], "body": "archived"},
              {"id": "3", "ts": "2026-10-16T11:00:00Z", "body": "TensorFold real"},
              {"id": "4", "ts": "2026-10-16T10:00:00Z", "body": "TensorFold older"}]
    # Two slots. Layer 1 would win one of them on score alone; filtering first
    # leaves both slots to live matches. If the filter ran after truncation, the
    # page would be [1, 3] and live layer 4 would be pushed out.
    ranked = rank_layers({"layers": layers}, "TensorFold", 2)
    assert [layer["id"] for layer in ranked["layers"]] == ["3", "4"]
    assert ranked["omitted_superseded"] == 1
    assert "1" not in [layer["id"] for layer in ranked["layers"]]


def test_recall_drops_superseded_before_spending_the_models_slots(tmp_path):
    class Client:
        def read_layers(self, project_id, **filters):
            return {"layers": [
                {"id": "1", "body": "retired", "supersedes": []},
                {"id": "2", "markers": ["!"], "supersedes": ["1"], "body": "archived"},
                {"id": "3", "body": "live", "supersedes": []},
            ]}

    capture = Capture(Client(), tmp_path, "work", "personal", "tester", recall_limit=2)
    result = capture.memory_recall(query="anything", project_id="work")
    assert [layer["id"] for layer in result["layers"]] == ["2", "3"]
    assert result["omitted_superseded"] == 1


def test_typed_notes_go_to_the_note_project(tmp_path):
    capture = Capture(None, tmp_path, "work", "personal", "tester", note_project="personal")
    capture.memory_note("routine")
    capture.memory_note("The operator decided X", type="decision")
    targets = sorted((item["project_id"], item["layer"]["type"]) for item in capture.debt())
    assert targets == [("personal", "decision"), ("work", "discovery")]


def test_memory_reads_are_not_recaptured_into_new_layers(tmp_path):
    capture = Capture(None, tmp_path, "work", "personal", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("session_start", {"session_id": "s"})
    bus.emit("turn_start", {"message": "What about the notes?"})
    recalled = {"layers": [{"body": "Turn 7 ... Tool memory_recall outcome: older nested layer"}]}
    bus.emit("pre_tool", {"name": "memory_recall", "id": "m", "arguments": {"query": "notes"}})
    bus.emit("post_tool", {"name": "memory_recall", "id": "m", "result": json.dumps(recalled), "success": True})
    bus.emit("pre_tool", {"name": "read_file", "id": "r", "arguments": {"path": "a"}})
    bus.emit("post_tool", {"name": "read_file", "id": "r", "result": "alpha", "success": True})
    bus.emit("turn_end", {"reply": "The notes are in the folder.", "success": True})
    body = capture.debt()[-1]["layer"]["body"]
    assert "older nested layer" not in body
    assert "Tool memory_recall arguments" not in body
    assert "alpha" not in body
    assert "memory_recall: recalled, not stored" in body
    assert "read_file: ok" in body
    assert "The notes are in the folder." in body


def test_capture_never_nests_a_prior_recall_body(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    prior = "Turn 3 Decision fact Outcome the nested token ALPHA-RECALL-BODY stays in the prior layer only."
    capture.turn_start({"message": "What did we decide?"})
    capture.post_tool({"name": "memory_recall", "id": "m", "success": True,
                       "result": {"layers": [{"body": prior}]}})
    capture.turn_end({"reply": "We decided this: " + prior, "success": True})
    body = capture.debt()[-1]["layer"]["body"]
    assert "ALPHA-RECALL-BODY" not in body
    assert "layers" not in body
    assert "memory_recall: recalled, not stored" in body
    assert "We decided this:" in body


def test_finished_greetings_are_not_unresolved(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    for message in ("Who are you?", "are you oriented?"):
        capture.turn_start({"message": message})
        capture.turn_end({"reply": "I am here and oriented.", "success": True,
                          "verification": {"completion": 0.1, "claims_done_but_failed": 0}})
    assert len(capture.debt()) == 2
    for item in capture.debt():
        assert "?" not in item["layer"]["markers"]
        assert "Failure recorded" not in item["layer"]["body"]


def test_missing_summary_tree_recalls_and_never_builds(tmp_path):
    calls = []

    class Client:
        def tree_view(self, project, budget_chars=2000):
            calls.append("tree_view")
            return {"success": False,
                    "error": "no summary tree at /store-placeholder/tree.aem; run build_tree first"}

        def read_layers(self, project, last_n=10, **kwargs):
            calls.append(("read_layers", last_n))
            return {"layers": [{"body": "a recent decision"}]}

        def build_tree(self, *args, **kwargs):
            raise AssertionError("build_tree must not be called")

    capture = Capture(Client(), tmp_path, "work", "", "tester", recall_limit=4)
    result = capture.memory_tree()
    assert result["state"] == "no_tree"
    assert result["message"] == "No summary tree; recalled recent layers instead."
    assert "build_tree" not in result["message"]
    assert calls == ["tree_view", ("read_layers", 4)]
    assert "a recent decision" in json.dumps(result["recall"])
    client = Client()

    def broken(project, last_n=10, **kwargs):
        raise OSError("offline")

    client.read_layers = broken
    capture.client = client
    fallback = capture.memory_tree()
    assert fallback["state"] == "no_tree"
    assert fallback["message"] == "No summary tree; recall is unavailable (OSError: offline)."
    assert "error" not in fallback


class RejectingClient:
    """A memory client that can reject specific captures permanently."""

    def __init__(self, reject_ids=(), fail_ids=()):
        self.reject_ids = set(reject_ids)
        self.fail_ids = set(fail_ids)
        self.calls = []

    def write_layer(self, project_id, request_id, layer):
        self.calls.append(request_id)
        if request_id in self.reject_ids:
            raise CaptureRejected("Native capture reference belongs to different content")
        if request_id in self.fail_ids:
            raise McpError("MCP transport or decoding failure (TimeoutError)")
        return {"success": True, "layer_id": "1", "request_id": request_id}


def _enqueue_three(capture, ids=("r-1", "r-2", "r-3")):
    for rid in ids:
        capture._enqueue(rid, {"type": "note", "body": rid, "ctx": "tessera/turn/session/x",
                               "author": "tester", "markers": []})


def test_a_rejected_capture_does_not_block_the_captures_behind_it(tmp_path):
    """Head-of-line blocking. One capture the server will never accept must not
    stop the walk, or every capture queued after it starves forever."""
    client = RejectingClient(reject_ids={"r-2"})
    capture = Capture(client, tmp_path, "work", "", "tester")
    _enqueue_three(capture)
    capture.retry_outbox()
    assert client.calls == ["r-1", "r-2", "r-3"]
    assert [item["request_id"] for item in capture.debt()] == ["r-2"]


def test_a_permanently_rejected_capture_is_not_retried_on_every_wake(tmp_path):
    client = RejectingClient(reject_ids={"r-2"})
    capture = Capture(client, tmp_path, "work", "", "tester")
    _enqueue_three(capture)
    capture.retry_outbox()
    first = list(client.calls)
    capture.retry_outbox()
    assert client.calls == first, "a rejected capture must not be attempted again"
    debt = capture.debt()
    assert [item["request_id"] for item in debt] == ["r-2"]
    assert debt[0]["permanent_error"] == "CaptureRejected"


def test_a_transport_failure_still_stops_the_walk(tmp_path):
    """The opposite direction: a dead service must not cost one timeout per item."""
    client = RejectingClient(fail_ids={"r-1"})
    capture = Capture(client, tmp_path, "work", "", "tester")
    _enqueue_three(capture)
    capture.retry_outbox()
    assert client.calls == ["r-1"]
    assert len(capture.debt()) == 3
    journal = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8")
    assert "TimeoutError" not in journal
    assert "MCP transport" not in journal
    failure = [json.loads(line) for line in journal.splitlines() if '"failure"' in line][-1]
    assert failure["error"] == "McpError"
    assert "detail" not in failure
    assert capture.service_error == "McpError"
    assert "MCP transport or decoding failure" in capture._delivery_errors["r-1"]


def test_a_clean_outbox_drains_everything(tmp_path):
    client = RejectingClient()
    capture = Capture(client, tmp_path, "work", "", "tester")
    _enqueue_three(capture)
    capture.retry_outbox()
    assert client.calls == ["r-1", "r-2", "r-3"]
    assert capture.debt() == []


def test_a_lower_turn_number_from_a_caller_does_not_walk_the_counter_backwards(tmp_path):
    """turn_start ignores a caller-supplied number that would walk the counter
    backwards. Taking it as-is would land the next capture on a spent ref."""
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("session_start", {"session_id": "s1"})
    for _ in range(3):
        bus.emit("turn_start", {"message": "a turn"})
        bus.emit("turn_end", {"reply": "ok", "success": True})
    assert capture.turn_number == 3
    bus.emit("turn_start", {"turn": 2, "message": "replayed turn"})
    assert capture.turn_number == 3, "a lower caller number must not rewind the counter"
    bus.emit("turn_start", {"message": "next turn"})
    assert capture.turn_number == 4


def test_a_collided_capture_is_reported_as_debt_and_keeps_its_body(tmp_path):
    """A turn whose derived ref already held different content stays in the
    debt and is not reported as write_debt 0, so a gap cannot look clean."""
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("session_start", {"session_id": "s1"})
    # Someone else already spent this ref on a different body.
    capture._enqueue("s1-t1", {"type": "note", "body": "a different writer's layer",
                               "ctx": "tessera/turn/session/s1", "author": "other", "markers": []})
    bus.emit("turn_start", {"message": "this turn is genuinely different"})
    capture._turn_request_id = "s1-t1"  # Force the defensive collision path.
    result = bus.emit("turn_end", {"reply": "It landed nowhere at first.", "success": True})[-1]
    assert result.get("write_debt", 0) >= 1, "a lost capture must never report debt 0"
    assert result.get("lost") == ["s1-t1"]
    collision_items = [item for item in capture.debt() if item.get("permanent_error") == "CaptureCollision"]
    assert len(collision_items) == 1
    assert "It landed nowhere at first." in collision_items[0]["layer"]["body"]


def test_turn_end_without_an_active_turn_still_reports_debt(tmp_path):
    """The collision variable must exist on the no-turn path too."""
    capture = Capture(None, tmp_path, "work", "", "tester")
    result = capture.turn_end({"reply": "nothing was running", "success": True})
    assert result["write_debt"] == 0
    assert "lost" not in result


class StrictCaptureClient:
    """Offline store double: exact repeats succeed, changed bodies are refused."""

    def __init__(self):
        self.storage = {"layers": []}
        self.native_write_calls = 0

    def write_layer(self, project_id, request_id, layer):
        for existing in self.storage["layers"]:
            if existing["project_root"] != project_id or existing["ref"] != request_id:
                continue
            if existing["body"] != layer["body"]:
                raise CaptureRejected("Reference already belongs to different content")
            return {"success": True, "deduplicated": True}
        self.native_write_calls += 1
        self.storage["layers"].append({**layer, "project_root": project_id, "ref": request_id})
        return {"success": True}


@pytest.fixture(params=["direct", "id", "path"])
def capture_store(request):
    if request.param == "direct":
        client = StrictCaptureClient()
        yield client, client
        return
    with FakeMcpServer(dialect=request.param) as server:
        yield AetherMindClient(server.url), server


@pytest.mark.parametrize("repeat", ["caller_counter", "reset_counter", "fresh_outbox"])
def test_repeated_turn_numbers_write_distinct_capture_refs(tmp_path, capture_store, repeat):
    client, server = capture_store
    capture = Capture(client, tmp_path / "first", "work")
    capture.session_start({"session_id": "reused-session"})
    capture.turn_start({"turn_number": 1, "message": "first request"})
    assert capture.turn_end({"reply": "First distinct outcome."}) == {"write_debt": 0}
    if repeat == "fresh_outbox":
        capture = Capture(client, tmp_path / "second", "work")
        capture.session_start({"session_id": "reused-session"})
    elif repeat == "reset_counter":
        capture.turn_number = 0
    capture.turn_start({"turn_number": 1, "message": "second request"})
    assert capture.turn_number == 1
    assert capture.turn_end({"reply": "Second distinct outcome."}) == {"write_debt": 0}
    layers = server.storage["layers"]
    assert len(layers) == 2
    assert server.native_write_calls == 2
    assert layers[0]["ref"] != layers[1]["ref"]
    for layer in layers:
        assert re.fullmatch(r"reused-session-t1-[0-9a-f]{32}", layer["ref"])
    assert "First distinct outcome." in layers[0]["body"]
    assert "Second distinct outcome." in layers[1]["body"]


def test_server_duplicate_with_different_body_reports_durable_write_debt(tmp_path, capture_store):
    client, server = capture_store
    original = Capture(client, tmp_path / "original", "work")
    original.session_start({"session_id": "duplicate"})
    original.turn_start({"message": "original request"})
    assert original.turn_end({"reply": "Original outcome."}) == {"write_debt": 0}
    spent_ref = server.storage["layers"][0]["ref"]
    capture = Capture(client, tmp_path / "new", "work")
    capture.session_start({"session_id": "duplicate"})
    capture.turn_start({"message": "different request"})
    capture._turn_request_id = spent_ref  # Simulate a spent ID from an older producer.
    assert capture.turn_end({"reply": "Lost outcome."}) == {"write_debt": 1}
    debt = capture.debt()
    assert debt[0]["permanent_error"] == "CaptureRejected"
    assert "Lost outcome." in debt[0]["layer"]["body"]
    assert "Write-debt: 1 unsent captures" in capture.inheritance_block()
    restored = Capture(client, tmp_path / "new", "work")
    restored.retry_outbox()
    assert restored.debt() == debt
    assert restored.turn_end({}) == {"write_debt": 1}
    assert server.native_write_calls == 1
    assert len(server.storage["layers"]) == 1
    assert "Original outcome." in server.storage["layers"][0]["body"]


def test_same_capture_rewrite_is_idempotent_without_write_debt(tmp_path, capture_store):
    capture = Capture(None, tmp_path / "original", "work")
    capture.session_start({"session_id": "idempotent"})
    capture.turn_start({"message": "request"})
    capture.turn_end({"reply": "Keep this outcome once."})
    item = capture.debt()[0]
    client, server = capture_store
    capture.client = client
    capture.retry_outbox()
    assert capture.debt() == []
    replay = Capture(client, tmp_path / "replay", "work")
    replay._enqueue(item["request_id"], item["layer"])
    assert replay.turn_end({}) == {"write_debt": 0}
    replay._enqueue(item["request_id"], item["layer"])
    assert replay.turn_end({}) == {"write_debt": 0}
    assert len(server.storage["layers"]) == 1
    assert server.native_write_calls == 1


def test_each_local_collision_adds_debt_without_overwriting_earlier_loss(tmp_path):
    capture = Capture(RejectingClient(), tmp_path, "work")
    capture.session_start({"session_id": "collision"})
    capture._enqueue("spent-ref", capture._layer("Original body", "turn"))
    capture.retry_outbox()
    assert capture.debt() == []
    for number in (1, 2):
        capture.turn_start({"message": f"Request {number}"})
        capture._turn_request_id = "spent-ref"
        result = capture.turn_end({"reply": f"Lost outcome {number}."})
        assert result == {"write_debt": number, "lost": ["spent-ref"]}
    debt = capture.debt()
    assert len(debt) == 2
    assert "Lost outcome 1." in debt[0]["layer"]["body"]
    assert "Lost outcome 2." in debt[1]["layer"]["body"]
    restored = Capture(None, tmp_path, "work")
    assert restored.debt() == debt


def test_resume_accepts_legacy_turn_refs_and_restores_completed_history(tmp_path):
    capture = Capture(None, tmp_path, "work")
    capture._enqueue("legacy-t7", capture._layer("Legacy outcome", "turn"))
    restored = Capture(None, tmp_path, "work")
    restored.session_start({"session_id": "legacy"})
    assert restored.turn_number == 7
    assert restored._completed[0]["request_id"] == "legacy-t7"
    restored.turn_start({"message": "continue"})
    restored.turn_end({"reply": "New outcome."})
    assert re.fullmatch(r"legacy-t8-[0-9a-f]{32}", restored.debt()[1]["request_id"])


def test_a_tool_error_returned_as_json_text_is_captured_as_failed(tmp_path):
    capture = Capture(None, tmp_path, "work", "", "tester")
    bus = Bus()
    capture.install(bus)
    bus.emit("session_start", {"session_id": "s"})
    bus.emit("turn_start", {"message": "Write a journal entry"})
    bus.emit("pre_tool", {"name": "clock_journal_entry", "id": "j", "arguments": {"author": "a"}})
    bus.emit("post_tool", {"name": "clock_journal_entry", "id": "j",
                           "result": json.dumps({"error": "body cannot be empty"})})
    bus.emit("pre_tool", {"name": "read_file", "id": "r", "arguments": {"path": "a"}})
    bus.emit("post_tool", {"name": "read_file", "id": "r", "result": "{not json at all"})
    bus.emit("turn_end", {"reply": "Done.", "success": True})
    body = capture.debt()[-1]["layer"]["body"]
    assert "clock_journal_entry: failed" in body
    assert "read_file: ok" in body


def test_a_long_session_rereads_its_inheritance_after_the_refresh_age(tmp_path, monkeypatch):
    reads = []

    class Client:
        def prepare_inheritance(self, *args):
            reads.append("project")
            return "project layer " + str(len(reads))

        def tree_view(self, *args):
            return "personal cover"

    clock = [1000.0]
    monkeypatch.setattr("tessera.capture.time.monotonic", lambda: clock[0])
    capture = Capture(Client(), tmp_path, "work", "personal", "tester", inheritance_refresh_seconds=900)
    first = capture.inheritance_block()
    clock[0] += 600
    assert capture.inheritance_block() == first
    assert len(reads) == 1
    clock[0] += 400
    refreshed = capture.inheritance_block()
    assert len(reads) == 2
    assert "project layer 2" in refreshed
    once = Capture(Client(), tmp_path / "once", "work", "personal", "tester", inheritance_refresh_seconds=0)
    once.inheritance_block()
    clock[0] += 100000
    once.inheritance_block()
    assert len(reads) == 3
