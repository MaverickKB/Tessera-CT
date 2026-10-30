const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attrs = {}; this.value = ''; this.hidden = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  querySelectorAll(tag) { return this.children.flatMap(n => [...(n.tag === tag ? [n] : []), ...n.querySelectorAll(tag)]); }
}
const request = {id: 'a'.repeat(32), name: 'provider-key', purpose: 'Configure the judge', state: 'pending', replacing: false};
function setup(api) {
  const root = new Element('section');
  const context = {window: {}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'credential-prompt.js'), 'utf8'), context);
  const panel = context.window.TesseraCredentialPrompt.mount({api, root, element(tag, cls, text) {
    const node = new Element(tag); node.className = cls || ''; node.textContent = text || ''; return node;
  }});
  return {root, panel};
}
function text(root) { return [root.textContent || '', ...root.children.map(text)].join(' '); }

test('a protected value only uses the dedicated POST and is cleared after save', async () => {
  const calls = [];
  const {root, panel} = setup(async (url, method, body) => {
    calls.push([url, method, body]);
    return method === 'POST' ? {state: 'saved'} : {requests: [request]};
  });
  await panel.refresh('orientation');
  const input = root.querySelectorAll('input')[0];
  assert.equal(input.type, 'password');
  assert.equal(input.autocomplete, 'new-password');
  input.value = 'synthetic-private-value';
  await root.querySelectorAll('form')[0].listeners.submit({preventDefault() {}});
  assert.equal(calls[1][0], '/api/sessions/orientation/credential-prompts');
  assert.equal(calls[1][2].value, 'synthetic-private-value');
  assert.equal(calls[1][2].request_id, request.id);
  assert.equal(calls.some(call => call[0].endsWith('/messages')), false);
  assert.equal(input.value, '');
  assert.equal(text(root).includes('synthetic-private-value'), false);
});

test('metadata refresh keeps the current masked draft and its submit handler alive', async () => {
  let writes = 0;
  const {root, panel} = setup(async (url, method) => {
    if (method === 'POST') { writes++; return {state: 'saved'}; }
    return {requests: [request]};
  });
  await panel.refresh('orientation');
  const input = root.querySelectorAll('input')[0];
  input.value = 'synthetic-private-value';
  await panel.refresh('orientation');
  assert.equal(root.querySelectorAll('input')[0], input);
  assert.equal(input.value, 'synthetic-private-value');
  await root.querySelectorAll('form')[0].listeners.submit({preventDefault() {}});
  assert.equal(writes, 1);
});

test('cancel submits no value and a session change clears a masked draft', async () => {
  const calls = [];
  const {root, panel} = setup(async (url, method, body) => {
    calls.push([url, method, body]);
    return method === 'POST' ? {state: 'cancelled'} : {requests: url.includes('/other/') ? [] : [request]};
  });
  await panel.refresh('orientation');
  const input = root.querySelectorAll('input')[0];
  input.value = 'synthetic-private-value';
  await root.querySelectorAll('button').find(n => n.textContent === 'Cancel').listeners.click();
  assert.equal(calls[1][2].cancel, true);
  assert.equal(calls[1][2].value, undefined);
  assert.equal(input.value, '');
  await panel.refresh('orientation');
  const next = root.querySelectorAll('input')[0];
  next.value = 'synthetic-private-value';
  await panel.refresh('other');
  assert.equal(next.value, '');
  assert.equal(root.hidden, true);
});

test('late metadata cannot reopen another session and storage errors never echo the value', async () => {
  let finish;
  const {root, panel} = setup(async url => {
    if (url.includes('/orientation/')) return new Promise(resolve => { finish = resolve; });
    return {requests: []};
  });
  const pending = panel.refresh('orientation');
  await panel.refresh('other');
  finish({requests: [request]});
  await pending;
  assert.equal(root.hidden, true);
  const failed = setup(async (url, method) => {
    if (method === 'POST') throw new Error('synthetic-private-value');
    return {requests: [request]};
  });
  await failed.panel.refresh('orientation');
  failed.root.querySelectorAll('input')[0].value = 'synthetic-private-value';
  await failed.root.querySelectorAll('form')[0].listeners.submit({preventDefault() {}});
  assert.equal(text(failed.root).includes('synthetic-private-value'), false);
  assert.equal(failed.root.querySelectorAll('input')[0].value, 'synthetic-private-value');
});
