import json

from tessera.account_oauth import sign_in_openai, sign_in_xai
from tessera.oauth_login import LoginDesk


def test_openai_sign_in_sends_a_client_identity_and_keeps_the_status(tmp_path):
    seen = {}

    def poster(url, body, headers):
        seen['agent'] = headers.get('User-Agent')
        return 530, {'error': {'code': 530, 'message': 'A 1xxx error occured.', 'type': 'cf_route_error'}}

    try:
        sign_in_openai(tmp_path, tmp_path / 'status.json', poster=poster, sleep=lambda seconds: None, opener=False)
    except Exception as error:
        message = str(error)
    else:
        raise AssertionError('a rejected code request must fail')
    assert seen['agent'] == 'codex-cli'
    assert 'HTTP 530' in message
    assert '1xxx' in message
    desk = LoginDesk(tmp_path)
    (tmp_path / 'xai.json').write_text(json.dumps({
        'status': 'waiting',
        'verification_url': 'https://accounts.x.ai/device',
        'user_code': 'ABCD-1234',
        'message': 'Approve this code in the browser.',
        'access_token': 'must-not-leave-the-file',
    }), encoding='utf-8')
    status = desk.status('xai')
    assert status['user_code'] == 'ABCD-1234'
    assert 'access_token' not in status
    assert 'must-not-leave-the-file' not in json.dumps(status)


def test_sign_in_speaks_http_itself_and_does_not_call_another_program(tmp_path):
    calls = []

    def poster(url, body, headers):
        calls.append(url)
        if url.endswith('/device/code'):
            return 200, {
                'device_code': 'device-secret',
                'user_code': 'ABCD-1234',
                'verification_uri_complete': 'https://auth.x.ai/device',
                'interval': 1,
                'expires_in': 30,
            }
        return 200, {
            'access_token': 'owned-grant',
            'refresh_token': 'owned-refresh',
            'expires_in': 3600,
        }

    def getter(url, headers):
        return {'token_endpoint': 'https://auth.x.ai/oauth2/token'}

    clock = {'now': 1_000}
    opened = []

    def now():
        clock['now'] += 1
        return clock['now']

    sign_in_xai(
        tmp_path,
        tmp_path / 'status.json',
        poster=poster,
        getter=getter,
        sleep=lambda seconds: None,
        opener=lambda url: opened.append(url),
        now=now,
    )
    assert opened == ['https://auth.x.ai/device']
    status = json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))
    grant = json.loads((tmp_path / 'oauth' / 'xai.json').read_text(encoding='utf-8'))
    assert status['user_code'] == 'ABCD-1234'
    assert 'owned-grant' not in json.dumps(status)
    assert 'device-secret' not in json.dumps(status)
    assert grant['access_token'] == 'owned-grant'
    assert calls[0] == 'https://auth.x.ai/oauth2/device/code'
    login = open('tessera/oauth_login.py', encoding='utf-8').read()
    assert 'subprocess' not in login
