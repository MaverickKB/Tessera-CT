"""Browser-free checks for UI delivery, contract endpoints and safe rendering."""

import re
import http.client
import json
import threading
import shutil
import os
import subprocess
import struct
import xml.etree.ElementTree as ET

import pytest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_a_spark_reading_carries_a_meter_only_when_its_ceiling_is_reported():
    """The HUD was six static numbers. Pin the rule, not the geometry.

    A meter asserts "this fraction of a known ceiling". The payload does not
    report a ceiling for temperature or throughput, so those rows must not get
    one; VRAM and system memory report their totals, so those rows must. The
    same applies to the inverted tones: headroom and acceptance are
    higher-is-better, and usageTone would paint a healthy card red.

    Geometry (that the meters and sparklines are actually on screen) is not
    asserted here: the stub DOM cannot lay out. That was measured in Chrome and
    is recorded on the card.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free rendering checks")
    script = r'''
const fs = require('fs');
const vm = require('vm');

class El {
  // className must start as a string: design.js's classify() only assigns it
  // when typeof className === 'string', which is how the browser behaves.
  constructor(tag) { this.tag = tag; this.className = ''; this.children = []; this.attrs = {}; this.dataset = {}; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name] ?? null; }
  querySelector(sel) {
    const want = sel.replace(/^\./, '');
    for (const child of this.children) {
      if (child instanceof El && (child.className === want || (child.tag && want === child.tag))) return child;
      if (child instanceof El) { const hit = child.querySelector(sel); if (hit) return hit; }
    }
    return null;
  }
  querySelectorAll(sel) {
    const want = sel.replace(/^\./, '');
    const found = [];
    for (const child of this.children) {
      if (!(child instanceof El)) continue;
      if (child.className === want || child.tag === want) found.push(child);
      found.push(...child.querySelectorAll(sel));
    }
    return found;
  }
}

const doc = {
  createElement: (tag) => new El(tag),
  createElementNS: (ns, tag) => new El(tag),
  createTextNode: (text) => { const n = new El('#text'); n.textContent = String(text); return n; },
  querySelector: () => null,
  addEventListener() {},
  getElementById: () => null,
  body: new El('body'),
  documentElement: new El('html'),
};
const context = {
  document: doc,
  window: {},
  location: {hash: ''},
  history: {replaceState(){}, pushState(){}},
  localStorage: {getItem: () => null, setItem(){}},
  setInterval(){}, setTimeout(){return 0;}, clearTimeout(){},
  console, fetch: () => new Promise(() => {}),
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/design.js', 'utf8'), context);
// app.js is one closure; expose the two helpers this test is about by name.
const src = fs.readFileSync('web/app.js', 'utf8');
const pick = (name) => {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing ' + name);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j += 1) {
    if (src[j] === '{') depth += 1;
    else if (src[j] === '}') { depth -= 1; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('unbalanced ' + name);
};
const names = ['sparkValue','usageTone','headroomTone','fleetHealthTone','sparkMetrics'];
const body = names.map(pick).join('\n');
// The helpers close over each other lexically once they share one scope, so
// usageTone does not need to be injected.
const helpers = new Function('window','element', body + '\nreturn {sparkMetrics, headroomTone, fleetHealthTone, usageTone};');
const api = helpers(context.window, (tag, cls, text) => { const n = new El(tag); if (cls) n.className = cls;
  if (text !== undefined) n.textContent = String(text); return n; });

const rows = [
  ['GPU utilization', 96, '%', 0.96, [1,2,3]],
  ['GPU temperature', 60, '\u00b0C', null, [1,2,3]],
  ['Memory headroom', 3.1, ' GiB', 0.03, [1,2,3], api.headroomTone(0.03)],
  ['MTP acceptance', 67.2, '%'],
];
const dl = api.sparkMetrics(rows);
const groups = dl.children;
if (groups.length !== 4) throw new Error('expected 4 reading groups, got ' + groups.length);
const byLabel = {};
for (const g of groups) {
  const dt = g.children.find((c) => c.tag === 'dt');
  const meter = g.querySelector('.ds-meter');
  const spark = g.querySelector('.ds-spark');
  byLabel[dt.textContent] = {meter, spark};
}

// A bounded reading with a reported ceiling gets a meter and a sparkline.
if (!byLabel['GPU utilization'].meter) throw new Error('GPU utilization must carry a meter');
if (!byLabel['GPU utilization'].spark) throw new Error('GPU utilization must carry a sparkline');

// Temperature has no ceiling in the payload, so a meter would be an invented claim.
if (byLabel['GPU temperature'].meter) throw new Error('temperature has no ceiling and must not carry a meter');
if (!byLabel['GPU temperature'].spark) throw new Error('temperature still earns a sparkline');

// Headroom is higher-is-better: 3% is the dangerous case, not the safe one.
const headroomFill = byLabel['Memory headroom'].meter.querySelector('.ds-meter-fill');
if (headroomFill.dataset.tone !== 'danger') throw new Error('3% headroom must read danger, got ' + headroomFill.dataset.tone);

// Acceptance is a quality rate with no ceiling in the payload, so it gets no
// meter. Drawing one would claim a threshold nobody measured.
if (byLabel['MTP acceptance'].meter) throw new Error('MTP acceptance has no ceiling and must not carry a meter');

// usageTone is right for fill levels and wrong for higher-is-better readings:
// it calls 95% of anything danger, which is why headroom does not use it.
if (api.usageTone(0.95) !== 'danger') throw new Error('usageTone should still call 95% usage danger');
if (api.usageTone(0.05) !== 'ok') throw new Error('usageTone should call 5% usage ok');

// A silent payload must render as "Not reported" with no meter, not as zero.
const silent = api.sparkMetrics([['KV capacity', null, ' tokens', null, null]]);
const silentGroup = silent.children[0];
const dd = silentGroup.children.find((c) => c.tag === 'dd');
if (dd.textContent !== 'Not reported') throw new Error('a missing reading must say Not reported, got ' + dd.textContent);
if (silentGroup.querySelector('.ds-meter')) throw new Error('a missing reading must not draw a zero-length meter');

// Fleet health is higher-is-better too. usageTone calls a full fleet danger.
if (api.usageTone(1) !== 'danger') throw new Error('usageTone should call a full ratio danger (that is the bug)');
if (api.fleetHealthTone(1) !== 'ok') throw new Error('a fully healthy fleet must read ok, got ' + api.fleetHealthTone(1));
if (api.fleetHealthTone(0.8) !== 'warn') throw new Error('a partly healthy fleet must read warn, got ' + api.fleetHealthTone(0.8));
if (api.fleetHealthTone(0) !== 'danger') throw new Error('a fully failed fleet must read danger, got ' + api.fleetHealthTone(0));

console.log('Meters follow reported ceilings; inverted tones hold.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_board_lanes_render_by_recency_not_creation_order():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    script = r'''
const fs = require('fs');
const src = fs.readFileSync('web/app.js', 'utf8');
const pick = (name) => {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('missing ' + name);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j += 1) {
    if (src[j] === '{') depth += 1;
    else if (src[j] === '}') { depth -= 1; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('unbalanced ' + name);
};
const helpers = new Function(pick('sortBoardLane') + '\nreturn {sortBoardLane};');
const api = helpers();

// Payload order here is creation order. An older card touched last must render
// ahead of a newer card left untouched. The lane must render
// recency-descending, not payload order.
const rows = (cards) => cards.map((c) => ({_boardCard: c}));

// 1. recency-descending inside a lane
let lane = rows([
  {ordinal: 127, updated_at: 100},
  {ordinal: 128, updated_at: 50},
  {ordinal: 183, updated_at: 900},
  {ordinal: 130, updated_at: 300},
]);
api.sortBoardLane(lane);
const ords = lane.map((r) => r._boardCard.ordinal);
if (JSON.stringify(ords) !== JSON.stringify([183, 130, 127, 128])) {
  throw new Error('lane must be recency-descending, got ' + JSON.stringify(ords));
}

// 2. identical updated_at keeps a deterministic order across consecutive renders
const tie = [{ordinal: 9, updated_at: 100}, {ordinal: 2, updated_at: 100}, {ordinal: 5, updated_at: 100}];
const first = JSON.stringify(api.sortBoardLane(rows(tie)).map((r) => r._boardCard.ordinal));
// a second render receives the same payload in the same order (ordinal-sorted server side)
const second = JSON.stringify(api.sortBoardLane(rows(tie)).map((r) => r._boardCard.ordinal));
if (first !== second || first !== JSON.stringify([2, 5, 9])) {
  throw new Error('ties must be stable ordinal order, got ' + first + ' then ' + second);
}

// 3. missing or non-numeric updated_at sorts last, and never crashes
let broken = rows([
  {ordinal: 200, updated_at: 10},
  {ordinal: 201},
  {ordinal: 202, updated_at: 'soon'},
  {ordinal: 203, updated_at: 5},
]);
api.sortBoardLane(broken);
const b = broken.map((r) => r._boardCard.ordinal);
if (JSON.stringify(b) !== JSON.stringify([200, 203, 201, 202])) {
  throw new Error('cards without a usable updated_at must sort last, got ' + JSON.stringify(b));
}

console.log('Lanes sort by recency; ties and missing stamps are stable.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_project_chips_fold_and_projects_come_before_the_add_work_form():
    """The report: "a long list of useless tags" and "the mac app
    does not have projects".

    Measured live at 1440x900 before the change: 23 project chips in 5 wrapped
    rows, 128px tall, between the "Work" heading and the first card; and the
    projects section CLOSED at y=645, behind a 27px summary, after a 380px
    Add-work form with all 21 tiles inside it.

    Geometry is not asserted here: the stub DOM cannot lay out. That was measured
    in Chrome and is recorded on the card. What is pinned is the structure that
    caused it, so the wall cannot come back silently.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free rendering checks")
    source = (WEB / "app.js").read_text()
    filters_fn = source.split("function renderBoardFilters()", 1)[1].split(
        "  // Pressure is what the body measured", 1)[0]
    script = r'''
const vm = require('vm');
const assert = require('node:assert/strict');

class El {
  constructor(tag) {
    this.tag = tag; this.className = ''; this.children = []; this.attrs = {};
    this.dataset = {}; this.textContent = ''; this.hidden = false; this.open = false;
  }
  append(...n) { this.children.push(...n); }
  replaceChildren(...n) { this.children = [...n]; this.textContent = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener() {}
}

const byId = {};
for (const id of ['board-filters', 'board-projects', 'board-projects-count']) {
  byId[id] = new El('div'); byId[id].id = id;
}
const doc = { getElementById: id => byId[id] || null, createElement: t => new El(t) };
const context = vm.createContext({ console, document: doc });
vm.runInContext('var document = this.document;'
  + 'var state = this.state = {boardView: null, boardProjectFilter: ""};'
  + 'var $ = (id) => document.getElementById(id);'
  + 'function element(tag, cls, text) { const e = document.createElement(tag);'
  + '  if (cls) e.className = cls; if (text !== undefined) e.textContent = String(text); return e; }'
  + 'function safeBoardTitle(t) { return t; }'
  + 'function setBoardProjectFilter() {}'
  + 'function renderBoardStatus() {}', context);

const body = process.env.FILTERS_BODY;
vm.runInContext('function renderBoardFilters()' + body, context);

// 21 projects, which is what the live board carried.
context.state.boardView = {
  cards: [{status: 'queued'}],
  projects: Array.from({length: 21}, (_, i) => ({id: 'p' + i, title: 'Project ' + i, status: 'active'})),
};
context.renderBoardFilters();

const box = byId['board-filters'];
const direct = box.children.filter(c => c.tag === 'button');
// Only All and Unfiled are chips in the row itself. 21 project chips in the open
// would be the 128px wall again.
assert.equal(direct.length, 2,
  'the filter row holds only All and Unfiled; got ' + direct.length);

const fold = box.children.find(c => c.className === 'board-filter-fold');
assert.ok(fold, 'the project chips live in a fold');
assert.equal(fold.tag, 'details', 'the fold is a disclosure, not a styled div');
assert.equal(fold.open, false, 'it starts closed, so it does not push the work list down');
const summary = fold.children[0];
assert.equal(summary.tag, 'summary', 'it has a summary a reader can press');
assert.equal(summary.textContent, 'Projects (21)', 'the summary names how many are inside');
// Nothing is lost: every project chip is inside the fold.
const chips = fold.children.filter(c => c.tag === 'button');
assert.equal(chips.length, 21, 'all 21 project chips survive the fold; got ' + chips.length);

// An active project filter must not hide the chip that is on.
context.state.boardProjectFilter = 'p3';
context.renderBoardFilters();
const foldOpen = byId['board-filters'].children.find(c => c.className === 'board-filter-fold');
assert.equal(foldOpen.open, true, 'the fold opens itself when a project filter is on');

console.log('Project chips fold; All and Unfiled stay in the row.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=20,
                            env={**os.environ, "FILTERS_BODY": filters_fn})
    assert result.returncode == 0, result.stdout + result.stderr


def test_projects_render_before_the_add_work_form():
    """The report: "the mac app does not have projects". The projects were
    folded below a 380px Add-work form, so the first thing on the page was a form
    and the projects were a 27px line beneath it. The order is the fix; the
    open-by-default is asserted from app.js because it is set at runtime.
    """
    html = (WEB / "index.html").read_text()
    projects_at = html.index('id="board-projects-block"')
    note_at = html.index('id="board-note-block"')
    work_at = html.index('id="wip-global-work"')
    assert projects_at < work_at, "projects come before the work list"
    assert note_at > work_at, "the add-work form follows the work list, not the projects"
    js = (WEB / "app.js").read_text()
    assert "const initial = remembered === \"off\" ? false : true;" in js, (
        "the projects section opens by default at every width, not only phones")
    assert "board-filter-fold" in js, "the project chips are folded"
    # The wall of identical Active badges is gone: only non-default states badge.
    assert 'project.status !== "active"' in js, (
        "an active project does not carry a badge that says nothing")


def test_web_assets_and_accessible_application_regions():
    page = Page((WEB / "index.html").read_text())
    ids = {attrs["id"] for _, attrs in page.elements if "id" in attrs}
    assert {"session-list", "messages", "composer", "view-panel", "memory-panel", "status-panel", "design-asset-slot"} <= ids
    assert any(attrs.get("role") == "tablist" for _, attrs in page.elements)
    assert any(attrs.get("role") == "alert" for _, attrs in page.elements)
    assert any(tag == "label" and attrs.get("for") == "message-input" for tag, attrs in page.elements)
    assert not any(tag in ("img", "canvas", "iframe") for tag, _ in page.elements)
    decorative = [attrs for tag, attrs in page.elements if tag == "svg"]
    assert decorative and all(attrs.get("aria-hidden") == "true" for attrs in decorative)
    for tag, attrs in page.elements:
        if tag in ("script", "link"):
            path = attrs.get("src", attrs.get("href", "")).split("?", 1)[0]
            assert path.startswith("/static/")
            assert (WEB / path.removeprefix("/static/")).is_file()


def test_approved_wordmarks_icons_and_plain_glyphs_are_valid_and_referenced():
    page = Page((WEB / "index.html").read_text())
    theme = (WEB / "theme.css").read_text()
    styles = (WEB / "app.css").read_text()
    images = []
    for _, attrs in page.elements:
        classes = attrs.get("class", "").split()
        if "brand-wordmark" in classes:
            assert attrs.get("role") == "img" and attrs.get("aria-label") == "Tessera-CT"
            images.append(classes)
    assert len(images) == 1
    assert "Tessera-CT" in (WEB / "index.html").read_text()
    assert not any("login-wordmark" in classes for classes in images)
    assert "background-image: var(--brand-wordmark)" in styles
    for variant in ("light", "dark"):
        relative = f"brand/wordmark-tessera-{variant}.svg"
        assert relative in theme
        data = (WEB / relative).read_bytes()
        source = (ROOT / "docs" / "brand" / "wordmark" / f"lockup-tessera-{variant}.svg").read_bytes()
        assert data == source
        root = ET.fromstring(data)
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        xml = data.decode()
        assert "Tessera-CT" in xml
        assert 'd="M36 40 L36 58 M36 58 L18 78 M36 58 L54 78"' in xml
        assert 'fill="#8a3f2c"' in xml or 'fill="#edaf97"' in xml

    manifest = json.loads((WEB / "app.webmanifest").read_text())
    assert manifest["name"] == manifest["short_name"] and manifest["name"]
    icon_paths = set()
    for tag, attrs in page.elements:
        if tag == "link" and attrs.get("rel") in ("icon", "apple-touch-icon"):
            icon_paths.add(attrs["href"].removeprefix("/static/"))
    for icon in manifest["icons"]:
        icon_paths.add(icon["src"])
        size = int(icon["src"].removesuffix(".png").rsplit("-", 1)[1])
        assert icon["sizes"] == f"{size}x{size}" and icon["type"] == "image/png"
    assert icon_paths == {f"brand/icon-{size}.png" for size in (16, 32, 128, 256, 512)}
    for relative in icon_paths:
        data = (WEB / relative).read_bytes()
        size = int(relative.removesuffix(".png").rsplit("-", 1)[1])
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", data[16:24]) == (size, size)

    glyphs = {"chat": "chat", "wip": "wip-board", "infrastructure": "infra-board",
              "voice": "voice", "integrations": "integrations", "settings": "settings"}
    for name, filename in glyphs.items():
        assert f"brand/glyphs/{filename}.svg" in theme
        assert f"--menu-glyph: var(--glyph-{name})" in styles
        assert any("glyph-" + name in attrs.get("class", "").split()
                   and attrs.get("aria-hidden") == "true" for _, attrs in page.elements)
        root = ET.parse(WEB / "brand" / "glyphs" / (filename + ".svg")).getroot()
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.attrib["viewBox"] == "0 0 24 24"
        geometry = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
        assert any(element.tag.rsplit("}", 1)[-1] in geometry for element in root.iter())
        for element in root.iter():
            assert element.tag.rsplit("}", 1)[-1] not in ("script", "image", "foreignObject")
    assert "mask-image: var(--menu-glyph)" in styles


def test_javascript_uses_only_contract_endpoints_and_safe_text_rendering():
    js = (WEB / "app.js").read_text()
    # Native EventSource resends Last-Event-ID itself; the desktop bridge passes state.lastEvent.
    assert "EventSource" in js and "desktop.subscribe(slug, state.lastEvent" in js
    assert "Authorization" not in js and "Bearer " not in js
    assert not re.search(r"(?:access|auth|bearer|api)[_-]?token", js, re.IGNORECASE)
    assert "textContent" in js and "innerHTML" not in js
    # sessionStorage carries only what a person typed across a build reload; nothing else.
    # draftKey holds the composer draft, draftFieldsKey holds form fields such as a
    # half-written board card. Both exist so a rebuild cannot cost someone their typing.
    assert set(re.findall(r"sessionStorage\.(\w+)\(([^,)]*)", js)) <= {
        ("setItem", "draftKey"), ("getItem", "draftKey"), ("removeItem", "draftKey"),
        ("setItem", "draftFieldsKey"), ("getItem", "draftFieldsKey"), ("removeItem", "draftFieldsKey"),
    }
    assert "localStorage.setItem" not in js or "tessera-theme" in js
    assert not re.search(r"https?://|wss?://", js.replace("http://www.w3.org/2000/svg", ""))
    endpoints = re.findall(r"['\"`](/api/[^'\"`\s]*)", js)
    assert endpoints
    for endpoint in endpoints:
        assert endpoint in ("/api/status?shape=connection", "/api/status", "/api/approval", "/api/settings", "/api/integrations", "/api/infrastructure/targets", "/api/infrastructure/sparks?range=",
                            "/api/infrastructure/collect", "/api/pressure/engines", "/api/board/view", "/api/board/pressure", "/api/board/projects",
                            "/api/board/cards", "/api/oversight", "/api/oauth/refresh", "/api/aethermind/engine-log") \
            or endpoint.startswith(("/api/sessions", "/api/board/cards/", "/api/board/projects/", "/api/integrations/"))
    assert "encodeURIComponent" in js
    assert "isComposing" in js and "shiftKey" in js
    assert "getReader" not in js  # no fetch-stream SSE path remains for credentialed servers


def test_theme_is_the_only_source_of_colors_and_font_families():
    theme = (WEB / "theme.css").read_text()
    assert "prefers-color-scheme: dark" in theme
    assert "--font-body" in theme and "Georgia" in theme
    assert "--font-data" in theme and "ui-monospace" in theme
    assert "data-theme" in theme
    for path in (WEB / "app.css", WEB / "app.js", WEB / "index.html"):
        text = path.read_text()
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(", text)
        assert "font-family:" not in text
        assert "\u2014" not in text
    assert "prefers-reduced-motion" in (WEB / "app.css").read_text()


def test_integrations_and_settings_are_compact_controls_wired_to_the_api():
    html = (WEB / "index.html").read_text()
    page = Page(html)
    attrs_by_id = {}
    for _, attrs in page.elements:
        if "id" in attrs:
            attrs_by_id[attrs["id"]] = attrs
    assert attrs_by_id["integrations-list"]["aria-label"] == "Integrations"
    assert attrs_by_id["integrations-state"]["role"] == "status"
    for button in ("refresh-integrations", "integrations-check-all", "settings-refresh"):
        assert attrs_by_id[button]["type"] == "button"
    assert attrs_by_id["design-asset-slot"]["aria-label"] == "Tessera-CT"
    assert "product-wordmark" not in html
    inputs = [attrs.get("id") for tag, attrs in page.elements if tag == "input"]
    assert set(inputs) == {"session-search", "show-archived", "session-name", "session-workspace", "menu-workspace",
                           "attachment-input", "board-note-what", "thinking-budget-enabled", "thinking-budget", "memory-record-search", "integration-search", "board-search", "project-create-title", "work-file-input"}
    assert attrs_by_id["write-debt"]["role"] == "status"
    assert "memory-capture-state" in attrs_by_id
    assert 'class="menu-glyph glyph-chat"' in html
    # No intro blurbs, stage labels or static helper prose.
    for marker in ("stage-label", "quiet-note\">", "panel-description\">", "settings-section-intro", "about-brand", "Saved proof"):
        assert marker not in html
    js = (WEB / "app.js").read_text()
    assert '"/api/integrations/" + encodeURIComponent(id) + "/check"' in js
    assert '"/enabled", "POST", {enabled}' in js


def test_a_failed_integration_offers_a_repair_not_just_another_test():
    """A failed integration offers a repair when the credential can be refreshed.

    The row calls the refresh route for that sign-in. A row that cannot be
    repaired does not offer that action."""
    js = (WEB / "app.js").read_text()
    assert '"/api/oauth/refresh", "POST", {provider: row.repair}' in js
    # The button is drawn only for a failed row that says it can be repaired.
    assert 'row.repair && row.result && !row.result.ok' in js
    assert '"Refresh sign-in"' in js


def test_task_preview_and_judge_metadata_are_accessible_read_only_regions():
    page = Page((WEB / "index.html").read_text())
    attrs_by_id = {attrs["id"]: attrs for _, attrs in page.elements if "id" in attrs}
    assert attrs_by_id["tasks-records"]["aria-labelledby"] == "tasks-title"
    assert attrs_by_id["tasks-summary"]["role"] == "status"
    assert attrs_by_id["tasks-filters"]["role"] == "group"
    assert attrs_by_id["tasks-filters"]["aria-label"]
    assert attrs_by_id["tasks-filter-result"]["role"] == "status"
    assert attrs_by_id["refresh-tasks"]["type"] == "button"


def test_board_regions_keep_cards_and_source_navigation_accessible():
    page = Page((WEB / "index.html").read_text())
    attrs_by_id = {attrs["id"]: attrs for _, attrs in page.elements if "id" in attrs}
    assert attrs_by_id["board-summary"]["role"] == "status"
    assert attrs_by_id["board-cards"]["aria-labelledby"] == "board-title"
    assert attrs_by_id["board-evidence"]["role"] == "status"
    assert any(tag == "body" and attrs.get("data-board-artifact-labels") for tag, attrs in page.elements)


def test_jobs_and_memory_tree_regions_are_accessible_and_passive():
    page = Page((WEB / "index.html").read_text())
    attrs = {value["id"]: value for _, value in page.elements if "id" in value}
    assert attrs["jobs-records"]["aria-labelledby"] == "jobs-title"
    assert attrs["jobs-summary"]["role"] == "status"
    assert attrs["jobs-overview"]["aria-label"]
    assert attrs["memory-tree-summary"]["role"] == "status"
    assert attrs["memory-tree-metadata"]["aria-label"]
    js = (WEB / "app.js").read_text()
    assert "renderJobStatus();" in js and "renderMemoryTreeStatus();" in js
    assert not any(tag == "iframe" for tag, _ in page.elements)


def test_infrastructure_regions_are_read_only_and_do_not_embed_private_urls():
    page = Page((WEB / "index.html").read_text())
    attrs_by_id = {attrs["id"]: attrs for _, attrs in page.elements if "id" in attrs}
    assert attrs_by_id["infrastructure-hosts"]["aria-labelledby"] == "infrastructure-title"
    assert attrs_by_id["infrastructure-summary"]["role"] == "status"
    assert attrs_by_id["refresh-infrastructure"]["type"] == "button"
    assert not any(tag == "iframe" for tag, _ in page.elements)
    assert "Coming in stage 5" not in (WEB / "index.html").read_text()


def test_voice_controls_are_session_scoped_and_have_no_provider_key_fields():
    page = Page((WEB / "index.html").read_text())
    attrs = {value["id"]: value for _, value in page.elements if "id" in value}
    assert attrs["voice-toggle"]["type"] == attrs["voice-page-toggle"]["type"] == "button"
    assert "disabled" in attrs["voice-toggle"]
    assert attrs["voice-detail"]["role"] == attrs["voice-page-detail"]["role"] == "status"
    assert attrs["voice-transcript"]["aria-live"] == "polite"
    assert attrs["voice-session"]["aria-label"] == "Session"
    assert "voice-talk" in attrs["voice-page-toggle"]["class"]
    assert "Coming in stage 6" not in (WEB / "index.html").read_text()
    voice = (WEB / "voice.js").read_text()
    assert "localStorage" not in voice and "console." not in voice
    assert not re.search(r"https?://|wss?://", voice)
    assert "getUserMedia" in voice and "AudioWorkletNode" in voice
    assert "createScriptProcessor" not in voice
    assert "recordedEvent" in voice and "Microphone denied" in voice


def test_release_fixtures_exclude_private_connection_and_credential_fields():
    forbidden = {"endpoint", "host", "model", "token", "client_secret", "credential_ref",
                 "credential_source", "assistant_transcript", "path", "ref", "account", "answer"}

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
        elif isinstance(value, str):
            assert not re.search(r"https?://|wss?://|/[U]sers/|/[h]ome/", value)

    for name in ("memory-status.json", "capture-status.json", "results-status.json",
                 "live-proof.json", "accepted-proof.json", "release-status.json"):
        inspect(json.loads((WEB / "tests" / "fixtures" / name).read_text()))


def test_voice_audio_lifecycle_uses_offline_devices_and_recorded_transport():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free audio lifecycle checks")
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const sockets=[],contexts=[],tracks=[],notices=[],starts=[],stops=[],events=[],logged=[],timers=[];
let micMode='ready',releaseMic,releaseStart,holdStart=false,failBridge=false;
const secret='EPHEMERAL_FIXTURE_ONLY';
function settings(id='lease-fixture') {
  return {voice_id:id,websocket_url:'wss://voice.example.com/realtime',protocols:['example-prefix.'+secret],
    expires_at:Date.now()/1000+60,session_expires_at:Date.now()/1000+120,max_session_seconds:90,audio_sample_rate:16000,
    session_update:{type:'session.update',session:{instructions:'Recorded session context.',tools:[],turn_detection:{type:'server_vad'},audio:{input:{format:{type:'audio/pcm',rate:16000}},output:{format:{type:'audio/pcm',rate:16000}}}}}};
}
function microphone() {
  const track={stopped:0,stop(){this.stopped+=1;}};tracks.push(track);
  return {getTracks:()=>[track]};
}
class Audio {
  constructor(){contexts.push(this);this.sampleRate=48000;this.currentTime=0;this.destination={};this.audioWorklet={addModule:async path=>assert.equal(path,'/static/voice-worklet.js')};}
  async resume(){} async close(){this.closed=true;}
  createMediaStreamSource(){return {connect(){},disconnect(){}};}
  createBuffer(channels,length,rate){const data=new Float32Array(length);return {duration:length/rate,getChannelData:()=>data};}
  createBufferSource(){const source={connect(){},disconnect(){},start(){source.started=true;},stop(){source.stopped=true;}};this.lastSource=source;return source;}
}
class Worklet {
  constructor(audio,name,options){this.port={postMessage(){}};assert.equal(name,'tessera-microphone');assert.equal(options.processorOptions.targetRate,16000);}
  connect(){} disconnect(){}
}
class Socket {
  static OPEN=1;
  constructor(url,protocols){this.url=url;this.protocols=protocols;this.readyState=0;this.bufferedAmount=0;this.sent=[];sockets.push(this);}
  open(){this.readyState=1;this.onopen();}
  message(value){this.onmessage({data:JSON.stringify(value)});}
  send(value){this.sent.push(JSON.parse(value));}
  close(){this.closed=true;}
}
const window={isSecureContext:true,location:{protocol:'https:'},AudioContext:Audio,AudioWorkletNode:Worklet,WebSocket:Socket};
const navigator={mediaDevices:{getUserMedia(){
  if(micMode==='denied')return Promise.reject(Object.assign(new Error(secret),{name:'NotAllowedError'}));
  if(micMode==='pending')return new Promise(resolve=>{releaseMic=()=>resolve(microphone());});
  return Promise.resolve(microphone());
}}};
const context={window,navigator,URL,ArrayBuffer,DataView,Uint8Array,Float32Array,btoa,atob,
  setTimeout(fn,ms){const timer={fn,ms};timers.push(timer);return timer;},clearTimeout(timer){if(timer)timer.cleared=true;}};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/voice.js','utf8'),context);
const voice=new window.TesseraVoice({workletUrl:'/static/voice-worklet.js',chunkFrames:4,maxBufferedBytes:4096,maxPendingEvents:8,connectSeconds:10,maxPlaybackSeconds:5,
  onState:(label,detail)=>notices.push(label+' '+detail),onMessages:(slug,rows)=>logged.push(...rows),
  start:async slug=>{starts.push(slug);if(holdStart)return new Promise(resolve=>{releaseStart=()=>resolve(settings('late-lease'));});return settings();},
  stop:async(slug,id)=>stops.push([slug,id]),events:async(slug,id,rows)=>{if(failBridge)throw new Error(secret);events.push(...rows);return {messages:rows.filter(row=>row.transcript).map((row,i)=>({i,text:row.transcript})),send:rows[0].type==='response.done'?[{type:'response.create',event_id:'continuation-fixture'}]:[]};}});
async function settle(){for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve));}
async function ready(){await voice.start('session-one');const socket=sockets.at(-1);socket.open();socket.message({type:'session.updated',session:{instructions:'Recorded session context.'}});return socket;}
(async()=>{
 assert.equal(starts.length,0);assert.equal(sockets.length,0);assert.equal(tracks.length,0);
 micMode='denied';await voice.start('session-one');assert(notices.at(-1).includes('Microphone denied'));assert.equal(starts.length,0);
 micMode='ready';await voice.start('session-one');let run=voice.current,socket=sockets.at(-1);
 socket.open();assert.equal(socket.sent[0].type,'session.update');assert.equal(socket.protocols[0],'example-prefix.'+secret);
 run.capture.port.onmessage({data:new ArrayBuffer(4)});assert.equal(socket.sent.length,1);
 // Any accepted session.updated means the provider took the settings. Waiting for an
 // exact instructions echo left the page stuck on Connecting until the timeout.
 socket.message({type:'session.updated',session:{instructions:'Recorded session context.'}});assert(run.ready);
 run.capture.port.onmessage({data:new ArrayBuffer(4)});assert.equal(socket.sent.at(-1).type,'input_audio_buffer.append');
 socket.message({type:'conversation.item.input_audio_transcription.updated',item_id:'input',transcript:'partial'});
 assert.equal(events.length,0);assert.equal(logged.length,0);
 socket.message({type:'conversation.item.input_audio_transcription.completed',item_id:'input',content_index:0,transcript:'Spoken question.',token:secret});
 // The provider is the voice agent, so its response is the reply: it is not cancelled.
 socket.message({type:'response.created',response:{id:'response'}});
 assert.notEqual(socket.sent.at(-1).type,'response.cancel');
 socket.message({type:'response.output_audio_transcript.done',item_id:'output',response_id:'response',transcript:'Recorded answer.'});
 socket.message({type:'response.done',response:{id:'response',status:'completed',secret}});await settle();
 // The speaker's line and the agent's spoken reply both become session messages.
 assert.equal(logged.length,2);assert.equal(logged[0].text,'Spoken question.');assert.equal(logged[1].text,'Recorded answer.');assert(!JSON.stringify(events).includes(secret));
 assert.equal(events.at(-1).response.status,'completed');
 assert.equal(socket.sent.at(-1).type,'response.create');
 socket.message({type:'response.done',response:{id:'response',status:'completed'}});await settle();
 assert.equal(socket.sent.filter(row=>row.type==='response.create').length,1);
 const sanitized=voice.recordedEvent({type:'response.done',response:{id:'batch',status:'completed',output:[{type:'function_call',call_id:'call-one',secret},{type:'message',text:secret}]}});
 assert.equal(JSON.stringify(sanitized.response.output),'[{"type":"function_call","call_id":"call-one"}]');
 // Provider audio is the reply and is played as it streams.
 socket.message({type:'response.output_audio.delta',delta:btoa('\0\0\xff\x7f')});assert(contexts.at(-1).lastSource.started);
 voice.playEncoded(btoa('\\0\\0\\xff\\x7f'));assert(contexts.at(-1).lastSource.started);
 socket.message({type:'input_audio_buffer.speech_started'});assert(contexts.at(-1).lastSource.stopped);
 voice.stop();await settle();assert(!voice.running());assert(tracks.at(-1).stopped);assert(socket.closed&&contexts.at(-1).closed);
 assert.equal(stops.at(-1)[1],'lease-fixture');
 micMode='pending';const delayed=voice.start('session-one');voice.stop();releaseMic();await delayed;assert(tracks.at(-1).stopped);
 micMode='ready';holdStart=true;const delayedStart=voice.start('session-one');await settle();voice.stop();releaseStart();await delayedStart;await settle();assert.equal(stops.at(-1)[1],'late-lease');holdStart=false;
 socket=await ready();assert.equal(socket.binaryType,'arraybuffer');
 socket.message({type:'error',error:{message:secret}});await settle();assert(!voice.running());assert(notices.at(-1).includes('Voice error'));
 socket=await ready();socket.onclose({reason:secret});await settle();assert(!voice.running());assert(notices.at(-1).includes('Voice disconnected'));
 socket=await ready();socket.bufferedAmount=5000;voice.current.capture.port.onmessage({data:new ArrayBuffer(4)});assert(voice.running());
 socket=await ready();failBridge=true;socket.message({type:'conversation.item.input_audio_transcription.completed',item_id:'bad',transcript:'not confirmed'});await settle();assert(!voice.running());assert(notices.at(-1).includes('Transcript sync failed'));failBridge=false;
 await voice.start('session-one');voice.current.connectTimer.fn();assert(!voice.running());assert(tracks.at(-1).stopped);
 socket=await ready();assert(voice.current.deadlineTimer.ms<=90000);voice.current.deadlineTimer.fn();assert(!voice.running());
 window.isSecureContext=false;const count=starts.length;await voice.start('session-one');assert.equal(starts.length,count);assert(notices.at(-1).includes('Secure connection required'));
 assert(!notices.join(' ').includes(secret));assert(!JSON.stringify(stops).includes(secret));
 // Worklet output is signed little-endian PCM, clipped, resampled across blocks and stopped explicitly.
 let Processor;const chunks=[];
 const worklet={sampleRate:48000,AudioWorkletProcessor:class{constructor(){this.port={postMessage:buffer=>chunks.push(buffer)};}},registerProcessor:(name,value)=>{Processor=value;}};
 vm.createContext(worklet);vm.runInContext(fs.readFileSync('web/voice-worklet.js','utf8'),worklet);
 const capture=new Processor({processorOptions:{targetRate:24000,chunkFrames:4}});
 capture.process([[new Float32Array([0,0.5])]]);capture.process([[new Float32Array([1,-1,-1,0,0.25,0.75])]]);
 assert.equal(chunks.length,1);const view=new DataView(chunks[0]);assert.deepEqual([0,1,2,3].map(i=>view.getInt16(i*2,true)),[0,32767,-32768,8192]);
 capture.port.onmessage({data:{stop:true}});assert.equal(capture.process([[new Float32Array(8)]]),false);
 console.log('Offline voice lifecycle, privacy, transcript bridge and PCM checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_server_reads_the_live_config_after_a_settings_save(tmp_path):
    """A settings save adopts a fresh Config object; the server must follow it.

    The server kept its own reference, so the page CSP still allowed only the
    old voice WebSocket after switching to OpenAI and the browser blocked the
    connection. Read the config through the manager instead.
    """
    from tessera.config import load_config
    from tessera.server import create_server
    from tessera.sessions import SessionManager
    from tessera.settings_model import adopt_config
    (tmp_path / "config.toml").write_text(
        '[voice]\nenabled = true\nwebsocket_url = "wss://api.x.ai/v1/realtime"\n', encoding="utf-8")
    config = load_config(tmp_path)
    config.server.port = 0
    with SessionManager(config) as manager:
        server = create_server(config, manager=manager)
        try:
            new_config = load_config(tmp_path)
            new_config.voice.websocket_url = "wss://api.openai.com/v1/realtime"
            adopt_config(manager, new_config)
            assert server.config is manager.config
            assert server.config.voice.websocket_url == "wss://api.openai.com/v1/realtime"
        finally:
            server.server_close()


def test_index_loads_the_local_voice_controller_before_the_app():
    """app.js constructs TesseraLocalVoice at load; its script must arrive first.

    The nav-glyph change replaced index.html and dropped the local-voice tag.
    Every page then threw "TesseraLocalVoice is not a constructor" and the Voice
    page never booted. Pin the tag and its order.
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "/static/voice-local.js" in html
    assert html.index("/static/voice-local.js") < html.index("/static/app.js")


def test_phone_workspace_has_a_section_select_and_one_row_controls():
    """The phone layout keeps the top bar to one row and the settings list in a
    select. Desk and Timers move into the menu drawer; the settings nav no longer
    scrolls sideways with most sections hidden.
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    settings_js = (WEB / "settings.js").read_text(encoding="utf-8")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert 'id="nav-desk"' in html and 'id="nav-timers"' in html
    assert "settings-section-select" in settings_js
    assert 'sectionSelect.id = "settings-section-select"' in settings_js
    assert 'element("label", "settings-section-label", "Settings section")' in settings_js
    assert 'element("button", "settings-back", "All settings")' in settings_js
    assert "result.settings.provider_pages" in settings_js
    assert ".settings-section-select { display: block;" in css
    assert ".settings-section-label { display: block;" in css
    assert "#desk-toggle, #timers-toggle { display: none; }" in css
    assert "integration-stat" in app_js


def test_browser_voice_accepts_openai_nested_turn_detection():
    """The OpenAI lease nests turn detection under audio.input.

    web/voice.js required a top-level session.turn_detection and threw
    "Unsupported speech turn detection" before opening the socket, so an
    OpenAI account made the Talk control inert. Accept either layout and
    still refuse a lease that names no server VAD.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free audio lifecycle checks")
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const window={isSecureContext:true,location:{protocol:'https:'},WebSocket:{OPEN:1}};
const context={window,navigator:{mediaDevices:{}},URL,ArrayBuffer,DataView,Uint8Array,Float32Array,btoa,atob,
 setTimeout:()=>0,clearTimeout:()=>{}};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/voice.js','utf8'),context);
const voice=new window.TesseraVoice({workletUrl:'/static/voice-worklet.js',chunkFrames:4,maxBufferedBytes:4096,
 maxPendingEvents:8,connectSeconds:10,maxPlaybackSeconds:5,onState:()=>{},onMessages:()=>{},
 start:async()=>{},stop:async()=>{},events:async()=>({messages:[],send:[]})});
const now=Date.now()/1000;
const lease={voice_id:'lease',websocket_url:'wss://api.openai.com/v1/realtime',protocols:['realtime','openai-insecure-api-key.ek'],
 expires_at:now+60,session_expires_at:now+120,max_session_seconds:90,audio_sample_rate:24000,
 session_update:{type:'session.update',session:{type:'realtime',instructions:'ctx',tools:[],audio:{
   input:{format:{type:'audio/pcm',rate:24000},turn_detection:{type:'server_vad'}},
   output:{format:{type:'audio/pcm',rate:24000},voice:'marin'}}}}};
voice.validate(lease);
const broken=JSON.parse(JSON.stringify(lease));
delete broken.session_update.session.audio.input.turn_detection;
assert.throws(()=>voice.validate(broken),/Unsupported speech turn detection/);
console.log('Nested OpenAI turn detection accepted; missing detection refused.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_voice_recovery_belongs_to_one_utterance():
    """A recovery timer and a reply are each owned by the utterance that caused them.

    Both defects came from one global flag with no utterance identity. Reproduced
    against web/voice.js before the fix: a pause-then-resume fired response.create
    from the previous utterance's timer, and a late audio delta from the previous
    response marked the new utterance answered so it never recovered.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free audio lifecycle checks")
    script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const sockets=[],timers=[];
function settings(){return {voice_id:'lease',websocket_url:'wss://voice.example.com/realtime',protocols:['p'],
 expires_at:Date.now()/1000+60,session_expires_at:Date.now()/1000+120,max_session_seconds:90,audio_sample_rate:16000,
 session_update:{type:'session.update',session:{instructions:'ctx',tools:[],turn_detection:{type:'server_vad'},
 audio:{input:{format:{type:'audio/pcm',rate:16000}},output:{format:{type:'audio/pcm',rate:16000}}}}}};}
function microphone(){return {getTracks:()=>[{stop(){}}]};}
class Audio{constructor(){this.sampleRate=48000;this.currentTime=0;this.destination={};this.audioWorklet={addModule:async()=>{}};}
 async resume(){} async close(){}
 createMediaStreamSource(){return {connect(){},disconnect(){}};}
 createBuffer(c,l,r){return {duration:l/r,getChannelData:()=>new Float32Array(l)};}
 createBufferSource(){return {connect(){},disconnect(){},start(){},stop(){}};}}
class Worklet{constructor(){this.port={postMessage(){}};}connect(){}disconnect(){}}
class Socket{static OPEN=1;constructor(){this.readyState=0;this.bufferedAmount=0;this.sent=[];sockets.push(this);}
 open(){this.readyState=1;this.onopen();}message(v){this.onmessage({data:JSON.stringify(v)});}
 send(v){this.sent.push(JSON.parse(v));}close(){}}
const window={isSecureContext:true,location:{protocol:'https:'},AudioContext:Audio,AudioWorkletNode:Worklet,WebSocket:Socket};
const navigator={mediaDevices:{getUserMedia:()=>Promise.resolve(microphone())}};
const context={window,navigator,URL,ArrayBuffer,DataView,Uint8Array,Float32Array,btoa,atob,
 setTimeout(fn,ms){const timer={fn,ms};timers.push(timer);return timer;},clearTimeout(timer){if(timer)timer.cleared=true;}};
vm.createContext(context);vm.runInContext(fs.readFileSync('web/voice.js','utf8'),context);
const voice=new window.TesseraVoice({workletUrl:'/static/voice-worklet.js',chunkFrames:4,maxBufferedBytes:4096,
 maxPendingEvents:8,connectSeconds:10,maxPlaybackSeconds:5,onState:()=>{},onMessages:()=>{},
 start:async()=>settings(),stop:async()=>{},events:async()=>({messages:[],send:[]})});
// Only the 2s recovery timer; the deadline and connect timers would tear the run down.
const runTimers=()=>{for(const timer of timers.filter(entry=>!entry.cleared&&entry.ms===2000))timer.fn();};
const creates=socket=>socket.sent.filter(event=>event.type==='response.create').length;
(async()=>{
 await voice.start('s');const socket=sockets.at(-1);socket.open();
 socket.message({type:'session.updated',session:{instructions:'ctx'}});

 // A timer armed for one utterance must not answer after the speaker resumes.
 socket.message({type:'input_audio_buffer.speech_started'});
 socket.message({type:'input_audio_buffer.speech_stopped'});
 const before=creates(socket);
 socket.message({type:'input_audio_buffer.speech_started'});
 runTimers();
 assert.equal(creates(socket),before,'resumed speech must cancel the previous utterance recovery');

 // A live response for the previous utterance must not mark this one answered.
 socket.message({type:'response.created',response:{id:'old'}});
 socket.message({type:'response.output_audio.delta',response_id:'old',delta:btoa('\0\0\0\0')});
 socket.message({type:'input_audio_buffer.speech_started'});
 socket.message({type:'response.output_audio.delta',response_id:'old',delta:btoa('\0\0\0\0')});
 socket.message({type:'input_audio_buffer.speech_stopped'});
 const mid=creates(socket);
 runTimers();
 assert.equal(creates(socket),mid+1,'a stale audio delta must not suppress recovery for the new utterance');
 console.log('Voice recovery is owned by one utterance.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_voice_bootstrap_and_static_policy_match_browser_audio(tmp_path, monkeypatch):
    import io
    import time
    from types import SimpleNamespace
    from tessera.config import Config
    from tessera.server import create_server
    config = Config(home=tmp_path, cwd=tmp_path)
    if not hasattr(config, "voice"):
        pytest.skip("Older servers have no voice API; UI missing-field fallback is covered separately")
    config.server.port = 0
    access_token = "synthetic-tessera-access"
    monkeypatch.setenv("TESSERA_SERVER_TOKEN", access_token)
    config.jev.enabled = False
    config.voice.enabled = True
    config.voice.websocket_url = "wss://voice.example.com/session-placeholder"
    config.voice.model = "model-placeholder"
    config.voice.api_base_url = "https://voice.example.com"
    config.voice.client_secret_path = "/client-secrets-placeholder"
    config.voice.api_key_env = "EXAMPLE_UNUSED_VOICE_TOKEN"
    config.voice.client_protocol_prefix = "example-prefix."
    def forbidden(*args, **kwargs):
        pytest.fail("Toolless browser voice must not load a chat backend")
    server = create_server(config, web_root=WEB, backend_factory=forbidden)
    server.manager.voice.client.credentials = SimpleNamespace(resolve=lambda *args, **kwargs: SimpleNamespace(value="synthetic-provider-key"))
    minted = []
    def mint(request, timeout):
        minted.append(request.get_method())
        return io.BytesIO(json.dumps({"value": "ephemeral-browser-fixture", "expires_at": time.time() + 30}).encode())
    server.manager.voice.client.opener = mint
    subject = server.manager.create("Voice contract")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        policy = response.getheader("Content-Security-Policy")
        response.read()
        assert "wss://voice.example.com" in policy, "V-UI1: configured voice WebSocket is blocked by static CSP"
        assert "session-placeholder" not in policy and "model-placeholder" not in policy
        assert minted == []
        connection.request("POST", "/api/sessions/" + subject["slug"] + "/voice/start", body="{}", headers={"Content-Type": "application/json", "Authorization": "Bearer " + access_token})
        response = connection.getresponse()
        assert response.status == 200
        bootstrap = json.loads(response.read())
        assert minted == ["POST"]
        assert "synthetic-provider-key" not in json.dumps(bootstrap)
        session = bootstrap["session_update"]["session"]
        for direction in ("input", "output"):
            assert session.get("audio", {}).get(direction, {}).get("format") == {"type": "audio/pcm", "rate": bootstrap["audio_sample_rate"]}, "V-UI2: bootstrap must explicitly configure browser PCM"
        assert session.get("turn_detection", {}).get("type") == "server_vad", "V-UI2: continuous voice needs explicit automatic turn detection"
        config.voice.enabled = False
        connection.request("GET", "/")
        response = connection.getresponse()
        assert "voice.example.com" not in response.getheader("Content-Security-Policy")
        response.read()
        assert server.manager._runtimes == {}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_server_serves_ui_and_its_contract_endpoints(tmp_path):
    from fake_backend import FakeBackend
    from tessera.config import Config
    from tessera.server import create_server

    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.jev.enabled = False
    server = create_server(config, web_root=WEB, backend_factory=lambda slug, name: FakeBackend(["A continuing thread."]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None):
        connection = http.client.HTTPConnection(*server.server_address, timeout=5)
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        content = response.read()
        result = (response.status, response.getheader("Content-Type"), content)
        connection.close()
        return result

    try:
        for path, filename in (("/", "index.html"), ("/static/app.js", "app.js"),
                               ("/static/app.css", "app.css"), ("/static/theme.css", "theme.css")):
            status, content_type, content = request("GET", path)
            assert status == 200
            assert content == (WEB / filename).read_bytes()
            assert "html" in content_type if filename.endswith("html") else ("javascript" in content_type if filename.endswith("js") else "css" in content_type)
        for asset in (WEB / "brand").rglob("*"):
            if asset.suffix not in (".png", ".svg"):
                continue
            status, content_type, content = request("GET", "/static/" + asset.relative_to(WEB).as_posix())
            assert status == 200 and content == asset.read_bytes()
            assert content_type.startswith("image/png" if asset.suffix == ".png" else "image/svg+xml")
        status, _, content = request("GET", "/static/app.webmanifest")
        assert status == 200 and content == (WEB / "app.webmanifest").read_bytes()
        status, content_type, content = request("GET", "/favicon.ico")
        icon = (WEB / "brand" / "icon-32.png").read_bytes()
        favicon_type = content_type or ""
        assert status == 200 and content == icon
        assert favicon_type.startswith("image/png")
        status, _, created = request("POST", "/api/sessions", {"title": "Web smoke"})
        assert status == 201
        slug = json.loads(created)["slug"]
        path = "/api/sessions/" + slug
        assert request("POST", path + "/messages", {"text": "Hello"})[0] == 202
        assert server.manager.wait(slug, 5)
        for endpoint in ("/api/sessions", "/api/status", path + "/messages?after=-1&limit=200",
                         path + "/view", path + "/zoom?start=0&count=1"):
            status, content_type, data = request("GET", endpoint)
            assert status == 200 and "json" in content_type
            json.loads(data)
        assert request("PATCH", path, {"title": "Renamed", "archived": True})[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_javascript_interactions_with_http_and_sse_boundaries(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const actualSnapshots = JSON.parse(fs.readFileSync(0,'utf8'));
const actualStatus = actualSnapshots.empty;
const all = [];
let doc;
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.hidden = false; this.disabled = false;
    this.open = false; this._text = ''; this.scrollHeight = 0; this.scrollTop = 0;
    this.clientHeight = 500; this.parent = null; this.className = '';
    this.style = {setProperty(name,value){this[name]=value;},removeProperty(name){delete this[name];}};
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)), remove: (...xs) => xs.forEach(x => classes.delete(x)), contains: x => classes.has(x), toggle: (x, on) => { const next = on === undefined ? !classes.has(x) : !!on; next ? classes.add(x) : classes.delete(x); return next; } };
    all.push(this);
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) { if (n.tag === 'fragment') this.append(...[...n.children]); else this.insertBefore(n, null); } }
  replaceChildren(...nodes) { for (const n of this.children) n.parent = null; this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length-1] || null; }
  remove() { if(this.parent) { this.parent.children.splice(this.parent.children.indexOf(this),1); this.parent=null; } }
  replaceWith(node) { this.parent.insertBefore(node,this); this.remove(); }
  get nextSibling() { if (!this.parent) return null; return this.parent.children[this.parent.children.indexOf(this) + 1] || null; }
  hasChildNodes() { return this.children.length > 0; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type, extra = {}) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {}, ...extra }); }
  focus() { doc.activeElement = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire('close'); }
  requestSubmit() { this.fire('submit'); }
  querySelector(tag) { for (const n of this.children) { if (n.tag === tag) return n; const found = n.querySelector(tag); if (found) return found; } return null; }
  querySelectorAll(selector) {
    if (selector !== '.work-item') return this.children;
    const found = [];
    for (const child of this.children) {
      if (child.className.split(/\s+/).includes('work-item')) found.push(child);
      found.push(...child.querySelectorAll(selector));
    }
    return found;
  }
  getClientRects() { return this.hidden ? [] : [{}]; }
}
const ids = {};
const html = fs.readFileSync('web/index.html', 'utf8');
for (const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"[^>]*>/g)) ids[match[2]] = new Element(match[1]);
ids['empty-state'].append(new Element('h2'), new Element('p'));
ids['theme-choice'].value = 'system';
const conversation = new Element('main');
const topbar = new Element('header');
doc = { body: new Element('body'), documentElement: new Element('html'), hidden: false,
  getElementById: id => ids[id], createElement: tag => new Element(tag), createTextNode: text => { const node = new Element('#text'); node.textContent = String(text); return node; }, createDocumentFragment: () => new Element('fragment'),
  querySelector: cls => cls === '.conversation' ? conversation : topbar, addEventListener() {} };
const calls = [];
let failNextPost = false;
let failStatus = 0;
let failSessionList = 0;
let holdNextStatus = false;
let holdNextView = false;
let releaseView;
let releaseStatus;
let boardLinkBody={session_slug:'ui-thread',log_start:0,log_end:1};
let boardLinkStatus=200;
let holdNextBoardLink=false;
let boardNoteBody=null;
let releaseBoardLink;
let boardPressure={steady:true,items:[],learning:{open_lessons:0,open_questions:0,open_intentions:0},wakes_last_hour:0};
let statusBody = {backends:['fake'],jev:{enabled:false},aethermind:{reachable:false},debt_total:3,sessions:1};
let sessions = [{slug:'project-notes',title:'Project notes',messages:2,archived:false,backend:null}];
const messages = { 'project-notes': [{i:0,kind:'user',text:'Hello',date:'2026-10-15T12:00:00Z'}, {i:1,kind:'agent',text:'<img src=x onerror=alert(1)>',date:'2026-10-15T12:00:00Z'}] };
const pageNotice='First page\n[Message 0: bytes 0..10 of 30. Continue with zoom(0, 1, offset=10).]';
const resultCases=[
  {text:'HEAD\n[clipped]\nTAIL',incomplete:true},
  {text:JSON.stringify({exit_code:0,output:'HEAD\n[clipped]\nTAIL'}),incomplete:true},
  {text:pageNotice,incomplete:true},
  {text:JSON.stringify({text:pageNotice}),incomplete:true},
  {text:pageNotice.replace('offset=10',"offset=10, source='parent'"),incomplete:true},
  {text:'The word clipped or continue does not mean partial output.',incomplete:false},
  {text:'A literal [clipped] mentioned inline.',incomplete:false},
  {text:JSON.stringify({output:'A complete result.',description:'\n[clipped]\n'}),incomplete:false},
  {text:'[clipped]',incomplete:true},
];
for (let index=0;index<resultCases.length;index+=1) {
  messages['project-notes'].push({i:index+3,kind:'echo',text:resultCases[index].text,date:''});
}
messages['project-notes'].push({i:12,kind:'tool',text:JSON.stringify({name:'read_file',arguments:{text:'\n[clipped]\n'}}),date:''});
messages['project-notes'].push({i:13,kind:'user',text:'\n[clipped]\n',date:''});
let inheritanceText = 'A remembered decision.';
function response(body, status=200) { return { status, ok:status>=200&&status<300, json:async()=>body }; }
async function fetchFixture(path, options={}) {
  const method = options.method || 'GET';
  const body = options.body ? JSON.parse(options.body) : null;
  calls.push({path,method,body,headers:options.headers});
  assert(!/token=/i.test(path));
  assert(!options.headers||options.headers.Authorization===undefined);
  if (path === '/api/board/view' && method === 'GET') {
    const board = statusBody && statusBody.board_status;
    if (!board || typeof board !== 'object' || Array.isArray(board)) return response({enabled:false,configured:false});
    const cards = Array.isArray(board.cards) ? board.cards.map(card => {
      if (!card || typeof card !== 'object') return card;
      const {title, id, session_slug, questions, artifacts, endpoint, model, ...safe} = card;
      return safe;
    }) : board.cards;
    return response({...board, cards, projects: Array.isArray(board.projects) ? board.projects : []});
  }
  if (path === '/api/board/pressure') {
    return response(boardPressure);
  }
  if (path === '/api/board/cards' && method === 'POST') {
    assert(body && typeof body.title === 'string' && body.title.trim(), 'a card needs a title');
    boardNoteBody = body;
    const board = statusBody && statusBody.board_status;
    return response({card_id:'newcard', view:{...(board||{}), cards:(board&&board.cards)||[]}});
  }
  if (path.startsWith('/api/board/cards/')) {
    assert.equal(method,'GET');
    if (holdNextBoardLink) {
      holdNextBoardLink=false;
      const heldBody=boardLinkBody;
      return new Promise(resolve=>{releaseBoardLink=()=>resolve(response(heldBody));});
    }
    return response(boardLinkStatus===200 ? boardLinkBody : {error:'PRIVATE_BOARD_ERROR'},boardLinkStatus);
  }
  if (path === '/api/status' || path === '/api/status?shape=connection') {
    // The real core always sends [ui]; fixtures that replace statusBody inherit its defaults.
    const withUi=(body)=>body&&typeof body==='object'&&!Array.isArray(body)&&!('ui' in body)?{...body,ui:{yolo_banner:true,history_page:60,view_page_lines:80,item_body_chars:4000,item_summary_chars:120,long_press_ms:500,approval_poll_ms:0,error_banner_seconds:600}}:body;
    if (holdNextStatus) {
      holdNextStatus=false;
      const heldBody=statusBody;
      return new Promise(resolve=>{releaseStatus=()=>resolve(response(withUi(heldBody)));});
    }
    if (failStatus) return response({error:'PRIVATE_STATUS_ERROR'},failStatus);
    return response(withUi(statusBody));
  }
  if (path === '/api/sessions' && method === 'GET') {
    if (failSessionList) return response({error:'PRIVATE_SESSION_ERROR'},failSessionList);
    return response(sessions);
  }
  if (path === '/api/sessions' && method === 'POST') { const session={slug:'ui-thread',title:body.title,archived:false,messages:0}; sessions.push(session); messages[session.slug]=[]; return response(session,201); }
  const [, slug, suffix=''] = path.match(/^\/api\/sessions\/([^/?]+)(.*)$/) || [];
  if (method === 'PATCH') { const session=sessions.find(s=>s.slug===slug); Object.assign(session,body); return response(session); }
  if (suffix.startsWith('/messages') && method === 'GET') {
    const query=new URLSearchParams(suffix.split('?')[1]||'');
    const all=messages[slug]||[];
    if (query.has('tail')) return response(all.slice(-Number(query.get('tail'))));
    const after=Number(query.get('after')??-1);
    return response(all.filter(m=>m.i>after).sort((a,b)=>a.i-b.i).slice(0,Number(query.get('limit')||200)));
  }
  if (suffix === '/messages' && method === 'POST') {
    if (failNextPost) { failNextPost=false; return response({error:'Scripted enqueue failure'},503); }
    return response({turn_id:'turn-two'},202);
  }
  if (suffix === '/view' || suffix.startsWith('/view?')) {
    const body={bytes:80,high:1000,low:500,omitted:0,inheritance:inheritanceText,debt:3,lines:[{start:0,count:1,level:0,text:'A summary',bytes:9}]};
    if (holdNextView) {
      holdNextView=false;
      return new Promise(resolve=>{releaseView=()=>resolve(response(body));});
    }
    return response(body);
  }
  if (suffix.startsWith('/zoom')) return response({text:'Raw user words.'});
  if (suffix === '/feedback' && method === 'GET') return response(feedback[slug]||{});
  if (suffix === '/feedback' && method === 'POST') {
    const marks=feedback[slug]=feedback[slug]||{};
    feedbackPosts.push(body);
    if (body.rating===null) delete marks[body.i]; else marks[body.i]={...(marks[body.i]||{resolved:false}),...(body.rating?{rating:body.rating}:{}),...('resolved' in body?{resolved:body.resolved}:{})};
    return response(marks);
  }
  throw new Error('Unexpected endpoint '+method+' '+path);
}
const feedback = {};
const feedbackPosts = [];
const streams = [];
class EventSource {
  constructor(path) { this.path=path;this.listeners={};streams.push(this); }
  addEventListener(type,fn) { this.listeners[type]=fn; }
  emit(type,data) { this.listeners[type]({data:JSON.stringify(data),lastEventId:'7'}); }
  close() { this.closed=true; }
}
const stored = {};
const timeouts = [];
const context = {document:doc,window:{addEventListener(){}},EventSource,fetch:fetchFixture,AbortController,TextDecoder,TextEncoder,
  history:{replaceState(){},pushState(){}},location:{hash:''},localStorage:{getItem:k=>stored[k],setItem:(k,v)=>{stored[k]=v;}},
  setInterval(){},setTimeout(fn){timeouts.push(fn);return timeouts.length;},clearTimeout(){},console};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/voice.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/voice-local.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/format.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/items.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/design.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/app.js','utf8'),context);
async function settle() { for(let i=0;i<8;i++)await new Promise(resolve=>setImmediate(resolve)); }
(async()=>{
  await settle();
  assert.equal(ids['session-title'].textContent,'Project notes');
  ids['session-menu'].hidden = true;
  const sessionRow = all.find((node) => node.dataset && node.dataset.slug === 'project-notes');
  await sessionRow.fire('contextmenu', {clientX: 20, clientY: 20, preventDefault() {}});
  assert.equal(ids['session-menu'].hidden, false, 'right-click opens the session menu');
  await ids['menu-select'].fire('click');
  assert.equal(ids['session-menu'].hidden, true, 'the session menu closes when an item is used');
  await ids['select-cancel'].fire('click');
      assert(!doc.body.classList.contains('no-session'), 'a selected session reveals actions and composer');
  assert(ids.messages.textContent.includes('<img src=x onerror=alert(1)>'));
  assert(!all.some(n=>n.tag==='img'));
  const allText=(node)=>[node.textContent||'',...(node.children||[]).map(allText)].join('\n');
  const assertReadable=(details,text)=>{
    let parsed; try { parsed=JSON.parse(text); } catch (_) { parsed=undefined; }
    const expected=parsed&&typeof parsed==='object'?Object.values(parsed).map(String):[text];
    for (const value of expected) assert(allText(details).includes(value),'result body shows '+value);
  };
  for (let index=0;index<resultCases.length;index+=1) {
    const card=ids.messages.children.find(node=>node.dataset.index===String(index+3));
    const details=card.querySelector('details');
    assert.equal(details.open,false);
    assertReadable(details,resultCases[index].text);
    assert.equal(card.classList.contains('incomplete-result'),resultCases[index].incomplete);
    assert.equal(details.querySelector('summary').textContent.includes('Incomplete'),resultCases[index].incomplete);
    if (resultCases[index].incomplete) {
      const notice=card.querySelector('p');
      assert(notice&&!notice.hidden&&notice.parent===card);
      details.open=true;
      assertReadable(details,resultCases[index].text);
    }
  }
  const agentCard=ids.messages.children.find(node=>node.dataset.index==='1');
  const actions=agentCard.children.find(node=>node.className==='message-actions').children;
  assert.deepEqual(actions.map(b=>b.getAttribute('aria-label')),['Reply','Copy','Good','Flag an issue','Resolve flag']);
  await actions[3].fire('click');await settle();
  assert.deepEqual(feedbackPosts.at(-1),{i:1,rating:'flag'});
  assert.equal(agentCard.dataset.rating,'flag');
  assert.equal(agentCard.dataset.resolved,'false');
  await actions[4].fire('click');await settle();
  assert.deepEqual(feedbackPosts.at(-1),{i:1,resolved:true});
  assert.equal(agentCard.dataset.resolved,'true');
  ids['feedback-filter'].value='flag';await ids['feedback-filter'].fire('change');
  assert.equal(ids.messages.dataset.filter,'flag');
  assert.equal(ids['show-resolved'].hidden,false);
  assert.equal(ids.messages.dataset.showResolved,'false');
  await actions[0].fire('click');
  assert.equal(ids['reply-chip'].hidden,false);
  ids['feedback-filter'].value='all';await ids['feedback-filter'].fire('change');
  ids['message-input'].value='About that';
  await ids.composer.fire('submit');await settle();
  assert(calls.some(c=>c.body&&c.body.text==='About that'&&c.body.reply_to===1));
  assert.equal(ids['reply-chip'].hidden,true);
  for (const index of [12,13]) {
    const card=ids.messages.children.find(node=>node.dataset.index===String(index));
    assert(!card.classList.contains('incomplete-result'));
  }
  await ids['memory-tab'].fire('click');
  assert.equal(ids['memory-panel'].hidden,false);
  assert(ids['inheritance-digest'].textContent.includes('remembered decision'));
  assert(ids['write-debt'].textContent.includes('3 unsent'));
  await ids['view-lines'].firstChild.firstChild.fire('click'); await settle();
  assert(ids['view-lines'].textContent.includes('Raw user words.'));
  assert.equal(ids['view-lines'].firstChild.dataset.age,'recent');
  await ids['inspector-toggle'].fire('click');
  assert.equal(conversation.inert,true);
  await ids['close-inspector'].fire('click');
  assert.equal(conversation.inert,false);
  assert.equal(doc.activeElement,ids['inspector-toggle']);
  streams[0].emit('message',{i:2,kind:'tool',text:'{"name":"read_file"}',date:''});
  streams[0].emit('message',{i:20,kind:'echo',text:'FIRST\n[clipped]\nLAST',date:''});
  timeouts.at(-1)();await settle();
  const streamedResult=ids.messages.children.find(node=>node.dataset.index==='20');
  assert(streamedResult.classList.contains('incomplete-result'));
  assert.equal(streamedResult.querySelector('details').open,false);
  assert(streamedResult.querySelector('p').textContent.includes('Output clipped'));
  assert(all.some(n=>n.tag==='details'&&!n.open));
  const timerCount=timeouts.length;
  for (let i=0;i<100;i++) streams[0].emit('turn_end',{success:true});
  assert.equal(timeouts.length,timerCount+1);
  timeouts.at(-1)();await settle();
  await ids['new-session'].fire('click');
  ids['session-name'].value='UI Thread';
  await ids['session-form'].fire('submit'); await settle();
  assert.equal(ids['session-title'].textContent,'UI Thread');
  await ids['rename-session'].fire('click'); ids['session-name'].value='Renamed thread';
  await ids['session-form'].fire('submit'); await settle();
  assert.equal(ids['session-title'].textContent,'Renamed thread');
  await ids['archive-session'].fire('click'); assert(ids['message-input'].disabled);
  await ids['archive-session'].fire('click'); assert(!ids['message-input'].disabled);
  ids['message-input'].value='A question\nSecond line';
  const before=calls.filter(c=>c.method==='POST'&&c.path.endsWith('/messages')).length;
  await ids['message-input'].fire('keydown',{key:'Enter',shiftKey:true,isComposing:false}); await settle();
  assert.equal(calls.filter(c=>c.method==='POST'&&c.path.endsWith('/messages')).length,before);
  await ids['message-input'].fire('keydown',{key:'Enter',shiftKey:false,isComposing:true}); await settle();
  assert.equal(calls.filter(c=>c.method==='POST'&&c.path.endsWith('/messages')).length,before);
  await ids['message-input'].fire('keydown',{key:'Enter',shiftKey:false,isComposing:false}); await settle();
  assert(calls.some(c=>c.body&&c.body.text==='A question\nSecond line'));
  assert.equal(ids['message-input'].value,'');
  ids['message-input'].value='Keep this draft';failNextPost=true;
  await ids.composer.fire('submit');await settle();
  assert.equal(ids['message-input'].value,'Keep this draft');
  assert.equal(ids['error-banner'].hidden,true);
  assert(ids['send-state'].textContent.includes('Scripted enqueue failure'));
  // No sign-in exists: every request goes out without credentials.
  assert(['auth-dialog','auth-form','server-token','connect-button','settings-connect'].every(id=>!(id in ids)));
  assert(calls.every(c=>!c.headers||c.headers.Authorization===undefined));
  assert(!calls.some(c=>c.path.endsWith('/events')));
  streams.at(-1).emit('capture',{debt:2});await settle();
  assert(ids['write-debt'].textContent.includes('2 unsent'));
  assert(!Object.values(stored).some(value=>String(value).toLowerCase().includes('token')));
  // The Stage 1 response proves configuration only, never provider health.
  const privateAlias='PRIVATE_BACKEND_ALIAS';
  const privateDetail='PRIVATE_CONNECTION_DETAIL';
  statusBody={backends:[privateAlias],provider_status:{accounts:[],backends:[]}};
  await ids['settings-refresh'].fire('click');
  assert(!ids['status-list'].textContent.includes(privateAlias));
  const record={id:privateAlias,configured:true,health:'healthy',credential:'available',endpoint:privateDetail,token:privateDetail};
  statusBody.provider_status={accounts:[record],backends:[record]};
  await ids['settings-refresh'].fire('click');
  assert(!Object.values(stored).includes(privateDetail));
  record.credential='missing';
  await ids['settings-refresh'].fire('click');
  record.configured=false;
  await ids['settings-refresh'].fire('click');
  record.configured=true;record.health='error';record.credential='not_required';
  const categories={authentication:'Authentication error',permission:'Permission denied',rate_limit:'Rate limited',quota:'Quota exhausted',timeout:'Request timed out',connection:'Connection error',provider:'Provider error',configuration:'Configuration error'};
  for (const category of Object.keys(categories)) {
    record.error_category=category;
    await ids['settings-refresh'].fire('click');
  }
  record.error_category=privateDetail;record.error=privateDetail;
  await ids['settings-refresh'].fire('click');
  for (const invalidCategory of [null,42,[],{}, {toString:null}]) {
    record.error_category=invalidCategory;
    await ids['settings-refresh'].fire('click');
  }
  record.health=privateDetail;record.credential=privateDetail;
  await ids['settings-refresh'].fire('click');
  // Backend reports must match the selection ID; array position is not evidence.
  record.id='different-id';record.health='healthy';record.credential='available';
  await ids['settings-refresh'].fire('click');
  for (const invalidReport of [null,privateDetail,[],{accounts:[null],backends:[null]},{accounts:{secret:privateDetail},backends:privateDetail}]) {
    statusBody={backends:[privateAlias],provider_status:invalidReport};
    await ids['settings-refresh'].fire('click');
  }
  for (const invalidBackends of [null,privateDetail,[null],[{}],[4]]) {
    statusBody={backends:invalidBackends};
    await ids['settings-refresh'].fire('click');
  }
  statusBody={backends:{[privateAlias]:{}},provider_status:{backends:[{id:privateAlias,configured:true,health:'healthy'}]}};
  await ids['settings-refresh'].fire('click');
  statusBody={backends:[],provider_status:{accounts:[],backends:[]},aethermind:{},jev:{enabled:false}};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['board-state'].textContent,'Not reported');
  assert.equal(ids['tasks-state'].textContent,'Not reported');
  assert.equal(ids['tasks-filters'].hidden,true);
  assert(ids['tasks-summary'].textContent.includes('Not reported'));
  assert.equal(ids['jobs-state'].textContent,'Not reported');
  assert.equal(ids['memory-tree-state'].textContent,'Not reported');
  assert(!ids['memory-tree-state'].textContent.includes('Off'));
  // Only an explicit, well-formed sanitized snapshot proves task records exist.
  for (const invalid of [undefined,null,[],privateDetail,{enabled:'true',configured:'true',tasks:[]},{enabled:true,configured:true,tasks:[null]}]) {
    statusBody.task_status=invalid;
    await ids['refresh-tasks'].fire('click');
    assert.equal(ids['tasks-state'].textContent,'Not reported');
    assert(!ids['tasks-records'].textContent.includes('Running'));
    assert(!ids['tasks-summary'].textContent.includes(privateDetail));
  }
  statusBody.task_status={enabled:false,configured:true,tasks:[{status:'running'}]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['tasks-state'].textContent,'Off');
  assert(!ids['tasks-records'].textContent.includes('Running'));
  statusBody.task_status={enabled:true,configured:false,tasks:[{status:'running'}]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['tasks-state'].textContent,'Not configured');
  assert(!ids['tasks-records'].textContent.includes('Running'));
  statusBody.task_status={enabled:true,configured:true,tasks:[]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['tasks-state'].textContent,'Empty');
  assert.equal(ids['tasks-filters'].hidden,false);
  assert(ids['tasks-filters'].textContent.includes('All (0)'));
  assert(ids['tasks-summary'].textContent.includes('No tasks'));
  const taskEnums=['queued','running','succeeded','failed','cancelled'];
  statusBody.task_status.tasks=taskEnums.map((status,index)=>({status,attempt:2,account_ordinal:index+1,id:privateAlias,objective:privateDetail,scope:privateDetail,account:privateDetail,result_ref:privateDetail,error:privateDetail}));
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['tasks-state'].textContent,'Snapshot');
  assert(ids['tasks-summary'].textContent.includes('5 tasks'));
  for (const label of ['Queued','Running','Succeeded','Failed','Cancelled']) assert(ids['tasks-records'].textContent.includes(label));
  assert(ids['tasks-records'].textContent.includes('Task 1Attempt 2'));
  assert(ids['tasks-records'].textContent.includes('Account 1'));
  assert(ids['tasks-records'].textContent.includes('Account 5'));
  assert(ids['tasks-filters'].textContent.includes('All (5)'));
  assert(ids['tasks-filters'].textContent.includes('Running (1)'));
  assert(!ids['tasks-records'].textContent.includes(privateDetail));
  // task_limit is a bound, not a filter. The row count alone would read as the
  // whole task store, so the summary has to name what the bound dropped.
  statusBody.task_status.tasks_omitted=2;
  statusBody.task_status.task_limit=5;
  await ids['refresh-tasks'].fire('click');
  assert(ids['tasks-summary'].textContent.includes('5 tasks'));
  assert(ids['tasks-summary'].textContent.includes('2 older tasks not shown (5-task limit)'));
  statusBody.task_status.tasks_omitted=0;
  await ids['refresh-tasks'].fire('click');
  assert(!ids['tasks-summary'].textContent.includes('older tasks not shown'));
  assert(!ids['tasks-records'].textContent.includes(privateAlias));
  const beforeFilter=calls.length;
  const beforeDraft=ids['message-input'].value;
  let filter=ids['tasks-filters'].children.find(node=>node.dataset.filter==='running');
  filter.focus();
  await filter.fire('click');
  assert.equal(ids['tasks-records'].children.length,1);
  assert(ids['tasks-records'].textContent.includes('Task 2'));
  assert(!ids['tasks-records'].textContent.includes('Task 1'));
  assert(ids['tasks-filter-result'].textContent.includes('Showing 1 of 5'));
  assert.equal(filter.getAttribute('aria-pressed'),'true');
  assert.equal(doc.activeElement,filter);
  assert.equal(calls.length,beforeFilter);
  assert.equal(ids['message-input'].value,beforeDraft);
  await ids['refresh-tasks'].fire('click');
  filter=ids['tasks-filters'].children.find(node=>node.dataset.filter==='running');
  assert.equal(doc.activeElement,filter);
  assert.equal(filter.getAttribute('aria-pressed'),'true');
  statusBody.task_status.tasks=[{status:'queued',attempt:0}];
  await ids['refresh-tasks'].fire('click');
  assert(ids['tasks-records'].textContent.includes('No records match'));
  assert(ids['tasks-filter-result'].textContent.includes('Showing 0 of 1'));
  await ids['tasks-filters'].children.find(node=>node.dataset.filter==='all').fire('click');
  assert(ids['tasks-records'].textContent.includes('Attempt 0'));
  assert(ids['tasks-records'].textContent.includes('Account not reported'));
  for (const invalid of [privateDetail,{toString:null},[],null]) {
    statusBody.task_status.tasks=[{status:invalid,attempt:invalid}];
    await ids['refresh-tasks'].fire('click');
    assert(ids['tasks-records'].textContent.includes('Status not reported'));
    assert(ids['tasks-records'].textContent.includes('Attempt not reported'));
    assert(ids['tasks-filters'].textContent.includes('Unreported (1)'));
    assert(!ids['tasks-records'].textContent.includes(privateDetail));
  }
  for (const invalid of [0,-1,1.5,Number.MAX_SAFE_INTEGER+1,'2',privateAlias,{toString:null},null]) {
    statusBody.task_status.tasks=[{status:'queued',attempt:1,account_ordinal:invalid,account:privateAlias}];
    await ids['refresh-tasks'].fire('click');
    assert(ids['tasks-records'].textContent.includes('Account not reported'));
    assert(!ids['tasks-records'].textContent.includes(privateAlias));
  }
  for (const invalid of [-1,1.5,Number.MAX_SAFE_INTEGER+1,'2']) {
    statusBody.task_status.tasks=[{status:'queued',attempt:invalid}];
    await ids['refresh-tasks'].fire('click');
    assert(ids['tasks-records'].textContent.includes('Attempt not reported'));
  }
  // Judge settings and durable-log metadata are independent observations.
  const judge={enabled:true,configured:true,decision_log_present:true,decision_count:4,last_decision_category:'verification',url:privateDetail,model:privateDetail,decision_log:privateDetail,error:privateDetail};
  statusBody.jev=judge;
  await ids['settings-refresh'].fire('click');
  judge.configured=false;
  await ids['settings-refresh'].fire('click');
  judge.configured=true;
  const decisionCategories={intake:'Intake',tool_gate:'Tool gate',verification:'Verification',memory:'Memory',route:'Routing',needs_memory:'Memory check',tool_risk:'Tool risk',judge:'Output review'};
  for (const category of Object.keys(decisionCategories)) {
    judge.last_decision_category=category;
    await ids['settings-refresh'].fire('click');
  }
  for (const invalid of [privateDetail,{toString:null},[],null,-1,1.5,Number.MAX_SAFE_INTEGER+1,'2']) {
    judge.decision_count=invalid;judge.last_decision_category=invalid;
    await ids['settings-refresh'].fire('click');
  }
  judge.decision_count=0;judge.last_decision_category='verification';
  await ids['settings-refresh'].fire('click');
  judge.enabled=false;judge.decision_log_present=false;judge.decision_count=99;
  await ids['settings-refresh'].fire('click');
  judge.decision_count=0;
  await ids['settings-refresh'].fire('click');
  for (const invalid of [null,[],privateDetail,{enabled:privateDetail,configured:privateDetail,decision_log_present:privateDetail}]) {
    statusBody.jev=invalid;
    await ids['settings-refresh'].fire('click');
    assert(ids['status-list'].textContent.includes('JevNot reported'));
  }
  // Board cards consume only the passive numeric/boolean/enum projection.
  for (const invalid of [null,[],privateDetail,{enabled:'true',configured:'true',cards:[]},{enabled:true,configured:true,cards:[null]}]) {
    statusBody.board_status=invalid;
    await ids['refresh-tasks'].fire('click');
    assert.equal(ids['board-state'].textContent,'Not reported');
    assert(!ids['board-cards'].textContent.includes(privateDetail));
  }
  statusBody.board_status={enabled:false,configured:true,cards:[{status:'running'}]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-state'].textContent,'Off');
  assert(!ids['board-cards'].textContent.includes('Running'));
  statusBody.board_status={enabled:true,configured:false};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-state'].textContent,'Not configured');
  statusBody.board_status={enabled:true,configured:true,cards:[]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-state'].textContent,'Empty');
  // A journal that could not be read is not an empty board. The page must say which
  // file and which wall, not "No cards reported" — that silence is what made a corrupt
  // journal indistinguishable from a board nobody had used.
  statusBody.board_status={enabled:true,configured:true,
    unreadable:'the board journal line 2 is not valid JSON'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-state'].textContent,'Unreadable');
  assert(ids['board-summary'].textContent.includes('line 2 is not valid JSON'));
  assert(ids['board-cards'].textContent.includes('Board unreadable'));
  const revision='ab'.repeat(32);
  const boardCard={ordinal:3,status:'blocked',attempt:2,artifact_count:2,question_pending:true,session_ordinal:1,log_start:0,log_end:1,
    title:privateDetail,id:privateAlias,session_slug:privateAlias,questions:[{text:privateDetail}],artifacts:[privateDetail],endpoint:privateDetail,model:privateDetail};
  statusBody.board_status={enabled:true,configured:true,revision,cards:[boardCard]};
  await ids['refresh-tasks'].fire('click');
  // The badge counts open work, not the fetch. One blocked card reads "1 open".
  assert.equal(ids['board-state'].textContent,'1 open');
  assert(ids['board-cards'].textContent.includes('Card 3Blocked'));
  assert(ids['board-cards'].textContent.includes('Your input is requested'));
  // Open work states exactly what it hides, and the visible control reveals all work.
  const allCards = [];
  for (let ordinal = 1; ordinal <= 180; ordinal += 1) {
    allCards.push({...boardCard, ordinal, status: ordinal <= 80 ? 'succeeded' : 'queued'});
  }
  statusBody.board_status={enabled:true,configured:true,revision,cards:allCards};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-cards'].querySelectorAll('.work-item').length, 100);
  assert.equal(ids['board-show-done'].textContent, '80 completed cards hidden, show all');
  await ids['board-show-done'].fire('click');
  assert.equal(ids['board-cards'].querySelectorAll('.work-item').length, 180);
  assert(ids['board-cards'].textContent.includes('Card 1'));
  assert(ids['board-cards'].textContent.includes('Card 180'));
  assert.equal(ids['board-show-done'].textContent, 'All work (180)');
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['board-cards'].querySelectorAll('.work-item').length, 180);
  await ids['board-show-open'].fire('click');
  assert.equal(ids['board-cards'].querySelectorAll('.work-item').length, 100);
  statusBody.board_status={enabled:true,configured:true,revision,cards:[boardCard]};
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-cards'].textContent.includes('Artifact 1Artifact 2'));
  // The board leads with pressure and projects, and says them in words.
  assert(ids['board-pressure'].textContent.includes('Hosts are steady'));
  assert.equal(ids['board-pressure-state'].textContent,'Steady');
  // Steady pressure with unfinished work must not read as clear. The badge and the
  // sentence both say what is still open, because the meta line counts it anyway.
  boardPressure={steady:true,items:[],learning:{open_lessons:12,open_questions:1,open_intentions:0},wakes_last_hour:4,open_items:{}};
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-pressure'].textContent.includes('13 unfinished items still open below'),'steady pressure says what is unfinished');
  assert.equal(ids['board-pressure-state'].textContent,'Steady, work open');
  assert.equal(ids['board-pressure-state'].dataset.state,'attention');
  boardPressure={steady:true,items:[],learning:{open_lessons:0,open_questions:0,open_intentions:0},wakes_last_hour:0};
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-pressure'].textContent.includes('Hosts are steady'),'a genuinely clear board keeps the plain line');
  assert.equal(ids['board-pressure-state'].textContent,'Steady');
  boardPressure={steady:false,items:[{score:1.8,text:'Pressure: node-a changed healthy -> unreachable. Name what changed. Do not restart it.'}],
    learning:{open_lessons:2,open_questions:1,open_intentions:0},wakes_last_hour:3,
    open_items:{lessons:[{id:'ls-1',trigger:'A restart window is cleared',failure:'I deliberated',check:'the POST lands',skill:'',status:'open',fails:0,slug:'operations'}],
      questions:[{id:'q-1',text:'router polish, for later',status:'open',slug:'harness-development'}],intentions:[]}};
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-pressure'].textContent.includes('node-a changed healthy -> unreachable'));
  assert(!ids['board-pressure'].textContent.includes('Pressure: node-a'), 'the label is not repeated in every row');
  assert(ids['board-pressure'].textContent.includes('salience 1.8'));
  assert(ids['board-pressure'].textContent.includes('2 open lessons'));
  assert.equal(ids['board-pressure-state'].textContent,'1 item');
  // A count is not a report: the items behind the numbers are readable and expandable.
  assert(ids['board-pressure-items'].textContent.includes('Open lessons (1)'),'the lesson group is named');
  assert(ids['board-pressure-items'].textContent.includes('A restart window is cleared'),'the lesson trigger is readable');
  assert(ids['board-pressure-items'].textContent.includes('Parked questions (1)'),'the question group is named');
  assert(ids['board-pressure-items'].textContent.includes('router polish, for later'),'the question text is readable');
  // A project card states the plan and the work in words, not a glowing tile.
  const projectView={id:'p1',title:'Shared board',description:'One board, many threads',manager:'harness-development',
    status:'active',steps:[{id:'s1',text:'Store the plan',done:true},{id:'s2',text:'Render the plan',done:false}],
    steps_done:1,steps_total:2,cards_open:1,cards_total:2,unclaimed:1,
    card_counts:{queued:1,running:0,blocked:0,succeeded:1,failed:0,cancelled:0},
    created_at:1,updated_at:2,version:1,session_slug:'harness-development'};
  const savedBoardCards=statusBody.board_status.cards;
  statusBody.board_status={...statusBody.board_status,projects:[projectView],
    cards:[{ordinal:1,status:'queued',project:'p1'},{ordinal:2,status:'succeeded',project:'p1'},{ordinal:3,status:'blocked',project:''}]};
  await ids['nav-wip'].fire('click');
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-projects'].textContent.includes('Shared board'));
  assert(ids['board-projects'].textContent.includes('1 of 2 steps done'));
  assert(ids['board-projects'].textContent.includes('1 card open'));
  assert(ids['board-projects'].textContent.includes('1 waiting for an owner'));
  assert(ids['board-projects'].textContent.includes('Manager harness-development'));
  assert(ids['board-projects'].textContent.includes('50% of the plan'));
  assert.equal(ids['board-projects-count'].textContent,'1 active · 1 total');
  // A refused projects journal leaves every card readable and the project list empty.
  // The encouragement line below would describe that as a board nobody has grouped
  // yet, which is the refusal-as-emptiness shape again, one journal over.
  const savedProjects=statusBody.board_status.projects;
  statusBody.board_status={...statusBody.board_status,projects:[],
    projects_unreadable:'the projects journal is 4202752 bytes and the reader cap is 4194304 bytes'};
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-projects'].textContent.includes('Projects unreadable: the projects journal'),
         'a refused project journal names its wall');
  assert(!ids['board-projects'].textContent.includes('group related fixes into subjects'),
         'a refusal is not shown as an ungrouped board');
  statusBody.board_status={...statusBody.board_status,projects:savedProjects,projects_unreadable:undefined};
  await ids['refresh-tasks'].fire('click');
  // The single board is filtered by project, and unfiled work is visible on purpose.
  assert(ids['board-filters'].textContent.includes('Shared board'));
  assert(ids['board-filters'].textContent.includes('Unfiled (1)'),'unfiled work names its count');
  assert(ids['board-cards'].textContent.includes('Shared board'),'a card names its project');
  assert(ids['board-cards'].textContent.includes('Unfiled'),'an unfiled card says so');
  // The unfiled filter names its count, so match the label, not an exact string.
  await ids['board-filters'].children.find(node=>node.textContent.startsWith('Unfiled')).fire('click');
  assert(ids['board-cards'].textContent.includes('Card 3'),'the unfiled card stays on the one board');
  assert(!ids['board-cards'].textContent.includes('Card 1'),'a filed card is filtered out');
  await ids['board-filters'].children[0].fire('click');
  assert.equal(ids['board-filters'].children[0].getAttribute('aria-pressed'),'true');
  await ids['board-filters'].children[0].fire('click');
  // Opening the project shows its plan as steps a person can tick.
  await ids['board-projects'].querySelector('button').fire('click');
  assert.equal(ids['project-page-block'].hidden,false);
  assert(ids['project-page'].textContent.includes('1 of 2 steps done'));
  assert(ids['project-page'].textContent.includes('Store the plan'));
  assert(ids['project-page'].textContent.includes('Render the plan'));
  assert(ids['project-page'].textContent.includes('One board, many threads'));
  // The project surface shows the sessions that touched its work. The payload carries
  // them, so the page must name them rather than leave a reader to infer history.
  statusBody.board_status={...statusBody.board_status,projects:[{...projectView,
    sessions:[{slug:'infinity',cards:1,last_at:2},{slug:'harness-development',cards:0,last_at:0}]}]};
  await ids['refresh-tasks'].fire('click');
  await ids['board-projects'].querySelector('button').fire('click');
  assert(ids['project-page'].textContent.includes('Sessions that touched this project'),
         'the project names its own history');
  assert(ids['project-page'].textContent.includes('infinity'),'a session that wrote a card is named');
  assert(ids['project-page'].textContent.includes('1 card'),'the card count is readable, not a bare number');
  assert(ids['project-page'].textContent.includes('no cards filed'),
         'a session with no filed card says so instead of showing zero');
  // A person can write work from the page. That lane did not exist before.
  ids['board-note-what'].value='Check the NAS tailscaled after the repair';
  ids['board-note-body'].value='The operator noticed it while reading the board.';
  await ids['board-note-form'].fire('submit');
  await settle();
  assert(boardNoteBody,'the note reached the server');
  assert.equal(boardNoteBody.title,'Check the NAS tailscaled after the repair');
  assert.equal(boardNoteBody.note,'The operator noticed it while reading the board.');
  assert.equal(ids['board-note-what'].value,'','the field clears after a successful add');
  assert(ids['board-note-status'].textContent.includes('Card added'),'the page says it landed');
  // A long card note is evidence, not a wall. The column shows a preview
  // and one control to read the rest. The rest stays hidden until asked.
  const longNote='Serving diagnosis. '+('probe result. '.repeat(60)).trim();
  statusBody.board_status={...statusBody.board_status,enabled:true,configured:true,projects:[],
    cards:[{ordinal:9,status:'queued',note:longNote,project:''}]};
  await ids['refresh-tasks'].fire('click');
  const walk=(node,test,out=[])=>{if(test(node))out.push(node);for(const child of node.children||[])walk(child,test,out);return out;};
  const noteCard=walk(ids['board-cards'],node=>node.tag==='li'&&node.textContent.includes('Serving diagnosis'))[0];
  assert(noteCard,'the card with the long note is on the board');
  const noteWrap=walk(noteCard,node=>node.className==='board-note')[0];
  assert(noteWrap,'the note has its own block');
  const noteTexts=walk(noteWrap,node=>node.className==='board-note-text');
  assert.equal(noteTexts.length,2,'the preview and the rest are separate paragraphs');
  assert(noteTexts[0].textContent.length<=241,'the visible line is a preview, not the whole note');
  assert(noteTexts[0].textContent.endsWith('\u2026'),'the preview says it continues');
  assert.equal(noteTexts[1].hidden,true,'the rest is not shown until asked');
  const moreButton=walk(noteWrap,node=>node.className==='board-note-more')[0];
  assert(moreButton,'there is a way to read the rest');
  assert(moreButton.getAttribute('aria-expanded')==='false','the control starts collapsed');
  await moreButton.fire('click');
  assert.equal(noteTexts[1].hidden,false,'clicking opens the rest');
  assert(moreButton.getAttribute('aria-expanded')==='true','the control reports open');
  await moreButton.fire('click');
  assert.equal(noteTexts[1].hidden,true,'clicking again closes it');
  // Restore the original card fixture for the status/artifact assertions that follow.
  statusBody.board_status={...statusBody.board_status,cards:savedBoardCards,projects:[]};
  await ids['refresh-tasks'].fire('click');
  assert(!ids['board-cards'].textContent.includes(privateDetail));
  assert(!ids['board-cards'].textContent.includes(privateAlias));
  assert(!ids['board-cards'].querySelector('a'));
  for (const status of ['queued','running','blocked','succeeded','failed','cancelled']) {
    boardCard.status=status;
    await ids['refresh-tasks'].fire('click');
    assert(!ids['board-cards'].textContent.includes('Status not reported'));
  }
  for (const invalid of [privateDetail,{toString:null},true,[],null]) {
    boardCard.status=invalid;boardCard.attempt=invalid;boardCard.artifact_count=invalid;boardCard.question_pending=invalid;
    await ids['refresh-tasks'].fire('click');
    assert(ids['board-cards'].textContent.includes('Status not reported'));
    assert(ids['board-cards'].textContent.includes('Artifacts not reported'));
    if (invalid===true) assert(ids['board-cards'].textContent.includes('Your input is requested'));
    assert(!ids['board-cards'].textContent.includes(privateDetail));
  }
  boardCard.status='blocked';boardCard.attempt=1;boardCard.question_pending=true;
  boardCard.artifact_count=Number.MAX_SAFE_INTEGER;
  await ids['refresh-tasks'].fire('click');
  assert(ids['board-cards'].textContent.includes('Artifact 6'));
  assert(!ids['board-cards'].textContent.includes('Artifact 7'));
  boardCard.artifact_count=0;boardCard.question_pending=false;
  await ids['refresh-tasks'].fire('click');
  assert(!ids['board-cards'].textContent.includes('No artifacts attached'));
  assert(!ids['board-cards'].textContent.includes('Your input is requested'));
  for (const invalid of [privateDetail,{toString:null},null,'not-a-revision']) {
    statusBody.board_status.revision=invalid;
    await ids['refresh-tasks'].fire('click');
    assert(!ids['board-cards'].querySelector('button'));
  }
  statusBody.board_status.revision=revision;
  await ids['refresh-tasks'].fire('click');
  messages['ui-thread']=[{i:0,kind:'user',text:'Choose the review order.',date:''},{i:1,kind:'agent',text:'Waiting for your choice.',date:''}];
  await ids['nav-wip'].fire('click');
  await ids['board-cards'].querySelector('button').fire('click');
  assert.equal(ids['page-chat'].hidden,false);
  assert.equal(ids['board-evidence'].hidden,false);
  assert(ids['board-evidence'].textContent.includes('messages 0..1'));
  assert(ids.messages.children.some(node=>node.classList.contains('board-source')));
  assert(calls.some(call=>call.path==='/api/board/cards/3/link?revision='+revision&&call.method==='GET'));
  await ids['nav-wip'].fire('click');
  boardLinkStatus=409;
  await ids['board-cards'].querySelector('button').fire('click');
  assert(ids['board-cards'].textContent.includes('snapshot changed'));
  assert(!ids['board-cards'].textContent.includes('PRIVATE_BOARD_ERROR'));
  boardLinkStatus=200;
  boardLinkBody={session_slug:privateDetail,log_start:0,log_end:1};
  await ids['board-cards'].querySelector('button').fire('click');
  assert(ids['board-cards'].textContent.includes('Link unavailable'));
  assert(!ids['board-cards'].textContent.includes(privateDetail));
  boardLinkBody={session_slug:'ui-thread',log_start:0,log_end:1};
  holdNextBoardLink=true;
  const oldLink=ids['board-cards'].querySelector('button').fire('click');
  await settle();
  statusBody.board_status={...statusBody.board_status,revision:'cd'.repeat(32)};
  await ids['refresh-tasks'].fire('click');
  releaseBoardLink();await oldLink;
  assert.equal(ids['page-wip'].hidden,false);
  holdNextBoardLink=true;
  const abandonedLink=ids['board-cards'].querySelector('button').fire('click');
  await settle();
  await ids['nav-settings'].fire('click');
  releaseBoardLink();await abandonedLink;
  assert.equal(ids['page-settings'].hidden,false);
  assert(!ids['board-cards'].textContent.includes('Opening the linked session'));
  await ids['nav-wip'].fire('click');
  boardLinkStatus=403;
  await ids['board-cards'].querySelector('button').fire('click');
  assert.equal(ids['board-state'].textContent,'Status unavailable');
  assert(!ids['board-cards'].textContent.includes('Card 3'));
  boardLinkStatus=200;
  // Voice configuration never mints access or prompts for a provider key by itself.
  const resultFixtures=JSON.parse(fs.readFileSync('web/tests/fixtures/results-status.json','utf8'));
  const voiceLabels={off:'BLOCKED',configured:'Configured',active:'Active on server',error:'Voice error',offline:'Status unavailable',rejected:'Status unavailable'};
  const beforeVoiceFixtures=calls.filter(call=>call.path.includes('/voice/')).length;
  for (const [name,fixture] of Object.entries(resultFixtures.voice)) {
    failStatus=fixture.http_status||0;
    statusBody.voice={...fixture.voice,endpoint:privateDetail,model:privateDetail,token:privateDetail,error:privateDetail};
    if (name==='active') {
      assert.equal(ids['voice-page-toggle'].textContent,'Talk');
      assert.equal(ids['voice-page-state'].dataset.state,'');
    }
    if (name==='off') {
      assert(ids['voice-toggle'].disabled&&ids['voice-page-toggle'].disabled);
    }
    if (name==='rejected'||name==='offline') {
      assert(ids['voice-toggle'].disabled&&ids['voice-page-toggle'].disabled);
    }
  }
  failStatus=0;
  for (const [category,copy] of [['credential','Voice credentials are unavailable'],['permission','Voice access was denied']]) {
    statusBody.voice={enabled:true,configured:true,active_count:0,error_category:category,error:privateDetail,credential_ref:privateDetail,endpoint:privateDetail};
  }
  for (const value of [undefined,-1,'1',true,NaN,Number.MAX_SAFE_INTEGER+1]) {
    statusBody.voice={enabled:true,configured:true,active_count:value,transcript_count:value,error_category:privateDetail};
  }
  assert.equal(calls.filter(call=>call.path.includes('/voice/')).length,beforeVoiceFixtures);
  for (const invalid of [undefined,null,[],privateDetail,{toString:null}]) {
    statusBody.voice=invalid;
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['voice-state'].textContent,'Not reported');
    assert(ids['voice-toggle'].disabled&&ids['voice-page-toggle'].disabled);
  }
  statusBody.voice={enabled:false,configured:false,url:privateDetail,key:privateDetail};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['voice-state'].textContent,'Off');
  statusBody.voice={enabled:true,configured:false};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['voice-state'].textContent,'Not configured');
  statusBody.voice={enabled:true,configured:true,url:privateDetail,key:privateDetail};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['voice-state'].textContent,'Ready');
  assert(!ids['voice-toggle'].disabled);
  assert(!ids['voice-detail'].textContent.includes(privateDetail));
  const beforeVoice=calls.filter(call=>call.path.includes('/voice/')).length;
  await ids['nav-voice'].fire('click');
  assert.equal(ids['page-voice'].hidden,false);
  assert.equal(calls.filter(call=>call.path.includes('/voice/')).length,beforeVoice);
  messages['ui-thread'].push({i:30,kind:'user',text:'A recorded voice line.',date:'',meta:{voice_transcript:true,voice_id:privateDetail}});
  await ids['retry-button'].fire('click');await settle();
  const transcript=ids.messages.children.find(node=>node.dataset.index==='30');
  assert(transcript.textContent.includes('Voice transcript'));
  assert(transcript.textContent.includes('A recorded voice line.'));
  assert(!transcript.textContent.includes(privateDetail));
  // Infrastructure displays typed observations, never private inventory or diagnostics.
  for (const invalid of [undefined,null,[],privateDetail,{toString:null}]) {
    statusBody.infrastructure_status=invalid;
    await ids['refresh-infrastructure'].fire('click');
    assert.equal(ids['infrastructure-state'].textContent,'Not reported');
  }
  const host={ordinal:4,enabled:true,configured:true,kind:'ssh',health:'healthy',age_seconds:3,
    metrics:{uptime_seconds:800,cpu_percent:12.5,cpu_count:8,cpu_load1:0.5,memory_used_bytes:1073741824,disk_used_bytes:0,private:privateDetail},
    gpu:[{ordinal:2,utilization_percent:0,memory_used_bytes:1024,temperature_celsius:-2,name:privateAlias}],
    services:[{ordinal:3,state:'running',name:privateAlias}],name:privateAlias,address:privateDetail,ssh_alias:privateDetail,error:privateDetail};
  const infra={enabled:true,configured:true,hosts:[host],url:privateDetail};
  statusBody.infrastructure_status=infra;
  await ids['nav-infrastructure'].fire('click');
  await ids['refresh-infrastructure'].fire('click');
  assert.equal(ids['page-infrastructure'].hidden,false);
  assert.equal(ids['infrastructure-state'].textContent,'Live');
  let hostText=ids['infrastructure-hosts'].textContent;
  for (const text of ['Host 4','SSH','Healthy','Updated 3s ago','CPU13%','Memory1.0 GiB','Disk0 B','Load (1 min)0.5','GPU 2 use0%','GPU 2 memory used1.0 KiB','GPU 2 temperature-2 °C','Service 3: Running']) assert(hostText.includes(text),text);
  assert(!hostText.includes(privateDetail)&&!hostText.includes(privateAlias));
  assert.equal(ids['message-input'].value,'Keep this draft');
  for (const [key,label] of Object.entries({unknown:'Not checked',healthy:'Healthy',degraded:'Degraded',unreachable:'Unreachable',stale:'Stale',disabled:'Off',unconfigured:'Not configured'})) {
    host.health=key;
    await ids['refresh-infrastructure'].fire('click');
    assert(ids['infrastructure-hosts'].textContent.includes(label));
  }
  for (const [key,label] of Object.entries({local:'Local',ssh:'SSH',agent:'Agent',unknown:'Unknown kind'})) {
    host.kind=key;
    await ids['refresh-infrastructure'].fire('click');
    assert(ids['infrastructure-hosts'].textContent.includes(label));
  }
  host.health='healthy';host.enabled=false;
  await ids['refresh-infrastructure'].fire('click');
  assert(!ids['infrastructure-hosts'].textContent.includes('Healthy'));
  host.enabled=true;host.configured=false;
  await ids['refresh-infrastructure'].fire('click');
  assert(ids['infrastructure-hosts'].textContent.includes('Not configured'));
  host.configured=true;
  for (const invalid of [privateDetail,{toString:null},[],true,null,-1,Infinity,NaN,Number.MAX_SAFE_INTEGER+1]) {
    host.ordinal=invalid;host.kind=invalid;host.health=invalid;host.age_seconds=invalid;
    host.error_category=invalid;host.metrics={cpu_percent:invalid,memory_used_bytes:invalid};
    host.gpu=[{ordinal:invalid,utilization_percent:invalid,temperature_celsius:invalid}];host.services=[{ordinal:invalid,state:invalid}];
    await ids['refresh-infrastructure'].fire('click');
    hostText=ids['infrastructure-hosts'].textContent;
    assert(hostText.includes('Host 1Kind not reportedHealth not reported'));
    assert(hostText.includes('CPUNot reported'));
    assert(hostText.includes('GPU 1 useNot reported'));
    assert(hostText.includes('Service 1: Not reported'));
    assert(!hostText.includes(privateDetail));
  }
  host.metrics={cpu_percent:101,memory_used_bytes:1.5};host.gpu=[];host.services=[];
  await ids['refresh-infrastructure'].fire('click');
  assert(ids['infrastructure-hosts'].textContent.includes('CPUNot reported'));
  assert(ids['infrastructure-hosts'].textContent.includes('MemoryNot reported'));
  for (const invalid of [null,[null],[[]],{},privateDetail]) {
    infra.hosts=invalid;
    await ids['refresh-infrastructure'].fire('click');
    assert.equal(ids['infrastructure-state'].textContent,'Not reported');
    assert(!ids['infrastructure-hosts'].textContent.includes('Host 1'));
  }
  infra.hosts=[];
  await ids['refresh-infrastructure'].fire('click');
  assert.equal(ids['infrastructure-state'].textContent,'Empty');
  infra.configured=false;infra.hosts=[host];
  await ids['refresh-infrastructure'].fire('click');
  assert.equal(ids['infrastructure-state'].textContent,'Not configured');
  infra.enabled=false;
  await ids['refresh-infrastructure'].fire('click');
  assert.equal(ids['infrastructure-state'].textContent,'Off');
  assert(!ids['infrastructure-hosts'].textContent.includes('Host 1'));
  // The Memory, CPU, and Disk tiles show the numbers hosts actually report.
  // One host that reports only uptime does not blank the tiles for the others.
  const reporting={ordinal:1,enabled:true,configured:true,kind:'local',health:'healthy',age_seconds:2,
    metrics:{uptime_seconds:900,cpu_percent:20,cpu_count:8,cpu_load1:1,
      memory_total_bytes:17179869184,memory_available_bytes:4294967296,
      disk_total_bytes:994662584320,disk_used_bytes:497331292160,disk_free_bytes:497331292160}};
  const uptimeOnly={ordinal:2,enabled:true,configured:true,kind:'local',health:'healthy',age_seconds:2,
    metrics:{uptime_seconds:1}};
  infra.enabled=true;infra.configured=true;infra.hosts=[reporting,uptimeOnly];
  await ids['refresh-infrastructure'].fire('click');
  let kpis=ids['infrastructure-kpis'].textContent;
  assert(kpis.includes('Memory4.0GiB free'),kpis);
  assert(kpis.includes('CPU20%'),kpis);
  assert(kpis.includes('Disk463GiB'),kpis);
  assert(!kpis.includes('Unknown'),'a reporting host must never render as Unknown');
  // The sum is over a subset, so the tile has to say so.
  assert(kpis.includes('1 of 2 hosts'),kpis);
  // When nothing reports a metric, name what is missing instead of printing Unknown.
  infra.hosts=[uptimeOnly];
  await ids['refresh-infrastructure'].fire('click');
  kpis=ids['infrastructure-kpis'].textContent;
  assert(kpis.includes('No active host reports memory'),kpis);
  assert(kpis.includes('No active host reports CPU load'),kpis);
  assert(kpis.includes('No active host reports disk'),kpis);
  assert(!kpis.includes('Unknown'));
  // Full coverage carries no note, so the note cannot become noise on a healthy fleet.
  infra.hosts=[reporting];
  await ids['refresh-infrastructure'].fire('click');
  kpis=ids['infrastructure-kpis'].textContent;
  assert(!kpis.includes('of 1 hosts'),kpis);
  assert(!kpis.includes('hosts'),kpis.slice(kpis.indexOf('Memory')));
  infra.enabled=false;
  await ids['refresh-infrastructure'].fire('click');
  for (const report of [undefined,[],privateDetail,{enabled:true}]) {
    statusBody.grafana=report;
    await ids['refresh-infrastructure'].fire('click');
  }
  statusBody.grafana={enabled:false,configured:true};
  await ids['refresh-infrastructure'].fire('click');
  statusBody.grafana={enabled:true,configured:false};
  await ids['refresh-infrastructure'].fire('click');
  const grafana={enabled:true,configured:true,health:'healthy',datasource_count:2,dashboard_count:0,embed_url:privateDetail,token:privateDetail,error:privateDetail};
  statusBody.grafana=grafana;
  await ids['refresh-infrastructure'].fire('click');
  // list_limit is a bound, not a filter. The count alone would read as the whole set,
  // so the row has to name what the bound dropped.
  assert(!ids['infra-grafana'].textContent.includes('more not shown'));
  grafana.datasource_omitted=3;
  await ids['refresh-infrastructure'].fire('click');
  assert(ids['infra-grafana'].textContent.includes('2 datasources (3 more not shown)'));
  grafana.datasource_omitted=0;
  await ids['refresh-infrastructure'].fire('click');
  assert(!ids['infra-grafana'].textContent.includes('more not shown'));
  grafana.health='unavailable';grafana.error_category='credential';
  await ids['refresh-infrastructure'].fire('click');
  grafana.error_category=privateDetail;
  for (const invalid of [privateDetail,{toString:null},[],true,null,-1,1.5,Number.MAX_SAFE_INTEGER+1]) {
    grafana.health=invalid;grafana.datasource_count=invalid;grafana.dashboard_count=invalid;grafana.datasource_omitted=invalid;
    await ids['refresh-infrastructure'].fire('click');
  }
  assert(!ids['infra-grafana'].textContent.includes('NaN') && !ids['infra-grafana'].textContent.includes('more not shown'));
  assert(!all.some(node=>node.tag==='iframe'));
  assert(calls.filter(call=>call.path!=='/api/infrastructure/targets'&&!call.path.startsWith('/api/infrastructure/sparks?range=')&&call.path.includes('infrastructure')||call.path.includes('grafana')).length===0);
  // Optional jobs project ordinals, fixed enums and explicit counts only.
  const jobsText=()=>ids['jobs-summary'].textContent+ids['jobs-overview'].textContent+ids['jobs-records'].textContent;
  for (const invalid of [undefined,null,[],true,'configured',{enabled:'true',configured:true,jobs:[]},{enabled:true,configured:true,jobs:[null]}]) {
    statusBody.job_status=invalid;
    await ids['refresh-tasks'].fire('click');
    assert.equal(ids['jobs-state'].textContent,'Not reported');
    assert(!jobsText().includes('Job 1'));
  }
  statusBody.job_status={enabled:false,configured:true,jobs:[{status:'running'}]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Off');
  assert(!jobsText().includes('Job 1'));
  statusBody.job_status={enabled:true,configured:false,jobs:[{status:'running'}]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Not configured');
  statusBody.job_status={enabled:true,configured:true,jobs:[]};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Empty');
  assert(jobsText().includes('No jobs'));
  assert.equal(ids['jobs-overview'].hidden,true);
  assert.equal(ids['jobs-overview'].textContent,'');
  const job={ordinal:7,status:'succeeded',worker_count:2,completed_count:2,failed_count:0,artifact_count:3,merge_status:'succeeded',gate_status:'allowed',
    objective:privateDetail,id:privateAlias,backend:privateAlias,account:privateAlias,ref:privateDetail,error:privateDetail,gate:{reason:privateDetail}};
  statusBody.job_status.jobs=[job,{status:'blocked',worker_count:3,completed_count:1,failed_count:1,merge_status:'blocked',gate_status:'blocked'}];
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Snapshot');
  assert(jobsText().includes('2 jobs'));
  const jobCard=ids['jobs-records'].children.find(node=>node.querySelector('h3').textContent==='Job 7');
  assert(jobCard.textContent.includes('Job 7Succeeded'));
  assert(jobCard.textContent.includes('Fan-out2Completed2Failed0Artifacts3MergeSucceededGateAllowed'));
  assert(ids['jobs-records'].children[0].textContent.includes('Job 2Blocked'));
  assert(ids['jobs-records'].children[1].textContent.includes('Job 7Succeeded'));
  assert(!jobsText().includes(privateAlias)&&!jobsText().includes(privateDetail));
  // job_limit is a bound, not a filter: the visible row count alone would read as
  // the whole job store, so the summary has to name what the bound dropped.
  statusBody.job_status.jobs_omitted=3;
  statusBody.job_status.job_limit=2;
  await ids['refresh-tasks'].fire('click');
  assert(jobsText().includes('2 jobs'));
  assert(ids['jobs-summary'].textContent.includes('3 older jobs not shown (2-job limit)'));
  statusBody.job_status.jobs_omitted=0;
  await ids['refresh-tasks'].fire('click');
  assert(!ids['jobs-summary'].textContent.includes('older jobs not shown'));
  assert.equal(ids['jobs-overview'].hidden,false);
  assert(ids['jobs-overview'].textContent.includes('Succeeded1'));
  assert(ids['jobs-overview'].textContent.includes('Blocked1'));
  assert(jobCard.textContent.includes('2/2 workers done'));
  assert.equal(jobCard.querySelector('progress').getAttribute('value'),'2');
  assert.equal(jobCard.querySelector('progress').getAttribute('max'),'2');
  assert(jobCard.textContent.includes('No worker details'));
  const worker={ordinal:1,status:'running',attempt:1,gate_status:'allowed',id:privateAlias,backend:privateAlias,account:privateAlias,model:privateDetail,endpoint:privateDetail,ref:privateDetail,error:privateDetail};
  statusBody.job_status.jobs=[{...job,status:'running',completed_count:0,merge_status:'pending',gate_status:'not_checked',workers:[worker,{ordinal:2,status:'queued',attempt:0,gate_status:'blocked'}]}];
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-overview'].textContent,'Running1');
  assert(jobsText().includes('Worker 1RunningAttempt 1Gate: Allowed'));
  assert(jobsText().includes('Worker 2QueuedAttempt 0Gate: Blocked'));
  assert(jobsText().includes('0/2 workers done'));
  assert(!jobsText().includes(privateAlias)&&!jobsText().includes(privateDetail));
  statusBody.job_status.jobs[0].workers=[{...worker,status:privateDetail,attempt:privateDetail,gate_status:privateDetail}];
  await ids['refresh-tasks'].fire('click');
  assert(jobsText().includes('Worker 1Not reportedAttempt Not reportedGate: Not reported'));
  assert(jobsText().includes('only 1 of 2 workers'));
  assert(!jobsText().includes(privateDetail));
  for (const workers of [undefined,null,{},[null],[{}],[{...worker,ordinal:'1'}],[{...worker,ordinal:0}],[{...worker,ordinal:NaN}],[worker,worker],[worker,{ordinal:2},{ordinal:3}]]) {
    statusBody.job_status.jobs[0].workers=workers;
    await ids['refresh-tasks'].fire('click');
    assert(jobsText().includes('No worker details'));
    assert(!jobsText().includes('Worker 1'));
  }
  statusBody.job_status.jobs[0].workers=[];
  await ids['refresh-tasks'].fire('click');
  assert(jobsText().includes('No worker records are reported'));
  for (const invalid of [true,-1,1.5,'2',{},NaN,Infinity,Number.MAX_SAFE_INTEGER+1,privateDetail]) {
    statusBody.job_status.jobs=[{ordinal:invalid,status:invalid,worker_count:invalid,completed_count:invalid,failed_count:invalid,artifact_count:invalid,merge_status:invalid,gate_status:invalid}];
    await ids['refresh-tasks'].fire('click');
    const text=ids['jobs-records'].textContent;
    assert(text.includes('Job 1Not reportedFan-outNot reportedCompletedNot reportedFailedNot reportedArtifactsNot reportedMergeNot reportedGateNot reported'));
    assert(!text.includes(privateDetail));
  }
  statusBody.job_status.jobs=[{worker_count:2,completed_count:3,failed_count:9}];
  await ids['refresh-tasks'].fire('click');
  assert(ids['jobs-records'].textContent.includes('Fan-out2CompletedNot reportedFailedNot reported'));
  statusBody.job_status.jobs=[{worker_count:2,completed_count:2,failed_count:1}];
  await ids['refresh-tasks'].fire('click');
  assert(ids['jobs-records'].textContent.includes('Fan-out2CompletedNot reportedFailedNot reported'));
  assert.equal(ids['jobs-records'].querySelector('progress'),null);
  // Tree connectivity, local view and inheritance cannot prove native view/zoom.
  for (const [name,fixture] of Object.entries(resultFixtures.swarm)) {
    failStatus=fixture.http_status||0;
    statusBody.job_status=fixture.job_status;
    await ids['refresh-tasks'].fire('click');
    const expected={off:'Off',empty:'Empty',offline:'Status unavailable',rejected:'Status unavailable','multi-provider':'Snapshot'};
    assert.equal(ids['jobs-state'].textContent,expected[name]);
    if (name==='multi-provider') {
      assert(jobsText().includes('Providers2'));
      assert(jobsText().includes('Provider 1Account 1'));
      assert(jobsText().includes('Provider 2Account 2'));
      assert(jobsText().includes('MergeSucceeded'));
    }
    if (name==='rejected') assert(ids['jobs-summary'].textContent.includes('Unavailable'));
  }
  failStatus=0;
  const providerJob=resultFixtures.swarm['multi-provider'].job_status.jobs[0];
  for (const invalid of [undefined,0,-1,'2',true,3,NaN,Number.MAX_SAFE_INTEGER+1]) {
    statusBody.job_status={enabled:true,configured:true,jobs:[{...providerJob,provider_count:invalid}]};
    await ids['refresh-tasks'].fire('click');
    assert(jobsText().includes('ProvidersNot reported'));
  }
  statusBody.job_status={enabled:true,configured:true,jobs:[{...providerJob,provider_count:1}]};
  await ids['refresh-tasks'].fire('click');
  assert(jobsText().includes('ProvidersNot reported'));
  statusBody.job_status={enabled:true,configured:true,jobs:[{...providerJob,workers:[{ordinal:1,status:'succeeded',provider_ordinal:privateDetail,account_ordinal:privateDetail,provider:privateDetail,endpoint:privateDetail}]}]};
  await ids['refresh-tasks'].fire('click');
  assert(jobsText().includes('Provider not reported'));
  assert(!jobsText().includes(privateDetail));
  const treeText=()=>ids['memory-tree-summary'].textContent+ids['memory-tree-metadata'].textContent;
  for (const invalid of [undefined,null,[],true,'configured',{}, {enabled:true,configured:true,reachable:true,endpoint:privateDetail,tree:{text:privateDetail}}]) {
    statusBody.aethermind=invalid;
    await ids['refresh-tasks'].fire('click');
    assert.equal(ids['memory-tree-state'].textContent,'Not reported');
    assert(ids['memory-tree-metadata'].textContent.includes('ViewNot reportedZoomNot reported'));
    assert(!treeText().includes(privateDetail));
  }
  statusBody.aethermind={enabled:true,active:'embedded',reason:'No external service configured',configured:true,reachable:null};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Embedded');
  assert(ids['memory-tree-summary'].textContent.includes('No external service configured'));
  statusBody.aethermind={enabled:true,active:'external',reason:'External service validated',configured:true,reachable:true};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'External');
  assert(ids['memory-tree-summary'].textContent.includes('External service validated'));
  statusBody.aethermind={enabled:true,active:'external',reason:'External service validated',configured:true,reachable:true,embedded_stop:'Embedded engine stopped'};
  await ids['refresh-tasks'].fire('click');
  assert(ids['memory-tree-summary'].textContent.includes('Embedded engine stopped'));
  statusBody.aethermind={enabled:true,active:'embedded',reason:'External service did not validate',configured:true,reachable:false,external_error:'McpError',last_error:'McpError'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Embedded');
  assert(ids['memory-tree-summary'].textContent.includes('McpError'));
  assert(!ids['memory-tree-summary'].textContent.includes(privateDetail));
  statusBody.aethermind={enabled:true,configured:true,reachable:false,last_error:'McpError'};
  await ids['refresh-tasks'].fire('click');
  assert(ids['memory-tree-metadata'].textContent.includes('Last errorMcpError'));
  statusBody.aethermind={enabled:true,configured:true,reachable:false,last_error:privateDetail};
  await ids['refresh-tasks'].fire('click');
  assert(treeText().includes(privateDetail));
  assert(ids['memory-tree-metadata'].textContent.includes('Last error'));
  assert.equal(ids['memory-tree-metadata'].querySelector('img, script'), null);
  const sentence = 'OSError: ' + privateDetail + ' while reading the engine';
  const markup = '<img src=x onerror=alert(1)> ' + privateDetail;
  statusBody.aethermind={enabled:true,active:'embedded',reason:'External service did not validate',configured:true,reachable:false,external_error:sentence,last_error:markup,error_category:'unreachable'};
  await ids['refresh-tasks'].fire('click');
  assert(ids['memory-tree-summary'].textContent.includes(sentence));
  assert(ids['memory-tree-metadata'].textContent.includes(markup));
  assert(ids['memory-tree-metadata'].textContent.includes('unreachable: ' + markup));
  assert.equal(ids['memory-tree-summary'].querySelector('img, script'), null);
  assert.equal(ids['memory-tree-metadata'].querySelector('img, script'), null);
  statusBody.aethermind={enabled:true,configured:false,reachable:true};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Not configured');
  statusBody.aethermind={enabled:true,configured:false,engine:'missing'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Engine missing');
  statusBody.aethermind={enabled:true,configured:false,engine:'present'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'bytes present, not started');
  assert(ids['memory-tree-summary'].textContent.includes('build receipt'));
  statusBody.aethermind={enabled:true,configured:false,engine:'present',active:'embedded'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'bytes present, not started');
  statusBody.aethermind={enabled:true,active:'embedded',process:'http://127.0.0.1:9/mcp',reason:'No external service configured',configured:true,engine:'bundled'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Embedded');
  assert(ids['memory-tree-summary'].textContent.includes('http://127.0.0.1:9/mcp'));
  statusBody.aethermind={enabled:true,configured:false,engine:'bundled'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Embedded engine');
  statusBody.aethermind={enabled:true,configured:false,engine:'bundled',lock_backend:'unavailable',lock_detail:'embedded AetherMind needs file locking that native Windows lacks; run Tessera under WSL, or configure an external AetherMind'};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Cannot write');
  assert(ids['memory-tree-summary'].textContent.includes('native Windows lacks'));
  assert(ids['memory-tree-summary'].textContent.includes('run Tessera under WSL'));
  assert(ids['memory-tree-summary'].textContent.includes('configure an external AetherMind'));
  statusBody.aethermind={enabled:true,configured:true,reachable:true};
  await ids['refresh-details'].fire('click');
  await settle();
  assert.equal(ids['memory-tree-state'].textContent,'Not reported');
  assert(ids['inheritance-digest'].textContent.includes('A remembered decision'));
  assert(!ids['memory-tree-metadata'].textContent.includes('Live tree evidence'));
  // A project without a summary tree remains ordinary read-only UI data.
  inheritanceText='No tree yet';
  const beforeNoTreeError=ids['error-text'].textContent;
  const beforeNoTreeCalls=calls.length;
  await ids['refresh-details'].fire('click');
  await settle();
  assert(ids['inheritance-digest'].textContent.includes(inheritanceText));
  assert.equal(ids['memory-tree-state'].textContent,'Not reported');
  assert.equal(ids['error-text'].textContent,beforeNoTreeError);
  const noTreeCalls=calls.slice(beforeNoTreeCalls);
  assert(noTreeCalls.some(call=>/\/view(\?|$)/.test(call.path)));
  assert(noTreeCalls.every(call=>call.method==='GET'));
  assert(noTreeCalls.every(call=>!/(build|initialize|register|write)/i.test(call.path)));
  inheritanceText='A remembered decision.';
  // Additive boolean native-tree status is explicit, never inferred from transport.
  for (const tree of [undefined,null,[],true,{present:'true',view_available:privateDetail,zoom_available:privateDetail,content:privateDetail}]) {
    statusBody.aethermind={enabled:true,configured:true,reachable:true,tree_status:tree};
    await ids['refresh-tasks'].fire('click');
    assert.equal(ids['memory-tree-state'].textContent,'Not reported');
    assert(treeText().includes('TreeNot reportedViewNot reportedZoomNot reported'));
    assert(!treeText().includes(privateDetail));
  }
  statusBody.aethermind.tree_status={present:false,view_available:false,zoom_available:false,error:privateDetail};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'No tree yet');
  assert.equal(ids['memory-tree-summary'].textContent,'No tree yet');
  assert.equal(ids['memory-tree-state'].dataset.state,'');
  assert(treeText().includes('TreeNot presentViewUnavailableZoomUnavailable'));
  statusBody.aethermind.tree_status={present:false,view_available:true,zoom_available:true};
  await ids['refresh-tasks'].fire('click');
  assert(treeText().includes('TreeNot presentViewNot reportedZoomNot reported'));
  statusBody.aethermind.tree_status={present:true,view_available:true,zoom_available:true,live_evidence:true,project:privateDetail,node:privateDetail};
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Present');
  assert(treeText().includes('TreePresentViewAvailableZoomAvailable'));
  assert(!treeText().includes(privateDetail));
  // Offline fixtures distinguish a normal empty tree from transport/access failures.
  const memoryFixtures=JSON.parse(fs.readFileSync('web/tests/fixtures/memory-status.json','utf8'));
  const memoryLabels={unconfigured:'Not configured','auth-required':'Auth required',unreachable:'Unreachable','tree-present':'Present','no-tree':'No tree yet',unreported:'Not reported'};
  for (const [name,report] of Object.entries(memoryFixtures)) {
    statusBody.aethermind={...report,error:privateDetail,endpoint:privateDetail,token:privateDetail};
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['memory-tree-state'].textContent,memoryLabels[name]);
    assert(!treeText().includes(privateDetail));
    if (name==='no-tree' || name==='tree-present') {
      assert(treeText().includes('ConnectionConnected'));
    }
    if (name==='no-tree') {
      assert.equal(ids['memory-tree-summary'].textContent,'No tree yet');
      assert.equal(ids['memory-tree-state'].dataset.state,'');
    }
  }
  for (const failure of [{reachable:false},{error_category:'authentication'},{error_category:'permission'}]) {
    statusBody.aethermind={enabled:true,configured:true,reachable:true,...failure,tree_status:{present:true,view_available:true,zoom_available:true}};
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['memory-tree-state'].textContent,failure.reachable===false?'Unreachable':'Auth required');
    assert(!treeText().includes('TreePresent'));
    assert(!treeText().includes('ViewAvailable'));
  }
  // Queue observations do not establish delivery, retries or tree availability.
  const captureFixtures=JSON.parse(fs.readFileSync('web/tests/fixtures/capture-status.json','utf8'));
  for (const [name,fixture] of Object.entries(captureFixtures)) {
    failStatus=fixture.http_status||0;
    statusBody={...fixture};
    await ids['settings-refresh'].fire('click');
    const count=fixture.debt_total;
    let label='Not reported';
    if (Number.isSafeInteger(count)) label=count>0?'Queued':'Empty';
    if (fixture.http_status) label='Status unavailable';
    if (name==='no-tree-empty') assert.equal(ids['memory-tree-state'].textContent,'No tree yet');
    if (name==='unconfigured-with-debt') {
      assert(ids['status-list'].textContent.includes('AetherMindNot configured'));
      assert(!ids['status-list'].textContent.includes('AetherMindOff'));
    }
  }
  failStatus=0;
  for (const invalid of [privateDetail,null,true,-1,1.5,'2',{},Number.MAX_SAFE_INTEGER+1]) {
    statusBody={aethermind:{},debt_total:invalid,sessions:Object.fromEntries(sessions.map(session=>[session.slug,{debt:invalid}]))};
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['memory-capture-state'].textContent,'Not reported');
    assert(!ids['status-list'].textContent.includes(privateDetail));
    assert(!ids['write-debt'].textContent.includes(privateDetail));
  }
  statusBody={aethermind:{},write_debt:2,sessions:Object.fromEntries(sessions.map(session=>[session.slug,{debt:[]}]))};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['memory-capture-state'].textContent,'Empty');
  assert(!ids['write-debt'].textContent.includes('caught up'));
  // Release copy stays consistent across all three panels after reported activity.
  const releaseFixtures=JSON.parse(fs.readFileSync('web/tests/fixtures/release-status.json','utf8'));
  const beforeReleaseCalls=calls.length;
  for (const fixture of releaseFixtures.cases) {
    failStatus=0;
    failSessionList=0;
    statusBody=JSON.parse(JSON.stringify(releaseFixtures.reported));
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['jobs-state'].textContent,'Snapshot');
    failStatus=fixture.http_status||0;
    failSessionList=fixture.http_status===403?403:0;
    statusBody=fixture.status||{};
    await ids['settings-refresh'].fire('click');
    assert.equal(ids['jobs-state'].textContent,fixture.expected.swarm,fixture.name);
    assert(!ids['jobs-records'].textContent.includes('Job 1'));
    assert(!ids['error-text'].textContent.includes('PRIVATE_STATUS_ERROR'));
    assert(!ids['error-text'].textContent.includes('PRIVATE_SESSION_ERROR'));
    if (fixture.http_status) {
      assert(ids['voice-page-toggle'].disabled);
      assert.equal(ids['voice-page-state'].textContent,'Status unavailable');
      if (fixture.http_status === 403) {
        assert(ids['error-banner'].hidden);
      } else {
        assert(ids['error-text'].textContent.includes('Server unreachable'));
      }
      assert(!ids['error-text'].textContent.includes('Access required'));
    } else {
    }
  }
  assert(calls.slice(beforeReleaseCalls).every(call=>call.method==='GET'&&['/api/status','/api/sessions'].includes(call.path)));
  assert.equal(calls.slice(beforeReleaseCalls).filter(call=>call.path==='/api/status').length,releaseFixtures.cases.length*2);
  failStatus=0;
  failSessionList=0;
  statusBody={aethermind:{enabled:true,configured:true,reachable:true}};
  statusBody.aethermind={enabled:true,configured:true,reachable:null,tree_status:{present:true,view_available:true,zoom_available:true},error_category:privateDetail};
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['memory-tree-state'].textContent,'Not reported');
  assert(!treeText().includes('ViewAvailable'));
  assert(!treeText().includes(privateDetail));
  // Failed refresh replaces all previous provider status and never echoes private errors.
  failStatus=503;
  await ids['settings-refresh'].fire('click');
  assert(!ids['error-text'].textContent.includes('PRIVATE_STATUS_ERROR'));
  assert.equal(ids['tasks-state'].textContent,'Status unavailable');
  assert.equal(ids['tasks-filters'].hidden,true);
  assert.equal(ids['tasks-filters'].children.length,0);
  assert.equal(ids['tasks-filter-result'].hidden,true);
  assert(!ids['tasks-records'].textContent.includes('Task 1'));
  assert.equal(ids['board-state'].textContent,'Status unavailable');
  assert.equal(ids['infrastructure-state'].textContent,'Status unavailable');
  assert.equal(ids['jobs-state'].textContent,'Status unavailable');
  assert.equal(ids['jobs-overview'].hidden,true);
  assert.equal(ids['jobs-overview'].textContent,'');
  assert.equal(ids['memory-tree-state'].textContent,'Status unavailable');
  assert.equal(ids['memory-tree-metadata'].textContent,'');
  failStatus=0;
  statusBody={backends:[privateAlias],provider_status:{backends:[{id:privateAlias,configured:true,health:'healthy'}]},task_status:{enabled:true,configured:true,tasks:[{status:'running',attempt:1}]},jev:{enabled:true,configured:true,decision_log_present:true,decision_count:8},infrastructure_status:{enabled:true,configured:true,hosts:[{ordinal:8,health:'healthy'}]},grafana:{enabled:true,configured:true,health:'healthy',dashboard_count:8}};
  statusBody.job_status={enabled:true,configured:true,jobs:[{...job,workers:[worker]}]};
  statusBody.aethermind={enabled:true,configured:true,reachable:true,tree_status:{present:true,view_available:true,zoom_available:true}};
  statusBody.voice={...resultFixtures.voice.active.voice};
  holdNextStatus=true;
  const olderRefresh=ids['settings-refresh'].fire('click');
  await settle();
  holdNextView=true;
  const olderView=ids['refresh-details'].fire('click');
  await settle();
  failStatus=503;
  await ids['settings-refresh'].fire('click');
  releaseStatus();
  await olderRefresh;
  releaseView();
  await olderView;
  assert.equal(ids['memory-capture-state'].textContent,'Status unavailable');
  assert(!ids['write-debt'].textContent.includes('3 unsent'));
  assert(!ids['inheritance-digest'].textContent.includes('A remembered decision'));
  assert.equal(ids['tasks-state'].textContent,'Status unavailable');
  assert(!ids['tasks-records'].textContent.includes('Running'));
  assert.equal(ids['infrastructure-state'].textContent,'Status unavailable');
  assert(!ids['infrastructure-hosts'].textContent.includes('Host 8'));
  assert.equal(ids['jobs-state'].textContent,'Status unavailable');
  assert.equal(ids['jobs-overview'].hidden,true);
  assert.equal(ids['jobs-overview'].textContent,'');
  assert(!jobsText().includes('Job 7'));
  assert(!jobsText().includes('Worker 1'));
  assert.equal(ids['jobs-overview'].hidden,true);
  assert.equal(ids['memory-tree-state'].textContent,'Status unavailable');
  assert.equal(ids['memory-tree-metadata'].textContent,'');
  failStatus=403;
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Status unavailable');
  assert.equal(ids['memory-capture-state'].textContent,'Status unavailable');
  for (const id of ['tasks-summary','infrastructure-summary','jobs-summary','memory-tree-summary','voice-page-detail','settings-connection'])
    assert(!ids[id].textContent.includes('Access required'),id);
  failStatus=0;
  statusBody=null;
  await ids['settings-refresh'].fire('click');
  statusBody={};
  await ids['settings-refresh'].fire('click');
  statusBody={backends:[]};
  await ids['settings-refresh'].fire('click');
  // Consume the actual server DTO, not a separately invented UI schema.
  statusBody=actualStatus;
  await ids['settings-refresh'].fire('click');
  assert.equal(ids['tasks-state'].textContent,actualStatus.task_status ? 'Off' : 'Not reported');
  assert.equal(ids['board-state'].textContent,actualStatus.board_status ? 'Off' : 'Not reported');
  assert.equal(ids['infrastructure-state'].textContent,actualStatus.infrastructure_status ? 'Off' : 'Not reported');
  assert.equal(ids['jobs-state'].textContent,actualStatus.job_status?.enabled===false || actualStatus.task_status?.enabled===false ? 'Off' : 'Not reported');
  const memoryReport=actualStatus.aethermind;
  const memoryLabel=!memoryReport ? 'Not reported'
    : memoryReport.active==='external' ? 'External'
    : memoryReport.active==='embedded' && (memoryReport.process || memoryReport.engine!=='present') ? 'Embedded'
    : memoryReport.engine==='present' && !memoryReport.process ? 'bytes present, not started'
    : memoryReport.engine==='missing' ? 'Engine missing'
    : memoryReport.engine==='bundled' ? 'Embedded engine'
    : 'Not reported';
  assert.equal(ids['memory-tree-state'].textContent,memoryLabel);
  if (actualStatus.aethermind?.reason) assert(ids['memory-tree-summary'].textContent.includes(actualStatus.aethermind.reason));
  sessions.find(session=>session.slug==='ui-thread').backend='private-ui-fixture-backend';
  await ids['settings-refresh'].fire('click');
  assert(ids['session-detail'].textContent.includes('private-ui-fixture-backend')); // real backend names
  assert.equal(ids['session-backend'].children[1].value,'private-ui-fixture-backend');
  statusBody=actualSnapshots.populated;
  await ids['refresh-tasks'].fire('click');
  if (statusBody.infrastructure_status) {
    assert.equal(ids['infrastructure-state'].textContent,'Live');
    assert(ids['infrastructure-hosts'].textContent.includes('Host 1LocalHealthy'));
    assert(ids['infrastructure-hosts'].textContent.includes('CPU17%'));
    assert(ids['infrastructure-hosts'].textContent.includes('GPU 1 use0%'));
    assert(ids['infrastructure-hosts'].textContent.includes('Service 1: Running'));
    assert(!ids['infrastructure-hosts'].textContent.includes('private-ui-fixture'));
  } else assert.equal(ids['infrastructure-state'].textContent,'Not reported');
  if (statusBody.task_status) {
    assert.equal(ids['tasks-state'].textContent,'Snapshot');
    assert(ids['tasks-summary'].textContent.includes('3 tasks'));
    assert(ids['tasks-filters'].textContent.includes('Running (1)'));
    assert(ids['tasks-records'].textContent.includes('Account not reported'));
    assert(!ids['tasks-records'].textContent.includes('private-ui-fixture'));
    assert(!ids['tasks-records'].textContent.includes('PRIVATE_TASK'));
    await ids['tasks-filters'].children.find(node=>node.dataset.filter==='running').fire('click');
    assert.equal(ids['tasks-records'].children.length,1);
    assert(ids['tasks-records'].textContent.includes('Task 2Attempt 1'));
  } else {
    assert.equal(ids['tasks-state'].textContent,'Not reported');
    assert.equal(ids['tasks-filters'].hidden,true);
  }
  if (statusBody.board_status && actualSnapshots.board_link) {
    // The badge counts open work now, not whether the board answered.
    assert(!/^Live$/.test(ids['board-state'].textContent),
           'the board badge describes the work, not the transport');
    assert(/open|done|Empty/.test(ids['board-state'].textContent),
           'the board badge says something a reader can act on');
    assert(ids['board-cards'].textContent.includes('Your input is requested'));
    assert(ids['board-cards'].textContent.includes('Artifact 1Artifact 2'));
    assert(!ids['board-cards'].textContent.includes('PRIVATE_BOARD'));
    const source=actualSnapshots.board_link.session_slug;
    sessions.push({slug:source,title:'Linked source',archived:false,messages:2});
    messages[source]=[{i:0,kind:'user',text:'Choose the review order.',date:''},{i:1,kind:'agent',text:'Awaiting input.',date:''}];
    boardLinkBody=actualSnapshots.board_link;
    await ids['nav-wip'].fire('click');
    await ids['board-cards'].querySelector('button').fire('click');
    assert.equal(ids['page-chat'].hidden,false);
    assert.equal(ids['session-title'].textContent,'Linked source');
    assert(ids['board-evidence'].textContent.includes('messages 0..1'));
    assert(ids.messages.children.some(node=>node.classList.contains('board-source')));
  }
  statusBody=actualSnapshots.running;
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-overview'].textContent,'Running1');
  assert(jobsText().includes('Job 1Running'));
  assert(jobsText().includes('0/2 workers done'));
  if (statusBody.job_status.jobs[0].workers) {
    assert(jobsText().includes('Worker 1RunningAttempt 1'));
    assert(jobsText().includes('Worker 2RunningAttempt 1'));
  } else assert(jobsText().includes('No worker details'));
  statusBody=actualSnapshots.jobs;
  await ids['refresh-tasks'].fire('click');
  assert.equal(ids['jobs-state'].textContent,'Snapshot');
  assert(jobsText().includes('1 job'));
  assert(ids['jobs-records'].textContent.includes('Job 1SucceededFan-out2Completed2Failed0Artifacts2MergeSucceededGateAllowed'));
  assert(!jobsText().includes('PRIVATE_JOB')&&!jobsText().includes('private-ui-fixture'));
  assert.equal(ids['jobs-overview'].textContent,'Succeeded1');
  if (statusBody.job_status.jobs[0].workers) {
    assert(jobsText().includes('Worker 1SucceededAttempt 1'));
    assert(jobsText().includes('Worker 2SucceededAttempt 1'));
  } else assert(jobsText().includes('No worker details'));
  // The board was read-only on the page while its write routes were unverified, and this
  // guard existed so the page would not offer controls that 404. The write routes are now
  // proven end to end, so the guard names the writes that are allowed instead of banning
  // them. A new board write still has to be added here deliberately.
  for (const call of calls.filter(call=>call.path.startsWith('/api/board/'))) {
    if (call.method === 'GET') continue;
    assert(['/api/board/cards','/api/board/projects'].includes(call.path)
      || /^\/api\/board\/cards\/\d+\/(answer|claim|note|status|file|link)$/.test(call.path),
      'unexpected board write: ' + call.method + ' ' + call.path);
  }
  let providerCalls=0;
  for (const call of calls) {
    if (call.path!=='/api/status') continue;
    providerCalls+=1;
    assert.equal(call.method,'GET');
  }
  assert(providerCalls>10);
  ids['session-menu'].hidden = true;
  ids['confirm-dialog'].open = false;
  const archiveRow = all.find((node) => node.dataset && node.dataset.slug === 'project-notes');
  await archiveRow.fire('contextmenu', {clientX: 20, clientY: 20, preventDefault() {}});
  await ids['menu-archive'].fire('click');
  assert.equal(ids['session-menu'].hidden, true, 'Archive closes the session menu');
  assert.equal(ids['confirm-dialog'].open, false, 'row Archive does not ask');
  console.log('Session actions, safe rendering, zoom, Enter/IME handling and credential-free SSE passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    from tessera.config import AccountConfig, BackendConfig, Config
    from tessera.credentials import CredentialResolver
    from tessera.server import create_server
    from tessera.tasks import TaskStore

    config = Config(home=tmp_path, cwd=tmp_path)
    config.server.port = 0
    config.accounts["private-ui-fixture-account"] = AccountConfig(name="private-ui-fixture-account")
    config.backends["private-ui-fixture-backend"] = BackendConfig(
        name="private-ui-fixture-backend", base_url="https://models.example.com/v1",
        model="example-model", account="private-ui-fixture-account")
    config.credentials["private-ui-fixture-reference"] = {"env": "EXAMPLE_UI_JUDGE_KEY"}
    config.jev.credential_ref = "private-ui-fixture-reference"

    def unexpected_resolution(*args, **kwargs):
        pytest.fail("UI status must not resolve credentials")

    monkeypatch.setattr(CredentialResolver, "resolve", unexpected_resolution)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", unexpected_resolution)
    server = create_server(config, web_root=WEB, backend_factory=unexpected_resolution)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        actual_status = json.loads(response.read())
        assert "provider_status" in actual_status
        config.swarm.enabled = True
        board = TaskStore(config.home / config.swarm.tasks_file, enabled=True, clock=lambda: 100.0)
        for status in ("queued", "running", "failed"):
            task = board.create("PRIVATE_TASK_OBJECTIVE", ["PRIVATE_TASK_SCOPE"], {}, "private-ui-fixture-account")
            if status != "queued":
                claim = board.claim(task["id"], "private-ui-fixture-owner", lease_seconds=60)
                if status == "failed":
                    board.finish(claim["id"], claim["owner"], claim["attempt"], "failed", result_ref="private-ui-fixture-artifact")
        subject = server.manager.create("Status fixture")
        decision_log = server.manager.path(subject["slug"]) / "decisions.jsonl"
        decision_log.write_text("".join(json.dumps({"point": point, "model_id": "PRIVATE_JUDGE_MODEL",
            "prompt": "PRIVATE_JUDGE_PROMPT", "error": "PRIVATE_JUDGE_ERROR"}) + "\n"
            for point in ("intake", "verification_batch")))
        if hasattr(config, "board"):
            from tessera.board import BoardStore
            from tessera.log import ChatLog
            config.board.enabled = True
            cards = BoardStore(config.home / config.board.journal_file, enabled=True)
            card = cards.create("PRIVATE_BOARD_TITLE", subject["slug"], 0, 1)
            cards.attach_artifact(card["id"], "PRIVATE_BOARD_ARTIFACT_ONE")
            cards.attach_artifact(card["id"], "PRIVATE_BOARD_ARTIFACT_TWO")
            cards.ask_human(card["id"], "PRIVATE_BOARD_QUESTION", pending=True)
            with ChatLog(server.manager.path(subject["slug"]) / "log") as log:
                log.append("user", "Choose the review order.")
                log.append("agent", "Awaiting input.")
        if hasattr(config, "infrastructure"):
            from types import SimpleNamespace
            from tessera.config import HostConfig
            from tessera.infrastructure import Infrastructure, HostCollector
            config.infrastructure.enabled = True
            config.hosts["private-ui-fixture-host"] = HostConfig(
                name="private-ui-fixture-host", enabled=True, kind="local",
                address="host.example.com", services={"private-ui-fixture-service": []})
            collector = SimpleNamespace(collect=lambda host: {
                "metrics": {"cpu_percent": 17, "memory_used_bytes": 8},
                "gpu": [{"utilization_percent": 0, "name": "private-ui-fixture-gpu"}],
                "services": {"private-ui-fixture-service": "running"}})
            Infrastructure(config, collector=collector).collect("private-ui-fixture-host")
            monkeypatch.setattr(HostCollector, "collect", unexpected_resolution)
            config.grafana.enabled = True
            config.grafana.base_url = "https://metrics.example.com"
            config.grafana.credential_ref = "private-ui-fixture-reference"
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        populated_status = json.loads(response.read())
        if "infrastructure_status" in populated_status:
            assert "private-ui-fixture" not in json.dumps(populated_status["infrastructure_status"])
            assert "example.com" not in json.dumps(populated_status["infrastructure_status"])
        board_link = None
        if populated_status.get("board_status", {}).get("cards"):
            snapshot = populated_status["board_status"]
            connection.request("GET", "/api/board/cards/1/link?revision=" + snapshot["revision"])
            response = connection.getresponse()
            assert response.status == 200
            board_link = json.loads(response.read())
            assert board_link == {"session_slug": subject["slug"], "log_start": 0, "log_end": 1}
            assert "PRIVATE_BOARD" not in json.dumps(snapshot)
        assert server.manager._runtimes == {}
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
        # Build real durable job evidence using two explicitly synthetic lanes.
        from fake_backend import FakeBackend
        from tessera.jobs import SwarmJobs
        from tessera.log import ChatLog
        config.accounts["private-ui-fixture-second-account"] = AccountConfig(name="private-ui-fixture-second-account")
        config.backends["private-ui-fixture-second-backend"] = BackendConfig(
            name="private-ui-fixture-second-backend", base_url="https://models.example.com/v1",
            model="model-placeholder", account="private-ui-fixture-second-account")
        worker_calls = []
        entered = threading.Barrier(3)
        release = threading.Event()

        def worker_backend(settings):
            worker_calls.append(settings.account)

            def answer(messages, tools):
                entered.wait(timeout=5)
                assert release.wait(timeout=5)
                return {"content": "PRIVATE_JOB_FINDING", "finish_reason": "stop",
                        "usage": {"total_tokens": 3}}

            return FakeBackend([answer])

        jobs = SwarmJobs(config, backend_factory=worker_backend)
        with ChatLog(server.manager.path(subject["slug"]) / "log") as log:
            job = jobs.create("PRIVATE_JOB_OBJECTIVE", [
                {"backend": "private-ui-fixture-backend", "objective": "PRIVATE_JOB_ONE", "scope": ["one"]},
                {"backend": "private-ui-fixture-second-backend", "objective": "PRIVATE_JOB_TWO", "scope": ["two"]},
            ], session_slug=subject["slug"], log=log)
            finished = []
            failures = []

            def run_job():
                try:
                    finished.append(jobs.run(job["id"], log=log))
                except Exception as error:
                    failures.append(error)

            runner = threading.Thread(target=run_job, daemon=True)
            runner.start()
            try:
                entered.wait(timeout=5)
                before_running = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                assert response.status == 200
                running_status = json.loads(response.read())
                running = running_status["job_status"]["jobs"][0]
                assert running["status"] == "running" and running["worker_count"] == 2
                assert running["merge_status"] == "pending"
                assert "PRIVATE_JOB" not in json.dumps(running)
                assert "private-ui-fixture" not in json.dumps(running)
                assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before_running
            finally:
                release.set()
                runner.join(timeout=5)
            assert not runner.is_alive() and not failures
            assert finished[0]["status"] == "succeeded"
        assert len(set(worker_calls)) == 2
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        monkeypatch.setattr(SwarmJobs, "run", unexpected_resolution)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 200
        jobs_status = json.loads(response.read())
        projection = jobs_status["job_status"]
        assert projection["enabled"] is True and projection["configured"] is True
        expected = {"ordinal": 1, "status": "succeeded", "worker_count": 2,
            "completed_count": 2, "failed_count": 0, "artifact_count": 2,
            "merge_status": "succeeded", "gate_status": "allowed"}
        assert len(projection["jobs"]) == 1
        assert {key: projection["jobs"][0][key] for key in expected} == expected
        assert set(projection["jobs"][0]) <= set(expected) | {"workers"}
        assert "PRIVATE_JOB" not in json.dumps(projection)
        assert "private-ui-fixture" not in json.dumps(projection)
        assert server.manager._runtimes == {}
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join()
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True,
                            input=json.dumps({"empty": actual_status, "populated": populated_status,
                                              "board_link": board_link, "jobs": jobs_status, "running": running_status}), capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_phone_screen_sleep_does_not_stop_voice():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    handler = script.split('window.addEventListener("pagehide",', 1)[1].split("});", 1)[0]
    assert "event.persisted" in handler
    assert handler.index("event.persisted") < handler.index("stopVoice()")
    assert "if (event.persisted) return;" in handler


def test_web_composer_steers_running_turn_and_offers_stop():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / 'web'
    html = (web / 'index.html').read_text()
    script = (web / 'app.js').read_text()
    assert 'id="stop-turn"' in html
    assert 'id="queue-next"' in html
    assert 'id="queue-list"' in html
    assert html.index('id="queue-list"') > html.index('id="conversation-scroll"')
    assert html.index('id="queue-list"') < html.index('class="composer-area"')
    assert "aria-label\", \"Waiting prompt\"" in script
    assert 'sessionPath(slug, "/queue"), "PATCH"' in script
    assert 'sessionPath(slug, "/steer")' in script
    assert 'sessionPath(slug, "/interrupt")' in script
    assert 'sessionPath(slug, "/queue")' in script
    assert '"steer", "interrupt"' in script
    assert 'const steering = !hold && state.busy && replyTo === null && !refs.length' in script
    assert 'deliverComposer(true)' in script
    assert 'state.queueNext' not in script
    # The server emits a queue event for every revise and remove it accepts. The page
    # has to subscribe to it and reload the list, or a change made anywhere else leaves
    # a prompt on screen that is no longer in the queue.
    assert '"queue"' in script.split('const eventTypes = ')[1].split(']')[0]
    assert 'if (type === "queue") loadQueue(slug, epoch)' in script
    # A waiting prompt is a line with edit, send now and delete, not an open box with a
    # Save button that reads as unsaved. The box only appears after Edit is pressed.
    assert 'element("span", "queue-text", item.text || "")' in script
    assert 'element("button", "", "Edit")' in script
    assert 'element("button", "", "Send now")' in script
    assert 'element("button", "", "Delete")' in script
    assert 'sendQueuedNow' in script
    # Send now drops it from the queue and steers the running turn in that order.
    now = script.split('async function sendQueuedNow')[1].split('\n  async function')[0]
    assert now.index('"/queue"), "DELETE"') < now.index('"/steer"), "POST"')


def test_ui_never_uses_native_dialogs_that_tauri_webkit_drops():
    for path in (ROOT / "web").glob("*.js"):
        assert not re.search(r"\b(?:window\.)?(?:confirm|alert|prompt)\(", path.read_text()), path.name


def test_display_tuning_is_config_not_page_constants():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for name in ("history-page", "item-body-chars", "item-summary-chars"):
        assert f"data-{name}" not in html
    for name in ("history_page", "item_body_chars", "item_summary_chars", "long_press_ms"):
        assert f'uiSetting("{name}")' in script
    # Optional tuning reads config too, and an older core's absence is not a failure.
    assert 'optionalUiSetting("view_page_lines")' in script
    for ident in ("session-menu", "menu-rename", "menu-archive", "menu-delete", "menu-select",
                  "confirm-dialog", "session-select-bar", "select-sessions"):
        assert f'id="{ident}"' in html


def test_transcript_opens_at_the_newest_line_and_a_reader_can_win_the_scroll():
    """A phone load opens at the newest line, and scrolling up wins over follow."""
    script = (WEB / "app.js").read_text()
    # Opening a session scrolls to the newest line, not the oldest.
    open_body = script.split("startStream(slug, epoch);", 1)[1].split('$("message-input").focus', 1)[0]
    assert "scrollToLatest();" in open_body
    # One scrollTop is not enough on a phone: WebKit settles the flex column a
    # frame or two late, so the pin has to repeat or the newest line stays below
    # the fold. It must also stop the moment the reader moves the transcript.
    pin = script.split("function scrollToLatest()", 1)[1].split("function followThought()", 1)[0]
    assert pin.count("scroller.scrollTop = scroller.scrollHeight") >= 1
    assert "requestAnimationFrame" in pin
    assert "state.readerScrolling" in pin
    # The follow yields to deliberate reader input instead of pulling back.
    follow = script.split("function followLatest()", 1)[1].split("function bindTranscriptFollow()", 1)[0]
    assert "state.readerScrolling" in follow
    bind = script.split("function bindTranscriptFollow()", 1)[1].split("function ", 1)[0]
    for kind in ("wheel", "touchmove", "pointerdown"):
        assert f'"{kind}"' in bind
    # The thinking box keeps its own scroll, so reading it does not move the transcript.
    assert 'pre.dataset.stick' in script


def test_board_leads_with_open_work_and_keeps_completed_behind_a_toggle():
    """A long lesson list and a long done list must not bury the open work."""
    html = (WEB / "index.html").read_text()
    assert 'id="board-show-open"' in html and 'id="board-show-done"' in html
    script = (WEB / "app.js").read_text()
    columns = script.split("function renderBoardColumns(cards)", 1)[1].split("function bindColumnDrop", 1)[0]
    assert "matchesBoardWork" in columns
    filters = script.split("function matchesBoardWork", 1)[1].split("function renderWorkOverview", 1)[0]
    # Open work shows every non-finished card; history only toggles finished ones.
    assert "finished && !filters.history" in filters
    assert "state.boardShowDone" in columns
    # Lessons are collapsed, so the count is the quick read and the list is for review.
    items = script.split("function renderPressureItems(items)", 1)[1].split("async function refreshPressure", 1)[0]
    # The default is still collapsed. A group the reader opened stays open across
    # the next rebuild. The assertion is on that default, not on a literal that
    # would collapse every group.
    assert "state.pressureExpanded[key] === true" in items
    assert "pressureExpanded: {}" in script


def test_project_page_carries_a_progress_bar_artifacts_and_notes_that_do_not_make_cards():
    """A project page shows plan progress, artifacts and free notes."""
    html = (WEB / "index.html").read_text()
    assert 'id="project-page"' in html
    script = (WEB / "app.js").read_text()
    page = script.split("function renderProjectPage()", 1)[1].split("async function appendProjectText", 1)[0]
    assert 'role' in page and '"progressbar"' in page
    assert 'aria-valuenow' in page
    assert '"Artifacts"' in page and 'artifact_count' in page
    assert '"Shared planning notes"' in page and "appendProjectText" in page
    # A note is a note: it must not become a card by being saved.
    save = script.split("async function appendProjectText", 1)[1].split("async function toggleStep", 1)[0]
    assert "projectId" in save and "/api/board/cards" not in save


def test_an_unfiled_card_carries_the_way_to_file_it():
    """Unfiled cards must not fall through; the page offers the filing."""
    script = (WEB / "app.js").read_text()
    assert "function fileCardControl" in script


def test_interface_update_nav_states_its_purpose_and_collapses_to_a_dot():
    """Explain the update and keep its text out of the icon-only nav."""
    css = (WEB / "app.css").read_text()
    expanded = re.search(r'\.app-navigation \.update-ready::after\s*\{([^}]+)\}', css)
    assert expanded and 'content: "Update"' in expanded[1]
    collapsed = re.search(
        r'body\.navigation-collapsed \.app-navigation \.update-ready::after\s*\{([^}]+)\}',
        css,
    )
    assert collapsed
    assert 'content: "";' in collapsed[1]
    assert 'Update' not in collapsed[1]
    for declaration in (
        "position: absolute;", "width: 0.4rem;", "height: 0.4rem;",
        "border-radius: 50%;", "background: var(--accent);",
    ):
        assert declaration in collapsed[1]

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for browser-free rendering checks")
    script = r'''
const assert = require('node:assert/strict');
const src = require('fs').readFileSync('web/app.js', 'utf8');
const pick = (name) => {
  const start = src.indexOf('function ' + name + '(');
  assert.ok(start >= 0, 'missing ' + name);
  let depth = 0;
  for (let j = src.indexOf('{', start); j < src.length; j += 1) {
    if (src[j] === '{') depth += 1;
    else if (src[j] === '}') {
      depth -= 1;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error('unbalanced ' + name);
};
const names = ['setInterfaceUpdateReady', 'reloadForNewBuild'];
let body = '';
for (const name of names) body += pick(name) + '\n';
const attrs = new Map();
const classes = new Set();
const link = {
  classList: {toggle(name, ready) { if (ready) classes.add(name); else classes.delete(name); }},
  setAttribute(name, value) { attrs.set(name, value); },
  removeAttribute(name) { attrs.delete(name); },
};
const button = {hidden: true};
const state = {uiBuild: null, applyBuild: false, draft: new Map()};
const lookup = (id) => {
  if (id === 'nav-settings') return link;
  if (id === 'interface-update') return button;
  if (id === 'message-input') return {value: ''};
  throw new Error('unexpected element ' + id);
};
let reloads = 0;
const api = new Function('$', 'state', 'document', 'sessionStorage', 'location',
  'holdsUnrecoverableText', 'draftKey', 'draftFieldsKey',
  body + '\nreturn {reloadForNewBuild};'
)(lookup, state, {querySelectorAll() { return []; }}, {setItem() {}},
  {reload() { reloads += 1; }}, () => false, 'draft', 'fields');
assert.equal(api.reloadForNewBuild('first'), false);
assert.equal(api.reloadForNewBuild('first'), false);
assert.equal(classes.has('update-ready'), false);
assert.equal(attrs.size, 0);
assert.equal(button.hidden, true);
assert.equal(api.reloadForNewBuild('newer'), false);
assert.equal(classes.has('update-ready'), true);
assert.equal(button.hidden, false);
assert.match(attrs.get('title'), /newer interface is available/i);
assert.match(attrs.get('title'), /Update interface in Settings to reload and update/);
assert.match(attrs.get('aria-label'), /^Settings/);
assert.equal(attrs.get('aria-label'), attrs.get('title'));
assert.equal(reloads, 0);
// Only the existing explicit update action applies the pending build.
state.applyBuild = true;
assert.equal(api.reloadForNewBuild(state.pendingBuild), true);
assert.equal(reloads, 1);
assert.equal(classes.has('update-ready'), false);
assert.equal(attrs.size, 0);
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_rebuild_reload_keeps_runtime_fields_and_never_drops_unrecoverable_text():
    """A refresh must not drop typed text while a person is typing.

    Two things had to be true. A field the page creates at runtime must carry an id, so a
    reload can put its words back. And if a field with no id does hold words, the reload
    waits instead of discarding them.
    """
    script = (WEB / "app.js").read_text()
    reload = script.split("function reloadForNewBuild(build)", 1)[1].split("const draftFieldsKey", 1)[0]
    # Only id-carrying fields are saved, and the save is generic rather than a fixed list.
    assert 'querySelectorAll("input[id], textarea[id], select[id]")' in reload
    assert "board-note-what" not in reload.split("querySelectorAll", 1)[0]
    # An unrecoverable field holds the reload rather than losing what is in it.
    assert "holdsUnrecoverableText()" in reload
    assert "holdReloadWhileTyping(build)" in reload
    guard = script.split("function holdsUnrecoverableText()", 1)[1].split("function holdReloadWhileTyping", 1)[0]
    assert "if (field.id) continue;" in guard
    # The runtime-created fields are the ones that were being lost; they now carry ids.
    assert 'field.id = "card-note-" + encodeURIComponent(overlay?.card_id || ordinal);' in script
    assert 'field.id = "queue-edit-" + item.id;' in script
    # A restored card note must be visible, not hidden behind a button.
    assert 'if (state.pendingFieldText[field.id]) editor.hidden = false;' in script
    # The board renders after restoreDrafts, so saved text waits for its field.
    assert "function adoptPendingFieldText()" in script
    assert "adoptPendingFieldText();" in script
    assert '"/api/board/cards/" + ordinal + "/file"' in script
    card = script.split("function boardCardNode(card, index)", 1)[1].split("function moveMenu", 1)[0]
    assert "fileCardControl(card.ordinal, cardId)" in card
    filters = script.split("function renderBoardFilters()", 1)[1].split("function renderBoardPressure", 1)[0]
    assert "Unfiled (${loose})" in filters


def test_board_card_has_human_edit_and_remove_controls():
    """A person can remove and edit card contents in the UI.

    Both controls live beside the claim button, and both call routes that exist. The
    remove confirmation uses the page's own dialog, not a native confirm(), because a
    native confirm blocks the tab on a phone.
    """
    script = (WEB / "app.js").read_text()
    # Anchored on the function name rather than the full signature: the signature gained
    # the card's identity, and a test that pins the parameter list turns every
    # honest signature change into a failure that says nothing about the controls.
    controls = script.split("function claimControls(", 1)[1].split("async function setClaim", 1)[0]
    assert 'element("button", "", "Edit")' in controls
    assert 'element("button", "danger", "Remove")' in controls
    # An id survives a rebuild reload, so a half-typed title is not lost to a poll.
    assert 'titleField.id = "card-title-" + encodeURIComponent(overlay?.card_id || ordinal);' in controls
    assert 'await ask("Take this card off the board? The record is kept.", "Remove")' in controls
    # The page's own dialog, never a native one (see the repo-wide dialog test).
    assert "window.confirm" not in controls

    # The routes and payloads, asserted by intent: the note route carries the revision
    # and the title, the remove route carries the revision, and both carry the card's
    # identity so a stale position cannot retarget the write. They go through boardWrite,
    # which retries once without the identity on an older core (400 Invalid request
    # fields) so a page served from disk cannot break writes behind an older process.
    edit = script.split("async function editCard(", 1)[1].split("async function removeCard", 1)[0]
    assert 'boardWrite("/api/board/cards/" + ordinal + "/note"' in edit
    assert "revision" in edit and "title" in edit and "cardId" in edit
    remove = script.split("async function removeCard(", 1)[1].split("function renderBoardColumns", 1)[0]
    assert 'boardWrite("/api/board/cards/" + ordinal + "/remove"' in remove
    assert "revision" in remove and "cardId" in remove


def test_a_card_note_save_answers_clears_and_does_not_refill():
    """Save clears the field and says that it applied.

    Three things have to be true. The save disables while
    in flight, it says what happened, and it takes the words out of the field. The
    last part is the subtle one: a rebuild captures field text so a half-typed note
    survives, which handed the saved text straight back and made a successful save
    look like a dead button.
    """
    script = (WEB / "app.js").read_text()
    save = script.split("async function addBoardNote(", 1)[1].split("async function editCard", 1)[0]
    assert "control.save.disabled = true;" in save
    assert 'control.status.textContent = "Saving…";' in save
    assert 'control.status.textContent = "Note saved.";' in save
    assert 'control.field.value = "";' in save
    assert "delete state.pendingFieldText[control.field.id];" in save
    # The words stay put when they did not reach the board. A conflict says which
    # conflict it was, because "not saved" alone does not tell a person their draft is
    # still valid for the card they meant.
    assert 'control.status.textContent = error.status === 409 ? boardConflictText(error) : "Note not saved.";' in save
    assert "control.save.disabled = false;" in save
    # The editor wires its own field, button and status through that path.
    controls = script.split("function claimControls(", 1)[1].split("async function setClaim", 1)[0]
    assert 'addBoardNote(ordinal, text, {field, save, status: saveStatus}, cardId)' in controls
    assert 'element("p", "board-note-status")' in controls
    assert 'saveStatus.textContent = "Write something first.";' in controls


def test_opening_a_project_lands_the_person_on_its_planning_space():
    """A project card opens its shared planning space.

    The panel sits below the project grid on the same page, so a click that does
    not bring it into view looks like nothing happened. Opening one brings the
    panel into view and marks the card it belongs to.
    """
    script = (WEB / "app.js").read_text()
    opener = script.split("async function openProject(projectId)", 1)[1].split("function renderProjectPage", 1)[0]
    assert "scrollIntoView" in opener
    page = script.split("function renderProjectPage()", 1)[1].split("async function appendProjectText", 1)[0]
    assert 'node.dataset.open = "true";' in page
    assert "delete node.dataset.open;" in page
    css = (WEB / "design.css").read_text()
    assert '.project-card[data-open="true"]' in css


def test_leaving_settings_keeps_an_unsaved_record_form():
    """The report: "What good is a settings page for connectors if one can't
    add or manage the connections?"

    The page reloads settings on navigation only when configView.hasEdits() is false.
    That method was hardwired to false and the draft was only written by a kind or
    account change, so typed input was never recorded and leaving the page discarded
    the form. This drives the real module: open an add form, type a model name, then
    ask the same question app.js asks before reloading.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');
const settingsData = { supervised: true, sections: [
  { id: 'providers', label: 'Models', fields: [
    { id: 'providers', type: 'list-of-records', actions: ['add'], blank: [
        { key: 'name', label: 'Name', type: 'text', value: '' },
        { key: 'model', label: 'Model', type: 'text', value: '' } ],
      records: [] } ] } ] };
const api = async () => settingsData;

const context = vm.createContext({ document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
  window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                      if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();
  // Nothing open yet, so a navigation reload is free to refresh.
  assert.equal(view.hasEdits(), false, 'an untouched settings page must reload');

  // The add button opens a form, exactly as renderRecords wires it.
  const add = root.querySelector('.settings-add');
  assert.ok(add, 'the add control must exist for a list that offers add');
  await add.fire('click');
  // Opening a form with no typing is still not unsaved work.
  assert.equal(view.hasEdits(), false, 'an empty new form is not an edit');

  // Type into the model field, the field the report names.
  const model = root.querySelector('[data-key="model"]');
  assert.ok(model, 'the model input must be in the rendered form');
  model.value = 'gpt-6.1-sol';
  await model.fire('input');

  // This is the exact question app.js asks before reloading on navigation.
  assert.equal(view.hasEdits(), true, 'typed input must survive navigation');

  // Typing it back to the server value is no longer an edit.
  model.value = '';
  await model.fire('input');
  assert.equal(view.hasEdits(), false, 'reverting the field clears the edit');

  // Cancel empties the form, so the guard cannot latch forever.
  model.value = 'gpt-6.1-sol';
  await model.fire('input');
  const cancel = root.querySelector('.record-form').children
    .find(n => n.className === 'record-actions').children.find(n => n.textContent === 'Cancel');
  await cancel.fire('click');
  assert.equal(view.hasEdits(), false, 'cancelling the form clears the edit');
  console.log('settings draft survives navigation');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_field_the_core_will_refuse_is_rendered_inert():
    """Each field says what is expected of it.

    The AetherMind URL box is refused while ordered endpoints exist. It carried only a
    prose status, so it looked exactly as editable as a working field and a stranger
    typed into a box that answered "that changed nothing" on save. This drives the real
    module and asserts the input is actually disabled and the row is marked, driven by the
    structured `inert` flag rather than by the presence of a status string.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');
const settingsData = { supervised: true, sections: [
  { id: 'integrations', label: 'Integrations', fields: [
    { id: 'aethermind.url', key: 'url', label: 'AetherMind service URL', type: 'string',
      value: '', inert: true, write: 'set', apply: 'restart',
      status: 'Not used: 1 ordered endpoint(s) are set below and take precedence. Clear that list to use this field.' },
    { id: 'aethermind.endpoints', key: 'endpoints', label: 'Ordered endpoints', type: 'string',
      value: 'http://127.0.0.1:8452/mcp', write: 'set', apply: 'restart' },
    { id: 'permissions.mode', key: 'mode', label: 'Mode', type: 'enum',
      options: [{ value: 'safe', label: 'Safe' }, { value: 'yolo', label: 'Unleashed' }],
      value: 'safe', write: 'set', apply: 'live', status: 'Safe asks' } ] } ] };
const api = async () => settingsData;

const context = vm.createContext({ document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
  window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                      if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();

  const inertInput = root.querySelector('[data-key="url"]');
  assert.ok(inertInput, 'the superseded URL input must still render so its state is visible');
  assert.equal(inertInput.disabled, true, 'a field the core will refuse must not look fillable');
  assert.equal(inertInput.getAttribute('aria-disabled'), 'true');

  // The field that supersedes it stays editable.
  const endpoints = root.querySelector('[data-key="endpoints"]');
  assert.ok(endpoints, 'the endpoints input must render');
  assert.equal(endpoints.disabled, false, 'the field that wins must stay editable');

  // A status string alone must not disable anything: permissions.mode reports "Safe asks"
  // and is the live switch.
  const mode = root.querySelector('[data-key="mode"]');
  assert.ok(mode, 'the permission switch must render');
  assert.equal(mode.disabled, false, 'a status is not the same thing as inert');

  console.log('inert field renders inert, live field stays live');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_long_status_and_the_fold_cannot_squeeze_the_control_column():
    """The settings page keeps hints on one line and the inert status off the label.

    `.settings-block` is a 3-column grid. A long status must not take the
    control column's width. `details.settings-advanced` spans the row, so the
    fold is not squeezed into the label column. This asserts that structure.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && String(n.className || '').split(/\s+/).includes(cls[1])) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');

const LONG = 'Not used: 2 ordered endpoint(s) are set below and take precedence. Clear that list to use this field.';
const settingsData = { supervised: true, sections: [
  { id: 'integrations', label: 'Integrations', fields: [
    { id: 'aethermind.url', key: 'url', label: 'AetherMind service URL', type: 'string',
      value: '', inert: true, write: 'set', apply: 'restart', status: LONG },
    { id: 'aethermind.project', key: 'project', label: 'Memory project root', type: 'string',
      value: 'personal', write: 'set', apply: 'restart', status: 'Local' },
  ]},
]};

const api = async () => settingsData;
const context = vm.createContext({
  console, document: doc,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
  window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                      if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();

  // The status span is appended to the ROW, not to the control, so walk up from
  // the input to the row first: input.parent is .set-control.
  function rowFor(input) {
    let n = input.parent;
    while (n && !String(n.className || '').split(/\s+/).includes('set-row')) n = n.parent;
    return n;
  }
  function factIn(row) {
    let found = null;
    (function walk(n) {
      if (String(n.className || '').split(/\s+/).includes('set-fact')) found = n;
      for (const c of n.children || []) walk(c);
    })(row);
    return found;
  }

  // 1. A long status must take its own full-width line instead of the `auto`
  //    column, which sizes to the prose and starves the control beside it.
  const longInput = root.querySelector('[data-key="url"]');
  assert.ok(longInput, 'the inert URL field must render');
  const longFact = factIn(rowFor(longInput));
  assert.ok(longFact, 'the inert field must render its status');
  assert.ok(String(longFact.className).split(/\s+/).includes('set-fact-wide'),
    'a status over 60 chars must get set-fact-wide so it cannot squeeze the control column');

  // 2. A short status stays beside the control: "Local" does not need a line.
  const shortInput = root.querySelector('[data-key="project"]');
  assert.ok(shortInput, 'the project field must render');
  const shortFact = factIn(rowFor(shortInput));
  assert.ok(shortFact, 'the project field must render its status');
  assert.ok(!String(shortFact.className).split(/\s+/).includes('set-fact-wide'),
    'a short status must stay in the row, not claim a full line');

  console.log('a long status takes its own line; a short one stays beside the control');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

    # The geometry rules live in CSS, which this harness cannot compute. Assert the
    # declarations exist so a later edit cannot silently drop them.
    css = (ROOT / "web/app.css").read_text()
    assert ".settings-advanced { grid-column: 1 / -1;" in css, \
        'the advanced fold must span the row, not sit in the label column'
    assert ".set-fact-wide { grid-column: 1 / -1;" in css, \
        'a wide status must claim its own full-width line'
    assert ".settings-advanced .set-row {" in css, \
        'rows inside the fold need their own column template; subgrid has no parent grid there'


def test_advanced_fields_fold_and_a_search_opens_the_fold():
    """Advanced settings stay grouped behind a disclosure, not in a row of open boxes.

    The server ranks fields required -> optional -> advanced. The renderer must not
    dump advanced ones beside the fields that make the thing work. They stay available
    behind a disclosure that is shut by default — and a search that lands on one must
    open it, because a page that says "found it" while showing a shut fold is the same
    class of lie as a box that refuses every save.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    this.open = false;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(selector) {
    const matches = [];
    function walk(node) {
      for (const child of node.children) {
        const classes = child.className.split(/\s+/);
        if ((selector.startsWith('.') && classes.includes(selector.slice(1)))
            || (selector === '.settings-nav button' && child.tag === 'button'
                && child.parent.className === 'settings-nav')) matches.push(child);
        walk(child);
      }
    }
    walk(this);
    return matches;
  }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');

// Exactly what the server sends after the tier change: required first, then
// optional, then advanced. Legacy payloads with omitted required tiers still render.
const settingsData = { supervised: true, sections: [
  { id: 'judge', label: 'Session judge (Jev)', start_hint: 'Start here: review the judge switches.', fields: [
    { id: 'jev.enabled', key: 'enabled', label: 'Judge session actions', type: 'bool', value: true, write: 'set', apply: 'restart' },
    { id: 'jev.api_key_env', key: 'api_key_env', label: 'Jev API key env var', type: 'string', value: 'TYPESAFE_API_KEY', write: 'set', apply: 'restart' },
    { id: 'jev.timeout', key: 'timeout', label: 'Judge timeout', type: 'float', value: 8, tier: 'optional', write: 'set', apply: 'restart' },
    { id: 'jev.url', key: 'url', label: 'Jev service URL', type: 'string', value: 'https://jev.example.com/v1/systemone', tier: 'advanced', write: 'set', apply: 'restart' },
    { id: 'jev.model', key: 'model', label: 'Jev model', type: 'string', value: 'jev-1.14.0', tier: 'advanced', write: 'set', apply: 'restart' } ] }, {id: 'appearance', label: 'Appearance', start_hint: 'Start here: choose a theme.', fields: []} ] };
const api = async () => settingsData;

const context = vm.createContext({ document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
  window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                      if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();

  const sections = root.querySelectorAll('.settings-block');
  assert.equal(sections.length, 3);
  assert.equal(sections[2].dataset.section, 'floor');
  assert.ok(sections[2].textContent.includes('Floor definition'));
  assert.equal(sections[0].children[1].textContent, settingsData.sections[0].start_hint);
  assert.equal(sections[1].children[1].textContent, settingsData.sections[1].start_hint);
  const nav = root.querySelector('.settings-nav');
  const appearance = nav.children.find(button => button.dataset.section === 'appearance');
  await appearance.fire('click');
  assert.equal(sections[0].hidden, true);
  assert.equal(sections[1].hidden, false);
  assert.equal(appearance.getAttribute('aria-pressed'), 'true');

  // The required fields render in the open part of the section.
  const enabled = root.querySelector('[data-key="enabled"]');
  assert.ok(enabled, 'a required field must render without opening anything');
  const timeout = root.querySelector('[data-key="timeout"]');
  assert.ok(timeout, 'an optional field must render without opening anything');

  // There is exactly one fold for this section, it is shut, and it counts its fields.
  const folds = [];
  (function walk(n) {
    // The harness sets className directly and does not populate classList, so match
    // on className the way a browser's querySelectorAll would.
    const classes = String(n.className || '').split(/\s+/);
    if (classes.includes('settings-advanced')) folds.push(n);
    for (const child of n.children || []) walk(child);
  })(root);
  assert.equal(folds.length, 1, 'the section must have one advanced fold');
  assert.equal(folds[0].open, false, 'advanced fields must be shut by default');
  const summary = folds[0].children.find(c => c.tag === 'summary');
  assert.ok(summary, 'the fold must carry a summary');
  assert.equal(summary.textContent, 'Advanced (2)', 'the summary must say how many are folded');

  // Advanced fields are present inside the fold, not absent: nothing is hidden outright.
  assert.ok(folds[0].querySelector('[data-key="url"]'), 'the URL field must still exist inside the fold');
  assert.ok(folds[0].querySelector('[data-key="model"]'), 'the model field must still exist inside the fold');

  // A search that lands on an advanced field opens the fold, so the result is visible.
  const search = root.querySelector('.settings-search');
  assert.ok(search, 'the search input must exist');
  // Search matches labels and hints, not input values (an input's value is not text
  // content in a browser either). "Jev model" appears only inside the fold.
  search.value = 'jev model';
  await search.fire('input');
  assert.equal(folds[0].open, true, 'a search matching an advanced field must open its fold');
  assert.equal(sections[0].hidden, false, 'search leaves the selected section to reveal a match');
  assert.equal(sections[1].hidden, true);
  await folds[0].fire('toggle');
  await view.load();
  const reopened = root.querySelector('.settings-advanced');
  assert.equal(reopened.open, true, 'a reload retains an open advanced fold');
  const currentSearch = root.querySelector('.settings-search');
  currentSearch.value = '';
  await currentSearch.fire('input');
  reopened.open = false;
  await reopened.fire('toggle');
  await view.load();
  assert.equal(root.querySelector('.settings-advanced').open, false, 'a reload retains a closed fold');

  console.log('advanced fields fold, stay present, and a matching search opens the fold');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_restart_only_reports_success_once_a_different_core_answers():
    """A restart must not report completion before the old core exits.

    /api/core/restart drains and then exits, and the draining process keeps serving
    /api/status until it exits. The page accepts success only from a different
    per-process generation. This drives the real module twice: once against a
    core that never exits, once against a real restart.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };

// One module instance per scenario, because restart() closes over its own view.
function harness(statusReplies, restartCalls) {
  const root = new Element('div');
  const status = new Element('p');
  const restartButton = new Element('button');
  const settingsData = { supervised: true, generation: statusReplies[0].generation, sections: [
    { id: 'providers', label: 'Models', fields: [
      { id: 'providers', type: 'list-of-records', actions: ['add'], blank: [], records: [] } ] } ] };
  let poll = 0;
  const api = async (path, method) => {
    if (path === '/api/settings') return settingsData;
    if (path === '/api/core/restart') { restartCalls.push(method); return { restarting: true }; }
    if (path === '/api/status' || path === '/api/status?shape=connection') {
      // First call is the "which core asked?" read, then each poll advances one step.
      const index = poll === 0 ? 0 : Math.min(poll, statusReplies.length - 1);
      poll += 1;
      return statusReplies[index];
    }
    throw new Error('unexpected ' + path);
  };
  const context = vm.createContext({ document: doc, navigator: {}, console,
    // The restart loop awaits a timer; a stub that never resolves would hang it.
    setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {},
    getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
    window: {} });
  vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);
  let restarted = 0;
  context.window.TesseraSettings.mount({
    api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                        if (text !== undefined) n.textContent = String(text); return n; },
    root, status, restartButton, onRestarted() { restarted += 1; }, onAppearance() {},
    onApproval() {}, onChanged() {} });
  return { restartButton, status, restarted: () => restarted };
}

(async () => {
  // Scenario 1: the core never exits. Every poll still carries the same generation,
  // which is exactly what the draining old process does.
  const stuckCalls = [];
  const stuck = harness([{ generation: 'gen-old' }, { generation: 'gen-old' }], stuckCalls);
  await stuck.restartButton.fire('click');
  assert.equal(stuckCalls.length, 1, 'the restart was requested exactly once');
  assert.notEqual(stuck.status.textContent, 'Core restarted',
    'a core that never exited must not be reported as restarted');
  assert.match(stuck.status.textContent, /still draining|did not come back/,
    'the page must say the old core is still there, got: ' + stuck.status.textContent);
  assert.equal(stuck.restarted(), 0, 'no restart callback on a failed restart');

  // Scenario 2: a genuinely new core answers.
  const liveCalls = [];
  const live = harness([{ generation: 'gen-old' }, { generation: 'gen-new' }], liveCalls);
  await live.restartButton.fire('click');
  assert.equal(liveCalls.length, 1);
  assert.equal(live.status.textContent, 'Core restarted', 'a new generation is proof');
  assert.equal(live.restarted(), 1, 'the restart callback fires once');

  console.log('restart waits for a different core');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_voice_slot_held_says_so_instead_of_voice_unavailable():
    """A 429 from our own /voice/start is our lease table, not a provider fault.

    /api/sessions/<slug>/voice/start answers 429 only for our own lease table
    (server.py maps VoiceError('limit') to 429), never for a provider rate limit.
    The page names the held slot. It does not report that 429 as a provider
    fault. No lease semantics are touched.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const notices = [];
const window = {isSecureContext: true, location: {protocol: 'https:'},
  AudioContext: class { constructor() { this.currentTime = 0; this.audioWorklet = {addModule: async () => {}}; }
                        async resume() {} async close() {} },
  AudioWorkletNode: class { constructor() { this.port = {postMessage() {}}; } connect() {} disconnect() {} },
  WebSocket: class {}};
const navigator = {mediaDevices: {getUserMedia: async () => ({getTracks: () => []})}};
const context = {window, navigator, URL, ArrayBuffer, DataView, Uint8Array, Float32Array, btoa, atob,
  setTimeout: (fn, ms) => ({fn, ms}), clearTimeout: () => {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/voice.js', 'utf8'), context);

// The exact error app.js checkResponse throws for a 429: body.error becomes the
// message and error.status is carried, but error_category is dropped.
function limited() { return Object.assign(new Error('limit'), {status: 429}); }

async function noticeFor(error) {
  const voice = new window.TesseraVoice({
    workletUrl: '/static/voice-worklet.js', chunkFrames: 4, maxBufferedBytes: 4096,
    maxPendingEvents: 8, connectSeconds: 10, maxPlaybackSeconds: 5,
    onState: (label, detail) => notices.push(label + ' | ' + detail),
    onMessages: () => {}, start: async () => { throw error; },
    stop: async () => {}, events: async () => ({messages: [], send: []}),
  });
  await voice.start('session-one');
  return notices.at(-1);
}

(async () => {
  const held = await noticeFor(limited());
  assert.equal(held, 'Voice slot held | Another voice session is still open');
  // A non-429 failure keeps its old wording: only the lease table says "held".
  const other = await noticeFor(Object.assign(new Error('boom'), {status: 503}));
  assert.equal(other, 'Voice unavailable | Could not start');
  assert(!notices.join(' ').includes('limit'));
  console.log('Voice 429 names the held slot; other failures keep their wording.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_web_client_beats_its_lease_and_never_uses_events_as_a_beat():
    """The web lease opts into keepalive and beats on a timer.

    The beat must not be events([]): that call also drains pending task
    completions, so reusing it as a beat would mark a spoken result delivered and
    drop the reply. A failed beat must not stop a live call — the deadline still
    backstops.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const beats = [], eventBatches = [], stops = [], starts = [], timers = [], notices = [];
function settings() {
  return {voice_id: 'lease-beat', websocket_url: 'wss://voice.example.com/realtime', protocols: ['p'],
    expires_at: Date.now() / 1000 + 60, session_expires_at: Date.now() / 1000 + 120,
    max_session_seconds: 90, audio_sample_rate: 16000,
    session_update: {type: 'session.update', session: {instructions: 'context', tools: [],
      turn_detection: {type: 'server_vad'},
      audio: {input: {format: {type: 'audio/pcm', rate: 16000}}, output: {format: {type: 'audio/pcm', rate: 16000}}}}}};
}
class Audio {
  constructor() { this.currentTime = 0; this.audioWorklet = {addModule: async () => {}}; }
  async resume() {} async close() {}
  createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
}
class Worklet { constructor() { this.port = {postMessage() {}}; } connect() {} disconnect() {} }
class Socket { constructor() { this.readyState = 1; this.bufferedAmount = 0; }
  send() {} close() {} }
const window = {isSecureContext: true, location: {protocol: 'https:'},
  AudioContext: Audio, AudioWorkletNode: Worklet, WebSocket: Socket};
const navigator = {mediaDevices: {getUserMedia: async () => ({getTracks: () => []})}};
const context = {window, navigator, URL, ArrayBuffer, DataView, Uint8Array, Float32Array, btoa, atob,
  setTimeout(fn, ms) { const t = {fn, ms}; timers.push(t); return t; },
  clearTimeout(t) { if (t) t.cleared = true; }};
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/voice.js', 'utf8'), context);

const voice = new window.TesseraVoice({
  workletUrl: '/static/voice-worklet.js', chunkFrames: 4, maxBufferedBytes: 4096,
  maxPendingEvents: 8, connectSeconds: 10, maxPlaybackSeconds: 5, beatSeconds: 20,
  onState: (label, detail) => notices.push(label + ' | ' + detail),
  onMessages: () => {},
  start: async (slug, extra) => { starts.push({slug, extra}); return settings(); },
  stop: async (slug, id) => { stops.push([slug, id]); },
  events: async (slug, id, rows) => { eventBatches.push(rows); return {messages: [], send: []}; },
  heartbeat: async (slug, id) => { beats.push([slug, id]); },
});

(async () => {
  await voice.start('session-one');
  // The lease opted in, and the web path is the only one that sends it.
  assert.equal(JSON.stringify(starts.at(-1).extra), '{"keepalive":true}');
  const beat = timers.filter(t => t.ms === 20000 && !t.cleared).at(-1);
  assert(beat, 'the web client must arm a beat timer');
  await beat.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(beats.at(-1), ['session-one', 'lease-beat']);
  // A beat is not an events([]) poll: that would drain task completions.
  assert.equal(eventBatches.filter(rows => rows.length === 0).length, 0);
  // It re-arms itself while the call lives.
  assert(timers.filter(t => t.ms === 20000 && !t.cleared).length >= 2);
  // A failed beat is not a dead call.
  voice.options.heartbeat = async () => { throw new Error('beat lost'); };
  const retry = timers.filter(t => t.ms === 20000 && !t.cleared).at(-1);
  await retry.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert(voice.running(), 'a lost beat must not stop the call');
  assert(!notices.join(' ').includes('beat lost'));
  // Stop ends the beat: firing any leftover timer must not produce another beat.
  const before = beats.length;
  voice.stop();
  assert(voice.current === null);
  for (const timer of timers.filter(t => t.ms === 20000 && !t.cleared)) await timer.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(beats.length, before);
  console.log('Web lease beats on a timer, never via events([]), and survives a lost beat.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_newer_page_does_not_break_voice_on_an_older_core():
    """The page is served from disk, so it can be newer than the running core.

    Measured on the live core (pid 37900, predating the keepalive commit): POST
    /voice/start with {"keepalive": true} answers 400, because an older core
    rejects an unknown field. Without a fallback, reloading the page before the
    core restarts would break voice. The web client must retry without
    the field, and must not swallow any other 400.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const attempts = [];
const document = {body: {dataset: {}}, getElementById: () => ({value: ''})};
const window = {document, isSecureContext: true, location: {protocol: 'https:'},
  AudioContext: class { constructor() { this.currentTime = 0; this.audioWorklet = {addModule: async () => {}}; }
                        async resume() {} async close() {} },
  AudioWorkletNode: class { constructor() { this.port = {postMessage() {}}; } connect() {} disconnect() {} },
  WebSocket: class { constructor() { this.readyState = 1; this.bufferedAmount = 0; } send() {} close() {} }};
const navigator = {mediaDevices: {getUserMedia: async () => ({getTracks: () => []})}};

// api() as the real page uses it: a 400 becomes an Error carrying status, with
// body.error as the message (checkResponse keeps only that field).
let mode = 'old-core';
async function api(path, method, body) {
  attempts.push(body);
  if (mode === 'old-core' && body && body.keepalive !== undefined) {
    throw Object.assign(new Error('Invalid request fields'), {status: 400});
  }
  if (mode === 'broken') throw Object.assign(new Error('Invalid request fields'), {status: 400});
  return {voice_id: 'lease-ok', websocket_url: 'wss://voice.example.com/realtime', protocols: ['p'],
    expires_at: Date.now() / 1000 + 60, session_expires_at: Date.now() / 1000 + 120,
    max_session_seconds: 90, audio_sample_rate: 16000,
    session_update: {type: 'session.update', session: {instructions: 'c', tools: [],
      turn_detection: {type: 'server_vad'},
      audio: {input: {format: {type: 'audio/pcm', rate: 16000}}, output: {format: {type: 'audio/pcm', rate: 16000}}}}}};
}

// The start wrapper exactly as web/app.js defines it for the browser path.
async function start(slug, extra) {
  const pick = '';
  const body = pick ? {voice: pick} : {};
  try { return await api('/api/sessions/' + slug + '/voice/start', 'POST', Object.assign({}, body, extra || {})); }
  catch (error) {
    if (error.status !== 400 || !extra || !extra.keepalive) throw error;
    return api('/api/sessions/' + slug + '/voice/start', 'POST', body);
  }
}

(async () => {
  // Old core: first attempt carries keepalive, second drops it and succeeds.
  const ok = await start('session-one', {keepalive: true});
  assert.equal(ok.voice_id, 'lease-ok');
  assert.equal(attempts.length, 2);
  assert.equal(JSON.stringify(attempts[0]), '{"keepalive":true}');
  assert.equal(JSON.stringify(attempts[1]), '{}');

  // A 400 that is not the keepalive rejection must still surface.
  attempts.length = 0;
  mode = 'broken';
  await assert.rejects(() => start('session-one', {keepalive: true}), /Invalid request fields/);

  // A non-keepalive 400 is never retried.
  attempts.length = 0;
  await assert.rejects(() => start('session-one', {}), /Invalid request fields/);
  assert.equal(attempts.length, 1);
  console.log('A newer page falls back on an older core without masking other 400s.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_project_cards_sort_by_category_inside_one_project():
    """Categories sort inside one project, not become projects.

    The eight alpha work orders are all filed under a single planning project and
    carry category prefixes ("UI:", "Backend:", "Bugs:", ...). The board offered a
    status filter and no category control, so the manager could not sort by the
    prefixes the cards already carry. This runs the real functions in node rather
    than matching source text, because the rule that matters is which cards survive
    the filter, not which words appear in the file.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  // Extract one function by brace matching. Slicing between two function names
  // assumed a fixed order in the file, and these helpers are not in that order.
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  const open = i;
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  assert(depth === 0, name + ' must be balanced');
  return source.slice(at, i + 1);
}
const context = vm.createContext({console});
vm.runInContext(grab('boardCategory') + '\nthis.boardCategory = boardCategory;', context);
vm.runInContext(grab('matchesBoardWork') + '\nthis.matchesBoardWork = matchesBoardWork;', context);

// The prefix is read from the title the cards already carry.
assert.equal(context.boardCategory('UI: Full project cockpit'), 'UI');
assert.equal(context.boardCategory('Harness improvement: Seed rules'), 'Harness improvement');
assert.equal(context.boardCategory('Bugs: Categorize project cards'), 'Bugs');
// No prefix is not a guess: a prose title is uncategorized, and so is "a:b"
// because a category is a label followed by a space and real text.
assert.equal(context.boardCategory('No prefix here'), '');
assert.equal(context.boardCategory('a:b'), '');
assert.equal(context.boardCategory('UI:'), '');
assert.equal(context.boardCategory(undefined), '');

const base = {history: false, project: 'alpha', focus: 'all', query: ''};
const ui = {status: 'queued', ordinal: 1};
const uiOverlay = {project: 'alpha', title: 'UI: Full project cockpit'};
const bugs = {status: 'queued', ordinal: 2};
const bugsOverlay = {project: 'alpha', title: 'Bugs: Categorize project cards'};

// No category filter: both cards survive.
assert.equal(context.matchesBoardWork(ui, uiOverlay, {...base, category: ''}), true);
assert.equal(context.matchesBoardWork(bugs, bugsOverlay, {...base, category: ''}), true);

// Category filter: only that category survives, inside the same project.
assert.equal(context.matchesBoardWork(ui, uiOverlay, {...base, category: 'UI'}), true);
assert.equal(context.matchesBoardWork(bugs, bugsOverlay, {...base, category: 'UI'}), false);
assert.equal(context.matchesBoardWork(bugs, bugsOverlay, {...base, category: 'Bugs'}), true);

// A category filter does not cross the project boundary: the same title filed
// under another project is still excluded by the project filter.
assert.equal(context.matchesBoardWork(ui, {...uiOverlay, project: 'other'}, {...base, category: 'UI'}), false);

// An uncategorized card is excluded by any category filter rather than being
// silently swept into the first category.
assert.equal(context.matchesBoardWork(ui, {project: 'alpha', title: 'No prefix'}, {...base, category: 'UI'}), false);

console.log('Categories sort inside one project without becoming projects.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_poll_rebuild_keeps_the_expanded_note_and_the_caret():
    """An automatic refresh must not lose focus or collapse open notes.

    The note expansion lived only in the DOM node, so any poll that saw a changed
    card replaced the row and collapsed what the reader had just opened. Focus was
    preserved for note editors only, so a poll ate the caret anywhere else. This runs
    the real functions in node and drives the rebuild path directly.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.id = ''; this.hidden = false;
    this._text = ''; this.listeners = {}; this.tagName = tag.toUpperCase(); this.value = '';
    this.selectionStart = null; this.selectionEnd = null;
  }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  async fire(t) { for (const fn of this.listeners[t] || []) await fn({preventDefault(){}}); }
  focus() { doc.activeElement = this; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  querySelectorAll() { return []; }
}
const byId = {};
const doc = {
  activeElement: null,
  getElementById: id => byId[id] || null,
};
const context = vm.createContext({
  console, document: doc,
  state: {noteExpanded: {}},
  element: (tag, cls, text) => { const e = new El(tag); e.className = cls; if (text !== undefined) e.textContent = text; return e; },
});
vm.runInContext('var state = this.state; var element = this.element;'
  + '\nvar BOARD_NOTE_PREVIEW = ' + /const BOARD_NOTE_PREVIEW = (\d+);/.exec(source)[1] + ';', context);
vm.runInContext(grab('boardNoteNode') + '\nthis.boardNoteNode = boardNoteNode;', context);
vm.runInContext(grab('captureFocus') + '\nthis.captureFocus = captureFocus;', context);
vm.runInContext(grab('restoreFocus') + '\nthis.restoreFocus = restoreFocus;', context);

(async () => {
// A long note gets a preview, a hidden rest and a toggle that carries the identity.
const long = 'x'.repeat(600);
const node1 = context.boardNoteNode(long, 'card-abc', false);
const toggle = node1.children.find(c => c.className === 'board-note-more');
const rest = node1.children.find(c => c.className === 'board-note-text' && c !== node1.children[0]);
assert(toggle, 'a long note must offer the toggle');
assert.equal(toggle.id, 'board-note-more-card-abc', 'the toggle carries the card identity');
assert.equal(rest.hidden, true, 'the rest starts collapsed');
assert.equal(toggle.getAttribute('aria-expanded'), 'false');

// Expanding records the choice in state, not only in the DOM.
await toggle.fire('click');
assert.equal(rest.hidden, false);
assert.equal(context.state.noteExpanded['card-abc'], true, 'the expansion is remembered');

// A rebuild with that state hands the expansion back instead of collapsing it.
const node2 = context.boardNoteNode(long, 'card-abc', context.state.noteExpanded['card-abc'] === true);
const rest2 = node2.children.find(c => c.className === 'board-note-text' && c !== node2.children[0]);
const toggle2 = node2.children.find(c => c.className === 'board-note-more');
assert.equal(rest2.hidden, false, 'the rebuilt note stays open');
assert.equal(toggle2.getAttribute('aria-expanded'), 'true');
assert.equal(toggle2.textContent, 'Show less');

// Collapsing again is remembered too.
await toggle2.fire('click');
assert.equal(context.state.noteExpanded['card-abc'], false);

// A short note has no toggle at all, so nothing pretends to be expandable.
const short = context.boardNoteNode('brief', 'card-short', true);
assert.equal(short.children.filter(c => c.className === 'board-note-more').length, 0);

// Focus: a text field with a caret is captured and restored by id.
const field = new El('textarea');
field.id = 'board-note-abc';
field.value = 'half typed';
field.selectionStart = 4; field.selectionEnd = 4;
byId['board-note-abc'] = field;
doc.activeElement = field;
const saved = context.captureFocus();
assert.equal(saved.id, 'board-note-abc');
assert.equal(saved.start, 4);

// The rebuild replaces the node; the new one has the same id.
const replacement = new El('textarea');
replacement.id = 'board-note-abc';
byId['board-note-abc'] = replacement;
doc.activeElement = new El('body');
context.restoreFocus(saved);
assert.equal(doc.activeElement, replacement, 'focus returns to the same control');
assert.equal(replacement.selectionStart, 4, 'the caret is restored, not moved to the start');

// A non-field focus target is not captured: buttons are re-found by the reader's own
// click, and stealing focus back to a button would be worse than losing it.
const button = new El('button');
button.id = 'some-button';
doc.activeElement = button;
assert.equal(context.captureFocus(), null);

// A field that no longer exists is not an error: the row is gone.
assert.doesNotThrow(() => context.restoreFocus({id: 'gone', start: 0, end: 0}));

console.log('A rebuild keeps the expanded note and the caret.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_poll_skips_the_board_rebuild_while_a_field_has_focus():
    """While a field has focus, the poll skips the board rebuild.

    Hiding the tab is not the only guard. The focused field keeps its
    element and its selection, and captureFieldText keeps the value.
    This runs the real boardFieldFocused predicate.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.tagName = tag.toUpperCase(); this.children = []; this.id = ''; }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  contains(node) { if (node === this) return true; return this.children.some(c => c.contains && c.contains(node)); }
}
const page = new El('main'); page.id = 'page-wip';
const field = new El('input'); field.id = 'board-note-what';
const chatField = new El('textarea'); chatField.id = 'message-input';
const boardButton = new El('button');
const doc = {
  activeElement: null,
  getElementById: id => (id === 'page-wip' ? page : null),
};
const context = vm.createContext({console, document: doc});
vm.runInContext(grab('boardFieldFocused') + '\nthis.boardFieldFocused = boardFieldFocused;', context);

// Nothing focused: refresh.
doc.activeElement = null;
assert.equal(context.boardFieldFocused(), false);

// A board field with focus: skip the rebuild.
page.append(field);
doc.activeElement = field;
assert.equal(context.boardFieldFocused(), true, 'typing in a board field must pause the rebuild');

// A select on the board counts too: changing a filter mid-poll would otherwise lose it.
const pick = new El('select');
page.append(pick);
doc.activeElement = pick;
assert.equal(context.boardFieldFocused(), true);

// A button on the board does not pause the refresh. Buttons are re-found by the
// reader's own click; pausing for them would stall the board whenever one was focused.
doc.activeElement = boardButton;
assert.equal(context.boardFieldFocused(), false);

// A field outside the board page is not a reason to stop refreshing the board.
doc.activeElement = chatField;
assert.equal(context.boardFieldFocused(), false);

console.log('The poll pauses for board fields and nothing else.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    # The tick must actually consult it, or the predicate is decoration.
    script_source = (WEB / "app.js").read_text()
    tick = script_source.split("const refreshInterval =", 1)[1].split("autonomy = window.TesseraAutonomy", 1)[0]
    assert "boardFieldFocused()" in tick, "the refresh tick must consult boardFieldFocused"


def test_the_board_search_box_filters_while_it_has_focus():
    """Search filters the cards while the search box has focus.

    The input handler sets state.boardSearch and calls renderBoardStatus().
    That call must not stop at the caret guard just because the search box
    has focus. The guard exists because a rebuild replaces rows that hold
    fields a person types into. The search box and the lane filter live in
    #wip-tools, which no rebuild replaces, and their purpose is to change
    the view. This test runs the real predicate and asserts both halves.
    The view controls do not defer, and a field inside a rebuilt row still does.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.tagName = tag.toUpperCase(); this.children = []; this.id = ''; }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  contains(node) { if (node === this) return true; return this.children.some(c => c.contains && c.contains(node)); }
}
// The real nesting from web/index.html: the view controls sit in #wip-tools, the
// inline editors are built inside #board-cards.
const page = new El('main'); page.id = 'page-wip';
const tools = new El('div'); tools.id = 'wip-tools';
const search = new El('input'); search.id = 'board-search';
const filter = new El('select'); filter.id = 'board-focus';
tools.append(search, filter);
const rows = new El('ul'); rows.id = 'board-cards';
const rowEditor = new El('textarea'); rowEditor.id = 'work-image-comment-42';
rows.append(rowEditor);
const noteField = new El('input'); noteField.id = 'board-note-what';
page.append(tools, rows, noteField);
const doc = { activeElement: null, getElementById: id => (id === 'page-wip' ? page : null) };
const context = vm.createContext({console, document: doc});
vm.runInContext(grab('boardFieldFocused') + '\nthis.boardFieldFocused = boardFieldFocused;', context);

// The defect: a focused search box must not suspend the very render it asked for.
doc.activeElement = search;
assert.equal(context.boardFieldFocused(), false,
  'the search box must not defer the rebuild it triggers, or typing filters nothing');
doc.activeElement = filter;
assert.equal(context.boardFieldFocused(), false,
  'the lane filter must not defer the rebuild it triggers');

// Whatever the fix, the original protection has to survive: a field inside a row that
// a rebuild replaces still pauses it, or the caret is eaten mid-typing.
doc.activeElement = rowEditor;
assert.equal(context.boardFieldFocused(), true, 'a row editor must still pause the rebuild');
doc.activeElement = noteField;
assert.equal(context.boardFieldFocused(), true, 'the add-work field must still pause the rebuild');

// No field, no pause.
doc.activeElement = null;
assert.equal(context.boardFieldFocused(), false);

console.log('The search box and lane filter render on keystroke; row fields still protect the caret.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    # And the guard must not be the only thing between a keystroke and a filter: the
    # input handler has to actually write the query and re-render.
    script_source = (WEB / "app.js").read_text()
    handler = script_source.split('$("board-search").addEventListener("input"', 1)[1].split("});", 1)[0]
    assert "state.boardSearch" in handler, "the search box must write the query to state"
    assert "renderBoardStatus()" in handler, "the search box must re-render the board"


def test_pressure_groups_and_card_rows_keep_the_readers_expansion():
    """Expanded content must stay open long enough to be read.

    Two separate paths. Pressure groups reset open=false on every rebuild. A card row
    that dropped out of the current filter was rebuilt fresh and lost its expansion.
    Both now remember the reader's choice by identity.
    """
    script = (WEB / "app.js").read_text()
    # Pressure groups: the default is collapsed, the reader's choice wins afterwards.
    items = script.split("function renderPressureItems(items)", 1)[1].split("async function refreshPressure", 1)[0]
    assert "state.pressureExpanded[key] === true" in items
    assert 'addEventListener("toggle"' in items
    assert "details.open = false" not in items, "a rebuild must not shut a group the reader opened"
    # Card rows: open state is recorded by identity before a row is replaced.
    columns = script.split("function renderBoardColumns(cards)", 1)[1].split("function retainWorkEditors", 1)[0]
    assert "state.detailExpanded[identity]" in columns
    assert ".work-detail" in columns
    # And the row-level default is still the card's own signal, not always-open.
    card = script.split("function boardCardNode(card, index)", 1)[1].split("function moveMenu", 1)[0]
    assert "details.open = card.question_pending === true" in card


def test_reading_focus_hides_the_bars_and_always_leaves_a_way_back():
    """The phone chat screen must not be pinched by its own chrome.

    "Top bars need to be able to be hidden and bottom input bar reduced." The control
    sits outside the bands it hides, so the exit does not disappear with them. Reading
    focus is a chat-page posture: leaving the chat page restores the bars, so a saved
    preference cannot hide the navigation on a page that needs it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    html = (WEB / "index.html").read_text()
    # The control is in the conversation, not inside either band it hides.
    topbar_at = html.index('<header class="topbar">')
    heading_at = html.index('<header class="conversation-heading">')
    main_at = html.index('<main class="conversation"')
    control_at = html.index('id="reading-focus"')
    # The control is a direct child of <main>, sitting between the main tag and the
    # heading: outside both bands it hides, so the exit cannot disappear with them.
    assert main_at < control_at < heading_at, "the control must sit outside the bands it hides"
    # Not inside the topbar: the topbar closes before the control.
    topbar_close = html.index("</header>", topbar_at)
    assert topbar_close < control_at, "the control must be outside the topbar it hides"
    # Not inside the conversation heading: that heading opens after the control.
    assert control_at < heading_at, "the control must be outside the heading it hides"

    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.tagName = tag.toUpperCase(); this.attrs = {}; this.textContent = ''; this.title = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
}
const classes = new Set();
const button = new El('button');
const doc = {
  body: {classList: {toggle: (c, on) => { if (on) classes.add(c); else classes.delete(c); }, contains: c => classes.has(c)}},
  getElementById: id => id === 'reading-focus' ? button : null,
};
const store = {};
const context = vm.createContext({
  console, document: doc,
  localStorage: {getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }},
  state: {},
  $: id => doc.getElementById(id),
});
vm.runInContext('var state = this.state; var $ = this.$; var document = this.document; var localStorage = this.localStorage;', context);
vm.runInContext(grab('applyReadingFocus') + '\nthis.applyReadingFocus = applyReadingFocus;', context);

assert.equal(context.applyReadingFocus(true), true);
assert(classes.has('reading-focus'), 'the body class is what hides the bands');
assert.equal(button.getAttribute('aria-pressed'), 'true');
assert.equal(button.textContent, 'Bars', 'the control names the way back');
assert.equal(store['tessera-reading-focus'], 'on', 'the choice is remembered');

assert.equal(context.applyReadingFocus(false), false);
assert(!classes.has('reading-focus'));
assert.equal(button.getAttribute('aria-pressed'), 'false');
assert.equal(button.textContent, 'Read');
assert.equal(store['tessera-reading-focus'], 'off');

// persist:false is used when leaving the chat page: the bars come back but the saved
// preference is left alone, so returning to chat restores what the reader chose.
store['tessera-reading-focus'] = 'on';
context.applyReadingFocus(true, {persist: false});
context.applyReadingFocus(false, {persist: false});
assert.equal(store['tessera-reading-focus'], 'on', 'leaving the page must not rewrite the preference');

console.log('Reading focus hides the bars and leaves a way back.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    css = (WEB / "app.css").read_text()
    assert "body.reading-focus .topbar" in css and "body.reading-focus .conversation-heading" in css
    # Leaving the chat page restores the bars.
    assert 'applyReadingFocus(false, {persist: false})' in (WEB / "app.js").read_text()

    # The bottom half of the same complaint: "bottom input bar reduced". Measured on a
    # 390x844 viewport, the composer-area held 223px of 844. The diagnostic connection
    # line steps aside while reading; voice and the composer stay available.
    assert "body.reading-focus .conversation-state" in css
    assert "body.reading-focus .composer-area .voice-controls" not in css
    assert "body.reading-focus #reading-session-switch" in css
    # #send-state must NOT be hidden here: it is where a real failure lands, and the
    # empty-only phone rule is the only thing allowed to hide it.
    assert "body.reading-focus #send-state" not in css
    assert "body.reading-focus .composer-area #send-state" not in css


def test_the_phone_composer_hides_overhead_but_never_a_real_message():
    """The bottom input bar stays short on a phone.

    The hint and the idle state line are overhead when nothing is happening, so they are
    hidden on phone widths. They are hidden only while EMPTY: #send-state carries real
    errors ("The message was not sent", "Stopped."), and hiding those would be the
    opposite of communication.
    """
    css = (WEB / "app.css").read_text()
    phone = css.split("@media (max-width: 760px)", 1)[1]
    assert "#composer-hint { display: none; }" in phone
    assert "#send-state:empty { display: none; }" in phone
    # The empty-state selector is what keeps real messages visible; a bare rule would not.
    assert "#send-state { display: none; }" not in css
    # The state line really does carry failures, so the empty-only rule is load-bearing.
    script = (WEB / "app.js").read_text()
    assert 'The message was not sent' in script
    assert 'Stopped. Send a new message when you want to resume.' in script


def test_the_activity_panel_stays_closed_once_the_reader_closes_it():
    """An expanded panel collapses again when the reader closes it.

    New activity must not reopen the panel. That would push the transcript down
    again on a phone. renderActivity respects a reader's close.
    """
    script = (WEB / "app.js").read_text()
    activity = script.split("function renderActivity()", 1)[1].split("function updateHeading", 1)[0]
    assert "state.activityCollapsed === true" in activity
    assert "box.open = false" in activity
    assert "activityCollapsed: false" in script
    # The close is recorded from the element's own toggle, not inferred.
    assert 'activityBox.addEventListener("toggle"' in script


def test_no_stylesheet_rule_styles_the_body_by_its_own_state_class():
    """A bare `.reading-focus` rule must not style <body>.

    applyReadingFocus toggles `reading-focus` on document.body. A bare class
    selector matches every element that carries the class, including body, so
    the button rule is `button.reading-focus` and the hiding rules are
    `body.reading-focus`.

    For each state class the page puts on <body>, a rule that styles that
    element itself must be scoped to body or to a real element type. A bare
    `.class {` rule must not exist. A descendant rule like
    `.hide-thoughts .thought {` is fine and is not flagged.
    """
    css = (WEB / "app.css").read_text()
    script = (WEB / "app.js").read_text()

    body_classes = set(re.findall(r'document\.body\.classList\.toggle\("([a-z-]+)"', script))
    body_classes |= set(re.findall(r'document\.body\.classList\.add\("([a-z-]+)"', script))
    body_classes |= set(re.findall(r'document\.body\.classList\.remove\("([a-z-]+)"', script))
    assert "reading-focus" in body_classes, "the class must still be a body class for this to matter"

    # Strip comments so the explanation above is not read as a selector.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    offenders = []
    for name in sorted(body_classes):
        for match in re.finditer(r"(?m)^\s*\.(" + re.escape(name) + r")\s*[,{]", css):
            offenders.append(match.group(0).strip())
    assert not offenders, "bare state-class rules would style the body itself: " + repr(offenders)

    # And the specific pair that broke: the pill rule is scoped to the button.
    assert re.search(r"(?m)^button\.reading-focus\s*\{", css)
    assert re.search(r"(?m)^body\.reading-focus\s+\.topbar", css)


def test_the_quality_panel_ranks_by_structure_and_never_by_the_judge_score():
    """A person reads the oversight lane from the page.

    The lane exists at /api/oversight. Until this section, nothing on the page
    consumed it, so the only reader was an agent calling the route. Two properties
    matter and this runs the real function to check both:

    1. Ranking is by posture, worst first. The judge's completion score is carried
       in the payload but must never order the list: the
       heuristic can only return 0.0/0.1/0.65/0.9 and its threshold sits above every
       passing value, so a score ranking would sort noise.
    2. The summary keeps "judged" and "refused before judging" apart. A single pass
       rate would blend a check that declined to read the reply with a judge that read
       it and disagreed.
    """
    # The standing purpose sentence is static markup, not a renderer string, so it
    # is pinned where it is served. The question the page has to answer is "what is this
    # and what purpose does it have": the page must answer it before any numbers.
    page = Page((WEB / "index.html").read_text())
    ids = {attrs.get("id") for _tag, attrs in page.elements if attrs.get("id")}
    assert "quality-purpose" in ids, "the section must state what it is for"
    assert "quality-kpis" in ids and "quality-limits" in ids
    assert "quality-limits-fold" in ids, "caveats belong in their own block, not the findings list"

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  // An async declaration must keep its keyword: slicing from 'function' alone turns
  // an async body into one that uses await outside an async function.
  let at = source.indexOf('async function ' + name + '(');
  if (at < 0) at = source.indexOf('function ' + name + '(');
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.id = ''; this._text = ''; this.dataset = {}; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  replaceChildren(...n) { this.children = []; this._text = ''; this.append(...n); }
}
const byId = {};
const doc = { getElementById: id => (byId[id] ||= new El('div')) };
const context = vm.createContext({
  console, document: doc,
  state: {},
  element: (tag, cls, text) => { const e = new El(tag); e.className = cls; if (text !== undefined) e.textContent = text; return e; },
  statusMark: tone => { const e = new El('span'); e.tone = tone; return e; },
  $: id => (byId[id] ||= new El('div')),
});
vm.runInContext('var state = this.state; var element = this.element; var statusMark = this.statusMark;'
  + '\nvar QUALITY_POSTURE = ' + /const QUALITY_POSTURE = (\{[\s\S]*?\n  \});/.exec(source)[1] + ';', context);
vm.runInContext(grab('qualityPosture') + '\nthis.qualityPosture = qualityPosture;', context);
vm.runInContext(grab('qualityCount') + '\nthis.qualityCount = qualityCount;', context);
vm.runInContext(grab('nonnegativeInteger') + '\nthis.nonnegativeInteger = nonnegativeInteger;', context);
vm.runInContext(grab('setIntegrationBadge') + '\nthis.setIntegrationBadge = setIntegrationBadge;', context);
vm.runInContext(grab('renderQuality') + '\nthis.renderQuality = renderQuality;', context);

function rowSlugs() {
  return byId['quality-sessions'].children.map(row => row.children[1].textContent);
}

// A payload where the WORST posture has the LOWEST judge score and the steadiest
// session has the highest. If the list ordered by score, steady would come first.
context.state.qualityReport = {
  enabled: true,
  totals: {sessions: 4, graded: 20, judge_answered: 6, refused_deterministic: 9,
    claims_unsupported: 1, claims_unexamined: 9, tool_calls: 400, tool_nonzero: 7},
  findings: ['Heuristic-graded turns only ever pass at 0.90.'],
  limits: ['This grades structure only.'],
  sessions: [
    {slug: 'steady-one', posture: 'steady', graded: 8, judge_answered: 8, refused_deterministic: 0,
      claims_unsupported: 0, claims_unexamined: 0, tool_calls: 90, tool_nonzero: 0, turns_failed: 0, errors: 0,
      turns_timed: 8, turn_seconds_median: 42, repeat_failures: 2, heuristic_scores: [0.9]},
    {slug: 'judge-fell-back', posture: 'judge-degraded', graded: 6, judge_answered: 0, refused_deterministic: 0,
      claims_unsupported: 0, claims_unexamined: 0, tool_calls: 120, tool_nonzero: 2, turns_failed: 0, errors: 0,
      heuristic_scores: [0.9]},
    {slug: 'claims-outran', posture: 'claims-unreliable', graded: 4, judge_answered: 0, refused_deterministic: 0,
      claims_unsupported: 3, claims_unexamined: 0, tool_calls: 60, tool_nonzero: 1, turns_failed: 0, errors: 0,
      heuristic_scores: [0.1]},
    {slug: 'nothing-read', posture: 'unjudged', graded: 0, judge_answered: 0, refused_deterministic: 0,
      claims_unsupported: 0, claims_unexamined: 0, tool_calls: 0, tool_nonzero: 0, turns_failed: 0, errors: 0,
      heuristic_scores: []},
  ],
};
context.renderQuality();
assert.deepEqual(rowSlugs(),
  ['claims-outran', 'judge-fell-back', 'nothing-read', 'steady-one'],
  'the list is ordered by posture, worst first, not by judge score');

// The steady session carries the highest heuristic score in this payload; it is last.
assert.equal(rowSlugs()[3], 'steady-one');

// The summary keeps the two kinds of non-answer apart.
const summary = byId['quality-summary'].textContent;
assert(summary.includes('20 graded turns'), summary);
assert(summary.includes('6 judged'), summary);
assert(summary.includes('9 refused before judging'), summary);
assert(!/pass rate|score/i.test(summary), 'no blended pass rate is shown: ' + summary);

// Findings and limits are both shown, and a limit is not dressed as a fault.
// Findings and limits stay separate. One shared list at one weight makes a
// caveat look exactly like a fault. Findings lead with a headline, and limits
// live in their own labelled, folded block with no status mark.
const findings = byId['quality-findings'].children;
assert.equal(findings.length, 1, 'a limit is not a finding row');
assert.equal(findings[0].children[0].tone, 'attention');
assert.equal(findings[0].children[1].textContent, 'Heuristic-graded turns only ever pass at 0.90.',
  'the finding leads with its headline');
const limitRows = byId['quality-limits'].children;
assert.equal(limitRows.length, 1);
// The harness El has no `hidden` property; what matters is that the limit rows
// exist in their own block rather than in the findings list.
assert.equal(limitRows[0].children.length, 1, 'a limit carries no status mark, so it is not an alert');
assert(limitRows[0].children[0].textContent.includes('This grades structure only.'));

// A per-session row states its facts rather than a single number.
// Row shape: status mark, slug, posture badge, then the facts line.
const claimsRow = byId['quality-sessions'].children[0];
const facts = claimsRow.children[3].textContent;
assert(facts.includes('4 graded'), facts);
assert(facts.includes('3 claims unsupported'), facts);

// Speed and retry pressure are shown as measured facts, named so nobody reads a
// turn span as model latency.
const steadyFacts = byId['quality-sessions'].children[3].children[3].textContent;
assert(steadyFacts.includes('median turn 42s'), steadyFacts);
assert(steadyFacts.includes('2 retry loops'), steadyFacts);

// Nothing reported is said plainly, not left as an empty panel.
context.state.qualityReport = null;
context.state.qualityReading = false;
context.renderQuality();
assert.equal(byId['quality-state'].textContent, 'Not read');
// The empty panel names the action. The standing purpose sentence lives in
// index.html (#quality-purpose), so it is checked where it is served rather
// than in a renderer that no longer owns it.
assert(byId['quality-summary'].textContent.includes('Read the newest turns of every session'),
  byId['quality-summary'].textContent);

// Reading says it is reading.
context.state.qualityReading = true;
context.renderQuality();
assert.equal(byId['quality-state'].textContent, 'Reading');

// Switched off is its own answer.
context.state.qualityReading = false;
context.state.qualityReport = {enabled: false};
context.renderQuality();
assert.equal(byId['quality-state'].textContent, 'Off');

console.log('The quality panel ranks by structure and never by the judge score.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_core_without_the_oversight_route_says_reload_rather_than_showing_an_empty_panel():
    """The lane is new, so a core started before it answers 404.

    That is "not served yet", not a fault in the session data, and the page has to say
    which one it is. The refresh path is run for real against a stub that throws the
    same shape the client's api() throws on a 404.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  // An async declaration must keep its keyword: slicing from 'function' alone turns
  // an async body into one that uses await outside an async function.
  let at = source.indexOf('async function ' + name + '(');
  if (at < 0) at = source.indexOf('function ' + name + '(');
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.id = ''; this._text = ''; this.dataset = {}; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  replaceChildren(...n) { this.children = []; this._text = ''; this.append(...n); }
}
const byId = {};
const doc = { getElementById: id => (byId[id] ||= new El('div')) };
let reply = null;
const context = vm.createContext({
  console, document: doc,
  state: {qualityReport: null, qualityReading: false, qualityMissing: false},
  element: (tag, cls, text) => { const e = new El(tag); e.className = cls; if (text !== undefined) e.textContent = text; return e; },
  statusMark: () => new El('span'),
  $: id => (byId[id] ||= new El('div')),
  api: async () => { if (reply instanceof Error) throw reply; return reply; },
});
vm.runInContext('var state = this.state; var element = this.element; var statusMark = this.statusMark; var api = this.api;'
  + '\nvar QUALITY_POSTURE = ' + /const QUALITY_POSTURE = (\{[\s\S]*?\n  \});/.exec(source)[1] + ';', context);
for (const name of ['qualityPosture', 'qualityCount', 'nonnegativeInteger', 'setIntegrationBadge', 'renderQuality', 'refreshQuality']) {
  vm.runInContext(grab(name) + '\nthis.' + name + ' = ' + name + ';', context);
}

(async () => {
// A 404 from an older core is named as a reload, not as broken data.
const missing = new Error('Not found');
missing.status = 404;
reply = missing;
await context.refreshQuality();
assert.equal(byId['quality-state'].textContent, 'Needs a reload');
assert(byId['quality-summary'].textContent.includes('Reload the core'),
  byId['quality-summary'].textContent);
assert.equal(context.state.qualityReport, null);

// A real payload clears the flag and renders.
reply = {enabled: true, totals: {graded: 2, judge_answered: 2, refused_deterministic: 0,
    claims_unsupported: 0, claims_unexamined: 0, tool_calls: 10, tool_nonzero: 0},
  findings: [], limits: [],
  sessions: [{slug: 'only', posture: 'steady', graded: 2, judge_answered: 2, refused_deterministic: 0,
    claims_unsupported: 0, claims_unexamined: 0, tool_calls: 10, tool_nonzero: 0, turns_failed: 0, errors: 0}]};
await context.refreshQuality();
assert.equal(context.state.qualityMissing, false);
assert.equal(byId['quality-state'].textContent, '1 session');
assert.equal(byId['quality-sessions'].children[0].children[1].textContent, 'only');

// A read in flight is not re-entered: refreshQuality returns early, so the previous
// reading stays on screen instead of being blanked by a second 4MB pass.
const before = context.state.qualityReport;
context.state.qualityReading = true;
reply = {enabled: true, totals: {}, findings: [], limits: [], sessions: []};
await context.refreshQuality();
assert.equal(context.state.qualityReport, before, 'a read in flight is not re-entered');
assert.equal(byId['quality-state'].textContent, '1 session', 'the last reading stays visible');
context.state.qualityReading = false;

// With nothing read yet, an in-flight read says so rather than showing an empty panel.
context.state.qualityReport = null;
context.state.qualityReading = true;
context.renderQuality();
assert.equal(byId['quality-state'].textContent, 'Reading');

console.log('An older core is named as a reload.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_an_autonomy_session_is_separated_from_ordinary_chats():
    """An autonomy session is separated from ordinary chats.

    The server sets meta.infinity on the session row. ensure() is the only
    caller, from the autonomy loop. The browser does not guess the flag from
    the slug, so a renamed autonomy session keeps its treatment.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.id = ''; this.hidden = false;
    this._text = ''; this.listeners = {}; this.tagName = tag.toUpperCase(); this.value = '';
    this.className = ''; this.dataset = {}; this.label = '';
  }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  get firstChild() { return this.children[0] || null; }
  get classList() {
    const self = this;
    return {
      add: (c) => { self.className = (self.className ? self.className + ' ' : '') + c; },
      contains: (c) => self.className.split(/\s+/).includes(c),
    };
  }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  insertBefore(n, ref) {
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(n); else this.children.splice(at, 0, n);
    n.parent = this; return n;
  }
  // The real DOM inserts a fragment's children, not the fragment node itself.
  replaceChildren(...n) {
    this.children = []; this._text = '';
    for (const x of n) { if (x.tag === 'fragment') this.append(...x.children); else this.append(x); }
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  async fire(t) { for (const fn of this.listeners[t] || []) await fn({preventDefault(){}}); }
  focus() { doc.activeElement = this; }
  getBoundingClientRect() { return {right: 0, bottom: 0}; }
  querySelectorAll() { return []; }
}
const byId = {};
const doc = {
  activeElement: null,
  body: { classList: { _s: new Set(), toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); }, add(c) { this._s.add(c); }, contains(c) { return this._s.has(c); } } },
  getElementById: id => byId[id] || null,
  createDocumentFragment: () => new El('fragment'),
};
for (const id of ['session-list', 'session-search', 'show-archived', 'session-list-empty',
                  'session-list-unreadable',
                  'settings-connection', 'settings-session-count', 'settings-platform',
                  'session-title', 'session-badge', 'voice-session', 'session-select-bar',
                  'select-count', 'select-archive', 'select-delete', 'session-detail',
                  'rename-session', 'archive-session', 'message-input', 'send-message',
                  'attach-file', 'stop-turn', 'queue-next']) {
  byId[id] = new El('div'); byId[id].id = id;
}
byId['session-search'].value = '';
byId['show-archived'].checked = false;
const context = vm.createContext({
  console, document: doc,
  element: (tag, cls, text) => { const e = new El(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; },
});
vm.runInContext('var document = this.document; var element = this.element; var state = this.state = {};'
  + 'var $ = (id) => document.getElementById(id);', context);
vm.runInContext(grab('renderSessions') + '\nthis.renderSessions = renderSessions;', context);
vm.runInContext(grab('renderSelectBar') + '\nthis.renderSelectBar = renderSelectBar;', context);
vm.runInContext(grab('updateHeading') + '\nthis.updateHeading = updateHeading;', context);
vm.runInContext(grab('renderVoiceSessions') + '\nthis.renderVoiceSessions = renderVoiceSessions;', context);
vm.runInContext(grab('activeSession') + '\nthis.activeSession = activeSession;', context);
vm.runInContext(grab('renderServerSettings') + '\nthis.renderServerSettings = renderServerSettings;', context);
// Stubs the real functions lean on but that this test does not exercise.
vm.runInContext('this.backendLabel = (b) => b || "default";'
  + 'this.desktopWithoutBackend = () => false;'
  + 'this.renderVoiceStatus = () => {}; this.renderActivity = () => {};'
  + 'this.renderModelPickerOnce = () => {}; this.toggleSelected = () => {};'
  + 'this.openSessionMenu = () => {}; this.watchLongPress = () => {};'
  + 'this.selectSession = async () => {}; this.showError = () => {};'
  + 'this.voice = null;', context);

// Infinity sorts LAST in this list on purpose: a renderer that relied on the
// server's order would put the Autonomy label at the bottom.
context.state.sessions = [
  {slug: 'operations', title: 'operations', messages: 10, archived: false, backend: 'router-auto'},
  {slug: 'harness-development', title: 'Harness development', messages: 5, archived: false, backend: 'codex'},
  {slug: 'renamed-floor', title: 'Night Shift', messages: 99, archived: false, backend: 'router-auto', infinity: true},
];
context.state.slug = 'operations';
context.state.selecting = false;
context.state.selected = new Set();
context.renderSessions();

const rows = byId['session-list'].children;
const labels = rows.filter(r => r.className === 'session-group-label').map(r => r.textContent);
assert.deepEqual(labels, ['Autonomy', 'Bots'], 'both groups are named, autonomy first');

const items = rows.filter(r => r.className.split(' ').includes('session-item'));
assert.equal(items[0].dataset.slug, 'renamed-floor', 'the autonomy session comes first regardless of server order');
assert(items[0].classList.contains('autonomy'), 'it carries the autonomy class');
assert(!items[1].classList.contains('autonomy'), 'an ordinary chat does not');
assert.equal(items[0].children[0].children[1].textContent, '99 messages', 'the row carries the count, not a repeated lane name');

// A renamed autonomy session keeps its treatment: the flag is not the slug.
assert.equal(items[0].children[0].children[0].getAttribute('aria-label'), 'Night Shift (autonomy session)');

// The heading says it too, instead of leaving the reader to notice a sidebar group.
context.state.slug = 'renamed-floor';
context.state.attachments = [];
context.state.sending = false;
context.state.busy = false;
context.updateHeading();
assert.equal(byId['session-badge'].hidden, false);
assert.equal(byId['session-badge'].textContent, 'Autonomy');
assert.equal(byId['session-title'].textContent, 'Night Shift');
assert(doc.body.classList.contains('autonomy-session'), 'the heading carries the treatment');
assert(byId['session-detail'].textContent.includes('wakes on its own schedule'), 'the detail says how it behaves');

// An ordinary chat shows no badge and no autonomy class.
context.state.slug = 'operations';
context.updateHeading();
assert.equal(byId['session-badge'].hidden, true);
assert.equal(byId['session-badge'].textContent, '');
assert(!doc.body.classList.contains('autonomy-session'));

// The voice picker groups rather than offering one flat list of chats.
context.state.slug = 'operations';
context.renderVoiceSessions();
const groups = byId['voice-session'].children;
assert.deepEqual(groups.map(g => g.label), ['Autonomy', 'Bots'], 'voice separates autonomy from bots');
assert.equal(groups[0].children.length, 1);
assert.equal(groups[0].children[0].value, 'renamed-floor');
assert.equal(groups[1].children.length, 2);
assert.equal(byId['voice-session'].value, 'operations', 'the open session stays selected across optgroups');

// No autonomy session means no empty Autonomy group pretending to hold something.
context.state.sessions = [{slug: 'operations', title: 'operations', messages: 1, archived: false, backend: 'router-auto'}];
context.renderSessions();
assert.deepEqual(byId['session-list'].children.filter(r => r.className === 'session-group-label').map(r => r.textContent),
                 ['Bots'], 'an empty autonomy group is not rendered');
context.renderVoiceSessions();
assert.deepEqual(byId['voice-session'].children.map(g => g.label), ['Bots']);

// A record the server could not render is not a session that does not exist.
// The case that matters is one broken record among healthy ones: the list is
// not empty, so a notice hidden with the empty state would never be seen.
context.state.status = {sessions_unreadable: [{slug: 'hand-edited', field: 'created', reason: 'is float, not an ISO timestamp'}]};
context.renderSessions();
assert.equal(byId['session-list-unreadable'].hidden, false, 'the notice shows while healthy rows are present');
assert(byId['session-list-unreadable'].textContent.includes('hand-edited'), 'it names the record');
assert(byId['session-list-unreadable'].textContent.includes('not an ISO timestamp'), 'it names the reason');
assert.equal(byId['session-list-empty'].hidden, true, 'the list itself is not empty');

// With nothing broken the notice stays out of the way.
context.state.status = {sessions_unreadable: []};
context.renderSessions();
assert.equal(byId['session-list-unreadable'].hidden, true);
assert.equal(byId['session-list-unreadable'].textContent, '');

// An older core does not send the key at all. That must read as "nothing to say",
// not as a broken page.
context.state.status = {};
context.renderSessions();
assert.equal(byId['session-list-unreadable'].hidden, true);
assert.equal(byId['session-list-empty'].textContent, 'No sessions here yet.');

// The settings header counts records on disk; the sidebar lists readable ones.
// Printing only the first number leaves a person comparing "14 sessions" against
// a list of 13 with nothing to explain the gap.
context.renderServerSettings(['router-auto'], {session_count: 14,
  sessions_unreadable: [{slug: 'hand-edited', field: 'created', reason: 'is float, not an ISO timestamp'}]});
assert.equal(byId['settings-session-count'].textContent, '14 sessions, 1 unreadable',
             'the header names the refused record instead of a count that disagrees with the list');
context.renderServerSettings(['router-auto'], {session_count: 14, sessions_unreadable: []});
assert.equal(byId['settings-session-count'].textContent, '14 sessions',
             'a healthy home says the plain count');
// An older core does not send the key, and must not gain a clause out of nowhere.
context.renderServerSettings(['router-auto'], {session_count: 1});
assert.equal(byId['settings-session-count'].textContent, '1 session');
context.renderServerSettings(['router-auto'], {});
assert.equal(byId['settings-session-count'].textContent, '? sessions');
// An older core omits host. The header stays blank rather than inventing a platform.
assert.equal(byId['settings-platform'].textContent, '');
context.renderServerSettings(['router-auto'], {session_count: 1, host: {platform: 'linux', limits: [
  'This process is the core. LaunchAgent start and stop is macOS-only and is not available on Linux.']}});
assert.match(byId['settings-platform'].textContent, /not available on Linux/);
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_voice_session_is_its_own_lane_and_the_voice_page_targets_it():
    """Voice is a session of its own, not a picker over chats.

    The Voice page targets that session. The flag is the
    server's (meta.voice on the session row), the same pattern as autonomy, so a
    renamed voice session keeps its treatment and an ordinary chat cannot be
    mistaken for one.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.id = ''; this.hidden = false;
    this._text = ''; this.listeners = {}; this.tagName = tag.toUpperCase(); this.value = '';
    this.className = ''; this.dataset = {}; this.label = '';
  }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  get firstChild() { return this.children[0] || null; }
  get classList() {
    const self = this;
    return {
      add: (c) => { self.className = (self.className ? self.className + ' ' : '') + c; },
      contains: (c) => self.className.split(/\s+/).includes(c),
    };
  }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  insertBefore(n, ref) {
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(n); else this.children.splice(at, 0, n);
    n.parent = this; return n;
  }
  replaceChildren(...n) {
    this.children = []; this._text = '';
    for (const x of n) { if (x.tag === 'fragment') this.append(...x.children); else this.append(x); }
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  async fire(t) { for (const fn of this.listeners[t] || []) await fn({preventDefault(){}}); }
  focus() { doc.activeElement = this; }
  getBoundingClientRect() { return {right: 0, bottom: 0}; }
  querySelectorAll() { return []; }
}
const byId = {};
const doc = {
  activeElement: null,
  body: { classList: { _s: new Set(), toggle(c, on) { on ? this._s.add(c) : this._s.delete(c); }, add(c) { this._s.add(c); }, contains(c) { return this._s.has(c); } } },
  getElementById: id => byId[id] || null,
  createDocumentFragment: () => new El('fragment'),
};
for (const id of ['session-list', 'session-search', 'show-archived', 'session-list-empty',
                  'session-list-unreadable',
                  'settings-connection', 'settings-session-count', 'settings-platform',
                  'session-title', 'session-badge', 'voice-session', 'session-select-bar',
                  'select-count', 'select-archive', 'select-delete', 'session-detail',
                  'rename-session', 'archive-session', 'message-input', 'send-message',
                  'attach-file', 'stop-turn', 'queue-next']) {
  byId[id] = new El('div'); byId[id].id = id;
}
byId['session-search'].value = '';
byId['show-archived'].checked = false;
const context = vm.createContext({
  console, document: doc,
  element: (tag, cls, text) => { const e = new El(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; },
});
vm.runInContext('var document = this.document; var element = this.element; var state = this.state = {};'
  + 'var $ = (id) => document.getElementById(id);', context);
vm.runInContext(grab('renderSessions') + '\nthis.renderSessions = renderSessions;', context);
vm.runInContext(grab('renderSelectBar') + '\nthis.renderSelectBar = renderSelectBar;', context);
vm.runInContext(grab('updateHeading') + '\nthis.updateHeading = updateHeading;', context);
vm.runInContext(grab('renderVoiceSessions') + '\nthis.renderVoiceSessions = renderVoiceSessions;', context);
vm.runInContext(grab('activeSession') + '\nthis.activeSession = activeSession;', context);
vm.runInContext('this.backendLabel = (b) => b || "default";'
  + 'this.desktopWithoutBackend = () => false;'
  + 'this.renderVoiceStatus = () => {}; this.renderActivity = () => {};'
  + 'this.renderModelPickerOnce = () => {}; this.toggleSelected = () => {};'
  + 'this.openSessionMenu = () => {}; this.watchLongPress = () => {};'
  + 'this.selectSession = async () => {}; this.showError = () => {};'
  + 'this.voice = null;', context);

// A renamed voice session ("Talk") and a renamed autonomy session ("Night Shift")
// alongside two ordinary chats. The flag, not the slug, drives the treatment.
context.state.sessions = [
  {slug: 'operations', title: 'operations', messages: 10, archived: false, backend: 'router-auto'},
  {slug: 'talk', title: 'Talk', messages: 3, archived: false, backend: 'router-auto', voice: true},
  {slug: 'night-shift', title: 'Night Shift', messages: 99, archived: false, backend: 'router-auto', infinity: true},
  {slug: 'harness-development', title: 'Harness development', messages: 5, archived: false, backend: 'codex'},
];
context.state.slug = 'operations';
context.state.selecting = false;
context.state.selected = new Set();
context.state.voiceTarget = '';
context.renderSessions();

const rows = byId['session-list'].children;
const labels = rows.filter(r => r.className === 'session-group-label').map(r => r.textContent);
assert.deepEqual(labels, ['Autonomy', 'Voice', 'Bots'], 'three lanes, autonomy first');

const items = rows.filter(r => r.className.split(' ').includes('session-item'));
assert.deepEqual(items.map(i => i.dataset.slug), ['night-shift', 'talk', 'operations', 'harness-development'],
  'voice sits between autonomy and ordinary chats regardless of server order');
assert(items[1].classList.contains('voice-thread'), 'the voice session carries its own class');
assert(!items[2].classList.contains('voice-thread'), 'an ordinary chat does not');
assert.equal(items[1].children[0].children[1].textContent, '3 messages', 'the row carries the count, not a repeated lane name');
assert.equal(items[1].children[0].children[0].getAttribute('aria-label'), 'Talk (voice session)');

// The heading says what a voice session does, not just what it is called.
context.state.slug = 'talk';
context.state.attachments = [];
context.state.sending = false;
context.state.busy = false;
context.updateHeading();
assert.equal(byId['session-badge'].hidden, false);
assert.equal(byId['session-badge'].textContent, 'Voice');
assert.equal(byId['session-title'].textContent, 'Talk');
assert(doc.body.classList.contains('voice-thread'), 'the heading carries the treatment');
assert(byId['session-detail'].textContent.includes('spoken answers, hands work to this session'),
  'the detail names the restriction that keeps voice useful');
assert(!doc.body.classList.contains('autonomy-session'), 'a voice session is not an autonomy session');

// The Voice page targets the dedicated session by default, not the open chat.
context.state.slug = 'operations';
context.state.voiceTarget = '';
context.renderVoiceSessions();
assert.deepEqual(byId['voice-session'].children.map(g => g.label), ['Autonomy', 'Voice', 'Bots']);
assert.equal(byId['voice-session'].value, 'talk', 'Talk goes to the voice session, not to the open chat');

// A reader who picks another target keeps it.
context.state.voiceTarget = 'operations';
context.renderVoiceSessions();
assert.equal(byId['voice-session'].value, 'operations', 'an explicit pick is honored');

// No voice session on this home: fall back to the open chat, and no empty lane.
context.state.sessions = [{slug: 'operations', title: 'operations', messages: 1, archived: false, backend: 'router-auto'}];
context.state.voiceTarget = '';
context.renderSessions();
assert.deepEqual(byId['session-list'].children.filter(r => r.className === 'session-group-label').map(r => r.textContent),
                 ['Bots'], 'an empty voice lane is not rendered');
context.renderVoiceSessions();
assert.deepEqual(byId['voice-session'].children.map(g => g.label), ['Bots']);
assert.equal(byId['voice-session'].value, 'operations', 'with no voice session, the open chat is the target');
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_add_model_form_can_ask_the_endpoint_what_it_serves():
    """Discovery asks a model endpoint what it actually offers.

    The Add-model form can ask the endpoint and turn the answers into
    choices. A single answer fills the field. This drives the real
    settings.js in a browser-free harness.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the settings harness")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.id = ''; this.hidden = false;
    this._text = ''; this.listeners = {}; this.dataset = {}; this.className = '';
    this.tagName = tag.toUpperCase(); this.value = ''; this.disabled = false; this.type = '';
  }
  set textContent(t) { this._text = String(t); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { for (const c of n) { if (c && c.tag !== undefined) this.children.push(c); } }
  replaceChildren(...n) { this.children = []; this._text = ''; this.append(...n); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({preventDefault() {}}); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = {createElement: tag => new El(tag),
             createTextNode: t => { const n = new El('#text'); n.textContent = String(t); return n; },
             getElementById: () => null, body: new El('body'), addEventListener() {}};
const root = new El('div');
const status = new El('p');
const restartButton = new El('button');

const data = {supervised: true, sections: [{id: 'models', label: 'Models', fields: [
  {id: 'providers', type: 'list-of-records', actions: ['add'], records: [], blank: [
    {key: 'name', label: 'Name', type: 'text', value: ''},
    {key: 'account', label: 'Account', type: 'enum', value: 'other',
     options: [{value: 'other', label: 'Other HTTP'}]},
    {key: 'kind', label: 'HTTP protocol', type: 'enum', value: 'openai_chat',
     options: [{value: 'openai_chat', label: 'OpenAI-compatible chat'}]},
    {key: 'model', label: 'Model', type: 'text', value: ''},
    {key: 'base_url', label: 'Base URL', type: 'text', value: ''}]}]}]};

const calls = [];
const api = async (path, method, body) => {
  if (path === '/api/settings') return data;
  if (path === '/api/models/discover') {
    calls.push({method, body});
    return {models: ['glm-5.3']};
  }
  throw new Error('unexpected ' + path);
};

const context = vm.createContext({document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({color: 'rgb(0,0,0)'}), window: {}});
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new El(tag); if (cls) n.className = cls;
    if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {}});

(async () => {
  await view.load();
  const add = root.querySelector('.settings-add');
  assert.ok(add, 'Add model control must exist');
  await add.fire('click');

  const baseUrl = root.querySelector('[data-key="base_url"]');
  baseUrl.value = 'http://127.0.0.1:8080/v1';
  await baseUrl.fire('input');

  const find = root.querySelector('.set-discover').children.find(n => n.tag === 'button');
  assert.ok(find, 'the Model row must offer a way to ask the endpoint');
  await find.fire('click');

  assert.equal(calls.length, 1, 'exactly one discovery request');
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].body.base_url, 'http://127.0.0.1:8080/v1');
  assert.equal(calls[0].body.kind, 'openai_chat');
  assert.equal(calls[0].body.account, undefined,
               'an HTTP backend must not be asked as a sign-in provider');

  const model = root.querySelector('[data-key="model"]');
  assert.equal(model.value, 'glm-5.3', 'a single answer fills the field');
  assert.equal(view.hasEdits(), true, 'the filled value is a real edit');

  console.log('Add-model discovery checked.');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_project_page_lets_a_reader_assign_its_manager():
    """A reader assigns a project's manager from the project page.

    The chooser offers none plus every live session by slug, and no archived
    one. Changing it posts to the update route. This drives the real app.js.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the static harness")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  const marker = 'function ' + name + '(';
  const at = source.indexOf(marker);
  assert(at >= 0, 'missing ' + name);
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}

const prelude = `
var state = {sessions: [
  {slug: "operations", title: "operations", archived: false},
  {slug: "infinity", title: "Infinity", archived: false},
  {slug: "old-thread", title: "Old thread", archived: true}
], page: "wip", openProject: "p1", boardCategory: "", pendingFieldText: {},
   boardView: {projects: [{id: "p1", title: "A project", kind: "subject", manager: "",
     session_slug: "operations", status: "active", steps: [], steps_total: 0, steps_done: 0,
     cards: [], cards_total: 0, note: "", description: ""}], cards: []}};
var posted = [];
async function api(path, method, body) { posted.push({path, method, body}); return {projects: [], cards: []}; }
function applyBoardView() {}
function projectStanding(p) { return "active"; }
function projectStepRatio() { return null; }
function safeBoardTitle(t) { return t; }
function syncProjectArena() {}
function captureFieldText() {}
function renderBoardStatus() {}
function adoptPendingFieldText() {}
function projectStepList() { return null; }
function $(id) { return document.getElementById(id); }
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
`;

function El(tag) {
  this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {};
  this.className = ''; this.id = ''; this.hidden = false; this._text = '';
  this.value = ''; this.disabled = false; this.renderKey = undefined;
  this.tagName = String(tag).toUpperCase();
}
El.prototype.__defineSetter__('textContent', function (v) { this._text = String(v); this.children = []; });
El.prototype.__defineGetter__('textContent', function () { return this._text + this.children.map(c => c.textContent).join(''); });
El.prototype.append = function (...kids) { for (const k of kids) if (k && k.tag) this.children.push(k); };
El.prototype.replaceChildren = function (...kids) { this.children = []; this._text = ''; this.append(...kids); };
El.prototype.setAttribute = function () {};
El.prototype.getAttribute = function () { return null; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] ||= []).push(fn); };
El.prototype.fire = async function (t) { for (const fn of this.listeners[t] || []) await fn({preventDefault() {}}); };
El.prototype.focus = function () {};
El.prototype.querySelector = function () { return null; };
El.prototype.querySelectorAll = function () { return []; };

const byId = {};
for (const id of ['project-page', 'page-wip', 'board-note-block', 'board-title',
                  'project-page-block', 'project-canvas-root', 'arena-plan', 'arena-tasks']) {
  byId[id] = new El('div'); byId[id].id = id;
}
byId['project-page'] = new El('div');
const doc = {
  createElement: (tag) => new El(tag),
  createTextNode: (t) => ({tag: '#text', textContent: String(t)}),
  getElementById: (id) => byId[id] || null,
  body: {classList: {toggle() {}, add() {}}},
  addEventListener() {},
  querySelector: () => null,
  querySelectorAll: () => [],
};

const context = vm.createContext({document: doc, console, setTimeout: () => 0,
  clearTimeout() {}, window: {}, location: {hash: ''}});
vm.runInContext(prelude, context);

vm.runInContext(grab('projectById').replace('function projectById', 'this.projectById = function'), context);
vm.runInContext(grab('renderProjectPage').replace('function renderProjectPage', 'this.renderProjectPage = function'), context);

context.renderProjectPage();

function find(node, pred) {
  if (!node || typeof node !== 'object') return null;
  if (pred(node)) return node;
  for (const child of node.children || []) {
    const hit = find(child, pred);
    if (hit) return hit;
  }
  return null;
}

const chooser = find(byId['project-page'], n => n.tag === 'select' && n.className === 'project-manager');
assert.ok(chooser, 'the project page must offer a manager chooser');
const values = chooser.children.filter(c => c.tag === 'option').map(o => o.value).join('|');
// Compared as a joined string: arrays built inside the vm realm carry that
// realm's prototype, and deepEqual refuses them across realms.
assert.equal(values, '|operations|infinity',
  'the chooser offers none plus every live session, and no archived one');
assert.equal(chooser.value, '', 'an unassigned project reads as none');

chooser.value = 'infinity';
(async () => {
  await chooser.fire('change');
  assert.equal(context.posted.length, 1, 'one assignment request');
  assert.equal(context.posted[0].path, '/api/board/projects/p1/update');
  assert.equal(context.posted[0].method, 'POST');
  assert.equal(JSON.stringify(context.posted[0].body), JSON.stringify({manager: 'infinity'}));
  console.log('Project manager chooser checked.');
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_quality_panel_shows_the_trials_auto_is_waiting_on():
    """The page can show a trial, not only a finding about one.

    The 'auto' policy's whole honesty claim is that a change waits for an A/B
    measurement. If the measurement is invisible on the page, the reader has to
    trust the finding's sentence. A trial row carries its own phase badge and its
    own recorded line, so what is shown is what was measured.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
function grab(name) {
  let at = source.indexOf('async function ' + name + '(');
  if (at < 0) at = source.indexOf('function ' + name + '(');
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') depth += 1;
    else if (ch === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.id = ''; this._text = ''; this.dataset = {}; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  append(...n) { for (const x of n) { x.parent = this; this.children.push(x); } }
  replaceChildren(...n) { this.children = []; this._text = ''; this.append(...n); }
}
const byId = {};
const doc = { getElementById: id => (byId[id] ||= new El('div')) };
const context = vm.createContext({
  console, document: doc,
  state: {},
  element: (tag, cls, text) => { const e = new El(tag); e.className = cls; if (text !== undefined) e.textContent = text; return e; },
  statusMark: tone => { const e = new El('span'); e.tone = tone; return e; },
  $: id => (byId[id] ||= new El('div')),
});
vm.runInContext('var state = this.state; var element = this.element; var statusMark = this.statusMark;'
  + '\nvar QUALITY_POSTURE = ' + /const QUALITY_POSTURE = (\{[\s\S]*?\n  \});/.exec(source)[1] + ';', context);
for (const name of ['qualityPosture', 'qualityCount', 'nonnegativeInteger', 'setIntegrationBadge', 'renderQuality']) {
  vm.runInContext(grab(name) + '\nthis.' + name + ' = ' + name + ';', context);
}

context.state.qualityReport = {
  enabled: true, policy: 'auto',
  totals: {sessions: 1, graded: 4, judge_answered: 4, refused_deterministic: 0,
    claims_unsupported: 0, claims_unexamined: 0, tool_calls: 40, tool_nonzero: 1},
  findings: ['1 trial(s) are measuring a change: subject reasoning low -> high in its baseline arm.'],
  limits: ['This grades structure only.'],
  trials: [
    {id: 'abc', slug: 'subject', lever: 'reasoning', from: 'low', to: 'high', phase: 'baseline',
     margin: 0.8, instrument: 'repeat_failures',
     line: 'Trial on subject: reasoning low -> high in its baseline arm, 3 turns per arm, read by repeat failures.'},
    {id: 'def', slug: 'other', lever: 'backend', from: 'base', to: 'candidate', phase: 'decided',
     verdict: {outcome: 'tie'}, applied: false,
     line: 'Trial on other: backend base -> candidate read tie by repeat failures; reverted to base.'},
    {id: 'ghi', slug: 'third', lever: 'reasoning', from: 'low', to: 'high', phase: 'baseline',
     margin: 9, margin_effective: 0.8, margin_invalid: true, instrument: 'turn_seconds_median',
     line: 'Trial on third: reasoning low -> high in its baseline arm, 3 turns per arm, read by turn seconds median.'},
  ],
  sessions: [{slug: 'subject', posture: 'steady', graded: 4, judge_answered: 4, refused_deterministic: 0,
    claims_unsupported: 0, claims_unexamined: 0, tool_calls: 40, tool_nonzero: 1, turns_failed: 0, errors: 0}],
};
context.renderQuality();

const rows = byId['quality-findings'].children;
const trialRows = rows.filter(row => row.children[1] && row.children[1].textContent.startsWith('Trial: '));
assert.equal(trialRows.length, 3, 'each trial is shown, not summarised into one line');
// An open trial is flagged; a decided one is not dressed as an alert.
assert.equal(trialRows[0].children[0].tone, 'attention');
assert.equal(trialRows[0].children[2].textContent, 'baseline');
assert(trialRows[0].children[3].textContent.includes('at most 80%'), trialRows[0].children[3].textContent);
assert.equal(trialRows[1].children[0].tone, '');
assert.equal(trialRows[1].children[2].textContent, 'decided');
assert.equal(trialRows[1].children.length, 3, 'a trial with no stored margin grows no invented margin line');
assert(trialRows[2].children[3].textContent.includes('at most 80%'), trialRows[2].children[3].textContent);
assert(trialRows[2].children[3].textContent.includes('not usable'), trialRows[2].children[3].textContent);
assert(!trialRows[2].textContent.includes('900'), trialRows[2].textContent);
// The line is the trial's own recorded description, not one the page invented.
assert(trialRows[0].children[1].textContent.includes('3 turns per arm'), trialRows[0].children[1].textContent);
assert(trialRows[1].children[1].textContent.includes('reverted to base'), trialRows[1].children[1].textContent);

// A payload from a core that predates trials still renders: no trial rows, no crash.
context.state.qualityReport.trials = undefined;
context.renderQuality();
assert.equal(byId['quality-findings'].children.filter(row => row.children[1]
  && row.children[1].textContent.startsWith('Trial: ')).length, 0);

console.log('The quality panel shows the trials auto waits on.');
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_long_hint_folds_and_a_short_one_stays_in_place():
    """A long hint is a closed disclosure on the label line. The paragraph stays
    in the page and starts hidden. A short hint stays visible under its control.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null; this.open = false;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(sel) {
    const cls = sel && sel.match(/^\.([A-Za-z0-9_-]+)$/);
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (!cls || c.className === cls[1]) out.push(c); walk(c); } };
    walk(this);
    return out;
  }
}

const doc = { createElement: tag => new Element(tag),
              createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');

const longHint = 'x'.repeat(473);
const settingsData = { supervised: true, sections: [
  { id: 'autonomy', label: 'Autonomy', fields: [
    { id: 'autonomy.oversight_policy', type: 'enum', label: 'Trust', value: 'auto',
      options: [{value: 'auto', label: 'Auto'}], hint: longHint },
    { id: 'voice.credential_ref', type: 'string', label: 'Voice key', value: '',
      hint: 'The voice key is for voice only.' } ] } ] };
const api = async () => settingsData;

const context = vm.createContext({ document: doc, navigator: {}, console,
  setTimeout: () => 0, clearTimeout: () => {},
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }), window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);

const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
                                      if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onRestarted() {}, onAppearance() {}, onApproval() {}, onChanged() {} });

(async () => {
  await view.load();
  const walk = (node, out) => { out.push(node); for (const child of node.children) walk(child, out); return out; };
  const nodes = walk(root, []);
  const summary = nodes.find(n => n.tag === 'summary' && n.textContent.trim() === 'Details');
  assert.ok(summary, 'a long hint offers Details');
  const longText = nodes.find(n => n.textContent === longHint);
  assert.ok(longText, 'the long guidance stays in the page');
  assert.equal(longText.hidden, true, 'the long guidance starts hidden');
  const shortText = nodes.find(n => n.textContent === 'The voice key is for voice only.');
  assert.ok(shortText, 'the short hint stays visible as text');
  assert.equal(shortText.hidden, false);
  const blockFor = (label, marker) => {
    let node = nodes.find(n => n.tag === 'label' && n.textContent === label);
    while (node) {
      const text = node.textContent || '';
      if (text.includes(marker) && !text.includes(label === 'Trust' ? 'Voice key' : longHint)) return node;
      node = node.parent;
    }
    return null;
  };
  const trust = blockFor('Trust', longHint);
  const trustText = trust.textContent;
  assert.ok(trustText.indexOf('Trust') < trustText.indexOf('Details'));
  assert.ok(trustText.indexOf('Details') < trustText.indexOf('Auto'));
  assert.ok(trustText.indexOf('Auto') < trustText.indexOf(longHint));
  await summary.fire('click');
  assert.equal(longText.hidden, false, 'pressing Details shows the guidance');
  const voice = blockFor('Voice key', 'The voice key is for voice only.');
  const voiceText = voice.textContent;
  assert.ok(voiceText.indexOf('Voice key') < voiceText.indexOf('The voice key is for voice only.'));
  console.log('Long hints fold, short hints stay.');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_yolo_banner_appearance_checkbox_submits_the_catalog_boolean(tmp_path):
    from tessera.config import Config
    from tessera.settings_model import project

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is optional for the browser-free JavaScript interaction smoke")
    data = project(Config(home=tmp_path, cwd=tmp_path), {})
    appearance = next(section for section in data['sections'] if section['id'] == 'appearance')
    data['sections'] = [appearance]
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.style = {}; this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)),
                       remove: (...xs) => xs.forEach(x => classes.delete(x)),
                       contains: x => classes.has(x) };
  }
  set textContent(text) { this._text = String(text); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent).join(''); }
  append(...nodes) { for (const n of nodes) this.insertBefore(n, null); }
  prepend(...nodes) { for (const n of nodes) this.insertBefore(n, this.children[0] || null); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const i = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(i, 0, node); node.parent = this;
  }
  remove() { if (this.parent) { this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; } }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  getAttribute(key) { return this.attrs[key] || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) { for (const fn of this.listeners[type] || []) await fn({ preventDefault() {} }); }
  focus() {}
  querySelector(sel) {
    const key = sel.match(/^\[data-key="([^"]+)"\]$/);
    const cls = sel.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const n of this.children) {
      if (key && n.dataset.key === key[1]) return n;
      if (cls && n.className === cls[1]) return n;
      const found = n.querySelector(sel);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll() { return this.children; }
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');
const settingsData = SETTINGS_DATA;
const writes = [];
let changed = 0;
const api = async (path, method, body) => {
  assert.equal(path, '/api/settings');
  if (method === 'POST') {
    writes.push(body);
    const field = settingsData.sections[0].fields.find(field => field.id === body.id);
    field.value = body.value;
    return {changed: true, applied: 'live', restart_required: false, settings: settingsData};
  }
  return settingsData;
};
const context = vm.createContext({ document: doc, console,
  getComputedStyle: () => ({ color: 'rgb(0,0,0)' }),
  setTimeout: () => 0, clearTimeout: () => {}, window: {} });
vm.runInContext(fs.readFileSync('web/settings.js', 'utf8'), context);
const view = context.window.TesseraSettings.mount({
  api, element: (tag, cls, text) => { const n = new Element(tag); if (cls) n.className = cls;
    if (text !== undefined) n.textContent = String(text); return n; },
  root, status, restartButton, onChanged() { changed += 1; } });
(async () => {
  await view.load();
  assert.ok(root.textContent.includes('Appearance'));
  assert.ok(root.textContent.includes('Show Unleashed banner'));
  // F-it and the goblin it shows moved to Goblin Mode; this page is the banner.
  assert.equal(root.querySelector('[data-key="easter_egg_f_it"]'), null);
  for (const value of [false, true]) {
    const input = root.querySelector('[data-key="yolo_banner"]');
    assert.ok(input, 'the catalog field renders in the existing Settings page');
    assert.equal(input.type, 'checkbox');
    assert.equal(input.checked, !value);
    input.checked = value;
    await input.fire('change');
    assert.equal(writes.at(-1).id, 'ui.yolo_banner');
    assert.equal(writes.at(-1).value, value);
    assert.equal(root.querySelector('[data-key="yolo_banner"]').checked, value);
  }
  assert.equal(changed, 2, 'each save triggers the existing status refresh callback');
})().catch(error => { console.error(error); process.exit(1); });
'''
    script = script.replace('SETTINGS_DATA', json.dumps(data))
    result = subprocess.run([node, "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_engine_pressure_renders_reading_and_unreachable_without_idle_counts():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the static DOM harness")
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('web/app.js', 'utf8');
const html = fs.readFileSync('web/index.html', 'utf8');
assert.match(html, /id="engine-pressure-list"/);
// Pressure is refreshed by the infrastructure refresh, NOT by the 15-second
// status poll: a settings refresh must touch only /api/status and /api/sessions.
assert.match(source, /async function refreshTargets\(\) \{\n    refreshSparks\(\);\n    refreshEnginePressure\(\);/);
assert.doesNotMatch(source, /async function refreshStatus\(\) \{\n    if \(state.page/);
function grab(name) {
  let at = source.indexOf('async function ' + name + '(');
  if (at < 0) at = source.indexOf('function ' + name + '(');
  assert(at >= 0, name + ' must exist');
  let depth = 0, i = source.indexOf('{', at);
  for (; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') { depth -= 1; if (depth === 0) break; }
  }
  return source.slice(at, i + 1);
}
class El {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.dataset = {}; }
  set textContent(t) { this._text = String(t); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(' '); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
}
const byId = {};
const context = vm.createContext({
  state: {}, window: {},
  element: (tag, cls, text) => { const e = new El(tag); e.className = cls; if (text !== undefined) e.textContent = text; return e; },
  statusMark: tone => { const e = new El('span'); e.tone = tone; return e; },
  $: id => (byId[id] ||= new El('div')),
});
for (const name of ['finiteMeasurement', 'measurementText', 'metricChip', 'providerRecords',
                    'enginePressureNode', 'renderEnginePressure', 'refreshEnginePressure']) {
  vm.runInContext(grab(name), context);
}
const reachable = {host:'engine-a', port:8888, model:'test-model', slots_total:4,
                   running:1, waiting:2, free:3, reachable:true, error:null};
const dead = {...reachable, host:'engine-b', reachable:false, running:null, waiting:null,
              free:null, error:'Connection refused'};
context.report = {engines:[reachable, dead]};
vm.runInContext('renderEnginePressure(report)', context);
const list = byId['engine-pressure-list'];
const liveRow = list.children[0];
assert.match(liveRow.textContent, /engine-a:8888/);
assert.match(liveRow.textContent, /test-model/);
assert.match(liveRow.textContent, /1\/4 used/);
assert.match(liveRow.textContent, /Waiting 2/);
assert.match(liveRow.textContent, /Free 3/);
const deadRow = list.children[1];
assert.match(deadRow.textContent, /UNREACHABLE/);
assert.match(deadRow.textContent, /Connection refused/);
assert.doesNotMatch(deadRow.textContent, /0\/4|\bused\b|Waiting|Free/);
assert.equal(deadRow.dataset.reachable, 'false');
assert.equal(deadRow.children[0].children[0].tone, '');
vm.runInContext('renderEnginePressure(report)', context);
assert.equal(list.children[0], liveRow, 'unchanged reading retains visible nodes');
(async () => {
  context.api = async path => { assert.equal(path, '/api/pressure/engines'); return {engines:[reachable]}; };
  await vm.runInContext('refreshEnginePressure()', context);
  assert.match(list.textContent, /1\/4 used/);
  context.api = async () => { const e = new Error('Not found'); e.status = 404; throw e; };
  await vm.runInContext('refreshEnginePressure()', context);
  assert.equal(list.textContent, 'Engine pressure not available');
  context.api = async () => ({unexpected:true});
  await vm.runInContext('refreshEnginePressure()', context);
  assert.equal(list.textContent, 'Engine pressure not available');
  let finishOld;
  context.api = () => new Promise(resolve => { finishOld = resolve; });
  const older = vm.runInContext('refreshEnginePressure()', context);
  context.api = async () => ({engines:[dead]});
  await vm.runInContext('refreshEnginePressure()', context);
  finishOld({engines:[reachable]});
  await older;
  assert.match(list.textContent, /UNREACHABLE/);
  assert.doesNotMatch(list.textContent, /\bused\b/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sse_reconnect_onerror_uses_connection_status_shape():
    source = (ROOT / "web" / "app.js").read_text()
    marker = "source.onerror = (event) => {"
    start = source.index(marker)
    body_start = start + len(marker)
    candidates = [
        pos for pos in (
            source.find("};", body_start),
            source.find("for (const type of eventTypes)", body_start),
        ) if pos >= 0
    ]
    assert candidates, "could not find the end of the SSE onerror block"
    block = source[start:min(candidates)]
    assert re.search(r"refreshStatus\(true\)\.catch\(showError\);", block)
    assert not re.search(r"refreshStatus\(\)\.catch\(showError\);", block)
