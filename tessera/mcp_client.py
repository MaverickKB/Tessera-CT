"""Small synchronous streamable HTTP MCP client using only the standard library."""

import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .http_cancel import CancellableHTTP, CancelledIO


class McpError(RuntimeError):
    """A transport, protocol or remote tool failure. Credentials are never included."""


def _endpoint_for_error(url):
    """Scheme, host, port, and path. Userinfo and the query string are omitted."""
    parsed = urlsplit(url or '')
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return 'configured MCP endpoint'
    netloc = parsed.netloc.rsplit('@', 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, '', ''))


def _without_endpoint_secrets(text, url):
    """Mark an echoed userinfo and query instead of deleting a short substring.

    ``user:pass@`` becomes ``[REDACTED]@`` and ``?a=1&token=...`` becomes
    ``?[REDACTED]``. A password or query value of at least 4 characters is
    also marked wherever it appears. Shorter values are left alone so they
    cannot eat unrelated text.
    """
    if not text or not url:
        return text
    parsed = urlsplit(url)
    cleaned = text
    netloc = parsed.netloc or ''
    userinfo = netloc.split('@', 1)[0] if '@' in netloc else ''
    if userinfo:
        cleaned = cleaned.replace(userinfo + '@', '[REDACTED]@')
    if parsed.query:
        cleaned = cleaned.replace('?' + parsed.query, '?[REDACTED]')
    secrets = []
    if parsed.password and len(parsed.password) >= 4:
        secrets.append(parsed.password)
    for _name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if len(value) >= 4:
            secrets.append(value)
    marker = '[REDACTED]'
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret in marker:
            continue
        cleaned = cleaned.replace(secret, marker)
    return cleaned


class McpClient:
    def __init__(self, url, token_env="AETHERMIND_TOKEN", timeout=15,
                 protocol_version="2025-03-26", max_response_bytes=8 * 1024 * 1024):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            raise ValueError("MCP URL must be an HTTP(S) URL without credentials")
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("MCP timeout and response limit must be positive")
        self.url = url
        self.token_env = token_env
        self.timeout = timeout
        self.protocol_version = protocol_version
        self.max_response_bytes = max_response_bytes
        self.session_id = None
        self._initialized = False
        self._next_id = 0
        self._lock = threading.RLock()
        self._call_context = threading.local()
        # Credential-bearing calls must not follow redirects to another origin.
        self._opener = urllib.request.build_opener(_NoRedirect())

    def initialize(self):
        with self._lock:
            if self._initialized:
                return
            result = self._post("initialize", {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "tessera", "version": "0.1"},
            })
            if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
                raise McpError("MCP initialize returned no protocol version")
            self.protocol_version = result["protocolVersion"]
            self._post("notifications/initialized", {}, notification=True)
            self._initialized = True

    def request(self, method, params=None):
        with self._lock:
            self.initialize()
            return self._post(method, params or {})

    def list_tools(self):
        tools = []
        params = {}
        seen_cursors = set()
        while True:
            result = self.request("tools/list", params)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise McpError("MCP tools/list returned an invalid tool list")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise McpError("MCP tools/list repeated or invalid cursor")
            seen_cursors.add(cursor)
            params = {"cursor": cursor}

    def call_tool_cancellable(self, name, arguments, registry):
        # Catalog clients are shared. Cancel only this call, never another
        # session's request or the whole HTTP client.
        while not self._lock.acquire(timeout=0.05):
            registry.check_cancelled()
        transport = CancellableHTTP(_NoRedirect())
        transport.cancel_event = registry.cancelled
        context = {'transport': transport, 'request_id': None}

        def cancel():
            transport.cancel()
            request_id = context['request_id']
            if request_id is not None:
                threading.Thread(target=self._notify_cancelled, args=(request_id,), daemon=True).start()

        try:
            with registry.cancellation(cancel):
                self._call_context.current = context
                return self.call_tool(name, arguments)
        except CancelledIO:
            raise McpError('MCP request cancelled by operator') from None
        finally:
            self._call_context.current = None
            transport.finish()
            self._lock.release()

    def _notify_cancelled(self, request_id):
        payload = {'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                   'params': {'requestId': request_id, 'reason': 'Operator pressed Stop'}}
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Mcp-Protocol-Version': self.protocol_version}
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        token = os.environ.get(self.token_env, '').strip() if self.token_env else ''
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method='POST')
        try:
            with self._opener.open(request, timeout=2):
                pass
        except (OSError, ValueError):
            pass

    def call_tool(self, name, arguments=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise McpError("MCP tool returned an invalid result")
        if result.get("isError"):
            # Do not include an untrusted response body which could echo credentials.
            raise McpError("MCP tool reported an error")
        return result

    def _post(self, method, params, notification=False):
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        request_id = None
        if not notification:
            self._next_id += 1
            request_id = self._next_id
            payload["id"] = request_id
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        token = os.environ.get(self.token_env, "").strip() if self.token_env else ""
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if method != "initialize":
            headers["Mcp-Protocol-Version"] = self.protocol_version
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        deadline = time.monotonic() + self.timeout
        context = getattr(self._call_context, 'current', None)
        transport = context['transport'] if context is not None else None
        opener = transport.opener if transport is not None else self._opener
        if context is not None and method == 'tools/call':
            context['request_id'] = request_id
        try:
            with opener.open(request, timeout=self.timeout) as response:
                if transport is not None:
                    transport.response = response
                    transport.check_cancelled()
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                if notification and response.status in (200, 202, 204):
                    return None
                content_type = response.headers.get_content_type()
                if content_type == "text/event-stream":
                    envelope = self._read_sse(response, request_id, deadline)
                elif content_type == "application/json":
                    chunks = []
                    for chunk in self._chunks(response, deadline):
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    envelope = json.loads(raw)
                else:
                    raise McpError("MCP response has unsupported content type")
                return self._result(envelope, request_id)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            if status == 404:
                self._initialized = False
                self.session_id = None
            raise McpError(f"MCP HTTP {status}") from None
        except (OSError, ValueError) as error:
            cause = error
            reason = getattr(error, "reason", None)
            if reason is not None and not isinstance(reason, str):
                cause = reason
            detail = _without_endpoint_secrets(str(cause).replace("\n", " ").strip(), self.url)
            target = _endpoint_for_error(self.url)
            if isinstance(cause, TimeoutError):
                message = f"MCP timeout after {format(self.timeout, 'g')}s talking to {target}"
                if detail and detail not in message:
                    message += ": " + detail
                raise McpError(message) from error
            name = type(cause).__name__
            message = f"MCP transport or decoding failure ({name}) talking to {target}"
            if detail and detail != name:
                message += ": " + detail
            raise McpError(message) from error

    def _chunks(self, response, deadline):
        consumed = 0
        while True:
            if time.monotonic() >= deadline:
                raise McpError("MCP response deadline exceeded")
            # read1 returns available data, so periodic SSE keepalives cannot reset
            # the overall deadline. A stalled socket also has urllib's read timeout.
            chunk = response.read1(min(4096, self.max_response_bytes - consumed + 1))
            if time.monotonic() >= deadline:
                raise McpError("MCP response deadline exceeded")
            consumed += len(chunk)
            if consumed > self.max_response_bytes:
                raise McpError("MCP response exceeds size limit")
            if not chunk:
                return
            yield chunk

    def _read_sse(self, response, request_id, deadline):
        data_lines = []
        pending = b""
        for chunk in self._chunks(response, deadline):
            pending += chunk
            while b"\n" in pending:
                raw, pending = pending.split(b"\n", 1)
                line = raw.decode("utf-8").rstrip("\r")
                if not line and data_lines:
                    event = json.loads("\n".join(data_lines))
                    data_lines = []
                    if isinstance(event, dict) and event.get("id") == request_id:
                        return event
                elif line.startswith("data:"):
                    data = line[5:]
                    if data.startswith(" "):
                        data = data[1:]
                    data_lines.append(data)
        raise McpError("MCP SSE ended before the matching response")

    @staticmethod
    def _result(envelope, request_id):
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0":
            raise McpError("MCP returned an invalid JSON-RPC envelope")
        if envelope.get("id") != request_id:
            raise McpError("MCP response id does not match request")
        if "error" in envelope:
            error = envelope["error"]
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            if not isinstance(code, int):
                code = "unknown"
            raise McpError(f"MCP JSON-RPC error {code}")
        if "result" not in envelope:
            raise McpError("MCP response has no result")
        return envelope["result"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
