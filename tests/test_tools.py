from pathlib import Path
import shlex
import sys

import pytest

from tessera.tools import ToolRegistry, build_registry, clip_output
from tessera.log import ChatLog
from tessera.tree import SummaryTree
from tessera.view import View


@pytest.mark.parametrize("limit", [1, 2, 10, 11, 12, 64])
def test_every_clipped_output_keeps_a_complete_visible_notice(limit):
    text = "HEAD " + "synthetic words " * 30 + " TAIL"
    output = clip_output(text, limit)
    assert "\n[clipped]\n" in output
    assert output.startswith("H")
    assert len(output) <= max(limit, 12)
    assert clip_output(output, limit) == output
    assert clip_output("H", 1) == "H"


def test_minimum_file_and_shell_limits_preserve_a_notice(tmp_path):
    (tmp_path / "synthetic.txt").write_text("SOURCE!")
    registry = build_registry(cwd=tmp_path, approval="never", clip_chars=1)
    partial = registry.execute("read_file", {"path": "synthetic.txt", "limit": 1})
    assert partial.startswith("S") and "[clipped]" in partial
    assert registry.execute("read_file", {"path": "synthetic.txt", "offset": 6, "limit": 1}) == "!"
    shell = registry.execute("shell", {"command": f'{shlex.quote(sys.executable)} -c \'print("SOURCE!")\''})
    assert shell["exit_code"] == 0
    assert shell["output"].startswith("S") and "[clipped]" in shell["output"]


@pytest.mark.parametrize("source, continuation_suffix", [
    ("local", ").]"), ("parent", ", source='parent').]"),
])
def test_tiny_zoom_pages_preserve_exact_utf8_offsets_and_visible_continuation(tmp_path, source, continuation_suffix):
    with ChatLog(tmp_path / "log") as log:
        tree = SummaryTree(tmp_path, log, node_bytes=32)
        view = View(tmp_path, tree, high_bytes=500, low_bytes=250)
        log.subscribe(view.on_message)
        log.append("user", "A🙂éZ")
        registry = build_registry(cwd=tmp_path, view=view if source == "local" else None,
                                  parent_view=view if source == "parent" else None)
        for offset, limit, expected, next_offset in [(0, 1, "A", 1), (1, 4, "🙂", 5), (5, 2, "é", 7)]:
            output = registry.execute("zoom", {"start": 0, "count": 1, "offset": offset, "limit": limit, "source": source})
            payload, separator, notice = output.partition("\n[Message ")
            assert payload == expected and len(payload.encode("utf-8")) <= limit
            assert separator and f"0: bytes {offset}..{next_offset} of 8." in notice
            assert f"Continue with zoom(0, 1, offset={next_offset}{continuation_suffix}" in notice
        assert registry.execute("zoom", {"start": 0, "count": 1, "offset": 7, "limit": 1, "source": source}) == "Z"
        # Raw View pages retain their exact-data API for byte-for-byte assembly.
        assert view.zoom(0, 1, limit=1) == "A"


def test_files_exact_edit_and_shell_output(tmp_path):
    registry = build_registry(cwd=tmp_path, approval='never', clip_chars=100)
    registry.execute('write_file', {'path': 'a.txt', 'text': 'hello world'})
    registry.execute('edit_file', {'path': 'a.txt', 'old': 'world', 'new': 'reader'})
    assert registry.execute('read_file', {'path': 'a.txt'}) == 'hello reader'
    with pytest.raises(ValueError, match='exactly once'):
        registry.execute('edit_file', {'path': 'a.txt', 'old': 'missing', 'new': 'x'})
    output = registry.execute('shell', {'command': f'{shlex.quote(sys.executable)} -c \'print("a" * 200 + "TAIL")\''})
    assert 'TAIL' in output['output']
    assert 'clipped' in output['output']
    assert len(output['output']) <= 100


def test_risky_tool_policy_and_external_registration(tmp_path):
    registry = build_registry(cwd=tmp_path, approval='deny_risky')
    with pytest.raises(PermissionError):
        registry.execute('shell', {'command': 'echo blocked > forbidden'})
    assert not (tmp_path / 'forbidden').exists()
    with pytest.raises(PermissionError):
        registry.execute('write_file', {'path': 'forbidden', 'text': 'x'})
    registry.register('memory_recall', 'Recall', {'type': 'object', 'properties': {}}, lambda: 'memory')
    assert registry.execute('memory_recall', {}) == 'memory'
    restricted = registry.restricted(['read_file'])
    with pytest.raises(ValueError, match='Unknown tool'):
        restricted.execute('shell', {'command': 'pwd'})


def test_ask_requires_explicit_approval(tmp_path):
    denied = build_registry(cwd=tmp_path, approval='ask')
    with pytest.raises(PermissionError):
        denied.execute('write_file', {'path': 'x', 'text': 'x'})
    accepted = build_registry(cwd=tmp_path, approval='ask', approve=lambda name, args: True)
    accepted.execute('write_file', {'path': 'x', 'text': 'ok'})
    assert (tmp_path / 'x').read_text() == 'ok'


def test_schema_rejects_wrong_argument_types_before_side_effects(tmp_path):
    registry = build_registry(cwd=tmp_path, approval='never')
    with pytest.raises(ValueError, match='argument'):
        registry.execute('write_file', {'path': 'x', 'text': []})
    assert not (tmp_path / 'x').exists()


def test_shell_reports_failure_and_timeout_structurally(tmp_path):
    registry = build_registry(cwd=tmp_path, approval='never')
    failed = registry.execute('shell', {'command': 'exit 7'})
    assert failed['exit_code'] == 7
    assert failed['timed_out'] is False
    timed = registry.execute('shell', {'command': f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"', 'timeout': .05})
    assert timed['timed_out'] is True
    assert timed['exit_code'] != 0


def test_interrupt_kills_shell_process_group(tmp_path, monkeypatch):
    import subprocess
    import time
    original_wait = subprocess.Popen.wait
    interrupted = []
    def interrupt_once(process, *args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            time.sleep(.05)
            raise KeyboardInterrupt
        return original_wait(process, *args, **kwargs)
    monkeypatch.setattr(subprocess.Popen, 'wait', interrupt_once)
    registry = build_registry(cwd=tmp_path, approval='never')
    with pytest.raises(KeyboardInterrupt):
        registry.execute('shell', {'command': 'sleep .2; printf survived > orphan-marker'})
    time.sleep(.3)
    assert not (tmp_path / 'orphan-marker').exists()
