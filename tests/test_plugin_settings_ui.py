"""The Plugins row shows where a plugin came from, its last error, and a Check that posts."""
import shutil
import subprocess

import pytest

ROOT = __import__('pathlib').Path(__file__).resolve().parents[1]


def test_plugin_row_renders_source_error_and_check_posts():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is optional for the browser-free JavaScript interaction smoke')
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.value = ''; this.checked = false; this.hidden = false;
    this.disabled = false; this.className = ''; this._text = ''; this.parent = null;
    this.type = '';
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

function walk(node, found = []) {
  found.push(node);
  for (const child of node.children || []) walk(child, found);
  return found;
}

const doc = { createElement: tag => new Element(tag), createTextNode: t => { const n = new Element('#text'); n.textContent = String(t); return n; },
              getElementById: () => null, body: new Element('body'), addEventListener() {} };
const root = new Element('div');
const status = new Element('p');
const restartButton = new Element('button');
const posts = [];
const settingsData = { supervised: false, sections: [
  { id: 'permissions', label: 'Permissions', fields: [
    { id: 'permissions.mode', type: 'enum', label: 'Mode', value: 'safe' } ] },
  { id: 'plugins', label: 'Plugins', fields: [
    { id: 'mcp_servers', type: 'list-of-records', label: 'Installed plugins',
      hint: 'Each row is one plugin.',
      actions: ['add', 'edit', 'remove', 'check'],
      blank: [{ key: 'name', label: 'Name', type: 'string', value: '', hint: 'Short identifier.' }],
      records: [
        { name: 'clock', title: 'clock', status: 'Command',
          summary: 'Enabled. Session tools on. No token env. From config.toml [mcp_servers.clock].',
          source_label: 'Command', source: '/usr/bin/python /tmp/clock.py',
          connection: { ok: false, state: 'error', detail: 'McpError: MCP stdio start failed (FileNotFoundError)', last_error: 'McpError: MCP stdio start failed (FileNotFoundError)' },
          note: '',
          actions: ['edit', 'check', 'remove'],
          fields: [{ key: 'enabled', label: 'Enabled', type: 'bool', value: true, inline: true }] },
        { name: 'aethermind', title: 'AetherMind', status: 'Embedded', template: true,
          summary: 'External service did not validate',
          source_label: 'Active', source: 'Embedded AetherMind',
          connection: { ok: false, state: 'error', detail: 'URLError: refused', last_error: 'URLError: refused' },
          note: 'This row shows Embedded or External from the core status report. This row has no off switch.',
          actions: [],
          fields: [] },
        { name: 'missing', title: 'missing', status: 'Disabled',
          summary: 'Disabled. Session tools on. From config.toml [mcp_servers.missing].',
          source_label: 'Command', source: '/tmp/no-such-plugin',
          connection: { ok: null, state: 'disabled', detail: 'Disabled. Not connected.',
                        last_error: 'McpError: MCP stdio start failed (FileNotFoundError)' },
          actions: ['edit', 'check', 'remove'],
          fields: [{ key: 'enabled', label: 'Enabled', type: 'bool', value: false, inline: true }] },
        { name: 'memory', title: 'memory', status: 'Memory',
          summary: 'Serves AetherMind memory (mcp:memory). Session tools on.',
          source_label: 'Command', source: '/tmp/memory-plugin',
          connection: { ok: null, state: 'memory',
                        detail: 'Serves AetherMind memory (mcp:memory). The live memory process stays running.',
                        last_error: '' },
          note: 'memory serves AetherMind memory because aethermind.endpoints includes mcp:memory. AetherMind has no off switch.',
          actions: ['edit', 'check', 'remove'],
          fields: [{ key: 'enabled', label: 'Enabled', type: 'bool', value: true, inline: true, inert: true,
                     hint: 'memory serves AetherMind memory because aethermind.endpoints includes mcp:memory.' }] }
      ] }
  ] }
]};
let refuseAethermind = false;
const api = async (path, method, body) => {
  posts.push({ path, method, body });
  if (refuseAethermind && method === 'POST' && body && body.name === 'missing') {
    throw new Error('The plugin command is missing');
  }
  if (method === 'POST') return { settings: settingsData, changed: false, detail: body && body.action === 'check' ? 'Checked.' : 'Saved' };
  return settingsData;
};
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
      const text = root.textContent;
      assert.ok(text.includes('Plugins'), 'the Plugins section is in the navigation');
  assert.ok(text.includes('Add plugin'), text);
  assert.ok(text.includes('From config.toml [mcp_servers.clock]'));
  assert.ok(text.includes('Command: /usr/bin/python /tmp/clock.py'));
  assert.ok(text.includes('McpError: MCP stdio start failed (FileNotFoundError)'));
  assert.ok(text.includes('External service did not validate'));
  assert.ok(text.includes('no off switch'));
  assert.ok(!/\bPR\s*\d/.test(text));
  assert.ok(!/pull request/i.test(text));
  assert.ok(!text.includes('Optional'));
  assert.ok(text.includes('Active: Embedded AetherMind'));
  assert.ok(text.includes('URLError: refused'));
      assert.ok(text.includes('Last error: McpError: MCP stdio start failed (FileNotFoundError)'));
      assert.ok(text.includes('Serves AetherMind memory (mcp:memory)'));
      assert.ok(text.includes('aethermind.endpoints includes mcp:memory'));
      const nodes = walk(root);
      const locked = nodes.find(node => node.tag === 'input' && node.type === 'checkbox' && node.disabled);
      assert.ok(locked, 'the memory plugin Enabled box is locked');
      assert.equal(locked.checked, true);
      const postsBeforeLocked = posts.length;
      locked.checked = false;
      await locked.fire('change');
      assert.equal(posts.length, postsBeforeLocked, 'a locked Enabled box does not post');
      assert.equal(locked.getAttribute('aria-disabled'), 'true');
  const error = nodes.find(node => String(node.className || '').includes('plugin-error'));
  assert.ok(error, 'a real failure is marked as an error');
  const check = nodes.find(node => node.tag === 'button' && node.textContent === 'Check');
  assert.ok(check, 'Check is a control');
      await check.fire('click');
      const checked = posts.at(-1).body;
      assert.equal(checked.id, 'mcp_servers');
      assert.equal(checked.action, 'check');
      assert.equal(checked.name, 'clock');
      const box = nodes.find(node => node.tag === 'input' && node.type === 'checkbox' && node.checked === true);
      assert.ok(box, 'Enabled is a checkbox');
      box.checked = false;
      await box.fire('change');
      const edited = posts.at(-1).body;
      assert.equal(edited.id, 'mcp_servers');
      assert.equal(edited.action, 'edit');
      assert.equal(edited.name, 'clock');
      assert.equal(edited.record.enabled, false);
      refuseAethermind = true;
      const before = walk(root).filter(node => node.tag === 'input' && node.type === 'checkbox');
      assert.equal(before.length, 3, 'AetherMind has no on/off checkbox; the memory row keeps a locked one');
      assert.equal(before[1].checked, false, 'the missing plugin checkbox stays off');
      before[1].checked = true;
      await before[1].fire('change');
      const sent = posts.filter((item) => item.method === 'POST').at(-1).body;
      assert.equal(sent.name, 'missing');
      assert.equal(sent.record.enabled, true);
      assert.equal(status.textContent, 'The plugin command is missing');
      const after = walk(root).filter(node => node.tag === 'input' && node.type === 'checkbox');
      assert.equal(after[1].checked, false, 'a refused edit returns to the saved off state');
      const postsBeforeRemove = posts.length;
      const safeRemove = walk(root).find(node => node.tag === 'button' && node.textContent === 'Remove');
      await safeRemove.fire('click');
      assert.equal(posts.length, postsBeforeRemove, 'Safe mode asks before removing a plugin');
      assert.ok(root.textContent.includes('Remove now'));
      const cancel = walk(root).find(node => node.tag === 'button' && node.textContent === 'Cancel');
      await cancel.fire('click');
      settingsData.sections[0].fields[0].value = 'yolo';
      const yoloRemove = walk(root).find(node => node.tag === 'button' && node.textContent === 'Remove');
      await yoloRemove.fire('click');
      const removed = posts.filter((item) => item.method === 'POST').at(-1).body;
      assert.equal(removed.action, 'remove');
      assert.equal(removed.id, 'mcp_servers');
      assert.equal(removed.name, 'clock');
  console.log('plugin row shows source, error, check, and enable');
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, '-e', script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
