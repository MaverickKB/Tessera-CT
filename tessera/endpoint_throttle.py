"""Pace one model endpoint. This is not a swarm cap.

``throttle_requests_per_minute`` and ``throttle_tokens_per_minute`` are
optional. ``None`` means the user left the box empty. Empty is unlimited in
Safe mode and in YOLO mode. This gate adds no number of its own. A positive
integer set by hand is enforced in both modes. YOLO drops only a default the
system added, and there is no throttle default to drop.

Requests are counted when a call starts. Tokens are the usage that call
reports when it finishes. A call that reports no usage adds no tokens.
Host names and model ids are not consulted.
"""

from collections import deque
from contextlib import contextmanager
import threading
import time

from .endpoint_limit import EndpointWaitCancelled

_paces_lock = threading.Lock()
_paces = {}
_held = threading.local()


def throttle_limits(config, name):
    """``(requests_per_minute, tokens_per_minute)``, each a positive int or None.

    None is unlimited. A stored integer is the user's hand-set pace in Safe
    mode and in YOLO mode.
    """
    backend = config.backends[name]
    return (
        _explicit(backend.throttle_requests_per_minute, 'throttle_requests_per_minute'),
        _explicit(backend.throttle_tokens_per_minute, 'throttle_tokens_per_minute'),
    )


def _explicit(value, label):
    if value is None:
        return None
    if type(value) is not int or isinstance(value, bool) or value < 1:
        raise ValueError(label + ' must be a positive integer or omitted')
    return value


def _pace(key):
    with _paces_lock:
        pace = _paces.get(key)
        if pace is None:
            pace = threading.Condition(threading.Lock())
            pace.requests = deque()
            pace.tokens = deque()
            pace.token_sum = 0
            _paces[key] = pace
        return pace


def _holding():
    charges = getattr(_held, 'charges', None)
    if charges is None:
        charges = {}
        _held.charges = charges
    return charges


def _minute():
    # The settings are per minute. This is that window, not a second limit.
    return 60.0


def _expire(pace, now):
    cutoff = now - _minute()
    while pace.requests and pace.requests[0] <= cutoff:
        pace.requests.popleft()
    while pace.tokens and pace.tokens[0][0] <= cutoff:
        pace.token_sum -= pace.tokens.popleft()[1]


def _delay(pace, now, requests_per_minute, tokens_per_minute):
    waits = []
    if requests_per_minute is not None and len(pace.requests) >= requests_per_minute and pace.requests:
        waits.append(pace.requests[0] + _minute() - now)
    if tokens_per_minute is not None and pace.token_sum >= tokens_per_minute and pace.tokens:
        running = pace.token_sum
        for stamp, count in pace.tokens:
            running -= count
            if running < tokens_per_minute:
                waits.append(stamp + _minute() - now)
                break
        else:
            waits.append(pace.tokens[-1][0] + _minute() - now)
    delay = max(waits) if waits else _minute()
    return delay if delay > 0 else _minute()


def _charge_nothing(tokens):
    return None


def _as_tokens(value):
    if isinstance(value, dict):
        from .rate_limits import usage_totals
        value = usage_totals(value).get('tokens')
    if isinstance(value, bool) or type(value) not in (int, float) or value <= 0:
        return 0
    return int(value)


def _record_tokens(pace, count, now):
    if count <= 0:
        return
    pace.tokens.append((now, count))
    pace.token_sum += count


@contextmanager
def endpoint_pace(home, name, requests_per_minute, tokens_per_minute, *, clock=None, wait=None, on_start=None, on_wait=None, cancel=None):
    """Wait until this call fits the endpoint pace, then yield a charge function.

    The same thread does not take a second request slot. ``charge`` records
    token usage once. Unlimited (both limits None) does not wait.
    """
    key = (str(home), name)
    charges = _holding()
    if key in charges:
        yield charges[key]
        return
    if requests_per_minute is None and tokens_per_minute is None:
        if on_start is not None:
            on_start((clock or time.monotonic)())
        yield _charge_nothing
        return
    clock = clock or time.monotonic
    pace = _pace(key)
    while True:
        if cancel is not None and cancel.is_set():
            raise EndpointWaitCancelled()
        now = clock()
        with pace:
            _expire(pace, now)
            requests_fit = requests_per_minute is None or len(pace.requests) < requests_per_minute
            tokens_fit = tokens_per_minute is None or pace.token_sum < tokens_per_minute
            if requests_fit and tokens_fit:
                if requests_per_minute is not None:
                    pace.requests.append(now)
                if on_start is not None:
                    on_start(now)
                break
            delay = _delay(pace, now, requests_per_minute, tokens_per_minute)
        if on_wait is not None:
            on_wait(f'waiting for {name} throttle')
            on_wait = None
        if wait is not None:
            wait(delay)
        elif cancel is not None:
            cancel.wait(delay)
        else:
            time.sleep(delay)
        if cancel is not None and cancel.is_set():
            raise EndpointWaitCancelled()
    charged = False

    def charge(tokens):
        nonlocal charged
        if charged or tokens_per_minute is None:
            return
        charged = True
        count = _as_tokens(tokens)
        if count <= 0:
            return
        with pace:
            _record_tokens(pace, count, clock())

    charges[key] = charge
    try:
        yield charge
    finally:
        charges.pop(key, None)
