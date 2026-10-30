const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(resolve(__dirname, '..', 'app.js'), 'utf8');
const start = source.indexOf('  function jevStatusLabel(report)');
const end = source.indexOf('  async function refreshStatus(', start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const label = report => context.jevStatusLabel(report);

test('Jev reports readiness independently from its enabled switch', () => {
  assert.equal(label({enabled: false, state: 'off'}), 'Off');
  assert.equal(label({enabled: true, state: 'not_configured'}), 'Not configured');
  assert.equal(label({enabled: true, state: 'ready'}), 'Ready');
  assert.equal(label({enabled: true, state: 'unavailable'}), 'Unavailable');
});

test('old or incomplete status cannot become a ready judge', () => {
  assert.equal(label({enabled: true}), 'Not reported');
  assert.equal(label({enabled: true, configured: true}), 'Not reported');
  assert.equal(label({enabled: true, configured: false}), 'Not configured');
  assert.equal(label(true), 'Not reported');
  assert.equal(label(false), 'Off');
  assert.equal(label({state: 'constructor'}), 'Not reported');
  assert.equal(label(null), 'Not reported');
});
