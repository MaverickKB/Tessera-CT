"""Named accounts: the settings page can add, edit and remove the concurrency pools.

A settings page for connectors has to let a person add or
manage the connections? Answer, none."

Before this, no settings spec targeted accounts.<name>.* at all, so config.accounts
(pools of concurrent CLI workers with shared limits) was invisible on the page while
every other list had controls. These tests drive the real HTTP route.
"""
import json
from pathlib import Path

import pytest

from tessera.config import load_config
from tessera.settings_model import accounts_field, project
from test_server import call
from test_settings_model import serve, stop

ROOT_WEB = Path(__file__).resolve().parents[1] / "web"


ACCOUNTS = '''
[backends.fake]
base_url = "http://127.0.0.1:9/v1"
model = "scripted-local-fake"
chat = true
enabled = true

[backends.grok]
chat = true
enabled = true
account = "grok-pool"

[backends.grok.cli]
vendor = "grok"
command = "grok"
cwd = "workers"

[accounts.grok-pool]
provider_kind = "cli_worker"

[credentials.voice]
keychain_service = "example-service"
keychain_account = "example-voice"

[roles]
main = "fake"
'''


def test_named_accounts_are_projected_with_controls(tmp_path):
    """The pool that governs concurrency must appear, and carry edit/remove."""
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(ACCOUNTS)
    config = load_config(tmp_path)
    field = accounts_field(config)

    assert field['id'] == 'named_accounts' and field['section'] == 'integrations'
    assert field['type'] == 'list-of-records'
    assert field['actions'] == ['add', 'edit', 'remove']

    record = next(item for item in field['records'] if item['name'] == 'grok-pool')
    assert record['actions'] == ['edit', 'remove']
    assert 'cli_worker' in record['status']

    keys = {spec['key'] for spec in record['fields']}
    assert keys == {'provider_kind', 'credential_ref'}
    values = {spec['key']: spec['value'] for spec in record['fields']}
    assert values['provider_kind'] == 'cli_worker'

    blank_keys = {spec['key'] for spec in field['blank']}
    assert 'name' in blank_keys and 'max_concurrent' not in blank_keys

    # It must be reachable from the real projection the page reads.
    body = project(config, {'mode': 'safe'})
    sections = {section['id']: section for section in body['sections']}
    assert 'named_accounts' in [item['id'] for item in sections['integrations']['fields']]
    assert 'oauth_accounts' in [item['id'] for item in sections['providers']['fields']]


def test_add_edit_and_remove_a_named_account(tmp_path):
    server, thread = serve(tmp_path, ACCOUNTS)
    try:
        added = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'codex-pool', 'provider_kind': 'cli_worker'}})
        assert added[0] == 200, added
        saved = load_config(tmp_path).accounts['codex-pool']
        assert saved.name == 'codex-pool'
        assert 'max_concurrent' not in saved.limits and 'initial_concurrency' not in saved.limits

        edited = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'edit', 'name': 'codex-pool',
            'record': {'max_concurrent': 16}})
        assert edited[0] == 400
        assert 'max_concurrent_requests' in edited[1].get('error', '')
        assert 'max_concurrent' not in load_config(tmp_path).accounts['codex-pool'].limits

        # A duplicate name is refused rather than silently overwritten.
        duplicate = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'codex-pool', 'provider_kind': 'cli_worker'}})
        assert duplicate[0] == 400

        removed = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'remove', 'name': 'codex-pool'})
        assert removed[0] == 200, removed
        assert 'codex-pool' not in load_config(tmp_path).accounts
    finally:
        stop(server, thread)


def test_remove_is_refused_while_a_backend_uses_the_account(tmp_path):
    server, thread = serve(tmp_path, ACCOUNTS)
    try:
        refused = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'remove', 'name': 'grok-pool'})
        assert refused[0] == 400
        assert 'grok' in refused[1].get('error', '')
        assert 'grok-pool' in load_config(tmp_path).accounts
    finally:
        stop(server, thread)


def test_invalid_account_values_are_rejected(tmp_path):
    server, thread = serve(tmp_path, ACCOUNTS)
    try:
        unknown_kind = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'odd-pool', 'provider_kind': 'not_a_kind'}})
        assert unknown_kind[0] == 400

        negative = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'zero-pool', 'provider_kind': 'cli_worker', 'max_concurrent': 0}})
        assert negative[0] == 400
        assert 'max_concurrent_requests' in negative[1].get('error', '')
        assert 'zero-pool' not in load_config(tmp_path).accounts

        # A CLI account carrying an API credential is a config error, not a silent save.
        bad_credential = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'cred-pool', 'provider_kind': 'cli_worker', 'credential_ref': 'voice'}})
        assert bad_credential[0] == 400

        text = (tmp_path / 'config.toml').read_text()
        assert 'odd-pool' not in text and 'cred-pool' not in text and 'zero-pool' not in text
    finally:
        stop(server, thread)


def test_unknown_fields_are_refused(tmp_path):
    server, thread = serve(tmp_path, ACCOUNTS)
    try:
        result = call(server, 'POST', '/api/settings', {
            'id': 'named_accounts', 'action': 'add',
            'record': {'name': 'x-pool', 'provider_kind': 'cli_worker', 'surprise': True}})
        assert result[0] == 400
    finally:
        stop(server, thread)


def test_the_pool_form_speaks_plain_words_and_never_offers_a_refused_choice(tmp_path):
    """On the Add-account form: "Provider kind is vague
    bullshit and guessed names, not standard terms and why the actual fuck is
    credential a fucking dropdown list?"

    Two defects behind those words, both measured on the live page first:
    (1) the connection-type options were bare wire names -- 'cli_worker',
        'openai_chat' -- with no standard term for what a person is choosing;
    (2) the form offered a Credential on the default CLI pool, which
        config.validate() refuses on save with "CLI accounts use the official CLI
        login, not API credentials." The page asked for something it could not store.

    The stored values must not change: the config validator accepts these wire
    names, and the save path in this same file posts them.
    """
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(ACCOUNTS)
    config = load_config(tmp_path)
    field = accounts_field(config)

    kind = next(spec for spec in field['blank'] if spec['key'] == 'provider_kind')
    # Standard terms, not wire names.
    assert kind['label'] == 'Connection type'
    labels = [option['label'] for option in kind['options']]
    assert all('_' not in label for label in labels), labels
    assert any('Vendor CLI' in label for label in labels)
    assert any('OpenAI' in label for label in labels)
    # The values stay the wire names the validator and the save path use.
    values = [option['value'] for option in kind['options']]
    assert 'cli_worker' in values and 'openai_chat' in values

    # The section says what it is: pools, not sign-ins.
    assert field['label'] == 'Worker pools'
    assert 'sign-in' in field['hint']

    # A CLI pool cannot carry a credential, so the form must not ask for one. The
    # rule lives in web/settings.js showKey(); this pins the server-side facts it
    # depends on, so a change to either side is caught.
    settings_js = (ROOT_WEB / 'settings.js').read_text()
    assert 'listId === "named_accounts" && key === "credential_ref"' in settings_js
    assert 'kind !== "cli_worker"' in settings_js
    assert 'provider_kind' in settings_js

    # And the save path really does refuse it, which is why hiding it is honest.
    broken = tmp_path / 'broken'
    (broken / 'workers').mkdir(parents=True)
    (broken / 'config.toml').write_text(
        ACCOUNTS + '\n[accounts.cred-pool]\n'
        'provider_kind = "cli_worker"\n'
        'credential_ref = "voice"\n')
    with pytest.raises(ValueError, match='CLI accounts use the official CLI login'):
        load_config(broken)
