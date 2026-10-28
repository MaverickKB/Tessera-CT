"""Compaction uses configured CLI workers with API tool execution disabled."""

import json
import sys

import pytest

from cli_fixtures import synthetic_codex_auth
from tessera.backend import Backend, BackendError
from tessera.compactor import Compactor
from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import View


@pytest.fixture
def cli_backend(tmp_path):
    command = tmp_path / 'codex'
    command.write_text(f'#!{sys.executable}\n' + '''import json,sys
sys.stdin.read()
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"[user] model summary fixture"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5}}))
''')
    command.chmod(0o700)
    return Backend('worker-placeholder', '', '', extra={'provider_kind': 'cli_worker', 'cli': {
        'vendor': 'codex', 'command': str(command), 'cwd': str(tmp_path), 'timeout': 5,
        'auth_path': synthetic_codex_auth(tmp_path)}})


def test_cli_compactor_generates_a_model_node_instead_of_silent_fallback(tmp_path, cli_backend):
    # Rejecting even explicitly disabled API tools would persist an extractive
    # prefix, never this model-produced node and its existing judge audit.
    with ChatLog(tmp_path / 'log') as log:
        tree = SummaryTree(tmp_path, log, node_bytes=64)
        view = View(tmp_path, tree)
        log.subscribe(view.on_message)
        log.append('user', 'Distinct original packet ' * 20)
        schema = [{'type': 'function', 'function': {'name': 'zoom'}}]
        worker = Compactor(tree, view, cli_backend, 'Synthetic system', schema,
                           workers=1, retries=0, judge=lambda *_: 1.0)
        worker.start()
        try:
            assert worker.wait_until_summarized(0, timeout=10)
            assert tree.node(0, 0) == '[user] model summary fixture'
            assert tree.node_meta(0, 0)['faithfulness_passed'] is True
        finally:
            worker.stop()


@pytest.mark.parametrize('choice', [None, 'auto', 'required'])
def test_cli_backend_still_rejects_api_tools_when_not_explicitly_disabled(cli_backend, choice):
    schema = [{'type': 'function', 'function': {'name': 'zoom'}}]
    with pytest.raises(BackendError) as failure:
        cli_backend.chat([{'role': 'user', 'content': 'Synthetic request'}], tools=schema,
                         tool_choice=choice)
    assert failure.value.category == 'configuration'
