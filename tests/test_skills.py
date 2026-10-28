"""Skill shelf: load from the note, search past the cap, prompt and schema bytes stay put."""

import hashlib
import json

import pytest

from tessera.loop import Session
from tessera.skills import parse_skill_text
from fake_backend import FakeBackend

from test_loop import session_config


def turn_state(call):
    """The turn-state block, found by its prefix: its index moves with hook context."""
    return next(m['content'] for m in call['messages'] if str(m.get('content', '')).startswith('<turn-state>'))


SKILL = """---
name: {name}
description: {description}
---
Do the {name} procedure. {extra}
"""


def write_skill(root, name, description, extra=''):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(SKILL.format(name=name, description=description, extra=extra), encoding='utf-8')
    return folder


def prefix_hashes(session):
    schemas = json.dumps(session.tools.schemas(), sort_keys=True).encode()
    return hashlib.sha256(session.system_prompt.encode()).hexdigest(), hashlib.sha256(schemas).hexdigest()


def tool_outputs(backend):
    return [m.get('content', '') for call in backend.calls for m in call['messages'] if m.get('role') == 'tool']


def notes(session):
    return [m for m in session.log.iter() if m.kind == 'note' and m.meta.get('label') == 'skill-shelf']


def test_fake_model_loads_a_skill_named_in_the_session_start_note(tmp_path):
    write_skill(tmp_path / 'skills', 'desk-look', 'Use when looking at the desk screen.')
    backend = FakeBackend([
        {'tool_calls': [{'id': 'load', 'name': 'skill_load', 'arguments': {'name': 'desk-look'}}]},
        'Loaded.',
    ])
    with Session(session_config(tmp_path), backend=backend, compactor_backend=FakeBackend(['summary'])) as session:
        assert 'desk-look' not in session.system_prompt
        assert ('Procedures live on the skill shelf; search before house-specific or repeated work; load a skill '
                'before acting on a hit; a skill never restricts delegation.') in session.system_prompt
        [note] = notes(session)
        assert '- desk-look: Use when looking at the desk screen.' in note.text
        assert session.run_turn('Read the desk-look skill with tools.') == 'Loaded.'
        assert any('Do the desk-look procedure' in output for output in tool_outputs(backend))
        schemas = [s for s in session.tools.schemas() if s['function']['name'].startswith('skill_')]
        assert sorted(s['function']['name'] for s in schemas) == ['skill_load', 'skill_read', 'skill_search', 'skill_write']
        assert 'desk-look' not in json.dumps(session.tools.schemas())
    with Session(session_config(tmp_path), backend=FakeBackend([])) as again:
        assert len(notes(again)) == 1


def test_search_finds_a_skill_beyond_the_note_byte_cap(tmp_path):
    for index in range(80):
        write_skill(tmp_path / 'skills', f'skill-{index:02d}', f'Procedure number {index} with a long enough line.')
    write_skill(tmp_path / 'skills', 'zz-tail', 'The last one alphabetically.', 'Rotate the quokka keys.')
    config = session_config(tmp_path)
    config.skills.note_bytes = 1024
    with Session(config, backend=FakeBackend([])) as session:
        [note] = notes(session)
        assert len(note.text.encode()) <= 1024
        assert 'zz-tail' not in note.text
        assert 'more not listed; find them with skill_search' in note.text
        hit = session.tools.execute('skill_search', {'query': 'quokka'})
        assert hit.startswith('1 matched') and 'zz-tail' in hit
        assert session.tools.execute('skill_search', {'query': 'quokka'}) == hit


def test_editing_a_skill_leaves_prompt_and_schema_bytes_unchanged(tmp_path):
    write_skill(tmp_path / 'skills', 'desk-look', 'Use when looking at the desk screen.')
    config = session_config(tmp_path)
    with Session(config, backend=FakeBackend([])) as session:
        before = prefix_hashes(session)
        write_skill(tmp_path / 'skills', 'desk-look', 'Changed description.', 'New step.')
        write_skill(tmp_path / 'skills', 'brand-new', 'Added mid-session.')
        assert prefix_hashes(session) == before
        assert 'New step.' in session.tools.execute('skill_load', {'name': 'desk-look'})
    with Session(config, backend=FakeBackend([])) as fresh:
        assert prefix_hashes(fresh) == before


def test_skill_read_guards_path_traversal(tmp_path):
    folder = write_skill(tmp_path / 'skills', 'desk-look', 'Use when looking.')
    (folder / 'references').mkdir()
    (folder / 'references' / 'keys.md').write_text('Press F12.', encoding='utf-8')
    (tmp_path / 'secret.txt').write_text('nope', encoding='utf-8')
    (folder / 'escape').symlink_to(tmp_path / 'secret.txt')
    with Session(session_config(tmp_path), backend=FakeBackend([])) as session:
        assert 'references/keys.md' in session.tools.execute('skill_load', {'name': 'desk-look'})
        assert session.tools.execute('skill_read', {'name': 'desk-look', 'path': 'references/keys.md'}) == 'Press F12.'
        for bad in ('../../secret.txt', str(tmp_path / 'secret.txt'), 'escape', 'references/../../../secret.txt'):
            with pytest.raises(ValueError):
                session.tools.execute('skill_read', {'name': 'desk-look', 'path': bad})


def test_trusted_project_overrides_by_name_and_untrusted_is_ignored(tmp_path):
    home, project = tmp_path / 'home', tmp_path / 'project'
    write_skill(home / 'skills', 'deploy', 'User version.')
    write_skill(project / '.agents' / 'skills', 'deploy', 'Project version.')
    config = session_config(home)
    config.cwd = project
    with Session(config, backend=FakeBackend([])) as session:
        assert 'User version.' in session.tools.execute('skill_load', {'name': 'deploy'})
    config.skills.trusted_projects = [str(project)]
    with Session(config, backend=FakeBackend([])) as session:
        assert 'Project version.' in session.tools.execute('skill_load', {'name': 'deploy'})


def test_skill_write_never_prompts_and_writes_only_the_user_shelf(tmp_path):
    home, project = tmp_path / 'home', tmp_path / 'project'
    project.mkdir(parents=True)
    text = SKILL.format(name='learned', description='Use after a lesson.', extra='')
    for policy in ('ask', 'deny_risky', 'never', 'yolo'):
        asked = []
        backend = FakeBackend([
            {'tool_calls': [{'id': 'w', 'name': 'skill_write', 'arguments': {'name': 'learned', 'text': text}}]},
            'Done.',
        ])
        config = session_config(home)
        config.cwd = project
        config.skills.trusted_projects = [str(project)]
        config.tools.approval = policy
        config.jev.enabled = False
        with Session(config, backend=backend, approve=lambda name, args: asked.append(name) or False) as session:
            session.run_turn('Use the skill_write tool to save that lesson as a skill.')
        assert asked == [], policy
        assert (home / 'skills' / 'learned' / 'SKILL.md').read_text() == text, policy
        (home / 'skills' / 'learned' / 'SKILL.md').unlink()
    assert not (project / '.tessera').exists() and not (project / '.agents').exists()


def test_skill_write_validates_frontmatter(tmp_path):
    config = session_config(tmp_path)
    with Session(config, backend=FakeBackend([])) as session:
        for name, text in (('Bad', SKILL.format(name='Bad', description='x', extra='')),
                           ('good', SKILL.format(name='other', description='x', extra='')),
                           ('good', 'no frontmatter'),
                           ('good', '---\nname: good\n---\nno description')):
            with pytest.raises(ValueError):
                session.tools.execute('skill_write', {'name': name, 'text': text})
    assert not (tmp_path / 'skills').exists()
    assert parse_skill_text(SKILL.format(name='ok', description='Fine.', extra=''), 'ok')['description'] == 'Fine.'


def test_skill_tools_are_offered_on_every_route(tmp_path):
    write_skill(tmp_path / 'skills', 'desk-look', 'Use when looking.')
    config = session_config(tmp_path)
    config.tools.direct_route_tools = ['memory_recall']
    fast = FakeBackend([{'tool_calls': [{'id': 's', 'name': 'skill_search', 'arguments': {'query': 'desk'}}]}, 'Found it.'])
    main = FakeBackend(['Noted.'])
    shelf = ('skill_load', 'skill_read', 'skill_search', 'skill_write')
    with Session(config, backend=main, compactor_backend=FakeBackend(['s'])) as session:
        session.fast_backend = fast
        session.router.route = lambda *a, **k: {'route': 'direct'}
        assert 'desk-look' not in session.system_prompt
        assert 'desk-look' not in json.dumps(session.tools.schemas())
        assert session.run_turn('hi') == 'Found it.'
        direct = {t['function']['name'] for t in fast.calls[0]['tools']}
        assert set(shelf) <= direct
        assert any('desk-look' in output for output in tool_outputs(fast))
        session.router.route = lambda *a, **k: {'route': 'tools'}
        assert session.run_turn('audit the repo') == 'Noted.'
        tools = {t['function']['name'] for t in main.calls[0]['tools']}
        assert set(shelf) <= tools
        assert 'desk-look' not in json.dumps(main.calls[0]['tools'])


def test_shelf_can_be_disabled_and_moved(tmp_path):
    config = session_config(tmp_path)
    config.skills.shelf = str(tmp_path / 'elsewhere')
    write_skill(tmp_path / 'elsewhere', 'moved', 'Lives elsewhere.')
    with Session(config, backend=FakeBackend([])) as session:
        assert 'moved' in session.tools.execute('skill_search', {'query': 'elsewhere'})
    config.skills.enabled = False
    with Session(config, backend=FakeBackend([])) as session:
        assert 'skill_load' not in [s['function']['name'] for s in session.tools.schemas()]
        assert 'skill shelf' not in session.system_prompt


def test_search_ranks_partial_phrase_matches_deterministically(tmp_path):
    write_skill(tmp_path / 'skills', 'timers-and-rest', 'Use when you should come back to something later.')
    write_skill(tmp_path / 'skills', 'desk-look', 'Use when looking at the desk screen.')
    with Session(session_config(tmp_path), backend=FakeBackend([])) as session:
        found = session.tools.execute('skill_search', {'query': 'coming back to something later'})
        assert found.splitlines()[1].startswith('- timers-and-rest')
        assert session.tools.execute('skill_search', {'query': 'coming back to something later'}) == found
        assert session.tools.execute('skill_search', {'query': 'zebra'}).startswith('No skills matched')


def test_wake_does_not_reload_an_unchanged_skill_and_does_when_the_file_moves(tmp_path):
    write_skill(tmp_path / 'skills', 'timers-and-rest', 'Use when a wake arrives.')
    config = session_config(tmp_path)
    first = FakeBackend([
        {'tool_calls': [{'id': 'load', 'name': 'skill_load', 'arguments': {'name': 'timers-and-rest'}}]},
        'Loaded once.',
        'Watching.',
    ])
    with Session(config, backend=first, compactor_backend=FakeBackend(['summary'])) as session:
        before = prefix_hashes(session)
        assert session.run_turn('Load the wake skill.') == 'Loaded once.'
        session.run_turn('[wake] Timer fired.', turn_meta={'wake': True})
        state = turn_state(first.calls[-1])
        assert 'Already loaded, do not reload unless the file changed: timers-and-rest.' in state
        assert 'Do the timers-and-rest procedure' not in state
        assert prefix_hashes(session) == before
    skill_file = tmp_path / 'skills' / 'timers-and-rest' / 'SKILL.md'
    text = skill_file.read_text()
    skill_file.write_text(text.replace('procedure', 'method'))
    with Session(config, backend=FakeBackend([])) as restarted:
        line = restarted._skill_pin_line()
        assert 'Reload, the file changed: timers-and-rest.' in line
        assert 'Already loaded' not in line
        assert prefix_hashes(restarted) == before
