"""Concurrent requests follow the limit configured on that model.

A saved ``max_concurrent_requests`` is a number the user set by hand. It is
that model's cap in Safe mode and in YOLO mode. An empty setting is not a
number the user wrote. Safe mode then allows one request at a time on a
model with no credential. YOLO mode drops that default and does not add a
number. A model that names a credential or a vendor CLI is unlimited in both
modes until the user sets one. YOLO mode is the existing approval switch:
``tools.approval`` in ``never`` or ``yolo``.

Whether a model has a credential decides the Safe-mode default. Host names
and model ids are not consulted.
"""

from contextlib import contextmanager
import threading

# Safe mode only. A local model that omits its own cap. Not applied in YOLO mode.
LOCAL_MODEL_CONCURRENCY = 1

class EndpointWaitCancelled(Exception):
    """Stop arrived while this call waited for a slot or a pace window.

    This is not a connection failure. A connection error fails over to another
    model; an operator Stop must end the turn.
    """


_gates_lock = threading.Lock()
_gates = {}
_held = threading.local()


def yolo_mode(config):
    """True when the live approval policy is the YOLO side of the existing switch.

    ``SessionManager.approval_status`` reports the same fact as ``yolo``:
    ``tools.approval`` is ``yolo`` or ``never``.
    """
    from .tools import AUTO_APPROVE
    tools = getattr(config, 'tools', None)
    approval = getattr(tools, 'approval', 'ask')
    return approval in AUTO_APPROVE


def _is_local(config, name):
    from .settings_ladder import no_credential
    return no_credential(config, name)


def request_limit(config, name):
    """Positive integer, or None when this model has no cap.

    A number stored on the model was set by hand and is enforced as written,
    in Safe mode and in YOLO mode. Safe mode supplies one in-flight request
    for a local model that omits the number. YOLO mode drops that default.
    """
    explicit = config.backends[name].max_concurrent_requests
    if explicit is not None:
        if type(explicit) is not int or isinstance(explicit, bool) or explicit < 1:
            raise ValueError('max_concurrent_requests must be a positive integer or omitted')
        return explicit
    if yolo_mode(config) or not _is_local(config, name):
        return None
    return LOCAL_MODEL_CONCURRENCY


def _holding():
    held = getattr(_held, 'keys', None)
    if held is None:
        held = set()
        _held.keys = held
    return held


def _gate(key):
    with _gates_lock:
        gate = _gates.get(key)
        if gate is None:
            gate = threading.Condition(threading.Lock())
            gate.active = 0
            _gates[key] = gate
        return gate


def notify_waiters(home, name):
    """Wake whoever is blocked on this model's slot so Stop can be seen."""
    key = (str(home), name)
    with _gates_lock:
        gate = _gates.get(key)
    if gate is None:
        return
    with gate:
        gate.notify_all()


def _stopped(cancel):
    return cancel is not None and cancel.is_set()


@contextmanager
def model_slot_value(home, name, limit, on_state=None, *, cancel=None):
    """Hold one in-flight request for this model. None means no cap.

    Re-entering from the same thread does not take a second slot, so a job can
    wrap the call and the transport can wrap it again.
    """
    key = (str(home), name)
    held = _holding()
    if key in held:
        yield
        return
    if limit is None:
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
        return
    if type(limit) is not int or isinstance(limit, bool) or limit < 1:
        raise ValueError('A model concurrency limit must be a positive integer or unlimited')
    gate = _gate(key)
    announced = False
    with gate:
        while gate.active >= limit:
            if _stopped(cancel):
                raise EndpointWaitCancelled()
            if not announced:
                announced = True
                if on_state is not None:
                    on_state(f'waiting for {name} slot')
            # A short wait lets Stop land even if notify races the wait entry.
            # Jobs pass no cancel and keep an indefinite wait.
            gate.wait(0.05 if cancel is not None else None)
        if _stopped(cancel):
            raise EndpointWaitCancelled()
        gate.active += 1
    held.add(key)
    try:
        yield
    finally:
        held.discard(key)
        with gate:
            gate.active -= 1
            gate.notify()


@contextmanager
def model_slot(config, name, on_state=None):
    with model_slot_value(config.home, name, request_limit(config, name), on_state):
        yield
