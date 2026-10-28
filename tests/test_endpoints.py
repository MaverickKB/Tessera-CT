import pytest

from tessera.endpoints import EndpointPool, EndpointUnavailable


def test_empty_pool_is_inert():
    calls = []
    pool = EndpointPool([], lambda endpoint, timeout: calls.append(endpoint))
    assert pool.status() == []
    with pytest.raises(EndpointUnavailable):
        pool.select()
    with pytest.raises(EndpointUnavailable):
        pool.run(lambda endpoint: calls.append(endpoint))
    assert calls == []


def test_preferred_reprobe_and_failback_without_sleep():
    now = [0.0]
    healthy = {"preferred": False, "fallback": True}
    probes = []
    def probe(endpoint, timeout):
        probes.append((endpoint, timeout))
        if not healthy[endpoint]:
            raise OSError("down")
    pool = EndpointPool(["preferred", "fallback"], probe, probe_timeout=.2, probe_interval=5, clock=lambda: now[0])
    assert pool.select() == "fallback"
    healthy["preferred"] = True
    assert pool.select() == "fallback"
    assert len(probes) == 2
    now[0] = 6
    assert pool.select() == "preferred"
    assert pool.status()[0]["healthy"] is True


def test_operation_failover_and_explicit_nonretry_policy():
    calls = []
    def operation(endpoint):
        calls.append(endpoint)
        if endpoint == "first":
            raise OSError("unavailable")
        return "ok"
    pool = EndpointPool(["first", "second"], lambda endpoint, timeout: None)
    assert pool.run(operation) == ("ok", "second")
    assert calls == ["first", "second"]
    pool = EndpointPool(["first", "second"], lambda endpoint, timeout: None)
    calls.clear()
    with pytest.raises(OSError):
        pool.run(operation, retry=False)
    assert calls == ["first"]
    assert pool.select() == "second"


def test_all_down_recovery_is_lazy_and_each_selection_probes_once():
    healthy = False
    probes = []

    def probe(endpoint, timeout):
        probes.append(endpoint)
        if not healthy:
            raise OSError("unavailable")

    pool = EndpointPool(["preferred", "fallback"], probe, clock=lambda: 0)
    assert probes == []
    with pytest.raises(EndpointUnavailable):
        pool.select()
    assert probes == ["preferred", "fallback"]
    healthy = True
    assert pool.select() == "preferred"
    assert probes == ["preferred", "fallback", "preferred"]


def test_failed_operations_are_attempted_once_and_preserve_last_error():
    class TransportError(Exception):
        pass

    calls = []

    def operation(endpoint):
        calls.append(endpoint)
        raise TransportError("private detail placeholder")

    pool = EndpointPool(["first", "first", "second"], lambda endpoint, timeout: None,
                        retry_on=(TransportError,), probe_interval=0)
    with pytest.raises(TransportError, match="private detail placeholder"):
        pool.run(operation)
    assert calls == ["first", "second"]
    states = pool.status()
    assert [state["error"] for state in states] == ["TransportError", "TransportError"]
    assert "private detail placeholder" not in str(states)
    states[0]["healthy"] = True
    assert pool.status()[0]["healthy"] is False


def test_excluded_endpoints_are_never_probed_and_unexpected_errors_propagate():
    probes = []
    pool = EndpointPool(["preferred", "fallback"],
                        lambda endpoint, timeout: probes.append(endpoint))
    assert pool.select(exclude={"preferred"}) == "fallback"
    assert probes == ["fallback"]
    calls = []

    def operation(endpoint):
        calls.append(endpoint)
        raise ValueError("operation error")

    with pytest.raises(ValueError, match="operation error"):
        pool.run(operation)
    assert calls == ["preferred"]
    assert pool.status()[0]["healthy"] is True
