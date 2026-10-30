const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function firstChat({failSend = false, failOpen = false, cryptoApi} = {}) {
  const calls = [];
  let nextId = 0;
  const source = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  const start = source.indexOf('  let setupTest = null;');
  const end = source.indexOf('  configView = window.TesseraSettings', start);
  assert.ok(start >= 0 && end > start);
  const context = {
    crypto: cryptoApi === undefined
      ? {randomUUID: () => `00000000-0000-4000-8000-${String(++nextId).padStart(12, '0')}`}
      : cryptoApi,
    sessionPath: (slug, suffix) => `/api/sessions/${slug}${suffix}`,
    async api(url, method, body) {
      calls.push([url, method, JSON.parse(JSON.stringify(body))]);
      if (url === '/api/sessions') return {slug: 'getting-started-' + nextId};
      if (failSend) { failSend = Number(failSend) - 1; throw new Error('Connection lost after submission'); }
      return {turn_id: body.client_request_id};
    },
    async refreshSessions() {},
    async selectSession(slug) {
      if (failOpen) { failOpen = false; throw new Error('Could not open chat'); }
      calls.push(['open', slug]);
    },
    setPage(page) { calls.push(['page', page]); },
  };
  vm.runInNewContext(source.slice(start, end), context);
  return {calls, open: backend => context.openSetupTest(backend)};
}

test('first-use testing sends a normal chat turn only on an explicit request', async () => {
  const {calls, open} = firstChat();
  assert.equal(calls.length, 0);
  await open('chosen-model');
  assert.deepEqual(calls[0], ['/api/sessions', 'POST', {title: 'Getting started', backend: 'chosen-model'}]);
  assert.equal(calls[1][0], '/api/sessions/getting-started-1/messages');
  assert.equal(calls[1][1], 'POST');
  assert.match(calls[1][2].text, /first Tessera connection test/);
  assert.match(calls[1][2].client_request_id, /^[a-f0-9-]{36}$/);
  assert.deepEqual(calls.slice(2), [['open', 'getting-started-1'], ['page', 'chat']]);
});

test('an uncertain submission retries the same request identity without creating another chat', async () => {
  const {calls, open} = firstChat({failSend: true});
  await assert.rejects(open('chosen-model'), /Connection lost/);
  await open('chosen-model');
  const submissions = calls.filter(call => call[0].endsWith('/messages'));
  assert.equal(calls.filter(call => call[0] === '/api/sessions').length, 1);
  assert.equal(submissions.length, 2);
  assert.equal(submissions[0][2].client_request_id, submissions[1][2].client_request_id);
});

test('failure opening an accepted test never sends it again on retry', async () => {
  const {calls, open} = firstChat({failOpen: true});
  await assert.rejects(open('chosen-model'), /Could not open chat/);
  await open('chosen-model');
  assert.equal(calls.filter(call => call[0].endsWith('/messages')).length, 1);
  assert.deepEqual(calls.at(-1), ['page', 'chat']);
});

test('LAN HTTP crypto without randomUUID keeps one UUID across all submission retries', async () => {
  let randomReads = 0;
  const cryptoApi = {
    getRandomValues(bytes) { randomReads += 1; bytes.fill(255); return bytes; },
  };
  const {calls, open} = firstChat({failSend: 2, cryptoApi});
  await assert.rejects(open('chosen-model'), /Connection lost/);
  await assert.rejects(open('chosen-model'), /Connection lost/);
  await open('chosen-model');
  await open('chosen-model');
  const submissions = calls.filter(call => call[0].endsWith('/messages'));
  assert.equal(calls.filter(call => call[0] === '/api/sessions').length, 1);
  assert.equal(submissions.length, 3);
  const ids = submissions.map(call => call[2].client_request_id);
  assert.deepEqual(ids, Array(3).fill('ffffffff-ffff-4fff-bfff-ffffffffffff'));
  assert.equal(randomReads, 1, 'retrying must not create another identity');
  assert.deepEqual(calls.at(-1), ['page', 'chat']);
});

test('a browser without crypto still creates a UUID-compatible retry identity', async () => {
  const {calls, open} = firstChat({failSend: true, cryptoApi: null});
  await assert.rejects(open('chosen-model'), /Connection lost/);
  await open('chosen-model');
  const submissions = calls.filter(call => call[0].endsWith('/messages'));
  const id = submissions[0][2].client_request_id;
  assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(submissions[1][2].client_request_id, id);
  assert.equal(calls.filter(call => call[0] === '/api/sessions').length, 1);
});
