"""The web UI recovers from a core restart: banner clears, sessions resync, a deleted session's view drops.

Real Chromium against a real loopback core. Skipped unless Playwright+Chromium exist.
"""

from contextlib import contextmanager
import json
import shutil
import threading
import urllib.request

import pytest

sync_api = pytest.importorskip('playwright.sync_api')

from fake_backend import FakeBackend  # noqa: E402
from tessera import server as server_module  # noqa: E402
from tessera.config import Config  # noqa: E402


@contextmanager
def core(tmp_path, port=0, web_root=None):
    config = Config(home=tmp_path / 'home', cwd=tmp_path)
    config.server.host, config.server.port = '127.0.0.1', port
    config.tools.approval = 'never'
    config.backends = {}
    with server_module.create_server(config, web_root=web_root, backend_factory=lambda *a: FakeBackend(['ok'] * 4)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.url if isinstance(server.url, str) else server.url()
        finally:
            server.shutdown()
            thread.join()


def create_session(url, title):
    request = urllib.request.Request(url.rstrip('/') + '/api/sessions', method='POST', data=json.dumps({'title': title}).encode(),
                                     headers={'Content-Type': 'application/json', 'Origin': url.rstrip('/')})
    return json.load(urllib.request.urlopen(request))['slug']


def banner(page):
    return page.evaluate("document.getElementById('error-banner').hidden ? '' : document.getElementById('error-text').textContent")


def test_restart_clears_banner_resyncs_and_drops_a_deleted_session(tmp_path):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        try:
            with core(tmp_path) as url:
                port = int(url.rstrip('/').rsplit(':', 1)[1])
                slug = create_session(url, 'Doomed')
                page = engine.new_page()
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function("() => document.getElementById('connection-state').textContent === 'Connected'")
                assert not banner(page)
            page.wait_for_function("() => !document.getElementById('error-banner').hidden", timeout=15000)
            assert banner(page).startswith('Server unreachable')

            shutil.rmtree(tmp_path / 'home' / 'sessions' / slug)
            with core(tmp_path, port) as url:
                keep = create_session(url, 'Survivor')
                page.wait_for_function("() => document.getElementById('error-banner').hidden", timeout=15000)
                page.wait_for_function(f"() => location.hash.includes('session={keep}')", timeout=15000)
                titles = page.locator('#session-list').inner_text()
                assert 'Survivor' in titles and 'Doomed' not in titles
                assert page.evaluate("document.getElementById('settings-session-count').textContent") == '1 session'
                assert page.locator('#messages > *').count() == 0, 'the deleted session must not linger'
                assert typeable(page), 'after a resync the composer is typeable'
        finally:
            engine.close()


def typeable(page):
    return page.evaluate("""() => { const el = document.getElementById('message-input');
        return !el.disabled && !el.readOnly && el.offsetParent !== null; }""")


def test_composer_is_typeable_with_no_session_and_sending_starts_one(tmp_path):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        try:
            with core(tmp_path) as url:
                page = engine.new_page()
                page.goto(url)
                page.wait_for_function("() => document.getElementById('connection-state').textContent === 'Connected'")
                assert typeable(page), 'no session open: the composer still takes text'
                page.fill('#message-input', 'Plan the garden beds')
                page.press('#message-input', 'Enter')
                page.wait_for_function("() => location.hash.includes('session=')", timeout=15000)
                assert 'Plan the garden beds' in page.locator('#session-list').inner_text()
                page.wait_for_function("() => document.getElementById('message-input').value === ''", timeout=15000)
                assert typeable(page), 'still typeable while the first turn runs'
        finally:
            engine.close()


def test_core_restart_with_new_ui_assets_offers_the_update_and_keeps_the_draft(tmp_path):
    """A new build must never reload the page out from under a person.

    A rebuild marks the Settings nav and shows Update interface. The reload
    happens only when a person clicks it, and the draft survives that click.
    """
    web = tmp_path / 'web'
    shutil.copytree(server_module.asset_path('web'), web)
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        try:
            with core(tmp_path, web_root=web) as url:
                port = int(url.rstrip('/').rsplit(':', 1)[1])
                slug = create_session(url, 'Drafting')
                page = engine.new_page()
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function("() => document.getElementById('connection-state').textContent === 'Connected'")
                first = json.load(urllib.request.urlopen(url.rstrip('/') + '/api/status'))['ui_build']
                page.evaluate("() => { window.__stale = true; }")
                page.fill('#message-input', 'half-written thought')
            # Touch a file the document loads. ui_build_id hashes index.html plus
            # the scripts it references. A stylesheet applies on the next navigation
            # without reloading, so this test appends to app.js.
            with (web / 'app.js').open('a') as handle:
                handle.write('\n// new build\n')
            with core(tmp_path, port, web_root=web) as url:
                assert json.load(urllib.request.urlopen(url.rstrip('/') + '/api/status'))['ui_build'] != first
                # The status poll has to see the new build, and say so, on the old page.
                page.wait_for_function(
                    "() => document.getElementById('nav-settings').classList.contains('update-ready')",
                    timeout=20000)
                assert page.evaluate("() => window.__stale") is True, 'never reload unasked'
                assert page.evaluate("() => document.getElementById('message-input').value") == 'half-written thought'
                # The offer is reachable where a person goes to act on it.
                page.goto(url.rstrip('/') + '/#view=settings')
                page.wait_for_function(
                    "() => { const b = document.getElementById('interface-update');"
                    " return b && !b.hidden && b.offsetParent !== null }",
                    timeout=15000)
                page.click('#interface-update')
                page.wait_for_function("() => window.__stale === undefined", timeout=20000)
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function(
                    "() => document.getElementById('message-input').value === 'half-written thought'",
                    timeout=15000)
                page.wait_for_timeout(1500)
                assert page.evaluate("() => window.__stale") is None, 'one reload, no loop'
        finally:
            engine.close()


def test_open_session_survives_errors_and_brief_omissions_from_the_list(tmp_path):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        try:
            with core(tmp_path) as url:
                slug = create_session(url, 'Sacred')
                page = engine.new_page()
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function("() => document.getElementById('connection-state').textContent === 'Connected'")
                mode = {'list': 'ok'}
                def sessions(route):
                    if mode['list'] == 'error':
                        route.fulfill(status=500, body='{"error": "transient"}', content_type='application/json')
                    elif mode['list'] == 'empty':
                        route.fulfill(status=200, body='[]', content_type='application/json')
                    else:
                        route.continue_()
                page.route('**/api/sessions', sessions)
                listed = "() => document.getElementById('session-list').innerText.includes('Sacred')"
                mode['list'] = 'error'
                for _ in range(4):
                    page.evaluate("() => document.getElementById('settings-refresh')?.click()")
                    page.wait_for_timeout(300)
                    assert page.evaluate(listed) and location_has(page, slug), 'an error never drops the session'
                mode['list'] = 'empty'
                page.evaluate("() => document.getElementById('settings-refresh')?.click()")
                page.wait_for_timeout(500)
                assert page.evaluate(listed) and location_has(page, slug), 'one omission is not absence'
                mode['list'] = 'ok'
                page.wait_for_timeout(2500)
                assert page.evaluate(listed) and location_has(page, slug)
                assert typeable(page)
                mode['list'] = 'empty'
                page.evaluate("() => document.getElementById('settings-refresh')?.click()")
                page.wait_for_timeout(8000)
                assert not location_has(page, slug) and not page.evaluate(listed), 'confirmed absence drops the view'
        finally:
            engine.close()


def location_has(page, slug):
    return page.evaluate("(slug) => location.hash.includes('session=' + slug)", slug)


def test_chat_screen_model_picker_switches_the_session(tmp_path):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        try:
            from tessera.config import BackendConfig
            config_backends = {'local': BackendConfig(name='local', base_url='http://127.0.0.1:9/v1', model='auto'),
                               'grok-chat': BackendConfig(name='grok-chat', base_url='https://api.example.com/v1', model='grok-test')}
            config = Config(home=tmp_path / 'pick', cwd=tmp_path)
            config.server.host, config.server.port = '127.0.0.1', 0
            config.tools.approval = 'never'
            config.backends = config_backends
            config.roles = {'main': 'local', 'failover': 'grok-chat'}
            with server_module.create_server(config, backend_factory=lambda *a: FakeBackend(['ok'] * 4)) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = server.url if isinstance(server.url, str) else server.url()
                slug = create_session(url, 'Picker')
                page = engine.new_page()
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function("() => !document.getElementById('session-model').disabled")
                options = page.evaluate("() => [...document.getElementById('session-model').options].map(o => [o.value, o.textContent])")
                # The picker lists the configured backends, each tagged with its
                # credential. renderModelPicker builds one option per chat name and
                # modelLabel appends " · <tag>" when a credential label is known.
                # Assert the names rather than the exact tag text.
                assert [value for value, _ in options] == ['local', 'grok-chat']
                assert all(text.startswith(name) for (name, text) in zip(['local', 'grok-chat'], [t for _, t in options]))
                page.select_option('#session-model', 'grok-chat')
                page.wait_for_function("() => document.getElementById('send-state').textContent.includes('next turn')")
                listed = json.load(urllib.request.urlopen(url.rstrip('/') + '/api/sessions'))
                assert listed[0]['backend'] == 'grok-chat'
                server.shutdown()
                thread.join()
        finally:
            engine.close()


class Thinker:
    """A backend that streams thought, pauses mid-turn, then answers."""
    on_delta = None

    def __init__(self):
        self.release = threading.Event()

    def chat(self, messages, tools=None, **kw):
        if self.on_delta:
            self.on_delta('thought', 'Considering the garden layout.')
            self.on_delta('text', 'Draft answer')
        self.release.wait(10)
        return {'content': 'Final answer.', 'reasoning': 'Considering the garden layout.', 'tool_calls': []}


def test_thinking_streams_live_collapses_on_reply_and_can_be_hidden(tmp_path):
    with sync_api.sync_playwright() as playwright:
        try:
            engine = playwright.chromium.launch()
        except Exception:
            pytest.skip('Playwright Chromium is not installed')
        thinker = Thinker()
        config = Config(home=tmp_path / 'home', cwd=tmp_path)
        config.server.host, config.server.port = '127.0.0.1', 0
        config.tools.approval = 'never'
        config.backends = {}
        try:
            with server_module.create_server(config, backend_factory=lambda *a: thinker) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = server.url if isinstance(server.url, str) else server.url()
                slug = create_session(url, 'Thinking')
                page = engine.new_page()
                page.goto(url.rstrip('/') + '/#session=' + slug)
                page.wait_for_function("() => document.getElementById('connection-state').textContent === 'Connected'")
                page.fill('#message-input', 'Plan it')
                page.press('#message-input', 'Enter')
                page.wait_for_function("() => document.querySelector('.live-turn .thought-text')?.textContent.includes('garden')", timeout=10000)
                assert page.evaluate("() => document.querySelector('.live-turn details.thought').open")
                assert 'Draft answer' in page.inner_text('.live-turn .live-text')
                thinker.release.set()
                page.wait_for_function("() => !document.querySelector('.live-turn') && document.body.innerText.includes('Final answer.')", timeout=10000)
                assert page.evaluate("() => { const d = document.querySelector('article.agent details.thought'); return d && !d.open && d.innerText.includes('Thought'); }")
                page.wait_for_function("() => document.getElementById('show-thoughts')")
                page.evaluate("() => { const box = document.getElementById('show-thoughts'); box.checked = false; box.dispatchEvent(new Event('change')); }")
                assert page.evaluate("() => getComputedStyle(document.querySelector('article.agent details.thought')).display") == 'none'
                events = (tmp_path / 'home' / 'sessions' / slug / 'events.jsonl').read_text()
                assert '"delta"' not in events, 'live text is never persisted to the journal'
                server.shutdown()
                thread.join()
        finally:
            thinker.release.set()
            engine.close()
