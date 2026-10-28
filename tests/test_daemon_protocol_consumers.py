"""Independent framing and approval consumers for the documented pipe protocol."""

import importlib
import importlib.util
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def protocol():
    assert importlib.util.find_spec("tessera.rpc") is not None, "Required daemon framing module is missing"
    return importlib.import_module("tessera.rpc")


@pytest.mark.parametrize("frame,code", [
    (b"not-json\n", -32700),
    (b"[]\n", -32600),
    (b'{"jsonrpc":"1.0","id":1}\n', -32600),
    (b'{"jsonrpc":"2.0","id":true}\n', -32600),
    (b'{"jsonrpc":"2.0","id":null}\n', -32600),
    (b'{"jsonrpc":"2.0","id":[]}\n', -32600),
    (b'{"jsonrpc":"2.0","id":1,"method":"status"}', -32700),
])
def test_protocol_rejects_malformed_frames_and_ids_without_echoing_input(protocol, frame, code):
    settings = SimpleNamespace(max_frame_bytes=4096, max_pending_requests=2)
    daemon = SimpleNamespace(config=SimpleNamespace(daemon=settings),
                             initialize=Mock(side_effect=AssertionError("Invalid frames cannot initialize")),
                             dispatch=Mock(side_effect=AssertionError("Invalid frames cannot execute")), close=Mock())
    output = io.BytesIO()
    protocol.serve(daemon, source=io.BytesIO(frame), output=output)
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == code
    assert "not-json" not in response["error"]["message"]
    daemon.initialize.assert_not_called()
    daemon.dispatch.assert_not_called()
    daemon.close.assert_called_once_with()


@pytest.mark.parametrize("decision,allowed", [
    ({"decision": "accept"}, True),
    ({"decision": "decline"}, False),
    ({"decision": True}, False),
    ({"decision": "yes"}, False),
    ({"decision": "accept", "remember": True}, False),
    ({}, False),
    (None, False),
])
def test_only_the_exact_single_tool_acceptance_grants_approval(protocol, decision, allowed):
    written = threading.Event()

    class Output(io.BytesIO):
        def flush(self):
            written.set()

    output = Output()
    settings = SimpleNamespace(max_frame_bytes=4096, approval_timeout=10)
    peer = protocol.Peer(output, settings)
    results = []
    parameters = {"threadId": "thread-placeholder", "turnId": "turn-placeholder",
                  "itemId": "item-placeholder", "name": "write_file", "arguments": {"path": "fixture.txt"}}
    worker = threading.Thread(target=lambda: results.append(peer.approve("item/tool/requestApproval", parameters)), daemon=True)
    worker.start()
    try:
        assert written.wait(5), "Approval must be emitted before waiting for its decision"
        request = json.loads(output.getvalue())
        assert request["method"] == "item/tool/requestApproval"
        assert request["params"] == parameters
        peer.response({"jsonrpc": "2.0", "id": request["id"], "result": decision})
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert results == [allowed]
        assert peer.pending == {}
    finally:
        peer.close()
        worker.join(timeout=5)


def test_attachment_disconnect_denies_an_unresolved_approval(protocol):
    written = threading.Event()

    class Output(io.BytesIO):
        def flush(self):
            written.set()

    peer = protocol.Peer(Output(), SimpleNamespace(max_frame_bytes=4096, approval_timeout=10))
    results = []
    worker = threading.Thread(target=lambda: results.append(peer.approve("item/tool/requestApproval", {})), daemon=True)
    worker.start()
    try:
        assert written.wait(5)
        peer.close()
        worker.join(timeout=5)
        assert not worker.is_alive() and results == [False]
        assert peer.pending == {}
        assert peer.approve("item/tool/requestApproval", {}) is False
    finally:
        peer.close()
        worker.join(timeout=5)


def test_outbound_frame_limit_fails_before_writing_a_partial_reply(protocol):
    output = io.BytesIO()
    peer = protocol.Peer(output, SimpleNamespace(max_frame_bytes=64))
    with pytest.raises(protocol.RpcError) as failure:
        peer.send({"id": 1, "result": "fixture" * 100})
    assert failure.value.code == -32005
    assert output.getvalue() == b""
