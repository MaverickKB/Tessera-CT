"""Directory sync preserves POSIX failures and closes owned descriptors."""
from types import SimpleNamespace

import pytest

from tessera import durability


def test_windows_does_not_open_directory(monkeypatch):
    monkeypatch.setattr(durability, 'os', SimpleNamespace(name='nt'))
    durability.sync_directory('unused')


@pytest.mark.parametrize('fail', [False, True])
def test_posix_closes_descriptor_and_propagates_failure(monkeypatch, fail):
    calls = []
    def sync(fd):
        calls.append(('sync', fd))
        if fail:
            raise OSError('sync failed')
    fake = SimpleNamespace(name='posix', O_RDONLY=0,
        open=lambda path, flags: 42, fsync=sync,
        close=lambda fd: calls.append(('close', fd)))
    monkeypatch.setattr(durability, 'os', fake)
    if fail:
        with pytest.raises(OSError, match='sync failed'):
            durability.sync_directory('directory')
    else:
        durability.sync_directory('directory')
    assert calls == [('sync', 42), ('close', 42)]
