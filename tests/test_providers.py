"""Provider wire contracts use synthetic HTTP responses and no cloud access."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from tessera.backend import Backend, BackendError


@contextmanager
def provider_server(script):
    pending = list(script)
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"path": self.path, "headers": dict(self.headers), "body": payload})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(pending.pop(0)).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "Look up an item",
          "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}}]


@pytest.mark.parametrize("kind", ["openai_responses", "anthropic_messages"])
def test_native_tool_round_trip_preserves_provider_blocks_and_secret_hygiene(kind, monkeypatch):
    secret = "synthetic-provider-secret"
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", secret)
    if kind == "openai_responses":
        native = [
            {"type": "reasoning", "id": "reason-1", "summary": [], "encrypted_content": "opaque-example"},
            {"type": "function_call", "id": "function-1", "call_id": "call-1", "name": "lookup", "arguments": '{"q":"example"}'},
        ]
        first = {"id": "response-1", "status": "completed", "output": native, "usage": {"input_tokens": 4}}
        final = {"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Result " + secret}]}], "usage": {"output_tokens": 2}}
    else:
        native = [
            {"type": "thinking", "thinking": "opaque example", "signature": "synthetic-signature"},
            {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "example"}},
        ]
        first = {"id": "message-1", "type": "message", "role": "assistant", "stop_reason": "tool_use",
                 "content": native, "usage": {"input_tokens": 4}}
        final = {"type": "message", "role": "assistant", "stop_reason": "end_turn", "content": [
            {"type": "text", "text": "Result " + secret}], "usage": {"output_tokens": 2}}
    messages = [{"role": "system", "content": "Follow local policy."}, {"role": "user", "content": "Look up example"}]
    with provider_server([first, final]) as (url, received):
        backend = Backend("native", url, "example-model", "EXAMPLE_PROVIDER_KEY", {
            "provider_kind": kind, "api_version": "2023-06-01", "parameters": {"max_tokens": 64}})
        reply = backend.chat(messages, tools=TOOLS)
        assert reply["tool_calls"] == [{"id": "call-1", "name": "lookup", "arguments": {"q": "example"}}]
        messages += [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"example"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "Found it"}]
        final_reply = backend.chat(messages, tools=TOOLS)
        assert final_reply["content"] == "Result [REDACTED]"
        assert final_reply["finish_reason"] == "stop"
        assert "thinking" not in reply and "encrypted_content" not in reply
    body = received[1]["body"]
    assert body["model"] == "example-model" and body["stream"] is False
    if kind == "openai_responses":
        assert received[0]["path"] == "/v1/responses"
        assert received[0]["headers"]["Authorization"] == "Bearer " + secret
        assert body["input"][2:4] == native
        assert body["input"][-1] == {"type": "function_call_output", "call_id": "call-1", "output": "Found it"}
        assert body["store"] is False and "previous_response_id" not in body
        assert body["tools"][0]["name"] == "lookup"
    else:
        assert received[0]["path"] == "/v1/messages"
        headers = {key.lower(): value for key, value in received[0]["headers"].items()}
        assert headers["x-api-key"] == secret and headers["anthropic-version"] == "2023-06-01"
        assert body["system"] == [{"type": "text", "text": "Follow local policy."}]
        assert body["messages"][1]["content"] == native
        assert body["messages"][-1]["content"] == [{"type": "tool_result", "tool_use_id": "call-1", "content": "Found it"}]
        assert body["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]


@pytest.mark.parametrize("kind,response", [
    ("openai_responses", {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                          "output": [{"type": "message", "content": [{"type": "output_text", "text": "Partial"}]}]}),
    ("anthropic_messages", {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "Partial"}]}),
])
def test_native_truncation_reaches_existing_loop_failure_contract(kind, response):
    with provider_server([response]) as (url, received):
        backend = Backend("partial", url, "example", extra={"provider_kind": kind, "api_version": "2023-06-01", "parameters": {"max_tokens": 64}})
        reply = backend.chat([{"role": "user", "content": "Hello"}])
    assert reply["content"] == "Partial" and reply["finish_reason"] == "length"


def test_unknown_adapter_and_missing_anthropic_options_fail_without_http():
    with provider_server([]) as (url, received):
        with pytest.raises((ValueError, BackendError), match="provider"):
            Backend("bad", url, "example", extra={"provider_kind": "unknown"}).chat([])
        with pytest.raises(BackendError, match="api_version|max_tokens"):
            Backend("missing", url, "example", extra={"provider_kind": "anthropic_messages"}).chat([])
    assert received == []


@pytest.mark.parametrize("kind,first,final", [
    ("openai_responses", {"status": "completed", "output": [{"type": "function_call", "call_id": "call-1", "name": "lookup", "arguments": '{"q":"example"}'}]},
     {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Found."}]}]}),
    ("anthropic_messages", {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "call-1", "name": "lookup", "input": {"q": "example"}}]},
     {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Found."}]}),
])
def test_configured_account_drives_real_session_tool_loop(tmp_path, kind, first, final):
    from tessera.config import load_config
    from tessera.loop import Session
    effects = []
    def configure(registry):
        def lookup(q):
            effects.append(q)
            return "Found example"
        registry.register("lookup", "Look up an item", TOOLS[0]["function"]["parameters"], lookup)
    with provider_server([first, final]) as (url, received):
        (tmp_path / "config.toml").write_text(f'''
[accounts.work]
provider_kind = "{kind}"
[backends.main]
base_url = "{url}"
model = "example-model"
account = "work"
api_version = "2023-06-01"
max_tokens = 64
''')
        with Session(load_config(tmp_path), configure_tools=configure) as session:
            assert session.run_turn("lookup example") == "Found."
            assert session.last_turn_success is True
        assert len(received) == 2
    assert effects == ["example"]


@pytest.mark.parametrize("kind,response", [
    ("openai_responses", {"status": "failed", "error": {"message": "synthetic-secret"}}),
    ("openai_responses", {"status": "completed", "output": [{"type": "function_call", "call_id": "c", "name": "lookup", "arguments": "[]"}]}),
    ("anthropic_messages", {"type": "error", "error": {"message": "synthetic-secret"}}),
    ("anthropic_messages", {"stop_reason": "max_tokens", "content": [{"type": "tool_use", "id": "c", "name": "lookup", "input": {}}]}),
])
def test_malformed_or_failed_native_reply_does_not_expose_body(kind, response):
    with provider_server([response]) as (url, received):
        backend = Backend("invalid", url, "example", extra={"provider_kind": kind, "api_version": "2023-06-01", "parameters": {"max_tokens": 64}})
        with pytest.raises(BackendError) as caught:
            backend.chat([{"role": "user", "content": "Hello"}])
    assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("kind,response", [
    ("openai_responses", {"status": "completed", "output": []}),
    ("anthropic_messages", {"stop_reason": "tool_use", "content": []}),
])
def test_native_empty_completion_cannot_claim_success(kind, response):
    with provider_server([response]) as (url, received):
        backend = Backend("empty", url, "example", extra={"provider_kind": kind, "api_version": "2023-06-01", "parameters": {"max_tokens": 64}})
        with pytest.raises(BackendError):
            backend.chat([{"role": "user", "content": "Hello"}])


def test_messages_preserves_explicit_cache_markers_and_configured_route():
    final = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Ready"}]}
    block = {"type": "text", "text": "Stable rules", "cache_control": {"type": "ephemeral"}}
    with provider_server([final]) as (url, received):
        backend = Backend("cache", url, "example", extra={"provider_kind": "anthropic_messages", "api_version": "2023-06-01",
                                                          "max_tokens": 64, "path": "/configured-messages"})
        assert backend.chat([{"role": "system", "content": [block]}, {"role": "user", "content": "Hello"}])["content"] == "Ready"
    assert received[0]["path"] == "/v1/configured-messages"
    assert received[0]["body"]["system"] == [block]


def test_responses_preserves_permissive_tool_schema_unless_strict_is_explicit():
    from copy import deepcopy
    tools = deepcopy(TOOLS)
    strict_tool = deepcopy(TOOLS[0])
    strict_tool["function"]["name"] = "strict_lookup"
    strict_tool["function"]["strict"] = True
    tools.append(strict_tool)
    response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "Ready"}]}]}
    with provider_server([response]) as (url, received):
        backend = Backend("schema", url, "example", extra={"provider_kind": "openai_responses"})
        backend.chat([{"role": "user", "content": "Hello"}], tools=tools)
    assert received[0]["body"]["tools"][0]["strict"] is False
    assert received[0]["body"]["tools"][1]["strict"] is True
    assert "strict" not in tools[0]["function"]


def test_keychain_secret_is_redacted_from_real_session_logs(tmp_path):
    import subprocess
    from tessera.credentials import CredentialResolver
    from tessera.config import load_config
    from tessera.loop import Session
    secret = "synthetic-keychain-secret"
    resolver = CredentialResolver({"work": {"keychain_service": "example-service", "keychain_account": "example-account"}},
                                  environ={}, platform="darwin", which=lambda name: name,
                                  run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, secret, ""))
    response = {"choices": [{"message": {"content": "Echo " + secret}, "finish_reason": "stop"}]}
    with provider_server([response]) as (url, received):
        backend = Backend("keychain", url, "example", extra={"credential_ref": "work"})
        backend._credentials = resolver
        with Session(load_config(tmp_path), backend=backend) as session:
            assert session.run_turn("Hello") == "Echo [REDACTED]"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore")


def test_chat_stream_reports_waiting_then_thought_and_answer_before_completion():
    from tessera.providers import ChatCompletionsAdapter
    events = []

    def lines():
        yield b": router waiting for serving endpoint\n"
        assert events == [("status", "Router connected, waiting for upstream output")]
        yield b'data: {"choices":[{"delta":{"reasoning_content":"Checking the inputs"}}]}\n'
        assert events[-1] == ("thought", "Checking the inputs")
        yield b'data: {"choices":[{"delta":{"content":"The answer"}}]}\n'
        assert events[-1] == ("text", "The answer")
        yield b"data: [DONE]\n"

    result = ChatCompletionsAdapter.assemble(lines(), lambda kind, text: events.append((kind, text)))
    assert result["choices"][0]["message"]["content"] == "The answer"
    assert result["choices"][0]["message"]["reasoning_content"] == "Checking the inputs"


def test_chat_stream_error_cannot_become_an_empty_success():
    from tessera.providers import ChatCompletionsAdapter
    with pytest.raises(ValueError, match="stream error"):
        ChatCompletionsAdapter.assemble([b'data: {"error":{"message":"private upstream error"}}'], lambda *args: None)


def test_waiting_snapshot_survives_reconnect_without_heartbeat_reset(tmp_path):
    from tessera.sessions import EventJournal
    journal = EventJournal(tmp_path / "events.jsonl")
    for at in ("2026-10-22T07:00:00+00:00", "2026-10-22T07:00:05+00:00"):
        journal.push_live("delta", {"turn_id": "turn", "kind": "status", "text": "Waiting upstream", "at": at})
    journal.push_live("delta", {"turn_id": "turn", "kind": "heartbeat", "text": "Connected", "at": "2026-10-22T07:00:10+00:00"})
    snapshot = journal.turn_snapshot("turn")
    assert snapshot["status"] == "Waiting upstream"
    assert snapshot["at"] == "2026-10-22T07:00:00+00:00"
    assert snapshot["thought"] == snapshot["text"] == ""
