"""Mixed-provider jobs resolve only the configured gateway credential fixture."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from cli_fixtures import cli_dispatch
from tessera.credentials import CredentialResolver
from tessera.jobs import SwarmJobs
from tessera.log import ChatLog
from test_swarm_job_consumers import offline_capabilities
from test_swarm_proof_consumers import mixed_providers
from test_swarm_job_consumers import worker_specs


# Keep the real resolver for the one explicitly injected, fully stubbed object.
# The inherited fixture still forbids default resolution on every other object.
RESOLVE_REFERENCE = CredentialResolver.resolve


@pytest.mark.parametrize("source", ["env", "keychain", "unavailable"])
def test_mixed_provider_job_uses_reference_gateway_and_keeps_cli_login_separate(tmp_path, mixed_providers, source, cli_dispatch):
    config, factory, observed = mixed_providers
    config.backends["alpha-backend-placeholder"].api_key_env = ""
    config.accounts["alpha-account-placeholder"].credential_ref = "gateway-reference-placeholder"
    config.credentials = {"gateway-reference-placeholder": {
        "env": "EXAMPLE_GATEWAY_REFERENCE_KEY", "keychain_service": "gateway-service-placeholder",
        "keychain_account": "gateway-account-placeholder", "keychain_command": "helper-placeholder"}}
    environment = {"EXAMPLE_GATEWAY_REFERENCE_KEY": "gateway-placeholder"} if source == "env" else {}
    helper = Mock(return_value=subprocess.CompletedProcess([], 1 if source == "unavailable" else 0,
                                                          "gateway-placeholder", "private-diagnostic-placeholder"))
    which = Mock(return_value="helper-placeholder")
    resolver = CredentialResolver(config.credentials, environ=environment, platform="darwin", which=which, run=helper)
    resolved_sources = []

    def resolve(reference, *, env):
        result = RESOLVE_REFERENCE(resolver, reference, env=env)
        resolved_sources.append(result.source)
        return result

    observed["credential"].resolve.side_effect = resolve
    jobs = SwarmJobs(config, backend_factory=factory)
    assert helper.call_count == which.call_count == 0
    with ChatLog(tmp_path / "reference-proof-log") as log:
        job = jobs.create("Join reference-authenticated fixtures", worker_specs(), session_slug="reference-proof-placeholder", log=log)
        assert helper.call_count == which.call_count == 0
        result = jobs.run(job["id"], log=log)
        assert set(observed["selected"]) == {("openai_chat", "alpha-account-placeholder"), ("cli_worker", "beta-account-placeholder")}
        observed["credential"].resolve.assert_called_once_with("gateway-reference-placeholder", env="")
        assert helper.call_count == (0 if source == "env" else 1)
        if source == "unavailable":
            assert result["status"] == "failed" and result["merged"] is None and observed["http"] == []
            assert len(result["artifacts"]) == 1 and resolved_sources == []
            assert sorted(task["status"] for task in jobs.tasks.list()) == ["failed", "succeeded"]
        else:
            assert result["status"] == "succeeded" and len(observed["http"]) == 1
            assert resolved_sources == [source]
            merged = jobs.artifact(job["id"])
            assert merged["gate"]["allowed"] is True and all(merged["checks"].values())
            assert [row["content"] for row in merged["artifacts"]] == [
                "Alpha verified fixture", "Beta fixture inspected: beta-evidence-placeholder"]
        recorded = observed["command"].with_suffix(".request.json").read_text()
        assert "Inspect beta fixture" in json.loads(recorded)["prompt"]
        assert "gateway-placeholder" not in recorded and "gateway-reference-placeholder" not in recorded
        for path in tmp_path.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert b"gateway-placeholder" not in raw and b"private-diagnostic-placeholder" not in raw
