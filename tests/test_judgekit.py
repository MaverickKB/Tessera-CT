import io
import json
from unittest.mock import patch

from tessera.jev import JudgeKit
from tessera.capture import Capture
from tessera.hooks import HookBus


def typed_http(calls, malformed=None):
    def respond(request, timeout):
        body = json.loads(request.data)
        calls.append(body)
        answers = {}
        for name, question in body["questions"].items():
            kind = question["type"]
            if kind == "noul":
                answer = {"type": kind, "noul": .8}
            elif kind == "choice":
                options = list(question["criteria"])
                choice = "correction" if "correction" in options else "tools"
                probabilities = {}
                for option in options:
                    probabilities[option] = 1.0 if option == choice else 0.0
                answer = {"type": kind, "choice": choice, "probabilities": probabilities, "confidence": 1.0}
            else:
                answer = {"type": kind, "score": 1.5, "probabilities": {"0": 0, "1": .5, "2": .5}, "confidence": .5}
            answers[name] = answer
        if malformed in answers:
            answers[malformed] = {"type": "noul", "noul": "invalid"}
        return io.BytesIO(json.dumps({"model": "jev-test-pinned", "answers": answers}).encode())
    return respond


def test_batch_each_point_and_log_every_question_even_cache_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "do-not-log-this-key")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", decision_log=tmp_path / "decisions.jsonl", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    calls = []
    with patch("tessera.jev.urllib.request.urlopen", side_effect=typed_http(calls)):
        first = kit.intake("Read the file", "view")
        assert first["route"] == "tools" and first["needs_clarification"] == .8
        assert kit.intake("Read the file", "view") == first
        assert kit.tool_gate("shell", {"command": "rm file"})["risk"] == .8
        check = kit.verify_output("Read it", "Done", [{"success": True, "result": "fact"}])
        assert check["completion"] == .75
        assert kit.memory("Actually use the new value", "Understood")["layer_type"] == "correction"
        assert kit.faithfulness("keep the exact decision", "the decision") == .8
    assert len(calls) == 5
    assert [len(body["questions"]) for body in calls] == [3, 1, 4, 3, 1]
    records = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert len(records) == 15
    assert sum(row["cache_hit"] for row in records) == 3
    for row in records:
        assert row["question_id"] and row["model_id"] == "jev-test-pinned"
        assert row["probabilities"] and row["latency_ms"] >= 0
        assert row["batch_id"] and row["text_hash"]
    encoded = json.dumps(records)
    assert "do-not-log-this-key" not in encoded and "Read the file" not in encoded


def test_one_malformed_answer_preserves_valid_siblings(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "test-key")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", decision_log=tmp_path / "decisions.jsonl", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    calls = []
    with patch("tessera.jev.urllib.request.urlopen", side_effect=typed_http(calls, "needs_clarification")):
        intake = kit.intake("Read the file")
    assert intake["route"] == "tools" and intake["needs_memory"] == .8
    assert intake["needs_clarification"] < .5
    assert len(calls) == 1
    sources = {row["question_id"]: row["source"] for row in kit.decisions}
    assert sources == {"route": "jev", "needs_memory": "jev", "needs_clarification": "heuristic"}


def test_offline_capture_marks_correction_and_records_evidence_even_low_keep(tmp_path):
    kit = JudgeKit(enabled=False, decision_log=tmp_path / "decisions.jsonl")
    capture = Capture(None, tmp_path / "outbox", "work", "", "tester", judgekit=kit)
    hooks = HookBus()
    capture.install(hooks)
    hooks.emit("session_start", {"session_id": "j1"})
    hooks.emit("turn_start", {"message": "Actually, use the new path instead."})
    hooks.emit("post_tool", {"name": "shell", "result": {"exit_code": 1}, "success": False})
    hooks.emit("turn_end", {"reply": "Done, tests passed.", "success": True})
    layer = capture.debt()[0]["layer"]
    assert layer["type"] == "correction" and "!" in layer["markers"]
    assert "Decision: correction" in layer["body"]
    assert "Reply claimed success while a tool failed." in layer["body"]
    assert "Output verification" not in layer["body"] and "claims_done_but_failed" not in layer["body"]
    hooks.emit("turn_start", {"message": "Thanks"})
    hooks.emit("turn_end", {"reply": "Welcome", "success": True})
    assert len(capture.debt()) == 2
    assert kit.memory("Thanks", "Welcome")["keep"] < .5


def test_supplied_verification_is_reused_and_bound_logs_survive_restart(tmp_path):
    kit = JudgeKit(enabled=False)
    kit.bind_session(tmp_path, "session")
    capture = Capture(None, tmp_path / "outbox", "work", "", "tester", judgekit=kit)
    hooks = HookBus()
    capture.install(hooks)
    hooks.emit("turn_start", {"message": "Hello"})
    hooks.emit("turn_end", {"reply": "Hello", "verification": {"completion": 1, "claims_supported": 1}})
    assert {row["point"] for row in kit.decisions} == {"memory"}
    before = (tmp_path / "decisions.jsonl").read_bytes()
    reopened = JudgeKit(enabled=False)
    reopened.bind_session(tmp_path, "session")
    reopened.faithfulness("abc", "abc")
    assert (tmp_path / "decisions.jsonl").read_bytes().startswith(before)


def test_optional_judgment_is_inert_until_all_choices_are_configured(monkeypatch):
    for name in ("TESSERA_JEV_ENABLED", "TESSERA_JEV_URL", "TESSERA_JEV_MODEL", "TESSERA_JEV_API_KEY_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "present-but-not-configured")
    kit = JudgeKit()
    with patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("must remain offline")):
        assert kit.intake("Hello")["source"] == "heuristic"
        assert kit.compaction_judge("never delete files", "never delete files") >= .5
    assert kit.url == "" and kit.model == "" and kit.enabled is False


def test_real_session_shares_kit_and_verifies_each_turn_once(tmp_path, monkeypatch):
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.loop import Session

    monkeypatch.setenv("JUDGE_TEST_KEY", "session-test-key")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    config = Config(home=tmp_path, cwd=tmp_path)
    config.jev.enabled = False
    (tmp_path / "fact.txt").write_text("A fact")
    backend = FakeBackend([{"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "fact.txt"}}]}, "The file contains a fact."])
    calls = []
    from tessera.http_cancel import CancellableHTTP
    def cancellable_transport():
        transport = CancellableHTTP()
        transport.opener.open = typed_http(calls)
        return transport
    monkeypatch.setattr('tessera.jev.CancellableHTTP', cancellable_transport)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=typed_http(calls)):
        with Session(config, backend=backend, router=kit, root=tmp_path / "chat") as session:
            session.run_turn("Read fact.txt")
            assert session.capture.judgekit is kit
            body = session.capture.debt()[0]["layer"]["body"]
            assert "read_file: ok" in body and "The file contains a fact." in body
            assert "Output verification" not in body
    assert len(calls) == 4
    assert [len(call["questions"]) for call in calls] == [3, 1, 4, 3]
    assert (tmp_path / "chat" / "decisions.jsonl").is_file()


def test_truncated_http_response_falls_back_without_dropping_batch(monkeypatch):
    import http.client

    monkeypatch.setenv("JUDGE_TEST_KEY", "test-key")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"partial")):
        assert kit.intake("Hello")["source"] == "heuristic"
    assert len(kit.decisions) == 3


def test_cached_answers_are_detached_and_eviction_still_logs_each_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "synthetic-key")
    log = tmp_path / "decisions.jsonl"
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate",
                   model="test-judge", enabled=True, cache_size=2, decision_log=log)
    calls = []
    with patch("tessera.jev.urllib.request.urlopen", side_effect=typed_http(calls)):
        first = kit.intake("Read the file", "first context")
        first["probabilities"]["tools"] = 0
        cached = kit.intake("Read the file", "first context")
        assert cached["probabilities"] == {"direct": 0, "tools": 1, "delegate": 0}
        assert len(calls) == 1
        kit.intake("Read the file", "changed context")
        kit.intake("Read another file", "changed context")
        kit.intake("Read the file", "first context")
    assert len(calls) == 4
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 15
    assert len(kit.decisions) == 2
    batches = [records[start:start + 3] for start in range(0, 15, 3)]
    assert [batch[0]["cache_hit"] for batch in batches] == [False, True, False, False, False]
    assert len({batch[0]["batch_id"] for batch in batches}) == 5
    for batch in batches:
        assert {row["question_id"] for row in batch} == {"route", "needs_memory", "needs_clarification"}
        assert len({row["batch_id"] for row in batch}) == 1
        route = next(row for row in batch if row["question_id"] == "route")
        assert route["probabilities"] == {"direct": 0, "tools": 1, "delegate": 0}
    assert batches[0][0]["text_hash"] == batches[1][0]["text_hash"] == batches[4][0]["text_hash"]
    assert batches[0][0]["text_hash"] != batches[2][0]["text_hash"]


def test_verification_partial_batch_preserves_valid_answers_and_logs_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "synthetic-key")
    log = tmp_path / "decisions.jsonl"
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate",
                   model="test-judge", enabled=True, decision_log=log)
    envelope = {"model": "synthetic-pinned-judge", "answers": {
        "completion": {"type": "score", "score": 1, "probabilities": {"0": 0, "1": 1, "2": 0}},
        "scope_creep": {"type": "noul", "noul": .25},
        "claims_supported": {"type": "noul", "noul": True},
        "unrequested": {"type": "noul", "noul": .99},
    }}
    with patch("tessera.jev.urllib.request.urlopen",
               return_value=io.BytesIO(json.dumps(envelope).encode())) as transport:
        result = kit.verify_output("Run the tests", "Done, all tests passed.",
                                   [{"result": {"exit_code": 1}}])
    assert transport.call_count == 1
    assert result == {"completion": .5, "scope_creep": .25, "claims_supported": .1,
                      "claims_done_but_failed": .95, "source": "mixed"}
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 4
    assert len({row["batch_id"] for row in records}) == 1
    indexed = {row["question_id"]: row for row in records}
    assert set(indexed) == {"completion", "scope_creep", "claims_supported", "claims_done_but_failed"}
    for name in ("completion", "scope_creep"):
        assert indexed[name]["source"] == "jev"
        assert indexed[name]["model_id"] == "synthetic-pinned-judge"
    for name in ("claims_supported", "claims_done_but_failed"):
        assert indexed[name]["source"] == "heuristic"
        assert indexed[name]["model_id"] == "heuristic"
    assert indexed["claims_done_but_failed"]["probabilities"]["true"] == .95
    assert "Run the tests" not in log.read_text()
    assert "synthetic-key" not in log.read_text()


def test_disabled_configured_kit_uses_only_heuristics_at_all_existing_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "synthetic-key")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate",
                   model="test-judge", enabled=False, decision_log=tmp_path / "decisions.jsonl")
    bus = HookBus()
    kit.install(bus)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("offline boundary")) as transport:
        intake = bus.emit("turn_start", {"message": "Recall our previous decision"})[0]
        risk = bus.emit("pre_tool", {"name": "write_file", "arguments": {"path": "fact.txt"},
                                     "policy": "deny_risky"})[0]
        verification = kit.verify_output("Run tests", "Done, tests passed", [{"success": False}])
        memory = kit.memory("Actually, never delete the file", "Understood", verification=verification)
        assert kit.compaction_judge("delete the file", "never delete the file") < .5
    transport.assert_not_called()
    assert intake["source"] == risk["source"] == verification["source"] == memory["source"] == "heuristic"
    assert intake["needs_memory"] > .5
    assert risk["block"] is True
    assert verification["completion"] == 0
    assert verification["claims_done_but_failed"] > .5
    assert memory["layer_type"] == "correction"
    assert {row["source"] for row in kit.decisions} == {"heuristic"}


def test_calibration_log_failure_preserves_decisions_and_can_rebind(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE_TEST_KEY", "synthetic-key")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("preserve this file")
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate",
                   model="test-judge", enabled=True, decision_log=blocked / "decisions.jsonl")
    calls = []
    with patch("tessera.jev.urllib.request.urlopen", side_effect=typed_http(calls)):
        first = kit.intake("Read synthetic material")
        assert first["source"] == "jev"
        assert len(kit.decisions) == len(kit.log_errors) == 3
        kit.bind_session(tmp_path / "recovered", "synthetic-session")
        assert kit.intake("Read synthetic material") == first
    assert len(calls) == 1
    assert blocked.read_text() == "preserve this file"
    log = tmp_path / "recovered" / "decisions.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 3
    assert {row["session_id"] for row in records} == {"synthetic-session"}
    assert all(row["cache_hit"] and row["source"] == "jev" for row in records)
    assert {row["question_id"] for row in records} == {"route", "needs_memory", "needs_clarification"}


def test_calibration_records_redact_echoed_credentials_and_discard_remote_extras(tmp_path, monkeypatch):
    key = "synthetic-secret"
    monkeypatch.setenv("JUDGE_TEST_KEY", key)
    kit = JudgeKit(api_key_env="JUDGE_TEST_KEY", url="https://judge.example.com/evaluate",
                   model="test-judge", enabled=True)
    kit.bind_session(tmp_path, "session-" + key)
    envelope = {"model": "judge-" + key, "debug": "synthetic-private-diagnostic", "answers": {
        "faithfulness": {"type": "noul", "noul": .85, "explanation": "synthetic-private-diagnostic"},
    }}
    with patch("tessera.jev.urllib.request.urlopen",
               return_value=io.BytesIO(json.dumps(envelope).encode())):
        assert kit.faithfulness("synthetic-private-source", "synthetic-private-summary") == .85
    records = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert records == kit.decisions
    assert records[0]["source"] == "jev" and records[0]["noul"] == .85
    assert records[0]["model_id"] == "judge-[REDACTED]"
    assert records[0]["session_id"] == "session-[REDACTED]"
    serialized = json.dumps(records)
    for private in (key, "synthetic-private-diagnostic", "synthetic-private-source", "synthetic-private-summary"):
        assert private not in serialized
