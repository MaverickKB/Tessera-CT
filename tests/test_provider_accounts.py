"""Named accounts are shared provider/credential pools, not backend secrets."""

import pytest

from fake_backend import fake_http_server
from tessera.backend import Backend, BackendError
from tessera.config import load_config


def test_named_account_reaches_existing_backend_constructor(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERA_EXAMPLE_API_KEY", "example-secret")
    with fake_http_server([(200, {"status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Ready."}]}
    ], "usage": {"input_tokens": 4, "output_tokens": 2}})]) as (url, received):
        (tmp_path / "config.toml").write_text(f'''
[credentials.work]
env = "TESSERA_EXAMPLE_API_KEY"
[accounts.shared]
provider_kind = "openai_responses"
credential_ref = "work"
limits = {{ max_concurrent = 2 }}
budget = {{ tokens = 1000 }}
[backends.main]
base_url = "{url}"
model = "example-main-model"
account = "shared"
allow_api_key = true
[backends.small]
base_url = "{url}"
model = "example-small-model"
account = "shared"
allow_api_key = true
[roles]
main = "main"
fast = "small"
''')
        config = load_config(tmp_path)
        settings = config.backend_for()
        backend = Backend(settings.name, settings.base_url, settings.model, settings.api_key_env, settings.extra)
        small = config.backend_for("fast")
        assert settings.provider_kind == "openai_responses" and settings.credential_ref == "work"
        assert config.accounts[small.account].name == "shared"
        assert backend.account == "shared"
        assert "max_concurrent" not in backend.account_settings["limits"]
        assert backend.account_settings["budget"] == {"tokens": 1000}
        assert any("accounts.shared.limits.max_concurrent" in note for note in config.migration_notes)
        other = Backend(small.name, small.base_url, small.model, small.api_key_env, small.extra)
        assert other.account == backend.account
        assert backend.chat([{"role": "user", "content": "Hello"}])["content"] == "Ready."
    assert received[0]["path"] == "/v1/responses"
    assert received[0]["authorization"] == "Bearer example-secret"
    assert received[0]["body"]["input"] == [{"role": "user", "content": "Hello"}]
    assert "account" not in received[0]["body"] and "credential_ref" not in received[0]["body"]


@pytest.mark.parametrize("text", [
    'accounts = []',
    '[accounts.work]\nprovider_kind="unknown"',
    '[accounts.work]\ncredential_ref="missing"',
    '[accounts.work]\nbudget={tokens=-1}',
    '[credentials.work]\napi_key="never-inline-secrets"',
    '[backends.work]\nbase_url="https://models.example.com/v1"\nmodel="example"\naccount="missing"',
])
def test_invalid_account_or_inline_credential_fails_before_request(tmp_path, text):
    (tmp_path / "config.toml").write_text(text)
    with pytest.raises(ValueError) as caught:
        load_config(tmp_path)
    assert "never-inline-secrets" not in str(caught.value)


def test_explicit_missing_credential_prevents_http_request(monkeypatch):
    monkeypatch.delenv("ABSENT_EXAMPLE_KEY", raising=False)
    with fake_http_server([]) as (url, received):
        backend = Backend("missing", url, "example-model", "ABSENT_EXAMPLE_KEY")
        with pytest.raises(BackendError, match="Credential unavailable"):
            backend.chat([])
    assert received == []


def test_empty_config_and_injected_fake_backend_do_not_resolve_secrets(tmp_path):
    from fake_backend import FakeBackend
    from tessera.loop import Session
    config = load_config(tmp_path)
    assert config.accounts == {} and config.credentials == {}
    with Session(config, backend=FakeBackend(["Offline."])) as session:
        assert session.run_turn("Hello") == "Offline."
