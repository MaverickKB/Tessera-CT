const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(resolve(__dirname, '..', 'panels.js'), 'utf8');
const html = readFileSync(resolve(__dirname, '..', 'index.html'), 'utf8');

class Element {
  constructor(id) { this.id = id; this.attributes = {}; this.listeners = {}; this.textContent = id === 'nav-link' ? ' Chat sessions ' : ''; this.title = ''; this.children = []; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name]; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'title') this.title = ''; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { this.listeners.click(); }
  focus() { doc.activeElement = this; }
  contains(node) { return node === this || this.children.includes(node); }
}
let doc;

function setup({width = 1280, stored, storage} = {}) {
  const ids = {};
  for (const id of ['navigation-collapse', 'navigation-collapse-label', 'sessions-collapse', 'sessions-expand', 'sessions-panel', 'session-search']) ids[id] = new Element(id);
  ids['sessions-panel'].children.push(ids['session-search']);
  const link = new Element('nav-link');
  const classes = new Set();
  const store = storage || {data: stored === undefined ? {} : {'tessera-panels': stored}, getItem(k) { return this.data[k] ?? null; }, setItem(k, v) { this.data[k] = v; }};
  const queries = [];
  const media = query => {
    const limit = Number(/max-width: (\d+)px/.exec(query)[1]);
    const entry = {query, limit, matches: width <= limit, listeners: [], addEventListener(_t, fn) { this.listeners.push(fn); }};
    queries.push(entry);
    return entry;
  };
  doc = {
    activeElement: null,
    body: {dataset: {navigationCollapseBelow: '1100', sessionsCollapseBelow: '900', drawerBelow: '760'},
      classList: {toggle(name, on) { on ? classes.add(name) : classes.delete(name); }, add(name) { classes.add(name); }, contains: name => classes.has(name)}},
    getElementById: id => ids[id],
    querySelectorAll: () => [link],
    listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
  };
  const context = {window: {}, document: undefined};
  vm.runInNewContext(source, context);
  const panels = context.window.TesseraPanels.create({document: doc, storage: store, matchMedia: media, modifierLabel: '⌘'});
  const resize = next => { width = next; for (const q of queries) { q.matches = width <= q.limit; q.listeners.forEach(fn => fn()); } };
  const key = (k, extra = {}) => { const event = {key: k, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, prevented: false, preventDefault() { this.prevented = true; }, ...extra}; doc.listeners.keydown(event); return event; };
  return {panels, ids, classes, store, link, resize, key};
}

test('markup gives both panels labelled, controlling toggles and loads the module before app.js', () => {
  assert(html.indexOf('/static/panels.js') > 0 && html.indexOf('/static/panels.js') < html.indexOf('/static/app.js'));
  for (const [id, controls] of [['navigation-collapse', 'app-navigation'], ['sessions-collapse', 'sessions-panel'], ['sessions-expand', 'sessions-panel']]) {
    const tag = new RegExp('<button id="' + id + '"[^>]*>').exec(html)[0];
    assert(tag.includes('aria-controls="' + controls + '"') && tag.includes('aria-expanded=') && tag.includes('aria-label=') && tag.includes('type="button"'), id);
  }
});

test('buttons toggle state, aria-expanded and labels, and persist across launches', () => {
  const a = setup();
  assert(!a.classes.has('navigation-collapsed') && !a.classes.has('sessions-collapsed'));
  assert.equal(a.ids['navigation-collapse'].getAttribute('aria-expanded'), 'true');
  a.ids['navigation-collapse'].click();
  assert(a.classes.has('navigation-collapsed'));
  assert.equal(a.ids['navigation-collapse'].getAttribute('aria-expanded'), 'false');
  assert.equal(a.ids['navigation-collapse'].getAttribute('aria-label'), 'Expand menu');
  assert.equal(a.link.title, 'Chat sessions', 'icon-only links get a tooltip');
  a.ids['sessions-collapse'].click();
  assert(a.classes.has('sessions-collapsed'));
  for (const id of ['sessions-collapse', 'sessions-expand']) assert.equal(a.ids[id].getAttribute('aria-expanded'), 'false');
  assert.deepEqual(JSON.parse(a.store.data['tessera-panels']), {navigation: true, sessions: true});
  const b = setup({stored: a.store.data['tessera-panels']});
  assert(b.classes.has('navigation-collapsed') && b.classes.has('sessions-collapsed'), 'restored on next launch');
  b.ids['sessions-expand'].click();
  assert(!b.classes.has('sessions-collapsed'));
  assert.equal(b.link.title, 'Chat sessions');
  b.ids['navigation-collapse'].click();
  assert.equal(b.link.title, '', 'expanded links drop the tooltip');
});

test('Cmd/Ctrl+B toggles navigation and Cmd/Ctrl+Shift+B toggles sessions; other chords are ignored', () => {
  const f = setup();
  assert(f.key('b', {metaKey: true}).prevented);
  assert(f.classes.has('navigation-collapsed'));
  assert(f.key('B', {ctrlKey: true, shiftKey: true}).prevented);
  assert(f.classes.has('sessions-collapsed'));
  for (const extra of [{}, {metaKey: true, altKey: true}, {shiftKey: true}, {ctrlKey: true, isComposing: true}]) {
    assert(!f.key('b', extra).prevented);
  }
  assert(!f.key('k', {metaKey: true}).prevented);
  assert(f.classes.has('navigation-collapsed') && f.classes.has('sessions-collapsed'));
});

test('collapsing Sessions with focus inside moves focus to the reopen button', () => {
  const f = setup();
  doc.activeElement = f.ids['session-search'];
  f.ids['sessions-collapse'].click();
  assert.equal(doc.activeElement, f.ids['sessions-expand']);
  f.ids['sessions-expand'].click();
  assert.equal(doc.activeElement, f.ids['sessions-collapse']);
});

test('narrow windows auto-collapse without overwriting the saved preference', () => {
  const f = setup({width: 1280});
  f.resize(1000);
  assert(f.classes.has('navigation-collapsed') && !f.classes.has('sessions-collapsed'));
  f.resize(850);
  assert(f.classes.has('sessions-collapsed'));
  f.ids['navigation-collapse'].click();
  assert(!f.classes.has('navigation-collapsed'), 'temporary expand while narrow');
  assert.equal(f.store.data['tessera-panels'], undefined, 'auto state is never persisted');
  f.resize(1280);
  assert(!f.classes.has('navigation-collapsed') && !f.classes.has('sessions-collapsed'), 'saved expanded preference returns');
  const mobile = setup({width: 600});
  assert(!mobile.key('b', {metaKey: true}).prevented, 'mobile drawers own the panels');
});

test('blocked or corrupt storage falls back to expanded defaults', () => {
  const broken = {getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }};
  const f = setup({storage: broken});
  assert(!f.classes.has('navigation-collapsed'));
  f.ids['navigation-collapse'].click();
  assert(f.classes.has('navigation-collapsed'));
  const g = setup({stored: '{not json'});
  assert(!g.classes.has('sessions-collapsed'));
  const h = setup({stored: '{"navigation":"yes","sessions":1}'});
  assert(!h.classes.has('navigation-collapsed') && !h.classes.has('sessions-collapsed'));
});

test('empty chat starts with the sentence and Start a session; actions wait for a session, the composer stays typeable (#29)', () => {
  const css = readFileSync(resolve(__dirname, '..', 'app.css'), 'utf8');
  assert(/<body class="no-session"/.test(html));
  const rule = /body\.no-session \.session-heading[^{]*\{[^}]*\}/.exec(css)[0];
  for (const part of ['.session-actions', '.inspector-control', '.empty-state p']) assert(rule.includes('body.no-session ' + part), part);
  assert(!css.includes('body.no-session .composer-area'), 'sending with no session starts one, so the composer is never hidden');
  assert(rule.includes('display: none'));
  assert.equal((/<section id="empty-state"[\s\S]*?<\/section>/.exec(html)[0].match(/<button/g) || []).length, 1);
  assert(/\.voice-controls|voice-controls/.test(/<div class="composer-area">[\s\S]*?<\/form>/.exec(html)[0]), 'voice strip lives inside the composer area');
});
