const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const start = app.indexOf('async function stopTurn(');
const end = app.indexOf('\n  $("message-input").addEventListener("input"', start);
const source = app.slice(start, end);

test('Stop acknowledges the click immediately and a late HTTP reply cannot replace Stopped', async () => {
  let resolve;
  const response = new Promise(done => { resolve = done; });
  const nodes = {'send-state': {textContent: ''}};
  const context = {
    state: {slug:'session',busy:true,stopping:null,live:null}, activity:{phase:''},
    $: id => nodes[id], api: () => response, sessionPath: () => '/interrupt',
    updateHeading(){}, renderActivity(){}, clearError(){}, showError(){}, clearLive(){},
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  const pending = context.stopTurn();
  assert.equal(nodes['send-state'].textContent, 'Cancelling now...');
  assert.equal(context.activity.phase, 'Cancelling now');
  context.state.busy = false;
  context.state.stopping = null;
  nodes['send-state'].textContent = 'Stopped.';
  resolve({interrupted:true});
  await pending;
  assert.equal(nodes['send-state'].textContent, 'Stopped.');
});
