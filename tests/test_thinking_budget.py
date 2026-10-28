import pytest

from fake_backend import FakeBackend, fake_http_server
from tessera.backend import Backend
from tessera.sessions import SessionManager
from test_loop import session_config
from test_server import call, running_server


def test_budget_is_optional_persistent_and_per_response(tmp_path):
    backend = FakeBackend([
        {'tool_calls': [{'id': 'read', 'name': 'read_file', 'arguments': {'path': 'input.txt'}}]},
        'Completed the tool and answered.',
        'Thinking is off.',
        'Using the engine default.',
    ])
    (tmp_path / 'input.txt').write_text('test input')
    config = session_config(tmp_path)
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        slug = manager.create('Budget')['slug']
        assert manager.describe(slug)['thinking_budget'] == 0
        manager.update(slug, thinking_budget=1024)
        manager.submit(slug, 'Read the input file and answer')
        assert manager.wait(slug, 5)
        assert len(backend.calls) == 2
        for request in backend.calls:
            assert request['thinking_budget'] == 1024
            assert request['reasoning_effort'] == 'low'
        manager.update(slug, reasoning='off')
        manager.submit(slug, 'Answer without thinking')
        assert manager.wait(slug, 5)
        assert 'thinking_budget' not in backend.calls[-1]
        assert backend.calls[-1]['chat_template_kwargs'] == {'enable_thinking': False}
        assert manager.describe(slug)['thinking_budget'] == 1024
    with SessionManager(config, backend_factory=lambda *args: backend) as manager:
        assert manager.describe(slug)['thinking_budget'] == 1024
        assert manager.get(slug).thinking_budget == 1024
        manager.update(slug, reasoning='high', thinking_budget=0)
        manager.submit(slug, 'Continue using the engine default')
        assert manager.wait(slug, 5)
        assert 'thinking_budget' not in backend.calls[-1]
        assert backend.calls[-1]['reasoning_effort'] == 'high'


@pytest.mark.parametrize('limit_name', ['max_tokens', 'max_completion_tokens'])
def test_thinking_budget_preserves_the_configured_answer_allowance(limit_name):
    reply = {'choices': [{'message': {'content': 'Complete answer'}, 'finish_reason': 'stop'}]}
    with fake_http_server([(200, reply), (200, reply)]) as (url, received):
        backend = Backend('fixture', url, 'fixture', extra={'parameters': {limit_name: 32768}})
        backend.chat([{'role': 'user', 'content': 'Think and answer'}], thinking_budget=4096)
        backend.chat([{'role': 'user', 'content': 'Use the default'}])
        assert received[0]['body']['thinking_budget'] == 4096
        assert received[0]['body'][limit_name] == 36864
        assert received[1]['body'][limit_name] == 32768
        assert 'thinking_budget' not in received[1]['body']
        assert backend.extra['parameters'][limit_name] == 32768


def test_budget_setting_uses_the_session_http_contract(tmp_path):
    with running_server(tmp_path) as server:
        _, row = call(server, 'POST', '/api/sessions', {'title': 'Budget'})
        path = '/api/sessions/' + row['slug']
        status, row = call(server, 'PATCH', path, {'thinking_budget': 2048})
        assert status == 200
        assert row['thinking_budget'] == 2048
        assert call(server, 'GET', path)[1]['thinking_budget'] == 2048
        assert call(server, 'PATCH', path, {'thinking_budget': 0})[1]['thinking_budget'] == 0
        for invalid in (-1, True, 1.5, '2048', 4294967296):
            assert call(server, 'PATCH', path, {'thinking_budget': invalid})[0] == 400
