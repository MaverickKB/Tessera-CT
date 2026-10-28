"""Real WebKit (iPhone emulation) drives the approval mode control. Skipped unless Playwright+WebKit exist."""

from contextlib import contextmanager
import json
import threading

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from fake_backend import FakeBackend  # noqa: E402
from tessera import server as server_module  # noqa: E402
from tessera.config import Config  # noqa: E402


@contextmanager
def core(tmp_path):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.server.host, config.server.port = '127.0.0.1', 0
    config.tools.approval = 'deny_risky'
    config.jev.enabled = False
    config.backends = {}
    with server_module.create_server(config, backend_factory=lambda *a: FakeBackend(['ok'] * 4)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.url if isinstance(server.url, str) else server.url(), config
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize('device', ['iPhone 14', 'iPhone 14 landscape'])
def test_yolo_switch_on_phone_layout(tmp_path, device):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.webkit.launch()
        except Exception:
            pytest.skip('Playwright WebKit browser is not installed')
        with core(tmp_path) as (url, config):
            context = engine.new_context(**playwright.devices[device])
            page = context.new_page()
            dialogs = []
            page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
            page.goto(url)
            safe, yolo = page.locator('#approval-safe'), page.locator('#approval-yolo')
            page.wait_for_selector('#approval-mode:not([hidden])')
            assert page.get_attribute('#approval-mode', 'data-mode') == 'deny_risky'
            assert safe.get_attribute('aria-checked') == 'true' and yolo.get_attribute('aria-checked') == 'false'
            assert safe.inner_text() == 'Safe' and yolo.inner_text() == 'Unleashed'
            assert not page.locator('#yolo-banner').is_visible()
            safe.click()
            assert not dialogs
            with page.expect_response(lambda r: r.url.endswith('/api/approval') and r.request.method == 'POST') as info:
                yolo.click()
            assert json.loads(info.value.text())['mode'] == 'yolo' and not dialogs
            page.wait_for_selector('#yolo-banner:not([hidden])')
            assert yolo.get_attribute('aria-checked') == 'true' and safe.get_attribute('aria-checked') == 'false'
            assert page.get_attribute('#approval-mode', 'data-mode') == 'yolo'
            assert 'Unleashed mode is on' in page.inner_text('#yolo-banner')
            assert page.locator('#approval-yolo svg').count() == 0
            assert config.tools.approval == 'yolo'
            # Status polls never repaint the old mode; a reload shows the persisted switch.
            page.reload()
            page.wait_for_selector('#approval-yolo[aria-checked="true"]')
            assert page.locator('#yolo-banner').is_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
            with page.expect_response(lambda r: r.url.endswith('/api/approval') and r.request.method == 'POST'):
                page.locator('#approval-safe').click()
            page.wait_for_selector('#yolo-banner', state='hidden')
            assert page.locator('#approval-safe').get_attribute('aria-checked') == 'true'
            assert config.tools.approval == 'deny_risky' and not dialogs
            context.close()
        engine.close()
