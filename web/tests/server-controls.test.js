const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function workspace(running = true) {
  const nodes = {};
  for (const id of ['server-controls', 'server-start', 'server-stop', 'server-status']) {
    nodes[id] = {
      hidden: true,
      disabled: true,
      textContent: '',
      listeners: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
    };
  }
  const service = {running, loaded: running, failure: '', changes: []};
  const window = {
    addEventListener() {},
    setInterval() {},
    TesseraServer: {
      async status() { return service; },
      async change(start) {
        service.changes.push(start);
        if (service.failure) throw new Error(service.failure);
        service.running = start;
        service.loaded = start;
      },
    },
  };
  const document = {getElementById(id) { return nodes[id]; }};
  const source = fs.readFileSync(path.join(__dirname, '..', 'desktop.js'), 'utf8');
  vm.runInNewContext(source, {window, document});
  return {nodes, service};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test('workspace controls stop and start without fetching from the stopped server', async () => {
  const {nodes, service} = workspace();
  await settle();
  assert.equal(nodes['server-controls'].hidden, false);
  assert.equal(nodes['server-start'].disabled, true);
  assert.equal(nodes['server-stop'].disabled, false);
  await nodes['server-stop'].listeners.click();
  assert.equal(nodes['server-status'].textContent, 'Stopped');
  assert.equal(nodes['server-start'].disabled, false);
  assert.equal(nodes['server-stop'].disabled, true);
  await nodes['server-start'].listeners.click();
  assert.equal(nodes['server-status'].textContent, 'Running');
  assert.deepEqual(service.changes, [false, true]);
});

test('the browser error guard is not treated as a system service', () => {
  const nodes = {};
  for (const id of ['server-controls', 'server-start', 'server-stop', 'server-status']) {
    nodes[id] = {hidden: true, disabled: true, textContent: '', listeners: {}};
  }
  const window = {TesseraShell: {bannerText() {}, isMissingObject() {}}};
  const document = {getElementById(id) { return nodes[id]; }};
  const source = fs.readFileSync(path.join(__dirname, '..', 'desktop.js'), 'utf8');
  vm.runInNewContext(source, {window, document});
  assert.equal(nodes['server-controls'].hidden, true);
  assert.equal(nodes['server-status'].textContent, '');
});

test('a shell without a system service says so instead of offering start and stop', async () => {
  const nodes = {};
  for (const id of ['server-controls', 'server-start', 'server-stop', 'server-status']) {
    nodes[id] = {hidden: true, disabled: true, textContent: '', listeners: {}};
  }
  const window = {TesseraShell: {platform: 'linux', service: 'child'}};
  const document = {getElementById(id) { return nodes[id]; }};
  const source = fs.readFileSync(path.join(__dirname, '..', 'desktop.js'), 'utf8');
  vm.runInNewContext(source, {window, document});
  assert.equal(nodes['server-controls'].hidden, false);
  assert.equal(nodes['server-start'].hidden, true);
  assert.equal(nodes['server-stop'].hidden, true);
  assert.match(nodes['server-status'].textContent, /not available on Linux/);
  assert.match(nodes['server-status'].textContent, /LaunchAgent/);
});

test('failed service operations stay visible and allow another attempt', async () => {
  const {nodes, service} = workspace(false);
  await settle();
  service.failure = 'Service could not start';
  await nodes['server-start'].listeners.click();
  assert.match(nodes['server-status'].textContent, /Service could not start/);
  assert.equal(nodes['server-start'].disabled, false);
  service.failure = '';
  await nodes['server-start'].listeners.click();
  assert.equal(nodes['server-status'].textContent, 'Running');
});
