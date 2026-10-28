"""MCP over a child process's stdio (newline-delimited JSON-RPC), standard library only.

The command, arguments, working directory and extra environment come from config.
One long-lived child per client; it is restarted on the next call if it exits.
"""

import json
import os
import queue
import signal
import subprocess
import threading
import time

from .mcp_client import McpClient, McpError


class StdioMcpClient:
    def __init__(self, command, env=None, cwd=None, timeout=15, protocol_version="2025-03-26",
                 max_response_bytes=8 * 1024 * 1024):
        if not isinstance(command, list) or not command or any(not isinstance(p, str) or not p for p in command):
            raise ValueError("MCP stdio command must be a nonempty argv list")
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("MCP timeout and response limit must be positive")
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd or None
        self.timeout = timeout
        self.protocol_version = protocol_version
        self.max_response_bytes = max_response_bytes
        self._process = None
        self._lines = None
        self._next_id = 0
        self._initialized = False
        self._lock = threading.RLock()

    # Reuse the pagination and error policy of the HTTP client.
    list_tools = McpClient.list_tools
    call_tool = McpClient.call_tool
    _result = staticmethod(McpClient._result)

    def call_tool_cancellable(self, name, arguments, registry):
        while not self._lock.acquire(timeout=0.05):
            registry.check_cancelled()

        def cancel():
            process = self._process
            if process is not None:
                from .procutil import kill_group
                try:
                    kill_group(process.pid)
                except ProcessLookupError:
                    pass
            if self._lines is not None:
                self._lines.put(None)

        try:
            with registry.cancellation(cancel):
                return self.call_tool(name, arguments)
        finally:
            self._lock.release()

    def _start(self):
        environment = dict(os.environ)
        environment.update(self.env)
        try:
            from .procutil import popen
            self._process = popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, cwd=self.cwd, env=environment)
        except OSError as error:
            raise McpError(f"MCP stdio start failed ({type(error).__name__})") from None
        self._lines = queue.Queue()
        process, lines, limit = self._process, self._lines, self.max_response_bytes

        def pump():
            try:
                while True:
                    line = process.stdout.readline(limit + 1)
                    if not line:
                        break
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        self._initialized = False

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            self._initialized = False
            if process and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def initialize(self):
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            if self._initialized:
                return
            result = self._send("initialize", {"protocolVersion": self.protocol_version, "capabilities": {},
                                               "clientInfo": {"name": "tessera", "version": "0.1"}})
            if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
                self.close()
                raise McpError("MCP initialize returned no protocol version")
            self.protocol_version = result["protocolVersion"]
            self._send("notifications/initialized", {}, notification=True)
            self._initialized = True

    def request(self, method, params=None):
        with self._lock:
            self.initialize()
            return self._send(method, params or {})

    def _send(self, method, params, notification=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        request_id = None
        if not notification:
            self._next_id += 1
            request_id = payload["id"] = self._next_id
        try:
            self._process.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            self._process.stdin.flush()
        except (OSError, ValueError):
            self.close()
            raise McpError("MCP stdio write failed") from None
        if notification:
            return None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._lines.get(timeout=max(remaining, 0.001)) if remaining > 0 else None
            except queue.Empty:
                line = None
            if line is None:
                self.close()
                raise McpError("MCP stdio response deadline exceeded or server exited")
            if len(line) > self.max_response_bytes:
                self.close()
                raise McpError("MCP response exceeds size limit")
            try:
                envelope = json.loads(line)
            except ValueError:
                continue  # Non-protocol output on stdout is ignored.
            if isinstance(envelope, dict) and "method" in envelope and "id" not in envelope:
                continue  # Server notification.
            if isinstance(envelope, dict) and envelope.get("id") == request_id:
                return self._result(envelope, request_id)
