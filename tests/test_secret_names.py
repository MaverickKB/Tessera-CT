"""Env-var fields accept names and reject secret values."""

from tessera.secret_names import flagged_env_value, is_secret_name


def test_flagged_env_value_accepts_names_and_rejects_secret_values():
    for name in ("MODEL_API_KEY", "pasted_api_key", "api_key", "XAI_API_KEY", "DEMO_XAI_KEY", "A"):
        assert flagged_env_value(name) is False
    for secret in (
        "sk-live-secret-value",
        "xai-live-secret-value",
        "AbCdEfGhIjKlMnOpQrStUv0123456789",
    ):
        assert flagged_env_value(secret) is True
    assert is_secret_name("pasted_api_key") is True
    assert is_secret_name("api_key_env") is False
