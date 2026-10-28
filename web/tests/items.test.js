const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const context = {window: {}};
vm.runInNewContext(readFileSync(resolve(__dirname, '..', 'items.js'), 'utf8'), context);
const {describe} = context.window.TesseraItems;
const plain = (value) => JSON.parse(JSON.stringify(value));

test('a memory recall note gets a one-line header and readable layers', () => {
  const layers = [{id: '0159', timestamp: '2026-10-16T19:51:57Z', author: 'tessera', type: 'discovery', body: 'Turn 6\n\nUser words: TensorFold'}];
  const item = plain(describe({kind: 'note', meta: {needs_memory: 0.9}, text: 'Memory recall: ' + JSON.stringify({success: true, layers})}));
  assert.equal(item.label, 'Memory recall');
  assert.equal(item.summary, '1 layer');
  assert.deepEqual(item.body, {kind: 'layers', layers: [{head: 'discovery · tessera · 2026-10-16T19:51:57Z', text: 'Turn 6\n\nUser words: TensorFold'}]});
});

test('a memory_note tool call shows its note as the summary', () => {
  const item = plain(describe({kind: 'tool', text: JSON.stringify({name: 'memory_note', arguments: {text: 'TensorFold = Ash Hart\'s runtime', kind: 'fact'}})}));
  assert.equal(item.label, 'Memory note');
  assert.equal(item.summary, 'TensorFold = Ash Hart\'s runtime');
  assert.deepEqual(item.body.fields, [['text', 'TensorFold = Ash Hart\'s runtime'], ['kind', 'fact']]);
});

test('results summarise scalar fields, flag blocks, and long text is cut to one line', () => {
  assert.equal(describe({kind: 'echo', meta: {}, text: '{"request_id": "abc", "queued": false}'}).summary, 'request_id: abc, queued: false');
  assert.equal(describe({kind: 'echo', meta: {blocked: true}, text: 'Blocked: risk'}).label, 'Blocked');
  const long = describe({kind: 'echo', meta: {}, text: 'x'.repeat(500)}, 50).summary;
  assert.equal(long.length, 50);
  assert.ok(long.endsWith('…'));
});

test('tool arguments that hold JSON strings are shown formatted', () => {
  const item = plain(describe({kind: 'tool', text: JSON.stringify({name: 'mesh_run', arguments: {params: '{"command": "ls"}'}})}));
  assert.equal(item.label, 'mesh_run');
  assert.deepEqual(item.body.fields, [['params', '{\n  "command": "ls"\n}']]);
});

test('conversation turns are not items', () => {
  for (const kind of ['user', 'agent', 'work']) assert.equal(describe({kind, text: 'hi'}), null);
});

test('a recall clipped mid-layer keeps the whole layers and says it was clipped', () => {
  const whole = {id: '1', type: 'discovery', author: 'tessera', timestamp: 't1', body: 'braces { and "quotes" stay'};
  const broken = JSON.stringify({id: '2', type: 'note', body: 'x'.repeat(40)});
  const text = 'Memory recall: {"success": true, "layers": [' + JSON.stringify(whole) + ', ' + broken.slice(0, 20) + '\n[clipped]\n' + broken.slice(-10) + '], "count": 2}';
  const item = plain(describe({kind: 'note', meta: {}, text}));
  assert.equal(item.summary, '1 layer (clipped)');
  assert.deepEqual(item.body.layers, [{head: 'discovery · tessera · t1', text: 'braces { and "quotes" stay'}]);
});

test('the session-start skill shelf note is one compact collapsed item', () => {
  const text = 'Skill shelf: 2 skills. skill_load a name before acting on it.\n- desk-ops: Desk.\n- infra-check: Hosts.';
  const item = plain(describe({kind: 'note', meta: {label: 'skill-shelf', count: 2}, text}));
  assert.equal(item.label, 'Skill shelf');
  assert.equal(item.summary, '2 skills on the shelf');
  assert.ok(JSON.stringify(item.body).includes('infra-check: Hosts.'));
});


test('memory rows preserve source text and only expose valid native tree coordinates', () => {
  const rows = context.window.TesseraItems.memoryRows('[x8-15 L3] Older decisions\n[must-know today] Keep the original\n[x1-2 L3] Unaligned');
  assert.equal(rows[0].level, 3);
  assert.equal(rows[0].index, 1);
  assert.equal(rows[0].text, 'Older decisions');
  assert.equal(rows[1].text, '[must-know today] Keep the original');
  assert.equal(rows[2].level, undefined);
});
