"""Run objective Session scenarios and advisory Jev scores without live endpoints.

Usage: .venv/bin/python -m evals.run_evals [--output evals/results.json]
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from local_memory import QuietMemory

from tessera.capture import Capture
from tessera.config import Config
from tessera.hooks import HookBus
from tessera.jev import JevRouter
from tessera.loop import Session


SCENARIOS = [
    {"name": "direct_answer", "request": "What is two plus two?", "script": ["Two plus two is four."],
     "expected": "four", "route": "direct"},
    {"name": "tool_read", "request": "Read fact.txt and report its color.", "script": [
        {"tool_calls": [{"id": "read", "name": "read_file", "arguments": {"path": "fact.txt"}}]},
        "The color in fact.txt is turquoise.",
    ], "expected": "turquoise", "route": "tools"},
    {"name": "risk_block", "request": "Run rm marker.txt; the policy should block deletion.", "policy": "deny_risky", "script": [
        {"tool_calls": [{"id": "remove", "name": "shell", "arguments": {"command": "rm marker.txt"}}]},
        "Policy blocked deletion; marker.txt remains.",
    ], "expected": "blocked", "route": "tools"},
    {"name": "offline_recovery", "request": "Repeat this phrase: capture survives offline.",
     "script": ["capture survives offline"], "expected": "capture survives offline", "route": "direct"},
    {"name": "memory_tools", "request": "Recall the prior decision and note that it was checked.", "script": [
        {"tool_calls": [{"id": "recall", "name": "memory_recall", "arguments": {"query": "prior decision"}}]},
        {"tool_calls": [{"id": "note", "name": "memory_note", "arguments": {"text": "The prior decision was checked."}}]},
        "The prior decision was turquoise. I recorded that it was checked.",
    ], "expected": "turquoise", "route": "tools"},
    {"name": "backend_failure", "request": "Hello", "script": [RuntimeError("scripted backend failure")],
     "expected_error": "RuntimeError", "route": "direct"},
]


class MemorySink:
    """An idempotent fake memory service. No network or memory tree access."""

    def __init__(self):
        self.writes = {}
        self.reads = []

    def write_layer(self, project_id, request_id, layer):
        self.writes.setdefault(request_id, {"project_id": project_id, "request_id": request_id, "layer": layer})
        return {"ok": True, "request_id": request_id}

    def prepare_inheritance(self, project_id, anchor, budget):
        return {"history": "The prior decision was turquoise."}

    def tree_view(self, project_id, budget_chars):
        return {"view": "Prefer explicit checks."}

    def read_layers(self, project_id, **filters):
        self.reads.append({"project_id": project_id, **filters})
        return {"layers": [{"body": "The prior decision was turquoise."}]}


def _fake_backend(script):
    # Reuse the core worker's fixture without requiring tests to be an installed package.
    spec = importlib.util.spec_from_file_location("tessera_eval_fake_backend", ROOT / "tests" / "fake_backend.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FakeBackend(script)


def _run_scenario(scenario, root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "fact.txt").write_text("turquoise", encoding="utf-8")
    (root / "marker.txt").write_text("keep", encoding="utf-8")
    config = Config(home=root / "home", cwd=root)
    config.aethermind.project = "eval-work"
    config.jev.enabled = False
    config.tools.approval = scenario.get("policy", "never")
    sink = MemorySink()
    client = sink if scenario["name"] == "memory_tools" else QuietMemory()
    capture = Capture(client, root / "outbox", "eval-work", "eval-personal", "eval")
    router = JevRouter(enabled=False)
    hooks = HookBus()
    outcomes = []
    hooks.register("post_tool", lambda payload: outcomes.append(dict(payload)))
    backend = _fake_backend(scenario["script"])
    reply = ""
    error = None
    with Session(config, root=root / "chat", backend=backend, hooks=hooks, capture=capture, router=router) as session:
        try:
            reply = session.run_turn(scenario["request"])
        except Exception as failure:
            error = type(failure).__name__
        turn_debt = capture.debt()
        route = router.route(scenario["request"], "")["route"]
        checks = {"expected_route": route == scenario["route"]}
        if scenario.get("expected_error"):
            checks["expected_error"] = error == scenario["expected_error"]
        else:
            checks["no_exception"] = error is None
            checks["expected_reply"] = scenario["expected"].lower() in reply.lower()
        if scenario["name"] == "memory_tools":
            checks["recall_used_service"] = bool(sink.reads)
            checks["explicit_note_written"] = any("prior decision was checked" in item["layer"]["body"] for item in sink.writes.values())
            # A turn capture's request_id is unique per turn: a uuid4 hex may
            # follow the turn marker. Assert the marker where it sits, including
            # either construction. Asserting the absence of the uuid would pin
            # the shape that omitted it.
            checks["automatic_turn_written"] = any(
                re.search(r"-t1(-[0-9a-f]{32})?$", item["request_id"]) for item in sink.writes.values()
            )
        else:
            checks["automatic_turn_queued"] = len(turn_debt) == 1
            if not scenario.get("expected_error"):
                body = turn_debt[0]["layer"]["body"] if turn_debt else ""
                checks["decision_stored"] = body.startswith("Decision:") and scenario["expected"].lower() in body.lower()
        if scenario["name"] == "tool_read":
            body = turn_debt[0]["layer"]["body"] if turn_debt else ""
            checks["tool_outcome_captured"] = "read_file: ok" in body and "turquoise" in body
        if scenario["name"] == "risk_block":
            checks["side_effect_prevented"] = (root / "marker.txt").read_text() == "keep" and bool(outcomes and outcomes[0]["blocked"])
        if scenario["name"] == "backend_failure":
            checks["failure_captured"] = bool(turn_debt and "Failure" in turn_debt[0]["layer"]["body"])
    session_debt = capture.debt()
    if scenario["name"] == "offline_recovery":
        checks["turn_and_session_queued"] = len(session_debt) == 2
        capture.client = sink
        capture.retry_outbox()
        checks["debt_drained"] = not capture.debt() and len(sink.writes) == 2
    score = router.judge(scenario["request"], reply)
    return {"name": scenario["name"], "passed": all(checks.values()), "checks": checks,
            "reply": reply, "error": error, "judge_score": score,
            "judge_source": router.decisions[-1]["source"], "backend_calls": len(backend.calls)}


def run_evals(output=None):
    output = Path(output) if output is not None else ROOT / "evals" / "results.json"
    rows = []
    with tempfile.TemporaryDirectory(prefix="tessera-evals-") as directory:
        for scenario in SCENARIOS:
            try:
                row = _run_scenario(scenario, Path(directory) / scenario["name"])
            except Exception as error:
                row = {"name": scenario["name"], "passed": False, "checks": {"scenario_completed": False},
                       "error": type(error).__name__, "judge_score": 0, "judge_source": "unavailable"}
            rows.append(row)
    passed = 0
    score_sum = 0
    for row in rows:
        if row["passed"]:
            passed += 1
        score_sum += row["judge_score"]
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "backend": "FakeBackend",
              "judge_mode": "heuristic", "scenarios": rows,
              "summary": {"total": len(rows), "passed": passed, "pass_rate": passed / len(rows),
                          "mean_judge_score": score_sum / len(rows)},
              "limits": "Scripted scenarios check harness behavior. Heuristic reply scores do not establish real model quality."}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(output)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "evals" / "results.json")
    args = parser.parse_args(argv)
    results = run_evals(args.output)
    print(json.dumps(results["summary"], sort_keys=True))
    return 0 if results["summary"]["passed"] == results["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
