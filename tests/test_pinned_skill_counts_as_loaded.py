"""A skill the prompt pinned is available to the turn, so scoring must agree.

The turn-state block tells the model "Already loaded, do not reload unless the
file changed: <names>." A session that obeys produced no skill_load call, and the
scorer counted only this turn's skill_load calls, so obedience was recorded as
"skill not loaded" and two such turns pushed the lesson to needs_revision.
"""

import json

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.hooks import HookBus
from tessera.learning import LearningStore
from tessera.loop import Session, run_turn
from tessera.sessions import SessionManager

from test_loop import session_config
from test_skills import turn_state, write_skill
from test_autonomy import config_for


TRIGGER = 'The operator asks how many cards are open on the board right now'


def store(tmp_path):
    return LearningStore(tmp_path / 'lessons.json', pass_score=0.5, fail_limit=2, clock=lambda: 100.0)


def bound(tmp_path, skill='board-count'):
    st = store(tmp_path)
    lesson = st.record_lesson('home', TRIGGER, 'answered from the ordinal',
                              'read the live board first', skill=skill)
    return st, lesson


def test_a_pinned_skill_absorbs_the_lesson_with_no_skill_load_call(tmp_path):
    st, lesson = bound(tmp_path)
    st.observe_turn('home', TRIGGER, [], 0.9, True, False, pinned=['board-count'])
    saved = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))
    row = saved['lessons'][lesson['id']]
    assert row['status'] == 'absorbed', row['status']
    assert row['fails'] == 0
    entry = row['checks'][-1]
    assert entry['passed'] is True
    assert entry['loaded'] is False, 'no skill_load call happened in this turn'
    assert entry['pinned'] is True, 'the prompt is what made the skill available'


def test_the_same_turn_without_the_pin_is_still_a_miss(tmp_path):
    """The discriminator: identical turn, no pin, and the old verdict must stand."""
    st, lesson = bound(tmp_path)
    st.observe_turn('home', TRIGGER, [], 0.9, True, False)
    saved = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))
    row = saved['lessons'][lesson['id']]
    assert row['status'] == 'written'
    assert row['fails'] == 1
    assert row['checks'][-1]['note'] == 'skill not loaded'
    assert row['checks'][-1]['pinned'] is False


def test_a_pin_for_a_different_skill_does_not_count(tmp_path):
    st, lesson = bound(tmp_path)
    st.observe_turn('home', TRIGGER, [], 0.9, True, False, pinned=['some-other-skill'])
    row = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))['lessons'][lesson['id']]
    assert row['status'] == 'written'
    assert row['fails'] == 1


def test_a_pinned_skill_with_a_low_score_is_not_absorbed(tmp_path):
    st, lesson = bound(tmp_path)
    st.observe_turn('home', TRIGGER, [], 0.1, True, False, pinned=['board-count'])
    row = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))['lessons'][lesson['id']]
    assert row['status'] == 'written'
    assert row['fails'] == 1
    assert row['checks'][-1]['note'] == 'check missed'


def test_the_loop_reports_the_pinned_names_on_turn_end(tmp_path):
    """End to end: the prompt says it, and turn_end carries the same names."""
    write_skill(tmp_path / 'skills', 'board-count', 'Read the live board before counting.')
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'skill_load', 'arguments': {'name': 'board-count'}}]},
        'Loaded it.',
        'Three cards are open.',
    ])
    ended = []
    hooks = HookBus()
    hooks.register('turn_end', ended.append)
    config = session_config(tmp_path)
    config.tools.approval = 'never'
    with Session(config, backend=backend, hooks=hooks) as session:
        run_turn(session, 'Load the board-count skill')
        # Turn 1 loaded it, so turn 1 has no pin yet and must not claim one.
        assert ended[-1].get('pinned_skills') == []
        run_turn(session, 'Now count the open cards')
        assert 'Already loaded, do not reload unless the file changed: board-count.' in turn_state(backend.calls[-1])
        assert ended[-1].get('pinned_skills') == ['board-count']


def test_the_real_session_manager_credits_a_pinned_skill_end_to_end(tmp_path):
    """The whole path, not just the parts: loop -> turn_end -> sessions -> store.

    This is the seam the unit tests above cannot cover. sessions.py:_on_turn_end is
    the only caller of observe_turn, so a bug in how it forwards `pinned_skills`
    would leave every other test in this file green.
    """
    write_skill(tmp_path / 'skills', 'board-count', 'Read the live board before counting.')
    config = config_for(tmp_path)
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'skill_load', 'arguments': {'name': 'board-count'}}]},
        'Loaded it.',
        'Three cards are open.',
    ])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    try:
        slug = manager.create('Home')['slug']
        manager.autonomy.learning.record_lesson(
            slug, TRIGGER, 'answered from the ordinal', 'read the live board first',
            skill='board-count')
        lesson_id = manager.autonomy.learning.list_lessons(slug)[0]['id']

        # Turn 1 loads the skill on a message that does NOT match the trigger, so the
        # lesson is untouched and turn 2 is the one that exercises the pin.
        manager.submit(slug, 'Load the board-count skill')
        assert manager.wait(slug, timeout=10)
        row = manager.autonomy.learning._lessons[lesson_id]
        assert row['status'] == 'written' and row['checks'] == [], row['status']

        manager.submit(slug, 'Now ' + TRIGGER)
        assert manager.wait(slug, timeout=10)
        row = manager.autonomy.learning._lessons[lesson_id]
        last = row['checks'][-1]
        assert last['loaded'] is False, 'turn 2 must not call skill_load'
        assert last['pinned'] is True, 'the prompt is what made it available'
        assert row['status'] == 'absorbed', row['status']
        assert row['fails'] == 0
    finally:
        manager.close()


def test_the_real_session_manager_keeps_a_pinned_lesson_open_after_a_failed_tool(tmp_path):
    write_skill(tmp_path / 'skills', 'board-count', 'Read the live board before counting.')
    config = config_for(tmp_path)
    config.jev.enabled = False
    config.jev.session_gates_enabled = False
    backend = FakeBackend([
        {'tool_calls': [{'id': 'a', 'name': 'skill_load', 'arguments': {'name': 'board-count'}}]},
        'Loaded it.',
        {'tool_calls': [{'id': 'b', 'name': 'shell', 'arguments': {'command': 'exit 1'}}]},
        'The build failed; I cannot finish.',
    ])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    try:
        slug = manager.create('Home')['slug']
        manager.autonomy.learning.record_lesson(
            slug, TRIGGER, 'answered from the ordinal', 'read the live board first',
            skill='board-count')
        lesson_id = manager.autonomy.learning.list_lessons(slug)[0]['id']

        manager.submit(slug, 'Load the board-count skill')
        assert manager.wait(slug, timeout=10)
        row = manager.autonomy.learning._lessons[lesson_id]
        assert row['status'] == 'written'
        assert row['checks'] == []

        manager.submit(slug, 'Now ' + TRIGGER)
        assert manager.wait(slug, timeout=10)
        row = manager.autonomy.learning._lessons[lesson_id]
        assert row['status'] == 'written'
        assert row['fails'] == 0
        last = row['checks'][-1]
        assert last['passed'] is False
        assert last['tools_failed'] is True
        assert last['pinned'] is True
        assert last['loaded'] is False
        assert last['note'] == 'tools failed'
        assert 'Open lesson' in manager.autonomy.learning.context_for(slug, 'Now ' + TRIGGER)

        session = manager.get(slug)
        messages = list(session.log.iter())
        last_agent = messages[-1]
        assert last_agent.kind == 'agent'
        for key in ('judge_score', 'judge_source', 'verification', 'output_gate'):
            assert key not in last_agent.meta
        assert session.last_turn_success is True
    finally:
        manager.close()
