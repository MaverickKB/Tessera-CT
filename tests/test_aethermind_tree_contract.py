"""Offline mirror of the sanitized read-only no-tree/no-node boundary.

Structured failures are preserved without wire payloads. These synthetic
envelopes keep those failure semantics. They do not claim an exact live
payload or successful tree rendering.
"""

import pytest

from integration_helpers import FakeMcpServer
from tessera.aethermind import AetherMindClient


@pytest.mark.parametrize("project", ["/harness-placeholder", "/personal-placeholder"])
def test_reachable_path_server_preserves_unsuccessful_tree_results_without_writes(project):
    failures = {
        "view": {"success": False, "error": "No summary tree"},
        "zoom": {"success": False, "error": "No node"},
    }
    with FakeMcpServer(dialect="path", tool_results=failures) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1,
                                  read_only_projects=["/harness-placeholder", "/personal-placeholder"])
        assert client.tree_view(project) == {"success": False, "error": "No summary tree"}
        assert client.tree_zoom(project, 0, 0) == {"success": False, "error": "No node"}
        assert client.status()["endpoints"][0]["healthy"] is True
        assert server.native_write_calls == 0 and server.storage["layers"] == []
        calls = []
        for request, _ in server.requests:
            if request["method"] == "tools/call":
                calls.append(request["params"])
        assert [call["name"] for call in calls] == ["capabilities", "view", "zoom"]
        assert calls[0]["arguments"] == {}
        assert all(call["arguments"]["project_root"] == project for call in calls[1:])


@pytest.mark.parametrize("project", ["harness-id-placeholder", "personal-id-placeholder"])
def test_id_dialect_reachable_transport_preserves_no_tree_without_writes(project):
    """Synthetic ID envelopes mirror failure semantics, not a claimed live payload."""
    failures = {
        "view": {"success": False, "error": "No summary tree"},
        "zoom": {"success": False, "error": "No node"},
    }
    with FakeMcpServer(dialect="id", tool_results=failures) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1, read_only_projects=[project])
        assert client.tree_view(project, budget_chars=100) == {"success": False, "error": "No summary tree"}
        assert client.tree_zoom(project, 0, 0) == {"success": False, "error": "No node"}
        assert client.status()["endpoints"][0]["healthy"] is True
        assert server.native_write_calls == 0 and server.writes == {} and server.storage["layers"] == []
        calls = [request["params"] for request, _ in server.requests if request["method"] == "tools/call"]
        assert calls == [
            {"name": "capabilities", "arguments": {}},
            {"name": "view", "arguments": {"project_root": project, "budget_chars": 100}},
            {"name": "zoom", "arguments": {"project_root": project, "level": 0, "index": 0}},
        ]


@pytest.mark.parametrize("project,label", [("/harness-placeholder", "<HARNESS_PROJECT>"),
                                           ("/personal-placeholder", "<PERSONAL_PROJECT>")])
def test_fresh_sanitized_no_tree_recheck_keeps_error_metadata_and_never_builds(project, label, monkeypatch):
    """Mirror the unavailable contract using local fixtures."""
    from unittest.mock import Mock

    view = {"success": False,
            "error": "no summary tree at " + label + "/.aethermind/tree.aem; run build_tree first",
            "error_type": "ValueError", "error_code": "INVALID_ARGUMENT"}
    zoom = {"success": False, "error": "no node at (level=0, index=0)"}
    spawn = Mock(side_effect=AssertionError("An error instruction must not invoke a tree builder"))
    monkeypatch.setattr("subprocess.Popen", spawn)
    with FakeMcpServer(dialect="path", tool_results={"view": view, "zoom": zoom}) as server:
        client = AetherMindClient(endpoints=[server.url], timeout=1, read_only_projects=[project])
        assert client.tree_view(project) == view
        assert client.tree_zoom(project, 0, 0) == zoom
        assert client.status()["endpoints"][0]["healthy"] is True
        calls = [request["params"] for request, _ in server.requests if request["method"] == "tools/call"]
        assert [call["name"] for call in calls] == ["capabilities", "view", "zoom"]
        assert calls[0]["arguments"] == {}
        assert all(call["arguments"]["project_root"] == project for call in calls[1:])
        assert server.native_write_calls == 0 and server.storage["layers"] == []
    spawn.assert_not_called()
