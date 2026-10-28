"""The floor definition is data. Shipped rules reproduce today's lines, and a
deleted or disabled rule produces nothing from that rule, in Safe and in YOLO.
"""

import json
from pathlib import Path
import re

import pytest

from tessera.autonomy import board_pressure_lines, coordination_offers, pressure_lines, pressure_seen
from tessera.config import Config
from tessera.floor_definition import (
    FloorDefinitionError, default_definition, evaluate, export_text, import_text, load_definition,
    save_definition,
)
from tessera.floor_signals import SIGNALS


def _down(label='north', ordinal=1):
    return {'hosts': [{'ordinal': ordinal, 'label': label, 'health': 'unreachable'}]}


def test_shipped_rules_match_the_lines_the_floor_already_sent():
    steady = pressure_lines({}, {}, 0)
    assert steady == ['Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']
    managed = pressure_lines(_down(), {}, 0, thresholds={1: {'managed': True}})
    assert managed == [
        'Pressure: north is unreachable. Repair it. Restart the service if that is the fix. Do not replace the kiosk.']
    declared_off = pressure_lines(_down(), {}, 0, thresholds={1: {'managed': False}})
    assert 'Do not invent a repair' in declared_off[0]
    assert 'not declared' not in declared_off[0]
    brief = {'cards': [
        {'ordinal': 3, 'status': 'queued', 'project': '', 'age_seconds': 60},
        {'ordinal': 9, 'status': 'queued', 'project': 'alpha', 'age_seconds': 7 * 3600},
    ]}
    lines = board_pressure_lines(brief)
    assert lines[0].startswith('Pressure: 1 board card(s) unfiled (3)')
    assert 'Do not ask a person to file them' in lines[0]
    assert 'older than six hours (9)' in lines[1]


def test_deleted_rules_leave_the_floor_with_nothing_from_them():
    definition = default_definition()
    definition['rules'] = []
    report = _down('Kiosk')
    assert pressure_lines(report, {}, 0, definition=definition) == []
    brief = {'cards': [{'ordinal': 3, 'status': 'queued', 'project': '', 'age_seconds': 99999}]}
    assert board_pressure_lines(brief, definition=definition) == []
    assert coordination_offers({'cards': [{
        'ordinal': 3, 'status': 'queued', 'version': 1, 'title': 'Open', 'card_id': 'a',
    }]}, 3, {}, now=1, definition=definition) == ([], {})


def test_a_disabled_rule_stays_off_in_yolo():
    definition = default_definition()
    for rule in definition['rules']:
        if rule['id'] == 'host-unhealthy-unmanaged':
            rule['enabled'] = False
    lines = pressure_lines(_down('bench'), {}, 0, definition=definition, mode='yolo')
    assert not any('bench' in line for line in lines)
    # A guard runs in Safe only. Turning it off wins over both modes.
    definition = default_definition()
    for rule in definition['rules']:
        if rule['id'] == 'host-disk':
            rule['guard'] = True
            rule['enabled'] = False
    disk = {'hosts': [{'ordinal': 1, 'label': 'Mac', 'health': 'healthy',
                       'metrics': {'disk_free_bytes': 1, 'disk_total_bytes': 100}}]}
    assert not any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='safe'))
    definition['rules'] = [rule for rule in default_definition()['rules'] if rule['id'] == 'host-disk']
    definition['rules'][0]['guard'] = True
    assert any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='safe'))
    assert not any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='yolo'))


def test_unknown_approval_mode_keeps_safe_guards_on(tmp_path, monkeypatch):
    from test_autonomy import open_manager
    definition = default_definition()
    definition['rules'] = [rule for rule in definition['rules'] if rule['id'] == 'host-disk']
    definition['rules'][0]['guard'] = True
    disk = {'hosts': [{'ordinal': 1, 'label': 'north', 'health': 'healthy',
                       'metrics': {'disk_free_bytes': 1, 'disk_total_bytes': 100}}]}
    assert any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='unknown'))
    assert any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='safe'))
    assert not any('disk free' in line for line in pressure_lines(disk, {}, 0, definition=definition, mode='yolo'))

    def _approval():
        raise RuntimeError('approval unread')

    manager, _slug, _clock, _backend = open_manager(tmp_path, [])
    monkeypatch.setattr(manager, 'approval_status', _approval)
    stored = default_definition()
    for rule in stored['rules']:
        if rule['id'] == 'visible-error':
            rule['guard'] = True
            rule['template'] = 'GUARD {source}'
    save_definition(manager.config, stored)
    with manager:
        report = manager.autonomy.pressure_report()
        assert report['approval_mode'] == 'unknown'
        assert any(item.get('source') == 'approval-status' and 'approval unread' in (item.get('message') or '')
                   for item in report['errors'])
        assert any((item.get('text') or '').startswith('GUARD ') for item in report['items'])
        assert 'Safe' not in report['approval_mode']


def test_h05_one_hold_drives_the_board_the_offer_and_the_sentence():
    definition = default_definition()
    definition['compose']['stale_seconds'] = 60
    definition['compose']['stale_label'] = 'one minute'
    for rule in definition['rules']:
        if rule['id'] == 'board-unfiled':
            rule['name_limit'] = 1
            rule['template'] = 'File them. ({names})'
    brief = {'cards': [
        {'ordinal': 1, 'status': 'queued', 'project': 'alpha', 'age_seconds': 30},
        {'ordinal': 2, 'status': 'queued', 'project': 'alpha', 'age_seconds': 120},
        {'ordinal': 3, 'status': 'queued', 'project': '', 'age_seconds': 10},
        {'ordinal': 4, 'status': 'queued', 'project': '', 'age_seconds': 10},
    ]}
    lines = board_pressure_lines(brief, definition=definition)
    assert lines[0] == 'File them. (3)'
    assert 'one minute (2)' in lines[1]
    assert '(1)' not in lines[1]
    offers, seen = coordination_offers({'cards': [{
        'ordinal': 5, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Held', 'card_id': 'id-5',
    }]}, 3, {}, now=1000, definition=definition)
    assert offers
    held, _ = coordination_offers({'cards': [{
        'ordinal': 5, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Held', 'card_id': 'id-5',
    }]}, 3, seen, now=1030, definition=definition)
    assert held == []
    back, _ = coordination_offers({'cards': [{
        'ordinal': 5, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Held', 'card_id': 'id-5',
    }]}, 3, seen, now=1061, definition=definition)
    assert back


def test_h07_a_set_priority_field_wins_and_a_title_pattern_fills_a_gap():
    """The integer field is the priority. The shipped pattern runs only when it is unset."""
    brief = {'cards': [
        {'ordinal': 2, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'P1 in the title', 'card_id': 'c'},
        {'ordinal': 11, 'status': 'queued', 'version': 1, 'project': '', 'title': 'P1 ignored', 'card_id': 'd', 'priority': 9},
        {'ordinal': 4, 'status': 'queued', 'version': 1, 'project': '', 'title': 'no code', 'card_id': 'e', 'priority': 2},
    ]}
    ranked, _ = coordination_offers(brief, 3, {}, now=1)
    assert 'P1 in the title' in ranked[0]
    assert 'no code' in ranked[1]
    assert 'P1 ignored' in ranked[2]


def test_h13_the_disk_latch_uses_the_same_limit_as_the_line():
    report = {'hosts': [{'ordinal': 1, 'label': 'Mac', 'health': 'healthy',
                         'metrics': {'disk_free_bytes': 20, 'disk_total_bytes': 100}}]}
    thresholds = {1: {'disk_free_percent_below': 25}}
    assert 'disk free is 20 percent' in pressure_lines(report, {}, 0, thresholds=thresholds)[0]
    seen = pressure_seen(report, 1, thresholds=thresholds)
    assert seen['Mac']['disk_low'] is True
    assert pressure_lines(report, seen, 2, thresholds=thresholds) == [
        'Pressure: hosts are steady. Do not invent work. Do not continue the last wake.']
    # A tighter declared limit must not latch a disk the wake would leave alone.
    quiet = {'hosts': [{'ordinal': 1, 'label': 'Mac', 'health': 'healthy',
                        'metrics': {'disk_free_bytes': 8, 'disk_total_bytes': 100}}]}
    tight = {1: {'disk_free_percent_below': 5}}
    assert 'disk free' not in ' '.join(pressure_lines(quiet, {}, 0, thresholds=tight))
    assert pressure_seen(quiet, 1, thresholds=tight)['Mac']['disk_low'] is False


def test_h04_templates_and_the_clip_are_settings():
    from tessera.autonomy import wake_footer
    from tessera.floor_definition import compose_wake
    definition = default_definition()
    definition['compose']['footer'] = 'Footer for {agent} and {operator}.'
    definition['compose']['quiet_paragraph'] = 'Quiet on purpose.'
    definition['compose']['coordination_paragraph'] = 'Coordinate for {operator}.'
    definition['compose']['world_view_max_chars'] = 5
    config = Config(agent_name='Bubba', operator_name='Ada')
    assert wake_footer(config, definition) == 'Footer for Bubba and Ada.'
    kind, text, rules = compose_wake(definition, config, [], 'abcdefghij')
    assert kind == 'reflection'
    assert rules == []
    assert 'Quiet on purpose.' in text
    assert 'abcd\u2026' in text
    definition['compose']['world_view_max_chars'] = 0
    _, unclipped, _ = compose_wake(definition, config, [], 'abcdefghij')
    assert 'abcdefghij' in unclipped


def test_invalid_definition_names_the_rule_and_the_field(tmp_path):
    definition = default_definition()
    definition['rules'][0]['when'] = "not_a_field == true"
    with pytest.raises(FloorDefinitionError) as caught:
        save_definition(Config(home=tmp_path), definition)
    message = str(caught.value)
    assert caught.value.rule_id == definition['rules'][0]['id']
    assert caught.value.field == 'when'
    assert 'not_a_field' in message
    assert not (tmp_path / 'floor-definition.json').exists()


def test_import_export_and_audit_round_trip(tmp_path):
    config = Config(home=tmp_path)
    definition = default_definition()
    definition['compose']['world_view'] = 'Keep the house warm.'
    definition['rules'][0]['note'] = 'edited'
    save_definition(config, definition, actor='ada')
    exported = export_text(load_definition(config), 'json')
    assert 'Keep the house warm.' in exported
    toml_text = export_text(load_definition(config), 'toml')
    imported = import_text(toml_text)
    assert imported['compose']['world_view'] == 'Keep the house warm.'
    broken = default_definition()
    broken['rules'][3]['score'] = 'high'
    with pytest.raises(FloorDefinitionError) as caught:
        import_text(json.dumps(broken))
    assert caught.value.field == 'score'
    assert caught.value.rule_id == broken['rules'][3]['id']
    from tessera.floor_definition import read_audit
    audit = read_audit(config)
    assert audit and audit[-1]['actor'] == 'ada'
    assert any(change['rule'] == definition['rules'][0]['id'] for change in audit[-1]['changes'])


def test_h06_every_score_and_the_wake_threshold_are_on_the_definition():
    from tessera.floor_definition import compose_wake
    definition = default_definition()
    scores = {rule['id']: rule['score'] for rule in definition['rules']}
    assert definition['compose']['wake_score'] == 1.0
    assert scores['host-changed-unreachable-managed'] == 1.8
    assert scores['host-changed-managed'] == 1.5
    assert scores['host-unhealthy-managed'] == 1.4
    assert scores['host-disk'] == 1.4
    assert scores['board-unfiled'] == 0.2
    assert scores['floor-dispatch'] == 1.3
    assert scores['coordination-offer'] == 1.3
    assert scores['session-pressure'] == 1.3
    hits = [{'rule': 'host-disk', 'signal': 'host', 'wake': True, 'steady': False,
             'pressure': True, 'score': 1.4, 'text': 'Pressure: disk'}]
    kind, _, rules = compose_wake(definition, Config(), hits, '')
    assert kind == 'pressure' and rules == ['host-disk']
    definition['compose']['wake_score'] = 2
    held_kind, _, held_rules = compose_wake(definition, Config(), hits, '')
    assert held_kind == 'reflection' and held_rules == []


def test_the_api_saves_previews_and_names_a_bad_rule(tmp_path):
    from test_server import call, running_server
    with running_server(tmp_path) as server:
        status, view = call(server, 'GET', '/api/autonomy/floor-definition')
        assert status == 200
        assert view['definition']['rules']
        assert 'host' in view['signals']
        definition = view['definition']
        definition['compose']['footer'] = 'Edited footer for {agent}.'
        definition['compose']['world_view'] = 'Warm house.'
        status, saved = call(server, 'PUT', '/api/autonomy/floor-definition',
                             {'definition': definition, 'actor': 'ada'})
        assert status == 200
        assert saved['definition']['compose']['footer'] == 'Edited footer for {agent}.'
        assert saved['audit'][-1]['actor'] == 'ada'
        status, preview = call(server, 'POST', '/api/autonomy/floor-definition/preview',
                               {'definition': definition})
        assert status == 200
        assert 'Edited footer' in preview['world_view']
        status, dry = call(server, 'POST', '/api/autonomy/floor-definition/dry-run',
                           {'rule_id': 'host-steady', 'definition': definition})
        assert status == 200
        assert dry['hits'] and 'hosts are steady' in dry['hits'][0]['text']
        status, exported = call(server, 'GET', '/api/autonomy/floor-definition/export?format=json')
        assert status == 200
        assert 'Warm house.' in exported['text']
        definition['sources']['host'] = False
        status, saved = call(server, 'PUT', '/api/autonomy/floor-definition',
                             {'definition': definition, 'actor': 'ada'})
        assert status == 200
        assert saved['definition']['sources']['host'] is False
        status, toml_export = call(server, 'GET', '/api/autonomy/floor-definition/export?format=toml')
        assert status == 200
        assert 'host = false' in toml_export['text']
        status, imported = call(server, 'POST', '/api/autonomy/floor-definition/import',
                                {'text': toml_export['text'], 'actor': 'ada'})
        assert status == 200
        assert imported['definition']['sources']['host'] is False
        assert imported['definition']['compose']['world_view'] == 'Warm house.'
        status, refused = call(server, 'POST', '/api/autonomy/floor-definition/import',
                               {'text': '{', 'actor': 'ada'})
        assert status == 400
        assert 'JSON' in refused['error']
        status, kept = call(server, 'GET', '/api/autonomy/floor-definition')
        assert status == 200
        assert kept['definition']['sources']['host'] is False
        assert kept['definition']['compose']['world_view'] == 'Warm house.'
        definition['rules'][0]['when'] = 'nope == true'
        status, refused = call(server, 'PUT', '/api/autonomy/floor-definition', {'definition': definition})
        assert status == 400
        assert 'nope' in refused['error']
        status, kept = call(server, 'GET', '/api/autonomy/floor-definition')
        assert status == 200
        assert kept['definition']['compose']['world_view'] == 'Warm house.'


def test_an_invalid_file_is_shown_as_an_error_not_as_the_shipped_rules(tmp_path):
    (tmp_path / 'floor-definition.json').write_text('{', encoding='utf-8')
    from test_server import call, running_server
    with running_server(tmp_path) as server:
        status, view = call(server, 'GET', '/api/autonomy/floor-definition')
        assert status == 200
        assert view['definition'] is None
        assert view['source'] == 'invalid'
        assert 'JSON' in view['error']
        assert view['raw'] == '{'


def test_h19_a_registry_error_and_an_intention_failure_stay_visible(tmp_path, monkeypatch):
    from test_autonomy import open_manager
    manager, slug, clock, _ = open_manager(tmp_path, [])

    def _boom(*_args, **_kwargs):
        raise RuntimeError('registry down')

    monkeypatch.setattr('tessera.autonomy.thresholds_by_ordinal', _boom)

    class _Tools:
        def schemas(self):
            return [{'function': {'name': 'demo_intention_status'}}]

        def execute(self, _name, _arguments):
            raise RuntimeError('intention down')

    class _Runtime:
        session = type('Session', (), {'tools': _Tools()})()

        def wait(self):
            return None

        def idle(self):
            return True

        def close(self):
            return None

    with manager:
        manager._runtimes[slug] = _Runtime()
        manager.autonomy._intentions_at = 0
        manager.autonomy.settings.self_prompt_gap_seconds = 0
        manager.autonomy._refresh_intentions(clock.now)
        report = manager.autonomy.pressure_report()
        messages = ' '.join(
            '%s %s %s' % (item.get('source', ''), item.get('message', ''), item.get('text', ''))
            for item in list(report['errors']) + list(report['items']))
        assert 'registry down' in messages
        assert 'intention down' in messages
        assert 'RuntimeError' in messages


def test_turning_a_pressure_source_off_stops_its_pressure_and_its_hold_in_yolo(tmp_path):
    for signal in SIGNALS:
        definition = default_definition()
        rule = dict(next(item for item in definition['rules'] if item['id'] == 'host-steady'))
        rule.update(id='probe', name='Probe', signal=signal, emit='each', when='true',
                    wake=True, template='probe', guard=False)
        definition['rules'] = [rule]
        definition['sources'][signal] = False
        assert evaluate(definition, {signal: [{}]}, mode='yolo') == []
        assert evaluate(definition, {signal: [{}]}, mode='safe') == []
        definition['sources'][signal] = True
        hits = evaluate(definition, {signal: [{}]}, mode='yolo')
        assert [hit['text'] for hit in hits] == ['probe']
    definition = default_definition()
    definition['sources']['board_card'] = False
    brief = {'cards': [{
        'ordinal': 3, 'status': 'queued', 'version': 1, 'title': 'Open', 'card_id': 'a', 'owner': '',
    }]}
    assert coordination_offers(brief, 3, {}, now=1, definition=definition) == ([], {})
    assert board_pressure_lines(brief, definition=definition, mode='yolo') == []
    definition['sources']['board_card'] = True
    lines, record = coordination_offers(brief, 3, {}, now=1, definition=definition)
    assert lines and record
    definition['sources']['session'] = False
    exported = export_text(definition, 'toml')
    assert 'session = false' in exported
    assert import_text(exported)['sources']['session'] is False
    refused = default_definition()
    refused['sources']['made-up'] = False
    with pytest.raises(FloorDefinitionError) as caught:
        save_definition(Config(home=tmp_path), refused)
    assert caught.value.field == 'sources'
    assert 'made-up' in str(caught.value)
    assert not (tmp_path / 'floor-definition.json').exists()


def test_each_failed_reading_is_a_visible_floor_error(tmp_path, monkeypatch):
    from test_autonomy import open_manager
    manager, _slug, _clock, _backend = open_manager(tmp_path, [])
    manager.config.board.enabled = True
    manager.config.infrastructure.pressure_engines = [{
        'host': 'engine.example', 'port': 9, 'model': 'scripted', 'slots': 1,
    }]

    def _approval():
        raise RuntimeError('approval unread')

    def _coordination():
        raise RuntimeError('coordination unread')

    def _board():
        raise RuntimeError('board unread')

    def _infrastructure():
        raise RuntimeError('infrastructure unread')

    def _snapshot(*_args, **_kwargs):
        raise RuntimeError('engine unread')

    monkeypatch.setattr(manager, 'approval_status', _approval)
    monkeypatch.setattr(manager, 'coordination_status', _coordination)
    monkeypatch.setattr(manager, 'board_brief', _board)
    monkeypatch.setattr(manager.infrastructure, 'status', _infrastructure)
    monkeypatch.setattr('tessera.engine_pressure.snapshot', _snapshot)
    expected = {
        'approval-status': 'approval unread',
        'coordination-status': 'coordination unread',
        'infrastructure-status': 'infrastructure unread',
        'board-brief': 'board unread',
        'engine-snapshot': 'engine unread',
    }
    with manager:
        report = manager.autonomy.pressure_report()
        visible = ' '.join(
            '%s %s %s' % (item.get('source', ''), item.get('message', ''), item.get('text', ''))
            for item in list(report['errors']) + list(report['items']))
        for source, message in expected.items():
            assert source in visible
            assert message in visible
        assert report['approval_mode'] == 'unknown'
        assert 'Safe' not in visible
        manager.autonomy.state['floor_errors'] = []
        manager.autonomy.floor_dry_run('host-steady')
        dry_sources = {item['source'] for item in manager.autonomy.state['floor_errors']}
        assert 'infrastructure-status' in dry_sources
        assert 'engine-snapshot' in dry_sources
        definition = default_definition()
        for rule in definition['rules']:
            if rule['id'] == 'visible-error':
                rule['guard'] = True
                rule['template'] = 'GUARD {source}'
        save_definition(manager.config, definition)
        guarded = manager.autonomy.pressure_report()
        assert guarded['approval_mode'] == 'unknown'
        assert any('GUARD' in (item.get('text') or '') for item in guarded['items'])
        assert any(item.get('source') == 'approval-status' for item in guarded['errors'])
        floor = manager.autonomy.floor_view()
        shown = ' '.join(
            '%s %s' % (item.get('source', ''), item.get('message', '')) for item in floor['errors'])
        assert 'approval unread' in shown
        assert floor['approval_mode'] == 'unknown'


def test_a_saved_invalid_file_is_refused_rather_than_reverted(tmp_path):
    config = Config(home=tmp_path)
    path = tmp_path / 'floor-definition.json'
    path.write_text('{', encoding='utf-8')
    with pytest.raises(FloorDefinitionError) as caught:
        load_definition(config)
    assert caught.value.field == 'file'
    assert 'JSON' in str(caught.value)


def test_a_runtime_failure_is_skipped_and_refused_when_saved(tmp_path):
    """A condition can validate and still fail when a row is evaluated."""
    from test_autonomy import config_for, install_fake_clock
    from test_server import call, running_server
    from tessera.sessions import SessionManager
    from fake_backend import FakeBackend
    definition = default_definition()
    for rule in definition['rules']:
        if rule['id'] == 'board-unfiled':
            rule['when'] = 'title < 5'
    with running_server(tmp_path) as server:
        status, refused = call(server, 'PUT', '/api/autonomy/floor-definition', {'definition': definition})
        assert status == 400
        assert refused['error']
        assert 'board-unfiled' in refused['error']
        assert 'when' in refused['error']
        assert not (tmp_path / 'floor-definition.json').exists()
        status, imported = call(server, 'POST', '/api/autonomy/floor-definition/import',
                                {'text': json.dumps(definition)})
        assert status == 400
        assert 'board-unfiled' in imported['error']
        assert 'when' in imported['error']
    path = tmp_path / 'floor-definition.json'
    path.write_text(json.dumps(definition), encoding='utf-8')
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=0)
    config.board.enabled = True
    backend = FakeBackend(['noted'])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Open work', slug, 0, 0, owner='')
        manager.autonomy.state['timers']['due-timer'] = {
            'id': 'due-timer', 'slug': slug, 'note': 'ping',
            'due': clock.now - 5, 'repeat': None, 'created': clock.now - 10,
        }
        manager.autonomy.tick()
        held = manager.autonomy.state['held'].get(slug) or []
        assert not any(event.get('kind') == 'timer' for event in held)
        assert any('timer' in (wake.get('kinds') or []) for wake in manager.autonomy.state['wakes'])
        report = manager.autonomy.pressure_report()
        visible = ' '.join(
            '%s %s %s' % (item.get('source', ''), item.get('message', ''), item.get('text', ''))
            for item in list(report['errors']) + list(report['items']))
        assert 'definition-rule:board-unfiled' in visible
        assert 'title < 5' in visible
        assert report.get('definition_error')


def test_rules_are_selected_by_emit_signal_and_fields():
    source = (Path(__file__).resolve().parents[1] / 'tessera' / 'autonomy.py').read_text(encoding='utf-8')
    for pinned in ('coordination-offer', 'floor-dispatch', 'session-pressure'):
        assert pinned not in source
    definition = default_definition()
    for rule in definition['rules']:
        if rule['emit'] == 'offers' and rule.get('sort') == 'priority':
            rule['id'] = 'renamed-offers'
        if rule['signal'] == 'session' and rule['emit'] == 'each':
            rule['id'] = 'renamed-session'
    lines, _ = coordination_offers({'cards': [{
        'ordinal': 1, 'status': 'queued', 'version': 1, 'title': 'Open', 'card_id': 'a', 'owner': '',
    }]}, 1, {}, now=1, definition=definition)
    assert lines and 'Open' in lines[0]
    hits = evaluate(definition, {'session': [{
        'slug': 's', 'busy': True, 'is_floor': False, 'turn_created': 't', 'activity': '', 'voice': False,
    }]})
    assert [hit['rule'] for hit in hits if hit['signal'] == 'session'] == ['renamed-session']


def test_rank_pressure_uses_the_steady_flag():
    from tessera.autonomy import rank_pressure
    steady = 'Pressure: hosts are steady. Do not invent work. Do not continue the last wake.'
    ranked = rank_pressure([
        (1.4, steady, False),
        (0, steady, True),
        {'score': 1.8, 'text': 'real change', 'steady': False},
    ])
    assert [line for _score, line in ranked] == ['real change', steady]


def test_pressure_is_an_editable_rule_field():
    from tessera.floor_definition import compose_wake
    disk = next(rule for rule in default_definition()['rules'] if rule['id'] == 'host-disk')
    offer = next(rule for rule in default_definition()['rules'] if rule['emit'] == 'offers')
    assert disk['pressure'] is True
    assert offer['pressure'] is False
    hit = {'rule': 'host-disk', 'signal': 'host', 'wake': True, 'steady': False,
           'pressure': False, 'score': 1.4, 'text': 'disk'}
    kind, _text, rules = compose_wake(default_definition(), Config(), [hit], '')
    assert kind == 'coordination' and rules == ['host-disk']
    hit['pressure'] = True
    kind, _text, rules = compose_wake(default_definition(), Config(), [hit], '')
    assert kind == 'pressure' and rules == ['host-disk']


def _reference_floor():
    path = Path(__file__).resolve().parent / 'fixtures' / 'floor_reference_lines.py'
    namespace = {}
    exec(path.read_text(encoding='utf-8'), namespace)
    return namespace


def test_shipped_defaults_match_main_for_the_same_inputs():
    """The shipped document reproduces the reference floor lines, including kiosk and P-tags."""
    namespace = _reference_floor()
    report = {'hosts': [{'ordinal': 1, 'label': 'Kiosk', 'health': 'unreachable'}]}
    assert pressure_lines(report, {}, 0) == namespace['pressure_lines'](report, {}, 0)
    plain = {'hosts': [{'ordinal': 2, 'label': 'north', 'health': 'unreachable'}]}
    assert pressure_lines(plain, {}, 0) == namespace['pressure_lines'](plain, {}, 0)
    declared_off = {'hosts': [{'ordinal': 1, 'label': 'Kiosk', 'health': 'degraded'}]}
    previous = {'Kiosk': {'health': 'healthy', 'disk_low': False}}
    thresholds = {1: {'managed': False}}
    assert pressure_lines(declared_off, previous, 1, thresholds) == namespace['pressure_lines'](
        declared_off, previous, 1, thresholds)
    disk = {'hosts': [{'ordinal': 1, 'label': 'north', 'health': 'healthy',
                       'metrics': {'disk_free_bytes': 4, 'disk_total_bytes': 100}}]}
    assert pressure_lines(disk, {}, 0) == namespace['pressure_lines'](disk, {}, 0)
    brief = {'cards': [
        {'ordinal': 3, 'status': 'queued', 'project': '', 'age_seconds': 10},
        {'ordinal': 9, 'status': 'queued', 'project': 'alpha', 'age_seconds': 7 * 3600},
        {'ordinal': 4, 'status': 'running', 'project': 'alpha', 'age_seconds': 10,
         'execution': {'session': 'gone'}},
    ], 'sessions': ['other']}
    assert board_pressure_lines(brief) == namespace['board_pressure_lines'](brief)
    cards = {'cards': [
        {'ordinal': 1, 'status': 'queued', 'version': 1, 'project': '', 'title': 'P1. First', 'card_id': 'a'},
        {'ordinal': 6, 'status': 'queued', 'version': 1, 'project': 'alpha', 'title': 'Filed work', 'card_id': 'b'},
        {'ordinal': 2, 'status': 'queued', 'version': 1, 'project': '', 'title': 'Second', 'card_id': 'c'},
        {'ordinal': 3, 'status': 'queued', 'version': 1, 'project': '', 'title': 'Third', 'card_id': 'd'},
    ]}
    assert coordination_offers(cards, 4, {}, now=1)[0] == namespace['coordination_offers'](cards, 4, {}, now=1)[0]
    assert [line.split()[2] for line in coordination_offers(cards, 3, {}, now=1)[0]] == ['#1', '#6', '#2']


def test_every_advertised_signal_is_read(tmp_path, monkeypatch):
    from fake_backend import FakeBackend
    from test_autonomy import config_for, install_fake_clock
    from tessera.sessions import SessionManager
    config = config_for(tmp_path)
    config.board.enabled = True
    backend = FakeBackend([])
    manager = SessionManager(config, backend_factory=lambda *args: backend)
    clock = install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Owned card', slug, 0, 0, owner=slug)
        manager.autonomy.state['endpoint_health'] = {'probe': 'up'}
        manager.autonomy.state['timers']['t'] = {
            'id': 't', 'slug': slug, 'note': 'n', 'due': clock.now + 10,
            'repeat': None, 'created': clock.now,
        }
        manager.autonomy.learning.park_question(slug, 'where next')
        reading = manager.autonomy._floor_reading({}, {}, clock.now)
        assert reading['rows']['endpoint'][0]['state'] == 'up'
        assert reading['rows']['timer'][0]['id'] == 't'
        assert any(row['kind'] == 'question' and row['text'] == 'where next'
                   for row in reading['rows']['agenda_item'])
        assert any(row['slug'] == slug and 'Owned card' in row['text']
                   for row in reading['rows']['session_card'])
        assert 'engine' in reading['rows']

        def _agenda():
            raise RuntimeError('agenda unread')

        monkeypatch.setattr(manager.autonomy.learning, 'wake_targets', _agenda)
        report = manager.autonomy.pressure_report()
        visible = ' '.join('%s %s' % (item.get('source', ''), item.get('message', ''))
                           for item in report['errors'])
        assert 'agenda-items' in visible
        assert 'agenda unread' in visible


def test_a_bad_audit_line_and_a_broken_world_view_file_are_visible(tmp_path):
    from tessera.floor_definition import peek_world_view, read_audit
    from tessera.settings_model import SettingsService, project
    from test_autonomy import open_manager
    config = Config(home=tmp_path)
    save_definition(config, default_definition(), actor='ada')
    from tessera.floor_definition import audit_path
    path = audit_path(config)
    with path.open('a', encoding='utf-8') as handle:
        handle.write('not json\n')
    entries = read_audit(config)
    assert any(entry.get('action') == 'error' and 'not valid JSON' in entry.get('error', '') for entry in entries)
    manager, _slug, _clock, _backend = open_manager(tmp_path, [])
    with manager:
        view = manager.autonomy.floor_view()
        assert any(item.get('source') == 'floor-audit' for item in view['errors'])
        (tmp_path / 'floor-definition.json').write_text('{', encoding='utf-8')
        (tmp_path / 'config.toml').write_text('[autonomy]\nworld_view = "kept"\n', encoding='utf-8')
        with pytest.raises(FloorDefinitionError) as peeked:
            peek_world_view(manager.config)
        assert peeked.value.field == 'file'
        service = SettingsService(manager)
        with pytest.raises(ValueError) as saved:
            service.apply({'id': 'autonomy.world_view', 'action': 'set', 'value': 'new text'})
        assert 'JSON' in str(saved.value)
        stored = (tmp_path / 'config.toml').read_text(encoding='utf-8')
        assert 'kept' in stored
        assert 'new text' not in stored
        section = next(item for item in project(manager.config, {})['sections'] if item['id'] == 'autonomy')
        field = next(item for item in section['fields'] if item['id'] == 'autonomy.world_view')
        assert 'JSON' in field['status']


def test_a_bad_token_names_the_rule_the_field_and_the_token(tmp_path):
    definition = default_definition()
    definition['rules'][0]['when'] = '1.2.3 == 1'
    with pytest.raises(FloorDefinitionError) as caught:
        save_definition(Config(home=tmp_path), definition)
    assert caught.value.rule_id == definition['rules'][0]['id']
    assert caught.value.field == 'when'
    assert '1.2.3' in str(caught.value)


def test_preview_renders_the_draft_world_view(tmp_path):
    from test_autonomy import open_manager
    manager, _slug, _clock, _backend = open_manager(tmp_path, [], world_view='from settings')
    definition = default_definition()
    definition['compose']['world_view'] = 'from the draft'
    with manager:
        preview = manager.autonomy.floor_preview(definition)
        assert 'from the draft' in preview['world_view']
        assert 'from settings' not in preview['world_view']
        definition['compose']['world_view'] = ''
        empty = manager.autonomy.floor_preview(definition)
        assert 'from settings' not in empty['world_view']
        assert 'What matters to you' not in empty['world_view']


def test_the_first_save_does_not_log_the_shipped_rules_as_added(tmp_path):
    from tessera.floor_definition import read_audit
    config = Config(home=tmp_path)
    save_definition(config, default_definition(), actor='ada')
    changes = read_audit(config)[-1]['changes']
    assert not any(change.get('change') == 'added' for change in changes)
    edited = default_definition()
    edited['rules'][0]['note'] = 'tuned'
    save_definition(config, edited, actor='ada')
    second = read_audit(config)[-1]['changes']
    assert {'rule': edited['rules'][0]['id'], 'change': 'edited'} in second
    assert not any(change.get('change') == 'added' for change in second)


def test_evaluate_validates_a_definition_once(monkeypatch):
    import tessera.floor_definition as floor
    calls = []
    original = floor.validate_definition

    def wrapped(raw):
        calls.append(raw.get('source'))
        return original(raw)

    monkeypatch.setattr(floor, 'validate_definition', wrapped)
    raw = default_definition()
    raw['source'] = 'editor'
    evaluate(raw, {})
    assert calls == ['editor']
    calls.clear()
    marked = original(default_definition())
    evaluate(marked, {})
    assert calls == []


def test_a_rule_error_survives_a_full_tick(tmp_path):
    """A rule that fails stays in floor_errors after tick and in the settings GET."""
    import threading
    from fake_backend import FakeBackend
    from tessera.server import create_server
    from tessera.sessions import SessionManager
    from test_autonomy import config_for, install_fake_clock
    from test_server import call
    definition = default_definition()
    for rule in definition['rules']:
        if rule['id'] == 'board-unfiled':
            rule['when'] = 'title < 5'
    (tmp_path / 'floor-definition.json').write_text(json.dumps(definition), encoding='utf-8')
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=0)
    config.board.enabled = True
    config.server.port = 0
    manager = SessionManager(config, backend_factory=lambda *args: FakeBackend(['noted']))
    install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    with manager:
        manager.board.create('Open work', slug, 0, 0, owner='')
        manager.autonomy.tick()
        stored = manager.autonomy.state['floor_errors']
        assert any(item.get('source') == 'definition-rule:board-unfiled' and 'title < 5' in item.get('message', '')
                   for item in stored)
        server = create_server(config, manager=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, body = call(server, 'GET', '/api/autonomy/floor-definition')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert status == 200
        assert any(item.get('source') == 'definition-rule:board-unfiled' and 'title < 5' in item.get('message', '')
                   for item in body['errors'])
        assert any(item.get('source') == 'definition-rule:board-unfiled'
                   for item in manager.autonomy.state['floor_errors'])


def _open_failing_unfiled(tmp_path):
    from fake_backend import FakeBackend
    from tessera.sessions import SessionManager
    from test_autonomy import config_for, install_fake_clock
    definition = default_definition()
    for rule in definition['rules']:
        if rule['id'] == 'board-unfiled':
            rule['when'] = 'title < 5'
    (tmp_path / 'floor-definition.json').write_text(json.dumps(definition), encoding='utf-8')
    config = config_for(tmp_path, min_wake_gap_seconds=0, self_prompt_gap_seconds=0)
    config.board.enabled = True
    manager = SessionManager(config, backend_factory=lambda *args: FakeBackend(['noted', 'noted', 'noted', 'noted']))
    install_fake_clock(manager)
    slug = manager.create('Home')['slug']
    return manager, slug, definition


def _rule_failures(errors, rule_id='board-unfiled'):
    source = 'definition-rule:' + rule_id
    return [item for item in errors or [] if item.get('source') == source]


def _settle_floor(manager):
    """The first pressure wake occupies the floor session. The next tick reads only once it is idle."""
    floor = manager.config.autonomy.infinity_slug
    assert manager.wait(floor, timeout=10)


def test_deleting_a_rule_clears_its_error_on_the_next_tick(tmp_path):
    manager, slug, definition = _open_failing_unfiled(tmp_path)
    with manager:
        manager.board.create('Open work', slug, 0, 0, owner='')
        manager.autonomy.tick()
        assert _rule_failures(manager.autonomy.state['floor_errors'])
        _settle_floor(manager)
        definition['rules'] = [rule for rule in definition['rules'] if rule['id'] != 'board-unfiled']
        (tmp_path / 'floor-definition.json').write_text(json.dumps(definition), encoding='utf-8')
        manager.autonomy.tick()
        assert not _rule_failures(manager.autonomy.state['floor_errors'])
        assert not _rule_failures(manager.autonomy.floor_view()['errors'])
        assert manager.autonomy.floor_preview()['kind'] != 'pressure'


def test_disabling_a_rule_in_yolo_clears_its_error_and_the_pressure_wake(tmp_path):
    import threading
    from tessera.server import create_server
    from test_server import call
    manager, slug, definition = _open_failing_unfiled(tmp_path)
    with manager:
        manager.board.create('Open work', slug, 0, 0, owner='')
        manager.set_yolo(True)
        manager.autonomy.tick()
        assert _rule_failures(manager.autonomy.state['floor_errors'])
        assert manager.autonomy.floor_preview()['kind'] == 'pressure'
        for rule in definition['rules']:
            if rule['id'] == 'board-unfiled':
                rule['enabled'] = False
        manager.config.server.port = 0
        server = create_server(manager.config, manager=manager)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, saved = call(server, 'PUT', '/api/autonomy/floor-definition', {'definition': definition})
            assert status == 200
            assert not _rule_failures(saved['errors'])
            status, viewed = call(server, 'GET', '/api/autonomy/floor-definition')
            assert status == 200
            assert not _rule_failures(viewed['errors'])
            status, preview = call(server, 'POST', '/api/autonomy/floor-definition/preview',
                                    {'definition': definition})
            assert status == 200
            assert preview['kind'] != 'pressure'
            _settle_floor(manager)
            for _ in range(3):
                manager.autonomy.tick()
                _settle_floor(manager)
            status, viewed = call(server, 'GET', '/api/autonomy/floor-definition')
            assert status == 200
            assert not _rule_failures(viewed['errors'])
            status, preview = call(server, 'POST', '/api/autonomy/floor-definition/preview',
                                    {'definition': definition})
            assert status == 200
            assert preview['kind'] != 'pressure'
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_turning_a_rules_source_off_clears_its_error(tmp_path):
    manager, slug, definition = _open_failing_unfiled(tmp_path)
    with manager:
        manager.board.create('Open work', slug, 0, 0, owner='')
        manager.autonomy.tick()
        assert _rule_failures(manager.autonomy.state['floor_errors'])
        _settle_floor(manager)
        definition['sources']['board_card'] = False
        (tmp_path / 'floor-definition.json').write_text(json.dumps(definition), encoding='utf-8')
        manager.autonomy.tick()
        assert not _rule_failures(manager.autonomy.state['floor_errors'])
        assert not _rule_failures(manager.autonomy.floor_view()['errors'])
        assert manager.autonomy.floor_preview()['kind'] != 'pressure'


def test_an_offers_rule_fires_on_its_own_signal(tmp_path):
    """An offers rule reads the rows of its own signal, whichever signal that is."""
    from test_autonomy import open_manager
    source = (Path(__file__).resolve().parents[1] / 'tessera' / 'autonomy.py').read_text(encoding='utf-8')
    assert "signal='board_card'" not in source
    manager, slug, clock, _backend = open_manager(tmp_path, [], min_wake_gap_seconds=0)
    definition = default_definition()
    base = next(rule for rule in definition['rules'] if rule['signal'] == 'session' and rule['emit'] == 'each')
    offer = dict(base)
    offer.update(id='busy-session-offer', name='Offer a busy session', emit='offers', sort='input',
                 template='Session offer: {slug}', pressure=False)
    priority = dict(offer)
    priority.update(id='busy-session-priority', sort='priority', template='Priority session offer: {slug}')
    definition['rules'].extend([offer, priority])
    save_definition(manager.config, definition, 'operator')
    with manager:
        runtime = manager.runtime(slug)
        with runtime.condition:
            runtime.active = {'id': 'active-proof', 'text': 'Working', 'created': '2026-10-22T01:00:00Z'}
            runtime.session._in_turn.set()
        try:
            floor = manager.config.autonomy.infinity_slug
            manager.autonomy._queue_floor_wake(clock.now, 1)
            held = manager.autonomy.state['held'][floor]
            text = held[0]['text']
            assert 'Session pressure: ' + slug in text
            assert 'Session offer: ' + slug in text
            assert 'Priority session offer: ' + slug in text
        finally:
            with runtime.condition:
                runtime.active = None
                runtime.session._in_turn.clear()


def test_an_unhashable_host_ordinal_is_a_visible_error(tmp_path, monkeypatch):
    from tessera.floor_signals import host_rows
    from test_autonomy import open_manager
    with pytest.raises(ValueError) as raised:
        host_rows({'hosts': [{'ordinal': ['x'], 'label': 'north', 'health': 'healthy'}]}, {}, {1: {'managed': True}})
    assert 'unhashable ordinal' in str(raised.value)
    recorded = []
    rows = host_rows(
        {'hosts': [
            {'ordinal': ['x'], 'label': 'north', 'health': 'healthy'},
            {'ordinal': 2, 'label': 'south', 'health': 'healthy'},
        ]},
        {},
        {2: {'managed': True}},
        on_error=recorded.append,
    )
    assert recorded and 'unhashable ordinal' in str(recorded[0])
    assert next(row for row in rows if row['label'] == 'south')['declared_managed'] is True
    assert next(row for row in rows if row['label'] == 'north')['declared_managed'] is None
    manager, _slug, _clock, _backend = open_manager(tmp_path, [])
    with manager:
        monkeypatch.setattr(
            manager.autonomy, '_infrastructure_report',
            lambda: {'hosts': [{'ordinal': ['x'], 'label': 'north', 'health': 'unreachable'}]},
        )
        report = manager.autonomy.pressure_report()
        assert any(item.get('source') == 'host-ordinal' and 'unhashable ordinal' in item.get('message', '')
                   for item in report['errors'])


def test_the_named_copy_does_not_narrate_history_or_use_the_host_name():
    root = Path(__file__).resolve().parents[1]
    assert 'used to be inferred' not in (root / 'docs' / 'floor-definition.md').read_text(encoding='utf-8')
    assert 'H14:' not in (root / 'tests' / 'test_learning.py').read_text(encoding='utf-8')
    consumers = (root / 'tessera' / 'settings_consumers.py').read_text(encoding='utf-8')
    assert 'Card 124' not in consumers
    assert 'readerless' not in consumers
    banned = 'Spa' + 'rk'
    for name in ('tests/test_floor_definition.py', 'tests/test_autonomy.py'):
        assert banned not in (root / name).read_text(encoding='utf-8')
