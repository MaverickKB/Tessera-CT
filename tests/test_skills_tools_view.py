"""A person answers what this agent can do, and what it will ask about, from the page.

The page reads /api/skills and /api/tools. Shelf skills and engine tools stay
invisible, and the risk flag that decides approval stays buried in tools.py,
unless those routes exist.
"""

import json
from pathlib import Path
import threading

from tessera.config import load_config
from tessera.server import create_server
from tessera.settings_model import skills_view, tools_view
from test_server import call


HOME = '''
[skills]
enabled = true

[tools]
approval = "deny_risky"

[backends.fake]
base_url = "http://127.0.0.1:9/v1"
model = "scripted-local-fake"
chat = true
enabled = true
'''


def serve(tmp_path, text=HOME):
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join()


def _write_skill(home, name, description, body='Do the thing.\n'):
    directory = home / 'skills' / name
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: {description}\n---\n{body}', encoding='utf-8')


def test_the_skills_route_lists_what_is_on_the_shelf(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    _write_skill(home, 'a-real-skill', 'Does a real thing')
    _write_skill(home, 'another-skill', 'Does another thing')
    config = load_config(home)
    view = skills_view(config, home)
    assert view['enabled'] is True
    assert view['count'] == 2
    names = [row['name'] for row in view['skills']]
    assert names == ['a-real-skill', 'another-skill']
    assert all(row['source'] == 'user' for row in view['skills'])
    assert all(not row['error'] for row in view['skills'])


def test_a_broken_skill_is_listed_with_its_error_not_dropped(tmp_path):
    """A malformed SKILL.md is exactly what a person needs to see."""
    home = tmp_path / 'home'
    home.mkdir()
    _write_skill(home, 'good-one', 'Fine')
    broken = home / 'skills' / 'broken-one'
    broken.mkdir(parents=True)
    (broken / 'SKILL.md').write_text('no frontmatter here', encoding='utf-8')
    config = load_config(home)
    view = skills_view(config, home)
    rows = {row['name']: row for row in view['skills']}
    assert 'broken-one' in rows
    assert 'frontmatter' in rows['broken-one']['error']
    assert not rows['good-one']['error']


def test_the_skills_route_says_so_when_the_shelf_is_off(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    _write_skill(home, 'a-skill', 'Present but disabled')
    (home / 'config.toml').write_text('[skills]\nenabled = false\n')
    config = load_config(home)
    view = skills_view(config, home)
    assert view['enabled'] is False and view['loaded'] is False
    assert view['count'] == 1
    assert view['skills'][0]['name'] == 'a-skill'
    assert view['skills'][0]['offered'] is False
    assert 'off' in view['note']
    assert any(row['source'] == 'user' and not row['error'] for row in view['directories'])


def test_the_tools_route_reports_risk_and_the_live_outcome(tmp_path):
    """Three outcomes, not two: runs, asks, refused.

    The registry's own rule: a risky tool runs under yolo/never or when an allow-rule
    names it, asks under `ask`, and is refused under `deny_risky`. Calling all three
    "asks" would reassure a reader about a tool that will actually be blocked.
    """
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(HOME)
    config = load_config(home)

    denied = tools_view(config, {'mode': 'deny_risky'})
    assert denied['policy'] == 'deny_risky'
    assert denied['risky'] > 0
    outcomes = {row['name']: row['outcome'] for row in denied['tools']}
    assert outcomes['shell'] == 'refused'
    assert outcomes['read_file'] == 'runs'

    asked = tools_view(config, {'mode': 'ask'})
    asked_outcomes = {row['name']: row['outcome'] for row in asked['tools']}
    assert asked_outcomes['shell'] == 'asks'
    assert sum(1 for row in asked['tools'] if row['asks']) == asked['risky']

    allowed = tools_view(config, {'mode': 'yolo'})
    assert all(row['outcome'] == 'runs' for row in allowed['tools'])


def test_the_live_policy_wins_over_the_configured_one(tmp_path):
    """The config.toml says deny_risky while the switch says YOLO.

    The page must report what actually happens, not what the file says, or it would
    understate the tools that run without a prompt.
    """
    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.toml').write_text(HOME)
    config = load_config(home)
    assert config.tools.approval == 'deny_risky'
    view = tools_view(config, {'mode': 'yolo', 'configured': 'deny_risky'})
    assert view['policy'] == 'yolo'
    assert all(row['outcome'] == 'runs' for row in view['tools'])


def test_both_routes_are_served_and_read_only(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/skills')
        assert code == 200, body
        assert 'skills' in body and 'count' in body

        code, body = call(server, 'GET', '/api/tools')
        assert code == 200, body
        assert 'tools' in body and 'policy' in body
        assert body['policy'] == server.manager.approval_status()['mode']

        before = (tmp_path / 'config.toml').read_text()
        for path in ('/api/skills', '/api/tools'):
            code, body = call(server, 'POST', path, {})
            assert code == 400, body
        assert (tmp_path / 'config.toml').read_text() == before
    finally:
        stop(server, thread)


def test_the_tools_route_names_its_source_rather_than_implying_completeness(tmp_path):
    """No session is loaded in this probe, so the answer must say it is the base set.

    Board, swarm, autonomy and computer-use tools register per session; claiming the
    base registry is the whole picture would understate what the agent can do.
    """
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/tools')
        assert code == 200
        assert 'source' in body
        assert 'base registry' in body['source'] or 'live session' in body['source']
    finally:
        stop(server, thread)
