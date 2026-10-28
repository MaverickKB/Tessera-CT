"""Installed and editable layouts both support init and the local web UI."""

import http.client
from pathlib import Path
import threading

import pytest

from tessera import assets
from tessera.cli import main
from tessera.config import Config
from tessera.server import create_server


@pytest.mark.parametrize('layout', ['wheel', 'editable'])
def test_init_and_static_server_without_the_checkout(layout, tmp_path, monkeypatch):
    template = Path('config.example.toml').read_text()
    package = tmp_path / 'site' / 'tessera'
    package.mkdir(parents=True)
    monkeypatch.setattr(assets, '__file__', str(package / 'assets.py'))
    root = package / '_assets' if layout == 'wheel' else package.parent
    web = root / 'web'
    web.mkdir(parents=True)
    (root / 'config.example.toml').write_text(template)
    (web / 'index.html').write_text('<title>Packaged UI</title>')
    (web / 'app.js').write_text('/* packaged script */')
    monkeypatch.chdir(tmp_path)

    home = tmp_path / 'home'
    assert main(['--home', str(home), 'init']) == 0
    assert (home / 'config.toml').read_text() == template

    config = Config(home=home, cwd=tmp_path)
    config.server.port = 0
    with create_server(config) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                for url, expected in (('/', b'<title>Packaged UI</title>'),
                                      ('/static/app.js', b'/* packaged script */')):
                    connection.request('GET', url)
                    response = connection.getresponse()
                    assert response.status == 200
                    assert response.read() == expected
            finally:
                connection.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_installed_package_does_not_borrow_a_sibling_engine(tmp_path, monkeypatch):
    """A wheel with no engine must not resolve one that happens to sit beside it."""
    package = tmp_path / 'site' / 'tessera'
    bundled = package / '_assets'
    bundled.mkdir(parents=True)
    (bundled / 'build-stamp.json').write_text('{"commit": "fixture"}\n', encoding='utf-8')
    decoy = tmp_path / 'site' / 'aethermind-engine'
    (decoy / 'mcp').mkdir(parents=True)
    (decoy / 'mcp' / 'aethermind_mcp.py').write_text('# decoy\n', encoding='utf-8')
    (decoy / 'build-receipt.json').write_text('{"files": 1}\n', encoding='utf-8')
    monkeypatch.setattr(assets, '__file__', str(package / 'assets.py'))

    found = assets.asset_path('aethermind-engine')

    assert found == bundled / 'aethermind-engine'
    assert not found.exists()


def test_source_checkout_still_resolves_repo_root_assets(tmp_path, monkeypatch):
    package = tmp_path / 'tessera'
    package.mkdir()
    (tmp_path / 'config.example.toml').write_text('placeholder\n', encoding='utf-8')
    monkeypatch.setattr(assets, '__file__', str(package / 'assets.py'))

    assert assets.asset_path('config.example.toml') == tmp_path / 'config.example.toml'


def test_missing_template_does_not_leave_an_empty_config(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, '__file__', str(tmp_path / 'site' / 'tessera' / 'assets.py'))
    home = tmp_path / 'home'
    assert main(['--home', str(home), 'init']) == 1
    assert not (home / 'config.toml').exists()
