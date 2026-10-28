"""Consumer contracts for credential priority and an inert offline harness."""

import subprocess

import pytest

from fake_backend import FakeBackend, fake_http_server
from tessera.backend import Backend
from tessera.config import load_config
from tessera.credentials import CredentialResolver
from tessera.loop import Session


@pytest.mark.parametrize("environment_value, helper_state, expected_value, expected_source", [
    (" synthetic-env-placeholder ", "available", "synthetic-env-placeholder", "env"),
    ("", "available", "synthetic-keychain-placeholder", "keychain"),
    ("   ", "available", "synthetic-keychain-placeholder", "keychain"),
    (None, "empty", "synthetic-fallback-placeholder", "env"),
    (None, "failure", "synthetic-fallback-placeholder", "env"),
    (None, "missing", "synthetic-fallback-placeholder", "env"),
    (None, "timeout", "synthetic-fallback-placeholder", "env"),
    (None, "os_error", "synthetic-fallback-placeholder", "env"),
])
def test_credential_priority_short_circuits_lower_sources(
        environment_value, helper_state, expected_value, expected_source, capsys):
    lookups = []
    processes = []
    discoveries = []

    class Environment(dict):
        def get(self, name, default=None):
            lookups.append(name)
            return super().get(name, default)

    environment = Environment(EXAMPLE_FALLBACK_KEY="synthetic-fallback-placeholder")
    if environment_value is not None:
        environment["EXAMPLE_PRIMARY_KEY"] = environment_value

    def which(command):
        discoveries.append(command)
        return None if helper_state == "missing" else command

    def run(argv, **kwargs):
        processes.append((argv, kwargs))
        if helper_state == "timeout":
            raise subprocess.TimeoutExpired(argv, .1, output="synthetic-private-placeholder")
        if helper_state == "os_error":
            raise OSError("synthetic-private-placeholder")
        code = 1 if helper_state == "failure" else 0
        output = " synthetic-keychain-placeholder \n" if helper_state == "available" else ""
        return subprocess.CompletedProcess(argv, code, output, "synthetic-private-placeholder")

    resolver = CredentialResolver({
        "primary": {"env": "EXAMPLE_PRIMARY_KEY", "keychain_service": "service-placeholder",
                    "keychain_account": "account-placeholder", "keychain_command": "helper-placeholder",
                    "keychain_timeout": .1, "config_ref": "fallback"},
        "fallback": {"env": "EXAMPLE_FALLBACK_KEY"},
    }, environ=environment, platform="darwin", which=which, run=run)
    assert lookups == discoveries == processes == []
    result = resolver.resolve("primary")
    assert result.value == expected_value and result.source == expected_source
    fallback_used = expected_value == "synthetic-fallback-placeholder"
    assert lookups == (["EXAMPLE_PRIMARY_KEY", "EXAMPLE_FALLBACK_KEY"] if fallback_used
                       else ["EXAMPLE_PRIMARY_KEY"])
    env_won = expected_value == "synthetic-env-placeholder"
    assert discoveries == ([] if env_won else ["helper-placeholder"])
    assert len(processes) == (0 if env_won or helper_state == "missing" else 1)
    if processes:
        argv, options = processes[0]
        assert argv == ["helper-placeholder", "find-generic-password", "-s", "service-placeholder",
                        "-a", "account-placeholder", "-w"]
        assert options["timeout"] == .1 and options["stdin"] == subprocess.DEVNULL
    assert expected_value not in repr(result)
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_keychain_credential_reaches_http_header_and_stays_out_of_provider_data(monkeypatch):
    secret = "synthetic-keychain-placeholder"
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, secret, "")

    sources = {"primary": {"keychain_service": "service-placeholder", "keychain_account": "account-placeholder"}}
    resolver = CredentialResolver(sources, environ={}, platform="darwin", which=lambda command: command, run=run)
    # Backend passes oauth_home to the resolver now; the stub must accept it or this
    # security assertion never runs. The test is about the secret staying out of the
    # provider payload, not about how the resolver is constructed.
    monkeypatch.setattr("tessera.backend.CredentialResolver",
                        lambda references, **options: resolver)
    response = {"choices": [{"message": {"content": "Echo " + secret}, "finish_reason": "stop"}]}
    with fake_http_server([(200, response)]) as (url, received):
        backend = Backend("backend-placeholder", url, "model-placeholder", extra={
            "credential_ref": "primary", "credential_sources": sources, "retries": 0})
        assert calls == []
        result = backend.chat([{"role": "user", "content": "Synthetic request"}])
    assert len(calls) == 1
    assert received[0]["authorization"] == "Bearer " + secret
    assert "credential_ref" not in received[0]["body"] and "credential_sources" not in received[0]["body"]
    assert secret not in str(received[0]["body"])
    assert result["content"] == "Echo [REDACTED]" and secret not in str(result)


def test_empty_provider_config_never_resolves_credentials_or_opens_outbound_http(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Empty providers must not resolve credentials or open outbound HTTP")

    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected)
    config = load_config(tmp_path)
    assert config.accounts == {} and config.credentials == {} and config.backends == {}
    with Session(config, backend=FakeBackend(["An offline answer."])) as session:
        assert session.run_turn("Synthetic request") == "An offline answer."
        assert session.status()["backends"] == []
        from local_memory import QuietMemory
        assert isinstance(session.capture.client, QuietMemory)
