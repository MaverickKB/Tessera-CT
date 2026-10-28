"""Windows taskkill results are the same kind of error POSIX killpg raises."""
import subprocess

import pytest


def _patch_taskkill(monkeypatch, result):
    import tessera.procutil as procutil

    seen = []

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        return result(command)

    monkeypatch.setattr(procutil.os, 'name', 'nt')
    monkeypatch.setattr(procutil.subprocess, 'run', fake_run)
    return procutil, seen


def test_taskkill_process_not_found_is_process_lookup_error(monkeypatch):
    procutil, seen = _patch_taskkill(monkeypatch, lambda command: subprocess.CompletedProcess(
        command, 128, stdout='', stderr='ERROR: The process "9" not found.'))
    with pytest.raises(ProcessLookupError, match='not found'):
        procutil.terminate_group(9)
    command, kwargs = seen[0]
    assert command[:1] == ['taskkill']
    assert '/F' not in command
    assert kwargs.get('check') is False
    assert 'DEVNULL' not in str(kwargs.get('stdout'))


def test_taskkill_no_running_instance_is_process_lookup_error(monkeypatch):
    procutil, _seen = _patch_taskkill(monkeypatch, lambda command: subprocess.CompletedProcess(
        command, 128, stdout='', stderr='ERROR: There is no running instance of the task.'))
    with pytest.raises(ProcessLookupError, match='no running instance'):
        procutil.kill_group(9)


def test_taskkill_failure_raises_with_its_output(monkeypatch):
    procutil, seen = _patch_taskkill(monkeypatch, lambda command: subprocess.CompletedProcess(
        command, 1, stdout='', stderr='ERROR: Access is denied.'))
    with pytest.raises(OSError, match='taskkill failed: ERROR: Access is denied') as caught:
        procutil.kill_group(9)
    assert not isinstance(caught.value, ProcessLookupError)
    command, _kwargs = seen[0]
    assert command[:3] == ['taskkill', '/F', '/T']


def test_taskkill_success_returns(monkeypatch):
    procutil, seen = _patch_taskkill(monkeypatch, lambda command: subprocess.CompletedProcess(
        command, 0, stdout='SUCCESS: The process with PID 9 has been terminated.', stderr=''))
    procutil.terminate_group(9)
    procutil.kill_group(9)
    assert len(seen) == 2
