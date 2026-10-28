const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
function source(name) {
  const start = app.indexOf('function ' + name + '(');
  let depth = 0;
  for (let i = app.indexOf('{', start); i < app.length; i++) {
    if (app[i] === '{') depth++;
    if (app[i] === '}' && --depth === 0) return app.slice(start, i + 1);
  }
  throw new Error('Missing function ' + name);
}
function harness() {
  const context = {window: {}, activity: {phase: '', tools: new Map(), recent: []}, renderActivity() {}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'items.js'), 'utf8'), context);
  vm.runInContext(source('observeActivity'), context);
  return context;
}
test('activity follows interleaved tool calls, failures, reasoning and response text', () => {
  const c = harness();
  c.observeActivity('turn_start', {});
  c.observeActivity('delta', {kind: 'thought', text: 'private reasoning'});
  assert.equal(c.activity.phase, 'Thinking');
  for (const id of ['a','b']) c.observeActivity('message', {kind:'tool', text:JSON.stringify({name:'read_file',arguments:{path:id}}), meta:{call_id:id}});
  c.observeActivity('message', {kind:'echo',text:'Denied',meta:{call_id:'a',success:false}});
  assert.equal(c.activity.tools.size, 1);
  assert.equal(c.activity.phase, 'Waiting for tool results');
  c.observeActivity('message', {kind:'echo',text:'contents',meta:{call_id:'b',success:true}});
  assert.equal(c.activity.phase, 'Waiting for model');
  c.observeActivity('delta', {kind:'text',text:'answer'});
  assert.equal(c.activity.phase, 'Writing');
  assert(!JSON.stringify(c.activity.recent).includes('private reasoning'));
  c.observeActivity('turn_end', {});
  assert.equal(c.activity.phase, 'Turn complete');
});
test('a model slot or throttle wait is the turn phase until work starts', () => {
  const c = harness();
  c.observeActivity('turn_start', {});
  c.observeActivity('delta', {kind: 'status', text: 'waiting for local slot'});
  assert.equal(c.activity.phase, 'waiting for local slot');
  c.observeActivity('delta', {kind: 'status', text: 'waiting for local throttle'});
  assert.equal(c.activity.phase, 'waiting for local throttle');
  c.observeActivity('delta', {kind: 'status', text: 'Running'});
  assert.equal(c.activity.phase, 'Running');
});
test('rejoining a turn resets pending tools and uses only the reported snapshot phase', () => {
  const c = harness();
  c.activity.tools.set('old','old work');
  c.observeActivity('resume', {busy:true,live:{thought:'thinking',text:'partial answer'}});
  assert.equal(c.activity.phase, 'Writing');
  assert.equal(c.activity.tools.size, 0);
  c.observeActivity('resume', {busy:true,live:{tool:'shell'}});
  assert.equal(c.activity.phase, 'Tool requested: shell');
});
