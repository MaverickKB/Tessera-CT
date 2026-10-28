import json
import sys
import time

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.sessions import SessionManager


def turn_state(call):
    """The turn-state block, found by its prefix: its index moves with hook context."""
    return next(m['content'] for m in call['messages'] if str(m.get('content', '')).startswith('<turn-state>'))


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def config_for(root, **autonomy):
    config = Config(home=root, cwd=root)
    config.jev.enabled = False
    config.tools.approval = 'never'
    config.autonomy.enabled = True
    config.autonomy.tick_seconds = 3600
    for key, value in autonomy.items():
        setattr(config.autonomy, key, value)
    return config


def call(name, **arguments):
    return {'tool_calls': [{'id': 'c-' + name, 'name': name, 'arguments': arguments}]}


def install_fake_clock(manager, clock=None):
    """Drive autonomy from this clock only. Explicit tick()/deliver() observe it.

    The background poller wakes as soon as post() notifies it, then reads the
    clock before it takes the lock. A jump can therefore land in a window where
    The operator is briefly not engaged, before the test records the presence event that
    belongs at that same instant, and the poller delivers the wake early.
    """
    clock = Clock() if clock is None else clock
    manager.autonomy.clock = clock
    manager.autonomy.close()
    thread = manager.autonomy.thread
    if thread is not None and thread.is_alive():
        raise RuntimeError('autonomy poller did not stop; fake-clock tests would stay order-dependent')
    return clock


def open_manager(root, script, **autonomy):
    clock = Clock()
    backend = FakeBackend(script)
    manager = SessionManager(config_for(root, **autonomy), backend_factory=lambda slug, name: backend)
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    return manager, slug, clock, backend


def deliver(manager, slug):
    manager.autonomy.tick()
    assert manager.wait(slug, timeout=10)


def messages(manager, slug):
    return list(manager.get(slug).log.iter())


def test_timer_wakes_and_the_reply_is_the_work(tmp_path):
    script = [call('set_timer', note='check the build', in_seconds=600), 'Timer set.', 'Checked the build.']
    manager, slug, clock, backend = open_manager(tmp_path, script)
    with manager:
        manager.submit(slug, 'Remind yourself to check the build in ten minutes')
        assert manager.wait(slug, timeout=10)
        assert [t['note'] for t in manager.autonomy.list_timers(slug)] == ['check the build']
        deliver(manager, slug)
        assert len(messages(manager, slug)) == 5
        clock.now += 601
        deliver(manager, slug)
        log = messages(manager, slug)
        wake = [m for m in log if m.kind == 'user' and m.meta.get('wake')]
        assert len(wake) == 1 and 'check the build' in wake[0].text
        assert wake[0].meta['wake']['events'] == ['timer']
        assert log[-1].kind == 'agent' and log[-1].text == 'Checked the build.'
        assert manager.autonomy.list_timers(slug) == []
        assert manager.autonomy.state['wakes'][-1]['spoke'] is True
        assert backend.calls[-2]['tools'] is not None


def test_a_wake_reply_is_proactive(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['The deploy finished cleanly.'])
    with manager:
        manager.autonomy.post(slug, 'timer', 'Timer t-1 "deploy" is due.')
        deliver(manager, slug)
        last = messages(manager, slug)[-1]
        assert last.text == 'The deploy finished cleanly.' and last.meta.get('proactive') is True
        assert manager.autonomy.state['wakes'][-1]['spoke'] is True


def test_rest_is_a_choice_that_suppresses_proactive_output(tmp_path):
    manager, slug, _, _ = open_manager(tmp_path, [])
    with manager:
        session = manager.get(slug)
        assert session.tools.execute('rest', {})['resting']
        assert session.rest_requested


def test_background_task_completion_wakes_with_its_output(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Build passed.'], min_wake_gap_seconds=0)
    with manager:
        task = manager.autonomy.start_task(slug, 'build', f'{sys.executable} -c "print(42)"')
        deadline = time.time() + 10
        while manager.autonomy._children[task['id']].poll() is None and time.time() < deadline:
            time.sleep(0.05)
        deliver(manager, slug)
        wake = [m for m in messages(manager, slug) if m.meta.get('wake')][0]
        assert 'done with exit 0' in wake.text and '42' in wake.text
        assert manager.autonomy.task_status(slug, task['id'])['state'] == 'done'


def test_stalled_and_overlong_tasks_wake_once_each(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Noted the stall.', 'Noted the timeout.'], min_wake_gap_seconds=0,
                                           task_stall_seconds=60, task_max_seconds=600)
    with manager:
        task = manager.autonomy.start_task(slug, 'sleepy', 'sleep 30')
        clock.now += 61
        deliver(manager, slug)
        clock.now += 61
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        clock.now += 600
        deliver(manager, slug)
        texts = [m.text for m in messages(manager, slug) if m.meta.get('wake')]
        assert 'printed nothing' in texts[0] and 'killed' in texts[1]
        assert manager.autonomy.task_status(slug, task['id'])['state'] == 'timed_out'


def test_presence_wakes_only_after_a_real_absence(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Welcome back.', 'Still here.', 'Back again.'], presence_away_seconds=300,
                                           min_wake_gap_seconds=0, engaged_focus_seconds=0)
    with manager:
        assert manager.autonomy.presence(slug, 'arrive', 'window')['woke'] is True
        deliver(manager, slug)
        clock.now += 10
        manager.autonomy.presence(slug, 'leave')
        clock.now += 30
        assert manager.autonomy.presence(slug, 'arrive')['woke'] is False
        manager.autonomy.presence(slug, 'leave')
        clock.now += 1800
        manager.autonomy.post(slug, 'timer', 'Timer t-9 "tea" is due.')
        deliver(manager, slug)
        clock.now += 1800
        assert manager.autonomy.presence(slug, 'arrive', 'window')['woke'] is True
        deliver(manager, slug)
        wakes = [m.text for m in messages(manager, slug) if m.meta.get('wake')]
        assert len(wakes) == 3 and 'away 60m' in wakes[2] and 'you woke 1 time(s) and spoke 1' in wakes[2]


def test_gap_paces_a_runaway_and_never_drops_events(tmp_path):
    script = []
    for _ in range(3):
        script += [call('set_timer', note='again', in_seconds=30), 'Timer set.']
    manager, slug, clock, _ = open_manager(tmp_path, script, min_wake_gap_seconds=60, max_wakes_per_hour=0,
                                           max_wakes_per_day=0, min_timer_seconds=30)
    with manager:
        manager.autonomy.post(slug, 'timer', 'first')
        deliver(manager, slug)
        clock.now += 31
        deliver(manager, slug)
        status = manager.autonomy.status()
        assert status['wakes_last_hour'] == 1
        assert status['capped']['reason'] == 'min_wake_gap_seconds'
        assert status['held'][slug] >= 1
        clock.now += 60
        deliver(manager, slug)
        wakes = [m.text for m in messages(manager, slug) if m.meta.get('wake')]
        assert len(wakes) == 2


def test_timer_limits_and_restart_persistence(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, [], min_timer_seconds=30, max_timers=1)
    with manager:
        for bad in ({'in_seconds': 5}, {'in_seconds': 60, 'repeat_seconds': 1}, {}):
            try:
                manager.autonomy.set_timer(slug, 'x', **bad)
            except ValueError:
                pass
            else:
                raise AssertionError(bad)
        manager.autonomy.set_timer(slug, 'keep', in_seconds=60)
        try:
            manager.autonomy.set_timer(slug, 'second', in_seconds=60)
        except ValueError as error:
            assert 'cap' in str(error)
    with SessionManager(config_for(tmp_path), backend_factory=lambda slug, name: FakeBackend([])) as restored:
        assert [t['note'] for t in restored.autonomy.list_timers(slug)] == ['keep']


def test_continue_wakes_after_the_current_reply(tmp_path):
    script = [call('background_task', action='continue', note='finish the summary'), 'On it; I will keep going.',
              'Summary finished.']
    manager, slug, clock, _ = open_manager(tmp_path, script, engaged_message_seconds=60, engaged_focus_seconds=0)
    with manager:
        manager.submit(slug, 'use background_task to summarize later')
        assert manager.wait(slug, timeout=10)
        deliver(manager, slug)
        assert not [m for m in messages(manager, slug) if m.meta.get('wake')], 'waits while the operator is engaged'
        clock.now += 61
        assert manager.wait(slug, timeout=10)
        deliver(manager, slug)
        log = messages(manager, slug)
        assert 'finish the summary' in [m for m in log if m.meta.get('wake')][0].text
        assert log[-1].meta.get('proactive') is True


def test_outside_yolo_actions_become_approval_requests_that_wake_with_the_outcome(tmp_path):
    script = [call('background_task', action='start', command='echo approved-run'), 'Asked the operator.',
              call('shell', command='echo second'), 'Asked again.', 'Saw it ran.']
    manager, slug, clock, _ = open_manager(tmp_path, script, min_wake_gap_seconds=0)
    with manager:
        manager.get(slug).tools.approval = 'deny_risky'
        manager.submit(slug, 'run it with background_task')
        assert manager.wait(slug, timeout=10)
        manager.submit(slug, 'now use shell')
        assert manager.wait(slug, timeout=10)
        pending = manager.autonomy.status()['approvals']
        assert [a['tool'] for a in pending] == ['background_task', 'shell']
        notes = [m for m in messages(manager, slug) if m.meta.get('approval')]
        assert len(notes) == 2 and notes[0].meta['approval']['state'] == 'pending'
        assert manager.autonomy.status()['tasks'] == []
        manager.autonomy.resolve(pending[0]['id'], True, source='card')
        deadline = time.time() + 10
        while not manager.autonomy.state['held'].get(slug) and time.time() < deadline:
            time.sleep(0.05)
        deliver(manager, slug)
        wake = [m for m in messages(manager, slug) if m.meta.get('wake')][0]
        assert 'The operator (from the chat card) approved background_task' in wake.text and 'it ran' in wake.text
        assert len(manager.autonomy.status()['tasks']) == 1
        manager.autonomy.resolve(pending[1]['id'], False)
        deliver(manager, slug)
        wakes = [m.text for m in messages(manager, slug) if m.meta.get('wake')]
        assert "through the API, not the operator's chat card" in wakes[-1] and 'denied shell' in wakes[-1]
        results = [m.meta['approval_result']['state'] for m in messages(manager, slug) if m.meta.get('approval_result')]
        assert results == ['approved', 'denied']
        try:
            manager.autonomy.resolve(pending[1]['id'], True)
        except ValueError as error:
            assert 'already denied' in str(error)
        else:
            raise AssertionError('double resolve')


def test_yolo_runs_actions_directly(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, [call('background_task', action='start', command='true'), 'ok'])
    with manager:
        manager.submit(slug, 'run it with background_task')
        assert manager.wait(slug, timeout=10)
        assert len(manager.autonomy.status()['tasks']) == 1
        assert manager.autonomy.status()['approvals'] == []


def test_unanswered_approvals_expire_and_wake(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, [call('shell', command='true'), 'asked'],
                                           approval_expire_seconds=60)
    with manager:
        manager.get(slug).tools.approval = 'deny_risky'
        manager.submit(slug, 'use shell')
        assert manager.wait(slug, timeout=10)
        clock.now += 61
        deliver(manager, slug)
        assert 'expired' in [m for m in messages(manager, slug) if m.meta.get('wake')][0].text


def test_disabled_autonomy_registers_no_tools(tmp_path):
    config = config_for(tmp_path)
    config.autonomy.enabled = False
    backend = FakeBackend(['hi'])
    with SessionManager(config, backend_factory=lambda slug, name: backend) as manager:
        slug = manager.create('Home')['slug']
        manager.submit(slug, 'hello')
        assert manager.wait(slug, timeout=10)
        names = {tool['function']['name'] for tool in backend.calls[0]['tools'] or []}
        assert not names & {'rest', 'set_timer', 'background_task'}


def test_tool_images_reach_the_model_but_not_the_log(tmp_path):
    manager, slug, clock, backend = open_manager(tmp_path, [call('peek'), 'I see it.'])
    with manager:
        session = manager.get(slug)
        session.tools.register('peek', 'test image', {'type': 'object', 'properties': {}},
                               lambda: {'width': 2, 'image': {'caption': 'pic', 'data_url': 'data:image/png;base64,AAAA'}})
        manager.submit(slug, 'look with peek')
        assert manager.wait(slug, timeout=10)
        parts = backend.calls[1]['messages'][-1]['content']
        assert parts[1]['image_url']['url'] == 'data:image/png;base64,AAAA'
        assert all('base64' not in m.text for m in messages(manager, slug))


def test_no_repeat_wake_for_a_task_already_checked(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['ok'])
    with manager:
        task = manager.autonomy.start_task(slug, 'build', f'{sys.executable} -c "print(1)"')
        deadline = time.time() + 10
        while manager.autonomy._children[task['id']].poll() is None and time.time() < deadline:
            time.sleep(0.05)
        with manager.autonomy._lock:
            manager.autonomy._poll_tasks(clock())
        assert any(e['kind'] == 'task' for e in manager.autonomy.state['held'].get(slug, []))
        assert manager.autonomy.task_status(slug, task['id'])['state'] == 'done'
        assert not any(e['kind'] == 'task' for e in manager.autonomy.state['held'].get(slug, []))


def test_the_operator_can_cancel_a_timer_by_id(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, [])
    with manager:
        timer = manager.autonomy.set_timer(slug, 'check later', in_seconds=120, repeat_seconds=60)
        assert manager.autonomy.cancel_timer_for_operator(timer['id']) == {'cancelled': timer['id'], 'slug': slug}
        assert manager.autonomy.list_timers(slug) == []
        assert any(m.meta.get('timer_cancelled') for m in messages(manager, slug))
        try:
            manager.autonomy.cancel_timer_for_operator(timer['id'])
        except KeyError:
            pass
        else:
            raise AssertionError('second cancel')


def test_desk_idle_reading_parses_the_gdbus_reply():
    from tessera.cua import idle_seconds
    assert idle_seconds('(uint64 1234,)') == 1
    assert idle_seconds('(uint64 64000,)') == 64
    assert idle_seconds('') is None
def test_memory_self_tools_skip_approval_but_acting_stays_gated(tmp_path):
    from tessera.tools import approval_rule
    config = config_for(tmp_path)
    classes, rules = config.tools.approval_classes, config.tools.approval_rules
    assert approval_rule('clock_heartbeat', classes, rules) == 'allow'
    assert approval_rule('aethermind_write_layer', classes, rules) == 'allow'
    for name in ('mac_act', 'desk_act', 'shell', 'remote_write_file', 'background_task', 'helmet_anything'):
        assert approval_rule(name, classes, rules) == 'gate'
    assert approval_rule('clock_heartbeat', classes, {**rules, 'clock_heartbeat': 'gate'}) == 'gate'

    ran = []
    manager, slug, clock, _ = open_manager(tmp_path, [])
    with manager:
        session = manager.get(slug)
        session.tools.register('clock_heartbeat', 'self note', {'type': 'object', 'properties': {}},
                               lambda: ran.append('beat') or 'ok', risky=True)
        session.tools.register('mcp_act', 'acts', {'type': 'object', 'properties': {}}, lambda: 'acted', risky=True)
        for policy in ('deny_risky', 'ask'):
            session.tools.approval = policy
            assert session.tools.execute('clock_heartbeat', {}) == 'ok'
            assert session.tools.execute('mcp_act', {})['approval'] == 'requested'
        assert ran == ['beat', 'beat']
        session.tools.approval = 'yolo'
        assert session.tools.execute('mcp_act', {}) == 'acted'
        assert len(manager.autonomy.status()['approvals']) == 2


def wake_texts(manager, slug):
    return [m.text for m in messages(manager, slug) if m.meta.get('wake')]


def test_presence_while_the_operator_is_focused_folds_into_the_next_turn_without_a_wake(tmp_path):
    manager, slug, clock, backend = open_manager(tmp_path, ['Hi operator.'], min_wake_gap_seconds=0)
    with manager:
        manager.autonomy.presence(slug, 'arrive', 'window')
        manager.autonomy.presence(slug, 'leave')
        clock.now += 600
        manager.autonomy.presence(slug, 'arrive', 'window')
        deliver(manager, slug)
        assert wake_texts(manager, slug) == []
        assert manager.autonomy.status()['held'] == {}
        manager.submit(slug, 'hello')
        assert manager.wait(slug, timeout=10)
        state = turn_state(backend.calls[0])
        assert state.count('The operator is here (window)') == 1 and 'away 10m' in state
        deliver(manager, slug)
        assert wake_texts(manager, slug) == []


def test_timer_waits_while_the_operator_is_engaged_and_wakes_once_they_go_quiet(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Answer.', 'Tea is due, operator.'], min_wake_gap_seconds=0,
                                           engaged_message_seconds=300, engaged_typing_seconds=60,
                                           engaged_focus_seconds=900, presence_stale_seconds=300)
    with manager:
        manager.autonomy.presence(slug, 'arrive', 'window')
        manager.submit(slug, 'question')
        assert manager.wait(slug, timeout=10)
        manager.autonomy.post(slug, 'timer', 'Timer t-1 "tea" is due.')
        clock.now += 200
        deliver(manager, slug)
        assert wake_texts(manager, slug) == []
        clock.now += 200
        manager.autonomy.presence(slug, 'typing', 'window')
        deliver(manager, slug)
        assert wake_texts(manager, slug) == []
        clock.now += 200
        manager.autonomy.presence(slug, 'here', 'window')
        deliver(manager, slug)
        assert wake_texts(manager, slug) == [], 'a focused window with recent typing still counts'
        clock.now += 900
        deliver(manager, slug)
        texts = wake_texts(manager, slug)
        assert len(texts) == 1 and 'tea' in texts[0]
        assert manager.autonomy.status()['held'] == {}


def test_a_due_watch_waits_while_the_operator_is_talking_in_that_thread(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Watching.'], min_wake_gap_seconds=0,
                                           engaged_message_seconds=300, engaged_focus_seconds=0)
    with manager:
        manager.submit(slug, 'keep an eye on it')
        assert manager.wait(slug, timeout=10)
        clock.now += 60
        manager.autonomy.post(slug, 'timer', 'Timer t-watch "check the worker" is due.')
        manager.autonomy.tick()
        assert wake_texts(manager, slug) == [], 'a watch on the conversation thread waits while the operator is talking'
        assert manager.autonomy.status()['held'].get(slug) == 1


def test_steer_and_voice_count_as_operator_activity(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Done.'], min_wake_gap_seconds=0, engaged_focus_seconds=0)
    with manager:
        manager.steer(slug, 'idle steer queues a turn')
        assert manager.wait(slug, timeout=10)
        manager.autonomy.post(slug, 'continue', 'Continuation you queued: more')
        deliver(manager, slug)
        assert wake_texts(manager, slug) == []
        clock.now += manager.config.autonomy.engaged_message_seconds + 1
        assert manager.autonomy.engaged(slug) is None
        manager.autonomy.activity(slug, 'message')
        assert manager.autonomy.engaged(slug) == 'message'


def test_a_failed_wake_flags_its_error_so_the_chat_shows_no_banner(tmp_path):
    from tessera.backend import BackendError
    manager, slug, clock, _ = open_manager(tmp_path, [BackendError('down', 'connection'), 'Fine.'], min_wake_gap_seconds=0)
    with manager:
        manager.autonomy.post(slug, 'timer', 'Timer t-2 "x" is due.')
        deliver(manager, slug)
        manager.submit(slug, 'hello')
        assert manager.wait(slug, timeout=10)
        errors = [e for e in manager.runtime(slug).events.after(0) if e['event'] == 'error']
        assert len(errors) == 1 and errors[0]['data'].get('wake') is True


def test_board_work_is_reported_as_pressure_but_never_wakes_the_floor():
    """A session wakes for its own cards; the floor wakes for house
    pressure. So board work has to reach the report a person reads without becoming a
    wake reason. Filing is the model's job, so an unfiled card is work nobody can
    find, which is pressure rather than a person's homework.
    """
    from tessera.autonomy import board_pressure_lines

    brief = {'cards': [
        {'ordinal': 3, 'status': 'queued', 'project': '', 'age_seconds': 60},
        {'ordinal': 7, 'status': 'queued', 'project': '', 'age_seconds': 60},
        {'ordinal': 9, 'status': 'queued', 'project': 'board-v2', 'age_seconds': 7 * 3600},
        {'ordinal': 11, 'status': 'succeeded', 'project': '', 'age_seconds': 60},
        {'ordinal': 12, 'status': 'queued', 'project': 'board-v2', 'age_seconds': 600},
    ]}
    lines = board_pressure_lines(brief)
    assert len(lines) == 2
    assert lines[0].startswith('Pressure: 2 board card(s) unfiled (3, 7)')
    assert 'File them under a project' in lines[0]
    assert 'Do not ask a person to file them' in lines[0]
    assert lines[1].startswith('Pressure: 1 filed board card(s) older than six hours (9)')
    # A succeeded card and a fresh filed card are not pressure.
    assert '11' not in ' '.join(lines) and '12' not in ' '.join(lines)

    # Not latched: the same brief reports the same fact every reading, because this is
    # the state of the board and not a one-shot event.
    assert board_pressure_lines(brief) == lines
    # Filing the unfiled cards clears that line by itself. The six-hour card stays
    # reported, because it is still queued: the fact, not the act of filing.
    filed = {'cards': [dict(card, project='board-v2') for card in brief['cards']]}
    remaining = board_pressure_lines(filed)
    assert len(remaining) == 1 and 'older than six hours (9)' in remaining[0]
    # Succeeding that card clears it too.
    moved = {'cards': [dict(card, project='board-v2', status='succeeded') for card in brief['cards']]}
    assert board_pressure_lines(moved) == []

    # And it is not a wake reason: the board rules score under the wake line and
    # are marked report-only. The score lives on the rule, not on the sentence.
    from tessera.floor_definition import default_definition, evaluate
    from tessera.floor_signals import board_rows
    hits = evaluate(default_definition(), {'board_card': board_rows(brief)})
    assert hits and all(hit['score'] < 1.0 and hit['wake'] is False for hit in hits)


def test_board_pressure_survives_a_board_it_cannot_read():
    from tessera.autonomy import board_pressure_lines
    # A disabled or unreadable board is silence, not a crash.
    assert board_pressure_lines({}) == []
    assert board_pressure_lines(None) == []
    assert board_pressure_lines({'cards': 'not-a-list'}) == []


def test_infinity_wakes_for_unclaimed_work_in_priority_order(tmp_path):
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=1)
    config.board.enabled = True
    backend = FakeBackend(['Coordination started.'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.board.create('P2. Lower priority', slug, 0, 0, owner='', priority=2)
        manager.board.create('P1. Important fix', slug, 0, 0, owner='', priority=1)
        manager.board.create('P0. Owned elsewhere', slug, 0, 0, owner=slug, priority=0)
        manager.autonomy._queue_floor_wake(clock.now, 1)
        held = manager.autonomy.state['held'][floor]
        assert len(held) == 1 and held[0]['kind'] == 'coordination'
        text = held[0]['text']
        assert text.index('P1. Important fix') < text.index('P2. Lower priority')
        assert 'P0. Owned elsewhere' not in text
        assert 'cheapest capable backend' in text
        manager.autonomy._deliver(clock.now)
        assert manager.wait(floor, 3)
        assert backend.calls
        assert 'P1. Important fix' in str(backend.calls[0]['messages'])


def test_the_floor_is_never_offered_its_own_cards_as_work(tmp_path, monkeypatch):
    manager, worker, clock, _ = open_manager(tmp_path, [], infinity_slug='oversight')
    with manager:
        manager.config.board.enabled = True
        floor = manager.config.autonomy.infinity_slug
        card = {'card_id': 'owned', 'ordinal': 100, 'title': 'Keep oversight',
                'owner': floor, 'status': 'queued', 'version': 1}
        monkeypatch.setattr(manager, 'board_brief', lambda: {'cards': [card]})
        # The same card remains personal work when assigned to a worker.
        card['owner'] = worker
        lines, _ = manager.autonomy._own_board_lines(worker, clock.now)
        assert lines == ['Your board card 100: "Keep oversight" (queued).']
        card['owner'] = floor
        assert manager.autonomy._own_board_lines(floor, clock.now) == ([], {})


def test_the_floor_is_not_a_board_target(tmp_path, monkeypatch):
    manager, worker, _, _ = open_manager(tmp_path, [], infinity_slug='oversight')
    with manager:
        manager.config.board.enabled = True
        floor = manager.autonomy._floor_slug()
        card = {'card_id': 'owned', 'ordinal': 100, 'title': 'Keep oversight',
                'owner': worker, 'status': 'queued', 'version': 1}
        monkeypatch.setattr(manager, 'board_brief', lambda: {'cards': [card]})
        assert manager.autonomy._board_targets() == [worker]
        card['owner'] = floor
        assert manager.autonomy._board_targets() == []


def test_a_floor_owned_card_reaches_the_floor_as_a_dispatch_offer(tmp_path, monkeypatch):
    manager, worker, clock, _ = open_manager(tmp_path, [], infinity_slug='oversight')
    with manager:
        manager.config.board.enabled = True
        floor = manager.config.autonomy.infinity_slug
        cards = [
            {'card_id': 'floor-card', 'ordinal': 100, 'title': 'Repair the lane',
             'owner': floor, 'status': 'queued', 'version': 1},
            {'card_id': 'worker-card', 'ordinal': 101, 'title': 'Worker task',
             'owner': worker, 'status': 'queued', 'version': 1},
            {'card_id': 'unclaimed-card', 'ordinal': 102, 'title': 'Unclaimed task',
             'owner': '', 'status': 'queued', 'version': 1},
        ]
        monkeypatch.setattr(manager, 'board_brief', lambda: {'cards': cards})
        manager.autonomy._queue_floor_wake(clock.now, 0)
        event = manager.autonomy.state['held'][floor][0]
        assert event['kind'] == 'coordination'
        assert ('Dispatch: #100 "Repair the lane" is assigned to the floor lane, not to a worker. '
                'Assign it to a capable worker or steer an owning session; do not execute it here.') in event['text']
        assert 'Your board card' not in event['text']
        assert 'Worker task' not in event['text']
        assert 'Work coordination: #102 Unclaimed task' in event['text']


def test_floor_dispatch_advances_and_rearms_by_card_identity_and_version(tmp_path, monkeypatch):
    manager, _, clock, _ = open_manager(tmp_path, [], board_offer_limit=1)
    with manager:
        manager.config.board.enabled = True
        floor = manager.config.autonomy.infinity_slug
        cards = [dict(card_id=f'card-{i}', ordinal=i, title=f'Task {i}', owner=floor,
                      status='queued', version=1) for i in range(1, 3)]
        monkeypatch.setattr(manager, 'board_brief', lambda: {'cards': cards})

        def wake():
            manager.autonomy.state.setdefault('held', {})[floor] = []
            manager.autonomy._queue_floor_wake(clock.now, 0)
            return manager.autonomy.state['held'].get(floor) or []

        first = wake()
        assert 'Dispatch: #1 ' in first[0]['text']
        assert 'Dispatch: #2 ' not in first[0]['text']
        # Seen state is persisted only after posting; ordinals are not identities.
        saved = json.loads(manager.autonomy.path.read_text())
        assert saved['dispatch_seen'][floor]['card-1']['fp'] == 'queued:1'
        restored = type(manager.autonomy)(manager, clock=clock)
        assert restored.state['dispatch_seen'] == saved['dispatch_seen']
        cards[0]['ordinal'] = 9
        second = wake()
        assert 'Dispatch: #2 ' in second[0]['text']
        assert 'Dispatch: #9 ' not in second[0]['text']
        assert all('Dispatch:' not in event['text'] for event in wake())
        cards[0]['version'] = 2
        assert 'Dispatch: #9 ' in wake()[0]['text']
        clock.now += 6 * 3600 + 1
        assert 'Dispatch: #9 ' in wake()[0]['text']
        manager.config.autonomy.board_offer_limit = 0
        cards[0]['version'] = 3
        assert all('Dispatch:' not in event['text'] for event in wake())
        manager.config.autonomy.board_offer_limit = 1
        manager.config.board.enabled = False
        assert all('Dispatch:' not in event['text'] for event in wake())
        manager.config.board.enabled = True
        cards[0]['status'] = 'succeeded'
        cards[1]['owner'] = 'someone-else'
        assert all('Dispatch:' not in event['text'] for event in wake())


def test_infinity_operator_stop_blocks_coordination_until_human_resume(tmp_path):
    config = config_for(tmp_path, min_wake_gap_seconds=0)
    config.board.enabled = True
    backend = FakeBackend(['must not run'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.board.create('P1. Important fix', slug, 0, 0, owner='')
        manager.interrupt(floor)
        manager.autonomy._queue_floor_wake(clock.now, 0)
        assert not manager.autonomy.state['held'].get(floor)
        assert manager.autonomy.state['suppress'][floor] == 'operator_stopped'
        assert not backend.calls


def test_infinity_observes_and_steers_active_work_without_resuming_stopped_work(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, [], min_wake_gap_seconds=0)
    with manager:
        runtime = manager.runtime(slug)
        runtime.session.log.append('user', 'Stop refining metrics; clear the important cards.')
        runtime.session.log.append('agent', 'Refining another metrics view.')
        with runtime.condition:
            runtime.active = {'id': 'active-proof', 'text': 'Clear important work', 'created': '2026-10-22T01:00:00Z'}
            runtime.session._in_turn.set()
        try:
            rows = manager.coordination_status()
            observed = next(row for row in rows if row['slug'] == slug)
            assert observed['busy']
            assert observed['recent'][-1]['text'] == 'Refining another metrics view.'
            floor = manager.config.autonomy.infinity_slug
            manager.autonomy.state.setdefault('pressure_at', {})[floor] = clock.now - 10
            manager.autonomy._queue_floor_wake(clock.now, 1)
            text = manager.autonomy.state['held'][floor][0]['text']
            assert 'Session pressure: ' + slug in text
            assert 'steer_session' in text and 'rather than becoming the executor' in text
            assert manager.coordinate_steer(slug, 'Return to the important cards.')['applied']
            assert runtime.session.take_unapplied_steers()[0]['text'].startswith('[Autonomy coordination]')
            manager.interrupt(slug)
            assert manager.coordinate_steer(slug, 'Resume')['reason'] == 'operator_stopped'
            assert not runtime.pending
        finally:
            with runtime.condition:
                runtime.active = None
                runtime.session._in_turn.clear()
        assert manager.coordinate_steer(slug, 'Resume')['reason'] == 'operator_stopped'


def test_coordination_offers_advance_through_a_backlog_instead_of_repeating(tmp_path):
    """Eleven unowned cards, three per wake: successive wakes must reach the ninth.

    Offers that sort by a tied title priority and keep no record of what they
    named repeat the first three, and later cards stay unreachable.
    """
    from tessera.autonomy import coordination_offers

    cards = [{'ordinal': ordinal, 'status': 'queued', 'version': 1, 'project': 'alpha',
              'title': f'Card {ordinal}', 'card_id': f'id-{ordinal}'}
             for ordinal in range(68, 79)]
    brief = {'cards': cards}

    first, seen = coordination_offers(brief, 3, {}, now=1_000.0)
    assert [line.split()[2] for line in first] == ['#68', '#69', '#70']

    second, seen2 = coordination_offers(brief, 3, seen, now=1_060.0)
    assert [line.split()[2] for line in second] == ['#71', '#72', '#73']
    # The two wakes are disjoint: nothing is offered twice while the backlog lasts.
    assert not set(first) & set(second)

    third, seen3 = coordination_offers(brief, 3, {**seen, **seen2}, now=1_120.0)
    assert [line.split()[2] for line in third] == ['#74', '#75', '#76']
    # The ninth offer is reachable, which is the whole point.
    assert any('#76' in line for line in third)
    # The record names the card, not the position it happened to occupy.
    assert seen3['id-76']['fp'] == 'queued:1'


def test_an_offered_card_comes_back_after_the_hold_window(tmp_path):
    """A card nobody claimed is not hidden forever; it returns rather than vanishing."""
    from tessera.autonomy import coordination_offers

    brief = {'cards': [{'ordinal': 5, 'status': 'queued', 'version': 2, 'project': 'alpha',
                        'title': 'Only card', 'card_id': 'id-5'}]}
    first, seen = coordination_offers(brief, 3, {}, now=1_000.0)
    assert len(first) == 1
    # Still held inside the window.
    held, _ = coordination_offers(brief, 3, seen, now=1_000.0 + 3600)
    assert held == []
    # Back after six hours, because ignoring work must not make it invisible.
    back, _ = coordination_offers(brief, 3, seen, now=1_000.0 + 6 * 3600 + 1)
    assert len(back) == 1
    # A card that changed re-arms immediately: version is the identity, not age.
    changed = {'cards': [dict(brief['cards'][0], version=3)]}
    rearmed, _ = coordination_offers(changed, 3, seen, now=1_000.0 + 60)
    assert len(rearmed) == 1


def test_a_filed_card_outranks_an_unfiled_card_of_equal_priority(tmp_path):
    """Filing has to mean something at equal priority.

    Equal fallback priority must not leave a filed card indistinguishable from an
    unfiled one. Work a project is waiting on goes first; unfiled work still gets offered.
    """
    from tessera.autonomy import coordination_offers

    brief = {'cards': [
        {'ordinal': 3, 'status': 'queued', 'version': 1, 'project': '', 'title': 'Orphan', 'card_id': 'a'},
        {'ordinal': 9, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Filed work', 'card_id': 'b'},
    ]}
    lines, _ = coordination_offers(brief, 1, {}, now=1_000.0)
    assert len(lines) == 1 and 'Filed work' in lines[0]
    both, _ = coordination_offers(brief, 2, {}, now=1_000.0)
    assert [line.split()[2] for line in both] == ['#9', '#3']
    # A stored priority beats the filing tiebreak. The title pattern is not used once the field is set.
    explicit = {'cards': [
        {'ordinal': 2, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Filed, no priority', 'card_id': 'c'},
        {'ordinal': 11, 'status': 'queued', 'version': 1, 'project': '', 'title': 'P1. Urgent orphan', 'card_id': 'd', 'priority': 1},
    ]}
    ranked, _ = coordination_offers(explicit, 2, {}, now=1_000.0)
    assert 'P1. Urgent orphan' in ranked[0]


def test_a_held_self_prompt_does_not_suppress_coordination(tmp_path):
    """A held event blocks only its own kind.

    An early return on any held self_prompt, coordination, pressure, or reflection
    would silence coordination for the whole hold. A held self_prompt must not do that.
    """
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=1)
    config.board.enabled = True
    backend = FakeBackend(['Coordinated.'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.board.create('Unclaimed work', slug, 0, 0, owner='')
        manager.autonomy.post(floor, 'self_prompt', 'Standing agenda is due.')
        manager.autonomy._queue_floor_wake(clock.now, 0)
        held = manager.autonomy.state['held'][floor]
        assert [event['kind'] for event in held] == ['self_prompt', 'coordination']
        assert 'Unclaimed work' in held[-1]['text']


def test_a_held_coordination_event_still_blocks_a_second_coordination_wake(tmp_path):
    """The same-kind guard is kept: coordination must not stack on itself."""
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=1)
    config.board.enabled = True
    backend = FakeBackend(['must not run'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.board.create('Unclaimed work', slug, 0, 0, owner='')
        manager.autonomy.post(floor, 'coordination', 'Earlier coordination wake.')
        manager.autonomy._queue_floor_wake(clock.now, 0)
        kinds = [event['kind'] for event in manager.autonomy.state['held'][floor]]
        assert kinds == ['coordination']


def test_a_reflection_wake_does_not_stack_on_another_held_floor_event(tmp_path):
    """Reflection asserts nothing else is pending, so it yields to anything held.

    The full suite caught this when the same-kind guard was first relaxed: one floor
    wake arrived as ['self_prompt', 'reflection'], and the reflection text ("no urgent
    signal is currently reported") contradicted the agenda beside it.
    """
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=1)
    backend = FakeBackend(['must not run'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.autonomy.post(floor, 'self_prompt', 'Standing agenda is due.')
        manager.autonomy._queue_floor_wake(clock.now, 0)
        kinds = [event['kind'] for event in manager.autonomy.state['held'][floor]]
        assert kinds == ['self_prompt']
        # With nothing held, reflection still arms: quiet is a valid floor wake.
        manager.autonomy.state['held'][floor] = []
        # _arm_or_post never backdates reflection_at, so the first reflection only
        # arms without posting. Pre-arm it so this turn exercises the posting path.
        manager.autonomy.state.setdefault('reflection_at', {})[floor] = clock.now - 10
        manager.autonomy._queue_floor_wake(clock.now, 0)
        assert [event['kind'] for event in manager.autonomy.state['held'][floor]] == ['reflection']


def test_floor_wake_includes_world_view_after_urgent_pressure(tmp_path, monkeypatch):
    from tessera.autonomy import wake_footer

    manager, _, clock, _ = open_manager(tmp_path, [], world_view='  Email and home matter.  ')

    class _Infra:
        def status(self):
            return {'hosts': [{'ordinal': 1, 'label': 'north', 'health': 'unreachable'}]}

        def stop(self):
            return None

    with manager:
        manager.infrastructure = _Infra()
        monkeypatch.setattr(manager.autonomy, '_thresholds', lambda: {1: {'managed': True}})
        floor = manager.config.autonomy.infinity_slug
        manager.autonomy._queue_floor_wake(clock.now, 0)
        text = manager.autonomy.state['held'][floor][0]['text']
        assert text == '\n'.join([
            wake_footer(manager.config),
            'The body measured pressure. Consider it within the wider situation.',
            'Pressure: north is unreachable. Repair it. Restart the service if that is the fix. Do not replace the kiosk.',
            'What matters to you, in your own words:\nEmail and home matter.',
        ])
        assert 'host-unhealthy-managed' in manager.autonomy.state['last_wake']['rules']


@pytest.mark.parametrize('view', ['', ' \n\t '])
def test_empty_world_view_preserves_floor_wake_exactly(tmp_path, view):
    from tessera.autonomy import wake_footer

    manager, _, clock, _ = open_manager(tmp_path, [], world_view=view)
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.autonomy.state.setdefault('reflection_at', {})[floor] = clock.now - 10
        manager.autonomy._queue_floor_wake(clock.now, 0)
        text = manager.autonomy.state['held'][floor][0]['text']
        assert 'What matters to you, in your own words:' not in text
        assert text == wake_footer(manager.config) + '\n' + (
            'No urgent signal is currently reported. Review available observations and the wider picture, '
            'then choose how to spend this time. Your own interests, reflection and rest are valid choices. '
            'A fabricated emergency or compulsory work report is unnecessary.')


@pytest.mark.parametrize('length', [2000, 2001, 10000])
def test_floor_wake_bounds_world_view(tmp_path, length):
    manager, _, clock, _ = open_manager(tmp_path, [], world_view='x' * length)
    with manager:
        floor = manager.config.autonomy.infinity_slug
        manager.autonomy.state.setdefault('reflection_at', {})[floor] = clock.now - 10
        manager.autonomy._queue_floor_wake(clock.now, 0)
        text = manager.autonomy.state['held'][floor][0]['text']
        view = text.split('What matters to you, in your own words:\n', 1)[1].split('\n', 1)[0]
        assert len(view) == 2000
        if length > 2000:
            assert view == 'x' * 1999 + '…'
        else:
            assert view == 'x' * 2000


def test_wake_footer_carries_budget_discipline():
    from tessera.autonomy import wake_footer
    footer = wake_footer(Config())
    assert "Budgets are evidence" in footer
    assert "never set arbitrary tool-call ceilings" in footer
    assert "escalate to an outside agent" in footer


def test_wake_footer_names_this_install_and_nobody_else():
    """A fresh config says "the operator" and the default agent name.

    A configured install says its own names.
    """
    from tessera.autonomy import wake_footer
    generic = wake_footer(Config())
    assert "the operator" in generic
    assert "Ada" not in generic
    configured = wake_footer(Config(agent_name="Bubba", operator_name="Ada"))
    assert "Bubba is the continuing autonomy thread" in configured
    assert "Ada’s intent" in configured


def test_a_running_card_whose_session_is_gone_is_pressure_not_silence():
    """A card that reads running while nobody holds it is pressure, not silence.

    `BoardStore.start_card` is the only writer of `running`, and only the executing
    session moves the card off it. A session that ends mid-work therefore leaves the
    card running, and the pressure reader has to say so. Skipping `running` outright
    would hide that card from the floor and the page.
    """
    from tessera.autonomy import board_pressure_lines

    brief = {'cards': [
        {'ordinal': 4, 'status': 'running', 'project': 'board-v2',
         'execution': {'session': 'operations', 'since': 100.0, 'receipt': 'sweeping'}},
        {'ordinal': 6, 'status': 'running', 'project': 'board-v2',
         'execution': {'session': 'retired-session', 'since': 100.0, 'receipt': 'work'}},
        # A running card with no execution record at all: the state exists but the
        # evidence that anyone is working it does not.
        {'ordinal': 8, 'status': 'running', 'project': 'board-v2', 'execution': None},
    ], 'sessions': ['operations', 'infinity']}

    lines = board_pressure_lines(brief)
    assert len(lines) == 1
    assert lines[0].startswith('Pressure: 2 board card(s) read running but the session working them is gone (6, 8)')
    assert 'Release or take them over' in lines[0]
    # The card a live session holds is genuinely in flight, so it is not pressure.
    assert '4,' not in lines[0] and '(4' not in lines[0]

    # Taking over an abandoned card clears its line, because the fact changed.
    taken = {'cards': [dict(card, execution={'session': 'infinity', 'since': 200.0, 'receipt': ''})
                       for card in brief['cards']], 'sessions': ['operations', 'infinity']}
    assert board_pressure_lines(taken) == []

    # Silence, not a guess, when the brief carries no session list: without it a
    # `running` card cannot be judged, and inventing a verdict would be a new lie.
    blind = {'cards': brief['cards']}
    assert board_pressure_lines(blind) == []

    # And it stays a report, never a wake reason.
    from tessera.floor_definition import default_definition
    rule = next(item for item in default_definition()['rules'] if item['id'] == 'board-abandoned')
    assert rule['wake'] is False and rule['score'] < default_definition()['compose']['wake_score']
