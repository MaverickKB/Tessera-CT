import json

from evals.run_evals import run_evals


def test_scenarios_run_real_session_and_report_objective_checks_and_judge(tmp_path):
    output = tmp_path / "results.json"
    results = run_evals(output)
    assert results["summary"]["total"] >= 5
    assert results["summary"]["passed"] == results["summary"]["total"]
    assert results["judge_mode"] == "heuristic"
    assert json.loads(output.read_text()) == results
    scenarios = {row["name"]: row for row in results["scenarios"]}
    assert scenarios["tool_read"]["checks"]["tool_outcome_captured"] is True
    assert scenarios["risk_block"]["checks"]["side_effect_prevented"] is True
    assert scenarios["offline_recovery"]["checks"]["debt_drained"] is True
    assert scenarios["backend_failure"]["checks"]["failure_captured"] is True
    for row in results["scenarios"]:
        assert 0 <= row["judge_score"] <= 1
        assert row["checks"] and all(row["checks"].values())
        assert row["judge_source"] == "heuristic"


def test_default_eval_does_not_use_live_key_or_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "must-not-be-used")
    monkeypatch.setenv("TESSERA_AETHERMIND_URL", "https://unused.invalid/mcp")
    from unittest.mock import patch

    with patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("no live eval calls")):
        results = run_evals(tmp_path / "results.json")
    assert results["summary"]["pass_rate"] == 1.0
