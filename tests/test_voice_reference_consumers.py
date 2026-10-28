"""Credential-reference voice consumers; OS helpers and provider replies are fakes."""

import io
import json
import subprocess
from unittest.mock import Mock

import pytest

from tessera.backend import Backend
from tessera.credentials import CredentialResolver
from tessera.jev import JudgeKit
from tessera.log import ChatLog
from test_server import call
from test_voice_consumers import AUTH, EPHEMERAL_KEY, HTTP_KEY, voice_config, voice_server


ENV_KEY = "env-placeholder"
KEYCHAIN_KEY = "keychain-placeholder"
FALLBACK_KEY = "fallback-placeholder"
ROTATED_KEY = "rotated-placeholder"
SECRETS = (ENV_KEY, KEYCHAIN_KEY, FALLBACK_KEY, ROTATED_KEY, EPHEMERAL_KEY, HTTP_KEY)


@pytest.fixture(autouse=True)
def offline_reference_capabilities(monkeypatch):
    tripwires = []
    for owner, name in ((Backend, "__init__"), (Backend, "chat"), (JudgeKit, "_evaluate_batch"),
                        (subprocess, "Popen")):
        tripwire = Mock(side_effect=AssertionError("Reference consumers require injected offline capabilities"))
        monkeypatch.setattr(owner, name, tripwire)
        tripwires.append(tripwire)
    for target in ("urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
        tripwire = Mock(side_effect=AssertionError("Mint transport must be an in-memory fixture"))
        monkeypatch.setattr(target, tripwire)
        tripwires.append(tripwire)
    yield
    for tripwire in tripwires:
        tripwire.assert_not_called()


def reference_config(tmp_path, monkeypatch, platform="darwin", mode="keychain"):
    config = voice_config(tmp_path)
    config.voice.api_key_env = ""
    config.voice.credential_ref = "voice-reference-placeholder"
    config.voice.max_session_seconds = 45
    config.credentials = {
        "voice-reference-placeholder": {"env": "EXAMPLE_VOICE_REFERENCE_KEY",
            "keychain_service": "service-placeholder", "keychain_account": "account-placeholder",
            "keychain_command": "helper-placeholder", "keychain_timeout": .25,
            "config_ref": "fallback-reference-placeholder"},
        "fallback-reference-placeholder": {"env": "EXAMPLE_VOICE_FALLBACK_KEY"},
    }
    environment = {}
    if mode == "env":
        environment["EXAMPLE_VOICE_REFERENCE_KEY"] = ENV_KEY
    elif mode == "fallback":
        environment["EXAMPLE_VOICE_FALLBACK_KEY"] = FALLBACK_KEY
    which = Mock(return_value="helper-placeholder")
    helper = Mock(return_value=subprocess.CompletedProcess([], 1 if mode == "fallback" else 0,
                                                          " " + KEYCHAIN_KEY + "\n", EPHEMERAL_KEY))
    resolver = CredentialResolver(config.credentials, environ=environment, platform=platform, which=which, run=helper)
    resolve = resolver.resolve
    sources = []

    def resolved(*args, **kwargs):
        result = resolve(*args, **kwargs)
        sources.append(result.source)
        assert result.value not in repr(result)
        return result

    resolver.resolve = Mock(side_effect=resolved)
    def fake_resolver(references, *, oauth_home=""):
        assert oauth_home == str(config.home), "voice must resolve credentials from the account home"
        return resolver
    monkeypatch.setattr("tessera.voice.CredentialResolver", fake_resolver)
    return config, resolver, helper, which, sources


def mint_fixture(manager, resolver):
    assert manager.voice.client.credentials is resolver
    manager.voice.clock = lambda: 100.0
    manager.voice.client.clock = lambda: 100.0
    requests = []

    def mint(request, timeout):
        requests.append(request)
        assert request.full_url == "https://voice.example.com/client-secrets-placeholder"
        assert request.get_method() == "POST" and timeout == manager.config.voice.timeout
        assert json.loads(request.data) == {"expires_after": {"seconds": manager.config.voice.secret_ttl_seconds}}
        return io.BytesIO(json.dumps({"value": EPHEMERAL_KEY, "expires_at": 160,
                                     "diagnostic": KEYCHAIN_KEY}).encode())

    manager.voice.client.opener = mint
    return requests


def private_data_absent(tmp_path, *responses):
    for response in responses:
        for secret in SECRETS:
            assert secret not in json.dumps(response)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for secret in SECRETS:
                assert secret.encode() not in raw


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("mode", ["env", "keychain", "fallback"])
def test_reference_resolution_drives_loopback_voice_lifecycle_without_native_helpers(
        tmp_path, monkeypatch, platform, mode):
    config, resolver, helper, which, sources = reference_config(tmp_path, monkeypatch, platform, mode)
    expected = {"env": ENV_KEY, "keychain": KEYCHAIN_KEY, "fallback": FALLBACK_KEY}[mode]
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Reference voice fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        requests = mint_fixture(server.manager, resolver)
        code, before = call(server, "GET", "/api/status")
        assert code == 200 and before["voice"]["active_count"] == 0
        code, passive = call(server, "GET", f"/api/sessions/{slug}")
        assert code == 200 and passive["slug"] == slug and requests == []
        resolver.resolve.assert_not_called()
        helper.assert_not_called()
        which.assert_not_called()
        code, started = call(server, "POST", prefix + "/start", {})
        assert code == 200 and started["max_session_seconds"] == 45
        assert started["protocols"] == ["client-placeholder." + EPHEMERAL_KEY]
        assert sources == ["keychain" if mode == "keychain" else "env"]
        resolver.resolve.assert_called_once_with("voice-reference-placeholder", env="")
        assert len(requests) == 1 and requests[0].get_header("Authorization") == "Bearer " + expected
        if mode == "env":
            helper.assert_not_called()
            which.assert_not_called()
        else:
            which.assert_called_once_with("helper-placeholder")
            arguments = (["find-generic-password", "-s", "service-placeholder", "-a", "account-placeholder", "-w"]
                         if platform == "darwin" else ["lookup", "service", "service-placeholder", "account", "account-placeholder"])
            helper.assert_called_once_with(["helper-placeholder"] + arguments, capture_output=True, text=True,
                                           stdin=subprocess.DEVNULL, timeout=.25, check=False)
        events = [
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "typed-input-placeholder",
             "transcript": "Typed fixture " + expected},
            {"type": "response.output_audio.delta", "delta": "discard-audio-placeholder"},
            {"type": "response.output_audio_transcript.done", "item_id": "agent-output-placeholder",
             "response_id": "response-placeholder", "transcript": "Voice fixture " + EPHEMERAL_KEY},
            {"type": "response.done", "response": {"id": "response-placeholder", "status": "completed"}},
        ]
        code, bridged = call(server, "POST", prefix + "/events",
                             {"voice_id": started["voice_id"], "events": events})
        # The provider is the voice agent, so its output transcript is the reply.
        assert code == 200 and [row["kind"] for row in bridged["messages"]] == ["user", "agent"]
        code, replay = call(server, "POST", prefix + "/events",
                            {"voice_id": started["voice_id"], "events": events})
        assert code == 200 and replay["messages"] == [] and replay["send"] == []
        code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]})
        assert code == 200 and stopped == {"stopped": True}
        code, status = call(server, "GET", "/api/status")
        assert code == 200 and status["voice"]["active_count"] == 0
        assert resolver.resolve.call_count == 1
        # No tools are configured here, so the provider answers as the voice agent
        # and no session runtime is started; the log/view is opened lazily instead.
        assert slug not in server.manager._runtimes
        client_without_protocol = {key: value for key, value in started.items() if key != "protocols"}
        private_data_absent(tmp_path, before, passive, client_without_protocol, bridged, replay, stopped, status)
        rows = list(server.manager.get(slug).log.iter())
        spoken = [row for row in rows if row.meta.get("provenance") == "client_reported"]
        assert [row.text for row in spoken] == ["Typed fixture [REDACTED]", "Voice fixture [REDACTED]"]
        assert all(b"discard-audio-placeholder" not in path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())


@pytest.mark.parametrize("failure", ["missing", "timeout", "nonzero"])
def test_unavailable_keychain_never_mints_and_recovers_without_a_reserved_lease(tmp_path, monkeypatch, failure):
    config, resolver, helper, which, _ = reference_config(tmp_path, monkeypatch)
    config.credentials["voice-reference-placeholder"]["config_ref"] = ""
    resolver.references["voice-reference-placeholder"].config_ref = ""
    if failure == "missing":
        which.return_value = None
    elif failure == "timeout":
        helper.side_effect = subprocess.TimeoutExpired("helper-placeholder", .25, output=KEYCHAIN_KEY, stderr=EPHEMERAL_KEY)
    else:
        helper.return_value = subprocess.CompletedProcess([], 1, KEYCHAIN_KEY, EPHEMERAL_KEY)
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Reference voice fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        requests = mint_fixture(server.manager, resolver)
        code, rejected = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 503 and rejected["error_category"] == "credential" and requests == []
        assert server.manager.voice.status()["active_count"] == 0 and server.manager.voice._leases == {}
        private_data_absent(tmp_path, rejected)
        which.return_value = "helper-placeholder"
        helper.side_effect = None
        helper.return_value = subprocess.CompletedProcess([], 0, KEYCHAIN_KEY, "")
        code, started = call(server, "POST", prefix + "/start", {}, AUTH)
        assert code == 200 and len(requests) == 1
        code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]}, AUTH)
        assert code == 200 and stopped == {"stopped": True}


def test_keychain_rotation_is_resolved_again_for_each_new_voice_lease(tmp_path, monkeypatch):
    config, resolver, helper, _, sources = reference_config(tmp_path, monkeypatch)
    helper.side_effect = [subprocess.CompletedProcess([], 0, KEYCHAIN_KEY, ""),
                          subprocess.CompletedProcess([], 0, ROTATED_KEY, "")]
    with voice_server(config, monkeypatch) as (server, _):
        slug = server.manager.create("Reference voice fixture")["slug"]
        prefix = f"/api/sessions/{slug}/voice"
        requests = mint_fixture(server.manager, resolver)
        identities = []
        for secret in (KEYCHAIN_KEY, ROTATED_KEY):
            code, started = call(server, "POST", prefix + "/start", {}, AUTH)
            assert code == 200 and requests[-1].get_header("Authorization") == "Bearer " + secret
            identities.append(started["voice_id"])
            code, stopped = call(server, "POST", prefix + "/stop", {"voice_id": started["voice_id"]}, AUTH)
            assert code == 200 and stopped == {"stopped": True}
        assert len(set(identities)) == 2 and helper.call_count == resolver.resolve.call_count == 2
        assert sources == ["keychain", "keychain"]
        private_data_absent(tmp_path, server.manager.voice.status())
