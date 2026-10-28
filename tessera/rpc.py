"""Bounded bidirectional NDJSON JSON-RPC, independent of any network transport."""

import json
import math
import queue
import sys
import threading


class RpcError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def valid_id(value):
    return type(value) in (str, int)


class Peer:
    def __init__(self, output, settings):
        self.output = output
        self.settings = settings
        self.lock = threading.RLock()
        self.pending = {}
        self.next_id = 0
        self.closed = False
        self.input_closed = False

    def send(self, message):
        raw = json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False, allow_nan=False).encode() + b'\n'
        if len(raw) > self.settings.max_frame_bytes:
            raise RpcError(-32005, 'Response exceeds configured frame limit')
        with self.lock:
            if self.closed:
                return
            try:
                self.output.write(raw)
                self.output.flush()
            except (BrokenPipeError, OSError):
                self.close()

    def error(self, request_id, code, message):
        self.send({'id': request_id, 'error': {'code': code, 'message': message}})

    def notify(self, method, params):
        try:
            self.send({'method': method, 'params': params})
        except RpcError:
            self.send({'method': 'error', 'params': {'category': 'frame_limit'}})

    def approve(self, method, params):
        with self.lock:
            if self.closed or self.input_closed:
                return False
            self.next_id += 1
            request_id = 'approval-' + str(self.next_id)
            pending = {'event': threading.Event(), 'accepted': False}
            self.pending[request_id] = pending
        try:
            self.send({'id': request_id, 'method': method, 'params': params})
            resolved = pending['event'].wait(self.settings.approval_timeout)
            return resolved and pending['accepted'] is True
        except (RpcError, ValueError):
            return False
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def response(self, message):
        with self.lock:
            if self.input_closed:
                return
            pending = self.pending.get(message.get('id'))
            if pending is not None:
                pending['accepted'] = set(message) == {'jsonrpc', 'id', 'result'} and message['result'] == {'decision': 'accept'}
                pending['event'].set()

    def close(self):
        with self.lock:
            self.closed = True
            self.deny_approvals()

    def deny_approvals(self):
        with self.lock:
            self.input_closed = True
            for pending in self.pending.values():
                pending['accepted'] = False
                pending['event'].set()


def _invalid_constant(value):
    raise ValueError('Nonfinite JSON number')


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Nonfinite JSON number')
    return result


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate JSON field')
        result[name] = value
    return result


def _parse(line):
    try:
        value = json.loads(line, parse_constant=_invalid_constant, parse_float=_finite_float,
                           object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise RpcError(-32700, 'Invalid JSON') from None
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
        raise RpcError(-32600, 'Invalid JSON-RPC request')
    if 'id' in value and not valid_id(value['id']):
        raise RpcError(-32600, 'Invalid request ID')
    return value


def serve(daemon, source=None, output=None):
    """Read responses while a serial request worker may be waiting for approval."""
    source = source or sys.stdin.buffer
    peer = Peer(output or sys.stdout.buffer, daemon.config.daemon)
    daemon.peer = peer
    requests = queue.Queue(daemon.config.daemon.max_pending_requests)
    active_ids = set()
    active_lock = threading.Lock()

    def work():
        while True:
            request = requests.get()
            if request is None:
                return
            request_id = request['id']
            try:
                result = daemon.dispatch(request['method'], request.get('params', {}))
                peer.send({'id': request_id, 'result': result})
            except RpcError as error:
                peer.error(request_id, error.code, error.message)
            except KeyError:
                peer.error(request_id, -32004, 'Session or resource not found')
            except (ValueError, TypeError, IndexError):
                peer.error(request_id, -32602, 'Invalid parameters or unavailable configuration')
            except Exception as error:
                print('Daemon request failed (' + type(error).__name__ + ')', file=sys.stderr)
                peer.error(request_id, -32603, 'Engine request failed')
            finally:
                with active_lock:
                    active_ids.discard(request_id)

    worker = threading.Thread(target=work, name='tessera-rpc-requests', daemon=True)
    worker.start()
    initialized = False
    ready = False
    try:
        while not peer.closed:
            line = source.readline(daemon.config.daemon.max_frame_bytes + 1)
            if not line:
                break
            if len(line) > daemon.config.daemon.max_frame_bytes:
                while line and not line.endswith(b'\n'):
                    line = source.readline(daemon.config.daemon.max_frame_bytes + 1)
                peer.error(None, -32005, 'Request exceeds configured frame limit')
                continue
            request_id = None
            try:
                if not line.endswith(b'\n'):
                    raise RpcError(-32700, 'Incomplete JSON frame')
                request = _parse(line)
                request_id = request.get('id')
                if 'method' not in request:
                    if 'id' not in request or ('result' in request) == ('error' in request):
                        raise RpcError(-32600, 'Invalid response fields')
                    peer.response(request)
                    continue
                if set(request) - {'jsonrpc', 'id', 'method', 'params'}:
                    raise RpcError(-32600, 'Invalid request fields')
                method = request['method']
                params = request.get('params', {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise RpcError(-32602, 'Method and object parameters required')
                if method == 'initialize' and 'id' in request:
                    if initialized:
                        raise RpcError(-32600, 'Already initialized')
                    peer.send({'id': request_id, 'result': daemon.initialize(params)})
                    initialized = True
                elif method == 'initialized' and 'id' not in request and initialized:
                    ready = True
                elif 'id' not in request:
                    continue
                elif not ready:
                    raise RpcError(-32002, 'Initialize the attachment first')
                else:
                    with active_lock:
                        if request_id in active_ids:
                            raise RpcError(-32600, 'Duplicate outstanding request ID')
                        active_ids.add(request_id)
                    try:
                        requests.put_nowait(request)
                    except queue.Full:
                        with active_lock:
                            active_ids.discard(request_id)
                        raise RpcError(-32006, 'Request queue is full') from None
            except RpcError as error:
                peer.error(request_id, error.code, error.message)
    except KeyboardInterrupt:
        pass
    finally:
        # A disconnected client can never authorize a tool during drain.
        peer.deny_approvals()
        requests.put(None)
        worker.join()
        daemon.close()
        peer.close()
