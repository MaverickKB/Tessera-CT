"""The soft band must corroborate before it refuses, and it must name every
address a Spark answers to.

Why this file exists. A score of 0.05 is not 0.25, and a host answers on more
than one address. Two faults follow from that.

FAULT 1, the floor. The soft band fired on `disrupts_reasoning` alone, which is
a max() over 9-12 independent judge answers. Replaying the live floor-session judge
log (the floor session's decisions, 3124 action_gate batches that Jev
actually answered, excluding the 23 strict-fallback batches):

    changes_hosting   median 0.040  p95 0.070  p98 0.110  p99 0.170
    resource ceiling  median 0.070  p95 0.120  p98 0.150  p99 0.180

Two real refusals fired with changes_hosting 0.05 and a ceiling of exactly 0.25
— an action with 1-in-20 odds of touching hosting, refused because one of nine
unrelated resources scored at the floor. 0.25 was a round number sitting just
above neither distribution's p99.

The fix is a conjunction at SOFT_FLOOR = 0.20, which sits above p99 of both
distributions. Replayed over the same 3124 batches it drops the two 0.05
refusals, keeps all 9 corroborated ones, and introduces 0 new refusals.

FAULT 2, the identity. The router probe compared an engine's base_url hostname
against exactly three fields per Spark (lanIp, llmHost, name) and emitted one
address per rank. Each Spark answers to
more than that: its LAN address, its Tailscale address, its inventory name, its
Tailscale DNS name and its MAC. A session that restarts a Spark over SSH may
name any of those. Jev was asked about a rank carrying only the LAN address, so
a command aimed at the Tailscale address looked like a different machine. That
is the hole described: the Sparks are the session's own brain, and the judge
could not see them.

Addresses in this file are placeholders. The measured values are in the
docstring above and in action_judge.py; nothing here embeds a real host.
"""
import pytest

from fake_backend import FakeBackend
from test_loop import session_config
from tessera.jev import JudgeKit
from tessera.loop import Session


def evaluator(per_resource, other=None):
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


def routed_session(tmp_path, monkeypatch, per_resource, other=None, ranks=None):
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
                     'ranks': ranks or [{'id': 'spark-head', 'name': 'TensorFold-head',
                                         'host': 'spark-head', 'role': 'head'}]}],
    })
    return session


def refusal(session, name='write_file', arguments=None):
    arguments = arguments or {'path': 'ordinary.txt', 'text': 'ok'}
    _, record, _ = session._call_tool({'id': 'call', 'name': name, 'arguments': arguments})
    return record


# --------------------------------------------------------------------------
# Fault 1: the floor is a conjunction, and both halves must be elevated.
# --------------------------------------------------------------------------

def test_a_low_hosting_action_is_not_refused_by_a_bare_interrupt_ceiling(tmp_path, monkeypatch):
    """The exact rejected shape: changes_hosting 0.05, ceiling 0.25.

    Two of these exist in the live log. Both were refused and both told the
    session it might interrupt a resource it had no path to touch.
    """
    with routed_session(tmp_path, monkeypatch, {1: 0.25}, {'changes_hosting': 0.05}) as session:
        record = refusal(session)
    assert not record['blocked'], record['output']


def test_the_floor_is_not_arbitrary_it_sits_above_the_judges_own_p99():
    """SOFT_FLOOR must sit above p99 of both measured distributions.

    This is the check that answers "has no justifiable basis": the constant is
    derived from the judge's own answers, not chosen. If someone lowers it
    without new measurement, this test says so.
    """
    from tessera.action_judge import SOFT_FLOOR, DENY_PROBABILITY
    measured_p99_changes_hosting = 0.17
    measured_p99_resource_ceiling = 0.18
    assert SOFT_FLOOR >= measured_p99_changes_hosting
    assert SOFT_FLOOR >= measured_p99_resource_ceiling
    # And it must stay below the deny band, or the soft band disappears.
    assert SOFT_FLOOR < DENY_PROBABILITY


def test_a_corroborated_soft_band_still_refuses(tmp_path, monkeypatch):
    """Both halves elevated is a real refusal, not a false one."""
    with routed_session(tmp_path, monkeypatch, {1: 0.55}, {'changes_hosting': 0.45}) as session:
        record = refusal(session)
    assert record['blocked']
    assert 'may interrupt' in record['output']


def test_one_elevated_half_alone_is_not_enough(tmp_path, monkeypatch):
    """Each half alone, at the same 0.45 level, must not refuse.

    The gate blocks on the conjunction. If either half alone suffices, the
    noise from a max() over nine resources is back.
    """
    with routed_session(tmp_path, monkeypatch, {}, {'changes_hosting': 0.45}) as session:
        ceiling_low = refusal(session)
    assert not ceiling_low['blocked'], ceiling_low['output']

    with routed_session(tmp_path, monkeypatch, {1: 0.45}, {'changes_hosting': 0.02}) as session:
        hosting_low = refusal(session)
    assert not hosting_low['blocked'], hosting_low['output']


def test_the_deny_band_is_untouched_by_the_conjunction(tmp_path, monkeypatch):
    """A confident interrupt still blocks with changes_hosting low.

    Stopping a resource IS a hosting change, so the deny band must not start
    requiring corroboration it never needed.
    """
    with routed_session(tmp_path, monkeypatch, {1: 0.99}, {'changes_hosting': 0.02}) as session:
        record = refusal(session, 'shell', {'command': 'restart-hosting'})
    assert record['blocked']
    assert 'would interrupt' in record['output']


# --------------------------------------------------------------------------
# Fault 2: every address a Spark answers to reaches the judge and the session.
# --------------------------------------------------------------------------

SPARK_ALIASES = ['spark-head', 'spark-head.lan.example', 'spark-head-1.ts.example',
                 '4c:bb:47:2c:48:e7']


def test_the_refusal_names_every_address_the_spark_answers_to(tmp_path, monkeypatch):
    """A restart aimed at the Tailscale name must not look like another machine.

    The old probe emitted one address per rank, so only one of a Spark's
    identifiers ever reached Jev or the session.
    """
    ranks = [{'id': 'spark-head', 'name': 'spark-head', 'host': 'spark-head.lan.example',
              'role': 'head', 'identifiers': SPARK_ALIASES}]
    with routed_session(tmp_path, monkeypatch, {3: 0.55}, {'changes_hosting': 0.45},
                        ranks=ranks) as session:
        record = refusal(session)
    assert record['blocked']
    output = record['output']
    assert 'spark-head-1.ts.example' in output, output
    assert '4c:bb:47:2c:48:e7' in output, output


def test_the_resource_question_carries_the_identifiers_and_says_any_of_them_counts():
    """The judge is told these aliases are one machine, in the criteria.

    Without this sentence Jev sees a resource whose host is the LAN name and a
    command naming the Tailscale name, and answers "preserves it" — which is
    exactly the miss described.
    """
    from tessera.action_judge import action_questions
    state = {
        'executing_session': {'host': 'mac', 'pid': 1},
        'serving_dependencies': [{
            'endpoint': 'http://router.example:8680/v1', 'requested_model': 'auto',
            'router_host': 'router',
            'engines': [{'engine': 'glm-5.3', 'model': 'glm-5.3-tensor',
                         'base_url': 'http://router.example:8680/v1',
                         'endpoint_host_namespace': 'sparks',
                         'ranks': [{'id': 'spark-head', 'name': 'spark-head',
                                    'host': 'spark-head.lan.example', 'role': 'head',
                                    'identifiers': SPARK_ALIASES}]}],
        }],
        'active_descendants': [],
        'unresolved_serving_dependencies': [],
    }
    questions = action_questions(state)
    rank_questions = [q for name, q in questions.items() if name.startswith('interrupts_resource_')
                      and q['instructions']['required_resource'].get('kind') == 'required distributed model rank']
    assert len(rank_questions) == 1
    resource = rank_questions[0]['instructions']['required_resource']
    assert resource['identifiers'] == SPARK_ALIASES
    false_criterion = rank_questions[0]['criteria']['false']
    assert 'Tailscale IP' in false_criterion, false_criterion
    assert 'same machine' in false_criterion, false_criterion


def test_the_rank_label_lists_aliases_without_repeating_the_primary():
    """The label must add information, not repeat the host it already names.

    Measured output for this fixture:
        'model rank spark-head on spark-head.lan.example (head) — also
         reachable as spark-head-1.ts.example, 4c:bb:47:2c:48:e7'
    The name and host appear once each as the subject; the aliases follow. A
    naive substring count would count 'spark-head' inside both aliases too, so
    the check is on the alias list itself rather than on substring frequency.
    """
    from tessera.action_judge import _resource_label
    label = _resource_label({'kind': 'required distributed model rank',
                             'name': 'spark-head', 'host': 'spark-head.lan.example',
                             'role': 'head', 'identifiers': SPARK_ALIASES})
    assert 'spark-head-1.ts.example' in label
    assert '4c:bb:47:2c:48:e7' in label
    # The primary subject appears exactly once, before the alias clause.
    subject, _, aliases = label.partition('— also reachable as ')
    assert subject.count('spark-head.lan.example') == 1, label
    assert subject.count('spark-head') == 2, label  # 'model rank spark-head on spark-head.lan.example'
    # And the host it already names is not repeated in the alias clause.
    assert 'spark-head.lan.example' not in aliases, label
    assert 'spark-head-1.ts.example' in aliases, label
