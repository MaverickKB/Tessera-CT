"""Local HTTP fixtures for integration tests. No external endpoints are used."""

import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeMcpServer:
    def __init__(self, mode="json", port=0, dialect="id", storage=None, tool_results=None, contract="adapter-v1"):
        self.mode = mode
        self.requests = []
        self.writes = {}
        self.available = True
        self.lose_next_ack = False
        self.dialect = dialect
        self.contract = contract
        self.storage = storage if storage is not None else {"layers": []}
        self.native_write_calls = 0
        self.tool_results = dict(tool_results or {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                owner.requests.append((request, dict(self.headers)))
                if not owner.available:
                    self.respond(503, b"offline")
                    return
                method = request["method"]
                if method == "notifications/initialized":
                    self.respond(202, b"")
                    return
                if owner.mode == "slow_sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for _ in range(100):
                        try:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        time.sleep(.01)
                    return
                result = {}
                if method == "initialize":
                    result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "fake", "version": "1"}}
                elif method == "tools/list":
                    if owner.contract == "adapter-v1":
                        reference = Path(__file__).resolve().parents[1] / "docs" / "reference" / "aethermind_server_tools.json"
                        selected = {"write_layer", "read_layers", "prepare_inheritance", "view", "zoom", "status", "capabilities"}
                        advertised = []
                        for tool in json.loads(reference.read_text()):
                            if tool["name"] in selected:
                                advertised.append(tool)
                        if owner.dialect == "id":
                            if request.get("params", {}).get("cursor"):
                                result = {"tools": advertised[1:]}
                            else:
                                result = {"tools": advertised[:1], "nextCursor": "second"}
                        else:
                            result = {"tools": advertised}
                    elif request.get("params", {}).get("cursor"):
                        result = {"tools": [{"name": "tree_view"}, {"name": "prepare_inheritance"},
                                            {"name": "tree_zoom"}, {"name": "read_layers"},
                                            {"name": "list_projects"}]}
                    else:
                        result = {"tools": [{"name": "write_layer"}], "nextCursor": "second"}
                elif method == "tools/call":
                    params = request["params"]
                    name = params["name"]
                    args = params["arguments"]
                    value = {"tool": name, "args": args}
                    if name == "write_layer":
                        if owner.contract == "adapter-v1":
                            owner.native_write_calls += 1
                            layer = dict(args)
                            layer["id"] = str(len(owner.storage["layers"]) + 1)
                            owner.storage["layers"].append(layer)
                            value = {"success": True, "layer_id": layer["id"]}
                        else:
                            owner.writes.setdefault(args["request_id"], args)
                            value = {"ok": True, "request_id": args["request_id"]}
                        if owner.lose_next_ack:
                            owner.lose_next_ack = False
                            self.respond(503, b"ack lost after commit")
                            return
                    if name == "prepare_inheritance":
                        value = {"inheritance": "project history", "layers": owner.storage["layers"]} if owner.contract == "adapter-v1" else {"inheritance": "project history"}
                    if name in ("tree_view", "view"):
                        value = {"view": "personal history"}
                    if name == "capabilities":
                        value = {"success": True, "runtime": {"runtime_id": "aethermind-python",
                                 "runtime_version": "0.3.0", "contract_version": "adapter-v1",
                                 "layer_types": ["discovery", "decision", "correction", "friction"]}}
                    if name == "read_layers" and owner.contract == "adapter-v1":
                        layers = []
                        for layer in owner.storage["layers"]:
                            if layer["project_root"] == args["project_root"] and layer["ctx"].startswith(args.get("ctx_prefix", "")):
                                layers.append(layer)
                        if args.get("last_n"):
                            layers = layers[-args["last_n"]:]
                        value = {"success": True, "layers": layers, "count": len(layers)}
                    if name == "status":
                        value = {"success": True, "layer_count": len(owner.storage["layers"])}
                    if name in owner.tool_results:
                        value = owner.tool_results[name]
                    result = {"content": [{"type": "text", "text": json.dumps(value)}]}
                    if owner.dialect == "path":
                        result["structuredContent"] = {"result": json.dumps(value)}
                    if name == "fail":
                        result = {"isError": True, "content": [{"type": "text", "text": "failed"}]}
                    if name == "structured":
                        result["structuredContent"] = {"answer": 42}
                response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
                if method == "rpc_error":
                    response.pop("result")
                    response["error"] = {"code": -32601, "message": "unknown method"}
                if owner.mode.startswith("sse"):
                    # Comments and notifications can precede the matching response.
                    data = ': keepalive\r\ndata: {"jsonrpc":"2.0","method":"notice"}\r\n\r\n'
                    if owner.mode == "sse_multiline":
                        encoded = json.dumps(response, indent=2)
                        data += "event: message\r\n"
                        for line in encoded.splitlines():
                            data += "data: " + line + "\r\n"
                        data += "\r\n"
                    else:
                        data += "event: message\r\ndata: " + json.dumps(response) + "\r\n\r\n"
                    self.respond(200, data.encode(), "text/event-stream")
                else:
                    self.respond(200, json.dumps(response).encode(), "application/json")

            def respond(self, status, body, content_type="text/plain"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Mcp-Session-Id", "fake-session")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
