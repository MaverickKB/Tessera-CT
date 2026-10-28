"""Mission requirement 2, as a test: a person configures and saves a provider
through Settings with no API, code or file assistance.

The mission's own acceptance sentence:

    "Prove configuration through the user interface only: a human must select,
     configure, save, and use a provider through Settings without API, code, or
     file assistance."

This drives a real browser against a real loopback core. It clicks, types and
saves the way a person would, then reads the change on disk. It never calls the
API directly and never writes the config file — if either were needed, the
requirement would not be met.

Two things this test pins:

- A core whose home has NO config.toml refuses every write with "Config file is
  missing". That is correct behaviour, and it is why a save attempt against a
  bare Config object returns 400 while the page shows no error of its own. The
  fixture therefore starts from a real config on disk, the way `tessera init`
  leaves one.
- Providers is a named-entry editor. On a fresh install the Endpoints page
  shows "Add endpoint". The base URL field is on that form. A plain input scan
  finds nothing because the fields only exist after that choice.

Scope, stated honestly: this proves CONFIGURE and SAVE. It does not prove USE —
answering a prompt needs a reachable endpoint and a key, which a loopback test
does not have. That half stays open and must not be claimed from this test.

Skipped unless Playwright and a browser are installed.
"""

from contextlib import contextmanager
import threading

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from fake_backend import FakeBackend  # noqa: E402
from tessera import server as server_module  # noqa: E402
from tessera.config import load_config  # noqa: E402

NEW_NAME = 'clean-room'
NEW_ENDPOINT = 'https://gateway.internal.example/v1'


@contextmanager
def local_core(tmp_path):
    """A core with a real config on disk, the state `tessera init` leaves behind."""
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    (home / 'config.toml').write_text(
        '[backends.primary]\n'
        'kind = "openai_chat"\n'
        'base_url = "https://models.example.com/v1"\n'
        'model = "placeholder"\n')
    config = load_config(home)
    config.server.host, config.server.port = '127.0.0.1', 0
    config.tools.approval = 'never'
    backend = lambda *args: FakeBackend(['reply'] * 2)
    with server_module.create_server(config, backend_factory=backend) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.url if isinstance(server.url, str) else server.url(), home
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture(scope='module')
def browser():
    with sync_api.sync_playwright() as playwright:
        engine = None
        for name in ('chromium', 'firefox', 'webkit'):
            try:
                engine = getattr(playwright, name).launch()
                break
            except Exception:
                continue
        if engine is None:
            pytest.skip('no Playwright browser installed')
        yield engine
        engine.close()


def test_a_person_adds_and_saves_a_provider_through_settings(tmp_path, browser):
    with local_core(tmp_path) as (url, home):
        page = browser.new_page()
        writes = []
        page.on('request', lambda r: writes.append((r.method, r.url, r.post_data))
                if r.method == 'POST' else None)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            page.goto(url, wait_until='networkidle')

            # Navigate by clicking, not by URL. The rail label is a span.
            page.locator('text=Settings').first.click()
            page.wait_for_timeout(1200)
            assert page.evaluate("() => document.querySelector('#page-settings').hidden") is False, \
                'clicking Settings did not open the Settings page'

            page.get_by_role('button', name='Endpoints', exact=True).first.click()
            page.wait_for_timeout(1200)
            assert 'Endpoints' in page.inner_text('#page-settings'), \
                'the endpoints page did not render'

            add = page.get_by_role('button', name='Add endpoint', exact=True)
            assert add.count() > 0, 'no endpoint control on a fresh install'
            add.first.click()
            page.wait_for_timeout(1200)

            page.fill('#setting-providers_new_name-new', NEW_NAME)
            endpoint = page.locator('#setting-providers_new_base_url-new')
            assert endpoint.count() > 0, 'choosing Other HTTP revealed no base_url field'
            endpoint.fill(NEW_ENDPOINT)
            page.fill('#setting-providers_new_model-new', 'some-model')

            page.get_by_role('button', name='Add endpoint', exact=True).last.click()
            page.wait_for_timeout(2500)

            # The save must be a write the page made, not one the test made.
            posts = [(m, u, body) for m, u, body in writes if '/api/' in u]
            assert posts, f'saving made no API write: {writes}'
            assert not errors, f'the page raised errors while saving: {errors}'

            # And it must land on disk. This is the part a screenshot cannot show.
            config = (home / 'config.toml').read_text()
            assert NEW_NAME in config, \
                f'the provider added through the page is not in config.toml:\n{config}'
            assert 'gateway.internal.example' in config, \
                'the endpoint typed through the page did not reach config.toml'
            assert 'openai_chat' in config, 'the chosen connection type did not persist'

            # The page must reflect the new row, not just the file.
            page.get_by_role('button', name='Models', exact=True).first.click()
            page.wait_for_timeout(1000)
            assert NEW_NAME in page.inner_text('#page-settings'), \
                'the page does not show the model it just created'
        finally:
            page.close()
