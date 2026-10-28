"""Independent offline voice consumers over temporary HTTP and recorded WS events."""

from contextlib import contextmanager
import io
import json
import threading
from unittest.mock import Mock

import pytest

from fake_backend import FakeBackend
from tessera.backend import Backend
from tessera.config import Config
from tessera.credentials import CredentialResolver, ResolvedCredential
from tessera.jev import JudgeKit
from tessera.server import create_server
from tessera.sessions import SessionManager
from test_server import call
from test_status_projection_consumers import disk_snapshot

PROVIDER_KEY = "provider-key-placeholder"
EPHEMERAL_KEY = "ephemeral-placeholder"
HTTP_KEY = "http-key-placeholder"
AUTH = {"Authorization": "Bearer " + HTTP_KEY}


@pytest.fixture(autouse=True)
def forbid_live_voice(monkeypatch):
    tripwires = []
    for owner, name in ((CredentialResolver, "resolve"), (Backend, "__init__"),
                        (Backend, "chat"), (JudgeKit, "_evaluate_batch")):
        tripwire = Mock(side_effect=AssertionError("Voice consumers require injected offline capabilities"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open", "subprocess.Popen"):
        tripwire = Mock(side_effect=AssertionError("Voice consumers cannot open live transports"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


def voice_config(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.voice.enabled = True
    config.voice.websocket_url = "wss://voice.example.com/realtime-placeholder"
    config.voice.model = "voice-model-placeholder"
    config.voice.api_base_url = "https://voice.example.com"
    config.voice.client_secret_path = "/client-secrets-placeholder"
    config.voice.api_key_env = "EXAMPLE_VOICE_PROVIDER_KEY"
    config.voice.client_protocol_prefix = "client-placeholder."
    return config


@contextmanager
def voice_server(config, monkeypatch, *, legacy_token=True):
    config.server.port = 0
    # The server has no auth; a stray legacy token variable must change nothing.
    if legacy_token:
        monkeypatch.setenv("TESSERA_SERVER_TOKEN", HTTP_KEY)
    else:
        monkeypatch.delenv("TESSERA_SERVER_TOKEN", raising=False)
    backend = FakeBackend([])
    server = create_server(config, backend_factory=lambda *args: backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, backend
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def fake_mint(manager, monkeypatch, response=None):
    monkeypatch.setenv("EXAMPLE_VOICE_PROVIDER_KEY", PROVIDER_KEY)
    manager.voice.clock = lambda: 100.0
    manager.voice.client.clock = lambda: 100.0
    resolver = Mock()
    resolver.resolve.return_value = ResolvedCredential(PROVIDER_KEY, "env")
    manager.voice.client.credentials = resolver
    calls = []
    payload = response if response is not None else {"value": EPHEMERAL_KEY, "expires_at": 160,
        "api_key": PROVIDER_KEY, "diagnostic": "private-mint-placeholder"}

    def send(request, timeout):
        calls.append(request)
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer " + PROVIDER_KEY
        assert timeout == manager.config.voice.timeout
        assert json.loads(request.data) == {"expires_after": {"seconds": manager.config.voice.secret_ttl_seconds}}
        return io.BytesIO(json.dumps(payload).encode())

    manager.voice.client.opener = send
    return calls, resolver


@pytest.mark.parametrize("configured", [False, True])
def test_disabled_voice_never_mints_connects_loads_runtime_or_writes(tmp_path, monkeypatch, configured):
    config = voice_config(tmp_path) if configured else Config(home=tmp_path, cwd=tmp_path)
    config.voice.enabled = False
    with SessionManager(config) as manager:
        slug = manager.create("voice fixture")["slug"]
        mint = Mock(side_effect=AssertionError("Disabled voice cannot mint"))
        connect = Mock(side_effect=AssertionError("Disabled voice cannot connect WebSocket"))
        runtime = Mock(side_effect=AssertionError("Disabled voice cannot load a chat runtime"))
        monkeypatch.setattr(manager.voice.client, "mint", mint)
        monkeypatch.setattr(manager, "runtime", runtime)
        monkeypatch.setattr("socket.create_connection", connect)
        before = disk_snapshot(tmp_path)
        for _ in range(2):
            assert manager.status()["voice"]["enabled"] is False
            with pytest.raises(ValueError):
                manager.voice.start(slug)
        assert manager._runtimes == {}
        assert disk_snapshot(tmp_path) == before
        mint.assert_not_called()
        connect.assert_not_called()
        runtime.assert_not_called()


@pytest.mark.parametrize("query,headers", [
    ("", {}),
    ("", {"Authorization": "Bearer wrong-placeholder"}),
    ("?token=" + HTTP_KEY, {}),
    ("", "same-origin"),
])
def test_loopback_mint_ignores_legacy_http_auth(tmp_path, monkeypatch, query, headers):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        if headers == "same-origin":
            headers = {"Origin": "http://127.0.0.1:%d" % server.server_address[1]}
        slug = server.manager.create("voice fixture")["slug"]
        calls, resolver = fake_mint(server.manager, monkeypatch)
        path = f"/api/sessions/{slug}/voice/start"
        before = disk_snapshot(tmp_path)
        code, status = call(server, "GET", "/api/status", headers=headers)
        assert code == 200 and status["voice"]["active_count"] == 0
        assert calls == []
        resolver.resolve.assert_not_called()
        assert disk_snapshot(tmp_path) == before
        code, started = call(server, "POST", path + query, {}, headers)
        assert code == 200 and len(calls) == 1
        assert resolver.resolve.call_count == 1
        assert started["protocols"] == ["client-placeholder." + EPHEMERAL_KEY]
        assert PROVIDER_KEY not in json.dumps(started) and HTTP_KEY not in json.dumps(started)
        assert call(server, "POST", f"/api/sessions/{slug}/voice/stop", {"voice_id": started["voice_id"]}) == (200, {"stopped": True})
        assert server.manager.voice.status()["active_count"] == 0


def test_loopback_mint_needs_no_http_key_and_keeps_provider_access_ephemeral(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch, legacy_token=False) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        calls, resolver = fake_mint(server.manager, monkeypatch)
        code, status = call(server, "GET", "/api/status")
        assert code == 200 and status["voice"]["active_count"] == 0
        assert calls == []
        resolver.resolve.assert_not_called()
        code, started = call(server, "POST", f"/api/sessions/{slug}/voice/start", {})
        assert code == 200 and len(calls) == 1 and resolver.resolve.call_count == 1
        assert started["protocols"] == ["client-placeholder." + EPHEMERAL_KEY]
        assert PROVIDER_KEY not in json.dumps(started) and HTTP_KEY not in json.dumps(started)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert PROVIDER_KEY.encode() not in path.read_bytes() and EPHEMERAL_KEY.encode() not in path.read_bytes()


def test_loopback_mint_returns_only_ephemeral_access_and_never_persists_secrets(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        calls, resolver = fake_mint(server.manager, monkeypatch)
        code, response = call(server, "POST", f"/api/sessions/{slug}/voice/start", {}, AUTH)
        assert code == 200
        assert len(calls) == 1 and resolver.resolve.call_count == 1
        assert response["protocols"] == ["client-placeholder." + EPHEMERAL_KEY]
        assert response["expires_at"] == 160
        # No tools are configured in this fixture, so the lease advertises none.
        # (With voice.tools set, the broker advertises those schemas.)
        assert response["session_update"]["session"]["tools"] == []
        assert server.manager._runtimes == {}
        assert PROVIDER_KEY not in json.dumps(response) and "private-mint-placeholder" not in json.dumps(response)
        assert EPHEMERAL_KEY not in response["websocket_url"]
        code, status = call(server, "GET", "/api/status", headers=AUTH)
        assert code == 200
        assert status["voice"] == {"enabled": True, "configured": True, "active_count": 1, "voices": [], "voice": ""}
        for private in (PROVIDER_KEY, EPHEMERAL_KEY, "EXAMPLE_VOICE_PROVIDER_KEY", "voice.example.com",
                        "voice-model-placeholder", "client-placeholder."):
            assert private not in json.dumps(status)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert PROVIDER_KEY.encode() not in path.read_bytes()
                assert EPHEMERAL_KEY.encode() not in path.read_bytes()


def test_provider_key_echo_cannot_be_accepted_as_ephemeral_access(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        calls, _ = fake_mint(server.manager, monkeypatch, {"value": PROVIDER_KEY, "expires_at": 160})
        code, error = call(server, "POST", f"/api/sessions/{slug}/voice/start", {}, AUTH)
        assert code >= 400
        assert len(calls) == 1
        assert PROVIDER_KEY not in json.dumps(error)
        assert server.manager.voice.status()["active_count"] == 0


class FakeWebSocket:
    """Recorded provider events enter the real bridge; server replies are outbound frames."""

    def __init__(self, events):
        self.incoming = [json.dumps(event) for event in events]
        self.sent = []

    def receive(self):
        return json.loads(self.incoming.pop(0))

    def send(self, event):
        self.sent.append(event)


def seed_typed_history(manager, slug):
    from tessera.log import ChatLog
    from tessera.tree import SummaryTree
    from tessera.view import View

    root = manager.path(slug)
    with ChatLog(root / "log") as log:
        tree = SummaryTree(root, log)
        view = View(root, tree)
        log.subscribe(view.on_message)
        log.append("user", "Typed fixture")
        log.append("agent", "Typed answer fixture")
        manager._save_work_count(slug, log.count())


def test_recorded_ws_partials_finals_and_replays_join_existing_chatlog_and_view(tmp_path, monkeypatch):
    from tessera.log import ChatLog
    from tessera.tree import SummaryTree
    from tessera.view import View

    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        seed_typed_history(server.manager, slug)
        calls, _ = fake_mint(server.manager, monkeypatch)
        prefix = f"/api/sessions/{slug}/voice"
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        # The provider is the voice agent: it gets the real prompt and the live view.
        assert started["session_update"]["session"]["instructions"].startswith("You are ")
        # The voice agent answers from the same conversation, so the live view is
        # part of the lease. A transcribe-only lease carried none of it.
        assert "Typed fixture" in started["session_update"]["session"]["instructions"]
        user = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "input-placeholder",
                "content_index": 0, "event_id": "event-placeholder", "transcript": "Spoken fixture café " + PROVIDER_KEY}
        events = [
            {"type": "response.output_audio.delta", "delta": "audio-placeholder"},
            {"type": "conversation.item.input_audio_transcription.updated", "item_id": "input-placeholder",
             "transcript": "partial-placeholder"},
            user,
            {**user, "event_id": "repeated-event-placeholder"},
            {"type": "response.output_audio_transcript.done", "item_id": "output-placeholder", "content_index": 0,
             "response_id": "response-placeholder", "transcript": "Spoken answer fixture"},
            {"type": "response.done", "response": {"id": "response-placeholder", "status": "completed", "output": []}},
        ]
        socket = FakeWebSocket(events)
        messages = []
        spoken_turn = None
        while socket.incoming:
            code, bridged = call(server, "POST", prefix + "/events",
                                 {"voice_id": started["voice_id"], "events": [socket.receive()]}, AUTH)
            assert code == 200
            messages.extend(bridged["messages"])
            if bridged.get("session_turn"):
                spoken_turn = bridged["session_turn"]
            for event in bridged["send"]:
                socket.send(event)
        assert socket.sent == []
        # The provider is the voice agent, so its output transcript is the reply.
        assert [row["kind"] for row in messages] == ["user", "agent"]
        # The provider answers on its own, so a plain spoken line does not also
        # steer the session -- that would answer twice.
        assert spoken_turn is None
        code, replay = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": events}, AUTH)
        assert code == 200 and replay["messages"] == [] and replay["send"] == []
        assert replay["session_turn"] is None
        server.manager.runtime(slug).wait(5)
        rows = list(server.manager.get(slug).log.iter())
        spoken = [row for row in rows if row.meta.get("voice_transcript")]
        assert [(row.kind, row.text) for row in spoken] == [
            ("user", "Spoken fixture café [REDACTED]"),
            ("agent", "Spoken answer fixture")]
        assert all(row.meta["provenance"] == "client_reported" for row in spoken)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                for private in (PROVIDER_KEY, EPHEMERAL_KEY, "partial-placeholder", "audio-placeholder"):
                    assert private.encode() not in raw


@pytest.mark.parametrize("state", ["foreign", "stopped", "expired"])
def test_rejected_ws_lease_cannot_append_transcripts_or_call_tools(tmp_path, monkeypatch, state):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        other = server.manager.create("other fixture")["slug"]
        calls, _ = fake_mint(server.manager, monkeypatch)
        code, started = call(server, "POST", f"/api/sessions/{slug}/voice/start", {}, AUTH)
        assert code == 200
        if state == "foreign":
            slug = other
        elif state == "stopped":
            code, stopped = call(server, "POST", f"/api/sessions/{slug}/voice/stop",
                                 {"voice_id": started["voice_id"]}, AUTH)
            assert code == 200 and stopped == {"stopped": True}
        else:
            server.manager.voice.clock = lambda: started["session_expires_at"] + 1
        before = disk_snapshot(tmp_path)
        event = {"type": "conversation.item.input_audio_transcription.completed", "item_id": "late-placeholder",
                 "transcript": "Rejected fixture"}
        code, error = call(server, "POST", f"/api/sessions/{slug}/voice/events",
                           {"voice_id": started["voice_id"], "events": [event]}, AUTH)
        assert code >= 400
        assert PROVIDER_KEY not in json.dumps(error) and EPHEMERAL_KEY not in json.dumps(error)
        assert disk_snapshot(tmp_path) == before
        assert server.manager._runtimes == {} and len(calls) == 1


def test_function_schemas_are_allowlisted_detached_and_recorded_batch_executes_once(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.tools = ["date"]
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        fake_mint(server.manager, monkeypatch)
        prefix = f"/api/sessions/{slug}/voice"
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        assert [tool["name"] for tool in started["session_update"]["session"]["tools"]] == ["date"]
        session = server.manager.get(slug)
        handler = Mock(side_effect=["first-tool-placeholder", "second-tool-placeholder"])
        monkeypatch.setattr(session.tools._tools["date"], "handler", handler)
        records = [
            {"type": "response.function_call_arguments.done", "response_id": "response-placeholder",
             "call_id": "call-one-placeholder", "name": "date", "arguments": "{}"},
            {"type": "response.function_call_arguments.done", "response_id": "response-placeholder",
             "call_id": "call-two-placeholder", "name": "date", "arguments": "{}"},
            {"type": "response.done", "response": {"id": "response-placeholder", "status": "completed"}},
        ]
        code, result = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": records}, AUTH)
        assert code == 200
        assert [event["type"] for event in result["send"]] == [
            "conversation.item.create", "conversation.item.create", "response.create"]
        assert handler.call_count == 2
        forged = {"type": "response.function_call_arguments.done", "response_id": "other-response-placeholder",
                  "call_id": "forged-call-placeholder", "name": "write_file",
                  "arguments": json.dumps({"path": "not-created-placeholder", "text": "fixture"})}
        code, _ = call(server, "POST", prefix + "/events",
                       {"voice_id": started["voice_id"], "events": [forged]}, AUTH)
        assert code >= 400
        assert handler.call_count == 2 and not (tmp_path / "not-created-placeholder").exists()


def test_allowed_risky_voice_function_keeps_existing_approval_policy(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.tools = ["write_file"]
    config.tools.approval = "deny_risky"
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        fake_mint(server.manager, monkeypatch)
        prefix = f"/api/sessions/{slug}/voice"
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        event = {"type": "response.function_call_arguments.done", "response_id": "response-placeholder",
                 "call_id": "write-call-placeholder", "name": "write_file",
                 "arguments": json.dumps({"path": "not-created-placeholder", "text": "fixture"})}
        code, result = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": [event]}, AUTH)
        assert code == 200
        output = result["send"][0]["item"]
        assert output["type"] == "function_call_output"
        assert json.loads(output["output"]) == {"error": "Voice tool was denied or failed"}
        assert not (tmp_path / "not-created-placeholder").exists()


def test_start_body_cannot_override_server_owned_context_tools_or_model(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        calls, resolver = fake_mint(server.manager, monkeypatch)
        before = disk_snapshot(tmp_path)
        for field, value in (("instructions", "override-placeholder"), ("model", "other-model-placeholder"),
                             ("tools", [{"type": "function", "name": "write_file"}])):
            code, _ = call(server, "POST", f"/api/sessions/{slug}/voice/start", {field: value}, AUTH)
            assert code == 400
        assert calls == [] and server.manager._runtimes == {}
        resolver.resolve.assert_not_called()
        assert disk_snapshot(tmp_path) == before


def test_voice_function_arguments_keep_normal_type_enum_and_extra_field_validation(tmp_path, monkeypatch):
    config = voice_config(tmp_path)
    config.voice.tools = ["date"]
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("voice fixture")["slug"]
        fake_mint(server.manager, monkeypatch)
        prefix = f"/api/sessions/{slug}/voice"
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200
        session = server.manager.get(slug)
        handler = Mock(side_effect=AssertionError("Invalid arguments must never reach the handler"))
        monkeypatch.setattr(session.tools._tools["date"], "handler", handler)
        records = [{"type": "response.function_call_arguments.done", "response_id": "response-placeholder",
                    "call_id": f"invalid-call-{index}-placeholder", "name": "date", "arguments": json.dumps(arguments)}
                   for index, arguments in enumerate(({"i": True}, {"source": "unknown-placeholder"}, {"unexpected": True}))]
        code, result = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": records}, AUTH)
        assert code == 200 and len(result["send"]) == 3
        assert all(json.loads(event["item"]["output"]) == {"error": "Voice tool was denied or failed"}
                   for event in result["send"])
        handler.assert_not_called()
        echoes = [row for row in session.log.iter() if row.kind == "echo"]
        assert len(echoes) == 3 and all(row.meta["success"] is False for row in echoes)
        assert all("ValueError" in row.text for row in echoes)
