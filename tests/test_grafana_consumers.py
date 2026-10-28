"""Optional Grafana consumer contracts use injected credentials and HTTP fakes."""

import io
import json
from unittest.mock import Mock
import urllib.error

import pytest

from tessera.config import GrafanaConfig
from tessera.credentials import ResolvedCredential
from test_infrastructure_consumers import forbid_external_capabilities


@pytest.mark.parametrize("options,configured", [
    ({}, False),
    ({"enabled": True}, False),
    ({"enabled": True, "base_url": "https://metrics.example.com/private-path-placeholder"}, False),
    ({"enabled": True, "credential_ref": "private-reference-placeholder"}, False),
    ({"enabled": False, "base_url": "https://metrics.example.com", "token_env": "EXAMPLE_GRAFANA_KEY"}, True),
])
def test_incomplete_or_disabled_clients_reject_operations_before_secret_or_transport(options, configured):
    from tessera.grafana import GrafanaClient, GrafanaError

    settings = GrafanaConfig(**options, allowed_proxy_paths=["read-placeholder"])
    resolver = Mock()
    resolver.resolve.side_effect = AssertionError("An inert client cannot resolve credentials")
    opener = Mock(side_effect=AssertionError("An inert client cannot open HTTP"))
    client = GrafanaClient(settings, credentials=resolver, opener=opener)
    before = client.status()
    assert before == {"enabled": settings.enabled, "configured": configured, "health": "unknown"}
    for operation in (client.list_datasources, client.list_dashboards,
                      lambda: client.query_proxy("source-placeholder", "read-placeholder", {})):
        with pytest.raises(GrafanaError):
            operation()
    resolver.resolve.assert_not_called()
    opener.assert_not_called()
    assert client.status() == before


def test_fully_configured_status_and_embed_builder_do_not_resolve_or_probe():
    from tessera.grafana import GrafanaClient

    settings = GrafanaConfig(enabled=True, base_url="https://metrics.example.com/private-path-placeholder",
                             credential_ref="private-reference-placeholder")
    resolver = Mock()
    resolver.resolve.side_effect = AssertionError("Status and URL construction are passive")
    opener = Mock(side_effect=AssertionError("Status and URL construction are passive"))
    client = GrafanaClient(settings, credentials=resolver, opener=opener)
    first = client.status()
    assert first == {"enabled": True, "configured": True, "health": "unknown"}
    first["health"] = "private-diagnostic-placeholder"
    assert client.status() == {"enabled": True, "configured": True, "health": "unknown"}
    url = client.embed_url("dashboard-placeholder", "panel-placeholder", panel_id=3)
    assert url.startswith("https://metrics.example.com/private-path-placeholder/")
    assert "private-reference-placeholder" not in url
    assert "token" not in url.lower() and "authorization" not in url.lower()
    resolver.resolve.assert_not_called()
    opener.assert_not_called()


@pytest.mark.parametrize("failure,category", [("timeout", "timeout"), ("access", "access"),
                                                ("invalid", "invalid_response")])
def test_explicit_fake_requests_leave_only_sanitized_failure_status(failure, category):
    from tessera.grafana import GrafanaClient, GrafanaError

    settings = GrafanaConfig(enabled=True, base_url="https://metrics.example.com/private-path-placeholder",
                             token_env="EXAMPLE_GRAFANA_KEY", timeout=0.25)
    resolver = Mock()
    resolver.resolve.return_value = ResolvedCredential("secret-placeholder", "env")
    calls = []

    def send(request, timeout):
        calls.append(request)
        assert timeout == 0.25
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer secret-placeholder"
        assert request.data is None
        if len(calls) == 1:
            return io.BytesIO(b'[{"uid":"private-source-placeholder","name":"private-name-placeholder","type":"metrics-placeholder"}]')
        if failure == "timeout":
            raise TimeoutError("private-error-placeholder secret-placeholder")
        if failure == "access":
            raise urllib.error.HTTPError(request.full_url, 403, "private-error-placeholder", {},
                                         io.BytesIO(b"private-body-placeholder secret-placeholder"))
        return io.BytesIO(b"private-error-placeholder secret-placeholder")

    client = GrafanaClient(settings, credentials=resolver, opener=send)
    assert len(client.list_datasources()) == 1
    assert client.status()["health"] == "healthy"
    with pytest.raises(GrafanaError) as caught:
        client.list_datasources()
    assert caught.value.category == category
    assert len(calls) == 2 and resolver.resolve.call_count == 2
    report = client.status()
    assert report["health"] == "unavailable" and report["error_category"] == category
    encoded = json.dumps(report) + str(caught.value)
    for private in ("secret-placeholder", "EXAMPLE_GRAFANA_KEY", "metrics.example.com",
                    "private-path-placeholder", "private-error-placeholder", "private-source-placeholder",
                    "private-name-placeholder", "private-body-placeholder"):
        assert private not in encoded
    assert len(calls) == 2 and resolver.resolve.call_count == 2
