"""A person is an identity, not the operator slot.

A person is an identity, and most installs have one person. The model allows more:
cards and tasking are shared by several people who coordinate only through projects
and cards. Authentication is a later layer; nothing here decides who may do what.

What this pins:
- Identity records are opt-in, so an install that never turns them on keeps today's
  free-string ownership and the exact view shape its page parses.
- When on, an owner resolves to a person record or a live session, and the board says
  which. An owner that resolves to nothing is shown as unresolved rather than tidied.
- A card cannot be claimed by a name nobody holds, which closes the failure the
  previous behavior pinned knowingly: a typo owner wakes nobody.
- A fresh install never inherits someone's identity, so an empty operator_name writes
  no person record.
"""

import pytest

from tessera.config import Config
from tessera.identities import IdentityError, IdentityStore, handle_for, replay_identities
from tessera.sessions import SessionManager


def manager(tmp_path, *, operator_name='alex', agent_name='Tessera', **identity):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    config.operator_name = operator_name
    config.agent_name = agent_name
    for key, value in identity.items():
        setattr(config.identity, key, value)
    return SessionManager(config)


def test_identity_records_are_opt_in_and_leave_the_view_shape_alone(tmp_path):
    """Off is the default, and off means the payload a page already parses is unchanged."""
    with manager(tmp_path) as sessions:
        sessions.create("Worker")
        slug = sessions.list_sessions()[0]["slug"]
        sessions.board.create("Work", slug, 0, 0)
        row = sessions.board_view()["cards"][0]
        assert "owner_known" not in row and "owner_id" not in row
        assert sessions.identities.enabled is False
        # No identity journal is written when the layer is off.
        assert not (tmp_path / "identities.jsonl").exists()


def test_a_configured_operator_becomes_a_person_record_once(tmp_path):
    with manager(tmp_path, enabled=True, record_operator=True) as sessions:
        sessions.create("Worker")
        people = [item for item in sessions.identities.all() if item["kind"] == "person"]
        assert [item["name"] for item in people] == ["alex", "tessera"]
        # Idempotent across a restart of the manager: the same record, not a second one.
        again = sessions.identities.ensure_operator("alex")
        assert again["id"] == people[0]["id"]
        assert len([item for item in sessions.identities.all() if item["kind"] == "person"]) == 2


def test_an_empty_operator_name_records_nobody(tmp_path):
    """A fresh install must not inherit anyone's identity."""
    with manager(tmp_path, operator_name='', agent_name='', enabled=True, record_operator=True) as sessions:
        assert sessions.identities.people() == []


def test_a_card_owner_resolves_to_a_person_or_a_session(tmp_path):
    with manager(tmp_path, enabled=True) as sessions:
        sessions.create("Worker")
        row = sessions.list_sessions()[0]
        sessions.board.create("Session work", row["slug"], 0, 0, owner=row["slug"])
        sessions.board.create("Person work", row["slug"], 0, 0, owner="alex")
        cards = {c["title"]: c for c in sessions.board_view()["cards"]}

        session_card = cards["Session work"]
        assert session_card["owner_known"] is True and session_card["owner_kind"] == "session"
        assert session_card["owner_id"] == row["id"]

        person_card = cards["Person work"]
        assert person_card["owner_known"] is True and person_card["owner_kind"] == "person"
        assert person_card["owner_id"] == sessions.identities.resolve("alex")["id"]


def test_an_owner_nobody_holds_is_refused_when_records_are_on(tmp_path):
    """The failure the earlier behavior pinned: a typo owner wakes nobody."""
    with manager(tmp_path, enabled=True) as sessions:
        sessions.create("Worker")
        slug = sessions.list_sessions()[0]["slug"]
        with pytest.raises(Exception) as problem:
            sessions.board.create("Typo owned", slug, 0, 0, owner="00000000000000000000000000000000")
        assert "No identity answers" in str(problem.value)
        # A claim is held to the same rule.
        sessions.board.create("Open work", slug, 0, 0, owner="")
        view = sessions.board_view()
        ordinal = view["cards"][0]["ordinal"]
        with pytest.raises(Exception) as claim:
            sessions.board_claim(ordinal, view["revision"], owner="nobody-at-all")
        assert "No identity answers" in str(claim.value)


def test_an_unresolved_owner_from_an_older_board_is_shown_not_rewritten(tmp_path):
    """A card written before this layer existed keeps its string and says it is unresolved."""
    with manager(tmp_path, enabled=True) as sessions:
        sessions.create("Worker")
        slug = sessions.list_sessions()[0]["slug"]
        card = sessions.board.create("Old work", slug, 0, 0)
        # Write an owner the way an older core would have: straight into the journal.
        sessions.board.append_note(card["id"], "written before identity records")
        sessions.board.identities = None  # simulate the writer having no registry
        sessions.board.claim(card["id"], "old-handle", actor=None)
        sessions.board.identities = sessions.identities
        row = next(c for c in sessions.board_view()["cards"] if c["card_id"] == card["id"])
        assert row["owner"] == "old-handle"
        assert row["owner_known"] is False
        assert row["owner_id"] == ""


def test_identity_journal_keeps_history_instead_of_overwriting(tmp_path):
    store = IdentityStore(tmp_path / "identities.jsonl", enabled=True)
    person = store.create("alex", "Example Person")
    renamed = store.rename(person["id"], name="alex-b", display_name="Alex B")
    assert renamed["version"] == 2 and renamed["name"] == "alex-b"
    # Both versions are in the journal: a rename is evidence, not a replacement.
    identities, sequence = replay_identities(store._read_records())
    assert sequence == 2
    assert identities[person["id"]]["version"] == 2
    lines = (tmp_path / "identities.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    # The old name is free again, and a duplicate of the current one is refused.
    store.create("alex")
    with pytest.raises(IdentityError):
        store.create("alex-b")


def test_a_torn_identity_line_is_preserved_before_the_journal_is_repaired(tmp_path):
    store = IdentityStore(tmp_path / "identities.jsonl", enabled=True)
    store.create("alex")
    with (tmp_path / "identities.jsonl").open("ab") as stream:
        stream.write(b'{"sequence": 2, "identity": {"id": "abc"')
    assert store.create("other")["name"] == "other"
    backups = [path for path in tmp_path.iterdir() if ".incomplete-" in path.name]
    assert backups and backups[0].read_text().startswith('{"sequence": 2')


def test_a_duplicate_owner_is_refused_rather_than_ambiguously_resolved(tmp_path):
    """Two identities answering to one name is the ambiguity this layer removes."""
    store = IdentityStore(tmp_path / "identities.jsonl", enabled=True)
    store.create("alex")
    with pytest.raises(IdentityError) as problem:
        store.create("alex")
    assert "already answers" in str(problem.value)


def test_a_name_typed_the_way_a_person_types_it_is_recorded_not_refused(tmp_path):
    """The defect this replaces: person names were validated as session slugs.

    In a fresh home an operator named 'alex' recorded a person, a typed 'Alex' recorded
    none, the operator's full name recorded none, and sessions.py swallowed the error.
    Every card owned by a typed name then showed as unresolved with no error anywhere.
    """
    assert handle_for('Alex') == 'alex'
    assert handle_for('Example Person') == 'example-person'
    assert handle_for('  Alex  ') == 'alex'
    # Punctuation folds rather than refusing: a person types a name, not a slug.
    assert handle_for('k.person') == 'k-person'
    with pytest.raises(IdentityError):
        handle_for('   ')
    with pytest.raises(IdentityError):
        handle_for(None)


@pytest.mark.parametrize('typed', ['alex', 'Alex', 'Example Person'])
def test_the_operator_is_recorded_whatever_case_and_spacing_is_typed(tmp_path, typed):
    with manager(tmp_path, operator_name=typed, enabled=True) as sessions:
        people = [p for p in sessions.identities.people() if p['name'] != 'tessera']
        assert len(people) == 1
        # The handle is the slug-safe form; the typed form stays readable.
        assert people[0]['name'] == handle_for(typed)
        assert people[0]['display_name'] == typed.strip()


def test_a_rename_keeps_the_typed_form_as_the_display(tmp_path):
    store = IdentityStore(tmp_path / "identities.jsonl", enabled=True)
    person = store.create("alex")
    renamed = store.rename(person["id"], name="Example Person")
    assert renamed["name"] == "example-person"
    assert renamed["display_name"] == "Example Person"


def test_identities_view_lists_people_sessions_and_owner_strings_nobody_holds(tmp_path):
    """The useful half: which cards are owned by a handle that resolves to nobody."""
    with manager(tmp_path, operator_name='alex', enabled=True) as sessions:
        sessions.create("Worker")
        slug = sessions.list_sessions()[0]["slug"]
        card = sessions.board.create("Old work", slug, 0, 0)
        # A card written before this layer existed: no registry at write time.
        sessions.board.identities = None
        sessions.board.claim(card["id"], "old-handle", actor=None)
        sessions.board.identities = sessions.identities

        view = sessions.identities_view()
        names = {item["name"] for item in view["identities"]}
        assert names == {"alex", "tessera", slug}
        assert {item["kind"] for item in view["identities"]} == {"person", "session"}

        unresolved = view["unresolved_owners"]
        assert len(unresolved) == 1
        assert unresolved[0]["handle"] == "old-handle"
        assert unresolved[0]["card_id"] == card["id"]
        assert unresolved[0]["ordinal"] == 1


def test_identities_view_is_empty_when_records_are_off(tmp_path):
    with manager(tmp_path, enabled=False) as sessions:
        sessions.create("Worker")
        view = sessions.identities_view()
        assert view["enabled"] is False
        assert view["identities"] == []
        assert view["unresolved_owners"] == []


def test_a_person_can_be_created_and_renamed_through_the_manager(tmp_path):
    """A second human must not need config.toml edited by hand."""
    with manager(tmp_path, operator_name='alex', enabled=True) as sessions:
        created = sessions.identity_create("Robin Hale")
        assert created["name"] == "robin-hale" and created["display_name"] == "Robin Hale"
        renamed = sessions.identity_rename(created["id"], name="Robyn Hale")
        assert renamed["name"] == "robyn-hale"
        assert renamed["display_name"] == "Robyn Hale"
        # A duplicate handle is still refused rather than ambiguously resolved.
        with pytest.raises(IdentityError):
            sessions.identity_create("ALEX")


def test_the_settings_page_projects_people_and_unresolved_owners(tmp_path):
    """The record layer was unreachable twice: once for want of a route, once for want
    of a projection. This pins the page half so a merge cannot drop it silently.

    Measured before the fix: `identity` section listed agent_name, operator_name,
    autonomy.floor_name, identity.enabled and nothing else, so a second human had no
    surface at all. The `identities` field appears only when records are on, and the
    unresolved list rides on it rather than becoming editable rows.
    """
    from tessera.settings_model import project

    with manager(tmp_path, operator_name='Alex', enabled=True) as sessions:
        sessions.create("Worker")
        body = project(sessions.config, sessions.approval_status(), sessions)
        identity = [s for s in body['sections'] if s['id'] == 'identity'][0]
        ids = [f['id'] for f in identity['fields']]
        assert 'identities' in ids
        people = [f for f in identity['fields'] if f['id'] == 'identities'][0]
        names = [r['name'] for r in people['records']]
        assert names == ['alex', 'tessera', 'worker']
        # A session is read live, so it is listed but not editable.
        worker = [r for r in people['records'] if r['name'] == 'worker'][0]
        assert worker['status'] == 'session' and worker['actions'] == []
        assert people['unresolved'] == []


def test_the_settings_projection_leaves_the_payload_alone_when_records_are_off(tmp_path):
    """Off is the default, and off must not grow a row an install cannot use."""
    from tessera.settings_model import project

    with manager(tmp_path, operator_name='Alex') as sessions:
        body = project(sessions.config, sessions.approval_status(), sessions)
        identity = [s for s in body['sections'] if s['id'] == 'identity'][0]
        ids = [f['id'] for f in identity['fields']]
        assert 'identities' not in ids


def test_an_owner_nobody_holds_reaches_the_settings_page_not_just_the_route(tmp_path):
    """The unresolved report is the point of the layer, and it has to arrive where a
    person looks. Measured: it is present on /api/identities and on the settings field.
    """
    from tessera.settings_model import project

    # Phase 1: records off, so a free-string owner is accepted — the legacy shape.
    with manager(tmp_path, operator_name='Alex') as legacy:
        legacy.create("Worker")
        legacy.board.create(title='Old work', session_slug='worker',
                            log_start=1, log_end=1, owner='old-handle')

    # Phase 2: records on over the same home.
    with manager(tmp_path, operator_name='Alex', enabled=True) as sessions:
        body = project(sessions.config, sessions.approval_status(), sessions)
        identity = [s for s in body['sections'] if s['id'] == 'identity'][0]
        people = [f for f in identity['fields'] if f['id'] == 'identities'][0]
        unresolved = people['unresolved']
        assert len(unresolved) == 1
        assert unresolved[0]['kind'] == 'card'
        assert unresolved[0]['handle'] == 'old-handle'
        assert unresolved[0]['title'] == 'Old work'


@pytest.mark.parametrize('typed', ['alex', 'Alex', 'ALEX', '  Alex  '])
def test_the_operator_resolves_under_the_spelling_written_in_config(tmp_path, typed):
    """Creation folded the name to a handle; resolution compared the raw string.

    ``ensure_operator`` folds 'Alex' to 'alex' when it creates the record, but
    ``resolve`` matched the raw string against the stored name and id. With
    operator_name = "Alex", resolve('alex') answered while resolve('Alex') and
    resolve('ALEX') answered nothing: one module disagreeing with itself.
    """
    with manager(tmp_path, operator_name=typed, enabled=True, record_operator=True) as sessions:
        people = [p for p in sessions.identities.people() if p['name'] != 'tessera']
        assert len(people) == 1
        for spelling in ('alex', 'Alex', 'ALEX', '  Alex  '):
            resolved = sessions.identities.resolve(spelling)
            assert resolved is not None, f'{spelling!r} did not resolve'
            assert resolved['id'] == people[0]['id']
        assert people[0]['display_name'] == typed.strip()


def test_a_serialized_id_still_resolves_and_is_never_folded(tmp_path):
    """Folding must not make an id match a name, or an id match its own upper-case."""
    with manager(tmp_path, enabled=True, record_operator=True) as sessions:
        person = sessions.identities.people()[0]
        assert sessions.identities.resolve(person['id'])['id'] == person['id']
        assert sessions.identities.resolve(person['id'].upper()) is None


def test_a_board_owner_must_be_a_handle_not_a_display_name(tmp_path):
    """The charset gate runs BEFORE identity resolution, and it is deliberate.

    With records both off and on, board.create(owner='Alex') is refused
    'Invalid board owner', while owner='alex' is accepted. An owner
    string is slug-shaped because wake routing uses it as a session slug, so a person
    with a display name must be named by the folded handle, not by what they type.
    This test pins that order so nobody "fixes" the refusal by loosening the charset.
    """
    for records_on in (False, True):
        with manager(tmp_path / str(records_on), operator_name='Alex',
                     enabled=records_on, record_operator=records_on) as sessions:
            sessions.create("Worker")
            slug = sessions.list_sessions()[0]["slug"]
            with pytest.raises(Exception) as refused:
                sessions.board.create("Owned by display name", slug, 0, 0, owner="Alex")
            assert "Invalid board owner" in str(refused.value)
            # The folded handle is the form that works, and it resolves to the person.
            sessions.board.create("Owned by handle", slug, 0, 0, owner="alex")
            row = [c for c in sessions.board_view()["cards"] if c["owner"] == "alex"][0]
            if records_on:
                assert row.get("owner_known") is True
                assert row.get("owner_kind") == "person"


def test_an_acceptance_must_be_signed_by_a_known_identity(tmp_path):
    """With records on, an acceptance cannot be signed by a name nobody holds.

    Card 127: acceptance is durable history carrying who accepted. A name that resolves
    to nobody is the failure this layer exists to catch, and it applies to acceptance the
    same way it applies to ownership.
    """
    with manager(tmp_path, enabled=True, record_operator=True) as sessions:
        sessions.create("Worker")
        slug = sessions.list_sessions()[0]["slug"]
        sessions.board.acceptance_required = True
        card = sessions.board.create("Gated work", slug, 0, 0)
        with pytest.raises(Exception) as refused:
            sessions.board.accept(card['id'], 'nobody-at-all', evidence='x')
        assert 'No identity answers' in str(refused.value)
        # The recorded operator is a person and may accept.
        sessions.board.accept(card['id'], 'alex', evidence='read the diff')
        sessions.board.update_status(card['id'], 'succeeded')
        row = [c for c in sessions.board_view()['cards'] if c['card_id'] == card['id']][0]
        assert row['acceptances'][0]['by'] == 'alex'
        assert row['acceptances'][0]['evidence'] == 'read the diff'


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('record_companion', [False, True])
def test_companion_registration_respects_both_switches(tmp_path, enabled, record_companion):
    with manager(tmp_path, agent_name='Tessera', enabled=enabled,
                 record_companion=record_companion, record_operator=False) as sessions:
        people = sessions.identities.people()
        if enabled and record_companion:
            assert [(p['name'], p['kind']) for p in people] == [('tessera', 'person')]
        else:
            assert people == []
            if not enabled:
                assert sessions.identities.ensure_companion('Tessera') is None
            assert not (tmp_path / 'identities.jsonl').exists()


@pytest.mark.parametrize('typed', ['tessera', 'Tessera', '  TESSERA  ', 'Example Companion'])
def test_companion_owns_work_and_keeps_one_identity_across_restarts(tmp_path, typed):
    with manager(tmp_path, agent_name=typed, enabled=True) as sessions:
        companion = sessions.identities.resolve(typed)
        assert companion['name'] == handle_for(typed)
        assert companion['display_name'] == typed.strip()
        assert companion['kind'] == 'person'
        assert sessions.identities.ensure_companion(typed)['id'] == companion['id']
        sessions.create('Worker')
        slug = sessions.list_sessions()[0]['slug']
        sessions.board.create('Companion work', slug, 0, 0, owner=handle_for(typed))
        row = sessions.board_view()['cards'][0]
        assert row['owner_known'] is True
        assert row['owner_id'] == companion['id']
        assert row['owner_kind'] == 'person'
    with manager(tmp_path, agent_name=typed, enabled=True) as sessions:
        assert sessions.identities.resolve(typed)['id'] == companion['id']
        assert len(sessions.identities.people()) == 2


def test_empty_companion_name_records_no_companion(tmp_path):
    with manager(tmp_path, agent_name='  ', enabled=True) as sessions:
        assert [p['name'] for p in sessions.identities.people()] == ['alex']
