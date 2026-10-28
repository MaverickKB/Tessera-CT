#!/usr/bin/env python3
"""Loopback chat-completions fixture backed by the test suite's FakeBackend.

The reply script is the same one demo/stage1_smoke.py uses. This process
never reads credentials and never dials a non-loopback address.
"""

import argparse
import importlib.util
import ipaddress
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def scripted_reply():
    path = ROOT / "demo" / "stage1_smoke.py"
    spec = importlib.util.spec_from_file_location("tessera_stage1_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the existing scripted fake reply")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scripted_reply


def fake_backend(replies):
    sys.path.insert(0, str(ROOT / "tests"))
    from fake_backend import FakeBackend

    return FakeBackend([scripted_reply()] * replies)


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(host, port, replies):
    if not loopback(host):
        raise SystemExit("The offline model binds a loopback address only.")
    backend = fake_backend(replies)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            return

        def do_GET(self):
            if self.path.split("?", 1)[0] != "/health":
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, b"tessera-offline-model", "text/plain")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                messages = body.get("messages") or [{"role": "user", "content": ""}]
                reply = backend.chat(messages, tools=body.get("tools"))
                message = {"role": "assistant", "content": reply.get("content") or ""}
                calls = []
                for call in reply.get("tool_calls") or []:
                    calls.append({
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    })
                if calls:
                    message["tool_calls"] = calls
                payload = {"choices": [{"message": message, "finish_reason": "stop"}], "usage": {}}
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
            except Exception as error:
                payload = {"error": type(error).__name__}
                self._send(500, json.dumps(payload).encode("utf-8"), "application/json")

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        server = Server((host, port), Handler)
    except OSError as error:
        raise SystemExit(f"Offline model could not bind: {error}") from None
    print(f"http://{server.server_address[0]}:{server.server_address[1]}/v1", flush=True)
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--replies", type=int, default=100000)
    args = parser.parse_args(argv)
    if args.replies < 1 or not 0 <= args.port <= 65535:
        raise SystemExit("Invalid offline model port or reply budget.")
    serve(args.host, args.port, args.replies)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
