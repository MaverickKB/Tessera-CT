import json
from pathlib import Path

import pytest

from demo.live_smoke import CallBudget, BudgetBackend, redact
from fake_backend import FakeBackend


def test_call_budget_survives_restart_and_blocks_seventh_attempt(tmp_path):
    path = tmp_path / 'budget.json'
    budget = CallBudget(path)
    for i in range(6):
        assert budget.claim() == i + 1
    reopened = CallBudget(path)
    with pytest.raises(RuntimeError, match='budget'):
        reopened.claim()
    assert json.loads(path.read_text())['attempts'] == 6


def test_budget_adapter_constrains_tool_turns_and_prevents_hidden_calls(tmp_path):
    fake = FakeBackend(['ok'] * 6)
    backend = BudgetBackend(fake, CallBudget(tmp_path / 'budget.json'))
    for _ in range(6):
        backend.chat([{'role': 'user', 'content': 'test'}], tools=[{'type': 'function'}])
    assert fake.calls[0]['tool_choice']['function']['name'] == 'read_file'
    assert fake.calls[2]['tool_choice']['function']['name'] == 'zoom'
    for index in (1, 3, 4, 5):
        assert fake.calls[index]['tool_choice'] == 'none'
    with pytest.raises(RuntimeError):
        backend.chat([])
    assert len(fake.calls) == 6


def test_transcript_redacts_url_host_and_private_home(tmp_path):
    url = 'http://private-model.example:9000/v1'
    text = f'{url} private-model.example:9000 private-model.example {tmp_path}/config.toml'
    cleaned = redact(text, url, tmp_path)
    assert 'private-model.example' not in cleaned
    assert str(tmp_path) not in cleaned
    assert '<model-host>' in cleaned
    assert '<TESSERA_HOME>/config.toml' in cleaned


@pytest.mark.parametrize("finish_reason, content", [
    ("length", "<think>continuity-example"),
    ("length", "A plausible completed answer."),
    ("content_filter", "A plausible completed answer."),
])
def test_truncated_provider_answer_cannot_pass_http_smoke(tmp_path, finish_reason, content):
    from demo.live_smoke import running_server, turn
    from tessera.config import Config
    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    fake = FakeBackend([{'content': content, 'finish_reason': finish_reason}])
    with running_server(config, lambda *args: fake) as probe:
        _, session = probe.request('POST', '/api/sessions', {'title': 'partial'})
        with pytest.raises(AssertionError):
            turn(probe, session['slug'], 'Recall the key')


def test_thinking_text_is_not_final_answer_evidence():
    from demo.live_smoke import visible_answer
    assert visible_answer('<think>continuity-example</think>I do not know.') == 'I do not know.'
    with pytest.raises(AssertionError):
        visible_answer('<think>continuity-example')
    with pytest.raises(AssertionError):
        visible_answer('<think>earlier</think><think>continuity-example')
