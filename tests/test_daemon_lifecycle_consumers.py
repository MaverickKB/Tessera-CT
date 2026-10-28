"""Actual pipe attachments and daemon restarts, independent of owner happy paths."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHILD = Path(__file__).with_name("daemon_consumer_child.py")


@pytest.fixture(autouse=True)
def require_daemon_contract():
    for name in ("tessera.daemon", "tessera.rpc", "tessera.app_server"):
        assert importlib.util.find_spec(name) is not None, "Required daemon module is missing: " + name


def offline_environment():
    environment = dict(os.environ)
    for name in list(environment):
        if name.startswith("TESSERA_") or name in {
            "AETHERMIND_TOKEN", "TYPESAFE_API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        }:
            environment.pop(name)
    environment["NO_COLOR"] = "1"
    return environment


class Attachment:
    def __init__(self, process):
        self.process = process
        self.incoming = queue.Queue()
        self.events = []
        self.next_id = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        try:
            while True:
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
                    raise ValueError("Unbounded or incomplete daemon output")
                self.incoming.put(json.loads(line))
        except Exception as error:
            self.incoming.put(error)
        finally:
            self.incoming.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self):
        packet = self.incoming.get(timeout=15)
        assert packet is not None, "Daemon attachment exited before its response"
        assert not isinstance(packet, Exception), "Daemon emitted an invalid frame"
        assert packet["jsonrpc"] == "2.0"
        return packet

    def exchange(self, method, params=None):
        self.next_id += 1
        number = self.next_id
        self.send({"id": number, "method": method, "params": params or {}})
        while True:
            packet = self.receive()
            if packet.get("id") == number and "method" not in packet:
                return packet
            self.events.append(packet)

    def call(self, method, params=None):
        response = self.exchange(method, params)
        assert "error" not in response, response
        return response["result"]

    def initialize(self):
        result = self.call("initialize", {"clientInfo": {"name": "consumer-placeholder"}})
        self.send({"method": "initialized"})
        assert result["capabilities"]["transport"] == "stdio"

    def event(self, method):
        for index, packet in enumerate(self.events):
            if packet.get("method") == method:
                return self.events.pop(index)
        while True:
            packet = self.receive()
            if packet.get("method") == method:
                return packet
            self.events.append(packet)

    def turn(self, thread_id, text):
        turn = self.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})["turn"]
        completed = self.event("turn/completed")["params"]
        assert completed["threadId"] == thread_id and completed["turn"]["id"] == turn["id"]
        assert completed["turn"]["status"] == "completed"


@contextmanager
def attachment(home):
    process = subprocess.Popen([sys.executable, "-u", str(CHILD), str(home)], cwd=ROOT,
                               env=offline_environment(), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peer = Attachment(process)
    try:
        yield peer
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        process.stdin = None
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("Daemon did not drain and exit after attachment EOF")
        peer.reader.join(timeout=5)
        error = process.stderr.read()
        process.stdout.close()
        process.stderr.close()
        assert not peer.reader.is_alive()
        assert process.returncode == 0, error.decode(errors="replace")


def test_initialize_is_required_and_unknown_methods_are_honest_errors(tmp_path):
    with attachment(tmp_path) as peer:
        assert peer.exchange("thread/start", {"title": "Rejected fixture"})["error"]["code"] == -32002
        peer.initialize()
        assert peer.call("thread/list") == {"data": []}
        assert peer.exchange("initialize")["error"]["code"] == -32600
        assert peer.exchange("consumer/missing")["error"]["code"] == -32601
        assert peer.call("thread/list") == {"data": []}


def test_history_and_optchat_sources_survive_a_real_daemon_restart(tmp_path):
    fact = "acceptance-fact:" + uuid.uuid4().hex
    with attachment(tmp_path) as first:
        first.initialize()
        slug = first.call("thread/start", {"title": "Durable consumer fixture"})["thread"]["id"]
        first.turn(slug, "Remember " + fact)
        history = first.call("thread/read", {"threadId": slug})["messages"]
        assert history[-1]["text"] == "Recorded Remember " + fact
        assert first.call("view/read", {"threadId": slug})["lines"]
        assert first.call("view/zoom", {"threadId": slug, "start": 0, "count": 1})["text"] == "Remember " + fact
        first_pid = first.process.pid
    logs = {}
    for path in (tmp_path / "sessions" / slug / "log").glob("*.jsonl"):
        logs[path] = path.read_bytes()
    assert logs
    with attachment(tmp_path) as second:
        assert second.process.pid != first_pid and first.process.returncode == 0
        second.initialize()
        resumed = second.call("thread/resume", {"threadId": slug})
        assert resumed["messages"] == history
        for path, original in logs.items():
            assert path.read_bytes() == original, "Reattach cannot rewrite the saved source log"
        second.turn(slug, "Recover the earlier fixture fact from saved context")
        restored = second.call("thread/read", {"threadId": slug})["messages"]
        assert restored[:len(history)] == history
        assert restored[-1]["text"] == "Recovered " + fact
        assert second.call("view/zoom", {"threadId": slug, "start": 0, "count": 1})["text"] == "Remember " + fact


def test_default_cli_attaches_then_resume_reattaches_to_saved_daemon_state(tmp_path):
    assert importlib.util.find_spec("tessera.terminal") is not None, "Required terminal client is missing"
    environment = offline_environment()
    environment["TESSERA_ENGINE_COMMAND"] = json.dumps([sys.executable, "-u", str(CHILD), str(tmp_path)])
    environment["TESSERA_HOME"] = str(tmp_path)
    fact = "acceptance-fact:" + uuid.uuid4().hex
    first = subprocess.run([sys.executable, "-m", "tessera"], cwd=ROOT, env=environment,
                           input="Remember " + fact + "\n/quit\n", text=True, capture_output=True, timeout=30)
    assert first.returncode == 0, first.stderr
    assert "Recorded Remember " + fact in first.stdout
    second = subprocess.run([sys.executable, "-m", "tessera", "resume", "--last"], cwd=ROOT, env=environment,
                            input="Recover the earlier fixture fact from saved context\n/view\n/zoom 0+1\n/quit\n",
                            text=True, capture_output=True, timeout=30)
    assert second.returncode == 0, second.stderr
    assert "Resumed:" in second.stdout and "user: Remember " + fact in second.stdout
    assert "Recovered " + fact in second.stdout
    assert "[0+1]" in second.stdout and "Remember " + fact in second.stdout
    pids = (tmp_path / "daemon-pids-fixture.txt").read_text().splitlines()
    assert len(pids) == 2 and len(set(pids)) == 2


def test_noninteractive_cli_denies_the_real_daemon_tool_and_shows_its_result(tmp_path):
    assert importlib.util.find_spec("tessera.terminal") is not None, "Required terminal client is missing"
    environment = offline_environment()
    environment["TESSERA_ENGINE_COMMAND"] = json.dumps([sys.executable, "-u", str(CHILD), str(tmp_path)])
    environment["TESSERA_HOME"] = str(tmp_path)
    result = subprocess.run([sys.executable, "-m", "tessera"], cwd=ROOT, env=environment,
                            input="Use write_file for the fixture\n/quit\n", text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "write_file" in result.stdout
    assert "Declined: interactive approval" in result.stdout
    assert "Tool not run: approval denied" in result.stdout
    assert not (tmp_path / "approved-fixture.txt").exists()


def test_zoom_pages_round_trip_utf8_with_honest_continuation_metadata(tmp_path):
    source = "Fixture café snowman ☃ and trailing text"
    with attachment(tmp_path) as peer:
        peer.initialize()
        slug = peer.call("thread/start", {"title": "Byte paging fixture"})["thread"]["id"]
        peer.turn(slug, source)
        offset = 0
        pages = []
        while True:
            page = peer.call("view/zoom", {"threadId": slug, "start": 0, "count": 1, "offset": offset, "limit": 7})
            size = len(page["text"].encode("utf-8"))
            assert page["offset"] == offset and 0 < size <= 7
            assert page["totalBytes"] == len(source.encode("utf-8"))
            pages.append(page["text"])
            if page["nextOffset"] is None:
                assert offset + size == page["totalBytes"]
                break
            assert page["nextOffset"] == offset + size
            offset = page["nextOffset"]
        assert "".join(pages) == source
        split_character = source.encode("utf-8").index("é".encode("utf-8")) + 1
        invalid = peer.exchange("view/zoom", {"threadId": slug, "start": 0, "count": 1, "offset": split_character})
        assert invalid["error"]["code"] == -32602
        assert peer.call("view/zoom", {"threadId": slug, "start": 0, "count": 1})["text"] == source


@pytest.mark.parametrize("decision", [{"decision": "decline"}, {"decision": "accept", "remember": True}])
def test_declined_or_malformed_approval_never_executes_the_actual_tool(tmp_path, decision):
    with attachment(tmp_path) as peer:
        peer.initialize()
        slug = peer.call("thread/start", {"title": "Denied tool fixture"})["thread"]["id"]
        turn = peer.call("turn/start", {"threadId": slug, "input": [{"type": "text", "text": "Use write_file for the fixture"}]})["turn"]
        approval = peer.event("item/tool/requestApproval")
        assert approval["params"]["threadId"] == slug
        assert approval["params"]["turnId"] == turn["id"]
        assert approval["params"]["name"] == "write_file"
        assert not (tmp_path / "approved-fixture.txt").exists()
        peer.send({"id": approval["id"], "result": decision})
        completed = peer.event("turn/completed")["params"]
        assert completed["threadId"] == slug and completed["turn"]["id"] == turn["id"]
        assert not (tmp_path / "approved-fixture.txt").exists()
        history = peer.call("thread/read", {"threadId": slug})["messages"]
        results = [row for row in history if row["kind"] == "echo"]
        assert results and results[-1]["meta"]["success"] is False
        assert history[-1]["text"] == "Tool not run: approval denied"


def test_disconnect_with_pending_approval_drains_denied_result_durably(tmp_path):
    with attachment(tmp_path) as peer:
        peer.initialize()
        slug = peer.call("thread/start", {"title": "EOF fixture"})["thread"]["id"]
        peer.call("turn/start", {"threadId": slug, "input": [{"type": "text", "text": "Use write_file for the fixture"}]})
        peer.event("item/tool/requestApproval")
        peer.process.stdin.close()
        peer.process.stdin = None
    assert not (tmp_path / "approved-fixture.txt").exists()
    with attachment(tmp_path) as resumed:
        resumed.initialize()
        rows = resumed.call("thread/read", {"threadId": slug})["messages"]
        results = [row for row in rows if row["kind"] == "echo"]
        assert results and results[-1]["meta"]["success"] is False
        assert resumed.call("status")["session_count"] == 1
        recalled = resumed.call("memory/recall", {"threadId": slug, "query": "fixture"})
        # The daemon process attaches the test-local engine. No project is
        # configured, so recall is an empty result rather than a missing client.
        assert recalled["success"] is True and recalled["layers"] == [] and recalled["query"] == "fixture"
