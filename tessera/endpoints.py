"""Ordered, lazy endpoint health checks with caller-controlled operation retries."""

import math
import threading
import time


class EndpointError(RuntimeError):
    pass


class EndpointUnavailable(EndpointError):
    pass


class EndpointPool:
    def __init__(self, endpoints, probe, probe_timeout=2, probe_interval=30,
                 *, retry_on=(OSError,), clock=time.monotonic):
        if not math.isfinite(probe_timeout) or probe_timeout <= 0:
            raise ValueError("Probe timeout must be positive and finite")
        if not math.isfinite(probe_interval) or probe_interval < 0:
            raise ValueError("Probe interval must be nonnegative and finite")
        # Endpoint identifiers belong to the caller's transport. The pool only
        # preserves preference order; constructing it never performs network I/O.
        self.endpoints = []
        for endpoint in endpoints:
            if endpoint not in self.endpoints:
                self.endpoints.append(endpoint)
        self.probe = probe
        self.probe_timeout = probe_timeout
        self.probe_interval = probe_interval
        self.retry_on = retry_on
        self.clock = clock
        self._health = {}
        for endpoint in self.endpoints:
            self._health[endpoint] = {
                "endpoint": endpoint, "healthy": None,
                "last_probe": None, "error": None,
            }
        self._lock = threading.RLock()
        self._selection_lock = threading.RLock()

    def _check(self, endpoint):
        try:
            self.probe(endpoint, self.probe_timeout)
        except self.retry_on as error:
            self.mark_failed(endpoint, error)
            return error
        with self._lock:
            self._health[endpoint].update(healthy=True, last_probe=self.clock(), error=None)
        return None

    def select(self, exclude=()):
        """Select the first healthy endpoint, periodically checking preferences.

        If every eligible endpoint is down, check again on the next selection
        so a completely unavailable service can recover without a healthy peer.
        The last probe failure stays attached so the caller can tell a timeout
        from a closed port.
        """
        with self._selection_lock:
            checked = set()
            failure = None
            for endpoint in self.endpoints:
                if endpoint in exclude:
                    continue
                with self._lock:
                    state = dict(self._health[endpoint])
                due = state["last_probe"] is None
                if not due:
                    due = self.clock() - state["last_probe"] >= self.probe_interval
                if due:
                    error = self._check(endpoint)
                    if error is not None:
                        failure = error
                    checked.add(endpoint)
                with self._lock:
                    healthy = self._health[endpoint]["healthy"]
                if healthy:
                    return endpoint
            for endpoint in self.endpoints:
                if endpoint in exclude or endpoint in checked:
                    continue
                error = self._check(endpoint)
                if error is not None:
                    failure = error
                with self._lock:
                    healthy = self._health[endpoint]["healthy"]
                if healthy:
                    return endpoint
        if failure is not None:
            raise EndpointUnavailable("No configured endpoints are reachable") from failure
        raise EndpointUnavailable("No configured endpoints are reachable")

    def mark_failed(self, endpoint, error):
        with self._lock:
            # Never store exception messages: transports can include credentials.
            self._health[endpoint].update(
                healthy=False, last_probe=self.clock(), error=type(error).__name__)

    def run(self, operation, *, retry=True):
        """Return (value, endpoint); retry only operations safe for replay."""
        attempted = set()
        last_error = None
        while True:
            try:
                endpoint = self.select(exclude=attempted)
            except EndpointUnavailable:
                if last_error is not None:
                    raise last_error from None
                raise
            attempted.add(endpoint)
            try:
                result = operation(endpoint)
            except self.retry_on as error:
                self.mark_failed(endpoint, error)
                if not retry:
                    raise
                last_error = error
            else:
                return result, endpoint

    def status(self):
        with self._lock:
            return [dict(self._health[endpoint]) for endpoint in self.endpoints]
