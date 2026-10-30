"""Settings registry: validation, atomic write, backup, and secrets that never land in config."""
import json
from pathlib import Path
import subprocess
import threading
import pytest

from tessera.config import load_config
from tessera.credentials import KeychainStore, MemoryKeychain
from tessera.server import create_server
from tessera.settings_model import _provider_fields, project
from test_server import call


HOME = '''
[backends.fake]
base_url = "http://127.0.0.1:9/v1"
model = "scripted-local-fake"
chat = true
enabled = true

[backends.grok]
provider_kind = "cli_worker"
chat = true
enabled = true

[backends.grok.cli]
vendor = "grok"
command = "grok"
cwd = "workers"
allow_api_key_fallback = false
fallback_env = "EXAMPLE_GROK_KEY"
fallback_credential_ref = "voice"

[backends.codex]
provider_kind = "cli_worker"
chat = false
enabled = true

[backends.codex.cli]
vendor = "codex"
command = "codex"
cwd = "workers"

[credentials.voice]
keychain_service = "example-service"
keychain_account = "example-voice"

[credentials.xai-oauth]
oauth = "xai"

[roles]
main = "fake"
failover = "grok"

[computer_use.jev]
enabled = false
credential_ref = "voice"
min_confidence = 0.5

[voice]
enabled = false
credential_ref = "voice"
'''


def serve(tmp_path, text=HOME):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    config.server.port = 0
    server = create_server(config)
    server.manager.settings.keychain = MemoryKeychain()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join()


def test_projection_describes_every_control(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    sections = {item['id']: item for item in body['sections']}
    assert list(sections) == ['providers', 'models', 'credentials', 'identity', 'accounts', 'integrations', 'hosts',
                              'systems', 'plugins', 'computer', 'voice', 'appearance', 'goblin', 'permissions',
                              'autonomy', 'judge', 'update']
    identity = {field['id']: field for field in sections['identity']['fields']}
    # Identity records are required: they are the layer ownership resolves through.
    assert list(identity) == ['identity.enabled', 'agent_name', 'operator_name', 'autonomy.floor_name']
    # A fresh install must not be pre-named as someone else's companion.
    assert identity['agent_name']['value'] == 'Tessera'
    assert identity['operator_name']['value'] == ''
    assert identity['autonomy.floor_name']['value'] == ''
    # Identity records are opt-in: an existing install keeps free-string ownership.
    assert identity['identity.enabled']['value'] is False
    assert identity['identity.enabled']['apply'] == 'restart'
    models = {field['id']: field for field in sections['models']['fields']}
    assert models['roles.main']['type'] == 'enum' and models['roles.main']['value'] == 'fake'
    assert models['roles.main']['apply'] == 'live' and models['roles.main']['write'] == 'set'
    assert any(option['value'] == 'fake' for option in models['roles.main']['options'])
    assert all(option['value'] != 'codex' for option in models['roles.main']['options'])
    permissions = sections['permissions']['fields'][0]
    assert permissions['id'] == 'permissions.mode' and permissions['options'] == [
        {'value': 'safe', 'label': 'Safe'}, {'value': 'yolo', 'label': 'Unleashed'}]
    assert permissions['status'] == 'Safe asks'
    assert 'providers' in sections
    providers = next(field for field in sections['providers']['fields'] if field['id'] == 'providers')
    names = [record['name'] for record in providers['records']]
    assert {'codex', 'grok', 'fake'} <= set(names)
    fake = next(record for record in providers['records'] if record['name'] == 'fake')
    assert fake['status'] == 'No credential'
    blob = json.dumps(body)
    blank_keys = [field['key'] for field in providers['blank']]
    assert 'vendor' not in blank_keys and 'command' not in blank_keys and 'cwd' not in blank_keys
    assert 'example-voice' in blob and 'secret' in blob
    assert 'synthetic' not in blob
    sign_in = next(field for field in sections['providers']['fields'] if field['id'] == 'oauth_accounts')
    assert [record['actions'] for record in sign_in['records']] == [['sign-in'], ['sign-in']]
    assert all(field['id'] != 'oauth_accounts' for field in sections['models']['fields'])


def test_api_key_endpoint_shows_its_credential_and_no_charge_section(tmp_path):
    text = HOME + """
[backends.grok-chat]
base_url = "https://api.example.invalid/v1"
model = "example-model"
api_key_env = "EXAMPLE_MODEL_KEY"
chat = true
"""
    (tmp_path / 'config.toml').write_text(text)
    config = load_config(tmp_path)
    body = project(config, {})
    sections = {section['id']: section for section in body['sections']}
    for section in body['sections']:
        assert 'bill' not in section.get('title', '').lower()
        assert 'bill' not in section['label'].lower()

    def assert_visible_text_is_clear(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('label', 'hint', 'title', 'status') and isinstance(child, str):
                    assert 'bill' not in child.lower(), (key, child)
                assert_visible_text_is_clear(child)
        elif isinstance(value, list):
            for child in value:
                assert_visible_text_is_clear(child)

    assert_visible_text_is_clear(body)
    assert_visible_text_is_clear(_provider_fields(config, 'grok', config.backends['grok']))
    providers = next(field for field in sections['providers']['fields'] if field['id'] == 'providers')
    provider = next(record for record in providers['records'] if record['name'] == 'grok-chat')
    assert 'EXAMPLE_MODEL_KEY' in provider['status']
    assert all(field.get('key') != 'allow_api_key' for field in provider['fields'])


def test_provider_fields_declare_their_rendered_section(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    section, providers = next(
        (section, field)
        for section in project(config, {})['sections']
        for field in section['fields'] if field['id'] == 'providers'
    )
    assert section['id'] == 'providers'
    assert providers['section'] == section['id']
    fields = list(providers['blank'])
    for record in providers['records']:
        fields.extend(record['fields'])
    # CLI workers are currently omitted from the page, but their field builder
    # must retain the same ownership, including Grok's fallback controls.
    for name, backend in config.backends.items():
        fields.extend(_provider_fields(config, name, backend))
    mismatches = {field['id']: field['section'] for field in fields
                  if field['section'] != section['id']}
    assert mismatches == {}


def test_signed_in_account_stays_visible_and_can_refresh(tmp_path):
    grant = tmp_path / 'oauth' / 'xai.json'
    grant.parent.mkdir()
    grant.write_text('{"access_token": "present-grant", "refresh_token": "keep-refreshing", "expires_at": 1}')
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    sections = {item['id']: item for item in body['sections']}
    sign_in = next(field for field in sections['providers']['fields'] if field['id'] == 'oauth_accounts')
    xai = next(record for record in sign_in['records'] if record['name'] == 'xai')
    codex = next(record for record in sign_in['records'] if record['name'] == 'openai-codex')
    assert xai['status'] == 'Signed in, token expired'
    assert xai['actions'] == ['sign-in', 'refresh']
    assert codex['status'] == 'Not signed in' and codex['actions'] == ['sign-in']
    blob = json.dumps(body)
    assert 'xAI Grok' in blob and 'ChatGPT or Codex' in blob
    assert 'present-grant' not in blob and 'keep-refreshing' not in blob


def test_write_validates_backs_up_and_reloads(tmp_path):
    server, thread = serve(tmp_path)
    try:
        added = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'local', 'account': 'other', 'kind': 'openai_chat',
            'base_url': 'http://127.0.0.1:9/v1', 'model': 'local-model', 'chat': True}})
        assert added[0] == 200, added[1]
        code, body = call(server, 'POST', '/api/settings', {'id': 'roles.main', 'value': 'local'})
        assert code == 200 and body['changed'] and body['applied'] == 'live' and body['restart_required'] is False
        assert (tmp_path / body['backup']).is_file()
        text = (tmp_path / 'config.toml').read_text()
        assert '"main" = "local"' in text and 'scripted-local-fake' in text
        assert load_config(tmp_path).roles['main'] == 'local'
        assert server.manager.config.roles['main'] == 'local'
        before = (tmp_path / 'config.toml').read_text()
        rejected = call(server, 'POST', '/api/settings', {'id': 'roles.main', 'value': 'missing'})
        assert rejected[0] == 400 and 'allowed' in rejected[1]['error']
        assert (tmp_path / 'config.toml').read_text() == before
        bad = call(server, 'POST', '/api/settings', {'id': 'computer_use.jev.min_confidence', 'value': 4})
        assert bad[0] == 400
        assert (tmp_path / 'config.toml').read_text() == before
        confidence = call(server, 'POST', '/api/settings', {'id': 'computer_use.jev.min_confidence', 'value': 0.25})
        assert confidence[0] == 200 and confidence[1]['restart_required'] is True
        assert load_config(tmp_path).computer_use.jev.min_confidence == 0.25
    finally:
        stop(server, thread)


def test_memory_settings_persist_and_reject_credential_bearing_urls(tmp_path):
    server, thread = serve(tmp_path)
    try:
        for ident, value in (('aethermind.url', 'http://127.0.0.1:9/mcp'),
                             ('aethermind.project', '/synthetic-project')):
            code, body = call(server, 'POST', '/api/settings', {'id': ident, 'value': value})
            assert code == 200 and body['restart_required'] is True
        refused = call(server, 'POST', '/api/settings', {'id': 'aethermind.enabled', 'value': False})
        assert refused[0] == 400
        saved = load_config(tmp_path).aethermind
        assert saved.url == 'http://127.0.0.1:9/mcp'
        assert saved.project == '/synthetic-project' and saved.create_project is False
        before = (tmp_path / 'config.toml').read_text()
        code, _ = call(server, 'POST', '/api/settings', {
            'id': 'aethermind.url', 'value': 'https://user:secret@example.com/mcp'})
        assert code == 400
        assert (tmp_path / 'config.toml').read_text() == before
    finally:
        stop(server, thread)


def test_required_fields_come_before_optional_before_advanced(tmp_path):
    """A required field such as the basic settings required to be at all functional
    should come first, clearly. The rest is ranked based on optional configurations
    and advanced customizations and grouped accordingly.

    Jev is the example: enabled and the key are all that is required, and the
    public URL sits below them.
    """
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    judge = next(section for section in body['sections'] if section['id'] == 'judge')
    order = [field['id'] for field in judge['fields']]
    # The two switches that make the judge exist at all, and the key that makes it
    # work, come first.
    assert order[:2] == ['jev.enabled', 'jev.action_gate_enabled']
    # The credential is the visible choice; the environment variable is the
    # advanced fallback below it.
    assert order[2] == 'jev.credential_ref'
    assert order.index('jev.api_key_env') > order.index('jev.credential_ref')
    # The optional URL is ranked advanced and sits below the credential.
    assert order.index('jev.url') > order.index('jev.credential_ref')
    assert order[-1] in ('jev.loop_window', 'jev.batch_items')


def test_tier_ranks_are_stable_and_unranked_fields_stay_visible(tmp_path):
    """The sort must not reorder inside a rank: declaration order is the author's
    choice, and a stable sort keeps it. An unranked field defaults to 'required',
    so a new Spec can never be pushed out of sight by omission."""
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    judge = next(section for section in body['sections'] if section['id'] == 'judge')
    tiers = [field.get('tier', 'required') for field in judge['fields']]
    ranks = {'required': 0, 'optional': 1, 'advanced': 2}
    assert [ranks[tier] for tier in tiers] == sorted(ranks[tier] for tier in tiers)
    # Within 'optional' the declaration order survives: timeout, gate bar, goal bar,
    # risk bar, human gate, session gates.
    optional = [field['id'] for field in judge['fields'] if field.get('tier') == 'optional']
    assert optional == ['jev.timeout', 'jev.gate_threshold', 'jev.goal_threshold',
                        'jev.risk_threshold', 'jev.human_gate', 'jev.session_gates_enabled']
    # Required is explicit too, so consumers do not need to infer it.
    required = [field for field in judge['fields'] if field.get('tier') == 'required']
    assert len(required) == 3


def test_permission_is_one_live_switch(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'yolo'})
        assert code == 200 and body['applied'] == 'live' and body['approval']['yolo'] is True
        assert server.manager.config.tools.approval == 'yolo'
        code, body = call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'safe'})
        assert body['approval']['yolo'] is False and body['approval']['mode'] == 'ask'
        assert call(server, 'POST', '/api/settings', {'id': 'permissions.mode', 'value': 'ask'})[0] == 400
    finally:
        stop(server, thread)


def test_a_status_is_not_the_same_thing_as_inert(tmp_path):
    """A field can carry a status and still be the live control.

    permissions.mode reports "Safe asks" while remaining the switch a person flips, so the
    page cannot infer inertness from "this field has a status". That is why the projection
    carries a structured `inert` flag instead.
    """
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    fields = {field['id']: field for section in body['sections'] for field in section['fields']
              if field.get('id')}
    assert fields['permissions.mode']['status'] == 'Safe asks'
    assert not fields['permissions.mode'].get('inert')


def test_a_superseded_field_is_marked_inert_and_names_the_way_back(tmp_path):
    """The URL box is refused while ordered endpoints exist, so it must say so structurally.

    A box that looks editable and answers "that changed nothing" on save must say
    so structurally, so the page can tell it apart from a working field.
    """
    text = HOME + '''
[aethermind]
url = "http://127.0.0.1:9/mcp"
endpoints = ["http://127.0.0.1:8452/mcp"]
project = "/synthetic-project"
'''
    server, thread = serve(tmp_path, text=text)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    fields = {field['id']: field for section in body['sections'] for field in section['fields']
              if field.get('id')}
    url = fields['aethermind.url']
    assert url['inert'] is True
    # The way out is named, not just the conflict.
    assert 'Clear that list' in url['status']
    # Only the superseded field carries the flag; the field that supersedes it stays
    # editable and the key is absent rather than false.
    assert 'inert' not in fields['aethermind_endpoints']


def test_the_url_is_not_marked_inert_when_no_endpoint_supersedes_it(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    fields = {field['id']: field for section in body['sections'] for field in section['fields']
              if field.get('id')}
    assert not fields['aethermind.url'].get('inert')


def test_provider_and_mcp_add_then_remove(tmp_path):
    server, thread = serve(tmp_path)
    try:
        added = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'local', 'kind': 'openai_chat', 'base_url': 'http://127.0.0.1:9/v1', 'model': 'local-model',
            'chat': True}})
        assert added[0] == 200 and 'local' in load_config(tmp_path).backends
        assert call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'remove', 'name': 'local'})[0] == 200
        assert 'local' not in load_config(tmp_path).backends
        # The chat model is reassigned to the remaining chat backend, so the
        # last-model refusal lives in the dedicated removal test.
        assert call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'remove', 'name': 'fake'})[0] == 200
        assert load_config(tmp_path).roles['main'] == 'grok'
        server_added = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'tools', 'enabled': True, 'command': 'tool-server', 'args': '--flag\nvalue', 'env': 'TOKEN=voice'}})
        assert server_added[0] == 200 and server_added[1]['restart_required'] is False
        assert server_added[1]['applied'] == 'live'
        saved = load_config(tmp_path).mcp_servers['tools']
        assert saved.command == ['tool-server', '--flag', 'value'] and saved.env == {'TOKEN': 'voice'} and not saved.url
        both = call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'add', 'record': {
            'name': 'both', 'url': 'http://127.0.0.1:9/mcp', 'command': 'tool-server'}})
        assert both[0] == 400 and 'both' not in (tmp_path / 'config.toml').read_text()
        assert call(server, 'POST', '/api/settings', {'id': 'mcp_servers', 'action': 'remove', 'name': 'tools'})[0] == 200
        assert 'tools' not in load_config(tmp_path).mcp_servers
    finally:
        stop(server, thread)


def test_removing_a_provider_reassigns_optional_roles_to_a_local_model(tmp_path):
    text = HOME.replace(
        '[roles]\nmain = "fake"\nfailover = "grok"',
        '[roles]\nmain = "fake"\nfast = "grok"\ncompactor = "grok"\nfailover = "grok"',
    )
    assert 'fast = "grok"' in text
    server, thread = serve(tmp_path, text)
    try:
        removed = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'remove', 'name': 'grok'})
        assert removed[0] == 200
        config = load_config(tmp_path)
        assert 'grok' not in config.backends
        assert config.roles == {
            'main': 'fake', 'fast': 'fake', 'compactor': 'fake', 'failover': 'fake'}
        assert 'now uses fake' in (removed[1] or {}).get('detail', '')
        refused = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'remove', 'name': 'fake'})
        assert refused[0] == 400
        error = (refused[1] or {}).get('error', '')
        assert 'Add another chat model' in error
        assert 'fake' in load_config(tmp_path).backends
    finally:
        stop(server, thread)


def test_removing_a_cloud_provider_prefers_a_local_model_for_every_role(tmp_path):
    text = HOME + '''
[backends.cloud]
base_url = "http://127.0.0.1:9/v1"
model = "cloud-model"
api_key_env = "EXAMPLE_CLOUD_KEY"
chat = true
enabled = true
'''
    text = text.replace(
        '[roles]\nmain = "fake"\nfailover = "grok"',
        '[roles]\nmain = "cloud"\nfast = "cloud"\ncompactor = "cloud"\nfailover = "cloud"',
    )
    assert 'fast = "cloud"' in text
    server, thread = serve(tmp_path, text)
    try:
        removed = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'remove', 'name': 'cloud'})
        assert removed[0] == 200
        config = load_config(tmp_path)
        assert 'cloud' not in config.backends
        assert config.roles == {
            'main': 'fake', 'fast': 'fake', 'compactor': 'fake', 'failover': 'fake'}
    finally:
        stop(server, thread)


def test_fast_and_compactor_roles_are_editable_in_settings(tmp_path):
    text = HOME.replace(
        '[roles]\nmain = "fake"\nfailover = "grok"',
        '[roles]\nmain = "fake"\nfast = "grok"\ncompactor = "grok"\nfailover = "grok"',
    )
    server, thread = serve(tmp_path, text)
    try:
        code, body = call(server, 'GET', '/api/settings')
        assert code == 200
        body = body or {}
        fields = {field['id']: field for section in body['sections']
                  for field in section['fields'] if field.get('id')}
        assert fields['roles.fast']['value'] == 'grok'
        assert fields['roles.compactor']['value'] == 'grok'
        changed = call(server, 'POST', '/api/settings', {
            'id': 'roles.fast', 'value': 'fake'})
        assert changed[0] == 200
        assert load_config(tmp_path).roles['fast'] == 'fake'
    finally:
        stop(server, thread)


def test_secret_is_stored_in_the_fake_keychain_and_nowhere_else(tmp_path, capsys):
    server, thread = serve(tmp_path)
    secret = 'synthetic-keychain-value'
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'add', 'record': {
            'name': 'desk', 'keychain_service': 'example-service', 'keychain_account': 'desk', 'secret': secret}})
        assert code == 200 and body['changed'] is True
        text = (tmp_path / 'config.toml').read_text()
        assert secret not in text and secret not in json.dumps(body)
        assert '"keychain_service" = "example-service"' in text and '"keychain_account" = "desk"' in text
        assert server.manager.settings.keychain.items[('example-service', 'desk')] == secret
        again = call(server, 'GET', '/api/settings')
        assert secret not in json.dumps(again[1])
        view = project(server.manager.config, server.manager.approval_status())
        assert secret not in json.dumps(view)
        removed = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'remove', 'name': 'desk'})
        assert removed[0] == 200 and ('example-service', 'desk') not in server.manager.settings.keychain.items
        assert 'desk' not in load_config(tmp_path).credentials
    finally:
        stop(server, thread)
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err


def test_grok_api_key_fallback_is_explicit_and_login_is_not_offered(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'providers', 'action': 'edit', 'name': 'grok', 'record': {'allow_api_key_fallback': True}})
        assert code == 200
        cli = load_config(tmp_path).backends['grok'].extra['cli']
        assert cli['allow_api_key_fallback'] is True and cli['fallback_env'] == 'EXAMPLE_GROK_KEY'
        names = []
        for section in body['settings']['sections']:
            for field in section['fields']:
                if field['id'] != 'providers':
                    continue
                for record in field['records']:
                    names.append(record['name'])
        assert 'grok' in names
        refused = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'connect', 'name': 'grok'})
        assert refused[0] == 400
    finally:
        stop(server, thread)


def test_linux_keychain_helper_takes_the_secret_on_stdin(capsys):
    secret = 'synthetic-stdin-secret'
    seen = {}

    def run(argv, **kwargs):
        seen['argv'] = argv
        seen['input'] = kwargs.get('input')
        return subprocess.CompletedProcess(argv, 0, '', '')

    store = KeychainStore(platform='linux', which=lambda name: '/usr/bin/' + name, run=run)
    store.put('example-service', 'example-account', secret)
    assert seen['argv'][:3] == ['/usr/bin/secret-tool', 'store', '--label']
    assert secret not in seen['argv'] and seen['input'] == secret
    assert secret not in capsys.readouterr().out


def test_darwin_keychain_argv_is_built_but_not_executed_here():
    seen = {}

    def run(argv, **kwargs):
        seen['argv'] = list(argv)
        return subprocess.CompletedProcess(argv, 0, '', '')

    store = KeychainStore(platform='darwin', which=lambda name: name, run=run)
    store.put('example-service', 'example-account', 'synthetic-darwin-secret')
    assert seen['argv'][:2] == ['security', 'add-generic-password']
    assert '-s' in seen['argv'] and 'example-service' in seen['argv']


def test_oauth_model_add_uses_the_xai_grant_and_not_a_key(tmp_path):
    server, thread = serve(tmp_path)
    try:
        added = call(server, 'POST', '/api/settings', {'id': 'providers', 'action': 'add', 'record': {
            'name': 'grok-fast', 'account': 'xai', 'model': 'grok-4.7', 'chat': True}})
        assert added[0] == 200, added[1]
        saved = load_config(tmp_path).backends['grok-fast']
        assert saved.base_url == 'https://api.x.ai/v1'
        assert saved.model == 'grok-4.7'
        assert saved.credential_ref == 'xai-oauth'
        assert saved.provider_kind == 'openai_chat'
        assert saved.api_key_env == ''
    finally:
        stop(server, thread)


def test_rejected_credential_add_does_not_destroy_the_existing_key(tmp_path):
    """An add rejected for a bad env name must not have already replaced the key.

    Reproduced before the fix: the secret was written to the keychain first and the
    env name validated afterwards, so a 400 arrived with a working credential gone.
    """
    server, thread = serve(tmp_path)
    kc = server.manager.settings.keychain
    kc.put('example-service', 'example-voice', 'the-working-voice-key')
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'add', 'record': {
            'name': 'desk', 'keychain_service': 'example-service', 'keychain_account': 'example-voice',
            'secret': 'replacement-key', 'env': 'not a valid name'}})
        assert code == 400 and body['error'] == 'Env var must be a name'
        assert kc.items[('example-service', 'example-voice')] == 'the-working-voice-key'
        assert 'desk' not in load_config(tmp_path).credentials
        assert 'voice' in load_config(tmp_path).credentials
    finally:
        stop(server, thread)


def test_credential_add_refuses_a_slot_another_credential_owns(tmp_path):
    """Two credentials on one keychain slot mean one reads the other's secret."""
    server, thread = serve(tmp_path)
    kc = server.manager.settings.keychain
    kc.put('example-service', 'example-voice', 'the-working-voice-key')
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'add', 'record': {
            'name': 'desk', 'keychain_service': 'example-service', 'keychain_account': 'example-voice',
            'secret': 'replacement-key'}})
        assert code == 400 and 'voice' in body['error']
        assert kc.items[('example-service', 'example-voice')] == 'the-working-voice-key'
        assert 'desk' not in load_config(tmp_path).credentials
    finally:
        stop(server, thread)


def test_rejected_credential_edit_does_not_destroy_the_existing_key(tmp_path):
    server, thread = serve(tmp_path)
    kc = server.manager.settings.keychain
    kc.put('example-service', 'example-voice', 'the-working-voice-key')
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'edit', 'name': 'voice',
                                                           'record': {'secret': 'replacement-key',
                                                                      'env': 'also not a name'}})
        assert code == 400 and body['error'] == 'Env var must be a name'
        assert kc.items[('example-service', 'example-voice')] == 'the-working-voice-key'
    finally:
        stop(server, thread)


def test_secret_only_edit_rotates_the_key_at_its_configured_slot(tmp_path):
    """The page sends only the secret on an edit. It must reach the credential's
    own slot rather than fail for want of a destination the config already names."""
    server, thread = serve(tmp_path)
    kc = server.manager.settings.keychain
    kc.put('example-service', 'example-voice', 'the-working-voice-key')
    try:
        code, body = call(server, 'POST', '/api/settings', {'id': 'credentials', 'action': 'edit', 'name': 'voice',
                                                           'record': {'secret': 'rotated-key'}})
        assert code == 200 and body['changed'] is True
        assert kc.items[('example-service', 'example-voice')] == 'rotated-key'
        assert 'rotated-key' not in (tmp_path / 'config.toml').read_text()
    finally:
        stop(server, thread)


def test_skins_and_custom_accent_save_live_and_restore_defaults(tmp_path):
    server, thread = serve(tmp_path)
    try:
        for skin in ('basalt', 'tide', 'strata'):
            code, result = call(server, 'POST', '/api/settings', {'id': 'ui.skin', 'value': skin})
            assert code == 200
            assert result['restart_required'] is False
            assert server.manager.config.ui.skin == skin
            assert load_config(tmp_path).ui.skin == skin
        code, result = call(server, 'POST', '/api/settings', {'id': 'ui.accent_color', 'value': '#C79D65'})
        assert code == 200
        assert server.manager.config.ui.accent_color == '#c79d65'
        assert call(server, 'GET', '/api/status')[1]['ui']['accent_color'] == '#c79d65'
        for invalid in ('red', '#12', 'url(example)', 4):
            assert call(server, 'POST', '/api/settings', {'id': 'ui.accent_color', 'value': invalid})[0] == 400
        assert call(server, 'POST', '/api/settings', {'id': 'ui.skin', 'value': 'unknown'})[0] == 400
        assert call(server, 'POST', '/api/settings', {'id': 'ui.accent_color', 'value': ''})[0] == 200
        assert load_config(tmp_path).ui.accent_color == ''
    finally:
        stop(server, thread)


def test_every_mcp_field_explains_its_expected_shape(tmp_path):
    """A plugin field has to say what it accepts.

    URL, command, arguments, environment, and token env are different shapes.
    The hint is the only guidance before a save is refused.
    """
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    plugins = next(s for s in body['sections'] if s['id'] == 'plugins')
    field = next(f for f in plugins['fields'] if f['id'] == 'mcp_servers')
    assert field['hint'], 'the list itself has to say what a row is'
    # The blank form is what an operator actually fills in first.
    for item in field['blank']:
        if item['key'] == 'enabled':
            continue
        assert item.get('hint'), f'{item["key"]} accepts input with no guidance'
    # URL and Command are mutually exclusive; each hint has to say so.
    hints = {item['key']: item.get('hint', '') for item in field['blank']}
    assert 'Leave empty when using Command' in hints['url']
    assert 'Leave empty when using URL' in hints['command']
    assert 'per line' in hints['args']
    assert 'passed as-is' in hints['env']
    assert 'actual value' in hints['env']
    assert 'stored in the config file' in hints['env']
    assert 'do not put secrets' in hints['env']
    assert 'not looked up' in hints['env']
    assert 'environment variable name' not in hints['env']
    assert 'not a secret' not in hints['env']
    assert 'never saved' in hints['token_env']
    assert 'including in Unleashed' in hints['enabled']
    assert 'including in Unleashed' in hints['session_tools']
    assert len(hints['enabled']) <= 120
    assert len(hints['session_tools']) <= 120
    plugins_section = next(s for s in body['sections'] if s['id'] == 'plugins')
    assert 'stay off when you turn them off, in Unleashed too' in plugins_section['start_hint']
    assert 'Unleashed only drops approvals, warnings, and default limits' in plugins_section['start_hint']
    assert 'stay off when you turn them off, in Unleashed too' in field['hint']


def test_a_saved_mcp_record_carries_the_same_guidance_as_the_blank_form(tmp_path):
    """Editing an existing server is where the guidance matters most: the
    fields are already populated, so a wrong shape looks legitimate."""
    text = HOME + '''
[mcp_servers.tools]
url = "http://127.0.0.1:9/mcp"
'''
    server, thread = serve(tmp_path, text)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    plugins = next(s for s in body['sections'] if s['id'] == 'plugins')
    field = next(f for f in plugins['fields'] if f['id'] == 'mcp_servers')
    record = next(r for r in field['records'] if r['name'] == 'tools')
    hints = {item['key']: item.get('hint', '') for item in record['fields']}
    assert hints['url'] and hints['command'] and hints['args'] and hints['env']


def test_mcp_env_remains_in_model_for_both_transports(tmp_path):
    text = HOME + '''
[mcp_servers.remote]
url = "http://127.0.0.1:9/mcp"
env = { TOKEN = "REMOTE_TOKEN" }

[mcp_servers.local]
command = ["tool-server"]
env = { TOKEN = "LOCAL_TOKEN" }
'''
    server, thread = serve(tmp_path, text)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    plugins = next(s for s in body['sections'] if s['id'] == 'plugins')
    field = next(f for f in plugins['fields'] if f['id'] == 'mcp_servers')
    records = {record['name']: record for record in field['records']}
    for name, expected in [('remote', 'TOKEN=REMOTE_TOKEN'), ('local', 'TOKEN=LOCAL_TOKEN')]:
        env = next(item for item in records[name]['fields'] if item['key'] == 'env')
        assert env['value'] == expected
        assert not env.get('hidden', False)
        assert 'Command (stdio)' in env['hint']
        assert 'remote' in env['hint']


def test_mcp_save_without_env_preserves_stored_map(tmp_path):
    text = HOME + '''
[mcp_servers.remote]
url = "http://127.0.0.1:9/mcp"
env = { TOKEN = "REMOTE_TOKEN" }
'''
    server, thread = serve(tmp_path, text)
    try:
        code, body = call(server, 'POST', '/api/settings', {
            'id': 'mcp_servers', 'action': 'edit', 'name': 'remote',
            'record': {'url': 'http://127.0.0.1:10/mcp', 'command': '', 'enabled': False}})
        assert code == 200, body
        saved = load_config(tmp_path).mcp_servers['remote']
        assert saved.url == 'http://127.0.0.1:10/mcp'
        assert saved.enabled is False
        assert saved.env == {'TOKEN': 'REMOTE_TOKEN'}
    finally:
        stop(server, thread)


def test_mcp_env_visibility_uses_record_transport():
    web = Path(__file__).resolve().parents[1] / 'web'
    source = (web / 'settings.js').read_text()
    assert 'function mcpTransport(fields)' in source
    assert 'item.key === "url"' in source
    assert 'item.key === "command"' in source
    assert 'if (url?.value?.trim()) return "http";' in source
    assert 'if (command?.value?.trim()) return "stdio";' in source
    assert 'return "unknown";' in source
    assert 'function showKey(listId, key, kind, account, fields)' in source
    assert 'if (listId === "mcp_servers" && key === "env")' in source
    assert 'if (mcpTransport(fields) === "http") return false;' in source
    assert 'showKey(listId, field.key, kind, account, fields)' in source
    assert 'showKey(list.id, field.key, kind, account, fields)' in source
    helpers = source[source.index('    function mcpTransport(fields)'):source.index('    function readForm(')]
    result = subprocess.run(['node', '-e', helpers + '''
const assert = require("node:assert/strict");
for (const [fields, transport, visible] of [
  [[{key: "url", value: "http://127.0.0.1:9/mcp"}], "http", false],
  [[{key: "command", value: "tool-server"}], "stdio", true],
  [[{key: "url", value: ""}, {key: "command", value: ""}], "unknown", true],
]) {
  assert.equal(mcpTransport(fields), transport);
  assert.equal(showKey("mcp_servers", "env", "", "", fields), visible);
}
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_mcp_env_literal_values_are_saved_as_is(tmp_path):
    expected = {
        'SURFACE_ID': 'example-surface',
        'CANON_PACKET_URL': 'http://example.test:8433/path?key=value',
        'PLAIN': 'identifier',
        'MYKEY': 'MY_SECRET_ENV_NAME',
        'LIMIT': 'x' * 500,
    }
    server, thread = serve(tmp_path)
    try:
        for index, env in enumerate(['\n'.join(f'{key}={value}' for key, value in expected.items()), expected]):
            name = f'tools{index}'
            code, body = call(server, 'POST', '/api/settings', {
                'id': 'mcp_servers', 'action': 'add',
                'record': {'name': name, 'command': 'tool-server', 'env': env}})
            assert code == 200, body
            assert load_config(tmp_path).mcp_servers[name].env == expected
    finally:
        stop(server, thread)


def test_mcp_malformed_env_refs_are_still_rejected(tmp_path):
    server, thread = serve(tmp_path)
    try:
        for env, error in [('TOKEN', 'Env entries are KEY=value lines'),
                           ('1BAD=x', 'Env keys must be environment variable names'),
                           ({'BAD\n': 'x'}, 'Env keys must be environment variable names'),
                           ('TOKEN=', 'Invalid env value'),
                           ('TOKEN=' + 'x' * 501, 'Invalid env value'),
                           ('TOKEN=x\x00y', 'Invalid env value'),
                           ({'TOKEN': 'x\ny'}, 'Invalid env value'),
                           ({'TOKEN': 'x\ry'}, 'Invalid env value'),
                           ({'TOKEN': 1}, 'Invalid env value')]:
            code, body = call(server, 'POST', '/api/settings', {
                'id': 'mcp_servers', 'action': 'add',
                'record': {'name': 'tools', 'command': 'tool-server', 'env': env}})
            assert code == 400
            assert error in str(body)
    finally:
        stop(server, thread)


def test_judge_threshold_hints_disclose_fixed_contextual_policy(tmp_path):
    server, thread = serve(tmp_path)
    try:
        code, body = call(server, 'GET', '/api/settings')
    finally:
        stop(server, thread)
    assert code == 200
    judge = next(section for section in body['sections'] if section['id'] == 'judge')
    fields = {field['id']: field for field in judge['fields']}
    gate = fields['jev.gate_threshold']
    risk = fields['jev.risk_threshold']
    assert gate['label'] == 'Session gate bar'
    assert risk['label'] == 'Risk bar'
    assert 'session, merge and completion gates' in gate['hint']
    assert 'session-tool risk enforcement' in risk['hint']
    for field in (gate, risk):
        assert field['tier'] == 'optional'
        assert 'contextual action gate' in field['hint']
        assert 'fixed' in field['hint']
        assert '0.7 deny / 0.20 soft floor' in field['hint']


def test_computer_setup_precedes_tuning_and_explains_jev(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    section = next(s for s in project(config, {})['sections'] if s['id'] == 'computer')
    fields = section['fields']
    assert [f['id'] for f in fields[:3]] == [
        'computer_use.provider', 'computer_use.jev.enabled', 'computer_use.jev.credential_ref']
    assert fields[3]['id'] == 'computer_use.jev.min_confidence'
    assert fields[3]['tier'] == 'optional'
    assert len([f for f in fields if f.get('tier') == 'advanced']) == 4
    enabled = fields[1]
    assert enabled['label'] == 'Use Jev for screen questions'
    assert 'Native computer use asks Jev' in enabled['hint']
    assert 'Session judge (Jev)' in enabled['hint']


def test_voice_setup_precedes_preferences_and_advanced_controls(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    # Both the free-text and provider-supplied voice picker carry the same rank.
    for voices in ([], ['example-voice']):
        config.voice.voices = voices
        section = next(s for s in project(config, {})['sections'] if s['id'] == 'voice')
        fields = section['fields']
        assert [f['id'] for f in fields if f.get('tier') == 'required'] == [
            'voice.enabled', 'voice.account', 'voice.model', 'voice.websocket_url', 'voice.pipe']
        assert [f['id'] for f in fields if f.get('tier') == 'optional'] == [
            'voice.stt.base_url', 'voice.stt.model', 'voice.tts.base_url', 'voice.tts.model',
            'voice.voice', 'voice.dedicated_title']
        assert [f['id'] for f in fields if f.get('tier') == 'advanced'] == [
            'voice.max_session_seconds', 'voice.dedicated_reasoning', 'voice.dedicated_thinking_budget']
        ranks = {'required': 0, 'optional': 1, 'advanced': 2}
        order = [ranks[f.get('tier', 'required')] for f in fields]
        assert order == sorted(order)


def test_autonomy_setup_precedes_preferences_and_advanced_controls(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    section = next(s for s in project(load_config(tmp_path), {})['sections'] if s['id'] == 'autonomy')
    fields = section['fields']
    # Personal priorities are consumed by the autonomy floor wake
    # (tessera/autonomy.py:_queue_floor_wake), so this control keeps its rank.
    assert [f['id'] for f in fields if f.get('tier') == 'required'] == [
        'autonomy.enabled', 'autonomy.world_view']
    assert [f['id'] for f in fields if f.get('tier') == 'optional'] == ['autonomy.oversight_policy']
    assert [f['id'] for f in fields if f.get('tier') == 'advanced'] == [
        'autonomy.infinity_slug', 'autonomy.min_wake_gap_seconds',
        'autonomy.self_prompt_gap_seconds', 'autonomy.trial_poll_seconds']
    ranks = {'required': 0, 'optional': 1, 'advanced': 2}
    order = [ranks[f.get('tier', 'required')] for f in fields]
    assert order == sorted(order)
    assert all(len(f.get('hint', '')) <= 140 for f in fields)


def test_identity_defaults_precede_advanced_session_naming(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    config = load_config(tmp_path)
    section = next(s for s in project(config, {})['sections'] if s['id'] == 'identity')
    fields = section['fields']
    # Neither name blocks setup: config supplies Tessera and "the operator".
    assert config.agent_name == 'Tessera'
    assert config.operator_label() == 'the operator'
    assert config.floor_label() == 'Autonomy'
    assert {f['id']: f.get('tier', 'required') for f in fields} == {
        'identity.enabled': 'required',
        'agent_name': 'optional',
        'operator_name': 'optional',
        'autonomy.floor_name': 'optional',
    }
    assert [f['id'] for f in fields] == [
        'identity.enabled', 'agent_name', 'operator_name', 'autonomy.floor_name']


def test_floor_display_name_and_session_slug_have_distinct_guidance(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    sections = project(load_config(tmp_path), {})['sections']
    fields = {}
    for section in sections:
        for field in section['fields']:
            fields[field['id']] = field
    display_name = fields['autonomy.floor_name']
    session_slug = fields['autonomy.infinity_slug']

    assert display_name['label'] == 'Floor display name'
    assert session_slug['label'] == 'Floor session slug'
    assert display_name['label'] != session_slug['label']
    assert 'name shown for the autonomy session' in display_name['hint']
    assert 'address is a separate setting: Floor session slug' in display_name['hint']
    assert 'Empty means "Autonomy"' in display_name['hint']
    assert 'session address' in session_slug['hint']
    assert 'state and cockpit lane are keyed under' in session_slug['hint']
    assert 'not its display name' in session_slug['hint']


def test_appearance_controls_are_optional_preferences(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    section = next(s for s in project(load_config(tmp_path), {})['sections'] if s['id'] == 'appearance')
    assert {f['id']: f.get('tier', 'required') for f in section['fields']} == {
        'ui.theme': 'optional',
        'ui.mark': 'optional',
        'ui.skin': 'optional',
        'ui.accent_color': 'optional',
        'ui.show_thoughts': 'optional',
        'ui.yolo_banner': 'optional',
        'ui.send_shortcut': 'optional',
        'ui.config_journal_entries': 'advanced',
    }
    # F-it lives in Goblin Mode now, not Appearance. The button keeps its cartoon.
    goblin = next(s for s in project(load_config(tmp_path), {})['sections'] if s['id'] == 'goblin')
    assert [f['id'] for f in goblin['fields']] == ['ui.easter_egg_f_it']


def test_models_keep_chat_prerequisites_before_optional_failover(tmp_path):
    (tmp_path / 'config.toml').write_text('')
    config = load_config(tmp_path)
    assert config.backends == {}
    assert config.roles == {}
    section = next(s for s in project(config, {})['sections'] if s['id'] == 'models')
    assert {f['id']: f.get('tier', 'required') for f in section['fields']} == {
        'roles.main': 'required',
        'roles.fast': 'optional',
        'roles.compactor': 'optional',
        'roles.failover': 'optional',
        'worker_policy': 'advanced',
        # Advanced so it stays behind the chat prerequisites.
        'swarm.pressure_hold_enabled': 'advanced',
        'swarm.pressure_soft_threshold': 'advanced',
        'swarm.max_job_tasks': 'advanced',
        'swarm.distinct_workers': 'advanced',
        'codex.models_url': 'advanced',
        'codex.client_version': 'advanced',
    }
    assert [f['id'] for f in section['fields']] == [
        'roles.main', 'roles.fast', 'roles.compactor', 'roles.failover',
        'swarm.pressure_hold_enabled', 'swarm.pressure_soft_threshold',
        'swarm.max_job_tasks', 'swarm.distinct_workers',
        'codex.models_url', 'codex.client_version', 'worker_policy']


def test_taxonomy_preserves_empty_accounts_and_record_tiers(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    sections = {s['id']: s for s in project(load_config(tmp_path), {})['sections']}
    accounts = sections['accounts']['fields']
    assert len(accounts) == 1
    # The service-account page is the accounts registry, not a placeholder.
    assert accounts[0]['id'] == 'named_accounts'
    assert accounts[0]['records'] == [] and accounts[0]['actions'] == ['add', 'edit', 'remove']
    assert accounts[0]['blank'], 'an empty page still needs a form to add one'
    assert 'sign-in' in accounts[0]['hint']
    assert [f['id'] for f in sections['providers']['fields']] == ['providers', 'oauth_accounts']
    assert sections['providers']['fields'][0]['tier'] == 'required'
    assert sections['providers']['fields'][1]['tier'] == 'optional'
    ids = [f['id'] for s in sections.values() for f in s['fields']]
    assert len(ids) == len(set(ids))


def test_previously_ranked_sections_keep_their_tier_counts(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    sections = {s['id']: s for s in project(load_config(tmp_path), {})['sections']}
    for section, expected in [('computer', (3, 1, 4)), ('judge', (3, 6, 5))]:
        tiers = [f.get('tier', 'required') for f in sections[section]['fields']]
        assert tuple(tiers.count(tier) for tier in ('required', 'optional', 'advanced')) == expected


def test_raw_config_adopt_updates_running_core(tmp_path):
    text = HOME + '\n[server]\nport = 0\n'
    server, thread = serve(tmp_path, text)
    try:
        path = tmp_path / 'config.toml'
        path.write_text('agent_name = "Adopted"\n' + text)
        assert server.manager.config.agent_name == 'Tessera'
        code, body = call(server, 'POST', '/api/config', {'adopt': True})
        assert code == 200
        assert server.manager.config.agent_name == 'Adopted'
        assert body['applied'] == 'live'
        assert body['changed'] == [['agent_name']]
        assert body['running_core'] == {'known': True, 'agrees': True, 'pending': []}
        assert 're-read config.toml' in body['detail']
    finally:
        stop(server, thread)


def test_raw_config_adopt_noop_reports_no_changes(tmp_path):
    server, thread = serve(tmp_path, HOME + '\n[server]\nport = 0\n')
    try:
        code, body = call(server, 'POST', '/api/config', {'adopt': True})
        assert code == 200
        assert body['applied'] == 'live'
        assert body['changed'] == []
        assert 'No values changed' in body['detail']
    finally:
        stop(server, thread)


def test_raw_config_adopt_rejects_unreadable_file(tmp_path):
    server, thread = serve(tmp_path, HOME + '\n[server]\nport = 0\n')
    try:
        previous = server.manager.config
        path = tmp_path / 'config.toml'
        for text in (None, '[invalid'):
            if text is None:
                path.unlink()
            else:
                path.write_text(text)
            code, body = call(server, 'POST', '/api/config', {'adopt': True})
            assert code == 400
            assert 'could not be re-read' in body['error']
            assert server.manager.config is previous
    finally:
        stop(server, thread)


def test_raw_config_edits_do_not_adopt_even_with_adopt_flag(tmp_path):
    server, thread = serve(tmp_path, HOME + '\n[server]\nport = 0\n')
    try:
        code, body = call(server, 'POST', '/api/config', {
            'adopt': True, 'edits': [{'path': ['agent_name'], 'value': 'Written'}]})
        assert code == 200
        assert load_config(tmp_path).agent_name == 'Written'
        assert server.manager.config.agent_name == 'Tessera'
        assert body['changed'] is True
        assert body['applied'] == 'restart'
        assert body['running_core']['agrees'] is False
    finally:
        stop(server, thread)


def test_every_section_publishes_setup_hint_and_explicit_ordered_tiers(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME)
    sections = project(load_config(tmp_path), {})['sections']
    ranks = {'required': 0, 'optional': 1, 'advanced': 2}
    for section in sections:
        assert section['start_hint'].startswith('Start here: ')
        order = []
        for field in section['fields']:
            order.append(ranks[field['tier']])
        assert order == sorted(order), section['id']
    integrations = next(section for section in sections if section['id'] == 'integrations')
    tiers = {field['id']: field['tier'] for field in integrations['fields']}
    assert tiers['aethermind.project'] == 'required'
    assert tiers['aethermind_endpoints'] == 'optional'
    assert tiers['aethermind.bundled_path'] == 'advanced'
    assert tiers['aethermind.engine_log_max_bytes'] == 'advanced'


def test_autonomy_priorities_control_describes_its_actual_consumer(tmp_path):
    (tmp_path / 'config.toml').write_text(HOME + '\n[autonomy]\nworld_view = "Home matters"\n')
    section = next(s for s in project(load_config(tmp_path), {})['sections'] if s['id'] == 'autonomy')
    field = next(f for f in section['fields'] if f['id'] == 'autonomy.world_view')
    assert field['label'] == 'Autonomy priorities'
    assert field['value'] == 'Home matters'
    assert field['type'] == 'string'
    assert field['constraints']['lines'] is True
    assert 'each floor wake' in field['hint']
    assert 'not a fixed 2,000' in field['hint']
    assert 'Not a world-state connection' in field['hint']
    assert all(f['label'] != 'World view' for f in section['fields'])


def test_the_hosts_section_serves_the_rows_the_floor_reads(tmp_path):
    """The hosts section serves the rows the floor reads."""
    from tessera.config import load_config
    (tmp_path / 'config.toml').write_text(
        '[infrastructure]\nprometheus_url = "http://127.0.0.1:9090"\n'
        '[hosts.alpha]\nkind = "prometheus"\nlabel = "alpha"\n'
        'prometheus_instance = "node-exporter:9100"\ntimeout = 3.0\n'
        '[hosts.kiosk]\nkind = "local"\nlabel = "Kiosk"\n'
        'address = "probe.example.com:8765"\nprobe_command = ["curl", "-fsS", "http://probe.example.com:8765/health"]\n')
    config = load_config(tmp_path)
    from tessera import settings_model
    field = settings_model.hosts_field(config)
    assert field['type'] == 'list-of-records' and field['apply'] == 'restart'
    names = [row['name'] for row in field['records']]
    assert names == ['alpha', 'kiosk'], names
    kiosk = {x['key']: x['value'] for x in field['records'][1]['fields']}
    # The probe argv is shown one argument per line, and round-trips.
    assert kiosk['probe_command'] == 'curl\n-fsS\nhttp://probe.example.com:8765/health'
    assert kiosk['kind'] == 'local'
    assert field['blank'], 'a person needs an empty row to add one'


def test_a_host_added_through_the_page_reaches_the_floor(tmp_path):
    """The whole point: adding a host on the page must change what the floor reads."""
    from tessera.config import load_config
    (tmp_path / 'config.toml').write_text(
        '[infrastructure]\nprometheus_url = "http://127.0.0.1:9090"\n'
        '[hosts.alpha]\nkind = "prometheus"\nlabel = "alpha"\n'
        'prometheus_instance = "node-exporter:9100"\ntimeout = 3.0\n'
        '[backends.primary]\nbase_url = "https://api.x.ai/v1"\nmodel = "grok-4.7"\n'
        'api_key_env = "MODEL_API_KEY"\n[roles]\nmain = "primary"\n')
    from tessera.sessions import SessionManager
    config = load_config(tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    config.server.port = 0
    with SessionManager(config) as manager:
        result = manager.settings.apply({'id': 'hosts', 'action': 'add', 'record': {
            'name': 'newbox', 'kind': 'local', 'label': 'New box', 'enabled': True,
            'address': 'probe.example.com:8765', 'timeout': 4.0}})
        assert result['changed'] is True
        # The running config the floor reads now carries it...
        assert 'newbox' in manager.config.hosts
        assert manager.config.hosts['newbox'].label == 'New box'
        # ...and so does a fresh load of the file on disk.
        reloaded = load_config(tmp_path)
        assert 'newbox' in reloaded.hosts
        assert reloaded.hosts['newbox'].timeout == 4.0


def test_a_host_row_is_refused_when_the_loader_would_refuse_it(tmp_path):
    """A form must not be able to write a config the next load rejects."""
    from tessera.config import load_config
    (tmp_path / 'config.toml').write_text(
        '[infrastructure]\nprometheus_url = "http://127.0.0.1:9090"\n'
        '[hosts.alpha]\nkind = "prometheus"\nlabel = "alpha"\n'
        'prometheus_instance = "node-exporter:9100"\n'
        '[backends.primary]\nbase_url = "https://api.x.ai/v1"\nmodel = "grok-4.7"\n'
        'api_key_env = "MODEL_API_KEY"\n[roles]\nmain = "primary"\n')
    from tessera.sessions import SessionManager
    config = load_config(tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    config.server.port = 0
    with SessionManager(config) as manager:
        for record, expected in (
                ({'name': 'b1', 'kind': 'prometheus'}, 'Prometheus instance'),
                ({'name': 'b2', 'kind': 'nonsense'}, 'Unknown host kind'),
                ({'name': 'b3', 'kind': 'local', 'timeout': 0}, 'Timeout'),
                ({'name': 'b4', 'kind': 'local', 'timeout': True}, 'Timeout')):
            with pytest.raises(ValueError) as refused:
                manager.settings.apply({'id': 'hosts', 'action': 'add', 'record': record})
            assert expected in str(refused.value), (record, str(refused.value))
        # Nothing landed on disk from the refused writes.
        assert 'b1' not in load_config(tmp_path).hosts


def test_an_existing_host_can_be_edited_and_removed(tmp_path):
    from tessera.config import load_config
    (tmp_path / 'config.toml').write_text(
        '[infrastructure]\nprometheus_url = "http://127.0.0.1:9090"\n'
        '[hosts.alpha]\nkind = "prometheus"\nlabel = "alpha"\n'
        'prometheus_instance = "node-exporter:9100"\n'
        '[backends.primary]\nbase_url = "https://api.x.ai/v1"\nmodel = "grok-4.7"\n'
        'api_key_env = "MODEL_API_KEY"\n[roles]\nmain = "primary"\n')
    from tessera.sessions import SessionManager
    config = load_config(tmp_path)
    config.board.enabled = True
    config.jev.enabled = False
    config.server.port = 0
    with SessionManager(config) as manager:
        manager.settings.apply({'id': 'hosts', 'action': 'edit', 'name': 'alpha',
                                'record': {'label': 'alpha (Ubuntu)'}})
        assert load_config(tmp_path).hosts['alpha'].label == 'alpha (Ubuntu)'
        manager.settings.apply({'id': 'hosts', 'action': 'remove', 'name': 'alpha'})
        assert 'alpha' not in load_config(tmp_path).hosts
        with pytest.raises(ValueError) as gone:
            manager.settings.apply({'id': 'hosts', 'action': 'remove', 'name': 'alpha'})
        assert 'Unknown host' in str(gone.value)


def _systems_home(tmp_path):
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text(
        '[infrastructure]\nprometheus_url = "http://127.0.0.1:9090"\n'
        '[hosts.alpha]\nkind = "prometheus"\nlabel = "alpha"\n'
        'prometheus_instance = "node-exporter:9100"\n'
        '[backends.primary]\nbase_url = "https://api.x.ai/v1"\nmodel = "grok-4.7"\n'
        'api_key_env = "MODEL_API_KEY"\n[roles]\nmain = "primary"\n')
    return home


def _systems_manager(home, tmp_path):
    from tessera.sessions import SessionManager
    config = load_config(home)
    config.board.enabled = True
    config.jev.enabled = False
    config.server.port = 0
    return SessionManager(config)


def test_a_threshold_set_on_the_page_reaches_the_floor(tmp_path):
    """The floor reads a threshold a person sets on the page."""
    from tessera.systems import host_thresholds
    home = _systems_home(tmp_path)
    with _systems_manager(home, tmp_path) as manager:
        manager.settings.apply({'id': 'systems', 'action': 'add', 'record': {
            'id': 'alpha', 'label': 'alpha', 'kind': 'host',
            'role': 'the always-on model host', 'watch': True,
            'disk_free_percent_below': '15', 'managed': True}})
        # The running config carries it...
        record = manager.config.systems['alpha']
        assert record.role == 'the always-on model host'
        assert record.signals == {'disk_free_percent_below': 15, 'managed': True}
        # ...and so does a fresh load of the file, which is what the floor actually reads.
        reloaded = load_config(home)
        thresholds = host_thresholds(reloaded.systems, 'alpha')
        assert thresholds['disk_free_percent_below'] == 15
        assert thresholds['managed'] is True


def test_a_system_record_is_refused_when_the_loader_would_refuse_it(tmp_path):
    home = _systems_home(tmp_path)
    with _systems_manager(home, tmp_path) as manager:
        for record, expected in (
                ({'id': 'x', 'kind': 'host', 'role': ''}, 'needs a purpose'),
                ({'id': 'y', 'kind': 'endpoint', 'role': 'poll', 'watch': True}, 'needs an address'),
                ({'id': 'z', 'kind': 'nonsense', 'role': 'r'}, 'Unknown system kind'),
                ({'id': 'w', 'kind': 'host', 'role': 'r', 'disk_free_percent_below': '150'},
                 'between 0 and 100'),
                ({'id': 'v', 'kind': 'host', 'role': 'r', 'stale_after_seconds': '-1'}, 'positive')):
            with pytest.raises(ValueError) as refused:
                manager.settings.apply({'id': 'systems', 'action': 'add', 'record': record})
            assert expected in str(refused.value), (record, str(refused.value))
        assert load_config(home).systems == {}


def test_the_systems_section_serves_the_records_the_floor_reads(tmp_path):
    from tessera import settings_model
    home = _systems_home(tmp_path)
    (home / 'config.toml').write_text((home / 'config.toml').read_text() + (
        '\n[systems.alpha]\nkind = "host"\nlabel = "alpha"\n'
        'role = "the always-on model host"\nwatch = true\n'
        '[systems.alpha.signals]\ndisk_free_percent_below = 15\nmanaged = true\n'))
    config = load_config(home)
    field = settings_model.systems_field(config)
    assert field['type'] == 'list-of-records' and field['apply'] == 'restart'
    assert [row['name'] for row in field['records']] == ['alpha']
    values = {x['key']: x['value'] for x in field['records'][0]['fields']}
    assert values['role'] == 'the always-on model host'
    assert values['disk_free_percent_below'] == '15'
    assert values['managed'] is True
    assert field['blank'], 'a person needs an empty row to add one'


def test_settings_memory_status_quotes_an_unavailable_engine_lock(tmp_path, monkeypatch):
    from tessera.aethermind import EMBEDDED_LOCK_REFUSAL
    from tessera.config import Config
    from tessera.settings_model import project
    monkeypatch.setattr('tessera.engine_lock.embedded_lock_backend', lambda config: 'unavailable')
    approval = {'mode': 'deny_risky', 'yolo': False, 'configured': 'deny_risky',
                'safe_mode': 'deny_risky', 'switchable': True}
    page = project(Config(home=tmp_path, cwd=tmp_path), approval)
    fields = [field for section in page['sections'] if section['id'] == 'integrations'
              for field in section['fields']]
    memory = next(field for field in fields if field['id'] == 'aethermind.source')
    assert memory['status'] == EMBEDDED_LOCK_REFUSAL


def test_a_system_can_be_edited_and_removed(tmp_path):
    home = _systems_home(tmp_path)
    with _systems_manager(home, tmp_path) as manager:
        manager.settings.apply({'id': 'systems', 'action': 'add', 'record': {
            'id': 'alpha', 'kind': 'host', 'label': 'alpha', 'role': 'model host'}})
        manager.settings.apply({'id': 'systems', 'action': 'edit', 'name': 'alpha',
                                'record': {'role': 'the model host', 'watch': True}})
        assert load_config(home).systems['alpha'].role == 'the model host'
        assert load_config(home).systems['alpha'].watch is True
        manager.settings.apply({'id': 'systems', 'action': 'remove', 'name': 'alpha'})
        assert 'alpha' not in load_config(home).systems
