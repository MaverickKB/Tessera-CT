"""The docs must not promise a coverage the page does not have.

The honest answer to "why is there no control for `tools`?" has to be on the
page a person is reading, not only a grep. The reference answers per key, which
is correct but only visible to someone who already knew which key to look up.
docs/SETUP.md carries the summary, so someone reading about the Settings page
learns its extent in one paragraph.

Two directions are checked, because a disclosure rots in both: a name in the list
that has since gained a control (a lie in the pessimistic direction), and an
attribute with no control that the list forgot to mention.
"""

import ast
from pathlib import Path
import re
import tempfile

from tessera import config as C
from tessera.settings_model import file_only, project

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / 'docs' / 'SETUP.md'


def _disclosure():
    """Only the disclosure section of SETUP.md, not the whole file.

    A name-based check over the entire document is worthless: every one of these
    sections already appears in the section table above, so a disclosure that
    omitted a name would still match somewhere. The text is cut at its heading and
    at the next heading of the same level, and only that slice is searched.
    """
    text = SETUP.read_text(encoding='utf-8')
    start = text.index('### What the Settings page edits')
    rest = text[start:]
    end = rest.find('\n## ', 1)
    return rest if end == -1 else rest[:end]


def _split():
    """The page's reachable and unreachable Config attributes.

    ``settings_model.file_only`` walks Config against the live catalog. A
    bare key such as ``agent_name`` can have a control without a dot, and a
    named-entry editor reaches its whole table. The test uses that walk
    instead of re-deriving the split.
    """
    report = file_only(C.Config(), {'yolo': False, 'safe_mode': 'deny_risky', 'configured': 'deny_risky'})
    absent = [row['name'] for row in report['rows']]
    tree = ast.parse((ROOT / 'tessera' / 'config.py').read_text(encoding='utf-8'))
    own = [s.target.id for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == 'Config'
           for s in n.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
    reachable = [a for a in own if a not in absent]
    return reachable, absent


def _lists():
    """The reachable and absent names as the disclosure STATES them.

    Presence-only checking cannot tell a name in the right list from a name in the
    wrong one: `test_the_disclosed_split_matches_the_catalog` passed while
    `systems` sat in the not-reachable list after it had gained a control, because
    the token was still somewhere in the slice. So the two lists are parsed apart
    and compared as sets, in both directions.
    """
    text = _disclosure()
    reachable_at = text.index('Reachable from the page (')
    absent_at = text.index('Not reachable from the page:')
    # The reachable list ends at the sentence describing record editors, which is
    # part of the same paragraph but not part of the list.
    reachable_text = text[reachable_at:absent_at].split(' are record editors')[0]
    tail = text[absent_at:]
    absent_text = tail.split('Editing those means')[0]
    return set(_names(reachable_text)), set(_names(absent_text))


def _names(block):
    return re.findall(r'`([A-Za-z_][A-Za-z0-9_]*)`', block)


def test_the_disclosed_lists_place_every_name_correctly():
    """A name in the wrong list is still a lie, even though the token is present."""
    reachable, absent = _split()
    stated_reachable, stated_absent = _lists()
    assert stated_reachable == set(reachable), (
        f'names documented as reachable but not in the catalog: {sorted(stated_reachable - set(reachable))}; '
        f'catalog-reachable names the docs list as unreachable or omit: {sorted(set(reachable) - stated_reachable)}')
    assert stated_absent == set(absent), (
        f'names documented as unreachable but the page reaches them: {sorted(stated_absent - set(absent))}; '
        f'absent names the docs list as reachable or omit: {sorted(set(absent) - stated_absent)}')
    assert not (stated_reachable & stated_absent), (
        f'named in both lists: {sorted(stated_reachable & stated_absent)}')


def test_the_disclosed_split_matches_the_catalog():
    reachable, absent = _split()
    text = _disclosure()
    for name in absent:
        assert f'`{name}`' in text, f'docs/SETUP.md does not disclose that {name} has no control'
    for name in reachable:
        assert f'`{name}`' in text, f'docs/SETUP.md does not name {name} as reachable'


def test_the_disclosed_counts_match_the_catalog():
    """A stale number is the first thing to rot, so the numbers are asserted."""
    reachable, absent = _split()
    text = _disclosure()
    assert f'**{len(reachable)} are reachable from the page**' in text, \
        f'the page reaches {len(reachable)} attributes; the docs say otherwise'
    assert f'**{len(absent)} are not**' in text, \
        f'{len(absent)} attributes have no control; the docs say otherwise'


def test_every_reachable_attribute_has_a_settings_spec():
    """Cross-check the measurement itself: the catalog is not empty or stale."""
    data = project(C.load_config(Path(tempfile.mkdtemp())), {})
    ids = {f['id'] for s in data['sections'] for f in s['fields']}
    assert 'ui.yolo_banner' in ids
    # A record editor's add-form fields live under the record's `blank`, not in the
    # section's flat field list, so the existence check looks where they actually are.
    hosts = next(s for s in data['sections'] if s['id'] == 'hosts')
    record = next(f for f in hosts['fields'] if f['type'] == 'list-of-records')
    assert {f['id'] for f in record['blank']} >= {'hosts.new.name', 'hosts.new.kind'}


def test_the_page_states_a_reason_for_every_attribute_it_does_not_edit():
    """The disclosure must be on the page, not only in this doc.

    A file-only row needs a reason for every table. A payload with no such section
    leaves a person reading Settings no way to learn that `agents.max_concurrent` exists.
    """
    data = project(C.Config(), {'yolo': False, 'safe_mode': 'deny_risky', 'configured': 'deny_risky'})
    report = data['file_only']
    assert report['count'] == len(report['rows'])
    assert report['reachable'] + report['count'] == 49
    for row in report['rows']:
        assert row['reason'].strip(), f'{row["name"]} has no stated reason'
    names = {row['name'] for row in report['rows']}
    assert 'agents' in names and 'server' in names and 'prompt' in names
    # A reachable attribute must never appear in the file-only list.
    reachable, absent = _split()
    assert not (set(absent) & set(reachable))


def test_a_new_config_table_without_a_reason_is_refused_not_ignored():
    """The guard that keeps the table honest as Config grows.

    Without this, adding a table to Config and forgetting FILE_ONLY_REASONS would ship an
    undisclosed gap: the page would simply not mention it, and nothing would fail.
    """
    from tessera import settings_model as S
    saved = dict(S.FILE_ONLY_REASONS)
    try:
        # `swarm` is only PARTIALLY reachable now: the page edits its two admission
        # fields, but the table still needs a reason for the rest. A wholly file-only
        # table is the honest probe, so use one that has no catalog spec at all.
        S.FILE_ONLY_REASONS.pop('compactor')
        try:
            S.file_only(C.Config(), {'yolo': False, 'safe_mode': 'deny_risky', 'configured': 'deny_risky'})
        except ValueError as error:
            assert 'compactor' in str(error)
        else:
            raise AssertionError('an unexplained table was accepted')
    finally:
        S.FILE_ONLY_REASONS.clear()
        S.FILE_ONLY_REASONS.update(saved)


def test_a_runtime_approval_override_is_disclosed_on_the_field():
    """The live config says deny_risky; the switch says YOLO. The field must say both.

    Measured before the change: /api/settings served permissions.mode with value "yolo"
    and status "Safe blocks", and no hint. A reader saw "Safe blocks" next to a mode that
    blocks nothing, with the configured policy nowhere on the page.
    """
    overridden = project(C.Config(), {'mode': 'yolo', 'yolo': True, 'configured': 'deny_risky',
                                     'safe_mode': 'deny_risky', 'switchable': True})
    field = next(f for s in overridden['sections'] if s['id'] == 'permissions'
                 for f in s['fields'] if f['id'] == 'permissions.mode')
    assert field['value'] == 'yolo'
    assert 'deny_risky' in field['hint']
    # Agreeing states carry no override prose: a hint that always appears is noise.
    agreeing = project(C.Config(), {'mode': 'deny_risky', 'yolo': False, 'configured': 'deny_risky',
                                   'safe_mode': 'deny_risky', 'switchable': True})
    agreed = next(f for s in agreeing['sections'] if s['id'] == 'permissions'
                  for f in s['fields'] if f['id'] == 'permissions.mode')
    assert not agreed.get('hint')
