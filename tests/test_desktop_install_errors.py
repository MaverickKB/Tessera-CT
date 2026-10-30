"""The public explicit installer reports recoverable failures without a traceback."""
from unittest.mock import Mock

import pytest

from tessera import desktop_install, terminal


@pytest.mark.parametrize('stage', ['install', 'launch'])
def test_public_installer_reports_native_failure(tmp_path, monkeypatch, capsys, stage):
    installer = Mock(return_value=tmp_path / 'native-app')
    launcher = Mock()
    target = installer if stage == 'install' else launcher
    target.side_effect = desktop_install.NativeShellNotFound('Synthetic native failure')
    monkeypatch.setattr(desktop_install, 'install', installer)
    monkeypatch.setattr(desktop_install, 'launch', launcher)
    result = terminal.main(['--home', str(tmp_path), 'install-desktop',
                            '--release-url', 'https://example.test/native', '--sha256', 'a' * 64])
    assert result == 1
    error = capsys.readouterr().err
    assert 'Synthetic native failure' in error
    assert 'Traceback' not in error
    if stage == 'install':
        launcher.assert_not_called()
