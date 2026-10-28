const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8');

test('path-capable chat drop registers a path without uploading a payload', async () => {
  const start = source.indexOf('  async function uploadAttachment(');
  const end = source.indexOf('  $("attach-file").addEventListener', start);
  const listeners = {};
  const requests = [];
  const drops = [];
  const context = {
    window: { addEventListener: (name, handler) => listeners[name] = handler },
    document: { elementFromPoint: () => ({ closest: selector => selector === '#composer' }) },
    sessionPath: (slug, suffix) => '/api/sessions/' + slug + suffix,
    api: async (...args) => { requests.push(args); return { id: 'reference' }; },
    fetch: () => { throw new Error('Local drop must not upload bytes'); },
    addAttachmentFiles: files => drops.push(...files),
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  listeners['cairn-local-file-drop']({ detail: { x: 10, y: 20, paths: ['/local/large.txt'] } });
  assert.equal(drops[0].path, '/local/large.txt');
  assert.equal(drops[0].name, 'large.txt');
  assert.equal(drops[0].size, undefined);
  const result = await context.uploadAttachment('chat', drops[0]);
  assert.equal(result.id, 'reference');
  assert.equal(requests[0][0], '/api/sessions/chat/attachment-references');
  assert.equal(requests[0][1], 'POST');
  assert.equal(requests[0][2].path, '/local/large.txt');
});

test('ordinary browser files retain their upload transport', async () => {
  const start = source.indexOf('  async function uploadAttachment(');
  const end = source.indexOf('  window.addEventListener("cairn-local-file-drop"', start);
  let request;
  const file = { name: 'remote.txt', type: 'text/plain' };
  const context = {
    sessionPath: (slug, suffix) => suffix,
    headers: () => ({}), checkResponse: async () => {},
    fetch: async (url, options) => { request = { url, options }; return { json: async () => ({ id: 'upload' }) }; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  await context.uploadAttachment('chat', file);
  assert.equal(request.url, '/attachments');
  assert.equal(request.options.body, file);
});

test('canvas path drop registers metadata without reading or uploading a File', async () => {
  const context = { window: {} };
  let request;
  let node;
  context.fetch = async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ id: 'ref', name: 'local.txt', media_type: 'text/plain' }) };
  };
  context.crypto = { randomUUID: () => 'piece' };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../project-canvas.js'), 'utf8'), context);
  const canvas = {
    notice: {}, options: { session: () => 'chat' }, file: {},
    mutate: async patch => { node = patch.nodes[0]; return true; }, select: () => {}, center: () => {},
  };
  await context.window.TesseraProjectCanvas.ProjectCanvas.prototype.upload.call(canvas, {
    path: '/local/local.txt', name: 'local.txt', type: 'text/plain',
    text: () => { throw new Error('Must not read local content at drop'); },
  });
  assert.equal(request.url, '/api/sessions/chat/attachment-references');
  assert.deepEqual(JSON.parse(request.options.body), { path: '/local/local.txt' });
  assert.equal(node.attachment_id, 'ref');
  assert.equal(node.body, undefined);
});
