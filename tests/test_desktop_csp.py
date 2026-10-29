"""The desktop CSP allows only the resources the shipped UI loads.

The window opens the core origin, so the core is ``'self'`` and its host and
port stay out of the policy. ``ipc:`` and ``http://ipc.localhost`` are Tauri's
IPC placeholders for the launch-agent server controls. No remote origin is
listed. The core appends a configured voice WebSocket origin only while voice
is enabled.

Inventory of web/ (production files, not tests):

- scripts: /static/*.js from index.html, plus the audio worklet named by
  data-voice-worklet. No inline scripts, handlers, eval, or remote scripts.
- styles: /static/theme.css, app.css, design.css. No style elements or
  attributes. CSSOM writes (menu position, canvas layout, theme variables)
  are not style attributes, so style-src stays 'self' without 'unsafe-inline'.
- images: same-origin icons, manifest icons, and CSS url() artwork;
  blob: attachment previews; data: desk screenshots.
- fonts: system font families only. No @font-face and no font URLs.
- connections: same-origin fetch and EventSource to the core ('self');
  Tauri IPC placeholders; the voice socket is a runtime setting, not a
  static origin.
"""

import json
import re
import threading
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
TAURI = ROOT / "desktop" / "tauri.conf.json"

# Locked to the shipped UI. A new source has to be added here and justified.
POLICY = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],
    "script-src-attr": ["'none'"],
    "style-src": ["'self'"],
    "img-src": ["'self'", "blob:", "data:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'", "ipc:", "http://ipc.localhost"],
    "media-src": ["'none'"],
    "object-src": ["'none'"],
    "frame-src": ["'none'"],
    "worker-src": ["'self'"],
    "manifest-src": ["'self'"],
    "form-action": ["'self'"],
    "base-uri": ["'none'"],
    "frame-ancestors": ["'none'"],
}

_URL = re.compile(r"""url\(\s*(?:(?P<q>["'])(?P<quoted>.*?)(?P=q)|(?P<bare>[^)\s]+))\s*\)""")
_REMOTE = re.compile(r"https?://|wss?://", re.I)


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def _policy_text():
    conf = json.loads(TAURI.read_text())
    text = conf["app"]["security"]["csp"]
    assert isinstance(text, str) and text.strip()
    return text


def _parsed(text):
    found = {}
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        name, *sources = part.split()
        assert name not in found, name
        found[name] = sources
    return found


def _production_sources():
    files = [WEB / "index.html", WEB / "app.webmanifest"]
    files.extend(WEB.glob("*.js"))
    files.extend(WEB.glob("*.css"))
    return files


def _web_path(reference):
    # Asset version queries are cache-busting, not part of the file path.
    return WEB / reference.removeprefix("/static/").split("?", 1)[0]


def _same_origin(reference):
    assert reference and not reference.startswith("//"), reference
    assert not _REMOTE.search(reference), reference
    assert "://" not in reference, reference
    assert not reference.startswith("javascript:"), reference


def test_desktop_csp_matches_the_shipped_ui_inventory():
    text = _policy_text()
    assert _parsed(text) == POLICY
    assert "unsafe-inline" not in text and "unsafe-eval" not in text
    assert "upgrade-insecure-requests" not in text
    assert "127.0.0.1" not in text and "[::1]" not in text
    # The only http origin is Tauri's IPC placeholder, not the core.
    assert re.findall(r"https?://[^\s;]+", text) == ["http://ipc.localhost"]

    page = Page((WEB / "index.html").read_text())
    scripts, styles, images, manifests = [], [], [], []
    for tag, attrs in page.elements:
        for name in attrs:
            assert not name.startswith("on"), name
            assert name != "style"
        if tag == "script":
            assert "src" in attrs and not attrs["src"].startswith("data:")
            scripts.append(attrs["src"])
        elif tag == "style":
            raise AssertionError("inline style element")
        elif tag == "link":
            rel = set(attrs.get("rel", "").split())
            href = attrs.get("href", "")
            if "stylesheet" in rel:
                styles.append(href)
            elif rel & {"icon", "apple-touch-icon", "shortcut"}:
                images.append(href)
            elif "manifest" in rel:
                manifests.append(href)
            else:
                raise AssertionError(f"unclassified link {attrs}")

    worklet = next(attrs["data-voice-worklet"] for _, attrs in page.elements if "data-voice-worklet" in attrs)
    scripts.append(worklet)
    assert scripts and styles and images and manifests

    for reference in scripts + styles + images + manifests:
        _same_origin(reference)
        assert reference.startswith("/static/")
        assert _web_path(reference).is_file()
    assert "'self'" in POLICY["script-src"]
    assert "'self'" in POLICY["style-src"]
    assert "'self'" in POLICY["img-src"] and "blob:" in POLICY["img-src"] and "data:" in POLICY["img-src"]
    assert "'self'" in POLICY["manifest-src"]
    assert POLICY["script-src-attr"] == ["'none'"]
    assert "'unsafe-inline'" not in POLICY["style-src"]

    css_images = []
    for sheet in styles:
        path = _web_path(sheet)
        body = path.read_text()
        assert "@font-face" not in body and "@import" not in body
        for match in _URL.finditer(body):
            target = match.group("quoted") if match.group("quoted") is not None else match.group("bare")
            _same_origin(target)
            css_images.append(path.parent / target)
    assert css_images
    for image in css_images:
        assert image.is_file(), image
    manifest = json.loads((WEB / "app.webmanifest").read_text())
    for icon in manifest["icons"]:
        _same_origin(icon["src"])
        assert (WEB / icon["src"]).is_file()

    script_text = "\n".join(path.read_text() for path in WEB.glob("*.js"))
    # The SVG namespace is not a fetch. Nothing else may name a remote URL.
    assert not _REMOTE.search(script_text.replace("http://www.w3.org/2000/svg", ""))
    assert "eval(" not in script_text and "new Function" not in script_text
    assert "createObjectURL(" in script_text, "attachment previews need img-src blob:"
    assert "data_url" in script_text, "desk screenshots need img-src data:"
    assert "audioWorklet.addModule" in script_text
    assert "EventSource(" in script_text and "fetch(" in script_text
    assert "'self'" in POLICY["connect-src"]
    assert "ipc:" in POLICY["connect-src"] and "http://ipc.localhost" in POLICY["connect-src"]
    assert POLICY["worker-src"] == ["'self'"]
    assert POLICY["font-src"] == ["'self'"]
    assert POLICY["media-src"] == ["'none'"]
    assert POLICY["frame-src"] == ["'none'"]
    assert POLICY["form-action"] == ["'self'"]


def test_served_document_uses_the_desktop_csp_and_adds_voice_only_when_configured(tmp_path):
    import http.client

    from tessera.config import Config
    from tessera.server import create_server

    base = _policy_text()
    config = Config(home=tmp_path, cwd=tmp_path)
    if not hasattr(config, "voice"):
        pytest.skip("Older servers have no voice API")
    config.server.port = 0
    config.jev.enabled = False
    config.voice.enabled = False
    server = create_server(config, web_root=WEB, backend_factory=lambda *_args, **_kwargs: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Security-Policy") == base
        response.read()

        config.voice.enabled = True
        config.voice.websocket_url = "wss://voice.example.com/session-placeholder"
        config.voice.model = "model-placeholder"
        config.voice.api_base_url = "https://voice.example.com"
        config.voice.client_secret_path = "/client-secrets-placeholder"
        config.voice.api_key_env = "EXAMPLE_UNUSED_VOICE_TOKEN"
        config.voice.client_protocol_prefix = "example-prefix."
        connection.request("GET", "/")
        response = connection.getresponse()
        policy = response.getheader("Content-Security-Policy")
        response.read()
        assert policy == base.replace(
            "connect-src 'self' ipc: http://ipc.localhost;",
            "connect-src 'self' ipc: http://ipc.localhost wss://voice.example.com;",
            1,
        )
        assert "session-placeholder" not in policy and "model-placeholder" not in policy
        config.voice.enabled = False
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.getheader("Content-Security-Policy") == base
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
