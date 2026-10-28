"""Synthetic JWTs only; never consult an account or a real credential file."""

import base64
import json
from pathlib import Path

import pytest

from tessera.codex_grant import grant_state, precheck


NOW = 1792972800  # A fixed instant, so refresh math does not follow the wall clock.


def jwt(claims):
    def encode(value):
        raw = json.dumps(value).encode('utf-8')
        return base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    return encode({'alg': 'HS256', 'typ': 'JWT'}) + '.' + encode(claims) + '.synthetic'


def write_auth(tmp_path, contents):
    path = tmp_path / 'auth.json'
    path.write_text(json.dumps(contents), encoding='utf-8')
    return path


def test_expired_access_token(tmp_path):
    path = write_auth(tmp_path, {'tokens': {'access_token': jwt({'exp': NOW - 3600}),
                                          'id_token': jwt({'exp': NOW - 7200})},
                                 'last_refresh': '2026-10-25T00:00:00Z'})
    result = grant_state(path, now=NOW)
    assert result['state'] == 'expired'
    assert result['access_exp'] == float(NOW - 3600)
    assert result['id_exp'] == float(NOW - 7200)
    assert result['last_refresh'] == '2026-10-25T00:00:00Z'
    assert '2026-10-25T23:00:00Z' in result['detail']
    assert '3600 seconds ago' in result['detail']
    assert precheck(path, now=NOW) == (False, result['detail'])


def test_future_access_token(tmp_path):
    path = write_auth(tmp_path, {'tokens': {'access_token': jwt({'exp': NOW + 3600}),
                                          'id_token': jwt({'exp': NOW - 1})}})
    result = grant_state(path, now=NOW)
    assert result['state'] == 'ok'
    assert isinstance(result['access_exp'], float)
    assert '2026-10-26T01:00:00Z' in result['detail']
    assert precheck(path, now=NOW) == (True, result['detail'])


def test_expiry_equal_to_now_is_expired(tmp_path):
    path = write_auth(tmp_path, {'tokens': {'access_token': jwt({'exp': NOW})}})
    assert grant_state(path, now=NOW)['state'] == 'expired'


def test_missing_file(tmp_path):
    path = tmp_path / 'absent.json'
    assert grant_state(path)['state'] == 'missing'
    assert precheck(path)[0] is False


def test_malformed_json(tmp_path):
    path = tmp_path / 'auth.json'
    path.write_text('{broken', encoding='utf-8')
    assert grant_state(path)['state'] == 'unreadable'
    assert precheck(path)[0] is False


@pytest.mark.parametrize('contents', [{}, {'OPENAI_API_KEY': 'synthetic'},
                                    {'tokens': None}, {'tokens': []}, {'tokens': {}}])
def test_no_access_token(tmp_path, contents):
    path = write_auth(tmp_path, contents)
    assert grant_state(path)['state'] == 'no_oauth'
    assert precheck(path)[0] is False


@pytest.mark.parametrize('token', ['opaque', 'a.%%%.b', 'a.e30.b', 'a.W10.b', None,
                                 jwt({'exp': 'tomorrow'}), jwt({'exp': True}),
                                 jwt({'exp': float('nan')}), jwt({'exp': float('inf')}),
                                 jwt({'exp': 10**1000})],
                         ids=['opaque', 'bad-base64', 'missing-exp', 'array-payload', 'null',
                              'text-exp', 'boolean-exp', 'nan-exp', 'infinite-exp', 'overflow-exp'])
def test_invalid_access_jwt(tmp_path, token):
    path = write_auth(tmp_path, {'tokens': {'access_token': token}})
    assert grant_state(path)['state'] == 'no_oauth'
    assert precheck(path)[0] is False


def test_non_object_json(tmp_path):
    path = write_auth(tmp_path, [])
    assert grant_state(path)['state'] == 'unreadable'


def test_permission_error(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError('synthetic-private-error')
    monkeypatch.setattr(Path, 'read_text', denied)
    result = grant_state(tmp_path / 'auth.json')
    assert result['state'] == 'unreadable'
    assert 'synthetic-private' not in result['detail']
    assert precheck(tmp_path / 'auth.json')[0] is False


def test_default_path_and_current_time(tmp_path, monkeypatch):
    directory = tmp_path / '.codex'
    directory.mkdir()
    write_auth(directory, {'tokens': {'access_token': jwt({'exp': NOW + 1})}})
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('tessera.codex_grant.time.time', lambda: NOW)
    assert grant_state()['state'] == 'ok'
    assert precheck()[0] is True


def test_invalid_id_token_does_not_block_valid_access(tmp_path):
    path = write_auth(tmp_path, {'tokens': {'access_token': jwt({'exp': NOW + 1}),
                                          'id_token': 'opaque'}, 'last_refresh': []})
    result = grant_state(path, now=NOW)
    assert result['state'] == 'ok'
    assert result['id_exp'] is None
    assert result['last_refresh'] is None
    assert set(result) == {'state', 'access_exp', 'id_exp', 'last_refresh', 'detail'}
