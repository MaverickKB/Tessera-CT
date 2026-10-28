"""The action gate must be switchable from Settings, and it must be the switch
that actually gates.

Why this test exists: config.jev.action_gate_enabled is what loop.py consults
before running judge_action() on a mutating tool (loop.py:1077). It was set true
in the live config and had no entry in the settings catalog at all, so the only
way to turn it off was to edit config.toml by hand. Meanwhile the settings page
offered jev.enabled with the hint "Off means no judge gating", which is not the
switch that gates -- jev.enabled controls the judge backend, not the gate.

The direction: make this configurable in settings, on or off, then
disable it, because a blanket safety that blocks every mutation is far past the
intent of stopping an action that would take the house offline.

These tests pin the three properties that keep that from drifting again:
1. the setting exists and round-trips through the settings write path;
2. turning it off actually stops the gate from consulting the judge;
3. the hint names the gate, so a reader is not told a different switch controls it.
"""

import subprocess
from pathlib import Path

import pytest

from tessera.action_judge import DENY_PROBABILITY
from tessera.config import load_config
from tessera.jev import JudgeKit
from tessera.settings_model import catalog, project
from tessera.sessions import Session
from fake_backend import FakeBackend
from test_action_judge import evaluator
from test_loop import session_config
from test_server import call


GATE_SETTING = 'jev.action_gate_enabled'

ROOT = Path(__file__).resolve().parents[1]


def spec_by_id(config, approval):
    return {spec.id: spec for spec, _value in catalog(config, approval)}


def test_the_action_gate_is_in_the_settings_catalog():
    config = session_config('/tmp')
    specs = spec_by_id(config, {'yolo': True})
    assert GATE_SETTING in specs, (
        "config.jev.action_gate_enabled gates every mutating tool (loop.py:1077) "
        "but is absent from the settings catalog, so it can only be changed by "
        "editing config.toml by hand")
    spec = specs[GATE_SETTING]
    assert spec.type == 'bool', "on or off, as directed"
    assert spec.path == ('jev', 'action_gate_enabled'), "it must write the field loop.py reads"
    assert spec.section == 'judge', "it belongs with the other Jev judge settings"


def test_the_hint_names_the_gate_and_not_a_different_switch():
    config = session_config('/tmp')
    specs = spec_by_id(config, {'yolo': True})
    hint = specs[GATE_SETTING].hint.lower()
    assert 'gate' in hint, (
        "the hint must say this is the action gate; jev.enabled's hint currently "
        "claims 'Off means no judge gating', which is a different switch")
    assert 'risky' in hint or 'mutating' in hint or 'action' in hint


def test_jev_enabled_hint_no_longer_claims_it_controls_gating():
    """jev.enabled turns the judge backend on. It does not stop the gate."""
    config = session_config('/tmp')
    specs = spec_by_id(config, {'yolo': True})
    hint = specs['jev.enabled'].hint.lower()
    assert 'no judge gating' not in hint, (
        "jev.enabled does not disable gating: action_gate_enabled does. The old "
        "hint told a reader the opposite")


def test_the_gate_applies_live_not_on_restart(tmp_path):
    """commit() -> adopt_config() refreshes every live session's config in place,
    and loop.py reads the gate per tool call, so this switch takes effect on the
    next call. Reporting 'restart' would tell the operator to reload for nothing."""
    from test_settings_model import serve, stop

    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': GATE_SETTING, 'value': True})
        assert code == 200, body
        assert body['changed'] is True
        assert body['applied'] == 'live', (
            "the action gate is read per tool call from session.config, and commit() "
            "adopts the reloaded config in-process; claiming 'restart' is a false "
            "instruction to the operator")
        assert body['restart_required'] is False
        assert server.manager.config.jev.action_gate_enabled is True, (
            "the running core must hold the new value without a restart")
    finally:
        stop(server, thread)


def test_the_setting_round_trips_through_the_settings_write_path(tmp_path):
    from test_settings_model import serve, stop

    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': GATE_SETTING, 'value': False})
        assert code == 200, body
        assert load_config(tmp_path).jev.action_gate_enabled is False
        code, body = call(server, 'POST', '/api/settings', {'id': GATE_SETTING, 'value': True})
        assert code == 200, body
        assert load_config(tmp_path).jev.action_gate_enabled is True
    finally:
        stop(server, thread)


@pytest.mark.parametrize('gate_on, blocked', [(True, True), (False, False)])
def test_off_means_the_gate_does_not_consult_the_judge(tmp_path, gate_on, blocked):
    """The behavioural half: with the gate off, a mutation the judge would refuse
    still runs, and the judge is never asked."""
    config = session_config(tmp_path)
    config.jev.action_gate_enabled = gate_on
    config.tools.approval = 'yolo'
    judge = JudgeKit(enabled=True, action_gate_enabled=gate_on,
                     evaluator=evaluator({'disrupts_reasoning': DENY_PROBABILITY + 0.05}))
    with Session(config, backend=FakeBackend([]), router=judge) as session:
        session._action_goal = 'Write the requested file.'
        session._call_tool({'id': 'action', 'name': 'write_file',
                            'arguments': {'path': 'receipt.txt', 'text': 'done'}})
        assert (tmp_path / 'receipt.txt').exists() is (not blocked)
        if gate_on:
            assert any(row['point'] == 'action_gate' for row in judge.decisions)
        else:
            assert not any(row['point'] == 'action_gate' for row in judge.decisions), (
                "with the gate off the judge must not be consulted for a mutation")


def test_the_live_config_field_is_the_one_the_loop_reads():
    """Guard against the two names drifting apart again."""
    source = subprocess.run(
        ['grep', '-n', 'action_gate_enabled', 'tessera/loop.py'],
        cwd=ROOT, capture_output=True, text=True).stdout
    assert 'config.jev.action_gate_enabled' in source, (
        "loop.py must keep reading config.jev.action_gate_enabled; if the field is "
        "renamed the settings entry silently stops controlling the gate")
