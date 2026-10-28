"""Independent semantic-gate consumers using only synthetic judgments and labels."""

import copy
import io
import json
from unittest.mock import patch

import pytest

from tessera.config import Config
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit, calibration_report


@pytest.fixture(autouse=True)
def forbid_external_work(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Judgment consumers must use fake transport only")

    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)


def configured_kit(tmp_path, monkeypatch, **options):
    monkeypatch.setenv("EXAMPLE_CONSUMER_JUDGE_KEY", "synthetic-key")
    return JudgeKit(api_key_env="EXAMPLE_CONSUMER_JUDGE_KEY", model="judge-placeholder",
                    url="https://judge.example.com/evaluate", enabled=True,
                    decision_log=tmp_path / "decisions.jsonl", **options)


def favorable_response(request, timeout):
    body = json.loads(request.data)
    answers = {}
    for name, question in body["questions"].items():
        if question["type"] == "score":
            answer = {"type": "score", "score": 2, "probabilities": {"0": 0, "1": 0, "2": 1}}
        else:
            favorable = name.startswith("independent_") or name.endswith("claims_supported")
            answer = {"type": "noul", "noul": .95 if favorable else .05}
        answers[name] = answer
    return io.BytesIO(json.dumps({"model": "judge-placeholder", "answers": answers}).encode())


def test_disabled_configured_jev_uses_heuristics_for_new_consumer_gates(tmp_path):
    config = Config(home=tmp_path)
    config.jev.enabled = False
    config.jev.url = "https://judge.example.com/evaluate"
    config.jev.model = "judge-placeholder"
    config.jev.credential_ref = "credential-placeholder"
    config.credentials = {"credential-placeholder": {"keychain_service": "service-placeholder",
                                                     "keychain_account": "account-placeholder"}}
    with patch.object(CredentialResolver, "resolve", side_effect=AssertionError("disabled credentials")) as resolver, \
         patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("disabled HTTP")) as transport:
        kit = JudgeKit.from_config(config)
        gate = kit.swarm_gates("Read synthetic value", tasks=[{"scope": ["A"]}, {"scope": ["B"]}],
                               checks={"tests": True}, artifacts=[{"content": "synthetic value"}])
        memory = kit.memory("Remember the decision", existing_layers=[{"body": "Remember the decision"}])
        outputs = kit.verify_outputs([
            {"request": "Read synthetic value", "reply": "Read synthetic value", "tool_outputs": [{"success": True}]},
            {"request": "Run tests", "reply": "Done", "tool_outputs": [{"exit_code": 1}]},
        ])
    resolver.assert_not_called()
    transport.assert_not_called()
    assert gate["source"] == memory["source"] == outputs[0]["source"] == "heuristic"
    assert gate["allowed"] is True and outputs[0]["allowed"] is True
    assert memory["duplicate_of_existing_layer"] > .5
    assert outputs[1]["source"] == "deterministic" and outputs[1]["allowed"] is False


def test_enabled_deterministic_rejections_do_not_resolve_judge_or_call_human(tmp_path, monkeypatch):
    kit = configured_kit(tmp_path, monkeypatch, human_gate=True)
    with patch.object(CredentialResolver, "resolve", side_effect=AssertionError("deterministic first")) as resolver, \
         patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("deterministic first")) as transport, \
         patch("builtins.input", side_effect=AssertionError("no interactive approval")):
        def unused_human(result):
            raise AssertionError("A failed check cannot be approved")

        swarm = kit.swarm_gates("Run tests", checks={"tests": False}, human=unused_human)
        output = kit.output_gate("Run tests", "Done", [{"success": False}], human=unused_human)
        batch = kit.verify_outputs([{"request": "Run tests", "reply": "Done", "checks": {"tests": False}}])
    resolver.assert_not_called()
    transport.assert_not_called()
    for result in (swarm, output, batch[0]):
        assert result["source"] == "deterministic" and result["allowed"] is False
    assert {row["question_id"] for row in kit.decisions} == {"_gate"}


@pytest.mark.parametrize("human_result", [False, "yes", "raise", True])
def test_human_gate_requires_explicit_true_and_callback_cannot_mutate_result(tmp_path, monkeypatch, human_result):
    kit = configured_kit(tmp_path, monkeypatch, human_gate=True)
    callbacks = []

    def approve(result):
        callbacks.append(copy.deepcopy(result))
        result["goal_met"] = 0
        if human_result == "raise":
            raise RuntimeError("synthetic-private-callback-error")
        return human_result

    with patch("tessera.jev.urllib.request.urlopen", side_effect=favorable_response) as transport:
        gate = kit.swarm_gates("Read artifact", artifacts=[{"content": "artifact"}],
                               checks={"tests": True}, human=approve)
    assert transport.call_count == len(callbacks) == 1
    assert callbacks[0]["goal_met"] == gate["goal_met"] == 1
    assert gate["requires_human"] is (human_result is not True)
    assert gate["allowed"] is (human_result is True)
    assert "synthetic-private-callback-error" not in json.dumps(kit.decisions)


def test_repeated_steps_override_positive_artifact_checks_before_judgment(tmp_path, monkeypatch):
    kit = configured_kit(tmp_path, monkeypatch, loop_window=2)
    with patch.object(CredentialResolver, "resolve", side_effect=AssertionError("literal loop first")) as resolver, \
         patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("literal loop first")) as transport:
        gate = kit.swarm_gates("Read artifact", artifacts=[{"content": "artifact"}],
                               checks={"tests": True}, steps=[{"action": "read"}, {"action": "read"}])
    resolver.assert_not_called()
    transport.assert_not_called()
    assert gate["source"] == "deterministic" and gate["goal_met"] == 0
    assert gate["looping"] == gate["escalate_to_human"] == 1
    assert gate["requires_human"] is True and gate["allowed"] is False


def test_verification_batch_excludes_failed_items_and_keeps_fallback_local(tmp_path, monkeypatch):
    kit = configured_kit(tmp_path, monkeypatch, batch_items=12)
    requests = []

    def respond(request, timeout):
        body = json.loads(request.data)
        requests.append(body)
        envelope = json.loads(favorable_response(request, timeout).read())
        envelope["answers"]["2_completion"] = {"type": "score", "score": "invalid"}
        return io.BytesIO(json.dumps(envelope).encode())

    items = [
        {"request": "Read artifact", "reply": "Artifact read", "tool_outputs": [{"success": True}]},
        {"request": "Run tests", "reply": "Done", "tool_outputs": [{"exit_code": 1}]},
        {"request": "Read artifact", "reply": "", "tool_outputs": []},
    ]
    with patch("tessera.jev.urllib.request.urlopen", side_effect=respond):
        first = kit.verify_outputs(items)
        assert kit.verify_outputs(items) == first
    assert len(requests) == 1
    assert set(requests[0]["questions"]) == {
        "0_completion", "0_scope_creep", "0_claims_supported", "0_claims_done_but_failed",
        "2_completion", "2_scope_creep", "2_claims_supported", "2_claims_done_but_failed",
    }
    assert [result["allowed"] for result in first] == [True, False, False]
    assert [result["source"] for result in first] == ["jev", "deterministic", "mixed"]
    assert first[2]["completion"] == 0
    records = [json.loads(line) for line in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    judgments = [row for row in records if row["point"] == "verification_batch"]
    assert len(judgments) == 16
    assert sum(row["cache_hit"] for row in judgments) == 8
    assert {row["source"] for row in judgments if row["question_id"] == "2_completion"} == {"heuristic"}
    assert all(row["source"] == "jev" for row in judgments if row["question_id"] != "2_completion")


@pytest.mark.parametrize("point", ["memory", "swarm", "outputs"])
def test_oversized_batches_fail_before_credential_resolution_or_logging(tmp_path, monkeypatch, point):
    kit = configured_kit(tmp_path, monkeypatch, batch_items=4)
    with patch.object(CredentialResolver, "resolve", side_effect=AssertionError("batch bound first")) as resolver, \
         patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("batch bound first")) as transport:
        with pytest.raises(ValueError, match="batch"):
            if point == "memory":
                kit.memory("Remember", existing_layers=["synthetic"] * 5)
            elif point == "swarm":
                kit.swarm_gates("Read", tasks=[{"scope": [label]} for label in ("A", "B", "C")])
            else:
                kit.verify_outputs([{"request": "Read", "reply": "Value"}] * 2)
    resolver.assert_not_called()
    transport.assert_not_called()
    assert kit.decisions == [] and not (tmp_path / "decisions.jsonl").exists()


def test_offline_output_batch_defaults_missing_tool_evidence_to_empty():
    kit = JudgeKit(enabled=False)
    assert kit.verify_outputs([]) == []
    result = kit.verify_outputs([{"request": "Read artifact", "reply": "Done, artifact read"}])[0]
    assert result["source"] == "heuristic"
    assert result["claims_supported"] < .5 and result["allowed"] is False


def test_calibration_joins_exact_labels_deduplicates_and_keeps_inputs_unchanged():
    records = [
        {"batch_id": "a", "question_id": "keep", "noul": .8, "source": "jev"},
        {"batch_id": "b", "question_id": "keep", "noul": .6, "source": "heuristic"},
        {"batch_id": "a", "question_id": "keep", "noul": .1, "source": "jev"},
        {"batch_id": "unlabeled", "question_id": "keep", "noul": .99},
        {"batch_id": "c", "question_id": "route", "choice": "tools", "probabilities": {"tools": .75, "direct": .25}},
        {"batch_id": "d", "question_id": "completion", "score": 1.5, "probabilities": {"0": .25, "1": 0, "2": .75}},
        {"batch_id": "e", "question_id": "_gate", "allowed": True},
    ]
    outcomes = {("a", "keep"): True, ("b", "keep"): False, ("unlabeled", "other-question"): False,
                ("c", "route"): True, ("d", "completion"): False, ("e", "_gate"): True}
    before = copy.deepcopy((records, outcomes))
    report = calibration_report(records, outcomes, threshold=.7)
    assert set(report) == {"keep", "route", "completion"}
    assert report["keep"]["n"] == 2 and report["keep"]["brier"] == pytest.approx(.2)
    assert report["keep"]["precision"] == 1
    assert report["route"] == {"n": 1, "brier": .0625, "precision": 1}
    assert report["completion"] == {"n": 1, "brier": .5625, "precision": 0}
    assert calibration_report(records, outcomes, threshold=.5)["keep"]["precision"] == .5
    assert (records, outcomes) == before


@pytest.mark.parametrize("label", [1, 0, "true", None])
def test_calibration_requires_explicit_boolean_outcomes(label):
    records = [{"batch_id": "a", "question_id": "keep", "noul": .8}]
    with pytest.raises(ValueError, match="booleans"):
        calibration_report(records, {("a", "keep"): label})
