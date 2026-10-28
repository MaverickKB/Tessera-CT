"""A spawned NDJSON JSON-RPC peer, with all terminal interaction on the caller."""

import json
import queue
import subprocess
import threading
import time


class RpcError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class RpcClient:
    def __init__(self, command, *, timeout=30, max_frame_bytes=8 * 1024 * 1024,
                 shutdown_timeout=5, notify=None, approve=None):
        self.timeout = timeout
        self.max_frame_bytes = max_frame_bytes
        self.shutdown_timeout = shutdown_timeout
        self.notify = notify or (lambda method, params: None)
        self.approve = approve or (lambda method, params: None)
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.incoming = queue.Queue()
        self.responses = {}
        self.pending = set()
        self.failure = None
        self.next_id = 0
        self.closed = False
        self.reader = threading.Thread(target=self._read, name='tessera-rpc-reader', daemon=True)
        self.diagnostics = threading.Thread(target=self._drain_errors, name='tessera-rpc-stderr', daemon=True)
        self.reader.start()
        self.diagnostics.start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(self.max_frame_bytes + 1)
                if not line:
                    break
                if len(line) > self.max_frame_bytes or not line.endswith(b'\n'):
                    raise ValueError('Invalid engine frame')
                message = json.loads(line, parse_constant=self._invalid_constant)
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('Invalid JSON-RPC message')
                self.incoming.put(message)
        except (ValueError, OSError, RecursionError):
            self.incoming.put(RpcError('The engine sent an invalid or oversized JSON-RPC frame.'))
        finally:
            self.incoming.put(RpcError('The stdio engine closed its output pipe.'))

    @staticmethod
    def _invalid_constant(_):
        raise ValueError('Nonfinite JSON value')

    def _drain_errors(self):
        # Diagnostics may contain private configuration; never mix them into chat.
        try:
            while self.process.stderr.read(4096):
                pass
        except OSError:
            pass

    def send(self, message):
        if self.closed:
            raise RpcError('The stdio engine is closed.')
        if self.failure:
            raise self.failure
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if len(raw) > self.max_frame_bytes:
            raise RpcError('The request exceeds the configured JSON-RPC frame limit.')
        try:
            remaining = memoryview(raw)
            while remaining:
                written = self.process.stdin.write(remaining)
                if not written:
                    raise BrokenPipeError()
                remaining = remaining[written:]
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RpcError('The stdio engine stopped accepting requests.') from None

    def notification(self, method, params=None):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params or {}})

    def begin(self, method, params=None):
        self.next_id += 1
        request_id = self.next_id
        self.pending.add(request_id)
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}})
        return request_id

    def pump(self, timeout=None):
        try:
            message = self.incoming.get(timeout=timeout)
        except queue.Empty:
            raise RpcError('Timed out waiting for the stdio engine.') from None
        if isinstance(message, Exception):
            self.failure = message
            raise message
        if 'method' in message:
            method = message['method']
            params = message.get('params', {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise RpcError('The engine sent invalid method parameters.')
            if 'id' in message:
                result = self.approve(method, params)
                if result is None:
                    self.send({'jsonrpc': '2.0', 'id': message['id'],
                               'error': {'code': -32601, 'message': 'Client method not supported'}})
                else:
                    self.send({'jsonrpc': '2.0', 'id': message['id'], 'result': result})
            else:
                self.notify(method, params)
        elif 'id' in message:
            request_id = message['id']
            if type(request_id) is not int or request_id not in self.pending or request_id in self.responses:
                raise RpcError('The engine replied with an unknown request ID.')
            self.responses[request_id] = message
        else:
            raise RpcError('The engine sent an unrecognized JSON-RPC message.')

    def wait(self, request_id, timeout=None):
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + limit if limit > 0 else None
        while request_id not in self.responses:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise RpcError('Timed out waiting for the stdio engine.')
            self.pump(remaining)
        response = self.responses.pop(request_id)
        self.pending.remove(request_id)
        if 'error' in response:
            error = response['error']
            if not isinstance(error, dict):
                raise RpcError('The engine returned an invalid error.')
            message = error.get('message')
            if not isinstance(message, str):
                message = 'The engine could not complete this request.'
            raise RpcError(message, error.get('code'))
        if 'result' not in response:
            raise RpcError('The engine omitted its JSON-RPC result.')
        return response['result']

    def call(self, method, params=None, timeout=None):
        return self.wait(self.begin(method, params), timeout)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=self.shutdown_timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=self.shutdown_timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=self.shutdown_timeout)
        self.diagnostics.join(timeout=self.shutdown_timeout)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
