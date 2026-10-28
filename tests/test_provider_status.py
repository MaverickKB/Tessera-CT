"""Status only projects config and past observations, never performs discovery."""

import json
import threading

import pytest

from fake_backend import fake_http_server
from tessera.backend import Backend, BackendError
from tessera.config import load_config
from tessera.credentials import CredentialResolver
from tessera.server import create_server
from tessera.sessions import SessionManager
from test_server import call


def provider_config(tmp_path, url="https://models.example.com/v1"):
    (tmp_path / "config.toml").write_text(f'''
[credentials.private_reference]
env = "EXAMPLE_STATUS_KEY"
[accounts.private_account]
provider_kind = "openai_chat"
credential_ref = "private_reference"
[backends.primary]
base_url = "{url}"
model = "private-model-placeholder"
account = "private_account"
allow_api_key = true
''')
    return load_config(tmp_path)


def forbid_io(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Status must not resolve credentials, construct adapters, spawn or probe")
    monkeypatch.setattr(CredentialResolver, "resolve", unexpected)
    monkeypatch.setattr("tessera.backend.create_adapter", unexpected)
    monkeypatch.setattr("subprocess.Popen", unexpected)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected)


def test_status_is_passive_and_omits_private_configuration(tmp_path, monkeypatch):
    config = provider_config(tmp_path)
    with SessionManager(config) as manager:
        forbid_io(monkeypatch)
        status = manager.status()["provider_status"]
    assert status == {
        "accounts": [{"configured": True, "health": "not_checked", "credential": "not_reported"}],
        "backends": [{"id": "primary", "configured": True, "health": "not_checked", "credential": "not_reported"}],
    }
    serialized = json.dumps(status)
    for private in ("private_reference", "private_account", "private-model-placeholder", "models.example.com", "EXAMPLE_STATUS_KEY"):
        assert private not in serialized


def test_http_status_publishes_empty_arrays_without_io(tmp_path, monkeypatch):
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        forbid_io(monkeypatch)
        code, status = call(server, "GET", "/api/status")
        assert code == 200
        assert status["provider_status"] == {"accounts": [], "backends": []}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_last_real_request_observation_is_categorized_without_reprobe(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_STATUS_KEY", "example-secret")
    with fake_http_server([(401, {"error": "example-secret"})]) as (url, received):
        config = provider_config(tmp_path, url)
        settings = config.backend_for()
        backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        with SessionManager(config, backend_factory=lambda slug, name: backend) as manager:
            manager.create("status")
            manager.runtime("status")
            with pytest.raises(BackendError):
                backend.chat([{"role": "user", "content": "Hello"}])
            forbid_io(monkeypatch)
            status = manager.status()["provider_status"]
            for record in status["accounts"] + status["backends"]:
                assert record["health"] == "error"
                assert record["error_category"] == "authentication"
                assert record["credential"] == "available"
            assert "example-secret" not in json.dumps(status)
        assert len(received) == 1


@pytest.mark.parametrize("cwd,configured", [("", False), ("/workspace-placeholder", True)])
def test_cli_configuration_status_requires_a_working_directory_without_discovery(tmp_path, monkeypatch, cwd, configured):
    (tmp_path / "config.toml").write_text(f'''
[backends.worker]
provider_kind = "cli_worker"
[backends.worker.cli]
vendor = "codex"
command = "codex"
cwd = "{cwd}"
''')
    with SessionManager(load_config(tmp_path)) as manager:
        forbid_io(monkeypatch)
        row = manager.status()["provider_status"]["backends"][0]
    assert row["configured"] is configured
    assert row["health"] == "not_checked" and row["credential"] == "not_reported"
    if cwd:
        assert cwd not in json.dumps(row)
