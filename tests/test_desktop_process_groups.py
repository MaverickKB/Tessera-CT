"""Darwin's zombie-only EPERM is distinct from a genuine cleanup refusal."""
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tessera import desktop_install as desktop


@pytest.mark.parametrize('output', ['', '123 Z\n', '123 Z+\n456 S\n'])
def test_zombie_or_absent_owned_group_is_already_clean(monkeypatch, output):
    monkeypatch.setattr(desktop.os, 'killpg', Mock(side_effect=PermissionError(1, 'Operation not permitted')), raising=False)
    query = Mock(return_value=SimpleNamespace(returncode=0, stdout=output))
    monkeypatch.setattr(desktop.subprocess, 'run', query)
    desktop._signal_native_group(123, signal.SIGTERM, 7)
    assert query.call_args.args[0] == ['/bin/ps', '-axo', 'pgid=,stat=']
    assert query.call_args.kwargs['timeout'] == 7


@pytest.mark.parametrize('code,output', [(0, '123 S\n'), (0, 'invalid output\n'), (1, '')])
def test_live_group_or_unverified_inspection_preserves_permission_failure(monkeypatch, code, output):
    monkeypatch.setattr(desktop.os, 'killpg', Mock(side_effect=PermissionError(1, 'Operation not permitted')), raising=False)
    monkeypatch.setattr(desktop.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=code, stdout=output)))
    with pytest.raises(PermissionError):
        desktop._signal_native_group(123, signal.SIGTERM, 7)
