"""The existing identity and Settings routes share real person records."""

import threading

import pytest

from fake_backend import FakeBackend
from tessera.config import Config
from tessera.server import create_server
from test_server import call


@pytest.fixture
def identity_server(tmp_path):
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.jev.enabled = False
    config.identity.enabled = True
    config.identity.record_operator = False
    # Same isolation as record_operator: these tests are about the route, not about
    # whether the configured companion is registered on start. Leaving it on made every
    # expectation below count a second person ('Tessera') that the route never created.
    config.identity.record_companion = False
    server = create_server(config, backend_factory=lambda slug, name: FakeBackend([]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def people_field(settings):
    for section in settings['sections']:
        for field in section['fields']:
            if field['id'] == 'identities':
                return field
    pytest.fail('Settings did not expose People with identity records enabled')


def test_identity_route_lists_real_registry_records(identity_server):
    registry = identity_server.manager.identities
    person = registry.store.create('Example Person')

    status, body = call(identity_server, 'GET', '/api/identities')

    assert status == 200
    assert body['enabled'] is True
    assert body['people'] == 1
    assert body['identities'] == [person]
    assert body['unresolved_owners'] == []


def test_identity_route_creates_a_person_in_the_settings_registry(identity_server):
    manager = identity_server.manager
    status, created = call(identity_server, 'POST', '/api/identities', {
        'name': 'Example Person', 'display_name': 'Example',
    })

    assert status == 201
    assert created['kind'] == 'person'
    assert created['name'] == 'example-person'
    assert created['display_name'] == 'Example'
    assert manager.config.identity_registry is manager.identities
    assert manager.identities.store.by_name('example-person') == created
    assert manager.identities.resolve(created['id']) == created
    status, settings = call(identity_server, 'GET', '/api/settings')
    assert status == 200
    assert people_field(settings)['records'][0]['name'] == created['name']


def test_settings_route_adds_and_renames_the_same_person(identity_server):
    status, result = call(identity_server, 'POST', '/api/settings', {
        'id': 'identities', 'action': 'add', 'record': {'name': 'Example Person'},
    })
    assert status == 200
    assert people_field(result['settings'])['records'][0]['name'] == 'example-person'
    registry = identity_server.manager.identities
    original = registry.store.by_name('example-person')
    assert original is not None

    status, result = call(identity_server, 'POST', '/api/settings', {
        'id': 'identities', 'action': 'edit', 'name': 'example-person',
        'record': {'name': 'Renamed Person', 'display_name': 'Renamed'},
    })
    assert status == 200
    assert people_field(result['settings'])['records'][0]['name'] == 'renamed-person'
    renamed = registry.store.by_name('renamed-person')
    assert renamed['id'] == original['id']
    assert renamed['version'] == original['version'] + 1
    assert renamed['display_name'] == 'Renamed'
    assert registry.store.by_name('example-person') is None
    status, body = call(identity_server, 'GET', '/api/identities')
    assert status == 200
    assert body['identities'] == [renamed]


def test_identity_route_rejects_duplicate_names_without_changing_records(identity_server):
    person = identity_server.manager.identities.store.create('Example Person')
    status, body = call(identity_server, 'POST', '/api/identities', {'name': 'EXAMPLE PERSON'})
    assert status == 400
    assert body == {'error': 'An identity already answers to that name'}
    assert identity_server.manager.identities.people() == [person]
