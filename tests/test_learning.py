import json
import os

from test_autonomy import deliver, messages, open_manager
from tessera.learning import LearningStore, intention_lines


def test_empty_agenda_does_not_wake(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0, standing_floor=False)
    with manager:
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        assert manager.autonomy.state['self_prompt_at'] == {}
        assert manager.autonomy.status()['learning']['open_lessons'] == 0


def test_a_session_wakes_for_its_own_board_card(tmp_path):
    """A session wakes for its own cards and nothing else."""
    from test_autonomy import config_for
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    clock = None
    backend = FakeBackend(['Taking the board card.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    from test_autonomy import Clock, install_fake_clock
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Shared board rewrite', 'home', 0, 0, note='Plan: one board for every session',
                             owner='')
        # The session takes the card itself; that is what makes it the session's work.
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert 'Your board card 1: "Shared board rewrite" (queued).' in wake.text
        assert 'Plan: one board for every session' in wake.text
        # The same card on the next wake is not offered again.
        clock.now += 1
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []


def test_another_sessions_card_is_visible_but_never_wakes_this_one(tmp_path):
    """Cards from another session are viewable so work can be coordinated, but
    awareness is not a call to act. A session only wakes for its own."""
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    manager = SessionManager(config, backend_factory=lambda slug, name: FakeBackend([]))
    clock = Clock()
    install_fake_clock(manager, clock)
    home = manager.create('Home')['slug']
    operations = manager.create('Operations')['slug']
    with manager:
        manager.board.create("Operations' work", operations, 0, 0, owner=operations)
        clock.now += 1
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(home, []) == []
        # The first tick arms the gap; the second posts and delivers in the same pass.
        clock.now += 1
        manager.autonomy.tick()
        assert manager.wait(operations, timeout=10)
        own = [item for item in messages(manager, operations) if item.meta.get('wake')]
        assert own and 'Your board card 1' in own[0].text
        # The card is still readable, so overlap is visible without a wake.
        view = manager.board_brief()
        assert any(card['title'] == "Operations' work" for card in view['cards'])
        assert 'Your board card' not in ' '.join(
            item.text for item in messages(manager, home))


def test_an_unclaimed_card_wakes_nobody(tmp_path):
    """An unclaimed card belongs to no session, so no session is woken for it."""
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    manager = SessionManager(config, backend_factory=lambda slug, name: FakeBackend([]))
    clock = Clock()
    install_fake_clock(manager, clock)
    home = manager.create('Home')['slug']
    with manager:
        manager.board.create('Nobody owns this yet', 'home', 0, 0, owner='')
        clock.now += 1
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(home, []) == []
        assert manager.autonomy.state['self_prompt_at'] == {}


def test_the_pressure_page_marks_coordination_work_the_floor_is_holding_back(tmp_path):
    """The page may show work the floor is not acting on, but it must say so.

    The page reads the live inventory with no seen record. The wake passes its
    seen record. Those two disagree whenever a card is inside the hold window.
    Keep the live inventory, because hiding unclaimed work from the person
    reading the board is worse, and flag the held-back entries so the page
    never implies the floor is acting on something it is deliberately not.
    """
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from tessera.autonomy import coordination_offers
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=True)
    config.board.enabled = True
    manager = SessionManager(config, backend_factory=lambda slug, name: FakeBackend([]))
    clock = Clock()
    install_fake_clock(manager, clock)
    with manager:
        manager.board.create('Nobody has claimed this', 'home', 0, 0, owner='')
        brief = manager.board_brief()

        # Before any offer, the card is inventory and the floor would act on it.
        report = manager.autonomy.pressure_report()
        coordination = [item for item in report['items']
                        if item['text'].startswith('Work coordination:')]
        assert len(coordination) == 1
        assert coordination[0]['held_back'] is False

        # Record the offer exactly as the floor wake does, then read the page again
        # inside the hold window. The line must survive, flagged.
        _, seen = coordination_offers(brief, config.autonomy.board_offer_limit, {},
                                      now=clock.now)
        assert seen, 'the offers function must record what it offered'
        manager.autonomy.state['coordination_seen'] = {'infinity': seen}
        report = manager.autonomy.pressure_report()
        coordination = [item for item in report['items']
                        if item['text'].startswith('Work coordination:')]
        assert len(coordination) == 1, 'held work must not vanish from the page'
        assert coordination[0]['held_back'] is True

        # And the two readings really are different now, which is the defect this
        # test exists for: the floor offers nothing, the page still shows the card.
        offered, _ = coordination_offers(brief, config.autonomy.board_offer_limit,
                                         seen, now=clock.now)
        assert offered == []


def test_the_pressure_report_names_what_the_floor_wake_would_act_on(tmp_path):
    """The board may only show a pressure the floor would actually wake for."""
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=True)
    manager = SessionManager(config, backend_factory=lambda slug, name: FakeBackend([]))
    clock = Clock()
    install_fake_clock(manager, clock)
    with manager:
        report = manager.autonomy.pressure_report()
        assert report['steady'] is True and report['items'] == []
        # A host that was healthy and is now unreachable is a measured change.
        manager.autonomy.state['pressure_seen'] = {'node-a': {'health': 'healthy', 'disk_low': False}}
        manager.infrastructure.status = lambda: {'hosts': [
            {'label': 'node-a', 'health': 'unreachable', 'metrics': {}}]}
        report = manager.autonomy.pressure_report()
        assert report['steady'] is False
        assert report['items'][0]['score'] >= 1.5
        assert 'node-a' in report['items'][0]['text']
        assert report['learning']['open_lessons'] == 0


def test_open_lesson_wakes_with_the_agenda_after_the_gap_arms(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Looking at the lesson.'], self_prompt_gap_seconds=0)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        assert slug in manager.autonomy.state['self_prompt_at']
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wake) == 1
        assert wake[0].meta['wake']['events'] == ['self_prompt']
        assert lesson['id'] in wake[0].text
        assert 'rebuild the desk helper' in wake[0].text
        assert 'body measured pressure' not in wake[0].text
        assert 'Standing agenda is due.' in wake[0].text


def test_timer_wake_carries_an_open_lesson(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Noted.'], self_prompt_gap_seconds=99999)
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        manager.autonomy.post(slug, 'timer', 'Timer t-1 "deploy" is due.')
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert '[agenda] Lesson' in wake.text
        assert 'rebuild the desk helper' in wake.text


def test_bound_skill_miss_then_revision_then_pass(tmp_path):
    store = LearningStore(tmp_path / 'lessons.json', pass_score=1.0, fail_limit=2, clock=lambda: 100.0)
    lesson = store.record_lesson('home', 'rebuild the desk helper', 'missing binary', 'binary exists', skill='desk-helper')
    store.observe_turn('home', 'Please rebuild the desk helper now', [], None, True, False)
    missed = store.list_lessons('home')[0]
    assert missed['fails'] == 1
    assert missed['status'] == 'written'
    store.observe_turn('home', 'rebuild the desk helper again', [], 0.0, False, False)
    revised = store.list_lessons('home')[0]
    assert revised['status'] == 'needs_revision'
    assert 'Revise the skill' in store.agenda_lines('home')[0]
    store.note_skill_written('home', 'desk-helper')
    reset = store.list_lessons('home')[0]
    assert reset['fails'] == 0
    assert reset['status'] == 'written'
    store.observe_turn('home', 'rebuild the desk helper', [
        {'name': 'skill_load', 'success': True, 'arguments': {'name': 'desk-helper'}},
    ], 1.0, True, False)
    assert store.list_lessons('home') == []
    saved = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))
    assert saved['lessons'][lesson['id']]['status'] == 'absorbed'


def test_matching_user_turn_injects_the_lesson_and_records_a_miss(tmp_path):
    manager, slug, _, backend = open_manager(tmp_path, ['I loaded the skill.'], self_prompt_gap_seconds=99999)
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'missing binary', 'binary exists', skill='desk-helper')
        manager.submit(slug, 'Please rebuild the desk helper')
        assert manager.wait(slug, timeout=10)
        blob = json.dumps(backend.calls[-1]['messages'])
        assert 'Open lesson' in blob
        assert 'Load skill desk-helper' in blob
        assert manager.autonomy.learning.list_lessons(slug)[0]['fails'] == 1


def test_intention_lines_skip_closed_items():
    payload = {'intentions': [
        {'title': 'fix the wake composer', 'status': 'open'},
        {'title': 'old cutover', 'status': 'satisfied'},
    ]}
    assert intention_lines(payload) == ['Open intention: fix the wake composer']
    assert intention_lines('not json') == []


def test_committed_intention_survives_restart_until_abandoned(tmp_path):
    path = tmp_path / 'lessons.json'
    store = LearningStore(path, clock=lambda: 10.0)
    committed = store.commit_intention('home', 'finish the desk helper')
    again = store.commit_intention('home', 'Finish the desk helper')
    assert again['id'] == committed['id']
    reloaded = LearningStore(path, clock=lambda: 20.0)
    assert reloaded.list_intentions('home')[0]['id'] == committed['id']
    assert 'Committed until done' in reloaded.agenda_lines('home')[0]
    closed = reloaded.abandon_intention('home', committed['id'], 'done')
    assert closed['status'] == 'done'
    assert closed['abandon'] == 'done'
    assert reloaded.agenda_lines('home') == []
    try:
        reloaded.abandon_intention('home', committed['id'], 'bored')
        raise AssertionError('bad condition should fail')
    except ValueError as error:
        assert 'done, failed, or dropped' in str(error)


def test_committed_intention_wakes_after_the_gap(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Looking at the intention.'], self_prompt_gap_seconds=0)
    with manager:
        intention = manager.autonomy.learning.commit_intention(slug, 'finish the desk helper')
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wake) == 1
        assert intention['id'] in wake[0].text
        assert 'finish the desk helper' in wake[0].text
        manager.autonomy.learning.abandon_intention(slug, intention['id'], 'dropped')
        clock.now += 5
        manager.autonomy.tick()
        later = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(later) == 1


def test_engaged_suppresses_a_due_agenda(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['quiet'], self_prompt_gap_seconds=0, engaged_message_seconds=300)
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        manager.autonomy.activity(slug, 'message')
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        assert manager.autonomy.state['suppress'][slug].startswith('engaged:')


def test_hold_suppresses_a_due_agenda(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['quiet'], self_prompt_gap_seconds=0, min_wake_gap_seconds=3600)
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        manager.autonomy.state['wakes'].append({
            'slug': slug, 'at': clock.now, 'turn_id': 't-hold', 'kinds': ['timer'], 'spoke': False,
        })
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        assert manager.autonomy.state['suppress'][slug].startswith('hold:')


def test_answerable_file_check_absorbs_without_a_model(tmp_path):
    helper = tmp_path / 'helper'
    helper.write_text('ok', encoding='utf-8')
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0, standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists',
            evidence_path=str(helper))
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.learning.list_lessons(slug) == []
        assert manager.autonomy.state['held'].get(slug, []) == []
        saved = json.loads((tmp_path / 'learning' / 'lessons.json').read_text(encoding='utf-8'))
        assert saved['lessons'][lesson['id']]['status'] == 'absorbed'


def test_missing_house_snapshot_allows_reflection_without_inventing_health(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Resting.'], self_prompt_gap_seconds=0)
    with manager:
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        infinity = manager.autonomy.settings.infinity_slug
        manager.autonomy.tick()
        wake = [item for item in messages(manager, infinity) if item.meta.get('wake')]
        assert manager.wait(infinity, timeout=10)
        wake = [item for item in messages(manager, infinity) if item.meta.get('wake')]
        assert wake and wake[0].meta['wake']['events'] == ['reflection']
        assert 'No urgent signal is currently reported' in wake[0].text


def test_specific_agenda_wins_over_the_floor(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['Looking at the lesson.'], self_prompt_gap_seconds=0)
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert 'rebuild the desk helper' in wake.text
        assert 'checkable thing' not in wake.text
        assert 'body measured pressure' not in wake.text


def test_session_agenda_and_infinity_pressure_stay_apart(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['Looking at the lesson.', 'The house moved.'],
        self_prompt_gap_seconds=0, min_wake_gap_seconds=0)
    floor = manager.create('Floor')['slug']
    manager.autonomy.settings.infinity_slug = floor
    readings = {'health': 'healthy'}
    manager.infrastructure.status = lambda: {
        'hosts': [{'label': 'Kiosk', 'ordinal': 1, 'health': readings['health']}]
    }
    with manager:
        manager.autonomy.learning.record_lesson(
            slug, 'Finish the phone thread.', 'The phone thread is unfinished.', 'The next turn says it is done.')
        manager.autonomy.tick()
        readings['health'] = 'unreachable'
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10)
        assert manager.wait(floor, timeout=10)
        session_wake = [item for item in messages(manager, slug) if item.meta.get('wake')]
        floor_wake = [item for item in messages(manager, floor) if item.meta.get('wake')]
        assert len(session_wake) == 1
        assert session_wake[0].meta['wake']['events'] == ['self_prompt']
        assert 'Standing agenda is due.' in session_wake[0].text
        assert 'body measured pressure' not in session_wake[0].text
        assert session_wake[0].meta.get('source') != 'voice'
        assert len(floor_wake) == 1
        assert floor_wake[0].meta['wake']['events'] == ['pressure']
        assert 'body measured pressure' in floor_wake[0].text
        assert 'Standing agenda is due.' not in floor_wake[0].text
        assert 'phone thread' not in floor_wake[0].text


def test_engaged_suppresses_the_floor(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['quiet'], self_prompt_gap_seconds=0, engaged_message_seconds=300)
    with manager:
        infinity = manager.autonomy.settings.infinity_slug
        manager.autonomy.activity(infinity, 'message')
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(infinity, []) == []
        assert manager.autonomy.state['suppress'][infinity] == 'engaged:message'


def test_floor_wakes_the_infinity_thread(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['Resting.', 'Still here.'], self_prompt_gap_seconds=0,
        engaged_focus_seconds=0, min_wake_gap_seconds=0)
    with manager:
        manager.autonomy.state['presence'][slug] = {'last_seen': clock.now, 'present': True, 'last_event': 0}
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        infinity = manager.autonomy.settings.infinity_slug
        deliver(manager, infinity)
        infinity_wakes = [item for item in messages(manager, infinity) if item.meta.get('wake')]
        home_wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(infinity_wakes) == 1
        assert infinity_wakes[0].meta['wake']['events'] == ['reflection']
        assert 'choose your own meaningful pursuits' in infinity_wakes[0].text
        assert home_wakes == []
        first_id = manager._metadata[infinity]['id']
        runtime = manager.runtime(infinity)
        assert runtime.session.session_id == first_id
        assert runtime.session.capture.session_id == first_id
        clock.now += 1
        deliver(manager, infinity)
        later = [item for item in messages(manager, infinity) if item.meta.get('wake')]
        assert len(later) == 2
        assert manager._metadata[infinity]['id'] == first_id


def test_a_finished_but_not_perfect_score_does_not_open_a_lesson(tmp_path):
    store = LearningStore(tmp_path / 'lessons.json', clock=lambda: 100.0)
    opened = store.observe_turn(
        'home', 'lets go ahead and merge', [], 0.9, True, False)
    assert opened is None
    assert store.list_lessons('home') == []
    failed = store.observe_turn(
        'home', 'lets go ahead and merge the other one', [], 0.4, True, False)
    assert failed is not None
    assert 'below the check' in failed['failure']
    assert failed['skill'] == 'compare-before-adopt'


def test_judge_failure_pushes_the_skill_on_the_session_wake_only(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['Running the comparison.'], self_prompt_gap_seconds=0,
        min_wake_gap_seconds=0, standing_floor=False)
    floor = manager.create('Floor')['slug']
    manager.autonomy.learning.floor_slug = floor
    with manager:
        opened = manager.autonomy.learning.observe_turn(
            slug, 'rebuild the desk helper now', [], 0.2, False, False)
        assert opened is not None
        assert opened['skill'] == 'compare-before-adopt'
        floor_lesson = manager.autonomy.learning.observe_turn(
            floor, 'rebuild the desk helper on the floor', [], 0.2, False, False)
        assert floor_lesson is not None
        assert floor_lesson['skill'] == ''
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert 'Load skill compare-before-adopt before acting.' in wake.text
        floor_text = ' '.join(item.text for item in messages(manager, floor) if item.meta.get('wake'))
        assert 'compare-before-adopt' not in floor_text


def test_retire_stops_the_agenda(tmp_path):
    manager, slug, clock, _ = open_manager(tmp_path, ['resting'], self_prompt_gap_seconds=0, standing_floor=False)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'missing binary', 'binary exists')
        manager.autonomy.learning.check(slug, lesson['id'], False, retire=True)
        clock.now += 5
        manager.autonomy.tick()
        clock.now += 5
        manager.autonomy.tick()
        assert manager.autonomy.state['held'].get(slug, []) == []
        assert manager.autonomy.learning.agenda_lines(slug) == []


def test_a_judged_session_stays_quiet_until_its_log_moves(tmp_path):
    from tessera.autonomy import load_judged, record_judgment, weather_lines
    session = tmp_path / 'sessions' / 'operations' / 'log'
    session.mkdir(parents=True)
    log = session / '2026-10-17.jsonl'
    log.write_text('{}\n', encoding='utf-8')
    stamp = log.stat().st_mtime
    now = stamp + 7200
    first = weather_lines(tmp_path, now)
    assert any('operations' in line and line.startswith('Weather:') for line in first)
    record_judgment(tmp_path, 'session:operations', verdict='finished', log_mtime=stamp)
    second = weather_lines(tmp_path, now + 10, load_judged(tmp_path))
    assert not any(line.startswith('Weather:') and 'operations' in line for line in second)
    assert any('operations finished' in line for line in second)
    log.write_text('{}\n{}\n', encoding='utf-8')
    # A second write in the same second does not move st_mtime on a
    # one-second filesystem, so the judgment still matches and the session
    # stays quiet. Advance the stamp; the product compares the two mtimes.
    os.utime(log, (stamp + 1, stamp + 1))
    moved = log.stat().st_mtime
    assert moved != stamp
    third = weather_lines(tmp_path, moved + 7200, load_judged(tmp_path))
    assert any(line.startswith('Weather:') and 'operations' in line for line in third)


def test_a_named_load_stays_quiet_for_six_hours(tmp_path, monkeypatch):
    from tessera import autonomy as autonomy_module
    from tessera.autonomy import load_judged, record_judgment, weather_lines
    monkeypatch.setattr(autonomy_module, 'collect_metrics', lambda home: {'cpu_load1': 12.0, 'cpu_count': 8})
    record_judgment(tmp_path, 'load', name='Spotlight')
    judged = load_judged(tmp_path)
    judged['load']['at'] = 1000.0
    (tmp_path / 'learning' / 'weather-judged.json').write_text(
        json.dumps({'facts': judged}), encoding='utf-8')
    lines = weather_lines(tmp_path, 1000.0 + 3600, load_judged(tmp_path))
    assert any('Already judged load as Spotlight' in line for line in lines)
    assert not any('Find what is using it' in line for line in lines)


def test_floor_carries_the_last_chat_turn(tmp_path):
    from tessera.autonomy import recent_chat
    manager, slug, clock, _ = open_manager(tmp_path, ['The cache is gone.'], self_prompt_gap_seconds=0)
    with manager:
        infinity = manager.autonomy.settings.infinity_slug
        manager.submit(infinity, 'What did the last wake change?')
        assert manager.wait(infinity, timeout=10)
        carried = recent_chat(manager.config.home, infinity)
        assert any('The cache is gone.' in line for line in carried)
        assert any('What did the last wake change?' in line for line in carried)


def test_steady_house_does_not_wake_and_a_change_does(tmp_path):
    from tessera.autonomy import pressure_lines, pressure_seen
    steady = {'hosts': [{'label': 'node-a', 'ordinal': 1, 'health': 'healthy',
                         'metrics': {'disk_free_bytes': 50, 'disk_total_bytes': 100}}]}
    assert pressure_lines(steady, {}, 1) == ['Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']
    seen = pressure_seen(steady, 1)
    down = {'hosts': [{'label': 'node-a', 'ordinal': 1, 'health': 'unreachable',
                       'metrics': {'disk_free_bytes': 50, 'disk_total_bytes': 100}}]}
    changed = pressure_lines(down, seen, 2)
    assert any('node-a changed healthy -> unreachable' in line for line in changed)
    again = pressure_lines(down, pressure_seen(down, 2), 3)
    assert again == ['Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']


def test_an_unchanged_lesson_stays_quiet_until_the_check_changes(tmp_path):
    manager, slug, clock, _ = open_manager(
        tmp_path, ['Looking.', 'Still looking.'], self_prompt_gap_seconds=0,
        standing_floor=False, min_wake_gap_seconds=0)
    with manager:
        lesson = manager.autonomy.learning.record_lesson(
            slug, 'rebuild the desk helper', 'the helper was not compiled', 'the helper binary exists')
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        clock.now += 1
        manager.autonomy.tick()
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wakes) == 1
        assert manager.autonomy.state['suppress'][slug] == 'agenda_unchanged'
        manager.autonomy.learning.check(slug, lesson['id'], False)
        clock.now += 1
        # deliver(), not a bare tick(). tick() enqueues the wake and returns
        # before the session worker appends it to the chat log, so reading
        # messages() straight after a tick() can see one wake while the
        # delivery ledger already holds two. deliver() ticks and then waits for
        # the worker, which is why every other wake in this test uses it.
        deliver(manager, slug)
        later = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(later) == 2


def test_salience_names_the_highest_change_and_drops_churn():
    """Scores are the numbers on the rules. A health change outranks disk, and board work stays under the wake line."""
    from tessera.autonomy import pressure_lines, rank_pressure
    from tessera.floor_definition import default_definition
    definition = default_definition()
    scores = {rule['id']: rule['score'] for rule in definition['rules']}
    assert scores['host-changed-unreachable-unmanaged'] > scores['host-disk'] > scores['board-unfiled']
    assert scores['board-unfiled'] < definition['compose']['wake_score']
    down = pressure_lines(
        {'hosts': [{'label': 'node-a', 'ordinal': 1, 'health': 'unreachable'}]},
        {'node-a': {'health': 'healthy', 'disk_low': False}}, 2)
    disk = pressure_lines(
        {'hosts': [{'label': 'Mac', 'ordinal': 1, 'health': 'healthy',
                    'metrics': {'disk_free_bytes': 4, 'disk_total_bytes': 100}}]}, {}, 1)
    change = next(line for line in down if 'changed' in line)
    disk_line = next(line for line in disk if 'disk free' in line)
    ranked = rank_pressure([
        (scores['host-disk'], disk_line),
        (scores['board-unfiled'], 'Pressure: energy metric moved. Ignore it.'),
        (scores['host-changed-unreachable-unmanaged'], change),
    ])
    assert ranked[0][1] == change
    assert scores['board-unfiled'] < 1.0


def test_the_shipped_kiosk_rule_repairs_a_kiosk_label_and_can_be_deleted():
    """The kiosk distinction is a shipped rule. A declared false flag still wins."""
    from tessera.autonomy import pressure_lines
    from tessera.floor_definition import default_definition
    report = {'hosts': [{'label': 'Kiosk', 'ordinal': 5, 'health': 'unreachable'}]}
    lines = pressure_lines(report, {}, 1)
    assert any('Repair it' in line and 'Kiosk' in line for line in lines)
    plain = pressure_lines({'hosts': [{'label': 'bench', 'ordinal': 5, 'health': 'unreachable'}]}, {}, 1)
    assert any('Name it. Do not invent a repair.' in line for line in plain)
    assert not any('Repair it' in line for line in plain)
    declared_off = pressure_lines(report, {}, 1, thresholds={5: {'managed': False}})
    assert any('Name it. Do not invent a repair.' in line for line in declared_off)
    assert not any('Repair it' in line for line in declared_off)
    definition = default_definition()
    definition['rules'] = [rule for rule in definition['rules'] if 'label_lower' not in rule['when']]
    assert pressure_lines(report, {}, 1, definition=definition) == [
        'Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']
    rule = next(item for item in default_definition()['rules'] if item['id'] == 'host-unhealthy-managed')
    assert rule['score'] >= default_definition()['compose']['wake_score']
    assert rule['pressure'] is True


def test_unsure_conscience_does_not_block(monkeypatch):
    from tessera.cua_jev import conscience
    monkeypatch.setattr('tessera.cua_jev.evaluate', lambda *args: {'answers_pressure': {'type': 'noul', 'noul': 0.5}})
    result = conscience('Salience 1.8. Pressure: node-a changed.', 'shell', {'command': 'uptime'}, None, 'key')
    assert result['verdict'] == 'abstain'
    monkeypatch.setattr('tessera.cua_jev.evaluate', lambda *args: (_ for _ in ()).throw(ValueError('down')))
    failed = conscience('Salience 1.8. Pressure: node-a changed.', 'shell', {'command': 'uptime'}, None, 'key')
    assert failed['verdict'] == 'abstain'


def test_a_parked_question_does_not_wake_the_same_session_twice(tmp_path):
    """The loop that made harness-development repeat itself every turn.

    Lessons were deduplicated through agenda_seen. Questions and intentions were not, so
    one parked question kept the agenda non-empty forever and the session woke on every
    gap tick carrying the same text, redid the same work, and read as stuck.
    """
    manager, slug, clock, _ = open_manager(tmp_path, ['quiet'], self_prompt_gap_seconds=0, standing_floor=False)
    with manager:
        question = manager.autonomy.learning.park_question(slug, 'router polish items, for later')
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wake) == 1
        assert question['id'] in wake[0].text
        # Second tick, nothing changed: no second wake carrying the same question.
        clock.now += 5
        manager.autonomy.tick()
        again = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(again) == 1
        assert manager.autonomy.state['suppress'][slug] == 'agenda_unchanged'
        # An edited question re-arms it, so a real change still reaches the session.
        manager.autonomy.learning._questions[question['id']]['text'] = 'router polish items, now urgent'
        manager.autonomy.learning._save()
        clock.now += 120
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        third = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(third) == 2
        assert 'now urgent' in third[1].text


def test_a_claimed_card_does_not_re_arm_every_minute(tmp_path):
    """The offer fingerprint used age//60, so an unchanged card changed bucket every
    minute and every wake offered it again forever. Identity is the card's version."""
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Taking the board card.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Shared board rewrite', 'home', 0, 0, owner=slug)
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        assert len([i for i in messages(manager, slug) if i.meta.get('wake')]) == 1
        # Two minutes later the card is unchanged: still not re-offered.
        clock.now += 120
        manager.autonomy.tick()
        assert len([i for i in messages(manager, slug) if i.meta.get('wake')]) == 1


def test_a_board_offer_quotes_the_newest_evidence_not_the_opening_line(tmp_path):
    """A wake kept asserting a voice handoff fault long after that stopped being true.

    append_note() puts each new line at the end of the note and trims oldest-first
    when the field fills, so the newest evidence is always the tail. The offer
    quoted note[:300] — the opening statement, which a long-lived card outgrows
    almost immediately. A session was therefore re-woken for its own card and told
    the state the card had at creation, not the state it has now.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, deliver, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Taking the board card.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0,
                             note="The floor session still isn't firing with autonomy anymore.",
                             owner='')
        card_id = manager.board_brief()['cards'][0]['card_id']
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        # Long enough that the opening line is far outside any short window.
        for index in range(12):
            manager.board.append_note(
                card_id,
                'FINDING %02d: verified evidence line %02d, padded so the note runs well past the offer window.' % (index, index),
                actor=slug)
        manager.board.append_note(card_id, 'LATEST: the coordination repair is verified and deployed.', actor=slug)
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert 'LATEST: the coordination repair is verified and deployed.' in wake.text
        assert "isn't firing with autonomy anymore" not in wake.text


def test_a_blocked_offer_names_the_question_it_is_waiting_on(tmp_path):
    """A card parked on a person is not work the session can advance.

    The offer said "(blocked)" and quoted the note, so a session re-woke for its
    own card with no way to see that the thing it owed was an answer from the operator.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, deliver, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Waiting on the operator.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='')
        card_id = manager.board_brief()['cards'][0]['card_id']
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.board.ask_human(card_id, 'Close the card on coordination evidence, or hold it open until the core reloads?',
                                actor=slug)
        clock.now += 1
        manager.autonomy.tick()
        clock.now += 1
        deliver(manager, slug)
        wake = [item for item in messages(manager, slug) if item.meta.get('wake')][0]
        assert 'Close the card on coordination evidence' in wake.text


def test_a_held_agenda_does_not_quote_a_status_the_card_has_left(tmp_path):
    """A held agenda must not quote a status the card has left.

    A self_prompt is composed once, stored as frozen text, and delivered
    whenever the wake caps allow. Nothing re-reads the board between compose
    and delivery unless delivery refreshes the card line, so the agenda would
    otherwise assert a state the board has left.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Reading the card as it is now.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='')
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.board_move(1, manager.board_brief()['revision'], 'blocked')
        # A lesson keeps the agenda warranted even after the card settles, so
        # this measures the card line being corrected rather than the whole
        # wake being suppressed.
        manager.autonomy.learning.record_lesson(
            slug, 'The tile would not render until a reload.', 'It rendered without one.',
            'The tile renders on the running core.')
        # Compose the agenda exactly as _queue_self_prompts does, while the card
        # is blocked. This text is now frozen in state['held'].
        text_lines = manager.autonomy.learning.agenda_lines(slug)
        board_lines, _ = manager.autonomy._own_board_lines(slug, clock.now)
        assert any('(blocked)' in line for line in board_lines)
        body = 'Standing agenda is due.\n' + '\n'.join(text_lines + board_lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert '(blocked)' in manager.autonomy.state['held'][slug][0]['text']
        # The card is closed before the wake is delivered.
        manager.board_move(1, manager.board_brief()['revision'], 'succeeded')
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10)
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wakes) == 1, f'expected the held agenda to be delivered; wakes={len(wakes)}'
        assert 'The tile would not render until a reload.' in wakes[-1].text
        assert '(blocked)' not in wakes[-1].text, (
            f'held agenda quoted a status the card had left: {wakes[-1].text!r}')


def test_a_held_agenda_that_only_named_settled_work_does_not_wake(tmp_path):
    """The other half of the same fault: a wake whose only content was a card
    that has since settled should not be sent at all. Waking a session to tell
    it about work that is already closed costs a turn and teaches it nothing.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Should not be asked anything.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='')
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.board_move(1, manager.board_brief()['revision'], 'blocked')
        board_lines, _ = manager.autonomy._own_board_lines(slug, clock.now)
        body = 'Standing agenda is due.\n' + '\n'.join(board_lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert manager.autonomy.state['held'][slug], 'agenda was never armed'
        # The card settles before delivery, leaving nothing still true to say.
        manager.board_move(1, manager.board_brief()['revision'], 'succeeded')
        manager.autonomy.tick()
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert wakes == [], (
            f'a wake was sent for work that had settled: {wakes[0].text!r}')


def test_a_board_offer_is_one_physical_line_whatever_the_note_holds(tmp_path):
    """A board offer is one physical line, whatever the note holds.

    _note_excerpt quotes the end of a multi-line markdown note, so it can
    return text containing newlines. Embedding that inline splits one card
    line into several physical lines in the composed agenda.

    _refresh_agenda classifies agenda lines by prefix. It rebuilds lines that
    start with 'Your board card ' and preserves every other line verbatim, so
    a continuation would be kept as static text and emitted above the rebuilt
    card line. A fragment must not ride in as current agenda content.
    """
    from tessera.autonomy import _board_offer_line

    # Long enough that the opening line falls outside the 300-char excerpt.
    padding = '\n'.join('FILLER %02d: a line of note text that occupies space in the note body.' % i
                        for i in range(12))
    note = ('EARLIEST: the card as it was created.\n' + padding + '\n'
            'LATEST: this is the newest line, and it ends with a second\n'
            'line that wraps inside the excerpt window.')
    card = {'ordinal': 56, 'status': 'blocked', 'owner': 'infinity',
            'title': 'A held agenda quotes a card status the board has left',
            'note': note,
            'questions': [{'pending': True, 'text': 'Does this line\nwrap too?'}]}
    line = _board_offer_line(card)
    assert '\n' not in line, f'board offer spans physical lines: {line!r}'
    assert 'LATEST: this is the newest line' in line
    assert 'EARLIEST' not in line, 'the excerpt should quote the tail, not the opening'
    # The wrapped continuation survives as words, just not as a new line.
    assert 'line that wraps inside the excerpt window.' in line
    assert 'Does this line wrap too?' in line


def test_a_held_agenda_delivers_no_stale_fragment_when_the_note_wrapped(tmp_path):
    """A held agenda delivers no stale fragment when the note wrapped.

    Composed while the card said blocked and its note ended mid-line; delivered
    after the card moved to queued and the note grew well past the excerpt
    window, so the compose-time tail is genuinely gone. Nothing from compose
    time may survive except the genuinely static lesson line, and the card line
    must still be a single line.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Reading the card as it is now.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='', note='Card opening line.')
        card_id = manager.board_brief()['cards'][0]['card_id']
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.board_move(1, manager.board_brief()['revision'], 'blocked')
        for index in range(14):
            manager.board.append_note(
                card_id,
                'FINDING %02d: measured line %02d that runs on long enough to fill the excerpt window and then wraps onto a second line.' % (index, index),
                actor=slug)
        manager.board.append_note(
            card_id, 'STALE-FROM-COMPOSE: this sentence must not reach the wake.', actor=slug)
        manager.autonomy.learning.record_lesson(
            slug, 'The tile would not render until a reload.', 'It rendered without one.',
            'The tile renders on the running core.')
        text_lines = manager.autonomy.learning.agenda_lines(slug)
        board_lines, _ = manager.autonomy._own_board_lines(slug, clock.now)
        assert any('(blocked)' in line for line in board_lines)
        body = 'Standing agenda is due.\n' + '\n'.join(text_lines + board_lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert 'STALE-FROM-COMPOSE' in manager.autonomy.state['held'][slug][0]['text']
        # The card moves, and enough new evidence arrives that the compose-time
        # tail falls outside the 300-char excerpt window.
        manager.board_move(1, manager.board_brief()['revision'], 'queued')
        for index in range(4):
            manager.board.append_note(
                card_id,
                'FRESH %02d: new evidence appended after the agenda was armed, long enough to push older text out of the window.' % index,
                actor=slug)
        manager.board.append_note(
            card_id, 'FRESH: the card is queued now and this is the newest evidence.', actor=slug)
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10)
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wakes) == 1, f'expected one held agenda; wakes={len(wakes)}'
        text = wakes[-1].text
        assert '(queued)' in text, f'held agenda quoted a status the card had left: {text!r}'
        assert '(blocked)' not in text
        assert 'FRESH: the card is queued now' in text
        assert 'STALE-FROM-COMPOSE' not in text, (
            f'a compose-time fragment survived the refresh: {text!r}')
        # The card line is still a single physical line, not split by the excerpt.
        card_lines = [line for line in text.split('\n') if line.startswith('Your board card ')]
        assert len(card_lines) == 1
        assert card_lines[0].count('Note: ') == 1


def test_a_held_agenda_survives_text_composed_before_the_line_contract(tmp_path):
    """Held text armed by an older build can still contain split lines.

    The composer now collapses them, but state['held'] persisted across a reload
    holds whatever the previous build wrote. The classifier must repair that
    text rather than trust it, or the stale fragment survives on exactly the
    wakes that were already frozen when the fix landed.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Reading the card as it is now.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='', note='Card opening line.')
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        manager.board_move(1, manager.board_brief()['revision'], 'blocked')
        # Hand-built text in the pre-fix shape: the card line ends mid-sentence
        # and its continuation sits on the next physical line, unprefixed.
        stale = ('Standing agenda is due.\n'
                 'Your board card 1: "Autonomy" (blocked). Note: […] STALE-COMPOSE-TAIL: this\n'
                 'continuation line was written by the old composer.\n'
                 'Lesson ls-test: trigger "x". Failure: y. Check: z. No skill bound yet. Write one and bind it.')
        manager.autonomy.post(slug, 'self_prompt', stale)
        manager.board_move(1, manager.board_brief()['revision'], 'queued')
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10)
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wakes) == 1, f'expected one held agenda; wakes={len(wakes)}'
        text = wakes[-1].text
        assert '(queued)' in text
        # The card line itself is rebuilt, so the token on it is dropped either
        # way. What discriminates is the CONTINUATION: unprefixed text on the
        # next physical line. The old classifier kept it as a static agenda line
        # and delivered it above the rebuilt card line.
        assert 'continuation line was written by the old composer' not in text, (
            f'an unprefixed fragment survived as static agenda text: {text!r}')
        # The lesson line is genuinely static and must still be delivered.
        assert 'Lesson ls-test' in text


def test_a_held_agenda_drops_lessons_retired_before_delivery(tmp_path):
    """A held agenda drops lessons retired before delivery.

    _refresh_agenda rebuilds the board half and leaves lesson lines in place:
    recomputing lessons would hold everything and deliver nothing. Delivery
    therefore filters. It drops lines whose lesson is not open, and never adds
    any. A lesson that is still open must survive, and the wake must still be sent.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Delivered.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        # An owned live card, so the agenda has a board half as the live wake did.
        manager.board.create('Autonomy', slug, 0, 0, owner='', note='Card opening line.')
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        keep = manager.autonomy.learning.record_lesson(
            slug, 'The tile would not render until a reload.',
            'It rendered without one.', 'The tile renders on the running core.')
        doomed = manager.autonomy.learning.record_lesson(
            slug, 'Voice is a parrot with no session or tools.',
            'The turn never completed.', 'The voice bridge answers with a session and tools.',
            skill='voice-bridge-probe')
        text_lines = manager.autonomy.learning.agenda_lines(slug)
        board_lines, _ = manager.autonomy._own_board_lines(slug, clock.now)
        body = 'Standing agenda is due.\n' + '\n'.join(text_lines + board_lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert doomed['id'] in manager.autonomy.state['held'][slug][0]['text']
        # Retired between compose and delivery, exactly as happened live.
        manager.autonomy.learning.check(slug, doomed['id'], True,
                                        note='Measured working.', retire=True)
        clock.now += 1800
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10)
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert len(wakes) == 1, f'expected one held agenda; wakes={len(wakes)}'
        text = wakes[-1].text
        assert doomed['id'] not in text, (
            f'a retired lesson survived into the wake: {text!r}')
        assert 'voice-bridge-probe' not in text, (
            'the wake still asks for a skill bound to retired work')
        assert keep['id'] in text, 'a still-open lesson was dropped with it'
        assert '(queued)' in text, 'the board half must still be rebuilt'


def test_a_held_agenda_that_only_named_retired_lessons_does_not_wake(tmp_path):
    """The lesson half of the same rule the board half already follows.

    When every line in a held agenda names work that has settled, there is
    nothing left to say. Waking a session to tell it about retired lessons
    costs a turn and teaches it nothing — so deliver nothing.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    backend = FakeBackend(['Should not be asked anything.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        doomed = manager.autonomy.learning.record_lesson(
            slug, 'Voice is a parrot with no session or tools.',
            'The turn never completed.', 'The voice bridge answers with a session and tools.',
            skill='voice-bridge-probe')
        body = 'Standing agenda is due.\n' + '\n'.join(
            manager.autonomy.learning.agenda_lines(slug))
        manager.autonomy.post(slug, 'self_prompt', body)
        assert manager.autonomy.state['held'][slug], 'agenda was never armed'
        manager.autonomy.learning.check(slug, doomed['id'], True,
                                        note='Measured working.', retire=True)
        # Past the wake gap, so delivery is actually attempted: without this the
        # caps alone would suppress the wake and the test would pass vacuously.
        clock.now += 600
        manager.autonomy.tick()
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert wakes == [], (
            f'a wake was sent for work that had settled: {wakes[0].text!r}')
        assert manager.autonomy.state['held'].get(slug) == [], (
            'the settled agenda stayed armed instead of being cleared')


def test_a_held_agenda_that_only_named_settled_reminders_does_not_wake(tmp_path):
    """A reminder is a lesson row, so the settled filter must know its prefix.

    The composer writes "Reminder <id>:" but a filter that matches only "Lesson"
    misses it, and a join loop that does not list "Reminder" as a prefix glues
    the reminder onto the preceding line so it is never filtered. A settled
    reminder must not be delivered.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    backend = FakeBackend(['Should not be asked anything.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        reminder = manager.autonomy.learning.record_lesson(
            slug, 'finish what you were working on please.',
            'Completion score 0 is below the check.', '', kind='reminder')
        lines = manager.autonomy.learning.agenda_lines(slug)
        assert any(line.startswith('Reminder ' + reminder['id']) for line in lines), (
            f'the fixture did not produce a reminder line: {lines!r}')
        body = 'Standing agenda is due.\n' + '\n'.join(lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert manager.autonomy.state['held'][slug], 'agenda was never armed'
        manager.autonomy.learning.check(slug, reminder['id'], True,
                                        note='The work finished.')
        # Past the wake gap, so delivery is actually attempted: without this the
        # caps alone would suppress the wake and the test would pass vacuously.
        clock.now += 600
        manager.autonomy.tick()
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert wakes == [], (
            f'a wake was sent for a reminder that had settled: {wakes[0].text!r}')
        assert manager.autonomy.state['held'].get(slug) == [], (
            'the settled agenda stayed armed instead of being cleared')


def test_a_settled_reminder_is_dropped_even_when_a_lesson_line_precedes_it(tmp_path):
    """The live shape: "Lesson ..." then "Reminder ..." on the next line.

    If "Reminder " is not in the classifier's prefix list, the reminder line is
    treated as the tail of the lesson line above it and is joined onto it — so
    it never reaches _drop_settled_items as a line of its own and survives even
    after its row is absorbed. That shape delivers settled reminders glued
    behind a still-open lesson.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    backend = FakeBackend(['Should not be asked anything.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        keep = manager.autonomy.learning.record_lesson(
            slug, 'A procedure that is still open.', 'It failed once.',
            'The procedure passes.')
        doomed = manager.autonomy.learning.record_lesson(
            slug, 'finish what you were working on please.',
            'Completion score 0 is below the check.', '', kind='reminder')
        lines = manager.autonomy.learning.agenda_lines(slug)
        # The fixture must hold the live shape, or the test proves nothing about
        # the join half: a lesson line, then a reminder line beneath it.
        assert any(line.startswith('Lesson ' + keep['id']) for line in lines)
        assert any(line.startswith('Reminder ' + doomed['id']) for line in lines)
        body = 'Standing agenda is due.\n' + '\n'.join(lines)
        manager.autonomy.post(slug, 'self_prompt', body)
        assert manager.autonomy.state['held'][slug], 'agenda was never armed'
        manager.autonomy.learning.check(slug, doomed['id'], True,
                                        note='The work finished.')
        clock.now += 600
        manager.autonomy.tick()
        assert manager.wait(slug, timeout=10), 'the delivery never ran'
        wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
        assert wakes, 'the still-open lesson should still be delivered'
        text = wakes[-1].text
        assert 'Reminder ' + doomed['id'] not in text, (
            f'the settled reminder survived the filter: {text!r}')
        assert 'Lesson ' + keep['id'] in text, (
            f'a still-open lesson was dropped with it: {text!r}')


def test_a_legacy_continuation_that_starts_with_a_prefix_does_not_duplicate(tmp_path):
    """A board note is free prose, so a wrapped continuation can begin with the
    very prefix the classifier uses to mark a new agenda item.

    Notes can quote delivered wake lines verbatim, so a note excerpt can contain
    a physical line beginning "Your board card". The join loop in _refresh_agenda
    treats such a line as a new logical line, and the classifier then rebuilds it
    into a second offer for an ordinal it has already named.

    The control below emits one card line. A prefixed continuation must not emit
    a second. The composer emits at most one line per ordinal, so keeping the
    first is lossless.
    """
    from test_autonomy import config_for, Clock, install_fake_clock, messages
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    def deliver_with(continuation, case):
        # Separate home per case: a shared state file carries self_prompt_at
        # across cases and the wake gap would suppress the second delivery.
        home = tmp_path / case
        home.mkdir()
        config = config_for(home, self_prompt_gap_seconds=0, standing_floor=False)
        config.board.enabled = True
        backend = FakeBackend(['Reading the card as it is now.'])
        manager = SessionManager(config, backend_factory=lambda slug, name: backend)
        clock = Clock()
        install_fake_clock(manager, clock)
        slug = manager.create('Home')['slug']
        with manager:
            manager.board.create('Autonomy', slug, 0, 0, owner='', note='Card opening line.')
            manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
            manager.board_move(1, manager.board_brief()['revision'], 'blocked')
            # Held text in the pre-fix shape: the card line ends mid-sentence and
            # its continuation sits on the next physical line.
            stale = ('Standing agenda is due.\n'
                     'Your board card 1: "Autonomy" (blocked). Note: […] STALE-COMPOSE-TAIL: this\n'
                     + continuation + '\n'
                     'end of the note tail.')
            manager.autonomy.post(slug, 'self_prompt', stale)
            manager.board_move(1, manager.board_brief()['revision'], 'queued')
            manager.autonomy.tick()
            assert manager.wait(slug, timeout=10)
            wakes = [item for item in messages(manager, slug) if item.meta.get('wake')]
            return wakes[-1].text

    # Control: differs from the case below in exactly one property — whether the
    # continuation collides with the classifier's own vocabulary.
    control = deliver_with('this line was written by the old composer.', 'control')
    assert len([l for l in control.split('\n') if l.startswith('Your board card 1')]) == 1, (
        f'the control should emit exactly one offer: {control!r}')

    text = deliver_with('Your board card 1: this line is prose quoted inside the note.', 'prefixed')
    offers = [l for l in text.split('\n') if l.startswith('Your board card 1')]
    assert len(offers) == 1, f'a prefixed continuation duplicated the offer: {text!r}'
    # The duplicate must not survive as a stray static line either.
    assert text.count('quoted inside the note') == 0, (
        f'the quoted prose leaked into the agenda: {text!r}')


def test_a_skill_write_never_binds_an_unrelated_lesson_by_arity(tmp_path):
    """skill_write must not bind an unrelated lesson by arity.

    Two unbound lessons existed; one was bound explicitly and retired; then a
    skill_write for that retired lesson's skill bound the SURVIVING lesson by
    arity. The survivor then carried a procedure written for a different lesson,
    status became 'written', and the agenda stopped saying 'No skill bound yet'.
    """
    store = LearningStore(tmp_path / 'lessons.json', pass_score=1.0, fail_limit=2, clock=lambda: 100.0)
    loader = store.record_lesson('home', 'prove the loader read the config file',
                                 'the loader silently used defaults', 'a known field matches')
    shared = store.record_lesson('home', 'anchor every read to a revision',
                                 'a read landed on another commit', 'the revision is named')
    store.bind_skill('home', shared['id'], 'revision-anchored-measurement')
    store.check('home', shared['id'], passed=True, retire=True)
    assert [l['id'] for l in store.list_lessons('home')] == [loader['id']]

    # A skill_write for the retired lesson's skill must not bind the survivor.
    store.observe_turn('home', '[wake] Standing agenda is due.', [
        {'name': 'skill_write', 'success': True, 'arguments': {'name': 'revision-anchored-measurement'}},
    ], None, True, True)

    survivor = store.list_lessons('home')[0]
    assert survivor['skill'] == '', f'the unrelated lesson was bound: {survivor!r}'
    assert survivor['status'] == 'open', f'the lesson was marked written: {survivor!r}'
    assert 'No skill bound yet. Write one and bind it.' in store.agenda_lines('home')[0]


def test_a_skill_write_still_resets_the_lesson_that_names_it(tmp_path):
    """The legitimate path: rewriting the skill a lesson already names resets it.

    This is the behaviour test_bound_skill_miss_then_revision_then_pass relies on,
    and removing the arity fallback must not take it away.
    """
    store = LearningStore(tmp_path / 'lessons.json', pass_score=1.0, fail_limit=2, clock=lambda: 100.0)
    lesson = store.record_lesson('home', 'rebuild the desk helper', 'missing binary',
                                 'binary exists', skill='desk-helper')
    # Two misses reach needs_revision (fail_limit=2), same as the existing test.
    store.observe_turn('home', 'rebuild the desk helper now', [], None, True, False)
    assert store.list_lessons('home')[0]['status'] == 'written'
    store.observe_turn('home', 'rebuild the desk helper again', [], 0.0, False, False)
    assert store.list_lessons('home')[0]['status'] == 'needs_revision'

    store.observe_turn('home', '[wake] Standing agenda is due.', [
        {'name': 'skill_write', 'success': True, 'arguments': {'name': 'desk-helper'}},
    ], None, True, True)

    reset = store.list_lessons('home')[0]
    assert reset['id'] == lesson['id']
    assert reset['skill'] == 'desk-helper'
    assert reset['status'] == 'written' and reset['fails'] == 0


def test_an_explicit_bind_still_binds_an_unbound_lesson(tmp_path):
    """lesson_bind is the explicit path and must keep working."""
    store = LearningStore(tmp_path / 'lessons.json', pass_score=1.0, fail_limit=2, clock=lambda: 100.0)
    lesson = store.record_lesson('home', 'prove the loader read the config file',
                                 'the loader silently used defaults', 'a known field matches')
    bound = store.bind_skill('home', lesson['id'], 'config-load-must-prove-it-read')
    assert bound['skill'] == 'config-load-must-prove-it-read'
    assert bound['status'] == 'written'


def test_a_clipped_lesson_field_says_it_was_clipped(tmp_path):
    """A field cut at its cap must not be delivered as a finished instruction.

    A check stored at the character cap can end mid-phrase. If the agenda
    composer appends its own period, the wake reads as a finished sentence.
    A reader then has no cue to open the stored row, which makes a clipped
    rule a different rule.
    """
    store = LearningStore(tmp_path / 'learning.json')
    long_check = ('Before ANY commit in a shared working tree, run the cached check FIRST, '
                  'then commit with an explicit pathspec rather than relying on the index, '
                  'so only my paths enter the commit. And when a file is already staged by '
                  'another session, leave it staged exactly as found rather than unstaging it. '
                  'Restage only what you yourself unstaged, and say so in the card note so the '
                  'next reader knows which file was yours.')
    assert len(long_check) > 300, len(long_check)
    lesson = store.record_lesson('home', 'commit in a shared tree', 'the commit swept in a file',
                                 long_check)
    assert len(lesson['check']) <= 300
    assert lesson['check'].endswith('[…]'), lesson['check'][-40:]
    assert not lesson['check'].rstrip().endswith('.')


def test_a_clipped_check_does_not_get_a_period_glued_onto_the_cut(tmp_path):
    store = LearningStore(tmp_path / 'learning.json')
    store.record_lesson('home', 'commit in a shared tree', 'the commit swept in a file',
                        'x ' * 400)
    line = store.agenda_lines('home')[0]
    # The whole point: "… […]." would read as a complete sentence.
    assert '[…].' not in line
    assert '[…]' in line


def test_a_field_that_fits_is_unchanged_and_still_ends_in_a_period(tmp_path):
    store = LearningStore(tmp_path / 'learning.json')
    lesson = store.record_lesson('home', 'rebuild the desk helper', 'the helper was not compiled',
                                 'the helper binary exists')
    assert lesson['check'] == 'the helper binary exists'
    assert '[…]' not in lesson['check']
    line = store.agenda_lines('home')[0]
    assert line.endswith('Check: the helper binary exists. No skill bound yet. Write one and bind it.'), line


def test_a_field_already_ending_in_a_period_does_not_gain_a_second(tmp_path):
    store = LearningStore(tmp_path / 'learning.json')
    store.record_lesson('home', 'rebuild the desk helper', 'the helper was not compiled',
                        'the helper binary exists.')
    line = store.agenda_lines('home')[0]
    assert 'exists..' not in line
    assert 'Check: the helper binary exists.' in line


def test_the_truncation_marker_does_not_break_lesson_matching(tmp_path):
    """A clipped trigger carries a marker; the match must use the words only.

    The discriminating case: a reader who saw the wake retypes the words without
    the marker. Matching on the stored string (marker included) would miss.
    """
    store = LearningStore(tmp_path / 'learning.json')
    lesson = store.record_lesson('home', 'commit in a shared tree ' + 'word ' * 80,
                                 'the commit swept in a file', 'check the staged list first')
    assert lesson['trigger'].endswith('[…]')
    words_only = lesson['trigger'].replace('[…]', '').strip()
    assert store._matching('home', words_only), 'the kept words must still match'
    assert store._matching('home', lesson['trigger']), 'quoting it with the marker must match too'


def test_a_row_stored_clipped_before_the_fix_is_marked_at_delivery(tmp_path):
    """A row stored at exactly the cap, with no marker, is marked at delivery.

    The row cannot say it was cut, so the composer says it. Rewriting the
    stored text is a separate decision.
    """
    path = tmp_path / 'learning.json'
    legacy_check = 'x ' * 150                      # exactly 300 chars after collapsing
    legacy_check = legacy_check.strip()
    assert len(legacy_check) == 299
    legacy_check = legacy_check + 'y'              # 300, no marker, ends mid-word
    path.write_text(json.dumps({'lessons': {'ls-legacy': {
        'id': 'ls-legacy', 'slug': 'home', 'trigger': 'a legacy clipped row',
        'failure': 'it was stored by a bare slice', 'check': legacy_check,
        'skill': '', 'status': 'open', 'fails': 0, 'checks': [], 'created': 1,
    }}, 'questions': {}, 'intentions': {}}), encoding='utf-8')
    store = LearningStore(path)
    line = store.agenda_lines('home')[0]
    assert '[…]' in line, line[-80:]
    assert not line.endswith('[…].'), line[-80:]


def test_a_row_stored_clipped_does_not_deliver_a_period_on_the_fragment(tmp_path):
    path = tmp_path / 'learning.json'
    legacy = ('I concluded it had crossed the cap and wrote that up as the root cause, '
              'then measured the actually rendered prompt and found the payload was well '
              'under it, so the estimate had been wrong from the start and the real '
              'failure was somewhere else entirely in the request path and the next '
              'reader would have inherited that wrong cause as settled fact')
    assert len(legacy) > 300, len(legacy)
    legacy = legacy[:300]                       # what the old bare slice stored
    assert len(legacy) == 300
    assert not legacy.rstrip().endswith('.')
    path.write_text(json.dumps({'lessons': {'ls-legacy': {
        'id': 'ls-legacy', 'slug': 'home', 'trigger': 'a legacy clipped row',
        'failure': 'stored by a bare slice', 'check': legacy,
        'skill': '', 'status': 'open', 'fails': 0, 'checks': [], 'created': 1,
    }}, 'questions': {}, 'intentions': {}}), encoding='utf-8')
    store = LearningStore(path)
    line = store.agenda_lines('home')[0]
    assert '[…]' in line
    assert not line.rstrip().endswith('[…].')


def test_a_field_written_to_exactly_the_cap_is_not_claimed_as_clipped(tmp_path):
    """The inverse fault: a complete rule at the cap must not be marked as cut.

    Marking it would claim a truncation that never happened — the same lie as
    delivering a cut field as if it were whole, just in the other direction.
    """
    path = tmp_path / 'learning.json'
    # Build to exactly 300 ending in a period, by arithmetic rather than slicing.
    body = 'A complete rule written to exactly the cap on purpose and never cut. '
    complete = body + ('x' * (299 - len(body)))
    assert len(complete) == 299 and not complete.endswith((' ', '.'))
    complete += '.'
    assert len(complete) == 300, len(complete)
    assert complete.endswith('.') and not complete.endswith('..')
    path.write_text(json.dumps({'lessons': {'ls-exact': {
        'id': 'ls-exact', 'slug': 'home', 'trigger': 'a rule at exactly the cap',
        'failure': 'none', 'check': complete,
        'skill': '', 'status': 'open', 'fails': 0, 'checks': [], 'created': 1,
    }}, 'questions': {}, 'intentions': {}}), encoding='utf-8')
    store = LearningStore(path)
    line = store.agenda_lines('home')[0]
    assert '[…]' not in line, 'a complete rule must not be marked as clipped'
    assert complete in line


def test_a_failed_turn_is_a_reminder_and_never_an_agenda_check(tmp_path):
    """A failed turn is a reminder, and never an agenda check.

    Storing the literal check 'The same request completes.' restates the failure,
    and the agenda would then deliver it as a procedure to run on a sentence.

    The close condition of a reminder is the work getting done, not a check
    passing, so this asserts both halves: the row is a reminder, and it leaves
    the agenda as one.
    """
    store = LearningStore(tmp_path / 'lessons.json', pass_score=0.5, clock=lambda: 100.0)
    created = store.observe_turn('home', 'lets go ahead and merge', [], 0.0, False, False)
    assert created is not None
    assert created['kind'] == 'reminder', created
    assert created['check'] == '', f'a reminder must not carry a check: {created!r}'
    lines = store.agenda_lines('home')
    assert lines, 'the reminder must still reach the session'
    assert 'Check: The same request completes.' not in lines[0], lines[0]
    assert lines[0].startswith('Reminder '), lines[0]
    assert 'lets go ahead and merge' in lines[0]
    assert 'record it done' in lines[0]
    # The reminder line itself must not tell the session to load a skill and
    # run it: there is no check to run. The separate session push that binds
    # compare-before-adopt is a different design; what this asserts is that
    # the reminder does not carry it.
    assert 'Load skill' not in lines[0], lines[0]
    assert 'Skill:' not in lines[0], lines[0]

    # A reminder closes when the same request comes back and completes.
    store.observe_turn('home', 'lets go ahead and merge', [], 1.0, True, False)
    assert store.list_lessons('home') == []
    saved = json.loads((tmp_path / 'lessons.json').read_text(encoding='utf-8'))
    assert saved['lessons'][created['id']]['status'] == 'absorbed'


def test_record_lesson_refuses_a_check_that_restates_the_failure(tmp_path):
    """The placeholder cannot be reintroduced through the explicit tool either.

    The turn path stopped inventing it, but `record_lesson` is a public tool.
    If it still accepted the literal, a caller could store the exact row this
    change exists to prevent, and the agenda would deliver it as a procedure.
    """
    store = LearningStore(tmp_path / 'lessons.json')
    try:
        store.record_lesson('home', 'rebuild the desk helper', 'it did not rebuild',
                            'The same request completes.')
    except ValueError as refused:
        assert 'restates the failure' in str(refused), refused
    else:
        raise AssertionError('the placeholder check was accepted')
    assert store.list_lessons('home') == []


def test_a_stored_placeholder_row_is_delivered_as_a_reminder(tmp_path):
    """A stored placeholder row is delivered as a reminder.

    The composer does not rewrite stored rows. It must not quote their
    placeholder as a check. The words come from the stored text.
    """
    path = tmp_path / 'learning.json'
    path.write_text(json.dumps({'lessons': {
        'ls-legacy': {
            'id': 'ls-legacy', 'slug': 'home',
            'trigger': 'continue and complete your work now.',
            'failure': 'Completion score 0.0 is below the check.',
            'check': 'The same request completes.',
            'skill': 'compare-before-adopt', 'status': 'written',
            'source': 'turn', 'fails': 0, 'checks': [], 'created': 1,
        },
        'ls-real': {
            'id': 'ls-real', 'slug': 'home',
            'trigger': 'A commit swept in another session staged file.',
            'failure': 'It committed the wrong files.',
            'check': 'git diff --cached --name-only is run before git add.',
            'skill': '', 'status': 'open', 'source': 'tool',
            'fails': 0, 'checks': [], 'created': 2,
        },
    }, 'questions': {}, 'intentions': {}}), encoding='utf-8')
    store = LearningStore(path)
    lines = store.agenda_lines('home')
    assert len(lines) == 2, lines
    assert all('Check: The same request completes.' not in line for line in lines), lines
    assert any(line.startswith('Reminder ls-legacy') for line in lines), lines
    # The genuine lesson keeps its own check: the filter must not swallow it.
    real = [line for line in lines if 'ls-real' in line][0]
    assert 'Check: git diff --cached --name-only is run before git add.' in real, real


def test_a_note_this_session_wrote_does_not_re_arm_its_own_card(tmp_path):
    """Recording progress is not news to the session that recorded it.

    The fingerprint includes the version, and append_note() moves the version, so
    writing progress re-arms the card the progress was written on unless a version
    whose last writer is this session is ignored. The delivered line would otherwise
    quote the note back.

    The fix is to ignore a version whose last writer is this session. `updated_by`
    is already carried on the card, so the card holds the evidence of who moved it.
    A version moved by anyone else must still re-arm, which the second half pins.
    """
    from test_autonomy import config_for, Clock, install_fake_clock
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = config_for(tmp_path, self_prompt_gap_seconds=0, standing_floor=False)
    config.board.enabled = True
    backend = FakeBackend(['Nothing to do.'])
    manager = SessionManager(config, backend_factory=lambda slug, name: backend)
    clock = Clock()
    install_fake_clock(manager, clock)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Autonomy', slug, 0, 0, owner='', note='Card opening line.')
        manager.board_claim(1, manager.board_brief()['revision'], owner=slug)
        # First offer: the card is claimed and nothing has been said about it.
        board_lines, seen = manager.autonomy._own_board_lines(slug, clock.now)
        assert len(board_lines) == 1, board_lines
        manager.autonomy.state.setdefault('board_seen', {})[slug] = dict(seen)
        # This session records its own progress, which moves the version.
        manager.board_note(1, manager.board_brief()['revision'],
                           'Wrote the tile and measured it.', actor=slug)
        card = [c for c in manager.board_brief()['cards'] if c['ordinal'] == 1][0]
        assert card['updated_by'] == slug, card['updated_by']
        board_lines, seen = manager.autonomy._own_board_lines(slug, clock.now)
        assert board_lines == [], (
            'a note this session wrote re-armed its own card: %r' % (board_lines,))
        # Another session moving the card is still news and must re-arm it. The card
        # stays owned by this session (another session cannot note it), so the brief is
        # supplied directly: what is being measured is the fingerprint rule, not the
        # ownership check that already has its own tests.
        foreign = dict(card, updated_by='infinity', version=card['version'] + 1)
        manager.board_brief = lambda: {'cards': [foreign], 'revision': 1}
        board_lines, seen = manager.autonomy._own_board_lines(slug, clock.now)
        assert len(board_lines) == 1, (
            'a version moved by another session must still re-arm: %r' % (board_lines,))
        manager.autonomy.state.setdefault('board_seen', {})[slug] = dict(seen)
        # Having offered the foreign change, this session's own follow-up note is still
        # not news: the earlier fingerprint described this same status. Without the
        # status-prefix comparison the version bump would re-arm the echo.
        follow_up = dict(card, updated_by=slug, version=card['version'] + 2)
        manager.board_brief = lambda: {'cards': [follow_up], 'revision': 2}
        board_lines, _ = manager.autonomy._own_board_lines(slug, clock.now)
        assert board_lines == [], (
            'a follow-up note re-armed the echo after a foreign change: %r' % (board_lines,))


def test_a_voice_session_is_never_woken_by_the_floor(tmp_path):
    """A voice session is a place to talk, not a mission.

    The floor wakes sessions for lessons, questions, intentions and their own board
    cards. A wake in the voice session would spend a reasoning turn on work nobody
    asked for, and the next thing a speaker heard would be the result of a turn they
    never requested. Measured before this: the wake target list was built from
    lessons/questions/intentions with no exclusion, so a voice session that ever
    acquired one would be woken like any chat.
    """
    from tessera.config import Config
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend

    config = Config(home=tmp_path, cwd=tmp_path)
    config.autonomy.enabled = True
    config.autonomy.self_prompt_enabled = True
    config.board.enabled = True
    backend = FakeBackend(['spoken'])
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        manager.ensure_voice()
        manager.create('ordinary-chat')
        voice_slug = manager.config.voice.dedicated_slug
        # Give the voice session something that WOULD wake an ordinary session.
        manager.autonomy.learning.record_lesson(voice_slug, 'Voice session lesson about spoken answers',
                                                'something failed', 'check it', source='test')
        manager.autonomy.learning.record_lesson('ordinary-chat', 'Chat lesson about a failing tool call',
                                                'something failed', 'check it', source='test')
        targets = manager.autonomy._board_targets()
        assert voice_slug not in targets, 'a voice session is not a board target'
        queued = []
        original = manager.autonomy._arm_or_post
        def spy(slug, now, body, last_map, gap, kind='self_prompt', rules=None):
            queued.append(slug)
            return original(slug, now, body, last_map, gap, kind, rules=rules)
        manager.autonomy._arm_or_post = spy
        manager.autonomy._queue_self_prompts(1000.0)
        assert voice_slug not in queued, 'the floor must not wake the voice session'
        assert 'ordinary-chat' in queued, 'an ordinary session with a lesson is still woken'
