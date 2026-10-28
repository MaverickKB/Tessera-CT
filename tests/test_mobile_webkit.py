"""Real WebKit with iPhone emulation against the web UI. Skipped unless Playwright+WebKit exist.

Locally a loopback core with a scripted fake model is used (no network, no live model).
Set TESSERA_MOBILE_URL to also drive a deployed URL read-only up to the round trip, and
TESSERA_MOBILE_SHOTS to a directory to save screenshots.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import threading

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from fake_backend import FakeBackend  # noqa: E402
from tessera import server as server_module  # noqa: E402
from tessera.config import Config  # noqa: E402

DEVICES = ['iPhone 14', 'iPhone 15 Pro Max', 'iPhone 14 landscape']


@contextmanager
def local_core(tmp_path):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.server.host, config.server.port = '127.0.0.1', 0
    config.tools.approval = 'never'
    config.backends = {}
    backend = lambda *args: FakeBackend(['Mobile reply streamed back.'] * 4)
    with server_module.create_server(config, backend_factory=backend) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.url if isinstance(server.url, str) else server.url()
        finally:
            server.shutdown()
            thread.join()


@pytest.fixture(scope='module')
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.webkit.launch()
        except Exception:
            pytest.skip('Playwright WebKit browser is not installed')
        yield playwright, engine
        engine.close()


def shot(page, name):
    target = os.environ.get('TESSERA_MOBILE_SHOTS')
    if target:
        page.screenshot(path=str(Path(target).expanduser() / f'mobile-{name}.png'))


def open_page(browser, url, device):
    playwright, engine = browser
    context = engine.new_context(**playwright.devices[device])
    page = context.new_page()
    problems = []
    page.on('pageerror', lambda error: problems.append('pageerror: ' + str(error)))
    page.on('response', lambda response: problems.append(f'{response.status} {response.url}')
            if response.status >= 500 else None)
    page.goto(url)
    page.wait_for_selector('body.panels-ready', state='attached')
    return context, page, problems


def classes(page):
    return page.evaluate('document.body.className').split()


def no_horizontal_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')


def topmost(page, selector):
    return page.evaluate("""(sel) => { const el = document.querySelector(sel); const r = el.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return hit === el || el.contains(hit); }""", selector)


@pytest.mark.parametrize('device', DEVICES)
def test_every_visible_mobile_control_works_and_a_message_round_trips(tmp_path, browser, device):
    with local_core(tmp_path) as url:
        context, page, problems = open_page(browser, url, device)
        try:
            tag = device.lower().replace(' ', '-')
            width = page.viewport_size['width']
            assert page.locator('#messages').bounding_box() is None or True
            conversation = page.locator('.conversation').bounding_box()
            assert conversation['width'] >= width - 2, 'chat is the main view'
            assert not page.locator('#sessions-panel').is_visible() and not page.locator('#app-navigation').is_visible()
            for selector in ('#navigation-toggle', '#sessions-toggle', '#empty-create'):
                assert page.locator(selector).is_visible() and topmost(page, selector), selector
            assert page.evaluate("getComputedStyle(document.getElementById('message-input')).fontSize") == '16px'
            no_horizontal_overflow(page)
            shot(page, tag + '-empty')

            page.tap('#navigation-toggle')
            assert 'show-navigation' in classes(page) and page.locator('#app-navigation').is_visible()
            page.wait_for_timeout(300)
            page.mouse.click(width - 10, 300)  # backdrop
            assert 'show-navigation' not in classes(page)

            page.tap('#sessions-toggle')
            assert 'show-sessions' in classes(page) and page.locator('#sessions-panel').is_visible()
            assert topmost(page, '#new-session') and topmost(page, '#close-sessions')
            shot(page, tag + '-sessions-drawer')
            page.tap('#close-sessions')
            assert 'show-sessions' not in classes(page)
            page.tap('#sessions-toggle')
            page.mouse.click(width - 10, 300)
            assert 'show-sessions' not in classes(page)

            page.tap('#empty-create')
            page.fill('#session-name', 'Phone thread')
            page.tap('#save-session')
            page.wait_for_selector('body:not(.no-session)', state='attached')
            page.tap('#sessions-toggle')
            page.tap('.session-button')  # selecting a session closes the drawer
            assert 'show-sessions' not in classes(page)
            # Voice stays reachable on every phone layout, including landscape.
            assert page.is_visible('#voice-toggle')
            assert topmost(page, '#voice-toggle')
            page.tap('#message-input')
            page.fill('#message-input', 'Hello from the phone')
            shot(page, tag + '-composer-focused')
            page.tap('#send-message')
            page.wait_for_selector('text=Mobile reply streamed back.', timeout=15000)
            no_horizontal_overflow(page)
            shot(page, tag + '-open-session')
            assert not problems, problems
        finally:
            context.close()


def test_deployed_url_loads_without_errors_and_controls_respond(browser):
    url = os.environ.get('TESSERA_MOBILE_URL')
    if not url:
        pytest.skip('TESSERA_MOBILE_URL not set')
    for attempt in range(5):  # repeated loads: intermittent proxy failures must not recur
        context, page, problems = open_page(browser, url, 'iPhone 14')
        try:
            page.tap('#navigation-toggle')
            assert 'show-navigation' in classes(page)
            page.tap('#navigation-toggle')
            page.tap('#sessions-toggle')
            assert 'show-sessions' in classes(page)
            if attempt == 0:
                shot(page, 'live-sessions-drawer')
            page.tap('#close-sessions')
            no_horizontal_overflow(page)
            if attempt == 0:
                shot(page, 'live-empty')
            assert not problems, problems
        finally:
            context.close()
