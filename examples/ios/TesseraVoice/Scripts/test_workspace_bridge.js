// Exercise the shipped injection without starting a microphone or provider call.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const swift = fs.readFileSync(path.join(__dirname, '../TesseraVoice/UI/NativeWorkspaceBridge.swift'), 'utf8');
const rawString = swift.split('static let script = #"""')[1];
const source = rawString.split('"""#')[0];

function fixture(origin = 'https://workspace.example', token = '') {
  const events = {};
  const messages = [];
  const requests = [];
  const nodes = {};
  for (const id of ['voice-toggle', 'voice-page-toggle', 'voice-mute', 'voice-page-state', 'voice-pick', 'voice-mode', 'voice-mic', 'voice-ptt']) {
    nodes[id] = {id, textContent: '', title: '', disabled: false, value: id === 'voice-pick' ? 'Rex' : ''};
  }
  const location = new URL(origin + '/#view=chat&session=operations');
  class BrowserRequest extends Request {
    constructor(input, options) {
      super(typeof input === 'string' ? new URL(input, location.href) : input, options);
    }
  }
  const window = {
    tesseraConnection: {origin: 'https://workspace.example', token},
    webkit: {messageHandlers: {tesseraNative: {postMessage: value => messages.push(value)}}},
    fetch: async request => { requests.push(request); return new Response('{}'); }
  };
  const document = {
    body: {},
    getElementById: id => nodes[id],
    addEventListener: (name, handler, capture) => { events[name] = {handler, capture}; }
  };
  const history = {};
  for (const method of ['pushState', 'replaceState']) {
    history[method] = (_, unused, url) => { location.href = new URL(url, location.href).href; };
  }
  vm.runInNewContext(source, {
    window, document, history, location, URL, URLSearchParams, Request: BrowserRequest,
    Headers, Response, MutationObserver: class { observe() {} },
    addEventListener: (name, handler) => { events[name] = {handler}; }
  });
  if (events.DOMContentLoaded) events.DOMContentLoaded.handler();
  function click(id) {
    const event = {
      target: {closest: () => nodes[id]},
      prevented: false, stopped: false,
      preventDefault() { this.prevented = true; },
      stopImmediatePropagation() { this.stopped = true; }
    };
    events.click.handler(event);
    return event;
  }
  return {events, messages, requests, nodes, history, window, click};
}

test('voice buttons intercept browser audio acquisition and bind selected session', () => {
  const f = fixture();
  const event = f.click('voice-toggle');
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(f.events.click.capture, true);
  const action = f.messages.find(message => message.action === 'voice');
  assert.equal(action.slug, 'operations');
  assert.equal(action.voice, 'Rex');
});

test('navigation reports selection without ending or replacing a native call', () => {
  const f = fixture();
  f.window.tesseraNativeState({running: true, muted: false, label: 'Listening', slug: 'operations'});
  f.history.pushState(null, '', '#view=wip&session=planning');
  f.history.replaceState(null, '', '#view=settings&session=planning');
  assert.equal(f.messages.filter(message => message.action === 'voice').length, 0);
  assert.equal(f.messages.at(-1).slug, 'planning');
  assert.equal(f.nodes['voice-toggle'].textContent, 'End native call');
  assert.equal(f.nodes['voice-page-state'].textContent, 'Listening · operations');
});

test('mute routes to native and web audio controls remain inactive', () => {
  const f = fixture();
  f.window.tesseraNativeState({running: true, muted: true, label: 'Muted', slug: 'operations'});
  f.click('voice-mute');
  assert.equal(f.messages.at(-1).action, 'mute');
  assert.equal(f.nodes['voice-mute'].textContent, 'Unmute');
  for (const id of ['voice-mode', 'voice-mic', 'voice-ptt']) assert.equal(f.nodes[id].disabled, true);
});

test('credentials apply only to same-origin fetch and preserve request data and cancellation', async () => {
  const f = fixture(undefined, 'test-cred');
  const controller = new AbortController();
  await f.window.fetch('/api/sessions/test/messages', {method: 'POST', body: '{"text":"hello"}', signal: controller.signal});
  const request = f.requests[0];
  assert.equal(request.method, 'POST');
  assert.equal(await request.text(), '{"text":"hello"}');
  assert.equal(request.headers.get('Authorization'), 'Bearer test-cred');
  controller.abort();
  assert.equal(request.signal.aborted, true);
  await f.window.fetch('https://external.example/file');
  assert.equal(f.requests[1], 'https://external.example/file');
});

test('foreign documents receive no native bridge handlers or credential wrapper', () => {
  const f = fixture('https://external.example', 'test-cred');
  assert.equal(f.window.tesseraNativeState, undefined);
  assert.equal(f.events.click, undefined);
  assert.equal(f.messages.length, 0);
});
