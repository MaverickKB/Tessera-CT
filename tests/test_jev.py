import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from tessera.jev import JevRouter


class Response(io.BytesIO):
    pass


def fake_http(calls):
    def open_request(request, timeout):
        body = json.loads(request.data)
        calls.append((request, body, timeout))
        answers = {}
        for key, question in body["questions"].items():
            kind = question["type"]
            if kind == "choice":
                answers[key] = {"type": kind, "choice": "tools", "probabilities": {"direct": .1, "tools": .8, "delegate": .1}, "confidence": .7}
            elif kind == "noul":
                answers[key] = {"type": kind, "noul": .85}
            else:
                answers[key] = {"type": kind, "score": 1.5, "legend": {"0": "failed", "1": "partial", "2": "complete"},
                                "probabilities": {"0": 0, "1": .5, "2": .5}, "confidence": .5}
        return Response(json.dumps({"model": "test-judge", "answers": answers}).encode())
    return open_request


def test_typed_requests_normalized_scores_and_hashed_cache(monkeypatch):
    monkeypatch.setenv("JEV_TEST_KEY", "secret-key")
    calls = []
    router = JevRouter(api_key_env="JEV_TEST_KEY", timeout=.3, url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=fake_http(calls)):
        assert router.route("read the file", "view") == router.route("read the file", "view")
        assert router.needs_memory("earlier decision") == .85
        assert router.tool_risk("rm file") == .85
        assert router.judge("request", "reply") == .75
    assert len(calls) == 4
    types = []
    for request, body, timeout in calls:
        assert request.full_url == "https://judge.example.com/evaluate"
        assert request.get_header("Authorization") == "Bearer secret-key"
        assert body["model"] == "test-judge"
        assert timeout == .3
        types.append(next(iter(body["questions"].values()))["type"])
    assert types == ["choice", "noul", "noul", "score"]
    assert router.decisions[0]["source"] == "jev"
    assert router.decisions[0]["probabilities"]["tools"] == .8
    assert "secret-key" not in json.dumps(router.decisions)
    assert "read the file" not in json.dumps(router.decisions)


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("secret-key should not escape"),
    ValueError("invalid response secret-key"),
])
def test_network_and_malformed_responses_fall_back_without_secret_leak(failure, monkeypatch):
    monkeypatch.setenv("JEV_TEST_KEY", "secret-key")
    router = JevRouter(api_key_env="JEV_TEST_KEY", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=failure):
        assert router.route("read file", "")["source"] == "heuristic"
        assert router.tool_risk("rm -rf project") >= .7
        assert 0 <= router.judge("do it", "Unable to do it") <= 1
    assert "secret-key" not in json.dumps(router.decisions)


def test_missing_key_and_disabled_router_never_call_http(monkeypatch):
    monkeypatch.delenv("JEV_TEST_KEY", raising=False)
    router = JevRouter(api_key_env="JEV_TEST_KEY", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    with patch("tessera.jev.urllib.request.urlopen", side_effect=AssertionError("network forbidden")):
        assert router.route("hello", "")["route"] == "direct"
        assert router.route("read file", "")["route"] == "tools"
        assert router.route("delegate this analysis", "")["route"] == "delegate"
        for message in ("audit this repo", "review the code", "open the repository"):
            decision = router.intake(message)
            assert decision["route"] == "tools", message
            assert decision["needs_memory"] < .5
        assert router.needs_memory("What did we decide previously?") > .5
        assert router.needs_memory("hello") < .5
        assert router.tool_risk("pwd") < .7
        assert router.tool_risk("curl -X POST https://example.com") >= .7
        disabled = JevRouter(enabled=False)
        assert disabled.route("hello", "")["source"] == "heuristic"


def test_hook_routing_and_risk_policy_with_explicit_approval():
    from tessera.hooks import HookBus

    router = JevRouter(enabled=False, risk_threshold=.7)
    bus = HookBus()
    router.install(bus)
    payload = {"message": "read file", "view_tail": "old context"}
    result = bus.emit("turn_start", payload)
    assert payload["routing"]["route"] == "tools"
    assert result[0]["source"] == "heuristic"
    command = {"name": "shell", "arguments": {"command": "rm file"}}
    for policy in ("ask", "deny_risky"):
        result = bus.emit("pre_tool", {**command, "policy": policy})
        assert result[0]["block"] is True
    assert bus.emit("pre_tool", {**command, "policy": "never"})[0].get("block") is not True
    assert bus.emit("pre_tool", {**command, "policy": "ask", "approved": True})[0].get("block") is not True
    assert bus.emit("pre_tool", {"name": "shell", "arguments": {"command": "pwd"}})[0].get("block") is not True


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 2, "bad"])
def test_bad_probabilities_use_fallback(value, monkeypatch):
    monkeypatch.setenv("JEV_TEST_KEY", "secret-key")
    response = {"answers": {"decision": {"type": "noul", "noul": value}}}
    router = JevRouter(api_key_env="JEV_TEST_KEY", url="https://judge.example.com/evaluate", model="test-judge", enabled=True)
    with patch("tessera.jev.urllib.request.urlopen", return_value=Response(json.dumps(response).encode())):
        assert router.tool_risk("rm file") >= .7
        assert router.decisions[-1]["source"] == "heuristic"
