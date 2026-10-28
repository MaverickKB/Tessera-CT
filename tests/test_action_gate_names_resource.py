"""A gate refusal names the resource that set the ceiling.

The soft band is 0.25 <= disrupts_reasoning < 0.7. The ceiling can be the
process executing the session, an endpoint, an engine, or a rank. The
refusal names that resource. The block decision is unchanged.
"""
import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.jev import JudgeKit
from tessera.loop import Session


def evaluator(per_resource, other=None):
    """Score each resource by index; everything else defaults low."""
    other = other or {}

    def evaluate(state, questions):
        answers = {}
        for name in questions:
            if name.startswith('interrupts_resource_'):
                index = int(name.rsplit('_', 1)[1])
                value = per_resource.get(index, 0.05)
            elif name == 'advances_goal':
                value = other.get(name, 0.99)
            else:
                value = other.get(name, 0.02)
            answers[name] = {'type': 'noul', 'noul': value}
        return {'model': 'test-judge', 'answers': answers}
    return evaluate


def routed_session(tmp_path, monkeypatch, per_resource, other=None):
    """A session whose serving chain resolves to one real endpoint with a rank."""
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = True
    judge = JudgeKit(enabled=True, action_gate_enabled=True,
                     evaluator=evaluator(per_resource, other))
    session = Session(config, backend=FakeBackend([]), router=judge)
    monkeypatch.setattr(session.dependency_resolver, 'resolve', lambda *args: {
        'backend': 'auto', 'endpoint': 'http://router.example:8680/v1',
        'requested_model': 'auto', 'resolution': 'resolved',
        'router_host': 'router',
        'engines': [{'engine': 'glm-5.3', 'model': 'glm-5.3-tensor',
                     'base_url': 'http://router.example:8680/v1',
                     'endpoint_host_namespace': 'sparks',
                     'rank_resolution': 'inventory_and_live_model',
                     'ranks': [{'id': 'spark-head', 'name': 'TensorFold-head',
                                'host': 'spark-head', 'role': 'head'}]}],
    })
    return session


def refusal(session, name='write_file', arguments=None):
    arguments = arguments or {'path': 'ordinary.txt', 'text': 'ok'}
    _, record, _ = session._call_tool({'id': 'call', 'name': name, 'arguments': arguments})
    return record


def test_the_soft_band_names_the_resource_that_scored_the_ceiling(tmp_path, monkeypatch):
    """Resource 3 is the rank; it is the one the judge worried about.

    Measured question order for this fixture: 0 executing process,
    1 inference endpoint, 2 backing model service, 3 distributed rank.

    The soft band now requires the judge to believe the action touches hosting
    as well as interrupting something (see SOFT_FLOOR in action_judge.py), so
    changes_hosting is set high enough to corroborate the 0.55 ceiling.
    """
    with routed_session(tmp_path, monkeypatch, {3: 0.55}, {'changes_hosting': 0.45}) as session:
        record = refusal(session)
    assert record['blocked']
    assert 'TensorFold-head' in record['output'], record['output']
    assert 'may interrupt' in record['output']
    # The vague sentence is gone.
    assert 'serving or reasoning' not in record['output']
    # The block decision is unchanged from before the fix: soft band still blocks.
    assert 'otherwise retry it' in record['output']


def test_the_soft_band_does_not_name_a_resource_that_scored_low(tmp_path, monkeypatch):
    """Naming must follow the ceiling, not question order.

    Resource 1 is the inference endpoint and scores 0.24; resource 2 is the
    backing model service and scores 0.55. The message must name the service.
    """
    with routed_session(tmp_path, monkeypatch, {1: 0.24, 2: 0.55}, {'changes_hosting': 0.45}) as session:
        record = refusal(session)
    assert record['blocked']
    assert 'glm-5.3' in record['output'], record['output']
    assert '8680' not in record['output'], record['output']


def test_the_deny_band_names_its_resource_too(tmp_path, monkeypatch):
    """A confident refusal gets the same treatment, not just the soft band."""
    with routed_session(tmp_path, monkeypatch, {1: 0.99}) as session:
        record = refusal(session, 'shell', {'command': 'restart-hosting'})
    assert record['blocked']
    assert '8680' in record['output'], record['output']
    assert 'would interrupt' in record['output']
    assert 'serving or execution required by this session' not in record['output']


def test_every_resource_kind_gets_a_distinguishable_label():
    """A label that collapses to 'a required resource' would be no better."""
    from tessera.action_judge import _resource_label
    labels = {
        _resource_label({'kind': 'executing process', 'host': 'mac', 'pid': 41643}),
        _resource_label({'kind': 'inference endpoint', 'url': 'http://h:8680/v1',
                         'model_requested': 'auto'}),
        _resource_label({'kind': 'backing model service', 'name': 'glm-5.3', 'model': 'm'}),
        _resource_label({'kind': 'required distributed model rank', 'name': 'head',
                         'host': 'spark-head', 'role': 'head'}),
    }
    assert len(labels) == 4
    assert all('required resource' not in label for label in labels)
    assert '41643' in _resource_label({'kind': 'executing process', 'host': 'mac', 'pid': 41643})


def test_closest_resource_picks_the_max_not_the_first(tmp_path, monkeypatch):
    """_closest_resource must agree with the max() that set the ceiling."""
    from tessera.action_judge import _closest_resource
    with routed_session(tmp_path, monkeypatch, {}) as session:
        state = {'executing_session': {'host': 'mac', 'pid': 1},
                 'serving_dependencies': [], 'active_descendants': []}
        probabilities = {'interrupts_resource_0': 0.10, 'interrupts_resource_1': 0.60}
        chosen = _closest_resource(state, probabilities)
    assert chosen is not None
    # Direct shape check: max wins even when it is not index 0.
    state = {'executing_session': {'host': 'mac', 'pid': 1},
             'serving_dependencies': [{'endpoint': 'http://h:8680/v1',
                                       'requested_model': 'auto', 'engines': []}],
             'active_descendants': []}
    chosen = _closest_resource(state, {'interrupts_resource_0': 0.10,
                                       'interrupts_resource_1': 0.60})
    assert chosen['kind'] == 'inference endpoint'
